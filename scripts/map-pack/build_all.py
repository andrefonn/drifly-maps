#!/usr/bin/env python3
"""
End-to-end reproducible builder for any DriFly 100% offline map pack
(any of the 27 Brazilian states or Joinville pilot), designed to run on both
free GitHub Actions Ubuntu runners (14 GB disk / 7 GB RAM) and local Windows.

Usage:
  python scripts/map-pack/build_all.py --region pa --out-dir scripts/map-pack/out/pa
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

FALLBACK_PROTOMAPS_BUILD = "20261125.pmtiles"


def latest_protomaps_key() -> str:
    try:
        req = urllib.request.Request(
            "https://build-metadata.protomaps.dev/builds.json",
            headers={"User-Agent": "DriFly-MapPack/1.0"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            builds = json.loads(resp.read().decode("utf-8"))
            if builds:
                return str(builds[-1]["key"])
    except Exception as exc:
        print(f"WARN: failed to query Protomaps builds.json ({exc}); using fallback {FALLBACK_PROTOMAPS_BUILD}", flush=True)
    return FALLBACK_PROTOMAPS_BUILD


def find_pmtiles_bin(root: Path) -> str:
    on_path = shutil.which("pmtiles")
    if on_path:
        return on_path
    win_exe = root / "bin" / "pmtiles.exe"
    if win_exe.is_file():
        return str(win_exe)
    raise FileNotFoundError("pmtiles CLI not found on PATH or scripts/map-pack/bin/pmtiles.exe")


def load_region(regions_file: Path, region_id: str) -> dict:
    data = json.loads(regions_file.read_text(encoding="utf-8"))
    for r in data["regions"]:
        if r["id"] == region_id:
            return r
    raise SystemExit(f"Unknown region '{region_id}' in {regions_file}")


def run(cmd: list[str]) -> None:
    print(f"\n>>> {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, check=True)


def main() -> int:
    root = Path(__file__).parent
    ap = argparse.ArgumentParser(description="Build a complete DriFly offline map pack for any Brazilian region.")
    ap.add_argument("--region", default="joinville", help="Region id in states_br.json (e.g. pa, sc, sp, joinville)")
    ap.add_argument("--regions-file", type=Path, default=root / "states_br.json")
    ap.add_argument("--out-dir", type=Path, default=None)
    ap.add_argument("--work-dir", type=Path, default=root / "work")
    ap.add_argument("--maxzoom", type=int, default=None, help="Override region maxzoom for pmtiles extract")
    ap.add_argument("--protomaps-build", default=None, help="Override Protomaps build key (e.g. 20261125.pmtiles)")
    ap.add_argument("--release-repo", default="andrefonn/drifly-maps")
    ap.add_argument("--release-tag", default="map-packs-v1")
    ap.add_argument("--clean-intermediates", action="store_true", help="Delete clipped .osm.pbf after building zip")
    args = ap.parse_args()

    region = load_region(args.regions_file, args.region)
    rid = region["id"]
    out_dir = args.out_dir or (root / "out" / rid if rid != "joinville" else root / "out")
    out_dir.mkdir(parents=True, exist_ok=True)
    py = sys.executable

    maxzoom = args.maxzoom if args.maxzoom is not None else int(region["maxzoom"])
    minzoom = int(region.get("minzoom", 0))
    w, s, e, n = region["bbox"]
    bbox_str = f"{w},{s},{e},{n}"

    # 1. Extract PMTiles vector map directly via HTTP range requests
    pmtiles_out = out_dir / f"{rid}.pmtiles"
    if not pmtiles_out.exists():
        pm_bin = find_pmtiles_bin(root)
        build_key = args.protomaps_build or latest_protomaps_key()
        pm_url = f"https://build.protomaps.com/{build_key}"
        run(
            [
                pm_bin,
                "extract",
                pm_url,
                str(pmtiles_out),
                f"--bbox={bbox_str}",
                f"--minzoom={minzoom}",
                f"--maxzoom={maxzoom}",
                "--download-threads=8",
            ]
        )

    # 2. Fetch PBF font glyphs (reuse if already present)
    glyphs_dir = out_dir / "glyphs"
    if not (glyphs_dir / "Noto Sans Regular" / "0-255.pbf").is_file():
        run([py, str(root / "fetch_glyphs.py"), str(glyphs_dir)])

    # 3. Build offline day/night MapLibre styles
    run(
        [
            py,
            str(root / "build_styles.py"),
            "--region",
            rid,
            "--regions-file",
            str(args.regions_file),
            "--out-dir",
            str(out_dir),
            "--glyphs-dir",
            str(glyphs_dir),
        ]
    )

    # 4. Download Geofabrik region PBF, clip to exact state polygon, delete regional PBF
    clipped_pbf = out_dir / f"{rid}.osm.pbf"
    if not clipped_pbf.exists():
        run(
            [
                py,
                str(root / "fetch_and_clip_osm.py"),
                "--region",
                rid,
                "--regions-file",
                str(args.regions_file),
                "--out",
                str(clipped_pbf),
                "--work-dir",
                str(args.work_dir),
            ]
        )

    # 5. Build SQLite FTS5 offline geocoder
    run(
        [
            py,
            str(root / "build_geocoder.py"),
            "--region",
            rid,
            "--regions-file",
            str(args.regions_file),
            "--pbf",
            str(clipped_pbf),
            "--db",
            str(out_dir / f"{rid}_geocoder.db"),
        ]
    )

    # 6. Build DFRG v1 compact binary routing graph
    run(
        [
            py,
            str(root / "build_dfrg.py"),
            "--region",
            rid,
            "--pbf",
            str(clipped_pbf),
            "--out",
            str(out_dir / "routing" / "routing.dfrg"),
        ]
    )

    # 7. Package zip + sidecar metadata
    run(
        [
            py,
            str(root / "package_pack.py"),
            "--region",
            rid,
            "--regions-file",
            str(args.regions_file),
            "--out-dir",
            str(out_dir),
            "--release-repo",
            args.release_repo,
            "--release-tag",
            args.release_tag,
            "--maxzoom",
            str(maxzoom),
        ]
    )

    if args.clean_intermediates and clipped_pbf.exists():
        clipped_pbf.unlink()
        print(f"Removed intermediate {clipped_pbf}", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
