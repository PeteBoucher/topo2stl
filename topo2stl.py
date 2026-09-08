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
IGN_MDS_URL = "https://wcs-mds.idee.es/mds"
IGN_MDS_SURFACE = "mds05"          # full digital surface model, 5 m
IGN_MDS_VEG = "mdsn_v025"          # normalised DSM, vegetation class, 2.5 m
IGN_MDS_BUILDINGS = "mdsn_e025"    # normalised DSM, building class, 2.5 m
EPSG_4326_URI = "http://www.opengis.net/def/crs/EPSG/0/4326"

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


def _fetch_ign_wcs(url, coverage, bbox, rows, cols, verbose,
                   subsetting_crs=None) -> np.ndarray:
    """One IGN INSPIRE WCS GetCoverage, resampled server-side to rows x cols.
    Returns a float grid, row 0 = north, NaN where the coverage has no data."""
    if requests is None:
        raise RuntimeError("The 'requests' package is required. pip install -r requirements.txt")
    min_lat, min_lon, max_lat, max_lon = bbox
    params = {
        "service": "WCS", "version": "2.0.1", "request": "GetCoverage",
        "coverageId": coverage, "format": "application/asc",
        "subset": [f"Lat({min_lat:.8f},{max_lat:.8f})",
                   f"Long({min_lon:.8f},{max_lon:.8f})"],
        "SCALESIZE": f"Long({cols}),Lat({rows})",
    }
    if subsetting_crs:
        params["subsettingCrs"] = subsetting_crs
    if verbose:
        print(f"  GET {url} {params}")
    r = requests.get(url, params=params, timeout=180)
    if r.status_code != 200 or b"ExceptionReport" in r.content[:2000]:
        raise RuntimeError(f"IGN WCS error (HTTP {r.status_code}): {r.text[:500]}")
    grid = _parse_asc(_split_multipart_asc(r.content))
    if grid.shape != (rows, cols):
        print(f"  note: server returned {grid.shape}, requested {(rows, cols)}")
    return grid


def download_grid_ign(bbox, rows, cols, ign_res, verbose) -> np.ndarray:
    coverage = IGN_COVERAGE[ign_res]
    print(f"Downloading {rows}x{cols} grid from IGN MDT{ign_res:02d} ({coverage}) ...")
    grid = _fetch_ign_wcs(IGN_WCS_URL, coverage, bbox, rows, cols, verbose)
    if np.isnan(grid).any():
        n = int(np.isnan(grid).sum())
        print(f"  warning: {n} nodata cells (outside MDT coverage?); filled with min")
        grid = np.where(np.isnan(grid), np.nanmin(grid), grid)
    return grid


def download_buildings_ign(bbox, rows, cols, method, verbose) -> np.ndarray:
    """
    Building height above ground (m), 0 where there is nothing.

    method 'surface' (default): full surface model minus bald-earth minus
        classified vegetation = every built structure, complete even where the
        LiDAR building-classifier missed a roof (e.g. the Mezquita). Keeps a
        little tree noise in leafy areas.
    method 'classified': IGN's normalised DSM building class directly — excludes
        trees but drops some monument / large low roofs.
    """
    def mds(cov):
        return _fetch_ign_wcs(IGN_MDS_URL, cov, bbox, rows, cols, verbose,
                              subsetting_crs=EPSG_4326_URI)

    if method == "classified":
        print(f"Downloading {rows}x{cols} building grid from IGN MDSn "
              f"({IGN_MDS_BUILDINGS}) ...")
        g = mds(IGN_MDS_BUILDINGS)
        return np.where(np.isnan(g) | (g < 0), 0.0, g)

    print(f"Downloading {rows}x{cols} building grid from IGN "
          f"(mds05 - mdt05 - vegetation) ...")
    surf = mds(IGN_MDS_SURFACE)
    terr = _fetch_ign_wcs(IGN_WCS_URL, IGN_COVERAGE[5], bbox, rows, cols, verbose)
    veg = mds(IGN_MDS_VEG)
    h = surf - terr
    h = np.where(np.isnan(h), 0.0, h)
    veg = np.where(np.isnan(veg) | (veg < 0), 0.0, veg)
    h = np.clip(h - veg, 0.0, None)
    return gaussian_blur(h, 0.6)          # de-stair the upsampled edges a touch


