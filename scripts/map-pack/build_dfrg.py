#!/usr/bin/env python3
"""
Convert `joinville.osm.pbf` into DriFly's compact little-endian CSR binary road
graph `routing.dfrg` (DFRG v1 specification).

Features:
  - Filters drivable car/moto OSM highways (respecting access, oneway, roundabouts,
    tolls, unpaved surfaces, bridges, tunnels, links, maxspeed + BR urban defaults).
  - Splits ways at intersections & contracts pass-through degree-2 nodes into
    edge intermediate shape points (`geomLatE6`, `geomLngE6`) to preserve exact
    road curves with minimal RAM (~4 MB on Android).
  - Keeps only the largest Strongly Connected Component (iterative Tarjan SCC)
    so routing never snaps to disconnected islands or one-way traps.
  - Self-verifies by re-parsing `routing.dfrg` and running a Joinville A* sanity
    route (Terminal Central -> Aeroporto de Joinville).
"""
from __future__ import annotations

import heapq
import math
import re
import struct
import sys
import time
from collections import defaultdict
from pathlib import Path

import osmium

# Highway -> (roadClass 0..8, isLink, default_speed_kmh)
HIGHWAY_SPEC: dict[str, tuple[int, bool, float]] = {
    "motorway": (0, False, 100.0),
    "motorway_link": (0, True, 55.0),
    "trunk": (1, False, 80.0),
    "trunk_link": (1, True, 50.0),
    "primary": (2, False, 60.0),
    "primary_link": (2, True, 40.0),
    "secondary": (3, False, 50.0),
    "secondary_link": (3, True, 35.0),
    "tertiary": (4, False, 45.0),
    "tertiary_link": (4, True, 30.0),
    "residential": (5, False, 35.0),
    "living_street": (6, False, 20.0),
    "service": (6, False, 20.0),
    "unclassified": (7, False, 38.0),
    "track": (8, False, 20.0),
}

UNPAVED_SURFACES = {
    "unpaved",
    "dirt",
    "earth",
    "gravel",
    "fine_gravel",
    "pebblestone",
    "sand",
    "grass",
    "mud",
    "ground",
    "compacted",
    "clay",
    "woodchips",
}

BLOCKED_ACCESS = {"no", "private", "agricultural", "forestry", "military", "customers"}
ALLOWED_ACCESS = {"yes", "permissive", "destination", "designated"}
EXCLUDED_SERVICE = {"driveway", "parking_aisle", "emergency_access", "private"}

FLAG_ONEWAY = 0x0010
FLAG_ROUNDABOUT = 0x0020
FLAG_TOLL = 0x0040
FLAG_UNPAVED = 0x0080
FLAG_BRIDGE = 0x0100
FLAG_TUNNEL = 0x0200
FLAG_LINK = 0x0400

MAXSPEED_RE = re.compile(r"(\d+)")


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp * 0.5) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl * 0.5) ** 2
    return 2.0 * r * math.asin(min(1.0, math.sqrt(a)))


def parse_maxspeed_kmh(tags: osmium.osm.TagList, default_speed: float, road_class: int) -> float:
    raw = tags.get("maxspeed", "").strip().lower()
    if raw:
        m = MAXSPEED_RE.search(raw)
        if m:
            val = float(m.group(1))
            if "mph" in raw:
                val *= 1.60934
            if 10.0 <= val <= 130.0:
                val = min(val, 110.0)
                # Realistic urban average flow vs posted limit
                factor = 0.94 if road_class <= 1 else 0.86
                return max(12.0, val * factor)
    return default_speed


