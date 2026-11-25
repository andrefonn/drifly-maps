#!/usr/bin/env python3
"""
Generate `scripts/map-pack/regions.json` containing all 27 Brazilian states
(26 states + DF) plus the pilot city pack `joinville`, with:
  - Official public-domain IBGE Malhas Territoriais v3 simplified polygons (<= 250 pts)
  - Exact [west, south, east, north] bounding boxes & centers
  - Geofabrik Brazil regional PBF source (`norte`, `nordeste`, `centro-oeste`, `sudeste`, `sul`)
  - State-appropriate PMTiles `maxzoom` (14 for medium/large states, 15 for tiny states & cities)
"""
from __future__ import annotations

import json
import math
import urllib.request
from pathlib import Path

IBGE_UF_GEOJSON_URL = (
    "https://servicodados.ibge.gov.br/api/v3/malhas/paises/BR"
    "?formato=application/vnd.geo+json&qualidade=minima&intrarregiao=UF"
)

# IBGE codarea -> (id, uf, name, geofabrik_region, maxzoom)
STATES_META: dict[str, tuple[str, str, str, str, int]] = {
    "11": ("ro", "RO", "Rondônia", "norte", 14),
    "12": ("ac", "AC", "Acre", "norte", 14),
    "13": ("am", "AM", "Amazonas", "norte", 14),
    "14": ("rr", "RR", "Roraima", "norte", 14),
    "15": ("pa", "PA", "Pará", "norte", 14),
    "16": ("ap", "AP", "Amapá", "norte", 14),
    "17": ("to", "TO", "Tocantins", "norte", 14),
    "21": ("ma", "MA", "Maranhão", "nordeste", 14),
    "22": ("pi", "PI", "Piauí", "nordeste", 14),
    "23": ("ce", "CE", "Ceará", "nordeste", 14),
    "24": ("rn", "RN", "Rio Grande do Norte", "nordeste", 15),
    "25": ("pb", "PB", "Paraíba", "nordeste", 15),
    "26": ("pe", "PE", "Pernambuco", "nordeste", 14),
    "27": ("al", "AL", "Alagoas", "nordeste", 15),
    "28": ("se", "SE", "Sergipe", "nordeste", 15),
    "29": ("ba", "BA", "Bahia", "nordeste", 14),
    "31": ("mg", "MG", "Minas Gerais", "sudeste", 14),
    "32": ("es", "ES", "Espírito Santo", "sudeste", 15),
    "33": ("rj", "RJ", "Rio de Janeiro", "sudeste", 14),
    "35": ("sp", "SP", "São Paulo", "sudeste", 14),
    "41": ("pr", "PR", "Paraná", "sul", 14),
    "42": ("sc", "SC", "Santa Catarina", "sul", 14),
    "43": ("rs", "RS", "Rio Grande do Sul", "sul", 14),
    "50": ("ms", "MS", "Mato Grosso do Sul", "centro-oeste", 14),
    "51": ("mt", "MT", "Mato Grosso", "centro-oeste", 14),
    "52": ("go", "GO", "Goiás", "centro-oeste", 14),
    "53": ("df", "DF", "Distrito Federal", "centro-oeste", 15),
}


def ring_area(ring: list[list[float]]) -> float:
    area = 0.0
    for i in range(len(ring) - 1):
        area += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
    return abs(area) * 0.5


def point_line_dist(pt: list[float], a: list[float], b: list[float]) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    if dx == 0.0 and dy == 0.0:
        return math.hypot(pt[0] - a[0], pt[1] - a[1])
    t = max(0.0, min(1.0, ((pt[0] - a[0]) * dx + (pt[1] - a[1]) * dy) / (dx * dx + dy * dy)))
    return math.hypot(pt[0] - (a[0] + t * dx), pt[1] - (a[1] + t * dy))


def rdp(points: list[list[float]], eps: float) -> list[list[float]]:
    if len(points) <= 2:
        return points
    max_d = -1.0
    idx = 0
    for i in range(1, len(points) - 1):
        d = point_line_dist(points[i], points[0], points[-1])
        if d > max_d:
            max_d = d
            idx = i
    if max_d > eps:
        left = rdp(points[: idx + 1], eps)
        right = rdp(points[idx:], eps)
        return left[:-1] + right
    return [points[0], points[-1]]