def download_veg_ign(bbox, rows, cols, verbose) -> np.ndarray:
    """Vegetation (tree canopy) height above ground, m, from IGN's normalised
    DSM vegetation class (mdsn_v025, 2.5 m). 0 where there is no vegetation."""
    print(f"Downloading {rows}x{cols} vegetation grid from IGN MDSn "
          f"({IGN_MDS_VEG}) ...")
    g = _fetch_ign_wcs(IGN_MDS_URL, IGN_MDS_VEG, bbox, rows, cols, verbose,
                       subsetting_crs=EPSG_4326_URI)
    g = np.where(np.isnan(g) | (g < 0), 0.0, g)
    return gaussian_blur(g, 0.7)


# --------------------------------------------------------------------------- #
# OpenStreetMap building footprints (Overpass)
# --------------------------------------------------------------------------- #
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)


def _stitch_rings(ways: list) -> list:
    """Chain a relation's outer/inner member ways into closed rings."""
    rings, pending = [], []
    for w in ways:
        if len(w) >= 4 and w[0] == w[-1]:
            rings.append(w)
        elif len(w) >= 2:
            pending.append(list(w))
    while pending:
        chain = pending.pop()
        moved = True
        while moved and chain[0] != chain[-1]:
            moved = False
            for i, w in enumerate(pending):
                if chain[-1] == w[0]:
                    chain += w[1:]
                elif chain[-1] == w[-1]:
                    chain += w[-2::-1]
                elif chain[0] == w[-1]:
                    chain = w[:-1] + chain
                elif chain[0] == w[0]:
                    chain = w[:0:-1] + chain
                else:
                    continue
                pending.pop(i)
                moved = True
                break
        if len(chain) >= 4 and chain[0] == chain[-1]:
            rings.append(chain)
    return rings


def fetch_osm(bbox, verbose, no_cache) -> tuple[list, list]:
    """(buildings, water). buildings: {'outer','holes','tags'} for every OSM
    building (ways + multipolygon relations). water: list of (lon,lat) rings for
    lakes/rivers. Cached as raw Overpass JSON."""
    if requests is None:
        raise RuntimeError("The 'requests' package is required.")
    CACHE_DIR.mkdir(exist_ok=True)
    min_lat, min_lon, max_lat, max_lon = bbox
    sig = f"{CACHE_VERSION}|mpw|{min_lat:.6f},{min_lon:.6f},{max_lat:.6f},{max_lon:.6f}"
    path = CACHE_DIR / f"osm_v{CACHE_VERSION}_{hashlib.sha1(sig.encode()).hexdigest()[:16]}.json"

    if path.exists() and not no_cache:
        print(f"Using cached OSM data: {path.name}")
        data = json.loads(path.read_text())
    else:
        b = f"{min_lat},{min_lon},{max_lat},{max_lon}"
        q = (f"[out:json][timeout:90];("
             f'way["building"]({b});relation["building"]["type"="multipolygon"]({b});'
             f'way["natural"="water"]({b});relation["natural"="water"]({b});'
             f'way["waterway"="riverbank"]({b});'
             f");out geom;")
        headers = {"User-Agent": "topo2stl/1.0 (+https://github.com/PeteBoucher/topo2stl)"}
        data = None
        for ep in OVERPASS_ENDPOINTS:
            try:
                print(f"Querying Overpass ({ep.split('/')[2]}) ...")
                r = requests.post(ep, data={"data": q}, headers=headers, timeout=120)
                if r.status_code == 200 and r.content[:1] == b"{":
                    data = r.json()
                    break
                print(f"  {ep.split('/')[2]}: HTTP {r.status_code}")
            except requests.RequestException as e:
                print(f"  {ep.split('/')[2]}: {e}")
        if data is None:
            raise SystemExit("Overpass unavailable - try again later, or use "
                             "--building-source raster")
        path.write_text(json.dumps(data))
        print(f"Cached OSM data -> {path.name}")

    def rel_rings(el):
        outers = [[(p["lon"], p["lat"]) for p in m["geometry"]]
                  for m in el.get("members", [])
                  if m.get("role") in ("outer", "") and m.get("geometry")]
        inners = [[(p["lon"], p["lat"]) for p in m["geometry"]]
                  for m in el.get("members", [])
                  if m.get("role") == "inner" and m.get("geometry")
                  and len(m["geometry"]) >= 4]
        return _stitch_rings(outers), inners

    buildings, water, rels = [], [], 0
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        is_water = tags.get("natural") == "water" or tags.get("waterway") == "riverbank"
        if el.get("type") == "way" and "geometry" in el:
            pts = [(p["lon"], p["lat"]) for p in el["geometry"]]
            if len(pts) < 4:
                continue
            if is_water:
                water.append(pts)
            elif "building" in tags:
                buildings.append({"outer": pts, "holes": [], "tags": tags})
        elif el.get("type") == "relation" and tags.get("type") == "multipolygon":
            outers, inners = rel_rings(el)
            for ring in outers:
                if is_water:
                    water.append(ring)
                elif "building" in tags:
                    buildings.append({"outer": ring, "holes": inners, "tags": tags})
                    rels += 1
    if verbose:
        print(f"  {len(buildings)} footprints ({rels} from relations), "
              f"{len(water)} water polygons")
    return buildings, water