def is_drivable(tags: osmium.osm.TagList) -> bool:
    hw = tags.get("highway", "")
    if hw not in HIGHWAY_SPEC:
        return False

    # Check explicit motor_vehicle / vehicle / access restrictions
    mv = tags.get("motor_vehicle", "")
    veh = tags.get("vehicle", "")
    acc = tags.get("access", "")
    if mv in BLOCKED_ACCESS:
        return False
    if not mv and veh in BLOCKED_ACCESS:
        return False
    if not mv and not veh and acc in BLOCKED_ACCESS:
        return False

    if hw == "service":
        svc = tags.get("service", "")
        if svc in EXCLUDED_SERVICE:
            return False
    if hw == "track":
        if tags.get("tracktype", "") == "grade5":
            return False

    # Skip ferries/proposed/construction (already excluded by HIGHWAY_SPEC)
    if tags.get("area") == "yes" and tags.get("junction") not in ("roundabout", "circular"):
        return False

    return True


class RoadExtractor(osmium.SimpleHandler):
    """Memory-efficient extractor using C++ libosmium locations=True (only stores road nodes)."""

    def __init__(self) -> None:
        super().__init__()
        self.node_coords: dict[int, tuple[float, float]] = {}  # osm_id -> (lat, lng)
        # ways: list of (node_ids, flags, speed_kmh, oneway_dir, name)
        # oneway_dir: 0=both, 1=forward, -1=backward
        self.ways: list[tuple[list[int], int, float, int, str]] = []

    def way(self, w: osmium.osm.Way) -> None:
        if not w.tags or not is_drivable(w.tags):
            return
        nids: list[int] = []
        for nr in w.nodes:
            if nr.location.valid():
                ref = nr.ref
                if ref not in self.node_coords:
                    self.node_coords[ref] = (nr.location.lat, nr.location.lon)
                nids.append(ref)
        if len(nids) < 2:
            return

        hw = w.tags.get("highway", "")
        road_class, is_link, def_speed = HIGHWAY_SPEC[hw]
        junction = w.tags.get("junction", "")
        is_roundabout = junction in ("roundabout", "circular")

        ow_tag = w.tags.get("oneway", "").strip().lower()
        if ow_tag in ("yes", "true", "1"):
            oneway_dir = 1
        elif ow_tag in ("-1", "reverse"):
            oneway_dir = -1
        elif ow_tag in ("no", "false", "0"):
            oneway_dir = 0
        elif is_roundabout or hw in ("motorway", "motorway_link"):
            oneway_dir = 1
        else:
            oneway_dir = 0

        is_toll = w.tags.get("toll", "").lower() in ("yes", "true", "1")
        surface = w.tags.get("surface", "").lower()
        is_unpaved = hw == "track" or surface in UNPAVED_SURFACES
        is_bridge = w.tags.get("bridge", "").lower() not in ("", "no", "false", "0")
        is_tunnel = w.tags.get("tunnel", "").lower() not in ("", "no", "false", "0")

        flags = road_class & 0x0F
        if oneway_dir != 0:
            flags |= FLAG_ONEWAY
        if is_roundabout:
            flags |= FLAG_ROUNDABOUT
        if is_toll:
            flags |= FLAG_TOLL
        if is_unpaved:
            flags |= FLAG_UNPAVED
        if is_bridge:
            flags |= FLAG_BRIDGE
        if is_tunnel:
            flags |= FLAG_TUNNEL
        if is_link:
            flags |= FLAG_LINK

        speed = parse_maxspeed_kmh(w.tags, def_speed, road_class)
        if is_unpaved:
            speed = min(speed, 22.0)
        if is_roundabout:
            speed = min(speed, 28.0)

        name = w.tags.get("name", "").strip()
        ref = w.tags.get("ref", "").strip()
        if not name and ref:
            name = ref
        elif name and ref and road_class <= 1 and ref not in name:
            name = f"{ref} ({name})"

        self.ways.append((nids, flags, speed, oneway_dir, name))


