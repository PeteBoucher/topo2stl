# Agent code map

Purpose-built index for coding agents working in this repo. Read this instead
of the whole source when you just need to find where something lives — it
gives file:line ranges and one-line purposes so you can jump straight to the
relevant 20-50 lines instead of loading all of `topo2stl.py` (1543 lines).
For CLI usage / user-facing behaviour, read [README.md](README.md) instead —
this file is about *where code is*, not how to run it.

Line numbers are accurate as of commit `e32a92d`. If a grep for a symbol
below doesn't match, the file has moved on — re-grep rather than trusting
the stale number.

## Repo layout

| File | Lines | Role |
| --- | --- | --- |
| [topo2stl.py](topo2stl.py) | ~1543 | Everything: CLI, download, cache, mesh build, buildings, emboss, STL write. Single file by design — see "Why one file" below. |
| [viewer.py](viewer.py) | ~283 | stdlib-only HTTP server: serves `viewer.html`, the STL, the `.topo.json` sidecar, and a `/regen` endpoint that shells out to `topo2stl.py`. |
| [viewer.html](viewer.html) | ~660 | The viewer's page: three.js render loop, HUD, Area pan/zoom panel, regen/save-as UI. All JS is inline in this one file. |
| [docs/buildings-scope.md](docs/buildings-scope.md) | — | Design notes for the buildings feature (why OSM vs raster, etc). Background reading, not code. |
| [BACKLOG.md](BACKLOG.md) | — | Known issues + unimplemented ideas. Check before "fixing" something that's a known, accepted limitation (e.g. sharp-peak stringing). |
| `cache/` | — | Downloaded elevation/building/vegetation grids as `.npy`, keyed by a hash of request params. Gitignored. Safe to delete; everything re-downloads. |

## topo2stl.py — section map

Section dividers in the file (`grep -n "^# ---" topo2stl.py` to relocate):

| Lines | Section | What's there |
| --- | --- | --- |
| 1-61 | Module header / constants | Docstring pipeline summary, `IGN_*` URLs, `CACHE_VERSION`, `CACHE_DIR`. |
| 65-77 | Geometry helpers | `m_per_deg_lon`, `bbox_from_center`. |
| 80-206 | Elevation download (TessaDEM) | `_parse_area_results`, `fetch_area`, `download_grid` (tiles requests to stay under `API_MAX_CELLS`/`API_MAX_DEGREES`). |
| 209-323 | IGN MDT via WCS | `_split_multipart_asc`, `_parse_asc`, `_fetch_ign_wcs` (generic WCS fetch, reused for terrain/buildings/veg), `download_grid_ign`, `download_buildings_ign` (2 methods: `surface` = mds−mdt−veg subtraction, `classified` = building-class DSM directly), `download_veg_ign`. |
| 326-549 | OSM buildings (Overpass) | `_stitch_rings` (join way fragments into closed rings for multipolygon relations), `fetch_osm` → `(buildings, water)`, `_building_height_m` (tag → metres logic), `_poly_area`, `_simplify`, `_clip_rect` (Sutherland-Hodgman clip to base plate), `_rasterize_polys`. |
| 552-618 | Cache dispatch | `cached_grid`, `cached_buildings`, `cached_veg` — hash the request params (bbox rounded to 6dp + rows/cols/method/`CACHE_VERSION`) to a `.npy` filename under `cache/`. |
| 621-666 | Grid smoothing | `gaussian_blur`, `_morph` (erode/dilate), `clip_peaks` (the `--peak-smooth` morphological-opening blend). |
| 669-767 | Mesh construction | `build_mesh` — the core: grid → watertight solid (top surface + 4 walls + bottom), `quad` closure inside it, `_box_tris`. |
| 770-1195 | Embossed corner coordinates | `_FONT5x7` (pixel font data), `_text_pixel_boxes`, `_have_manifold`/`_soup_to_manifold`/`_manifold_to_soup` (manifold3d bridge), `add_osm_buildings` (extrude + union footprints, optional LiDAR roof clipping), `_boolean_text`, `_fmt_lat`/`_fmt_lon`, `emboss_corner_coords`, `data_attribution` (credit-line text), `write_binary_stl`. |
| 1198-1306 | CLI | `parse_args` — every `--flag` definition. |
| 1309-1510 | `main()` | The pipeline glue — see "main() pipeline order" below. |
| 1512-1539 | `launch_viewer` | Starts or retargets `viewer.py` for `--view`. |

### Why one file
`topo2stl.py` is intentionally not split into modules — it's a single-purpose
CLI script, not a library. Don't propose a package refactor unless asked.

## Data conventions (load-bearing — get these wrong and meshes come out inverted/mirrored)

- **`bbox`** is always `(min_lat, min_lon, max_lat, max_lon)`, a plain 4-tuple.
- **Elevation grids** (`grid_m`, and any building/veg overlay) are `(rows, cols)`
  numpy arrays with **row 0 = north (max lat), col 0 = west (min lon)** — i.e.
  raster order, not Cartesian. Same convention used for `overlay_m`.
