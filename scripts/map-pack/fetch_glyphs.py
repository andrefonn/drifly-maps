#!/usr/bin/env python3
"""
Download minimal self-contained PBF font glyph ranges from Protomaps basemaps-assets
for 100% offline MapLibre rendering in Portuguese (pt-BR) + one-way arrow glyphs.

Ranges:
  - 0-255.pbf      : Basic Latin + Latin-1 Supplement (all Portuguese accents: áàâãçéêíóôõúºª°)
  - 256-511.pbf    : Latin Extended-A/B (European/Germanic street names in Joinville)
  - 8192-8447.pbf  : General Punctuation (en/em dashes, quotes, ›, •)
  - 8448-8703.pbf  : Letterlike & Arrows (→ U+2192 for crisp sprite-free one-way road arrows)
"""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

BASE_URL = "https://protomaps.github.io/basemaps-assets/fonts"
FONTS = [
    "Noto Sans Regular",
    "Noto Sans Medium",
    "Noto Sans Italic",
]
RANGES = [
    "0-255.pbf",
    "256-511.pbf",
    "8192-8447.pbf",
    "8448-8703.pbf",
]


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "out" / "glyphs"
    out_dir.mkdir(parents=True, exist_ok=True)

    total_bytes = 0
    manifest: dict[str, dict[str, int]] = {}

    for font in FONTS:
        font_dir = out_dir / font
        font_dir.mkdir(parents=True, exist_ok=True)
        manifest[font] = {}
        for rng in RANGES:
            url = f"{BASE_URL}/{urllib.parse.quote(font)}/{rng}"
            dest = font_dir / rng
            req = urllib.request.Request(url, headers={"User-Agent": "DriFly-MapPack/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            if len(data) == 0:
                raise RuntimeError(f"Empty glyph file downloaded from {url}")
            dest.write_bytes(data)
            manifest[font][rng] = len(data)
            total_bytes += len(data)
            print(f"  OK  {font}/{rng:14s}  {len(data):>7,} bytes")

    print(f"TOTAL GLYPHS: {total_bytes:,} bytes ({total_bytes / 1024:.1f} KB) in {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