def largest_scc(node_count: int, adj: list[list[int]]) -> set[int]:
    """Iterative Tarjan's Strongly Connected Components — safe for large graphs."""
    index_counter = 0
    indices = [-1] * node_count
    lowlink = [0] * node_count
    on_stack = [False] * node_count
    tarjan_stack: list[int] = []
    best_scc: list[int] = []

    for start in range(node_count):
        if indices[start] != -1:
            continue
        # Call stack: (node, neighbor_idx)
        call_stack: list[tuple[int, int]] = [(start, 0)]
        indices[start] = lowlink[start] = index_counter
        index_counter += 1
        tarjan_stack.append(start)
        on_stack[start] = True

        while call_stack:
            v, ni = call_stack[-1]
            nbrs = adj[v]
            if ni < len(nbrs):
                call_stack[-1] = (v, ni + 1)
                w = nbrs[ni]
                if indices[w] == -1:
                    indices[w] = lowlink[w] = index_counter
                    index_counter += 1
                    tarjan_stack.append(w)
                    on_stack[w] = True
                    call_stack.append((w, 0))
                elif on_stack[w]:
                    if indices[w] < lowlink[v]:
                        lowlink[v] = indices[w]
            else:
                call_stack.pop()
                if call_stack:
                    parent = call_stack[-1][0]
                    if lowlink[v] < lowlink[parent]:
                        lowlink[parent] = lowlink[v]
                if lowlink[v] == indices[v]:
                    comp: list[int] = []
                    while True:
                        w = tarjan_stack.pop()
                        on_stack[w] = False
                        comp.append(w)
                        if w == v:
                            break
                    if len(comp) > len(best_scc):
                        best_scc = comp

    return set(best_scc)


def polyline_length_m(coords: list[tuple[float, float]]) -> float:
    total = 0.0
    for i in range(len(coords) - 1):
        total += haversine_m(coords[i][0], coords[i][1], coords[i + 1][0], coords[i + 1][1])
    return total