- **Longitude is corrected by `cos(latitude)`** (`m_per_deg_lon`) everywhere
  distances are computed, so printed proportions stay true at the tile's
  latitude. Don't use a flat `111_320 m/deg` for both axes.
- **`tris`** is an `(n, 3, 3)` float32 array — n triangles × 3 vertices × xyz,
  in millimetres, model sitting on `z = 0`. This is the STL soup passed
  between mesh-building stages (`build_mesh` → `add_osm_buildings` →
  `emboss_corner_coords` → `write_binary_stl`).
- **`info` dict** (returned by `build_mesh`) carries `m_per_mm` and corner
  coordinates forward to the buildings/emboss stages — grep `info\[` if you
  need its exact keys.
- Buildings/trees are **never** touched by `--z-exaggeration` — only by
  their own `--building-exaggeration`/`--tree-exaggeration`. That's a
  deliberate product decision (README "Notes" under Buildings), not a bug.

## main() pipeline order (topo2stl.py:1309-1510)

1. Resolve `bbox` from `--bbox` or `--center`+`--width-km` (1312-1328).
2. Resolve `rows, cols` from `--grid` (1330-1343).
3. Force `--ign-res 5` if buildings/trees/lidar-roofs need it (1348-1357).
4. `cached_grid()` → `grid_m` (1362).
5. Fetch OSM footprints/water and/or raster buildings/veg into `overlay_m` /
   `osm_footprints` (1379-1418). Tree canopy over OSM water is zeroed here.
6. `gaussian_blur` (`--smooth`) then `clip_peaks` (`--peak-smooth`) on
   `grid_m` — **terrain only, not overlays**, and applied *after* the cache
   read so cached grids stay raw (1420-1437).
7. `build_mesh()` → `tris, info` (1439).
8. If OSM buildings: optionally build a second LiDAR-roof mesh and pass it
   into `add_osm_buildings()` (1442-1456).
9. If `--emboss-coords`: `emboss_corner_coords()` (1458-1461).
10. Write STL + `.topo.json` sidecar (bbox, settings, **and `sys.argv`** so
    the viewer can re-run for a new area) + `.CREDITS.txt` (1465-1506).
11. `--view` → `launch_viewer()` (1508).

## Cache directory (`cache/`)

Filename pattern: `{kind}_{method?}_v{CACHE_VERSION}_{rows}x{cols}_{hash16}.npy`
- `grid_ign_v1_...` / `grid_tessadem_v1_...` — elevation (`cached_grid`, topo2stl.py:553).
- `buildings_surface_v1_...` / `buildings_classified_v1_...` — `cached_buildings` (585).
- `veg_v1_...` — `cached_veg` (602).
- The hash is `sha1` of a JSON blob of the exact request params (bbox rounded
  to 6 decimals, rows/cols, method, `CACHE_VERSION`) — same params always hit
  the same file. `--no-cache` bypasses reads (still writes fresh).
- **`CACHE_VERSION` (topo2stl.py:58)**: bump this if you change what a cache
  file *contains* (fetch/parse/resample logic) — old files with a stale
  version are silently ignored, not migrated. Don't bump it for unrelated
  changes; it invalidates everyone's cache.
- Safe to `rm -rf cache/*` any time; it's a pure cache, gitignored.

## Adding or changing a CLI flag — 3 places, always