_TALL_BUILDING_TYPES = {"church", "cathedral", "basilica", "chapel", "mosque",
                        "temple", "synagogue", "monastery", "watermill",
                        "tower", "castle"}


def _building_height_m(tags: dict, level_h: float, default_h: float) -> float:
    for key in ("height", "building:height"):
        if key in tags:
            try:
                h = float(str(tags[key]).split()[0].replace(",", "."))
                if h >= 2.0:                    # ignore bogus placeholder heights
                    return h
            except ValueError:
                pass
    if "building:levels" in tags:
        try:
            lv = float(str(tags["building:levels"]).split(";")[0].replace(",", "."))
            rl = float(str(tags.get("roof:levels", 0) or 0).split(";")[0])
            if lv >= 1:
                return max(2.0, (lv + 0.5 * rl) * level_h)
        except ValueError:
            pass
    if (tags.get("building") in _TALL_BUILDING_TYPES
            or tags.get("historic") in _TALL_BUILDING_TYPES
            or tags.get("man_made") in _TALL_BUILDING_TYPES):
        return max(default_h, 14.0)             # monuments with no usable height
    return default_h


def _poly_area(pts) -> float:
    s = 0.0
    for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
        s += x1 * y2 - x2 * y1
    return 0.5 * s


def _simplify(pts, tol):
    """Douglas-Peucker on an open point list."""
    if len(pts) < 3:
        return pts
    (ax, ay), (bx, by) = pts[0], pts[-1]
    dx, dy = bx - ax, by - ay
    seg = math.hypot(dx, dy) or 1e-9
    dmax, idx = 0.0, 0
    for i in range(1, len(pts) - 1):
        px, py = pts[i]
        d = abs(dy * px - dx * py + bx * ay - by * ax) / seg
        if d > dmax:
            dmax, idx = d, i
    if dmax <= tol:
        return [pts[0], pts[-1]]
    return _simplify(pts[:idx + 1], tol)[:-1] + _simplify(pts[idx:], tol)


def _clip_rect(poly, x0, y0, x1, y1):
    """Sutherland-Hodgman clip of a polygon to an axis-aligned rectangle."""
    def clip(pts, keep, cut):
        out = []
        for i in range(len(pts)):
            a, b = pts[i - 1], pts[i]
            ka, kb = keep(a), keep(b)
            if kb:
                if not ka:
                    out.append(cut(a, b))
                out.append(b)
            elif ka:
                out.append(cut(a, b))
        return out

    p = list(poly)
    p = clip(p, lambda q: q[0] >= x0,
             lambda a, b: (x0, a[1] + (b[1] - a[1]) * (x0 - a[0]) / (b[0] - a[0])))
    p = clip(p, lambda q: q[0] <= x1,
             lambda a, b: (x1, a[1] + (b[1] - a[1]) * (x1 - a[0]) / (b[0] - a[0]))) if p else p
    p = clip(p, lambda q: q[1] >= y0,
             lambda a, b: (a[0] + (b[0] - a[0]) * (y0 - a[1]) / (b[1] - a[1]), y0)) if p else p
    p = clip(p, lambda q: q[1] <= y1,
             lambda a, b: (a[0] + (b[0] - a[0]) * (y1 - a[1]) / (b[1] - a[1]), y1)) if p else p
    return p


def _rasterize_polys(polys, rows, cols, W, H):
    """Boolean (rows, cols) mask (row 0 = north) of cells whose centre falls
    inside any polygon. polys are model-mm (x east, y north)."""
    cx = (np.arange(cols)) / max(cols - 1, 1) * W
    cy = (1.0 - np.arange(rows) / max(rows - 1, 1)) * H       # row 0 = north
    X, Y = np.meshgrid(cx, cy)
    mask = np.zeros((rows, cols), bool)
    for poly in polys:
        p = np.asarray(poly, float)
        if len(p) < 3:
            continue
        inside = np.zeros_like(X, bool)
        xj, yj = p[-1]
        for xi, yi in p:
            cond = ((yi > Y) != (yj > Y)) & \
                   (X < (xj - xi) * (Y - yi) / (yj - yi + 1e-12) + xi)
            inside ^= cond
            xj, yj = xi, yi
        mask |= inside
    return mask


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


