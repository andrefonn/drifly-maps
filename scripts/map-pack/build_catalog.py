#!/usr/bin/env python3
"""
Generate `catalog.json` (Schema v1) for DriFly's Hybrid Brazil-wide Offline Map.

Merges all 27 Brazilian states + Joinville from `states_br.json` (official IBGE
simplified polygons <= 200 pts & bboxes) with built pack sidecar metadata
(`*-pack-v1.meta.json`, or legacy `manifest.json` for Joinville).

Live built packs have `"available": true` with exact `zip_url`, `size_bytes`,
`installed_size_bytes`, `sha256`, `routing_nodes`, `minzoom`, `maxzoom`.
Not-yet-built states have `"available": false` with `bbox` and `polygon` so
the app can detect the user's Brazilian state offline and show "Em breve".
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def collect_sidecars(search_dirs: list[Path]) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for d in search_dirs:
        if not d.exists():
            continue
        for mf in sorted(d.rglob("*-pack-v*.meta.json")):
            data = json.loads(mf.read_text(encoding="utf-8"))
            pid = data.get("id")
            if pid:
                found[pid] = data
        # Fallback: also read legacy manifest.json (e.g. Joinville v1)
        legacy = d / "manifest.json"
        if legacy.is_file():
            ldata = json.loads(legacy.read_text(encoding="utf-8"))
            for p in ldata.get("packs", []):
                pid = p.get("id")
                if pid and pid not in found:
                    found[pid] = {
                        "id": pid,
                        "version": int(p.get("version", 1)),
                        "available": True,
                        "zip_url": p.get("url", ""),
                        "size_bytes": int(p.get("size_bytes", 0)),
                        "installed_size_bytes": int(p.get("uncompressed_bytes", 0)),
                        "sha256": p.get("sha256", ""),
                        "routing_nodes": int(p.get("routing_nodes", 0)),
                        "minzoom": 0,
                        "maxzoom": int(p.get("pmtiles_maxzoom", 15)),
                    }
        # Enrich routing_nodes from pack_meta.json if available
        pm = d / "pack_meta.json"
        if pm.is_file():
            pmdata = json.loads(pm.read_text(encoding="utf-8"))
            pid = pmdata.get("id")
            if pid and pid in found and found[pid].get("routing_nodes", 0) == 0:
                found[pid]["routing_nodes"] = int(pmdata.get("routing", {}).get("nodes", 0))
    return found


def main() -> int:
    root = Path(__file__).parent
    ap = argparse.ArgumentParser(description="Build DriFly Brazil-wide catalog.json (Schema v1).")
    ap.add_argument("--regions-file", type=Path, default=root / "states_br.json")
    ap.add_argument("--meta-dir", type=Path, action="append", default=None, help="Directory containing *.meta.json / manifest.json")
    ap.add_argument("--out", type=Path, default=root / "out" / "catalog.json")
    args = ap.parse_args()

    regions_data = json.loads(args.regions_file.read_text(encoding="utf-8"))
    search_dirs = args.meta_dir if args.meta_dir else [root / "out"]
    sidecars = collect_sidecars(search_dirs)

    packs: list[dict] = []
    avail_count = 0

    for reg in regions_data["regions"]:
        rid = reg["id"]
        poly = reg["polygon"]
        assert len(poly) <= 300, f"Region {rid} polygon exceeds 300 pts ({len(poly)})"
        meta = sidecars.get(rid)
        if meta and meta.get("sha256") and int(meta.get("size_bytes", 0)) > 0:
            avail_count += 1
            packs.append(
                {
                    "id": rid,
                    "uf": reg["uf"],
                    "name": reg["name"],
                    "kind": reg["kind"],
                    "version": int(meta.get("version", 1)),
                    "available": True,
                    "bbox": reg["bbox"],
                    "polygon": poly,
                    "zip_url": meta["zip_url"],
                    "size_bytes": int(meta["size_bytes"]),
                    "installed_size_bytes": int(meta.get("installed_size_bytes", meta["size_bytes"])),
                    "sha256": meta["sha256"],
                    "routing_nodes": int(meta.get("routing_nodes", 0)),
                    "minzoom": int(meta.get("minzoom", reg.get("minzoom", 0))),
                    "maxzoom": int(meta.get("maxzoom", reg["maxzoom"])),
                }
            )
        else:
            packs.append(
                {
                    "id": rid,
                    "uf": reg["uf"],
                    "name": reg["name"],
                    "kind": reg["kind"],
                    "version": 1,
                    "available": False,
                    "bbox": reg["bbox"],
                    "polygon": poly,
                    "zip_url": "",
                    "size_bytes": 0,
                    "installed_size_bytes": 0,
                    "sha256": "",
                    "routing_nodes": 0,
                    "minzoom": int(reg.get("minzoom", 0)),
                    "maxzoom": int(reg["maxzoom"]),
                }
            )

    catalog = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "packs": packs,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(catalog, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(
        f"Wrote {args.out} ({args.out.stat().st_size:,} bytes): "
        f"{len(packs)} entries ({avail_count} available, {len(packs) - avail_count} planned)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
