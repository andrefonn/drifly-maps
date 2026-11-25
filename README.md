# DriFly 100% Offline Map Packs (`drifly-maps`)

Reproducible build pipeline and catalog generator for DriFly's **Hybrid Brazil-wide Offline Map** packs (all 27 Brazilian states + Joinville pilot city pack).

## Pack Contents (`<id>-pack-v1.zip`)

Each downloadable pack is 100% self-contained and requires zero network calls after installation:

- `<id>.pmtiles` — Vector map tiles (Protomaps v4 schema, zoom `0..14` for states, `0..15` for compact states & cities)
- `<id>_geocoder.db` — SQLite FTS5 offline address, street, neighborhood, house-number & public POI database
- `routing/routing.dfrg` — Compact Little-Endian CSR binary road graph (`DFRG` v1) for `< 20 ms` Bidirectional A* routing
- `style_day.json`, `style_night.json` — Clean driver-focused MapLibre styles (zero external URLs, zero commercial POI clutter)
- `glyphs/` — Self-contained Noto Sans PBF glyph ranges covering Portuguese accents & sprite-free one-way arrows
- `pack_meta.json` — Pack metadata and checksums

## Building a State Pack

### On GitHub Actions (Recommended — Free Ubuntu Runner)

1. Ensure your GitHub CLI token has the `workflow` scope (once):
   ```bash
   gh auth refresh -h github.com -s workflow
   ```
   *(Or copy `workflows-template/build-packs.yml` into `.github/workflows/build-packs.yml` via the GitHub Web UI.)*
2. Trigger a build for one or more states (or `all` for all 27 Brazilian states):
   ```bash
   gh workflow run build-packs.yml -R andrefonn/drifly-maps -f regions=pa
   ```

### Locally (Linux / Windows)

```bash
python scripts/map-pack/build_all.py --region pa --out-dir scripts/map-pack/out/pa --clean-intermediates
python scripts/map-pack/build_catalog.py --meta-dir scripts/map-pack/out --out scripts/map-pack/out/catalog.json
```

## Data Sources & Attributions

- **Map & Routing Data:** © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors, licensed under the [ODbL 1.0](https://opendatacommons.org/licenses/odbl/). Regional extracts provided by [Geofabrik](https://download.geofabrik.de/).
- **Offline Vector Tiles:** Built from [Protomaps](https://protomaps.com/) daily Basemap builds (OpenStreetMap + Natural Earth).
- **Online Hybrid Map Tiles:** [OpenFreeMap](https://openfreemap.org/) © [OpenMapTiles](https://openmaptiles.org/) Data from OpenStreetMap.
- **Brazilian State Boundaries:** [IBGE — Malhas Territoriais v3](https://servicodados.ibge.gov.br/api/docs/malhas?versao=3) (Public Domain / Brazilian Federal Government Open Data).
- **Map Typography:** [Noto Sans](https://fonts.google.com/noto/specimen/Noto+Sans) licensed under the [SIL Open Font License 1.1](https://scripts.sil.org/OFL).