def cached_buildings(bbox, rows, cols, method, verbose, no_cache) -> np.ndarray:
    """Building-height grid (m) from IGN, cached like the elevation grid."""
    CACHE_DIR.mkdir(exist_ok=True)
    sig = json.dumps({"v": CACHE_VERSION, "kind": "buildings", "method": method,
                      "bbox": [round(b, 6) for b in bbox],
                      "rows": rows, "cols": cols}, sort_keys=True)
    h = hashlib.sha1(sig.encode()).hexdigest()[:16]
    path = CACHE_DIR / f"buildings_{method}_v{CACHE_VERSION}_{rows}x{cols}_{h}.npy"
    if path.exists() and not no_cache:
        print(f"Using cached building grid: {path.name}")
        return np.load(path)
    grid = download_buildings_ign(bbox, rows, cols, method, verbose)
    np.save(path, grid)
    print(f"Cached building grid -> {path.name}")
    return grid


def cached_veg(bbox, rows, cols, verbose, no_cache) -> np.ndarray:
    """Vegetation-height grid (m) from IGN, cached like the elevation grid."""
    CACHE_DIR.mkdir(exist_ok=True)
    sig = json.dumps({"v": CACHE_VERSION, "kind": "veg",
                      "bbox": [round(b, 6) for b in bbox],
                      "rows": rows, "cols": cols}, sort_keys=True)
    h = hashlib.sha1(sig.encode()).hexdigest()[:16]
    path = CACHE_DIR / f"veg_v{CACHE_VERSION}_{rows}x{cols}_{h}.npy"
    if path.exists() and not no_cache:
        print(f"Using cached vegetation grid: {path.name}")
        return np.load(path)
    grid = download_veg_ign(bbox, rows, cols, verbose)
    np.save(path, grid)
    print(f"Cached vegetation grid -> {path.name}")
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


def _morph(a, r, op):
    """Square-window grayscale erosion (op=min) or dilation (op=max)."""
    out = a
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dy or dx:
                out = op(out, np.roll(a, (dy, dx), axis=(0, 1)))
    return out


def clip_peaks(grid: np.ndarray, strength: float, r: int = 2) -> np.ndarray:
    """Grayscale morphological opening blended in by `strength` (0..1): pulls
    isolated summits and knife-edge ridges (narrower than ~2r cells) down toward
    their surroundings, so the print's top few layers aren't a scatter of tiny
    islands the nozzle strings between. Broad terrain is untouched."""
    if strength <= 0:
        return grid
    p = np.pad(grid.astype(np.float64), 2 * r, mode="edge")
    p = _morph(p, r, np.minimum)                     # erode
    p = _morph(p, r, np.maximum)                     # dilate -> opening (<= grid)
    opened = p[2 * r:-2 * r, 2 * r:-2 * r]
    return grid + strength * np.minimum(opened - grid, 0.0)