def simplify_closed_ring(ring: list[list[float]], max_pts: int = 240) -> list[list[float]]:
    # Remove duplicate closing point for RDP, then re-close
    open_pts = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else list(ring)
    if len(open_pts) <= max_pts - 1:
        res = open_pts + [open_pts[0]]
    else:
        lo, hi = 0.0005, 0.5
        best = open_pts
        for _ in range(24):
            mid = (lo + hi) * 0.5
            # Split at farthest point from start so closed loop simplifies cleanly
            far_i = max(range(1, len(open_pts)), key=lambda i: (open_pts[i][0] - open_pts[0][0]) ** 2 + (open_pts[i][1] - open_pts[0][1]) ** 2)
            p1 = rdp(open_pts[: far_i + 1], mid)
            p2 = rdp(open_pts[far_i:] + [open_pts[0]], mid)
            cand = p1[:-1] + p2[:-1]
            if len(cand) <= max_pts - 1:
                best = cand
                hi = mid
            else:
                lo = mid
        res = best + [best[0]]
    return [[round(p[0], 4), round(p[1], 4)] for p in res]


def main() -> int:
    req = urllib.request.Request(IBGE_UF_GEOJSON_URL, headers={"User-Agent": "DriFly-MapPack/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        gj = json.loads(resp.read().decode("utf-8"))

    regions: list[dict] = []

    # 1. Pilot city pack: Joinville (SC)
    jw, js, je, jn = -49.18, -26.55, -48.58, -26.02
    regions.append(
        {
            "id": "joinville",
            "uf": "SC",
            "name": "Joinville e Região (SC)",
            "kind": "city",
            "geofabrik_region": "sul",
            "minzoom": 0,
            "maxzoom": 15,
            "bbox": [jw, js, je, jn],
            "center": [-48.8464, -26.3044],
            "polygon": [[jw, js], [je, js], [je, jn], [jw, jn], [jw, js]],
        }
    )

    # 2. All 27 Brazilian states from IBGE
    by_code = {str(f["properties"]["codarea"]): f["geometry"] for f in gj["features"]}
    assert len(by_code) == 27, f"Expected 27 UFs from IBGE, got {len(by_code)}"

    for code, (rid, uf, name, geofabrik_reg, maxzoom) in sorted(STATES_META.items(), key=lambda kv: kv[1][0]):
        geom = by_code[code]
        gtype = geom["type"]
        if gtype == "Polygon":
            outer_rings = [geom["coordinates"][0]]
        elif gtype == "MultiPolygon":
            outer_rings = [poly[0] for poly in geom["coordinates"]]
        else:
            raise ValueError(f"Unsupported geometry type {gtype} for {rid}")

        # Pick largest outer ring (mainland) for primary polygon & compute full bbox across all rings
        main_ring = max(outer_rings, key=ring_area)
        all_pts = [p for r in outer_rings for p in r]
        # Exclude remote oceanic islands (Fernando de Noronha / Trindade) from mainland bbox
        # by computing bbox from main_ring + any ring within 1.5 deg of main_ring
        m_min_x = min(p[0] for p in main_ring)
        m_max_x = max(p[0] for p in main_ring)
        m_min_y = min(p[1] for p in main_ring)
        m_max_y = max(p[1] for p in main_ring)
        near_pts = [
            p
            for r in outer_rings
            if (min(q[0] for q in r) <= m_max_x + 1.5 and max(q[0] for q in r) >= m_min_x - 1.5
                and min(q[1] for q in r) <= m_max_y + 1.5 and max(q[1] for q in r) >= m_min_y - 1.5)
            for p in r
        ]
        west = round(min(p[0] for p in near_pts), 4)
        south = round(min(p[1] for p in near_pts), 4)
        east = round(max(p[0] for p in near_pts), 4)
        north = round(max(p[1] for p in near_pts), 4)
        center = [round((west + east) * 0.5, 4), round((south + north) * 0.5, 4)]

        poly = simplify_closed_ring(main_ring, max_pts=180)
        assert len(poly) <= 200, f"Polygon for {rid} has {len(poly)} > 200 pts"

        regions.append(
            {
                "id": rid,
                "uf": uf,
                "name": name,
                "kind": "state",
                "geofabrik_region": geofabrik_reg,
                "minzoom": 0,
                "maxzoom": maxzoom,
                "bbox": [west, south, east, north],
                "center": center,
                "polygon": poly,
            }
        )

    out_path = Path(__file__).parent / "states_br.json"
    payload = {
        "schema_version": 1,
        "source": "IBGE Malhas Territoriais v3 (Public Domain) + DriFly Joinville bbox",
        "geofabrik_base_url": "https://download.geofabrik.de/south-america/brazil",
        "regions": regions,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    max_v = max(len(r["polygon"]) for r in regions)
    print(f"Wrote {out_path} ({out_path.stat().st_size:,} bytes): {len(regions)} regions (27 states + joinville), max polygon pts={max_v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
