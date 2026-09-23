#!/usr/bin/env python3
"""
tileset_preview.py - Merge a topo2stl `--tile` tileset back into one STL,
each tile placed at its assembled position, so you can look at the whole map
in viewer.py before you commit to printing/gluing it.

Preview only: pegs and sockets are *not* booleaned across tiles here, tiles
are just placed side by side, so don't slice this merged file for printing -
print the individual tile STLs listed in the manifest instead.

Also writes a .topo.json sidecar next to the merged STL with each seam
traced out as a polyline hugging the actual terrain edge (viewer.py draws
these as a toggleable "Seams" overlay) plus the assembled bbox (so the
usual corner-coordinate labels work on the merged view too).

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


SEAM_LIFT = 0.15  # mm proud of the surface, so the line never z-fights it


def _wall_profile(verts: np.ndarray, axis: int, plane: float, base_mm: float,
                  eps: float = 1e-2) -> list:
    """The terrain top edge along one wall of an (already-offset) tile mesh,
    as a [x, y, z] polyline ordered along the wall.

    `axis` is the coordinate held constant at `plane` (0=x for an east/west
    wall, 1=y for a north/south wall). A wall is a flat vertical rectangle
    except where a peg/socket pokes through it, so `z >= base_mm` cleanly
    keeps the top (terrain) edge and drops the bottom edge (z=0) and any
    peg/socket geometry (centred at base_mm/2, by construction always
    shorter than base_mm - see _tile_seam_geoms in topo2stl.py)."""
    on_plane = np.abs(verts[:, axis] - plane) < eps
    top = verts[on_plane & (verts[:, 2] >= base_mm - eps)]
    if len(top) == 0:
        return []
    top = top[np.argsort(top[:, 1 - axis])]
    keep = np.ones(len(top), bool)
    keep[1:] = np.any(np.abs(np.diff(top, axis=0)) > 1e-4, axis=1)  # de-dupe shared verts
    top = top[keep]
    top[:, 2] += SEAM_LIFT
    return top.tolist()


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Merge a topo2stl tileset into one preview STL.")
    p.add_argument("manifest", help="the *.tileset.json written alongside a --tile run")
    p.add_argument("-o", "--output", help="merged preview STL path")
    p.add_argument("--gap", type=float, default=0.0,
                   help="extra mm gap between tiles in the preview - purely "
                        "visual (default: butted). Seam lines (see below) "
                        "are only drawn when the tiles are butted (gap 0), "
                        "since a gap already makes the seam obvious")
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

    parts, v_seams, h_seams = [], [], []
    for t in tiles:
        r, c = t["row"], t["col"]
        stl_path = manifest_path.with_name(t["file"])
        v = read_binary_stl(stl_path)
        xo, yo = x_off[c - 1], y_off[r - 1]
        v[:, :, 0] += xo
        v[:, :, 1] += yo
        parts.append(v)
        print(f"  tile {t['label']}: {stl_path.name}  ({len(v)} triangles)")

        # each tile draws the seam(s) it "owns" - its east wall if it has an
        # east neighbour, its south wall if it has a south neighbour. That's
        # exactly the walls write_tiles put pegs on, one line per seam.
        if a.gap == 0.0:
            verts = v.reshape(-1, 3)
            sidecar = stl_path.with_name(stl_path.stem + ".topo.json")
            base_mm = json.loads(sidecar.read_text())["base_mm"] if sidecar.exists() else 0.0
            if c < tcols:
                v_seams.append(_wall_profile(verts, 0, xo + t["width_mm"], base_mm))
            if r < trows:
                h_seams.append(_wall_profile(verts, 1, yo, base_mm))

    tris = np.concatenate(parts, axis=0)
    base = manifest_path.name[:-len(".tileset.json")] \
        if manifest_path.name.endswith(".tileset.json") else manifest_path.stem
    out = Path(a.output) if a.output else manifest_path.with_name(base + ".preview.stl")
    write_binary_stl(tris, out, "topo2stl tileset preview")
    print(f"Assembled {len(tiles)} tiles -> {out}")
    print("  preview only - pegs/sockets aren't unioned across tiles; print "
          "the individual tile files, not this one")

    sidecar_out = out.with_name(out.stem + ".topo.json")
    meta = {"bbox": manifest.get("bbox"), "generator": "tileset_preview",
           "seams": {"vertical": [s for s in v_seams if s],
                     "horizontal": [s for s in h_seams if s]}}
    sidecar_out.write_text(json.dumps(meta), encoding="utf-8")
    n_seams = len(meta["seams"]["vertical"]) + len(meta["seams"]["horizontal"])
    print(f"Wrote {sidecar_out} ({n_seams} seam lines" +
          (")" if a.gap == 0.0 else " skipped - only drawn with --gap 0)"))

    if a.view:
        launch_viewer(out)


if __name__ == "__main__":
    sys.exit(main())