# --------------------------------------------------------------------------- #
# Mesh construction
# --------------------------------------------------------------------------- #
def build_mesh(grid_m: np.ndarray, bbox, model_width_mm: float,
               z_exaggeration: float, base_mm: float,
               z_from_sea_level: bool,
               overlay_m: np.ndarray | None = None) -> np.ndarray:
    """
    grid_m: elevations in metres, [row0=north, col0=west].
    overlay_m: optional height-above-ground (m) for raster buildings / trees,
        same shape; added on top of the terrain surface *after* z-exaggeration
        so it keeps true scale.
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
    terrain_top_mm = float(z.max())
    if overlay_m is not None:            # true-scale (not z-exaggerated) massing
        z = z + overlay_m * mm_per_m

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
          f"{terrain_top_mm-base_mm:.1f} mm  (exaggeration {z_exaggeration}x, "
          f"base {base_mm} mm)")

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
        "z_mm": z.copy(),                     # full model-Z grid (mm), row0=north
        "mm_per_m": mm_per_m,                 # model mm per real horizontal metre
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


def _soup_to_manifold(tris):
    """(n,3,3) triangle soup -> welded manifold3d.Manifold."""
    import manifold3d as m3d
    soup = np.asarray(tris, dtype=np.float64).reshape(-1, 3)
    uniq, inv = np.unique(np.round(soup, 6), axis=0, return_inverse=True)
    return m3d.Manifold(m3d.Mesh(vert_properties=uniq.astype(np.float32),
                                 tri_verts=inv.reshape(-1, 3).astype(np.uint32)))


def _manifold_to_soup(res) -> np.ndarray:
    """manifold3d result -> (n,3,3) float32 soup. Welds the vertices manifold3d
    flags as coincident (union-find, so chains resolve) and drops only
    exactly-degenerate triangles."""
    mesh = res.to_mesh()
    verts = np.asarray(mesh.vert_properties)[:, :3].astype(np.float32)
    tv = np.asarray(mesh.tri_verts).astype(np.int64)
    mf = np.asarray(mesh.merge_from_vert)
    mt = np.asarray(mesh.merge_to_vert)
    if mf.size:
        parent = np.arange(len(verts))

        def find(i):
            r = i
            while parent[r] != r:
                r = parent[r]
            while parent[i] != r:
                parent[i], i = r, parent[i]
            return r

        for a, b in zip(mf.tolist(), mt.tolist()):
            parent[find(a)] = find(b)
        tv = np.array([find(i) for i in range(len(verts))], dtype=np.int64)[tv]

    out = verts[tv]
    degen = ((out[:, 0] == out[:, 1]).all(1) |
             (out[:, 1] == out[:, 2]).all(1) |
             (out[:, 0] == out[:, 2]).all(1))
    return out[~degen]


def add_osm_buildings(base_tris: list, info: dict, bbox, footprints: list,
                      level_h: float, default_h: float, exaggeration: float,
                      min_area_m2: float, simplify_mm: float,
                      roof_surface_tris=None) -> np.ndarray:
    """Extrude each OSM footprint to a prism seated on the terrain and union
    the lot onto the terrain solid. Returns an (n,3,3) float32 soup.

    roof_surface_tris: optional closed mesh of the LiDAR surface (terrain +
        building heights). If given, the prisms are made tall and intersected
        with it, so each building takes the real roof shape instead of a flat
        top."""
    if not _have_manifold():
        raise SystemExit("--building-source osm needs manifold3d:\n"
                         "  ./.venv/bin/pip install -r requirements.txt")
    import manifold3d as m3d

    min_lat, min_lon, max_lat, max_lon = bbox
    W, H = info["model_w"], info["model_h"]
    mm_per_m = info["mm_per_m"]
    z_mm = info["z_mm"]                        # (rows, cols) model-Z, row0 = north
    rows, cols = z_mm.shape
    min_area_mm2 = min_area_m2 * mm_per_m * mm_per_m

    def to_xy(lon, lat):
        return ((lon - min_lon) / (max_lon - min_lon) * W,
                (lat - min_lat) / (max_lat - min_lat) * H)

    def terrain_z_max_under(poly):
        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        cx = np.clip(np.array([min(xs), max(xs)]) / W, 0, 1) * (cols - 1)
        cy = np.clip(np.array([min(ys), max(ys)]) / H, 0, 1) * (rows - 1)
        r0, r1 = int(rows - 1 - cy[1]), int(np.ceil(rows - 1 - cy[0])) + 1
        c0, c1 = int(cx[0]), int(np.ceil(cx[1])) + 1
        block = z_mm[max(r0, 0):r1, max(c0, 0):c1]
        return float(block.max()) if block.size else float(z_mm.max())

    GROW = 0.04   # mm: grow each footprint so wall-to-wall neighbours overlap
                  # slightly rather than share an exact face (a CSG degeneracy)

    M = 0.1   # keep footprints just inside the base plate so nothing overhangs

    def prep_ring(coords, want_ccw):
        r = [to_xy(lon, lat) for lon, lat in coords[:-1]]     # drop closing dup
        r = _clip_rect(r, M, M, W - M, H - M)                 # trim to the base
        if simplify_mm > 0 and len(r) > 4:
            r = _simplify(r, simplify_mm)
        r = [p for i, p in enumerate(r) if p != r[i - 1]]     # drop repeats
        if len(r) < 3:
            return None
        if (_poly_area(r) < 0) == want_ccw:                   # fix winding
            r = r[::-1]
        return r

    SKY = float(z_mm.max()) + 300.0
    flat_prisms, tall_prisms, skipped = [], [], 0
    for fp in footprints:
        outer = prep_ring(fp["outer"], want_ccw=True)
        if outer is None or _poly_area(outer) < min_area_mm2:
            skipped += 1
            continue
        polys = [outer]
        for hole in fp.get("holes", []):
            h = prep_ring(hole, want_ccw=False)               # holes wound CW
            if h is not None and abs(_poly_area(h)) > min_area_mm2 * 0.25:
                polys.append(h)
        h_mm = _building_height_m(fp["tags"], level_h, default_h) * mm_per_m * exaggeration
        try:
            cs = m3d.CrossSection(polys).offset(GROW, m3d.JoinType.Miter)
            # flat prism to the tag height - guarantees the building is at least
            # this tall (small / open structures the LiDAR misses still show)
            flat = cs.extrude(terrain_z_max_under(outer) + h_mm + 1.0
                              ).translate([0.0, 0.0, -1.0])
        except Exception:
            skipped += 1
            continue
        if flat.is_empty() or flat.status() != m3d.Error.NoError:
            skipped += 1
            continue
        flat_prisms.append(flat)
        if roof_surface_tris is not None:
            tall = cs.extrude(SKY + 1.0).translate([0.0, 0.0, -1.0])
            if not tall.is_empty() and tall.status() == m3d.Error.NoError:
                tall_prisms.append(tall)

    if not flat_prisms:
        print(f"  no usable footprints ({skipped} skipped)")
        return np.asarray(base_tris, dtype=np.float32)

    print(f"  extruding {len(flat_prisms)} buildings ({skipped} skipped), "
          f"union with terrain ...")
    terrain = _soup_to_manifold(base_tris)
    built = m3d.Manifold.batch_boolean(flat_prisms, m3d.OpType.Add)
    if tall_prisms:                                              # add real roofscape
        print("  clipping buildings to the LiDAR surface ...")
        roofed = m3d.Manifold.batch_boolean(tall_prisms, m3d.OpType.Add)
        roofed = roofed ^ _soup_to_manifold(roof_surface_tris)
        built = built + roofed
    res = terrain + built
    if res.is_empty():
        raise SystemExit("building union produced an empty mesh")

    # CSG on hundreds of prisms leaves a few zero-volume sliver shells; keep only
    # the components with real volume and re-join them.
    comps = [c for c in res.decompose() if c.volume() > 0.05]
    if len(comps) > 1:
        res = m3d.Manifold.batch_boolean(comps, m3d.OpType.Add)
    elif comps:
        res = comps[0]
    return _manifold_to_soup(res)


def _boolean_text(base_tris: list, boxes: list, op: str) -> np.ndarray:
    """base - text  (op='sub')  or  base + text  (op='add'), via manifold3d."""
    import manifold3d as m3d

    base = _soup_to_manifold(base_tris)
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
    return _manifold_to_soup(res)


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


def data_attribution(source: str, ign_res: int | None = None,
                     buildings: bool = False, osm: bool = False) -> tuple[str, str]:
    """(full credit line for the sidecar, ASCII short form for the 80-byte STL header)."""
    osm_full = " · Building data © OpenStreetMap contributors (ODbL)" if osm else ""
    osm_hdr = " + OSM" if osm else ""
    if source == "ign":
        what = "Elevation & building data" if (buildings and not osm) else "Elevation data"
        return (f"{what} © Instituto Geográfico Nacional de España (CNIG) "
                "— https://www.ign.es — CC-BY 4.0 compatible, attribution "
                f"required{osm_full}",
                f"topo2stl | (c) IGN Espana / CNIG{osm_hdr}")
    if source == "tessadem":
        return (f"Elevation data via the TessaDEM API — https://tessadem.com{osm_full}",
                f"topo2stl | Elevation via TessaDEM{osm_hdr}")
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

    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
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
    os.replace(tmp, path)                     # atomic - the viewer never sees a half file
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
    p.add_argument("--smooth", default="auto",
                   help="Gaussian blur the elevation grid: 'auto' (default) "
                        "picks a sigma from how far the data is up/downsampled; "
                        "a number forces sigma in cells; '0' disables. Applied "
                        "after download - the cache is untouched.")
    p.add_argument("--peak-smooth", type=float, default=0.0,
                   help="0..1 - round off isolated summits and knife-edge "
                        "ridges (a morphological opening blended in by this "
                        "amount) so sharp peaks don't string / print as tiny "
                        "islands. ~0.5 is gentle; leaves broad terrain alone.")
    p.add_argument("--base", type=float, default=3.0,
                   help="solid base thickness in mm below the lowest terrain point")
    p.add_argument("--sea-level", action="store_true",
                   help="measure height from 0 m elevation instead of the "
                        "lowest point in the tile (keeps bathymetry/altitude honest)")
    p.add_argument("--unit", choices=["meters", "feet"], default="meters")

    p.add_argument("--buildings", action="store_true",
                   help="add building massing on top of the terrain - for "
                        "neighbourhood / city-block scenes. Forces 5 m "
                        "elevation data.")
    p.add_argument("--building-source",
                   choices=["osm", "raster", "raster-classified"], default="osm",
                   help="'osm' = OpenStreetMap footprints extruded to crisp "
                        "prisms (needs internet); 'raster' = IGN LiDAR surface "
                        "minus terrain minus vegetation (blocky, ~5 m, offline "
                        "once cached); 'raster-classified' = IGN building-class "
                        "DSM (no trees, misses some monument roofs)")
    p.add_argument("--building-roofs", choices=["flat", "lidar"], default="flat",
                   help="osm: 'flat' extrudes each footprint to a flat top; "
                        "'lidar' clips the prisms to IGN's LiDAR surface so the "
                        "real roofscape shows (domes, the Mezquita's nave). "
                        "Spain only, slower.")
    p.add_argument("--building-exaggeration", type=float, default=1.0,
                   help="vertical multiplier for buildings only (kept separate "
                        "from --z-exaggeration so massing stays true-scale)")
    p.add_argument("--building-level-height", type=float, default=3.0,
                   help="metres per floor when height comes from building:levels "
                        "(osm)")
    p.add_argument("--building-default-height", type=float, default=9.0,
                   help="metres for an osm footprint with no height/levels tag")
    p.add_argument("--building-min-area", type=float, default=10.0,
                   help="drop osm footprints smaller than this many m^2")
    p.add_argument("--building-simplify", type=float, default=0.4,
                   help="osm footprint simplification tolerance, model mm")
    p.add_argument("--building-min-height", type=float, default=2.0,
                   help="raster only: zero out building cells below this many "
                        "metres (drops walls/sheds/noise)")
    p.add_argument("--trees", action="store_true",
                   help="add tree canopy from IGN's vegetation-class DSM "
                        "(mdsn_v025, Spain) - parks, riverbanks, tree-lined "
                        "streets. Forces 5 m elevation data.")
    p.add_argument("--tree-exaggeration", type=float, default=1.0,
                   help="vertical multiplier for trees only")
    p.add_argument("--tree-min-height", type=float, default=2.0,
                   help="drop vegetation cells below this many metres")

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

    mean_lat = (min_lat + max_lat) / 2
    real_w = (max_lon - min_lon) * m_per_deg_lon(mean_lat)

    lidar_roofs = (a.buildings and a.building_source == "osm"
                   and a.building_roofs == "lidar")
    needs_ign = (a.trees or lidar_roofs
                 or (a.buildings and a.building_source != "osm"))
    if needs_ign and a.source != "ign":
        raise SystemExit("--trees, --building-roofs lidar and --building-source "
                         "raster* need IGN data (--source ign)")
    if (a.buildings or a.trees) and a.source == "ign" and a.ign_res != 5:
        a.ign_res = 5                  # sit on crisp terrain
        print("Using 5 m elevation data (MDT05)")

    print(f"BBox: {min_lat:.5f},{min_lon:.5f} -> {max_lat:.5f},{max_lon:.5f}")
    print(f"Grid: {rows} rows x {cols} cols")

    grid_m = cached_grid(a.source, a.key, bbox, rows, cols, a.unit,
                         a.ign_res, a.verbose, a.no_cache)
    rows, cols = grid_m.shape          # source may have clamped the grid
    if a.source == "tessadem" and a.unit == "feet":
        grid_m = grid_m * 0.3048      # mesh math is metric (IGN is always metres)

    mm_per_m = a.model_width / real_w
    model_h_mm = (max_lat - min_lat) * EARTH_M_PER_DEG_LAT * mm_per_m
    overlay_m = None                   # raster buildings + trees: added in build_mesh
    osm_footprints = None              # osm buildings: unioned after build_mesh
    osm_water = []

    def add_overlay(layer, exag):
        nonlocal overlay_m
        layer = layer * exag
        overlay_m = layer if overlay_m is None else overlay_m + layer

    if (a.buildings and a.building_source == "osm") or a.trees:
        b, osm_water = fetch_osm(bbox, a.verbose, a.no_cache)
        if a.buildings and a.building_source == "osm":
            osm_footprints = b
            print(f"Buildings (osm): {len(osm_footprints)} footprints")
    if a.buildings and a.building_source != "osm":
        method = {"raster": "surface",
                  "raster-classified": "classified"}[a.building_source]
        b = cached_buildings(bbox, rows, cols, method, a.verbose, a.no_cache)
        b = np.where(b < a.building_min_height, 0.0, b)
        add_overlay(b, a.building_exaggeration)
        tallest = float(b.max()) * mm_per_m * a.building_exaggeration
        print(f"Buildings ({a.building_source}): {int((b > 0).sum())} cells, "
              f"tallest {b.max():.0f} m -> {tallest:.1f} mm on the model")
        if tallest < 0.6:
            print("  ! buildings print < 0.6 mm tall - tighter --bbox / bigger --model-width")

    if a.trees:
        v = cached_veg(bbox, rows, cols, a.verbose, a.no_cache)
        v = np.where(v < a.tree_min_height, 0.0, v)
        add_overlay(v, a.tree_exaggeration)
        print(f"Trees: {int((v > 0).sum())} canopy cells, tallest {v.max():.0f} m")

    def to_mm(ring):
        return [((lon - min_lon) / (max_lon - min_lon) * a.model_width,
                 (lat - min_lat) / (max_lat - min_lat) * model_h_mm)
                for lon, lat in ring]

    # clear the tree canopy over open water - the LiDAR misclassifies the
    # Puente Romano, boats and weirs as vegetation. Building footprints (the
    # watermills in the river) are left alone.
    if overlay_m is not None and osm_water:
        w = _rasterize_polys([to_mm(r) for r in osm_water],
                             rows, cols, a.model_width, model_h_mm)
        if osm_footprints:
            w &= ~_rasterize_polys([to_mm(f["outer"]) for f in osm_footprints],
                                   rows, cols, a.model_width, model_h_mm)
        if w.any():
            overlay_m = np.where(w, 0.0, overlay_m)
            print(f"  cleared canopy over {int(w.sum())} open-water cells")

    # smooth the terrain (not the buildings) - post-download, cache untouched
    native_m = {5: 5.0, 25: 25.0}.get(a.ign_res, 25.0) if a.source == "ign" else 30.0
    if a.smooth == "auto":
        # blur roughly to the native post spacing: heavier when the server
        # upsampled a lot, a light floor otherwise (server-resampling weave)
        sigma = min(4.0, max(0.8, 0.8 * native_m / (real_w / cols)))
        smooth_label = f"{sigma:.1f} (auto)"
    else:
        sigma = float(a.smooth)
        smooth_label = f"{sigma:.1f}"
    if sigma > 0:
        grid_m = gaussian_blur(grid_m, sigma)
        print(f"Smoothed terrain (sigma {smooth_label} cells)")
    if a.peak_smooth > 0:
        before = float(grid_m.max())
        grid_m = clip_peaks(grid_m, min(a.peak_smooth, 1.0))
        print(f"Rounded peaks (strength {a.peak_smooth}): summit dropped "
              f"{before - grid_m.max():.1f} m")

    tris, info = build_mesh(grid_m, bbox, a.model_width, a.z_exaggeration,
                            a.base, a.sea_level, overlay_m=overlay_m)

    if osm_footprints is not None:
        roof_tris = None
        if lidar_roofs:
            bh = cached_buildings(bbox, rows, cols, "surface", a.verbose, a.no_cache)
            bh = np.where(bh < 1.0, 0.0, bh)
            print("Building LiDAR roof surface ...")
            roof_tris, _ = build_mesh(grid_m, bbox, a.model_width,
                                      a.z_exaggeration, a.base, a.sea_level,
                                      overlay_m=bh)
        tris = add_osm_buildings(
            tris, info, bbox, osm_footprints,
            a.building_level_height, a.building_default_height,
            a.building_exaggeration, a.building_min_area, a.building_simplify,
            roof_surface_tris=roof_tris)

    if a.emboss_coords:
        tris = emboss_corner_coords(tris, info, bbox, a.emboss_height,
                                    a.emboss_depth, a.emboss_decimals,
                                    a.emboss_style)
    else:
        tris = np.asarray(tris, dtype=np.float32)

    out = Path(a.output) if a.output else Path(
        f"topo_{min_lat:.3f}_{min_lon:.3f}_{rows}x{cols}.stl")
    credit, stl_header = data_attribution(
        a.source, a.ign_res, (a.buildings or a.trees),
        osm=(a.buildings and a.building_source == "osm"))
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
        "smooth": round(sigma, 2),
        "peak_smooth": a.peak_smooth or None,
        "buildings": (a.building_source if a.buildings else None),
        "building_exaggeration": (a.building_exaggeration if a.buildings else None),
        "trees": bool(a.trees),
        "generator": "topo2stl",
        "attribution": credit,
        "argv": list(sys.argv[1:]),          # lets viewer.py re-run for a new bbox
        "model_width": a.model_width,
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
