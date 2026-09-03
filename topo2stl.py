#!/usr/bin/env python3
"""
topo2stl - Generate a 3D-printable STL from public elevation data.

Pipeline:
  1. Work out a lat/lon bounding box (from --bbox or --center + --width-km).
  2. Download a grid of elevations from one of:
       - ign      : Spain's IGN MDT (5 m / 25 m, PNOA-LiDAR) via its free
                    INSPIRE WCS. No key, no cost. Spain only. (default)
       - tessadem : the global TessaDEM elevation API ("area" mode). Needs a
                    paid API key; tiled to stay under the per-request limits.
  3. Turn the grid into a watertight "solid block with base" mesh:
     draped top surface + vertical side walls + flat bottom.
  4. Write a binary STL.

The raw elevation grid is cached on disk (./cache) so re-running with different
model settings does not re-download.

IGN WCS:  https://servicios.idee.es/wcs-inspire/mdt?request=GetCapabilities&service=WCS
TessaDEM: https://tessadem.com/elevation-api/
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import struct
import sys
import time
from pathlib import Path

import numpy as np

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

API_URL = "https://tessadem.com/api/elevation"
API_MAX_CELLS = 16384          # max rows*columns per "area" request
API_MAX_DEGREES = 5.0          # request extent must fit within 5deg x 5deg
API_AREA_RATE_PER_MIN = 300    # "area" mode rate limit
EARTH_M_PER_DEG_LAT = 111_320.0

IGN_WCS_URL = "https://servicios.idee.es/wcs-inspire/mdt"
IGN_COVERAGE = {5: "Elevacion4258_5", 25: "Elevacion4258_25"}

# Bump when the fetch/parse/resample pipeline changes in a way that alters the
# stored grid — old cache files with a different version are ignored.
CACHE_VERSION = 1

CACHE_DIR = Path(__file__).parent / "cache"


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #
def m_per_deg_lon(lat_deg: float) -> float:
    return EARTH_M_PER_DEG_LAT * math.cos(math.radians(lat_deg))


def bbox_from_center(lat: float, lon: float, width_km: float,
                     height_km: float | None) -> tuple[float, float, float, float]:
    height_km = height_km if height_km is not None else width_km
    dlat = (height_km * 1000.0) / EARTH_M_PER_DEG_LAT / 2.0
    dlon = (width_km * 1000.0) / m_per_deg_lon(lat) / 2.0
    return (lat - dlat, lon - dlon, lat + dlat, lon + dlon)


# --------------------------------------------------------------------------- #
# Elevation download
# --------------------------------------------------------------------------- #
def _parse_area_results(payload: dict, rows: int, cols: int) -> np.ndarray:
    """Accept either a 2-D array of numbers or a flat/2-D array of point objects."""
    if "error" in payload:
        err = payload["error"]
        raise RuntimeError(f"API error: {err.get('type')}: {err.get('message')}")
    results = payload["results"]

    def to_elev(x):
        return float(x["elevation"] if isinstance(x, dict) else x)

    flat: list[float] = []

    # flatten arbitrarily nested lists in row-major order
    def walk(node):
        if isinstance(node, list):
            for n in node:
                walk(n)
        else:
            flat.append(to_elev(node))
    walk(results)

    arr = np.asarray(flat, dtype=np.float64)
    if arr.size != rows * cols:
        raise RuntimeError(
            f"expected {rows*cols} elevation samples, got {arr.size}")
    return arr.reshape(rows, cols)


def fetch_area(key: str, sw: tuple[float, float], ne: tuple[float, float],
               rows: int, cols: int, unit: str, session, verbose: bool) -> np.ndarray:
    params = {
        "key": key,
        "mode": "area",
        "rows": rows,
        "columns": cols,
        "unit": unit,
        "format": "json",
        "locations": f"{sw[0]:.8f},{sw[1]:.8f}|{ne[0]:.8f},{ne[1]:.8f}",
    }
    if verbose:
        redacted = dict(params, key="***")
        print(f"  GET {API_URL} {redacted}")
    r = session.get(API_URL, params=params, timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:400]}")
    return _parse_area_results(r.json(), rows, cols)


def download_grid(key: str, bbox: tuple[float, float, float, float],
                  rows: int, cols: int, unit: str, verbose: bool) -> np.ndarray:
    """
    Return an (rows x cols) float array of elevations.
    Row 0 = north (max lat), col 0 = west (min lon).
    Tiles the request to stay under API_MAX_CELLS and API_MAX_DEGREES.
    """
    min_lat, min_lon, max_lat, max_lon = bbox
    if requests is None:
        raise RuntimeError("The 'requests' package is required. pip install -r requirements.txt")

    # global sample coordinates, endpoints inclusive
    lats = np.linspace(max_lat, min_lat, rows)   # north -> south
    lons = np.linspace(min_lon, max_lon, cols)   # west -> east

    lat_span = max_lat - min_lat
    lon_span = max_lon - min_lon

    # Decide tiling: keep growing the tile grid until each tile satisfies both
    # the cell-count limit and the 5-degree extent limit.
    n_tiles_r = n_tiles_c = 1
    while True:
        rr = math.ceil((rows - 1) / n_tiles_r) + 1
        cc = math.ceil((cols - 1) / n_tiles_c) + 1
        lat_deg = lat_span * (rr - 1) / max(rows - 1, 1)
        lon_deg = lon_span * (cc - 1) / max(cols - 1, 1)
        ok_cells = rr * cc <= API_MAX_CELLS
        ok_deg = lat_deg < API_MAX_DEGREES and lon_deg < API_MAX_DEGREES
        if ok_cells and ok_deg:
            break
        if not ok_cells:
            if rr >= cc:
                n_tiles_r += 1
            else:
                n_tiles_c += 1
        else:
            if lat_deg >= API_MAX_DEGREES:
                n_tiles_r += 1
            if lon_deg >= API_MAX_DEGREES:
                n_tiles_c += 1

    grid = np.full((rows, cols), np.nan, dtype=np.float64)
    session = requests.Session()

    # row/col index breakpoints for tiles (overlapping by 1 shared edge)
    r_bounds = np.linspace(0, rows - 1, n_tiles_r + 1).round().astype(int)
    c_bounds = np.linspace(0, cols - 1, n_tiles_c + 1).round().astype(int)

    n_requests = n_tiles_r * n_tiles_c
    min_interval = 60.0 / API_AREA_RATE_PER_MIN
    print(f"Downloading {rows}x{cols} elevation grid in {n_requests} tile request(s)...")

    done = 0
    for ti in range(n_tiles_r):
        r0, r1 = r_bounds[ti], r_bounds[ti + 1]
        for tj in range(n_tiles_c):
            c0, c1 = c_bounds[tj], c_bounds[tj + 1]
            t_rows = r1 - r0 + 1
            t_cols = c1 - c0 + 1
            # SW corner = (south lat, west lon); lats array is north->south
            sw = (float(lats[r1]), float(lons[c0]))
            ne = (float(lats[r0]), float(lons[c1]))
            t0 = time.time()
            sub = fetch_area(key, sw, ne, t_rows, t_cols, unit, session, verbose)
            # sub row 0 = north => aligns with grid[r0]
            grid[r0:r1 + 1, c0:c1 + 1] = sub
            done += 1
            print(f"  tile {done}/{n_requests} rows[{r0}:{r1}] cols[{c0}:{c1}] ok")
            if done < n_requests:
                sleep = min_interval - (time.time() - t0)
                if sleep > 0:
                    time.sleep(sleep)

    if np.isnan(grid).any():
        raise RuntimeError("grid has gaps after download (tiling bug)")
    return grid


# --------------------------------------------------------------------------- #
# IGN MDT (Spain) via INSPIRE WCS - free, no key
# --------------------------------------------------------------------------- #
def _split_multipart_asc(raw: bytes) -> str:
    """The IGN WCS returns the ESRI ASCII grid inside a multipart/related body."""
    text = raw.decode("latin-1")
    i = text.find("ncols")
    if i == -1:
        raise RuntimeError(f"unexpected WCS response: {text[:400]}")
    j = text.find("--wcs", i)          # closing MIME boundary, if present
    return text[i:] if j == -1 else text[i:j]


def _parse_asc(text: str) -> np.ndarray:
    tokens = text.split()
    hdr: dict[str, float] = {}
    k = 0
    while k + 1 < len(tokens):
        key = tokens[k].lower()
        if key in ("ncols", "nrows", "xllcorner", "yllcorner", "xllcenter",
                   "yllcenter", "cellsize", "dx", "dy", "nodata_value"):
            hdr[key] = float(tokens[k + 1])
            k += 2
        else:
            break
    ncols, nrows = int(hdr["ncols"]), int(hdr["nrows"])
    vals = np.asarray(tokens[k:k + ncols * nrows], dtype=np.float64)
    if vals.size != ncols * nrows:
        raise RuntimeError(f"ASC grid: expected {ncols*nrows} values, got {vals.size}")
    grid = vals.reshape(nrows, ncols)          # row 0 = north
    nodata = hdr.get("nodata_value", -9999.0)
    grid[grid == nodata] = np.nan
    return grid


def download_grid_ign(bbox, rows, cols, ign_res, verbose) -> np.ndarray:
    if requests is None:
        raise RuntimeError("The 'requests' package is required. pip install -r requirements.txt")
    min_lat, min_lon, max_lat, max_lon = bbox
    coverage = IGN_COVERAGE[ign_res]
    params = {
        "service": "WCS",
        "version": "2.0.1",
        "request": "GetCoverage",
        "coverageId": coverage,
        "format": "application/asc",
        "subset": [f"Lat({min_lat:.8f},{max_lat:.8f})",
                   f"Long({min_lon:.8f},{max_lon:.8f})"],
        "SCALESIZE": f"Long({cols}),Lat({rows})",
    }
    print(f"Downloading {rows}x{cols} grid from IGN MDT{ign_res:02d} "
          f"({coverage}) ...")
    if verbose:
        print(f"  GET {IGN_WCS_URL} {params}")
    r = requests.get(IGN_WCS_URL, params=params, timeout=180)
    if r.status_code != 200 or b"ExceptionReport" in r.content[:2000]:
        raise RuntimeError(f"IGN WCS error (HTTP {r.status_code}): {r.text[:500]}")
    grid = _parse_asc(_split_multipart_asc(r.content))
    if grid.shape != (rows, cols):
        # server may clamp to coverage extent; accept and let caller see it
        print(f"  note: server returned {grid.shape}, requested {(rows, cols)}")
    if np.isnan(grid).any():
        n = int(np.isnan(grid).sum())
        print(f"  warning: {n} nodata cells (outside MDT coverage?); filled with min")
        grid = np.where(np.isnan(grid), np.nanmin(grid), grid)
    return grid


# --------------------------------------------------------------------------- #
# Cache dispatch
# --------------------------------------------------------------------------- #
def cached_grid(source, key, bbox, rows, cols, unit, ign_res,
                verbose, no_cache) -> np.ndarray:
    CACHE_DIR.mkdir(exist_ok=True)
    sig = json.dumps({"v": CACHE_VERSION, "source": source,
                      "bbox": [round(b, 6) for b in bbox],   # ~0.1 m; dedupes near-identical requests
                      "rows": rows, "cols": cols, "unit": unit,
                      "ign_res": ign_res if source == "ign" else None},
                     sort_keys=True)
    h = hashlib.sha1(sig.encode()).hexdigest()[:16]
    path = CACHE_DIR / f"grid_{source}_v{CACHE_VERSION}_{rows}x{cols}_{h}.npy"
    if path.exists() and not no_cache:
        print(f"Using cached elevation grid: {path.name}")
        return np.load(path)

    if source == "ign":
        grid = download_grid_ign(bbox, rows, cols, ign_res, verbose)
    elif source == "tessadem":
        if not key:
            raise SystemExit(
                "No TessaDEM API key. Set TESSADEM_API_KEY or pass --key.\n"
                "Get one at https://tessadem.com/elevation-api/ (area mode has "
                "no free tier), or use --source ign for free Spanish coverage.\n"
                f"(No cache found at {path})")
        grid = download_grid(key, bbox, rows, cols, unit, verbose)
    else:
        raise SystemExit(f"unknown source: {source}")

    np.save(path, grid)
    print(f"Cached elevation grid -> {path.name}")
    return grid


# --------------------------------------------------------------------------- #
# Grid smoothing
# --------------------------------------------------------------------------- #
def gaussian_blur(a: np.ndarray, sigma: float) -> np.ndarray:
    """Separable Gaussian blur, sigma in grid cells, edge-reflected. numpy only."""
    if sigma <= 0:
        return a
    r = max(1, int(round(sigma * 3)))
    x = np.arange(-r, r + 1)
    k = np.exp(-(x * x) / (2.0 * sigma * sigma))
    k /= k.sum()
    out = a.astype(np.float64)
    for axis in (0, 1):
        pad = [(r, r) if i == axis else (0, 0) for i in range(2)]
        ap = np.pad(out, pad, mode="reflect")
        acc = np.zeros_like(out)
        for t, w in enumerate(k):
            sl = [slice(None), slice(None)]
            sl[axis] = slice(t, t + out.shape[axis])
            acc += w * ap[tuple(sl)]
        out = acc
    return out


# --------------------------------------------------------------------------- #
# Mesh construction
# --------------------------------------------------------------------------- #
def build_mesh(grid_m: np.ndarray, bbox, model_width_mm: float,
               z_exaggeration: float, base_mm: float,
               z_from_sea_level: bool) -> np.ndarray:
    """
    grid_m: elevations in metres, [row0=north, col0=west].
    Returns an (n_tri, 3, 3) float32 array of triangle vertices.
    """
    min_lat, min_lon, max_lat, max_lon = bbox
    rows, cols = grid_m.shape
    mean_lat = (min_lat + max_lat) / 2.0

    real_w_m = (max_lon - min_lon) * m_per_deg_lon(mean_lat)
    real_h_m = (max_lat - min_lat) * EARTH_M_PER_DEG_LAT
    mm_per_m = model_width_mm / real_w_m
    model_h_mm = real_h_m * mm_per_m

    # X: west->east (col), Y: south->north, so flip rows (row0 is north)
    xs = np.linspace(0.0, model_width_mm, cols)
    ys = np.linspace(0.0, model_h_mm, rows)[::-1]

    base_ref = 0.0 if z_from_sea_level else float(np.min(grid_m))
    z = (grid_m - base_ref) * mm_per_m * z_exaggeration
    z = z - z.min() + base_mm            # lift so lowest surface point sits at base_mm

    X, Y = np.meshgrid(xs, ys)           # (rows, cols)
    top = np.stack([X, Y, z], axis=-1)   # (rows, cols, 3)
    bot = np.stack([X, Y, np.zeros_like(z)], axis=-1)

    tris: list = []

    def quad(a, b, c, d, flip=False):
        # quad a->b->c->d listed counter-clockwise as seen from OUTSIDE the
        # solid; emits two outward-facing triangles. `flip` picks the other
        # diagonal (b-d instead of a-c).
        if flip:
            tris.append((a, b, d))
            tris.append((b, c, d))
        else:
            tris.append((a, b, c))
            tris.append((a, c, d))

    # top surface: CCW seen from +Z -> normals point up. Alternate the split
    # diagonal per cell so the triangulation has no directional bias (which
    # otherwise shows as fine corrugation on exaggerated slopes).
    for i in range(rows - 1):
        for j in range(cols - 1):
            quad(top[i, j], top[i + 1, j], top[i + 1, j + 1], top[i, j + 1],
                 flip=(i + j) % 2 == 1)

    # bottom: CCW seen from -Z  ->  normals point down
    for i in range(rows - 1):
        for j in range(cols - 1):
            quad(bot[i, j], bot[i, j + 1], bot[i + 1, j + 1], bot[i + 1, j])

    # walls (row 0 = north / +Y, row rows-1 = south / -Y; col 0 = west / -X)
    for j in range(cols - 1):                       # south edge, normal -Y
        i = rows - 1
        quad(top[i, j + 1], top[i, j], bot[i, j], bot[i, j + 1])
    for j in range(cols - 1):                       # north edge, normal +Y
        quad(top[0, j], top[0, j + 1], bot[0, j + 1], bot[0, j])
    for i in range(rows - 1):                       # west edge, normal -X
        quad(top[i, 0], bot[i, 0], bot[i + 1, 0], top[i + 1, 0])
    for i in range(rows - 1):                       # east edge, normal +X
        j = cols - 1
        quad(top[i + 1, j], bot[i + 1, j], bot[i, j], top[i, j])

    print(f"Model: {model_width_mm:.1f} x {model_h_mm:.1f} mm, "
          f"{len(tris)} triangles")
    print(f"  ground sampling: ~{real_w_m/ (cols-1):.0f} m/px E-W, "
          f"~{real_h_m/(rows-1):.0f} m/px N-S")
    print(f"  relief: {grid_m.max()-grid_m.min():.0f} m -> "
          f"{z.max()-base_mm:.1f} mm  (exaggeration {z_exaggeration}x, base {base_mm} mm)")

    info = {
        "model_w": model_width_mm,
        "model_h": model_h_mm,
        "base_mm": base_mm,
        "m_per_mm": 1.0 / (mm_per_m * z_exaggeration),  # real elevation m per model-Z mm
        "relief_mm": float(z.max() - z.min()),
        # wall-top height (mm) sampled along each edge, ordered low coord -> high
        "south_z": z[rows - 1].copy(),        # x: 0 -> W
        "north_z": z[0].copy(),               # x: 0 -> W
        "west_z": z[::-1, 0].copy(),          # y: 0 -> H
        "east_z": z[::-1, cols - 1].copy(),   # y: 0 -> H
    }
    return tris, info


# --------------------------------------------------------------------------- #
# Embossed corner coordinates (raised 5x7 pixel text on the side walls)
# --------------------------------------------------------------------------- #
_FONT5x7 = {
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
    ".": ["00000", "00000", "00000", "00000", "00000", "01100", "01100"],
    "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
    ":": ["00000", "01100", "01100", "00000", "01100", "01100", "00000"],
    " ": ["00000", "00000", "00000", "00000", "00000", "00000", "00000"],
    "°": ["01100", "10010", "10010", "01100", "00000", "00000", "00000"],
    "N": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
    "S": ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
    "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
    "W": ["10001", "10001", "10001", "10101", "10101", "10101", "01010"],
}
_GLYPH_W, _GLYPH_H, _GLYPH_ADV = 5, 7, 6   # cell is 5 wide, 7 tall, +1 px gap


def _box_tris(p, A, B, C):
    """The 12 outward-facing triangles of the parallelepiped p + iA + jB + kC."""
    p = np.asarray(p, float); A = np.asarray(A, float)
    B = np.asarray(B, float); C = np.asarray(C, float)
    v = [p + i * A + j * B + k * C
         for k in (0, 1) for j in (0, 1) for i in (0, 1)]     # idx = i + 2j + 4k
    ctr = p + 0.5 * (A + B + C)
    out = []
    for f in ((0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1),
              (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)):
        q = [v[f[0]], v[f[1]], v[f[2]], v[f[3]]]
        nrm = np.cross(q[1] - q[0], q[2] - q[0])
        if np.dot(nrm, (q[0] + q[1] + q[2] + q[3]) / 4 - ctr) < 0:
            q = q[::-1]
        out.append((q[0], q[1], q[2]))
        out.append((q[0], q[2], q[3]))
    return out


def _text_pixel_boxes(text: str, origin, u_axis, v_axis, normal,
                      px: float, out_mm: float, in_mm: float):
    """
    One box per horizontal run of 'on' pixels in `text`. Each box straddles the
    wall surface: out_mm proud of it, in_mm behind it. Returns a list of
    (corner, edge_u, edge_v, edge_n) tuples (all axis-aligned in practice).
    """
    o = np.asarray(origin, float)
    ua, va, na = (np.asarray(x, float) for x in (u_axis, v_axis, normal))
    B, C = va * px, na * (out_mm + in_mm)
    boxes = []
    for gi, ch in enumerate(text):
        glyph = _FONT5x7.get(ch, _FONT5x7.get(ch.upper()))
        if glyph is None:
            continue
        base_c = gi * _GLYPH_ADV
        for r in range(_GLYPH_H):
            row = glyph[r]
            c = 0
            while c < _GLYPH_W:
                if row[c] != "1":
                    c += 1
                    continue
                c0 = c
                while c < _GLYPH_W and row[c] == "1":
                    c += 1
                u = (base_c + c0) * px
                w = (_GLYPH_H - 1 - r) * px
                corner = o + ua * u + va * w - na * in_mm
                boxes.append((corner, ua * (px * (c - c0)), B, C))
    return boxes


def _have_manifold() -> bool:
    try:
        import manifold3d  # noqa: F401
        return True
    except ImportError:
        return False


def _boolean_text(base_tris: list, boxes: list, op: str) -> np.ndarray:
    """base - text  (op='sub')  or  base + text  (op='add'), via manifold3d."""
    import manifold3d as m3d

    soup = np.asarray(base_tris, dtype=np.float64).reshape(-1, 3)
    uniq, inv = np.unique(np.round(soup, 6), axis=0, return_inverse=True)
    base = m3d.Manifold(m3d.Mesh(
        vert_properties=uniq.astype(np.float32),
        tri_verts=inv.reshape(-1, 3).astype(np.uint32)))
    if base.is_empty():
        raise SystemExit("base mesh is not a valid solid for embossing")

    eps = 0.02   # grow each box in-plane so stacked pixels interpenetrate rather
                 # than share exact faces (which would leave CSG seams)
    cubes = []
    for corner, A, B, C in boxes:
        ua, va = A / np.linalg.norm(A), B / np.linalg.norm(B)
        corner = corner - eps * ua - eps * va
        A, B = A + 2 * eps * ua, B + 2 * eps * va
        pts = np.array([corner + i * A + j * B + k * C
                        for k in (0, 1) for j in (0, 1) for i in (0, 1)])
        lo, hi = pts.min(0), pts.max(0)
        cubes.append(m3d.Manifold.cube((hi - lo).tolist()).translate(lo.tolist()))

    print(f"  {'cutting' if op == 'sub' else 'fusing'} "
          f"{len(cubes)} pixel boxes into {base.num_tri()} tris ...")
    text = m3d.Manifold.batch_boolean(cubes, m3d.OpType.Add)
    res = (base - text) if op == "sub" else (base + text)
    if res.is_empty():
        raise SystemExit("emboss boolean produced an empty mesh")

    mesh = res.to_mesh()
    verts = np.asarray(mesh.vert_properties)[:, :3].astype(np.float32)
    tv = np.asarray(mesh.tri_verts)
    mf, mt = np.asarray(mesh.merge_from_vert), np.asarray(mesh.merge_to_vert)
    if mf.size:                       # weld the verts manifold3d marks coincident
        remap = np.arange(len(verts))
        remap[mf] = mt
        tv = remap[tv]
    out = verts[tv]
    n = np.cross(out[:, 1] - out[:, 0], out[:, 2] - out[:, 0])
    return out[np.linalg.norm(n, axis=1) > 1e-7]   # drop CSG sliver triangles


def _fmt_lat(lat: float, d: int) -> str:
    return f"{abs(lat):.{d}f}°{'N' if lat >= 0 else 'S'}"


def _fmt_lon(lon: float, d: int) -> str:
    return f"{abs(lon):.{d}f}°{'E' if lon >= 0 else 'W'}"


def emboss_corner_coords(tris: list, info: dict, bbox, cap_mm: float,
                         depth: float, decimals: int,
                         style: str = "engraved") -> np.ndarray:
    """
    Put each edge's own coordinate on its side wall, anchored at a corner:
      south wall = min latitude   (anchored at the SW corner)
      north wall = max latitude   (anchored at the NE corner)
      west  wall = min longitude  (anchored at the SW corner)
      east  wall = max longitude  (anchored at the NE corner)
    Text reads left-to-right when viewed square-on from outside that wall.

    style='engraved' cuts the text in (boolean); 'raised' stands it proud.
    Returns the final (n, 3, 3) triangle array.
    """
    min_lat, min_lon, max_lat, max_lon = bbox
    W, Hd = info["model_w"], info["model_h"]
    margin = max(2.0, min(W, Hd) * 0.03)
    baseline = 0.6

    # per wall: text, long-axis length L, edge-height profile (ordered 0->L),
    # outward normal, and a builder that maps a fitted pixel size to placement.
    walls = [
        ("south", _fmt_lat(min_lat, decimals), W, info["south_z"], (0, -1, 0),
         # anchor SW (x=0); reads +X away from the corner
         lambda tw: ((margin, 0.0, baseline), (1, 0, 0), margin, margin + tw)),
        ("north", _fmt_lat(max_lat, decimals), W, info["north_z"], (0, 1, 0),
         # anchor NE (x=W); reads -X away from the corner
         lambda tw: ((W - margin, Hd, baseline), (-1, 0, 0), W - margin - tw, W - margin)),
        ("west", _fmt_lon(min_lon, decimals), Hd, info["west_z"], (-1, 0, 0),
         # anchor SW (y=0); viewer from the west reads -Y, so text ends at the corner
         lambda tw: ((0.0, margin + tw, baseline), (0, -1, 0), margin, margin + tw)),
        ("east", _fmt_lon(max_lon, decimals), Hd, info["east_z"], (1, 0, 0),
         # anchor NE (y=H); viewer from the east reads +Y, so text ends at the corner
         lambda tw: ((W, Hd - margin - tw, baseline), (0, 1, 0), Hd - margin - tw, Hd - margin)),
    ]

    raised = style == "raised"
    out_mm, in_mm = (depth, 0.3) if raised else (0.4, depth)

    boxes: list = []
    for name, text, L, edge_z, na, build in walls:
        n_px = len(text) * _GLYPH_ADV - 1
        px = min(cap_mm / _GLYPH_H, (L - 2 * margin) / n_px)   # fit wall width
        tw = n_px * px
        _, _, s0, s1 = build(tw)

        # tallest text the wall can physically back over the span it covers
        lo = max(0, int(min(s0, s1) / L * (len(edge_z) - 1)))
        hi = min(len(edge_z),
                 int(math.ceil(max(s0, s1) / L * (len(edge_z) - 1))) + 1)
        wall_h = float(np.min(edge_z[lo:hi]))
        px = min(px, (wall_h - 0.4 - baseline) / _GLYPH_H)   # keep text below wall top
        cap_now = px * _GLYPH_H

        if px <= 0 or cap_now < 1.6:
            print(f"  ! {name} wall only {wall_h:.1f} mm tall here - no room "
                  f"for text; raise --base or lower --emboss-height. Skipped.")
            continue

        origin, uax, _, _ = build(n_px * px)
        boxes += _text_pixel_boxes(text, origin, uax, (0, 0, 1), na, px,
                                   out_mm=out_mm, in_mm=in_mm)
        note = "  (auto-shrunk to fit)" if cap_now < cap_mm - 0.1 else ""
        print(f"  {'raised' if raised else 'engraved'} {name} wall: \"{text}\"  "
              f"{cap_now:.1f} mm tall, {depth} mm {'proud' if raised else 'deep'}{note}")

    if not boxes:
        return np.asarray(tris, dtype=np.float32)

    if _have_manifold():
        return _boolean_text(tris, boxes, "add" if raised else "sub")
    if not raised:
        raise SystemExit(
            "engraved text needs manifold3d:\n"
            "  ./.venv/bin/pip install -r requirements.txt\n"
            "or pass --emboss-style raised (no extra deps).")
    print("  note: manifold3d not installed - raised text added as separate "
          "shells (any slicer unions them; install manifold3d for one clean solid)")
    for c, A, B, C in boxes:
        tris.extend(_box_tris(c, A, B, C))
    return np.asarray(tris, dtype=np.float32)


def data_attribution(source: str, ign_res: int | None = None) -> tuple[str, str]:
    """(full credit line for the sidecar, ASCII short form for the 80-byte STL header)."""
    if source == "ign":
        return ("Elevation data © Instituto Geográfico Nacional de "
                "España (CNIG) — https://www.ign.es — CC-BY 4.0 "
                "compatible, attribution required",
                "topo2stl | Elevation (c) IGN Espana / CNIG")
    if source == "tessadem":
        return ("Elevation data via the TessaDEM API — https://tessadem.com",
                "topo2stl | Elevation via TessaDEM")
    return ("", "topo2stl")


def write_binary_stl(tris: np.ndarray, path: Path, header: str = "topo2stl"):
    n = len(tris)
    v0 = tris[:, 0, :]
    v1 = tris[:, 1, :]
    v2 = tris[:, 2, :]
    normals = np.cross(v1 - v0, v2 - v0)
    lens = np.linalg.norm(normals, axis=1, keepdims=True)
    lens[lens == 0] = 1.0
    normals = normals / lens

    with open(path, "wb") as f:
        h = header.encode("ascii", "replace")[:80]
        if h[:5].lower() == b"solid":        # never let a binary STL start with "solid"
            h = (b"topo2stl " + h)[:80]
        f.write(h + b"\x00" * (80 - len(h)))
        f.write(struct.pack("<I", n))
        buf = bytearray()
        for i in range(n):
            buf += struct.pack("<3f", *normals[i])
            buf += struct.pack("<3f", *tris[i, 0])
            buf += struct.pack("<3f", *tris[i, 1])
            buf += struct.pack("<3f", *tris[i, 2])
            buf += b"\x00\x00"
        f.write(buf)
    print(f"Wrote {path}  ({path.stat().st_size/1e6:.1f} MB, {n} triangles)")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Generate a 3D-printable STL from public elevation data.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--bbox", help="min_lat,min_lon,max_lat,max_lon")
    g.add_argument("--center", help="lat,lon  (use with --width-km)")

    p.add_argument("--source", choices=["ign", "tessadem"], default="ign",
                   help="elevation source: 'ign' = free Spain-only IGN MDT WCS; "
                        "'tessadem' = global, paid API key")
    p.add_argument("--ign-res", type=int, choices=[5, 25], default=25,
                   help="IGN mesh resolution in metres (5 m is heavier/slower)")

    p.add_argument("--width-km", type=float, help="E-W extent for --center mode")
    p.add_argument("--height-km", type=float,
                   help="N-S extent for --center mode (default: = width-km)")

    p.add_argument("--grid", default="300",
                   help="samples as N or ROWSxCOLS. IGN resamples server-side in "
                        "one request; TessaDEM auto-tiles past its 16384-cell / "
                        "5deg limit")
    p.add_argument("--model-width", type=float, default=150.0,
                   help="printed model width in mm (E-W)")
    p.add_argument("--z-exaggeration", type=float, default=1.5,
                   help="vertical scale multiplier vs true scale")
    p.add_argument("--smooth", type=float, default=0.0,
                   help="Gaussian blur the elevation grid, sigma in cells "
                        "(~0.8-1.5 removes server-resampling weave on large "
                        "areas; applied after download, cache is untouched)")
    p.add_argument("--base", type=float, default=3.0,
                   help="solid base thickness in mm below the lowest terrain point")
    p.add_argument("--sea-level", action="store_true",
                   help="measure height from 0 m elevation instead of the "
                        "lowest point in the tile (keeps bathymetry/altitude honest)")
    p.add_argument("--unit", choices=["meters", "feet"], default="meters")

    p.add_argument("--emboss-coords", action="store_true",
                   help="mark each side wall with its edge coordinate (latitude "
                        "on N/S walls, longitude on E/W), anchored at the SW/NE "
                        "corners - for identifying the print")
    p.add_argument("--emboss-style", choices=["engraved", "raised"],
                   default="engraved",
                   help="'engraved' cuts the text in (needs manifold3d); "
                        "'raised' stands it proud")
    p.add_argument("--emboss-height", type=float, default=4.0,
                   help="text cap height in mm")
    p.add_argument("--emboss-depth", type=float, default=0.6,
                   help="engraving / relief depth in mm")
    p.add_argument("--emboss-decimals", type=int, default=4,
                   help="decimal places in the embossed coordinates")

    p.add_argument("-o", "--output", help="output STL path")
    p.add_argument("--key", default=os.environ.get("TESSADEM_API_KEY"),
                   help="TessaDEM API key (or env TESSADEM_API_KEY)")
    p.add_argument("--no-cache", action="store_true",
                   help="ignore any cached grid and re-download")
    p.add_argument("--view", action="store_true",
                   help="open/refresh the live STL viewer (viewer.py) after writing")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv=None):
    a = parse_args(argv)

    if a.bbox:
        parts = [float(x) for x in a.bbox.split(",")]
        if len(parts) != 4:
            raise SystemExit("--bbox needs 4 comma-separated numbers")
        min_lat, min_lon, max_lat, max_lon = parts
    else:
        lat, lon = [float(x) for x in a.center.split(",")]
        if not a.width_km:
            raise SystemExit("--center requires --width-km")
        min_lat, min_lon, max_lat, max_lon = bbox_from_center(
            lat, lon, a.width_km, a.height_km)

    if max_lat <= min_lat or max_lon <= min_lon:
        raise SystemExit("bounding box is empty or inverted")
    if a.source == "tessadem" and not (-80 <= min_lat and max_lat <= 84):
        raise SystemExit("TessaDEM latitude coverage is -80 to 84")
    bbox = (min_lat, min_lon, max_lat, max_lon)

    if "x" in a.grid.lower():
        rows, cols = (int(v) for v in a.grid.lower().split("x"))
    else:
        # square-ish grid honouring the area's real aspect ratio
        n = int(a.grid)
        mean_lat = (min_lat + max_lat) / 2
        w = (max_lon - min_lon) * m_per_deg_lon(mean_lat)
        h = (max_lat - min_lat) * EARTH_M_PER_DEG_LAT
        if w >= h:
            cols, rows = n, max(2, round(n * h / w))
        else:
            rows, cols = n, max(2, round(n * w / h))
    if rows < 2 or cols < 2:
        raise SystemExit("grid must be at least 2x2")

    print(f"BBox: {min_lat:.5f},{min_lon:.5f} -> {max_lat:.5f},{max_lon:.5f}")
    print(f"Grid: {rows} rows x {cols} cols")

    grid_m = cached_grid(a.source, a.key, bbox, rows, cols, a.unit,
                         a.ign_res, a.verbose, a.no_cache)
    rows, cols = grid_m.shape          # source may have clamped the grid
    if a.source == "tessadem" and a.unit == "feet":
        grid_m = grid_m * 0.3048      # mesh math is metric (IGN is always metres)

    if a.smooth > 0:                   # post-download; does not touch the cache
        grid_m = gaussian_blur(grid_m, a.smooth)
        print(f"Smoothed grid (sigma {a.smooth} cells)")

    tris, info = build_mesh(grid_m, bbox, a.model_width, a.z_exaggeration,
                            a.base, a.sea_level)

    if a.emboss_coords:
        tris = emboss_corner_coords(tris, info, bbox, a.emboss_height,
                                    a.emboss_depth, a.emboss_decimals,
                                    a.emboss_style)
    else:
        tris = np.asarray(tris, dtype=np.float32)

    out = Path(a.output) if a.output else Path(
        f"topo_{min_lat:.3f}_{min_lon:.3f}_{rows}x{cols}.stl")
    credit, stl_header = data_attribution(a.source, a.ign_res)
    write_binary_stl(tris, out, stl_header)
    if credit:
        print(f"  attribution (required if published/sold): {credit}")

    # sidecar the viewer reads to label the SW / NE corners with real coords
    meta = {
        "bbox": [min_lat, min_lon, max_lat, max_lon],
        "source": a.source + (f" MDT{a.ign_res:02d}" if a.source == "ign" else ""),
        "z_exaggeration": a.z_exaggeration,
        "grid": [rows, cols],
        "elev_m_per_mm": round(info["m_per_mm"], 4),   # for the viewer's contour lines
        "base_mm": a.base,
        "smooth": a.smooth,
        "generator": "topo2stl",
        "attribution": credit,
    }
    meta_path = out.with_name(out.stem + ".topo.json")
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False),
                         encoding="utf-8")
    print(f"Wrote {meta_path}")

    if a.view:
        launch_viewer(out)


def launch_viewer(stl_path: Path, port: int = 8731):
    """Start viewer.py, or retarget an already-running one at `port`."""
    import socket
    import subprocess
    stl_path = stl_path.resolve()
    with socket.socket() as s:
        s.settimeout(0.3)
        already_running = s.connect_ex(("127.0.0.1", port)) == 0
    if already_running:
        try:
            import urllib.request
            urllib.request.urlopen(
                urllib.request.Request(f"http://127.0.0.1:{port}/target",
                                       data=str(stl_path).encode()),
                timeout=2).read()
            print(f"viewer at http://localhost:{port}/ now showing {stl_path.name}")
        except Exception:
            print(f"viewer already live at http://localhost:{port}/ "
                  f"(couldn't retarget it; restart it on {stl_path.name})")
        return
    viewer = Path(__file__).parent / "viewer.py"
    if not viewer.exists():
        print("viewer.py not found; skipping --view")
        return
    subprocess.Popen([sys.executable, str(viewer), str(stl_path)],
                     start_new_session=True)
    print(f"viewer starting at http://localhost:{port}/  "
          f"(leave it open; future runs auto-reload {stl_path.name})")


if __name__ == "__main__":
    sys.exit(main())
