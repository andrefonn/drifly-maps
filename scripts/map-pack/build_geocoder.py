#!/usr/bin/env python3
"""
Build an ultra-fast, low-RAM SQLite FTS5 offline geocoding database
(`<region>_geocoder.db`) from `<region>.osm.pbf`.

Memory-optimized for large Brazilian states (runs comfortably on a 7 GB GitHub
Actions runner):
  - Pass 1 scans relations to find which way IDs belong to boundary/multipolygon
    relations, plus collects nodes (cities/suburbs/POIs/addresses).
  - Pass 2 uses libosmium C++ `locations=True, idx='flex_mem'` so non-boundary
    ways never store coordinates in Python dicts.
  - Grid-indexed point-in-polygon (`0.05 deg` cells) makes street->bairro lookup
    O(1) even on states with thousands of neighborhoods.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import osmium

STREET_PREFIX_RE = re.compile(
    r"^(rua|r\.|avenida|av\.|estrada|est\.|servidao|serv\.|rodovia|rod\.|"
    r"travessa|trav\.|alameda|al\.|praca|pca\.|largo|beco|viela|passagem|"
    r"contorno|acesso|marginal|via|sc-\d+|br-\d+|pa-\d+|sp-\d+|mg-\d+|pr-\d+|rs-\d+|ba-\d+)\s+",
    re.IGNORECASE,
)

HIGHWAY_RANK: dict[str, int] = {
    "motorway": 82,
    "trunk": 80,
    "primary": 76,
    "secondary": 72,
    "tertiary": 68,
    "unclassified": 56,
    "residential": 52,
    "living_street": 48,
    "pedestrian": 46,
    "motorway_link": 40,
    "trunk_link": 40,
    "primary_link": 40,
    "secondary_link": 38,
    "tertiary_link": 36,
    "service": 30,
    "track": 24,
}

PUBLIC_POI_RULES: list[tuple[str, str, str, int]] = [
    ("aeroway", "aerodrome", "airport", 96),
    ("aeroway", "terminal", "airport", 94),
    ("amenity", "bus_station", "terminal", 92),
    ("public_transport", "station", "terminal", 88),
    ("railway", "station", "terminal", 88),
    ("amenity", "hospital", "hospital", 92),
    ("shop", "mall", "mall", 90),
    ("amenity", "university", "university", 84),
    ("amenity", "college", "university", 76),
    ("amenity", "townhall", "public", 82),
    ("amenity", "courthouse", "public", 78),
    ("amenity", "police", "public", 76),
    ("amenity", "fire_station", "public", 74),
    ("amenity", "marketplace", "public", 78),
    ("leisure", "stadium", "stadium", 82),
    ("leisure", "sports_centre", "sports", 74),
    ("leisure", "park", "park", 76),
    ("place", "square", "square", 74),
    ("tourism", "museum", "public", 74),
    ("tourism", "attraction", "public", 72),
    ("amenity", "theatre", "public", 72),
    ("amenity", "arts_centre", "public", 72),
    ("amenity", "ferry_terminal", "terminal", 86),
]


def normalize_text(text: str) -> str:
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text)
    ascii_only = "".join(ch for ch in nfkd if not unicodedata.combining(ch))
    lowered = ascii_only.lower()
    cleaned = re.sub(r"[^a-z0-9\s\-]", " ", lowered)
    return re.sub(r"\s+", " ", cleaned).strip()


def strip_street_prefix(name_norm: str) -> str:
    stripped = STREET_PREFIX_RE.sub("", name_norm).strip()
    return stripped if len(stripped) >= 2 else name_norm


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def point_in_ring(x: float, y: float, ring: list[tuple[float, float]]) -> bool:
    inside = False
    n = len(ring)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-15) + xi):
            inside = not inside
        j = i
    return inside


def stitch_ways_to_rings(way_coords: list[list[tuple[float, float]]]) -> list[list[tuple[float, float]]]:
    segments = [list(c) for c in way_coords if len(c) >= 2]
    rings: list[list[tuple[float, float]]] = []
    while segments:
        ring = segments.pop(0)
        changed = True
        while changed and ring[0] != ring[-1]:
            changed = False
            for idx, seg in enumerate(segments):
                if seg[0] == ring[-1]:
                    ring.extend(seg[1:])
                    segments.pop(idx)
                    changed = True
                    break
                if seg[-1] == ring[-1]:
                    ring.extend(reversed(seg[:-1]))
                    segments.pop(idx)
                    changed = True
                    break
                if seg[-1] == ring[0]:
                    ring = seg[:-1] + ring
                    segments.pop(idx)
                    changed = True
                    break
                if seg[0] == ring[0]:
                    ring = list(reversed(seg[1:])) + ring
                    segments.pop(idx)
                    changed = True
                    break
        if len(ring) >= 4:
            if ring[0] != ring[-1]:
                ring.append(ring[0])
            rings.append(ring)
    return rings


def ring_bbox_and_centroid(rings: list[list[tuple[float, float]]]) -> tuple[float, float, float, float, float, float]:
    xs = [p[0] for r in rings for p in r]
    ys = [p[1] for r in rings for p in r]
    min_lng, max_lng = min(xs), max(xs)
    min_lat, max_lat = min(ys), max(ys)
    best_ring = max(rings, key=len)
    area2 = 0.0
    cx = 0.0
    cy = 0.0
    for i in range(len(best_ring) - 1):
        x0, y0 = best_ring[i]
        x1, y1 = best_ring[i + 1]
        cross = x0 * y1 - x1 * y0
        area2 += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if abs(area2) > 1e-12:
        lng = cx / (3.0 * area2)
        lat = cy / (3.0 * area2)
    else:
        lng = (min_lng + max_lng) / 2.0
        lat = (min_lat + max_lat) / 2.0
    return lat, lng, min_lat, min_lng, max_lat, max_lng


class GridPolygonIndex:
    """Grid-accelerated point-in-polygon index (O(1) cell lookup even for whole states)."""

    def __init__(self, cell_deg: float = 0.1) -> None:
        self.cell = cell_deg
        self.items: list[tuple[str, float, float, float, float, float, list[list[tuple[float, float]]]]] = []
        self.grid: dict[tuple[int, int], list[int]] = defaultdict(list)

    def add(self, name: str, rings: list[list[tuple[float, float]]]) -> tuple[float, float, float, float, float, float]:
        lat, lng, min_lat, min_lng, max_lat, max_lng = ring_bbox_and_centroid(rings)
        area_approx = (max_lng - min_lng) * (max_lat - min_lat)
        self.items.append((name, min_lng, min_lat, max_lng, max_lat, area_approx, rings))
        return lat, lng, min_lat, min_lng, max_lat, max_lng

    def finalize(self) -> None:
        self.items.sort(key=lambda x: x[5])
        self.grid.clear()
        inv = 1.0 / self.cell
        for idx, (_name, min_lng, min_lat, max_lng, max_lat, _area, _rings) in enumerate(self.items):
            cx0 = int(math.floor(min_lng * inv))
            cx1 = int(math.floor(max_lng * inv))
            cy0 = int(math.floor(min_lat * inv))
            cy1 = int(math.floor(max_lat * inv))
            # Guard against huge malformed polygons
            if (cx1 - cx0 + 1) * (cy1 - cy0 + 1) > 40000:
                continue
            for cx in range(cx0, cx1 + 1):
                for cy in range(cy0, cy1 + 1):
                    self.grid[(cx, cy)].append(idx)

    def lookup(self, lat: float, lng: float) -> str:
        cands = self.grid.get((int(math.floor(lng / self.cell)), int(math.floor(lat / self.cell))))
        if not cands:
            return ""
        for idx in cands:
            name, min_lng, min_lat, max_lng, max_lat, _area, rings = self.items[idx]
            if not (min_lng <= lng <= max_lng and min_lat <= lat <= max_lat):
                continue
            for ring in rings:
                if point_in_ring(lng, lat, ring):
                    return name
        return ""


class GridPointIndex:
    """Grid-accelerated nearest-point fallback for suburb/city nodes."""

    def __init__(self, cell_deg: float = 0.05) -> None:
        self.cell = cell_deg
        self.grid: dict[tuple[int, int], list[tuple[str, float, float]]] = defaultdict(list)

    def add(self, name: str, lat: float, lng: float) -> None:
        self.grid[(int(math.floor(lng / self.cell)), int(math.floor(lat / self.cell)))].append((name, lat, lng))

    def nearest(self, lat: float, lng: float, max_km: float) -> str:
        cx = int(math.floor(lng / self.cell))
        cy = int(math.floor(lat / self.cell))
        r_cells = max(1, int(math.ceil((max_km / 111.0) / self.cell)))
        best_name, best_d = "", max_km
        for dx in range(-r_cells, r_cells + 1):
            for dy in range(-r_cells, r_cells + 1):
                pts = self.grid.get((cx + dx, cy + dy))
                if not pts:
                    continue
                for name, plat, plng in pts:
                    d = haversine_km(lat, lng, plat, plng)
                    if d < best_d:
                        best_d, best_name = d, name
        return best_name


def classify_poi(tags: osmium.osm.TagList) -> tuple[str, int] | None:
    name = tags.get("name", "").strip()
    if not name or len(name) < 3:
        return None
    for k, v, subkind, rank in PUBLIC_POI_RULES:
        if tags.get(k) == v:
            return subkind, rank
    lname = normalize_text(name)
    if tags.get("amenity") == "clinic" and (
        "upa" in lname.split() or "pronto atendimento" in lname or "hospital" in lname
    ):
        return "hospital", 90
    if (
        tags.get("public_transport") in ("platform", "stop_position")
        or tags.get("highway") == "bus_stop"
    ) and ("terminal" in lname.split() or "rodoviaria" in lname):
        return "terminal", 88
    return None


class RelationScanner(osmium.SimpleHandler):
    """Pass 1: identify which way IDs belong to boundary/POI multipolygon relations."""

    def __init__(self) -> None:
        super().__init__()
        self.needed_ways: set[int] = set()
        self.rel_specs: list[tuple[str, str, str, str, list[int], tuple[str, int] | None, str]] = []

    def relation(self, r: osmium.osm.Relation) -> None:
        if not r.tags:
            return
        rtype = r.tags.get("type", "")
        name = r.tags.get("name", "").strip()
        if rtype not in ("boundary", "multipolygon") or not name:
            return
        al = r.tags.get("admin_level", "")
        place = r.tags.get("place", "")
        is_city = rtype == "boundary" and al == "8"
        is_suburb = (rtype == "boundary" and al in ("9", "10")) or place in ("suburb", "neighbourhood", "quarter")
        poi = None if (is_city or is_suburb) else classify_poi(r.tags)
        if not (is_city or is_suburb or poi):
            return
        outer_wids = [m.ref for m in r.members if m.type == "w" and m.role in ("outer", "")]
        if not outer_wids:
            return
        self.needed_ways.update(outer_wids)
        self.rel_specs.append((rtype, al, place, name, outer_wids, poi, r.tags.get("addr:suburb", "").strip()))


class OsmCollector(osmium.SimpleHandler):
    """Pass 2 (with C++ locations=True): stream nodes & ways with minimal Python RAM."""

    def __init__(self, bbox: tuple[float, float, float, float], needed_ways: set[int]) -> None:
        super().__init__()
        self.west, self.south, self.east, self.north = bbox
        self.needed_ways = needed_ways
        self.way_coords: dict[int, list[tuple[float, float]]] = {}

        self.city_polys = GridPolygonIndex(cell_deg=0.25)
        self.suburb_polys = GridPolygonIndex(cell_deg=0.05)

        self.cities: dict[str, dict] = {}
        self.suburbs: dict[str, dict] = {}
        self.pois: list[dict] = []
        self.street_segments: list[tuple[str, str, str, float, float]] = []
        self.addr_points: list[tuple[str, int, str, str, float, float]] = []

    def _in_bbox(self, lat: float, lng: float) -> bool:
        return self.west <= lng <= self.east and self.south <= lat <= self.north

    def node(self, n: osmium.osm.Node) -> None:
        if not n.tags or not n.location.valid():
            return
        lng, lat = n.location.lon, n.location.lat
        if not self._in_bbox(lat, lng):
            return

        place = n.tags.get("place", "")
        name = n.tags.get("name", "").strip()
        if place in ("city", "town", "village") and name:
            norm = normalize_text(name)
            rank = 100 if place == "city" else (92 if place == "town" else 84)
            cur = self.cities.get(norm)
            if cur is None or rank > cur["rank"]:
                self.cities[norm] = {"name": name, "subkind": place, "lat": lat, "lng": lng, "rank": rank}
        elif place in ("suburb", "neighbourhood", "quarter") and name:
            norm = normalize_text(name)
            rank = 86 if place == "suburb" else 82
            cur = self.suburbs.get(norm)
            if cur is None or rank > cur["rank"]:
                self.suburbs[norm] = {"name": name, "subkind": "suburb", "lat": lat, "lng": lng, "rank": rank}

        poi = classify_poi(n.tags)
        if poi:
            subkind, rank = poi
            self.pois.append(
                {
                    "name": name,
                    "subkind": subkind,
                    "suburb_tag": n.tags.get("addr:suburb", "").strip(),
                    "lat": lat,
                    "lng": lng,
                    "rank": rank,
                }
            )

        street = n.tags.get("addr:street", "").strip()
        hn = n.tags.get("addr:housenumber", "").strip()
        if street and hn:
            m = re.match(r"^(\d+)", hn)
            if m:
                self.addr_points.append(
                    (normalize_text(street), int(m.group(1)), hn[:16], n.tags.get("addr:suburb", "").strip(), lat, lng)
                )

    def way(self, w: osmium.osm.Way) -> None:
        keep_geom = w.id in self.needed_ways
        has_tags = bool(w.tags)
        if not keep_geom and not has_tags:
            return

        coords: list[tuple[float, float]] = []
        for nr in w.nodes:
            if nr.location.valid():
                coords.append((nr.location.lon, nr.location.lat))
        if not coords:
            return

        if keep_geom:
            self.way_coords[w.id] = coords

        if not has_tags:
            return

        mid_lng, mid_lat = coords[len(coords) // 2]
        if not self._in_bbox(mid_lat, mid_lng):
            return

        is_closed = len(coords) >= 4 and coords[0] == coords[-1]
        place = w.tags.get("place", "")
        name = w.tags.get("name", "").strip()
        if is_closed and place in ("suburb", "neighbourhood", "quarter") and name:
            lat, lng, min_lat, min_lng, max_lat, max_lng = self.suburb_polys.add(name, [coords])
            norm = normalize_text(name)
            self.suburbs[norm] = {
                "name": name,
                "subkind": "suburb",
                "lat": lat,
                "lng": lng,
                "min_lat": min_lat,
                "min_lng": min_lng,
                "max_lat": max_lat,
                "max_lng": max_lng,
                "rank": 87,
            }

        hw = w.tags.get("highway", "")
        if hw in HIGHWAY_RANK:
            street_name = name or w.tags.get("ref", "").strip()
            if street_name and len(street_name) >= 2:
                self.street_segments.append(
                    (street_name, hw, w.tags.get("addr:suburb", "").strip(), mid_lat, mid_lng)
                )

        poi = classify_poi(w.tags)
        if poi:
            subkind, rank = poi
            avg_lng = sum(c[0] for c in coords) / len(coords)
            avg_lat = sum(c[1] for c in coords) / len(coords)
            self.pois.append(
                {
                    "name": name,
                    "subkind": subkind,
                    "suburb_tag": w.tags.get("addr:suburb", "").strip(),
                    "lat": avg_lat,
                    "lng": avg_lng,
                    "rank": rank + 1,
                }
            )

        street = w.tags.get("addr:street", "").strip()
        hn = w.tags.get("addr:housenumber", "").strip()
        if street and hn:
            m = re.match(r"^(\d+)", hn)
            if m:
                avg_lng = sum(c[0] for c in coords) / len(coords)
                avg_lat = sum(c[1] for c in coords) / len(coords)
                self.addr_points.append(
                    (normalize_text(street), int(m.group(1)), hn[:16], w.tags.get("addr:suburb", "").strip(), avg_lat, avg_lng)
                )


def load_region(regions_file: Path, region_id: str) -> dict:
    data = json.loads(regions_file.read_text(encoding="utf-8"))
    for r in data["regions"]:
        if r["id"] == region_id:
            return r
    raise SystemExit(f"Unknown region id '{region_id}' in {regions_file}")


def build_database(pbf_path: Path, db_path: Path, region: dict) -> dict:
    t0 = time.time()
    bbox = tuple(region["bbox"])
    default_city = "Joinville" if region["id"] == "joinville" else region["name"]
    print(f"Building geocoder for '{region['id']}' ({region['name']}) from {pbf_path}...", flush=True)

    rel_scan = RelationScanner()
    rel_scan.apply_file(str(pbf_path), locations=False)

    col = OsmCollector(bbox, rel_scan.needed_ways)
    col.apply_file(str(pbf_path), locations=True, idx="flex_mem")

    # Assemble boundary/POI relation polygons
    for rtype, al, place, name, outer_wids, poi, suburb_tag in rel_scan.rel_specs:
        outer_ways = [col.way_coords[wid] for wid in outer_wids if wid in col.way_coords]
        if not outer_ways:
            continue
        rings = stitch_ways_to_rings(outer_ways)
        if not rings:
            continue
        if rtype == "boundary" and al == "8":
            lat, lng, min_lat, min_lng, max_lat, max_lng = col.city_polys.add(name, rings)
            norm = normalize_text(name)
            col.cities[norm] = {
                "name": name,
                "subkind": "city",
                "lat": lat,
                "lng": lng,
                "min_lat": min_lat,
                "min_lng": min_lng,
                "max_lat": max_lat,
                "max_lng": max_lng,
                "rank": 100,
            }
        elif (rtype == "boundary" and al in ("9", "10")) or place in ("suburb", "neighbourhood", "quarter"):
            lat, lng, min_lat, min_lng, max_lat, max_lng = col.suburb_polys.add(name, rings)
            if col._in_bbox(lat, lng):
                norm = normalize_text(name)
                col.suburbs[norm] = {
                    "name": name,
                    "subkind": "suburb",
                    "lat": lat,
                    "lng": lng,
                    "min_lat": min_lat,
                    "min_lng": min_lng,
                    "max_lat": max_lat,
                    "max_lng": max_lng,
                    "rank": 88,
                }
        elif poi:
            subkind, rank = poi
            lat, lng, *_ = ring_bbox_and_centroid(rings)
            if col._in_bbox(lat, lng):
                col.pois.append(
                    {"name": name, "subkind": subkind, "suburb_tag": suburb_tag, "lat": lat, "lng": lng, "rank": rank + 1}
                )

    col.way_coords.clear()
    rel_scan.rel_specs.clear()
    rel_scan.needed_ways.clear()
    col.city_polys.finalize()
    col.suburb_polys.finalize()

    print(
        f"Collected in {time.time() - t0:.2f}s: "
        f"{len(col.cities)} cities ({len(col.city_polys.items)} polys), "
        f"{len(col.suburbs)} suburbs ({len(col.suburb_polys.items)} polys), "
        f"{len(col.street_segments)} street segments, "
        f"{len(col.pois)} raw POIs, "
        f"{len(col.addr_points)} address points",
        flush=True,
    )

    suburb_pts = GridPointIndex(cell_deg=0.05)
    for s in col.suburbs.values():
        suburb_pts.add(s["name"], s["lat"], s["lng"])

    city_pts = GridPointIndex(cell_deg=0.5)
    for c in col.cities.values():
        city_pts.add(c["name"], c["lat"], c["lng"])

    def resolve_city(lat: float, lng: float) -> str:
        c = col.city_polys.lookup(lat, lng)
        if c:
            return c
        return city_pts.nearest(lat, lng, max_km=80.0) or default_city

    def resolve_suburb(lat: float, lng: float, explicit_tag: str = "") -> str:
        if explicit_tag:
            return explicit_tag
        s = col.suburb_polys.lookup(lat, lng)
        if s:
            return s
        return suburb_pts.nearest(lat, lng, max_km=2.5)

    rows: list[tuple] = []

    for c in col.cities.values():
        nn = normalize_text(c["name"])
        rows.append(
            (
                "city", c["subkind"], c["name"], nn, nn, "", "", c["name"], nn,
                round(c["lat"], 6), round(c["lng"], 6),
                round(c["min_lat"], 6) if "min_lat" in c else None,
                round(c["min_lng"], 6) if "min_lng" in c else None,
                round(c["max_lat"], 6) if "max_lat" in c else None,
                round(c["max_lng"], 6) if "max_lng" in c else None,
                c["rank"],
            )
        )

    for s in col.suburbs.values():
        city = resolve_city(s["lat"], s["lng"])
        nn = normalize_text(s["name"])
        cn = normalize_text(city)
        rows.append(
            (
                "suburb", "suburb", s["name"], nn, nn, s["name"], nn, city, cn,
                round(s["lat"], 6), round(s["lng"], 6),
                round(s["min_lat"], 6) if "min_lat" in s else None,
                round(s["min_lng"], 6) if "min_lng" in s else None,
                round(s["max_lat"], 6) if "max_lat" in s else None,
                round(s["max_lng"], 6) if "max_lng" in s else None,
                s["rank"],
            )
        )

    street_groups: dict[tuple[str, str, str], dict] = {}
    for sname, hw, sub_tag, lat, lng in col.street_segments:
        suburb = resolve_suburb(lat, lng, sub_tag)
        city = resolve_city(lat, lng)
        name_norm = normalize_text(sname)
        if not name_norm:
            continue
        suburb_norm = normalize_text(suburb)
        city_norm = normalize_text(city)
        key = (name_norm, suburb_norm, city_norm)
        rank = HIGHWAY_RANK.get(hw, 30)
        g = street_groups.get(key)
        if g is None:
            street_groups[key] = {
                "name": sname, "hw": hw, "rank": rank, "suburb": suburb,
                "city": city, "lats": [lat], "lngs": [lng],
            }
        else:
            g["lats"].append(lat)
            g["lngs"].append(lng)
            if rank > g["rank"]:
                g["rank"] = rank
                g["hw"] = hw
                g["name"] = sname

    for (name_norm, suburb_norm, city_norm), g in street_groups.items():
        lats, lngs = g["lats"], g["lngs"]
        avg_lat = sum(lats) / len(lats)
        avg_lng = sum(lngs) / len(lngs)
        best_i = min(range(len(lats)), key=lambda i: (lats[i] - avg_lat) ** 2 + (lngs[i] - avg_lng) ** 2)
        seg_boost = min(8, int(math.log2(len(lats) + 1) * 2))
        rows.append(
            (
                "street", g["hw"], g["name"], name_norm, strip_street_prefix(name_norm),
                g["suburb"], suburb_norm, g["city"], city_norm,
                round(lats[best_i], 6), round(lngs[best_i], 6),
                round(min(lats), 6), round(min(lngs), 6), round(max(lats), 6), round(max(lngs), 6),
                g["rank"] + seg_boost,
            )
        )

    poi_dedup: dict[tuple[str, str, str], dict] = {}
    for p in col.pois:
        nn = normalize_text(p["name"])
        if not nn:
            continue
        city = resolve_city(p["lat"], p["lng"])
        k = (nn, p["subkind"], normalize_text(city))
        cur = poi_dedup.get(k)
        if cur is None or p["rank"] > cur["rank"]:
            p["city"] = city
            poi_dedup[k] = p

    for (nn, subkind, cn), p in poi_dedup.items():
        suburb = resolve_suburb(p["lat"], p["lng"], p["suburb_tag"])
        rows.append(
            (
                "poi", subkind, p["name"], nn, strip_street_prefix(nn),
                suburb, normalize_text(suburb), p["city"], cn,
                round(p["lat"], 6), round(p["lng"], 6), None, None, None, None, p["rank"],
            )
        )

    addr_dedup: dict[tuple[str, int], tuple[str, str, float, float]] = {}
    for street_norm, num_int, num_raw, sub_tag, lat, lng in col.addr_points:
        if not street_norm or num_int <= 0 or num_int > 99999:
            continue
        key = (street_norm, num_int)
        if key not in addr_dedup:
            suburb = sub_tag or col.suburb_polys.lookup(lat, lng)
            addr_dedup[key] = (num_raw, suburb, round(lat, 6), round(lng, 6))

    if db_path.exists():
        db_path.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA page_size = 4096;")
    conn.execute("PRAGMA journal_mode = DELETE;")
    conn.execute("PRAGMA synchronous = OFF;")

    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

        CREATE TABLE places (
            id INTEGER PRIMARY KEY,
            kind TEXT NOT NULL,
            subkind TEXT NOT NULL,
            name TEXT NOT NULL,
            name_norm TEXT NOT NULL,
            short_norm TEXT NOT NULL,
            suburb TEXT NOT NULL DEFAULT '',
            suburb_norm TEXT NOT NULL DEFAULT '',
            city TEXT NOT NULL DEFAULT '',
            city_norm TEXT NOT NULL DEFAULT '',
            lat REAL NOT NULL,
            lng REAL NOT NULL,
            min_lat REAL,
            min_lng REAL,
            max_lat REAL,
            max_lng REAL,
            rank INTEGER NOT NULL DEFAULT 50
        );

        CREATE VIRTUAL TABLE places_fts USING fts5(
            name_norm, short_norm, suburb_norm, city_norm,
            content='places', content_rowid='id',
            tokenize='unicode61', prefix='2 3'
        );

        CREATE TABLE street_numbers (
            street_norm TEXT NOT NULL,
            num_int INTEGER NOT NULL,
            num_raw TEXT NOT NULL,
            suburb TEXT NOT NULL DEFAULT '',
            lat REAL NOT NULL,
            lng REAL NOT NULL,
            PRIMARY KEY (street_norm, num_int)
        ) WITHOUT ROWID;
        """
    )

    conn.executemany(
        """
        INSERT INTO places (
            kind, subkind, name, name_norm, short_norm,
            suburb, suburb_norm, city, city_norm,
            lat, lng, min_lat, min_lng, max_lat, max_lng, rank
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.execute("INSERT INTO places_fts(places_fts) VALUES('rebuild');")
    conn.executemany(
        "INSERT OR IGNORE INTO street_numbers (street_norm, num_int, num_raw, suburb, lat, lng) VALUES (?, ?, ?, ?, ?, ?)",
        [(sn, ni, nr, sub, lat, lng) for (sn, ni), (nr, sub, lat, lng) in addr_dedup.items()],
    )
    conn.executescript(
        """
        CREATE INDEX idx_places_kind_rank ON places(kind, rank DESC);
        CREATE INDEX idx_places_name_norm ON places(name_norm);
        CREATE INDEX idx_places_short_norm ON places(short_norm);
        CREATE INDEX idx_places_suburb_norm ON places(suburb_norm);
        """
    )

    counts = dict(conn.execute("SELECT kind, COUNT(*) FROM places GROUP BY kind").fetchall())
    addr_count = conn.execute("SELECT COUNT(*) FROM street_numbers").fetchone()[0]
    w, s, e, n = region["bbox"]
    meta_rows = [
        ("pack_id", region["id"]),
        ("uf", region["uf"]),
        ("schema_version", "1"),
        ("bbox", f"{w},{s},{e},{n}"),
        ("built_at_utc", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
        ("count_cities", str(counts.get("city", 0))),
        ("count_suburbs", str(counts.get("suburb", 0))),
        ("count_streets", str(counts.get("street", 0))),
        ("count_pois", str(counts.get("poi", 0))),
        ("count_street_numbers", str(addr_count)),
    ]
    conn.executemany("INSERT INTO meta(key, value) VALUES (?, ?)", meta_rows)
    conn.execute("INSERT INTO places_fts(places_fts) VALUES('optimize');")
    conn.commit()
    conn.execute("ANALYZE;")
    conn.execute("VACUUM;")
    conn.close()

    size_mb = db_path.stat().st_size / (1024 * 1024)
    print(
        f"Built {db_path} ({size_mb:.2f} MB) in {time.time() - t0:.2f}s | "
        f"counts={counts}, street_numbers={addr_count}",
        flush=True,
    )
    return {"counts": counts, "street_numbers": addr_count, "size_bytes": db_path.stat().st_size}


def main() -> int:
    root = Path(__file__).parent
    ap = argparse.ArgumentParser(description="Build SQLite FTS5 offline geocoder for a region.")
    ap.add_argument("--region", default="joinville")
    ap.add_argument("--regions-file", type=Path, default=root / "states_br.json")
    ap.add_argument("--pbf", type=Path, default=None)
    ap.add_argument("--db", type=Path, default=None)
    args = ap.parse_args()

    region = load_region(args.regions_file, args.region)
    pbf = args.pbf or (root / "out" / f"{region['id']}.osm.pbf")
    db = args.db or (root / "out" / f"{region['id']}_geocoder.db")
    if not pbf.exists():
        print(f"ERROR: OSM PBF not found: {pbf}", file=sys.stderr)
        return 1
    build_database(pbf, db, region)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