def build_dfrg(pbf_path: Path, out_path: Path) -> dict:
    t0 = time.time()
    print(f"Extracting drivable road network from {pbf_path}...")
    ext = RoadExtractor()
    ext.apply_file(str(pbf_path), locations=True, idx="flex_mem")
    print(f"  Parsed {len(ext.ways):,} drivable OSM ways ({len(ext.node_coords):,} road nodes) in {time.time() - t0:.2f}s", flush=True)

    # 1. Identify anchor/junction OSM nodes (degree > 2 or way endpoints or repeated in same way)
    node_use_count: dict[int, int] = defaultdict(int)
    endpoints: set[int] = set()
    for nids, _flags, _speed, _ow, _name in ext.ways:
        endpoints.add(nids[0])
        endpoints.add(nids[-1])
        seen_in_way: set[int] = set()
        for nid in nids:
            node_use_count[nid] += 1
            if nid in seen_in_way:
                endpoints.add(nid)
            seen_in_way.add(nid)

    def is_anchor(nid: int) -> bool:
        return nid in endpoints or node_use_count[nid] > 1

    # 2. Split ways at anchors -> directed segments with intermediate geometry
    # Store raw directed edges keyed by OSM anchor IDs
    name_to_id: dict[str, int] = {}
    names: list[str] = []

    def get_name_id(name: str) -> int:
        if not name:
            return -1
        nid = name_to_id.get(name)
        if nid is None:
            nid = len(names)
            name_to_id[name] = nid
            names.append(name)
        return nid

    # Directed edges: (u_osm, v_osm, dist_m, time_ds, flags, name_id, geom_coords_excluding_endpoints)
    raw_edges: list[tuple[int, int, int, int, int, int, list[tuple[float, float]]]] = []

    def emit_segment(
        u_osm: int,
        v_osm: int,
        full_coords: list[tuple[float, float]],
        flags: int,
        speed_kmh: float,
        name_id: int,
    ) -> None:
        if u_osm == v_osm and len(full_coords) <= 2:
            return
        dist = polyline_length_m(full_coords)
        dist_m = max(1, int(round(dist)))
        time_ds = max(1, int(round((dist / (speed_kmh / 3.6)) * 10.0)))
        inter = full_coords[1:-1]
        raw_edges.append((u_osm, v_osm, dist_m, time_ds, flags, name_id, inter))

    for nids, flags, speed_kmh, oneway_dir, name in ext.ways:
        name_id = get_name_id(name)
        seg_start = 0
        for i in range(1, len(nids)):
            if i == len(nids) - 1 or is_anchor(nids[i]):
                sub = nids[seg_start : i + 1]
                coords = [ext.node_coords[nid] for nid in sub]
                u_osm, v_osm = sub[0], sub[-1]
                if oneway_dir == 1:
                    emit_segment(u_osm, v_osm, coords, flags, speed_kmh, name_id)
                elif oneway_dir == -1:
                    emit_segment(v_osm, u_osm, list(reversed(coords)), flags, speed_kmh, name_id)
                else:
                    emit_segment(u_osm, v_osm, coords, flags, speed_kmh, name_id)
                    emit_segment(v_osm, u_osm, list(reversed(coords)), flags, speed_kmh, name_id)
                seg_start = i

    # 3. Map OSM anchor IDs -> dense integer IDs
    used_osm: set[int] = set()
    for u, v, *_ in raw_edges:
        used_osm.add(u)
        used_osm.add(v)
    osm_to_idx = {oid: i for i, oid in enumerate(sorted(used_osm))}
    idx_coords = [ext.node_coords[oid] for oid in sorted(used_osm)]
    n_pre = len(idx_coords)

    # Free raw extractor memory
    ext.ways.clear()
    ext.node_coords.clear()

    # Build temporary directed edges with dense IDs
    dense_edges: list[tuple[int, int, int, int, int, int, list[tuple[float, float]]]] = [
        (osm_to_idx[u], osm_to_idx[v], dm, td, fl, nid, geom)
        for u, v, dm, td, fl, nid, geom in raw_edges
    ]
    raw_edges.clear()

    # 4. Second-pass degree-2 contraction across split OSM ways with identical attributes
    # (Skip roundabouts and long edges > 1800m so blacklist avoidance stays spatially sharp)
    out_map: list[list[int]] = [[] for _ in range(n_pre)]
    in_map: list[list[int]] = [[] for _ in range(n_pre)]
    alive = [True] * len(dense_edges)
    for ei, (u, v, *_rest) in enumerate(dense_edges):
        out_map[u].append(ei)
        in_map[v].append(ei)

    contracted_nodes = 0
    for mid in range(n_pre):
        outs = [e for e in out_map[mid] if alive[e]]
        ins = [e for e in in_map[mid] if alive[e]]

        # Case A: one-way pass-through (1 in, 1 out)
        if len(ins) == 1 and len(outs) == 1:
            e1, e2 = ins[0], outs[0]
            if e1 == e2:
                continue
            u, m1, d1, t1, f1, n1, g1 = dense_edges[e1]
            m2, v, d2, t2, f2, n2, g2 = dense_edges[e2]
            if (
                u != v
                and u != mid
                and v != mid
                and f1 == f2
                and n1 == n2
                and (f1 & FLAG_ROUNDABOUT) == 0
                and (d1 + d2) <= 1800
            ):
                new_ei = len(dense_edges)
                new_geom = g1 + [idx_coords[mid]] + g2
                dense_edges.append((u, v, d1 + d2, t1 + t2, f1, n1, new_geom))
                alive.append(True)
                alive[e1] = False
                alive[e2] = False
                out_map[u].append(new_ei)
                in_map[v].append(new_ei)
                contracted_nodes += 1

        # Case B: bidirectional pass-through (2 in, 2 out, between exactly 2 distinct neighbors a & b)
        elif len(ins) == 2 and len(outs) == 2:
            nbr_in = {dense_edges[e][0] for e in ins}
            nbr_out = {dense_edges[e][1] for e in outs}
            if len(nbr_in) == 2 and nbr_in == nbr_out and mid not in nbr_in:
                a, b = sorted(nbr_in)
                e_am = [e for e in ins if dense_edges[e][0] == a][0]
                e_mb = [e for e in outs if dense_edges[e][1] == b][0]
                e_bm = [e for e in ins if dense_edges[e][0] == b][0]
                e_ma = [e for e in outs if dense_edges[e][1] == a][0]
                _, _, d1, t1, f1, n1, g1 = dense_edges[e_am]
                _, _, d2, t2, f2, n2, g2 = dense_edges[e_mb]
                _, _, d3, t3, f3, n3, g3 = dense_edges[e_bm]
                _, _, d4, t4, f4, n4, g4 = dense_edges[e_ma]
                if (
                    f1 == f2 == f3 == f4
                    and n1 == n2 == n3 == n4
                    and (f1 & FLAG_ROUNDABOUT) == 0
                    and (d1 + d2) <= 1800
                    and (d3 + d4) <= 1800
                ):
                    for old in (e_am, e_mb, e_bm, e_ma):
                        alive[old] = False
                    ei_ab = len(dense_edges)
                    dense_edges.append((a, b, d1 + d2, t1 + t2, f1, n1, g1 + [idx_coords[mid]] + g2))
                    alive.append(True)
                    out_map[a].append(ei_ab)
                    in_map[b].append(ei_ab)

                    ei_ba = len(dense_edges)
                    dense_edges.append((b, a, d3 + d4, t3 + t4, f3, n3, g3 + [idx_coords[mid]] + g4))
                    alive.append(True)
                    out_map[b].append(ei_ba)
                    in_map[a].append(ei_ba)
                    contracted_nodes += 1

    active_edges = [dense_edges[i] for i, ok in enumerate(alive) if ok]
    print(f"  Pre-SCC: {n_pre:,} anchors, contracted {contracted_nodes:,} split-way nodes -> {len(active_edges):,} directed edges")

    # 5. Compute largest Strongly Connected Component (eliminates unreachable islands & dead traps)
    adj: list[list[int]] = [[] for _ in range(n_pre)]
    for u, v, *_ in active_edges:
        adj[u].append(v)

    scc_nodes = largest_scc(n_pre, adj)
    scc_edges = [e for e in active_edges if e[0] in scc_nodes and e[1] in scc_nodes]

    # Compact final node IDs 0..nodeCount-1
    final_old_nodes = sorted(scc_nodes)
    old_to_new = {old: new for new, old in enumerate(final_old_nodes)}
    node_count = len(final_old_nodes)

    node_lat_e6 = [int(round(idx_coords[o][0] * 1_000_000)) for o in final_old_nodes]
    node_lng_e6 = [int(round(idx_coords[o][1] * 1_000_000)) for o in final_old_nodes]

    # Remap names to only those referenced by surviving SCC edges
    used_name_ids = sorted({e[5] for e in scc_edges if e[5] >= 0})
    old_name_to_new = {old: new for new, old in enumerate(used_name_ids)}
    final_names = [names[old] for old in used_name_ids]

    # Group edges by new fromNode (CSR order)
    by_from: list[list[tuple[int, int, int, int, int, list[tuple[float, float]]]]] = [
        [] for _ in range(node_count)
    ]
    for u, v, dm, td, fl, nid, geom in scc_edges:
        nu = old_to_new[u]
        nv = old_to_new[v]
        nnid = old_name_to_new[nid] if nid >= 0 else -1
        by_from[nu].append((nv, dm, td, fl, nnid, geom))

    first_edge: list[int] = [0] * (node_count + 1)
    edge_to: list[int] = []
    edge_dist_m: list[int] = []
    edge_time_ds: list[int] = []
    edge_flags: list[int] = []
    edge_name_id: list[int] = []
    edge_geom_offset: list[int] = [0]
    geom_lat_e6: list[int] = []
    geom_lng_e6: list[int] = []

    for u in range(node_count):
        edges_u = by_from[u]
        # Deterministic ordering by destination node
        edges_u.sort(key=lambda x: (x[0], x[2]))
        first_edge[u] = len(edge_to)
        for nv, dm, td, fl, nnid, geom in edges_u:
            edge_to.append(nv)
            edge_dist_m.append(dm)
            edge_time_ds.append(td)
            edge_flags.append(fl)
            edge_name_id.append(nnid)
            for glat, glng in geom:
                geom_lat_e6.append(int(round(glat * 1_000_000)))
                geom_lng_e6.append(int(round(glng * 1_000_000)))
            edge_geom_offset.append(len(geom_lat_e6))
    first_edge[node_count] = len(edge_to)

    edge_count = len(edge_to)
    name_count = len(final_names)
    geom_count = len(geom_lat_e6)

    bbox_min_lat = min(node_lat_e6)
    bbox_max_lat = max(node_lat_e6)
    bbox_min_lng = min(node_lng_e6)
    bbox_max_lng = max(node_lng_e6)

    # 6. Serialize DFRG v1 binary file (strict little-endian)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("wb") as f:
        # 48-byte Header
        header = struct.pack(
            "<4sHHiiiiiiii8s",
            b"DFRG",
            1,
            0,
            node_count,
            edge_count,
            name_count,
            geom_count,
            bbox_min_lat,
            bbox_min_lng,
            bbox_max_lat,
            bbox_max_lng,
            b"\x00" * 8,
        )
        assert len(header) == 48
        f.write(header)

        # Nodes
        f.write(struct.pack(f"<{node_count}i", *node_lat_e6))
        f.write(struct.pack(f"<{node_count}i", *node_lng_e6))
        f.write(struct.pack(f"<{node_count + 1}i", *first_edge))

        # Directed Edges
        f.write(struct.pack(f"<{edge_count}i", *edge_to))
        f.write(struct.pack(f"<{edge_count}i", *edge_dist_m))
        f.write(struct.pack(f"<{edge_count}i", *edge_time_ds))
        f.write(struct.pack(f"<{edge_count}H", *edge_flags))
        f.write(struct.pack(f"<{edge_count}i", *edge_name_id))
        f.write(struct.pack(f"<{edge_count + 1}i", *edge_geom_offset))

        # Intermediate Edge Geometry
        if geom_count > 0:
            f.write(struct.pack(f"<{geom_count}i", *geom_lat_e6))
            f.write(struct.pack(f"<{geom_count}i", *geom_lng_e6))

        # Street Name Table
        for s in final_names:
            b = s.encode("utf-8")
            if len(b) > 65535:
                b = b[:65535]
            f.write(struct.pack("<H", len(b)))
            f.write(b)

    size_bytes = out_path.stat().st_size
    est_heap_mb = (node_count * 20 + edge_count * 28 + geom_count * 8) / (1024 * 1024)
    print(
        f"Wrote {out_path} ({size_bytes / (1024 * 1024):.2f} MB, {size_bytes:,} bytes) in {time.time() - t0:.2f}s\n"
        f"  Nodes: {node_count:,} | Directed Edges: {edge_count:,} | "
        f"Geom Pts: {geom_count:,} | Street Names: {name_count:,} | Est. Android heap: ~{est_heap_mb:.1f} MB",
        flush=True,
    )

    return {
        "nodes": node_count,
        "edges": edge_count,
        "geom_points": geom_count,
        "names": name_count,
        "size_bytes": size_bytes,
        "bbox": [
            bbox_min_lng / 1e6,
            bbox_min_lat / 1e6,
            bbox_max_lng / 1e6,
            bbox_max_lat / 1e6,
        ],
    }


