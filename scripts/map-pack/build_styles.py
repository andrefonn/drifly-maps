#!/usr/bin/env python3
"""
Generate self-contained 100% offline MapLibre Style v8 JSON files
(`style_day.json` and `style_night.json`) targeting Protomaps v4.15+ vector tiles
for DriFly (Joinville, SC, Brazil).

Design principles:
  - Uber / 99-inspired clean driver map hierarchy: crisp road casings, clear
    arterials & highways, subtle 3D buildings at zoom >= 15, sprite-free
    one-way road arrows using SDF glyph U+2192 (→) from 8448-8703.pbf.
  - Zero commercial POI clutter (matching DriFly's EstiloMapa.SEM_COMERCIOS).
  - Portuguese labels first: ["coalesce", ["get", "name:pt"], ["get", "name"]].
  - 100% offline: zero external HTTP/HTTPS URLs. LocalMapTileServer replaces
    `http://127.0.0.1:8765` with its bound localhost port when serving the style.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "http://127.0.0.1:8765"
JOINVILLE_BOUNDS = [-49.18, -26.55, -48.58, -26.02]
JOINVILLE_CENTER = [-48.8464, -26.3044]

NAME_PT: list[Any] = ["coalesce", ["get", "name:pt"], ["get", "name"]]

DAY_PALETTE = {
    "bg": "#EFEFF2",
    "earth": "#F1F3F6",
    "park": "#D8ECD4",
    "wood": "#CCE4C7",
    "landuse_residential": "#ECEFF4",
    "landuse_industrial": "#E6EAF0",
    "landuse_commercial": "#EAECEF",
    "landuse_hospital": "#F5E7EA",
    "landuse_school": "#ECECF4",
    "landuse_aerodrome": "#E4E8EE",
    "water": "#B7DAF4",
    "water_line": "#A8D2F0",
    "boundary": "#B8C2D1",
    "building_fill": "#E2E6EE",
    "building_outline": "#D4DBE6",
    "building_3d": "#DCE2EC",
    "runway": "#CFD6E1",
    "rail": "#BFC8D6",
    "rail_dash": "#E5E9F0",
    "path": "#CFD7E3",
    "service_casing": "#DCE1EA",
    "service_fill": "#F8F9FB",
    "minor_casing": "#D3DBE6",
    "minor_fill": "#FFFFFF",
    "tertiary_casing": "#C7D1DF",
    "tertiary_fill": "#FFFFFF",
    "secondary_casing": "#BDC8D8",
    "secondary_fill": "#FFFFFF",
    "primary_casing": "#B4C0D2",
    "primary_fill": "#FFFDF8",
    "trunk_casing": "#D6B072",
    "trunk_fill": "#FDF0D2",
    "highway_casing": "#D49E54",
    "highway_fill": "#FBE0AE",
    "tunnel_opacity": 0.55,
    "bridge_casing": "#9EAAC0",
    "oneway_arrow": "#64748B",
    "oneway_halo": "#FFFFFF",
    "road_label": "#263449",
    "road_label_major": "#162033",
    "road_halo": "#FFFFFF",
    "shield_text": "#1E293B",
    "shield_halo": "#FEF3C7",
    "water_label": "#4278A3",
    "water_halo": "#EAF5FD",
    "poi_label": "#446350",
    "poi_halo": "#F4F8F4",
    "suburb_label": "#4F5D73",
    "suburb_halo": "#F4F6FA",
    "city_label": "#162033",
    "city_halo": "#FFFFFF",
}

NIGHT_PALETTE = {
    "bg": "#0F1622",
    "earth": "#111824",
    "park": "#162A24",
    "wood": "#13251F",
    "landuse_residential": "#141C2A",
    "landuse_industrial": "#161E2C",
    "landuse_commercial": "#17202E",
    "landuse_hospital": "#241B26",
    "landuse_school": "#192030",
    "landuse_aerodrome": "#182130",
    "water": "#0B2036",
    "water_line": "#102B48",
    "boundary": "#2C3B52",
    "building_fill": "#192334",
    "building_outline": "#223047",
    "building_3d": "#1E2B40",
    "runway": "#2A384D",
    "rail": "#2F3E54",
    "rail_dash": "#182232",
    "path": "#253347",
    "service_casing": "#0D141F",
    "service_fill": "#1E293B",
    "minor_casing": "#0C121C",
    "minor_fill": "#253347",
    "tertiary_casing": "#0B111B",
    "tertiary_fill": "#2D3E56",
    "secondary_casing": "#0A1019",
    "secondary_fill": "#334763",
    "primary_casing": "#090F18",
    "primary_fill": "#3D5475",
    "trunk_casing": "#182436",
    "trunk_fill": "#4B6488",
    "highway_casing": "#1E2D42",
    "highway_fill": "#5A769E",
    "tunnel_opacity": 0.50,
    "bridge_casing": "#6B80A1",
    "oneway_arrow": "#94A8C4",
    "oneway_halo": "#141D2B",
    "road_label": "#C8D5E6",
    "road_label_major": "#E7EEF8",
    "road_halo": "#0F1622",
    "shield_text": "#F8FAFC",
    "shield_halo": "#1E3A5F",
    "water_label": "#6495C2",
    "water_halo": "#0B2036",
    "poi_label": "#85B59B",
    "poi_halo": "#111824",
    "suburb_label": "#8B9EB8",
    "suburb_halo": "#0F1622",
    "city_label": "#F1F5F9",
    "city_halo": "#0B111C",
}


def road_layers(p: dict[str, Any], prefix: str, state_filter: list[Any], is_bridge: bool = False, is_tunnel: bool = False) -> list[dict[str, Any]]:
    opacity = p["tunnel_opacity"] if is_tunnel else 1.0
    dasharray = [2, 1.5] if is_tunnel else None
    bridge_c = p["bridge_casing"]

    def mk_filter(*conds: Any) -> list[Any]:
        return ["all", state_filter, *conds]

    def line_layer(
        lid: str,
        flt: list[Any],
        color: str,
        widths: list[Any],
        minzoom: int,
        cap: str = "round",
        dash: list[float] | None = None,
    ) -> dict[str, Any]:
        paint: dict[str, Any] = {
            "line-color": color,
            "line-width": ["interpolate", ["exponential", 1.5], ["zoom"], *widths],
        }
        if opacity < 1.0:
            paint["line-opacity"] = opacity
        if dash:
            paint["line-dasharray"] = dash
        return {
            "id": f"{prefix}-{lid}",
            "type": "line",
            "source": "protomaps",
            "source-layer": "roads",
            "minzoom": minzoom,
            "filter": flt,
            "layout": {"line-cap": cap, "line-join": "round"},
            "paint": paint,
        }

    is_highway = ["in", ["get", "kind_detail"], ["literal", ["motorway", "motorway_link"]]]
    is_trunk = ["in", ["get", "kind_detail"], ["literal", ["trunk", "trunk_link"]]]
    is_primary = ["in", ["get", "kind_detail"], ["literal", ["primary", "primary_link"]]]
    is_secondary = ["in", ["get", "kind_detail"], ["literal", ["secondary", "secondary_link"]]]
    is_tertiary = ["in", ["get", "kind_detail"], ["literal", ["tertiary", "tertiary_link"]]]
    is_minor = ["in", ["get", "kind_detail"], ["literal", ["residential", "unclassified", "living_street"]]]
    is_service = ["in", ["get", "kind_detail"], ["literal", ["service", "pedestrian"]]]
    is_path = ["all", ["==", ["get", "kind"], "path"], ["!", ["in", ["get", "kind_detail"], ["literal", ["bus_stop", "crossing"]]]]]

    cap_casing = "butt" if (is_bridge or is_tunnel) else "round"

    layers = [
        # Footpaths / cycleways
        line_layer(
            "path",
            mk_filter(is_path),
            p["path"],
            [14, 0.7, 17, 1.8],
            14,
            cap="butt",
            dash=[2, 2],
        ),
        # Service roads
        line_layer(
            "service-casing",
            mk_filter(is_service),
            bridge_c if is_bridge else p["service_casing"],
            [14, 2.2, 16, 4.5, 18, 9.0],
            14,
            cap=cap_casing,
        ),
        line_layer(
            "service-fill",
            mk_filter(is_service),
            p["service_fill"],
            [14, 1.2, 16, 3.2, 18, 7.2],
            14,
            dash=dasharray,
        ),
        # Residential / unclassified
        line_layer(
            "minor-casing",
            mk_filter(is_minor),
            bridge_c if is_bridge else p["minor_casing"],
            [12, 1.4, 14, 4.4, 16, 9.5, 18, 17.0],
            12,
            cap=cap_casing,
        ),
        line_layer(
            "minor-fill",
            mk_filter(is_minor),
            p["minor_fill"],
            [12, 0.7, 14, 3.0, 16, 7.6, 18, 14.5],
            12,
            dash=dasharray,
        ),
        # Tertiary
        line_layer(
            "tertiary-casing",
            mk_filter(is_tertiary),
            bridge_c if is_bridge else p["tertiary_casing"],
            [11, 1.8, 14, 6.0, 16, 12.0, 18, 20.0],
            11,
            cap=cap_casing,
        ),
        line_layer(
            "tertiary-fill",
            mk_filter(is_tertiary),
            p["tertiary_fill"],
            [11, 1.0, 14, 4.4, 16, 9.8, 18, 17.5],
            11,
            dash=dasharray,
        ),
        # Secondary
        line_layer(
            "secondary-casing",
            mk_filter(is_secondary),
            bridge_c if is_bridge else p["secondary_casing"],
            [10, 2.0, 14, 7.2, 16, 13.5, 18, 22.0],
            10,
            cap=cap_casing,
        ),
        line_layer(
            "secondary-fill",
            mk_filter(is_secondary),
            p["secondary_fill"],
            [10, 1.1, 14, 5.4, 16, 11.2, 18, 19.2],
            10,
            dash=dasharray,
        ),
        # Primary
        line_layer(
            "primary-casing",
            mk_filter(is_primary),
            bridge_c if is_bridge else p["primary_casing"],
            [8, 1.6, 12, 4.5, 14, 8.5, 16, 15.0, 18, 24.0],
            8,
            cap=cap_casing,
        ),
        line_layer(
            "primary-fill",
            mk_filter(is_primary),
            p["primary_fill"],
            [8, 0.9, 12, 3.2, 14, 6.6, 16, 12.5, 18, 21.0],
            8,
            dash=dasharray,
        ),
        # Trunk (SC-418, major expressways)
        line_layer(
            "trunk-casing",
            mk_filter(is_trunk),
            bridge_c if is_bridge else p["trunk_casing"],
            [7, 1.6, 12, 5.2, 14, 9.6, 16, 16.5, 18, 26.0],
            7,
            cap=cap_casing,
        ),
        line_layer(
            "trunk-fill",
            mk_filter(is_trunk),
            p["trunk_fill"],
            [7, 0.9, 12, 3.8, 14, 7.6, 16, 13.8, 18, 22.5],
            7,
            dash=dasharray,
        ),
        # Motorway / Highway (BR-101)
        line_layer(
            "highway-casing",
            mk_filter(is_highway),
            bridge_c if is_bridge else p["highway_casing"],
            [5, 1.4, 10, 4.2, 14, 11.0, 16, 18.0, 18, 28.0],
            5,
            cap=cap_casing,
        ),
        line_layer(
            "highway-fill",
            mk_filter(is_highway),
            p["highway_fill"],
            [5, 0.8, 10, 2.8, 14, 8.6, 16, 15.0, 18, 24.5],
            5,
            dash=dasharray,
        ),
    ]
    return layers


def build_style(
    name: str,
    p: dict[str, Any],
    base_url: str = DEFAULT_BASE_URL,
    center: list[float] | None = None,
    bounds: list[float] | None = None,
    maxzoom: int = 15,
) -> dict[str, Any]:
    not_tunnel = ["!", ["==", ["get", "is_tunnel"], True]]
    not_bridge = ["!", ["==", ["get", "is_bridge"], True]]

    layers: list[dict[str, Any]] = [
        {
            "id": "background",
            "type": "background",
            "paint": {"background-color": p["bg"]},
        },
        {
            "id": "earth",
            "type": "fill",
            "source": "protomaps",
            "source-layer": "earth",
            "paint": {"fill-color": p["earth"]},
        },
        # Landuse zones
        {
            "id": "landuse-residential",
            "type": "fill",
            "source": "protomaps",
            "source-layer": "landuse",
            "minzoom": 10,
            "filter": ["in", ["get", "kind"], ["literal", ["residential"]]],
            "paint": {"fill-color": p["landuse_residential"], "fill-opacity": 0.65},
        },
        {
            "id": "landuse-industrial",
            "type": "fill",
            "source": "protomaps",
            "source-layer": "landuse",
            "minzoom": 10,
            "filter": ["in", ["get", "kind"], ["literal", ["industrial", "commercial", "retail", "military"]]],
            "paint": {"fill-color": p["landuse_industrial"], "fill-opacity": 0.75},
        },
        {
            "id": "landuse-institutional",
            "type": "fill",
            "source": "protomaps",
            "source-layer": "landuse",
            "minzoom": 12,
            "filter": ["in", ["get", "kind"], ["literal", ["school", "university", "college", "kindergarten"]]],
            "paint": {"fill-color": p["landuse_school"], "fill-opacity": 0.7},
        },
        {
            "id": "landuse-hospital",
            "type": "fill",
            "source": "protomaps",
            "source-layer": "landuse",
            "minzoom": 12,
            "filter": ["==", ["get", "kind"], "hospital"],
            "paint": {"fill-color": p["landuse_hospital"], "fill-opacity": 0.75},
        },
        {
            "id": "landuse-aerodrome",
            "type": "fill",
            "source": "protomaps",
            "source-layer": "landuse",
            "minzoom": 10,
            "filter": ["in", ["get", "kind"], ["literal", ["aerodrome", "runway"]]],
            "paint": {"fill-color": p["landuse_aerodrome"]},
        },
        {
            "id": "landuse-park",
            "type": "fill",
            "source": "protomaps",
            "source-layer": "landuse",
            "minzoom": 8,
            "filter": [
                "in",
                ["get", "kind"],
                ["literal", ["park", "garden", "grass", "meadow", "pitch", "playground", "cemetery", "recreation_ground", "village_green"]],
            ],
            "paint": {"fill-color": p["park"]},
        },
        {
            "id": "landuse-wood",
            "type": "fill",
            "source": "protomaps",
            "source-layer": "landuse",
            "minzoom": 7,
            "filter": [
                "in",
                ["get", "kind"],
                ["literal", ["wood", "forest", "nature_reserve", "national_park", "scrub", "wetland"]],
            ],
            "paint": {"fill-color": p["wood"], "fill-opacity": 0.8},
        },
        # Water polygons & waterways
        {
            "id": "water-fill",
            "type": "fill",
            "source": "protomaps",
            "source-layer": "water",
            "filter": ["all", ["==", ["geometry-type"], "Polygon"], ["!=", ["get", "kind"], "swimming_pool"]],
            "paint": {"fill-color": p["water"]},
        },
        {
            "id": "water-stream",
            "type": "line",
            "source": "protomaps",
            "source-layer": "water",
            "minzoom": 10,
            "filter": ["==", ["geometry-type"], "LineString"],
            "layout": {"line-cap": "round", "line-join": "round"},
            "paint": {
                "line-color": p["water_line"],
                "line-width": ["interpolate", ["linear"], ["zoom"], 10, 0.7, 14, 1.8, 16, 3.2],
            },
        },
        # Municipal / state boundaries
        {
            "id": "boundaries-admin",
            "type": "line",
            "source": "protomaps",
            "source-layer": "boundaries",
            "minzoom": 6,
            "filter": ["in", ["get", "kind"], ["literal", ["country", "region", "county", "locality"]]],
            "paint": {
                "line-color": p["boundary"],
                "line-width": ["interpolate", ["linear"], ["zoom"], 6, 0.8, 12, 1.4],
                "line-dasharray": [4, 2],
                "line-opacity": 0.65,
            },
        },
        # 2D Buildings (zoom 13-15, fade subtly under 3D at z>=15)
        {
            "id": "buildings-2d",
            "type": "fill",
            "source": "protomaps",
            "source-layer": "buildings",
            "minzoom": 13,
            "paint": {
                "fill-color": p["building_fill"],
                "fill-outline-color": p["building_outline"],
                "fill-opacity": ["interpolate", ["linear"], ["zoom"], 13, 0.0, 14, 0.75, 15, 0.85],
            },
        },
        # Airport runways
        {
            "id": "roads-aeroway",
            "type": "line",
            "source": "protomaps",
            "source-layer": "roads",
            "minzoom": 11,
            "filter": ["==", ["get", "kind"], "aeroway"],
            "layout": {"line-cap": "butt"},
            "paint": {
                "line-color": p["runway"],
                "line-width": ["interpolate", ["exponential", 1.5], ["zoom"], 11, 2.0, 15, 16.0],
            },
        },
        # Railways
        {
            "id": "roads-rail",
            "type": "line",
            "source": "protomaps",
            "source-layer": "roads",
            "minzoom": 11,
            "filter": ["==", ["get", "kind"], "rail"],
            "paint": {
                "line-color": p["rail"],
                "line-width": ["interpolate", ["linear"], ["zoom"], 11, 0.8, 15, 1.8],
            },
        },
        {
            "id": "roads-rail-dash",
            "type": "line",
            "source": "protomaps",
            "source-layer": "roads",
            "minzoom": 12,
            "filter": ["==", ["get", "kind"], "rail"],
            "paint": {
                "line-color": p["rail_dash"],
                "line-width": ["interpolate", ["linear"], ["zoom"], 12, 0.6, 15, 1.2],
                "line-dasharray": [3, 3],
            },
        },
    ]

    # Tunnels -> Surface roads -> Bridges
    layers.extend(road_layers(p, "tunnel", ["==", ["get", "is_tunnel"], True], is_tunnel=True))
    layers.extend(road_layers(p, "road", ["all", not_tunnel, not_bridge]))
    layers.extend(road_layers(p, "bridge", ["==", ["get", "is_bridge"], True], is_bridge=True))

    # Subtle 3D Buildings at zoom >= 15 (Uber/Apple Maps navigation polish)
    layers.append(
        {
            "id": "buildings-3d",
            "type": "fill-extrusion",
            "source": "protomaps",
            "source-layer": "buildings",
            "minzoom": 15,
            "paint": {
                "fill-extrusion-color": p["building_3d"],
                "fill-extrusion-height": [
                    "interpolate",
                    ["linear"],
                    ["zoom"],
                    15,
                    0,
                    15.4,
                    ["coalesce", ["get", "height"], 7],
                ],
                "fill-extrusion-base": ["coalesce", ["get", "min_height"], 0],
                "fill-extrusion-opacity": 0.58,
            },
        }
    )

    # Crisp sprite-free One-Way Road Arrows using SDF glyph → (U+2192 in 8448-8703.pbf)
    layers.append(
        {
            "id": "roads-oneway-arrows",
            "type": "symbol",
            "source": "protomaps",
            "source-layer": "roads",
            "minzoom": 15,
            "filter": [
                "all",
                ["==", ["get", "oneway"], "yes"],
                [
                    "in",
                    ["get", "kind_detail"],
                    [
                        "literal",
                        [
                            "motorway",
                            "motorway_link",
                            "trunk",
                            "trunk_link",
                            "primary",
                            "primary_link",
                            "secondary",
                            "secondary_link",
                            "tertiary",
                            "tertiary_link",
                            "residential",
                            "unclassified",
                            "living_street",
                        ],
                    ],
                ],
            ],
            "layout": {
                "symbol-placement": "line",
                "symbol-spacing": 115,
                "text-field": "\u2192",
                "text-font": ["Noto Sans Medium"],
                "text-size": ["interpolate", ["linear"], ["zoom"], 15, 11, 18, 15],
                "text-keep-upright": False,
                "text-rotation-alignment": "map",
                "text-pitch-alignment": "map",
                "text-ignore-placement": False,
                "text-allow-overlap": False,
            },
            "paint": {
                "text-color": p["oneway_arrow"],
                "text-halo-color": p["oneway_halo"],
                "text-halo-width": 1.0,
                "text-opacity": 0.78,
            },
        }
    )

    # Labels: Water -> Roads -> Shields -> Public Reference POIs -> Bairros -> Cities
    layers.extend(
        [
            {
                "id": "water-label",
                "type": "symbol",
                "source": "protomaps",
                "source-layer": "water",
                "minzoom": 12,
                "filter": ["has", "name"],
                "layout": {
                    "symbol-placement": "point",
                    "text-field": NAME_PT,
                    "text-font": ["Noto Sans Italic"],
                    "text-size": ["interpolate", ["linear"], ["zoom"], 12, 10, 15, 12],
                    "text-max-width": 8,
                },
                "paint": {
                    "text-color": p["water_label"],
                    "text-halo-color": p["water_halo"],
                    "text-halo-width": 1.2,
                },
            },
            {
                "id": "roads-label-minor",
                "type": "symbol",
                "source": "protomaps",
                "source-layer": "roads",
                "minzoom": 14,
                "filter": [
                    "all",
                    ["has", "name"],
                    ["in", ["get", "kind_detail"], ["literal", ["residential", "unclassified", "living_street", "service", "pedestrian"]]],
                ],
                "layout": {
                    "symbol-placement": "line",
                    "symbol-spacing": 280,
                    "text-field": NAME_PT,
                    "text-font": ["Noto Sans Regular"],
                    "text-size": ["interpolate", ["linear"], ["zoom"], 14, 10.5, 16, 12, 18, 13.5],
                    "text-rotation-alignment": "map",
                    "text-max-angle": 30,
                    "text-padding": 4,
                },
                "paint": {
                    "text-color": p["road_label"],
                    "text-halo-color": p["road_halo"],
                    "text-halo-width": 1.6,
                },
            },
            {
                "id": "roads-label-major",
                "type": "symbol",
                "source": "protomaps",
                "source-layer": "roads",
                "minzoom": 11,
                "filter": [
                    "all",
                    ["has", "name"],
                    ["in", ["get", "kind_detail"], ["literal", ["motorway", "trunk", "primary", "secondary", "tertiary"]]],
                ],
                "layout": {
                    "symbol-placement": "line",
                    "symbol-spacing": 300,
                    "text-field": NAME_PT,
                    "text-font": ["Noto Sans Medium"],
                    "text-size": ["interpolate", ["linear"], ["zoom"], 11, 10.5, 14, 12, 17, 14],
                    "text-rotation-alignment": "map",
                    "text-max-angle": 30,
                    "text-padding": 4,
                },
                "paint": {
                    "text-color": p["road_label_major"],
                    "text-halo-color": p["road_halo"],
                    "text-halo-width": 1.8,
                },
            },
            {
                "id": "roads-shield",
                "type": "symbol",
                "source": "protomaps",
                "source-layer": "roads",
                "minzoom": 9,
                "filter": [
                    "all",
                    ["has", "ref"],
                    ["in", ["get", "kind_detail"], ["literal", ["motorway", "trunk", "primary"]]],
                ],
                "layout": {
                    "symbol-placement": "line",
                    "symbol-spacing": 420,
                    "text-field": ["coalesce", ["get", "shield_text"], ["get", "ref"]],
                    "text-font": ["Noto Sans Medium"],
                    "text-size": 10.5,
                    "text-rotation-alignment": "viewport",
                    "text-padding": 8,
                },
                "paint": {
                    "text-color": p["shield_text"],
                    "text-halo-color": p["shield_halo"],
                    "text-halo-width": 2.4,
                },
            },
            # Public reference landmarks ONLY (ZERO commercial shops/restaurants/etc.)
            {
                "id": "pois-public-landmarks",
                "type": "symbol",
                "source": "protomaps",
                "source-layer": "pois",
                "minzoom": 13,
                "filter": [
                    "all",
                    ["has", "name"],
                    [
                        "in",
                        ["get", "kind"],
                        [
                            "literal",
                            [
                                "aerodrome",
                                "park",
                                "garden",
                                "national_park",
                                "nature_reserve",
                                "station",
                                "bus_station",
                                "ferry_terminal",
                                "stadium",
                                "sports_centre",
                                "townhall",
                                "university",
                                "attraction",
                            ],
                        ],
                    ],
                ],
                "layout": {
                    "text-field": NAME_PT,
                    "text-font": ["Noto Sans Regular"],
                    "text-size": ["interpolate", ["linear"], ["zoom"], 13, 10, 16, 11.5],
                    "text-max-width": 9,
                    "text-padding": 6,
                },
                "paint": {
                    "text-color": p["poi_label"],
                    "text-halo-color": p["poi_halo"],
                    "text-halo-width": 1.5,
                },
            },
            # Neighborhood / Bairro labels (crucial for Joinville drivers + Blacklist awareness)
            {
                "id": "places-suburb",
                "type": "symbol",
                "source": "protomaps",
                "source-layer": "places",
                "minzoom": 11,
                "maxzoom": 16,
                "filter": [
                    "in",
                    ["get", "kind"],
                    ["literal", ["macrohood", "neighbourhood"]],
                ],
                "layout": {
                    "text-field": ["upcase", NAME_PT],
                    "text-font": ["Noto Sans Medium"],
                    "text-size": ["interpolate", ["linear"], ["zoom"], 11, 10, 13, 11.5, 15, 12.5],
                    "text-letter-spacing": 0.08,
                    "text-max-width": 8,
                    "text-padding": 8,
                },
                "paint": {
                    "text-color": p["suburb_label"],
                    "text-halo-color": p["suburb_halo"],
                    "text-halo-width": 1.8,
                    "text-opacity": ["interpolate", ["linear"], ["zoom"], 11, 0.75, 12.5, 0.92, 15.5, 0.45],
                },
            },
            # Cities / Towns / Villages
            {
                "id": "places-locality",
                "type": "symbol",
                "source": "protomaps",
                "source-layer": "places",
                "minzoom": 4,
                "maxzoom": 14,
                "filter": ["==", ["get", "kind"], "locality"],
                "layout": {
                    "text-field": NAME_PT,
                    "text-font": ["Noto Sans Medium"],
                    "text-size": [
                        "interpolate",
                        ["linear"],
                        ["zoom"],
                        5,
                        11,
                        10,
                        ["case", ["==", ["get", "kind_detail"], "city"], 16, 12.5],
                        13,
                        ["case", ["==", ["get", "kind_detail"], "city"], 18, 13.5],
                    ],
                    "text-max-width": 10,
                    "text-padding": 6,
                },
                "paint": {
                    "text-color": p["city_label"],
                    "text-halo-color": p["city_halo"],
                    "text-halo-width": 2.0,
                },
            },
        ]
    )

    return {
        "version": 8,
        "name": name,
        "center": center or JOINVILLE_CENTER,
        "zoom": 12 if maxzoom <= 14 else 13,
        "sources": {
            "protomaps": {
                "type": "vector",
                "tiles": [f"{base_url}/tiles/{{z}}/{{x}}/{{y}}.mvt"],
                "minzoom": 0,
                "maxzoom": maxzoom,
                "bounds": bounds or JOINVILLE_BOUNDS,
                "attribution": "© OpenStreetMap",
            }
        },
        "glyphs": f"{base_url}/glyphs/{{fontstack}}/{{range}}.pbf",
        "layers": layers,
    }


def validate_style(style: dict[str, Any], glyphs_dir: Path, valid_layers: set[str]) -> None:
    raw = json.dumps(style)
    if "https://" in raw or "http://" in raw.replace(DEFAULT_BASE_URL, ""):
        raise AssertionError("External URL leaked into offline MapLibre style!")
    assert "sprite" not in style, "Offline style must not reference external sprite sheets"
    used_fonts: set[str] = set()
    seen_ids: set[str] = set()
    for layer in style["layers"]:
        lid = layer["id"]
        assert lid not in seen_ids, f"Duplicate layer id: {lid}"
        seen_ids.add(lid)
        sl = layer.get("source-layer")
        if sl is not None:
            assert sl in valid_layers, f"Unknown source-layer '{sl}' in layer '{lid}'"
        tf = layer.get("layout", {}).get("text-font")
        if tf:
            used_fonts.update(tf)
    for font in used_fonts:
        fdir = glyphs_dir / font
        if not fdir.is_dir():
            raise AssertionError(f"Missing glyph folder for font '{font}' at {fdir}")
        for rng in ("0-255.pbf", "256-511.pbf", "8192-8447.pbf", "8448-8703.pbf"):
            if not (fdir / rng).is_file():
                raise AssertionError(f"Missing glyph file {fdir / rng}")


def main() -> int:
    import argparse

    root = Path(__file__).parent
    ap = argparse.ArgumentParser(description="Generate offline MapLibre day/night styles.")
    ap.add_argument("--region", default="joinville")
    ap.add_argument("--regions-file", type=Path, default=root / "states_br.json")
    ap.add_argument("--out-dir", type=Path, default=None)
    ap.add_argument("--glyphs-dir", type=Path, default=None)
    args, unknown = ap.parse_known_args()

    out_dir = args.out_dir or (Path(unknown[0]) if unknown else root / "out")
    out_dir.mkdir(parents=True, exist_ok=True)
    glyphs_dir = args.glyphs_dir or (out_dir / "glyphs")

    center = JOINVILLE_CENTER
    bounds = JOINVILLE_BOUNDS
    maxzoom = 15
    label = "Joinville"
    if args.regions_file.is_file():
        data = json.loads(args.regions_file.read_text(encoding="utf-8"))
        for r in data.get("regions", []):
            if r["id"] == args.region:
                center = r["center"]
                bounds = r["bbox"]
                maxzoom = int(r.get("maxzoom", 14))
                label = r["name"]
                break

    valid_layers = {
        "boundaries",
        "buildings",
        "earth",
        "landcover",
        "landuse",
        "places",
        "pois",
        "roads",
        "water",
    }

    for filename, title, palette in [
        ("style_day.json", f"DriFly {label} Day", DAY_PALETTE),
        ("style_night.json", f"DriFly {label} Night", NIGHT_PALETTE),
    ]:
        style = build_style(title, palette, center=center, bounds=bounds, maxzoom=maxzoom)
        validate_style(style, glyphs_dir, valid_layers)
        dest = out_dir / filename
        dest.write_text(json.dumps(style, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote & validated {dest} ({dest.stat().st_size:,} bytes, {len(style['layers'])} layers, maxzoom={maxzoom})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