1. `parse_args()` (topo2stl.py:1199-1306) — the `argparse` definition.
2. `main()` (1309-1510) — actually consume `a.<flag>`, and if it's worth
   regenerating from (viewer's Regenerate button), it's already covered
   automatically since the sidecar stores raw `sys.argv`.
3. `README.md` "Key options" table (README.md:73-98) — user-facing docs.

If the flag should survive a viewer-triggered regenerate with a *new area*
(different `--bbox`/`--center`), also check `viewer.py`'s `_AREA_OPTS`
(viewer.py:33) — only area/output flags are stripped and replaced; everything
else round-trips via the stored `argv` untouched.

## viewer.py — endpoint map (Handler class, viewer.py:156-249)

| Method | Path | Does |
| --- | --- | --- |
| GET | `/` | Serves `viewer.html` (viewer.py:145-151, reads the file fresh each time). |
| GET | `/name` | Current STL filename. |
| GET | `/version` | `"<mtime_ns>|<name>"` — polled by the page to detect file changes/switches. |
| GET | `/model.stl` | The STL bytes. |
| GET | `/meta` | The `.topo.json` sidecar (bbox, m_per_mm, etc), or `{}`. |
| GET | `/regen/available` | Whether Regenerate/Save-as can run (needs `topo2stl.py` beside `viewer.py`, a sidecar with `argv`, and numpy) — `_regen_available()`, viewer.py:70. |
| GET | `/regen/status` | Poll target for an in-flight regen: `running/done/error/log/saved_as`. |
| GET | `/exists?name=` | Whether a "Save as" filename already exists (for the confirm-overwrite prompt). |
| POST | `/target` | Retarget the running server at a different STL path (used by `launch_viewer()`'s "already running" path and by a successful Save-as). |
| POST | `/regen` | Body `{bbox, filename?}`. No `filename` = overwrite current file; with `filename` = "Save as" a new one (`_target_from_name`, viewer.py:59). Rebuilds the argv via `_area_args`/`_strip_area_args` (viewer.py:84-110) and runs `topo2stl.py` in a background thread (`_run_regen`, viewer.py:113). |

Global mutable state: `Handler.stl_path` (class attribute, the file currently
served) and `_regen` dict (module-level, regen progress) guarded by
`_regen_lock`. Single-viewer-process assumption throughout — this is a
personal dev tool, not a multi-user server.

## viewer.html — JS map

All inline `<script>`, one file. Key DOM ids: `#c` (canvas), `#bar` (button
row: Fit/Relief/Wireframe/Spin/Coords/Area), `#area` (pan/zoom panel),
`#regen` (regenerate progress modal), `#cSW`/`#cNE` (corner coord labels).

| Function | Line | Purpose |
| --- | --- | --- |
| `niceInterval` | 215 | Pick a round contour-line interval from the model's elevation range. |
| `applyReliefUniforms` / `updateReliefRange` | 220, 232 | Hypsometric tint + contour shader uniforms for the Relief toggle. |
| `frame` / `setModel` | 259, 273 | Fit camera to geometry / swap in a newly loaded STL, keeping camera unless `refit`. |
| `poll` | 315 | Polls `/version` on an interval; reloads the mesh on change (this is how `--view` auto-refresh works). |
| `resize` | 353 | Canvas/renderer resize on window resize. |
| `applyMeta` / `placeCorner` / `placeCorners` | 370, 390, 405 | Read `.topo.json` sidecar → position the SW/NE lat/lon HUD labels in screen space. |
| `refreshArea` | 428 | Redraw the blue pan/zoom rectangle for the Area panel from the pending bbox. |
| `panPend` / `zoomPend` | 452, 458 | Mutate the pending bbox (Shift = fine, Alt = coarse step, per README). |
| `checkRegenAvail` | 487 | Calls `/regen/available`, enables/disables Regenerate + Save-as. |
| `startRegen` | 509 | POSTs `/regen` with `{bbox, filename?}` — shared by Regenerate and Save-as buttons, `filename` present only for Save-as. |
| `regenFail` | 524 | Show an error in the regen modal. |
| `pollRegen` | 529 | Polls `/regen/status` while a regen runs, streams `log` into `#regenlog`. |
| `updateCompass` / `tick` | 547, 555 | North-needle rotation from camera azimuth; main render-loop tick. |

## Common tasks → where to look

| Task | Start at |
| --- | --- |
| Change how buildings are extruded/unioned | `add_osm_buildings`, topo2stl.py:895 |
| Change building height-from-tags logic | `_building_height_m`, topo2stl.py:452 |
| Change terrain smoothing behaviour | `gaussian_blur`/`clip_peaks`, topo2stl.py:622-666, and the `--smooth auto` sigma formula in `main()` around topo2stl.py:1422-1429 |
| Change what's embossed / font | `_FONT5x7` + `_text_pixel_boxes` + `emboss_corner_coords`, topo2stl.py:771-1150 |
| Add a new elevation source | Mirror the `download_grid_ign`/`download_grid` pair + add a `cached_grid` branch (topo2stl.py:553) + a `--source` choice (parse_args) |
| Change STL header / credits text | `data_attribution`, topo2stl.py:1150 |
| Change the sidecar `.topo.json` schema | `meta = {...}` dict in `main()`, topo2stl.py:1475-1491 — remember `viewer.py` reads `meta["argv"]` and `meta["bbox"]` specifically |
| Change viewer Regenerate/Save-as behaviour | `_run_regen`/`_area_args`/`_strip_area_args`, viewer.py:84-141, and `startRegen`/`pollRegen` in viewer.html:509-546 |
| Change the Area pan/zoom panel UI | viewer.html `#area` block (~104-119) + `refreshArea`/`panPend`/`zoomPend` (viewer.html:428-486) |
| Known accepted limitations (don't "fix" without asking) | [BACKLOG.md](BACKLOG.md) "Known issues" — sharp-peak stringing, minor non-manifold edges on flat-roof unions |

## Gotchas

- `manifold3d` is optional (only needed for `--emboss-coords` and OSM
  building union); `_have_manifold()` (topo2stl.py:847) gates it. Code paths
  without it fall back to unioned/separate shells — check both branches when
  touching boolean-op code.
- `--buildings`/`--trees`/`--building-roofs lidar` silently **force
  `--ign-res 5`** and require `--source ign` (raster methods and lidar roofs
  are Spain-only) — see topo2stl.py:1348-1357. A "why is my grid 5m when I
  asked for defaults" bug report likely starts here.
- `--smooth` and `--peak-smooth` run on the **decoded grid after the cache
  read**, never on what's stored in `cache/` — the cache is always raw
  server data regardless of smoothing flags used when it was written.
- The viewer's Regenerate/Save-as literally re-invokes `topo2stl.py` as a
  subprocess with a reconstructed argv — it does not call any Python
  function directly. Changing `main()`'s argument parsing can silently break
  the viewer's regen path.
