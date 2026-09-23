#!/usr/bin/env python3
"""
tileset_preview.py - Merge a topo2stl `--tile` tileset back into one STL,
each tile placed at its assembled position, so you can look at the whole map
in viewer.py before you commit to printing/gluing it.

Preview only: pegs and sockets are *not* booleaned across tiles here, tiles
are just placed side by side, so don't slice this merged file for printing -
print the individual tile STLs listed in the manifest instead.

Usage:
  python tileset_preview.py NAME.tileset.json [-o preview.stl] [--gap 2] [--view]
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

import numpy as np

from topo2stl import write_binary_stl, launch_viewer


def read_binary_stl(path: Path) -> np.ndarray:
    """Binary STL -> (n, 3, 3) float32 triangle soup (drops normals/attrs)."""
    dt_tri = np.dtype([("normal", "<f4", (3,)), ("verts", "<f4", (3, 3)),
                       ("attr", "<u2")])
    with open(path, "rb") as f:
        f.seek(80)
        n = struct.unpack("<I", f.read(4))[0]
        recs = np.fromfile(f, dtype=dt_tri, count=n)
    return recs["verts"].astype(np.float32)


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Merge a topo2stl tileset into one preview STL.")
    p.add_argument("manifest", help="the *.tileset.json written alongside a --tile run")
    p.add_argument("-o", "--output", help="merged preview STL path")
    p.add_argument("--gap", type=float, default=0.0,
                   help="extra mm gap between tiles in the preview - purely "
                        "visual, makes seams easy to spot (default: butted)")
    p.add_argument("--view", action="store_true", help="open the result in viewer.py")
    a = p.parse_args(argv)

    manifest_path = Path(a.manifest)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    tiles = manifest["tiles"]
    trows, tcols = manifest["tile_grid"]

    by_rc = {(t["row"], t["col"]): t for t in tiles}
    col_w = [by_rc[(1, c)]["width_mm"] for c in range(1, tcols + 1)]
    row_h = [by_rc[(r, 1)]["height_mm"] for r in range(1, trows + 1)]
    # row 1 = north = highest Y; col 1 = west = lowest X (matches topo2stl's
    # own model frame). Rows/cols share exact widths/heights across the grid
    # (see AGENTS.md), so any row/col's tile can be used to size the others.
    x_off = [sum(col_w[:c]) + c * a.gap for c in range(tcols)]
    y_off = [sum(row_h[r + 1:]) + (trows - 1 - r) * a.gap for r in range(trows)]

    parts = []
    for t in tiles:
        r, c = t["row"], t["col"]
        stl_path = manifest_path.with_name(t["file"])
        v = read_binary_stl(stl_path)
        v[:, :, 0] += x_off[c - 1]
        v[:, :, 1] += y_off[r - 1]
        parts.append(v)
        print(f"  tile {t['label']}: {stl_path.name}  ({len(v)} triangles)")

    tris = np.concatenate(parts, axis=0)
    base = manifest_path.name[:-len(".tileset.json")] \
        if manifest_path.name.endswith(".tileset.json") else manifest_path.stem
    out = Path(a.output) if a.output else manifest_path.with_name(base + ".preview.stl")
    write_binary_stl(tris, out, "topo2stl tileset preview")
    print(f"Assembled {len(tiles)} tiles -> {out}")
    print("  preview only - pegs/sockets aren't unioned across tiles; print "
          "the individual tile files, not this one")

    if a.view:
        launch_viewer(out)


if __name__ == "__main__":
    sys.exit(main())