def verify_and_test_route(dfrg_path: Path, region_id: str = "joinville") -> None:
    """Re-parse DFRG v1 strictly from disk and run Joinville A* sanity routes."""
    data = dfrg_path.read_bytes()
    off = 0
    magic, ver, flags, n_cnt, e_cnt, name_cnt, g_cnt, min_lat, min_lng, max_lat, max_lng, res = (
        struct.unpack_from("<4sHHiiiiiiii8s", data, off)
    )
    off += 48
    assert magic == b"DFRG" and ver == 1 and flags == 0 and res == b"\x00" * 8

    node_lat = struct.unpack_from(f"<{n_cnt}i", data, off); off += 4 * n_cnt
    node_lng = struct.unpack_from(f"<{n_cnt}i", data, off); off += 4 * n_cnt
    first_edge = struct.unpack_from(f"<{n_cnt + 1}i", data, off); off += 4 * (n_cnt + 1)
    edge_to = struct.unpack_from(f"<{e_cnt}i", data, off); off += 4 * e_cnt
    edge_dist = struct.unpack_from(f"<{e_cnt}i", data, off); off += 4 * e_cnt
    edge_time = struct.unpack_from(f"<{e_cnt}i", data, off); off += 4 * e_cnt
    edge_flags = struct.unpack_from(f"<{e_cnt}H", data, off); off += 2 * e_cnt
    edge_name = struct.unpack_from(f"<{e_cnt}i", data, off); off += 4 * e_cnt
    edge_geom_off = struct.unpack_from(f"<{e_cnt + 1}i", data, off); off += 4 * (e_cnt + 1)
    geom_lat = struct.unpack_from(f"<{g_cnt}i", data, off); off += 4 * g_cnt
    geom_lng = struct.unpack_from(f"<{g_cnt}i", data, off); off += 4 * g_cnt

    names: list[str] = []
    for _ in range(name_cnt):
        (blen,) = struct.unpack_from("<H", data, off); off += 2
        names.append(data[off : off + blen].decode("utf-8"))
        off += blen

    assert off == len(data), f"Trailing bytes: parsed {off} vs file {len(data)}"
    assert first_edge[0] == 0 and first_edge[n_cnt] == e_cnt
    assert edge_geom_off[0] == 0 and edge_geom_off[e_cnt] == g_cnt

    # Count flags for sanity diagnostics
    n_oneway = sum(1 for fl in edge_flags if fl & FLAG_ONEWAY)
    n_roundabout = sum(1 for fl in edge_flags if fl & FLAG_ROUNDABOUT)
    n_toll = sum(1 for fl in edge_flags if fl & FLAG_TOLL)
    n_unpaved = sum(1 for fl in edge_flags if fl & FLAG_UNPAVED)
    print(
        f"Binary integrity OK ({len(data):,} bytes exact) | "
        f"oneway={n_oneway:,}, roundabout={n_roundabout:,}, toll={n_toll:,}, unpaved={n_unpaved:,}"
    )

    def nearest_node(lat: float, lng: float) -> tuple[int, float]:
        tlat, tlng = int(lat * 1e6), int(lng * 1e6)
        best_i, best_d2 = -1, 10**30
        for i in range(n_cnt):
            dl = node_lat[i] - tlat
            dg = node_lng[i] - tlng
            d2 = dl * dl + dg * dg
            if d2 < best_d2:
                best_d2, best_i = d2, i
        dist_m = haversine_m(lat, lng, node_lat[best_i] / 1e6, node_lng[best_i] / 1e6)
        return best_i, dist_m

    def run_astar(src: int, dst: int) -> tuple[float, float, list[str], int, float]:
        t0 = time.perf_counter()
        dlat, dlng = node_lat[dst] / 1e6, node_lng[dst] / 1e6
        max_mps = 110.0 / 3.6  # admissible heuristic speed
        g_score = [10**15] * n_cnt
        parent_node = [-1] * n_cnt
        parent_edge = [-1] * n_cnt
        g_score[src] = 0
        pq: list[tuple[float, int]] = [(0.0, src)]
        settled = 0

        while pq:
            _f, u = heapq.heappop(pq)
            settled += 1
            if u == dst:
                break
            gu = g_score[u]
            for ei in range(first_edge[u], first_edge[u + 1]):
                v = edge_to[ei]
                ng = gu + edge_time[ei]
                if ng < g_score[v]:
                    g_score[v] = ng
                    parent_node[v] = u
                    parent_edge[v] = ei
                    h = (haversine_m(node_lat[v] / 1e6, node_lng[v] / 1e6, dlat, dlng) / max_mps) * 10.0
                    heapq.heappush(pq, (ng + h, v))

        ms = (time.perf_counter() - t0) * 1000.0
        assert parent_node[dst] != -1, "Route not found!"
        path_edges: list[int] = []
        cur = dst
        while cur != src:
            path_edges.append(parent_edge[cur])
            cur = parent_node[cur]
        path_edges.reverse()

        total_m = sum(edge_dist[e] for e in path_edges)
        total_ds = sum(edge_time[e] for e in path_edges)
        streets: list[str] = []
        for e in path_edges:
            nid = edge_name[e]
            sn = names[nid] if nid >= 0 else ""
            if sn and (not streets or streets[-1] != sn):
                streets.append(sn)
        return total_m / 1000.0, total_ds / 600.0, streets, settled, ms

    if region_id == "joinville":
        tests = [
            ("Terminal Central -> Aeroporto de Joinville", (-26.3046, -48.8461), (-26.2244, -48.7974)),
            ("Terminal Central -> Shopping Garten", (-26.3046, -48.8461), (-26.2544, -48.8535)),
            ("Iririú -> Araquari Centro", (-26.2712, -48.8172), (-26.3703, -48.7219)),
        ]
    elif region_id == "pa":
        tests = [
            ("Belém Ver-o-Peso -> Aeroporto Val-de-Cans", (-1.4525, -48.5037), (-1.3847, -48.4787)),
            ("Belém Nazaré -> Ananindeua Centro", (-1.4530, -48.4810), (-1.3656, -48.3722)),
        ]
    else:
        # Generic connectivity test between two graph nodes
        s0, d0 = 0, min(n_cnt - 1, max(1, n_cnt // 3))
        km, mins, streets, settled, ms = run_astar(s0, d0)
        print(f"Generic SCC A* check (node {s0}->{d0}): {km:.2f} km, {mins:.1f} min ({ms:.1f} ms)")
        return

    print(f"\n--- {region_id.upper()} DFRG v1 Sanity Routes (Python A*) ---")
    for label, p_from, p_to in tests:
        s, sd = nearest_node(*p_from)
        d, dd = nearest_node(*p_to)
        km, mins, streets, settled, ms = run_astar(s, d)
        preview = " -> ".join(streets[:6]) + (" -> ..." if len(streets) > 6 else "")
        print(
            f"  {label}: {km:.2f} km, {mins:.1f} min "
            f"(snap {sd:.0f}m/{dd:.0f}m, settled={settled:,}, {ms:.1f} ms)\n"
            f"    Via: {preview}"
        )


def main() -> int:
    import argparse

    root = Path(__file__).parent
    ap = argparse.ArgumentParser(description="Build DFRG v1 binary routing graph from OSM PBF.")
    ap.add_argument("--region", default="joinville")
    ap.add_argument("--pbf", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--no-test", action="store_true")
    args, unknown = ap.parse_known_args()

    pbf = args.pbf or (Path(unknown[0]) if len(unknown) > 0 else root / "out" / f"{args.region}.osm.pbf")
    out = args.out or (Path(unknown[1]) if len(unknown) > 1 else root / "out" / "routing" / "routing.dfrg")
    if not pbf.exists():
        print(f"ERROR: OSM PBF not found: {pbf}", file=sys.stderr)
        return 1
    build_dfrg(pbf, out)
    if not args.no_test:
        verify_and_test_route(out, args.region)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
