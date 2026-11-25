#!/usr/bin/env python3
"""
Download the Geofabrik Brazil regional OSM PBF for any region in `states_br.json`
(all 27 Brazilian states + Joinville), clip cleanly to the region's exact IBGE
polygon using `osmium-tool` (Linux CI) or Osmosis 0.49.2 (Windows), and delete
the regional Geofabrik PBF immediately to minimize disk usage.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

OSMOSIS_URL = "https://github.com/openstreetmap/osmosis/releases/download/0.49.2/osmosis-0.49.2.zip"


def load_region(regions_file: Path, region_id: str) -> tuple[dict, str]:
    data = json.loads(regions_file.read_text(encoding="utf-8"))
    base_url = data["geofabrik_base_url"].rstrip("/")
    for r in data["regions"]:
        if r["id"] == region_id:
            return r, base_url
    raise SystemExit(f"Unknown region id '{region_id}' in {regions_file}")


def write_osmosis_poly(region: dict, poly_path: Path) -> None:
    """Write standard Osmosis/osmium .poly file from region polygon [[lng, lat], ...]."""
    poly_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [region["id"], "1"]
    for lng, lat in region["polygon"]:
        lines.append(f"   {lng:.6f}   {lat:.6f}")
    lines.extend(["END", "END", ""])
    poly_path.write_text("\n".join(lines), encoding="utf-8")


def download_file(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    curl_bin = "curl.exe" if os.name == "nt" else "curl"
    print(f"Downloading {url} -> {dest} ...", flush=True)
    t0 = time.time()
    subprocess.run([curl_bin, "-fSL", "--retry", "3", "-o", str(dest), url], check=True)
    size_mb = dest.stat().st_size / (1024 * 1024)
    print(f"Downloaded {size_mb:.2f} MB in {time.time() - t0:.1f}s", flush=True)


def ensure_osmosis(bin_dir: Path, work_dir: Path) -> Path:
    osmosis_dir = bin_dir / "osmosis"
    found = list(osmosis_dir.rglob("osmosis.bat")) if osmosis_dir.exists() else []
    if found:
        return found[0]
    ozip = work_dir / "osmosis.zip"
    download_file(OSMOSIS_URL, ozip)
    osmosis_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(ozip, "r") as zf:
        zf.extractall(osmosis_dir)
    ozip.unlink(missing_ok=True)
    found = list(osmosis_dir.rglob("osmosis.bat"))
    if not found:
        raise FileNotFoundError(f"osmosis.bat not found under {osmosis_dir}")
    return found[0]


def clip_with_osmium(work_pbf: Path, poly_path: Path, out_pbf: Path) -> None:
    osmium_bin = shutil.which("osmium")
    if not osmium_bin:
        raise RuntimeError("osmium binary not found on PATH")
    print(f"Clipping via osmium-tool ({poly_path.name}) -> {out_pbf}...", flush=True)
    subprocess.run(
        [
            osmium_bin,
            "extract",
            "-p",
            str(poly_path),
            "--strategy=complete_ways",
            "--overwrite",
            "-o",
            str(out_pbf),
            str(work_pbf),
        ],
        check=True,
    )


def clip_with_osmosis(
    osmosis_bat: Path,
    work_pbf: Path,
    region: dict,
    poly_path: Path,
    out_pbf: Path,
) -> None:
    env = os.environ.copy()
    env["JAVACMD_OPTIONS"] = "-Xmx1536m"
    if region["kind"] == "city":
        w, s, e, n = region["bbox"]
        filter_args = [
            "--bounding-box",
            f"left={w}",
            f"bottom={s}",
            f"right={e}",
            f"top={n}",
            "clipIncompleteEntities=true",
        ]
    else:
        filter_args = [
            "--bounding-polygon",
            f"file={poly_path}",
            "clipIncompleteEntities=true",
        ]
    print(f"Clipping via Osmosis ({region['id']}) -> {out_pbf}...", flush=True)
    subprocess.run(
        [
            str(osmosis_bat),
            "--read-pbf-fast",
            "workers=4",
            f"file={work_pbf}",
            *filter_args,
            "--write-pbf",
            f"file={out_pbf}",
            "omitmetadata=true",
        ],
        env=env,
        check=True,
    )


def main() -> int:
    root = Path(__file__).parent
    ap = argparse.ArgumentParser(description="Fetch & clip OSM PBF for a Brazilian state or city.")
    ap.add_argument("--region", default="joinville", help="Region id from states_br.json (e.g. pa, sc, joinville)")
    ap.add_argument("--regions-file", type=Path, default=root / "states_br.json")
    ap.add_argument("--out", type=Path, default=None, help="Output PBF path")
    ap.add_argument("--work-dir", type=Path, default=root / "work")
    ap.add_argument("--keep-temp", action="store_true", help="Do not delete regional Geofabrik PBF")
    args = ap.parse_args()

    region, base_url = load_region(args.regions_file, args.region)
    geo_reg = region["geofabrik_region"]
    url = f"{base_url}/{geo_reg}-latest.osm.pbf"

    work_dir: Path = args.work_dir
    work_dir.mkdir(parents=True, exist_ok=True)
    work_pbf = work_dir / f"{geo_reg}-latest.osm.pbf"
    poly_path = work_dir / f"{region['id']}.poly"
    out_pbf = args.out or (root / "out" / f"{region['id']}.osm.pbf")
    out_pbf.parent.mkdir(parents=True, exist_ok=True)

    write_osmosis_poly(region, poly_path)

    try:
        if not work_pbf.exists():
            download_file(url, work_pbf)

        t0 = time.time()
        if shutil.which("osmium"):
            clip_with_osmium(work_pbf, poly_path, out_pbf)
        else:
            osmosis_bat = ensure_osmosis(root / "bin", work_dir)
            clip_with_osmosis(osmosis_bat, work_pbf, region, poly_path, out_pbf)

        out_mb = out_pbf.stat().st_size / (1024 * 1024)
        print(f"Clipped {out_pbf} ({out_mb:.2f} MB) in {time.time() - t0:.1f}s", flush=True)
    finally:
        poly_path.unlink(missing_ok=True)
        if not args.keep_temp and work_pbf.exists():
            print(f"Cleaning up regional extract {work_pbf}...", flush=True)
            work_pbf.unlink()
            print("Deleted regional PBF.", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
