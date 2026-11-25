#!/usr/bin/env python3
"""
Package any Brazilian state or city offline map pack (`<id>-pack-v1.zip`),
`pack_meta.json`, `<id>-pack-v1.meta.json` (CI catalog sidecar), and legacy
`manifest.json` with SHA-256 checksums.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import struct
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

PACK_VERSION = 1
DEFAULT_REPO = "andrefonn/drifly-maps"
DEFAULT_TAG = "map-packs-v1"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def inspect_dfrg_header(path: Path) -> dict:
    with path.open("rb") as f:
        hdr = f.read(48)
    magic, ver, _flags, n_cnt, e_cnt, name_cnt, g_cnt, min_lat, min_lng, max_lat, max_lng, _res = (
        struct.unpack("<4sHHiiiiiiii8s", hdr)
    )
    assert magic == b"DFRG" and ver == 1
    return {
        "format": "DFRG",
        "version": ver,
        "nodes": n_cnt,
        "edges": e_cnt,
        "names": name_cnt,
        "geom_points": g_cnt,
        "bbox": [min_lng / 1e6, min_lat / 1e6, max_lng / 1e6, max_lat / 1e6],
    }


def inspect_geocoder_meta(path: Path) -> dict:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    conn.close()
    return meta


def find_pmtiles_bin(root: Path) -> str:
    on_path = shutil.which("pmtiles")
    if on_path:
        return on_path
    win_exe = root / "bin" / "pmtiles.exe"
    if win_exe.is_file():
        return str(win_exe)
    raise FileNotFoundError("pmtiles CLI not found on PATH or scripts/map-pack/bin/pmtiles.exe")


def inspect_pmtiles_layers(pmtiles_bin: str, pmtiles_path: Path) -> list[str]:
    out = subprocess.check_output([pmtiles_bin, "show", "--metadata", str(pmtiles_path)])
    meta = json.loads(out.decode("utf-8"))
    return [layer["id"] for layer in meta.get("vector_layers", [])]


def load_region(regions_file: Path, region_id: str) -> dict:
    data = json.loads(regions_file.read_text(encoding="utf-8"))
    for r in data["regions"]:
        if r["id"] == region_id:
            return r
    raise SystemExit(f"Unknown region '{region_id}' in {regions_file}")


def main() -> int:
    root = Path(__file__).parent
    ap = argparse.ArgumentParser(description="Package offline map pack zip + metadata sidecar.")
    ap.add_argument("--region", default="joinville")
    ap.add_argument("--regions-file", type=Path, default=root / "states_br.json")
    ap.add_argument("--out-dir", type=Path, default=None)
    ap.add_argument("--release-repo", default=DEFAULT_REPO)
    ap.add_argument("--release-tag", default=DEFAULT_TAG)
    ap.add_argument("--maxzoom", type=int, default=None, help="Override actual extracted maxzoom")
    args, unknown = ap.parse_known_args()

    region = load_region(args.regions_file, args.region)
    rid = region["id"]
    out_dir = args.out_dir or (Path(unknown[0]) if unknown else root / "out")

    pmtiles_path = out_dir / f"{rid}.pmtiles"
    geocoder_path = out_dir / f"{rid}_geocoder.db"
    dfrg_path = out_dir / "routing" / "routing.dfrg"
    style_day_path = out_dir / "style_day.json"
    style_night_path = out_dir / "style_night.json"

    for rf in (pmtiles_path, geocoder_path, dfrg_path, style_day_path, style_night_path):
        if not rf.is_file():
            raise FileNotFoundError(f"Missing required pack asset: {rf}")

    glyphs_dir = out_dir / "glyphs"
    glyph_files = sorted(glyphs_dir.rglob("*.pbf"))
    if not glyph_files:
        raise FileNotFoundError(f"No PBF glyph files found under {glyphs_dir}")

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    dfrg_info = inspect_dfrg_header(dfrg_path)
    geocoder_meta = inspect_geocoder_meta(geocoder_path)
    pmtiles_layers = inspect_pmtiles_layers(find_pmtiles_bin(root), pmtiles_path)
    maxzoom = args.maxzoom if args.maxzoom is not None else int(region["maxzoom"])
    minzoom = int(region.get("minzoom", 0))

    file_sizes = {
        pmtiles_path.name: pmtiles_path.stat().st_size,
        geocoder_path.name: geocoder_path.stat().st_size,
        "routing/routing.dfrg": dfrg_path.stat().st_size,
        "style_day.json": style_day_path.stat().st_size,
        "style_night.json": style_night_path.stat().st_size,
        "glyphs_total": sum(p.stat().st_size for p in glyph_files),
    }
    uncompressed_total = sum(file_sizes.values())

    pack_meta = {
        "id": rid,
        "uf": region["uf"],
        "name": region["name"],
        "kind": region["kind"],
        "version": PACK_VERSION,
        "schema_version": 1,
        "bbox": region["bbox"],
        "center": region["center"],
        "created_at": now_utc,
        "pmtiles": {
            "file": pmtiles_path.name,
            "minzoom": minzoom,
            "maxzoom": maxzoom,
            "layers": pmtiles_layers,
        },
        "geocoder": {
            "file": geocoder_path.name,
            "fts_table": "places_fts",
            "places_table": "places",
            "street_numbers_table": "street_numbers",
            "counts": {
                "cities": int(geocoder_meta.get("count_cities", 0)),
                "suburbs": int(geocoder_meta.get("count_suburbs", 0)),
                "streets": int(geocoder_meta.get("count_streets", 0)),
                "pois": int(geocoder_meta.get("count_pois", 0)),
                "street_numbers": int(geocoder_meta.get("count_street_numbers", 0)),
            },
        },
        "routing": {"file": "routing/routing.dfrg", **dfrg_info},
        "styles": {
            "day": "style_day.json",
            "night": "style_night.json",
            "glyphs_dir": "glyphs",
            "fonts": sorted({p.parent.name for p in glyph_files}),
        },
        "file_sizes": file_sizes,
        "uncompressed_total_bytes": uncompressed_total,
    }

    pack_meta_path = out_dir / "pack_meta.json"
    pack_meta_path.write_text(json.dumps(pack_meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    zip_filename = f"{rid}-pack-v{PACK_VERSION}.zip"
    zip_path = out_dir / zip_filename
    if zip_path.exists():
        zip_path.unlink()

    with zipfile.ZipFile(zip_path, "w", allowZip64=True) as zf:
        zf.write(pmtiles_path, pmtiles_path.name, compress_type=zipfile.ZIP_STORED)
        zf.write(geocoder_path, geocoder_path.name, compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)
        zf.write(dfrg_path, "routing/routing.dfrg", compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)
        zf.write(style_day_path, "style_day.json", compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        zf.write(style_night_path, "style_night.json", compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        zf.write(pack_meta_path, "pack_meta.json", compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        for gf in glyph_files:
            zf.write(gf, f"glyphs/{gf.parent.name}/{gf.name}", compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)

    with zipfile.ZipFile(zip_path, "r") as zf:
        bad = zf.testzip()
        assert bad is None, f"Corrupt zip entry: {bad}"
        for name in zf.namelist():
            assert not name.startswith("/") and ".." not in name.split("/"), f"Unsafe zip entry: {name}"
        entry_count = len(zf.namelist())

    zip_size = zip_path.stat().st_size
    zip_sha256 = sha256_file(zip_path)
    zip_url = f"https://github.com/{args.release_repo}/releases/download/{args.release_tag}/{zip_filename}"

    # Standalone sidecar metadata for CI `build_catalog.py`
    sidecar = {
        "id": rid,
        "uf": region["uf"],
        "name": region["name"],
        "kind": region["kind"],
        "version": PACK_VERSION,
        "available": True,
        "bbox": region["bbox"],
        "zip_url": zip_url,
        "filename": zip_filename,
        "size_bytes": zip_size,
        "installed_size_bytes": uncompressed_total,
        "sha256": zip_sha256,
        "routing_nodes": dfrg_info["nodes"],
        "routing_edges": dfrg_info["edges"],
        "routing_geom_points": dfrg_info["geom_points"],
        "minzoom": minzoom,
        "maxzoom": maxzoom,
        "created_at": now_utc,
    }
    sidecar_path = out_dir / f"{rid}-pack-v{PACK_VERSION}.meta.json"
    sidecar_path.write_text(json.dumps(sidecar, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"Packaged {zip_path}:")
    print(f"  Entries : {entry_count}")
    print(f"  Zip Size: {zip_size:,} bytes ({zip_size / (1024 * 1024):.2f} MB)")
    print(f"  Uncomp  : {uncompressed_total:,} bytes ({uncompressed_total / (1024 * 1024):.2f} MB)")
    print(f"  SHA-256 : {zip_sha256}")
    print(f"  Sidecar : {sidecar_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
