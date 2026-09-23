# Agent code map

Purpose-built index for coding agents working in this repo. Read this instead
of the whole source when you just need to find where something lives — it
gives file:line ranges and one-line purposes so you can jump straight to the
relevant 20-50 lines instead of loading all of `topo2stl.py` (~1911 lines).
For CLI usage / user-facing behaviour, read [README.md](README.md) instead —
this file is about *where code is*, not how to run it.

Line numbers are accurate as of the `--tile` (multi-tile printing) commit. If
a grep for a symbol below doesn't match, the file has moved on — re-grep
rather than trusting the stale number.

## Repo layout

| File | Lines | Role |
| --- | --- | --- |
| [topo2stl.py](topo2stl.py) | ~1911 | Everything: CLI, download, cache, mesh build, buildings, emboss, tiling, STL write. Single file by design — see "Why one file" below. |
| [viewer.py](viewer.py) | ~283 | stdlib-only HTTP server: serves `viewer.html`, the STL, the `.topo.json` sidecar, and a `/regen` endpoint that shells out to `topo2stl.py`. |
| [viewer.html](viewer.html) | ~660 | The viewer's page: three.js render loop, HUD, Area pan/zoom panel, regen/save-as UI. All JS is inline in this one file. |
| [tileset_preview.py](tileset_preview.py) | ~130 | Standalone script: merges a `--tile` run's tiles back into one non-manifold preview STL (positioned as assembled, not booleaned) so `viewer.py` can show the whole map, plus a `.topo.json` sidecar with a seam polyline per internal wall (`_wall_profile`, traced from the tile mesh's own top-edge vertices - not synthesized) that `viewer.html`'s Seams overlay draws. Imports `write_binary_stl`/`launch_viewer` from `topo2stl.py`. |
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
| 669-797 | Mesh construction | `_grid_to_z_mm` (elevation m → model-Z mm, lifted so the lowest point sits at `base_mm` — pure per-cell math, no geometry), `_mesh_from_z` (X/Y/Z arrays → watertight triangle soup: top + 4 walls + bottom), `build_mesh` (thin wrapper composing the two for the single-file path — kept so existing callers/behaviour are untouched). |
| 800-1112 | Multi-tile printing | `_split_range` (axis samples → per-tile index ranges sharing a boundary sample), `_peg_positions`, `_cyl_tris` (arbitrary-axis frustum soup), `_tile_seam_geoms` (peg/socket specs for one tile's neighbours), `_apply_tile_keys` (union pegs / cut sockets via manifold3d), `_emboss_tile_label` (row-col ID on the south wall), `_footprints_in_bbox` (cheap pre-filter), `write_tiles` (the orchestrator: slices one global Z field per `--tile` ROWSxCOLS, keys+labels+writes each tile). See "Multi-tile printing" in README for the design. |
| 1115-1540 | Embossed corner coordinates | `_FONT5x7` (pixel font data), `_text_pixel_boxes`, `_have_manifold`/`_soup_to_manifold`/`_manifold_to_soup` (manifold3d bridge — also used by tiling and OSM buildings), `add_osm_buildings` (extrude + union footprints, optional LiDAR roof clipping), `_boolean_text`, `_fmt_lat`/`_fmt_lon`, `emboss_corner_coords`, `data_attribution` (credit-line text), `write_binary_stl`. |
| 1543-1667 | CLI | `parse_args` — every `--flag` definition. |
| 1669-1878 | `main()` | The pipeline glue — see "main() pipeline order" below. |
| 1880-1907 | `launch_viewer` | Starts or retargets `viewer.py` for `--view`. |

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
- **`info` dict** (returned by `build_mesh`/`_mesh_from_z`) carries `m_per_mm`
  and corner coordinates forward to the buildings/emboss stages — grep
  `info\[` if you need its exact keys.
- Buildings/trees are **never** touched by `--z-exaggeration` — only by
  their own `--building-exaggeration`/`--tree-exaggeration`. That's a
  deliberate product decision (README "Notes" under Buildings), not a bug.

## main() pipeline order (topo2stl.py:1669-1878)

1. Resolve `bbox` from `--bbox` or `--center`+`--width-km`.
2. Resolve `rows, cols` from `--grid`.
3. Force `--ign-res 5` if buildings/trees/lidar-roofs need it.
4. `cached_grid()` → `grid_m`.
5. Fetch OSM footprints/water and/or raster buildings/veg into `overlay_m` /
   `osm_footprints`. Tree canopy over OSM water is zeroed here.
6. `gaussian_blur` (`--smooth`) then `clip_peaks` (`--peak-smooth`) on
   `grid_m` — **terrain only, not overlays**, and applied *after* the cache
   read so cached grids stay raw.
7. If `--tile`: `write_tiles()` does its own steps 8-10 per tile (slicing one
   shared `_grid_to_z_mm` field instead of calling `build_mesh` per tile) and
   `main()` returns early — see "Multi-tile printing" in README.
8. Otherwise: `build_mesh()` → `tris, info`.
9. If OSM buildings: optionally build a second LiDAR-roof mesh and pass it
   into `add_osm_buildings()`.
10. If `--emboss-coords`: `emboss_corner_coords()`.
11. Write STL + `.topo.json` sidecar (bbox, settings, **and `sys.argv`** so
    the viewer can re-run for a new area) + `.CREDITS.txt`.
12. `--view` → `launch_viewer()` (tiling prints a note instead — there's no
    single STL to preview).

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

1. `parse_args()` (topo2stl.py:1543-1667) — the `argparse` definition.
2. `main()` (1669-1878) — actually consume `a.<flag>` (and, for a `--tile-*`
   flag, `write_tiles()` too), and if it's worth regenerating from (viewer's
   Regenerate button), it's already covered automatically since the sidecar
   stores raw `sys.argv`.
3. `README.md` "Key options" table — user-facing docs.

If the flag should survive a viewer-triggered regenerate with a *new area*
(different `--bbox`/`--center`), also check `viewer.py`'s `_AREA_OPTS`
(viewer.py:33) — only area/output flags are stripped and replaced; everything
else round-trips via the stored `argv` untouched.

## viewer.py — endpoint map (Handler class, viewer.py:156-249)

| Method | Path | Does |
| --- | --- | --- |
| GET | `/` | Serves `viewer.html` (viewer.py:145-151, reads the file fresh each time). |
| GET | `/name` | Current STL filename. |
| GET | `/version` | `"<mtime_ns>\|<name>"` — polled by the page to detect file changes/switches. |
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
| `applyMeta` / `placeCorner` / `placeCorners` | 370, 390, 405 | Read `.topo.json` sidecar → position the SW/NE lat/lon HUD labels in screen space; also calls `rebuildSeams`. |
| `rebuildSeams` | ~394 | Draws `meta.seams.{vertical,horizontal}` (written by `tileset_preview.py`) as red `THREE.Line`s in the scene - the "Seams" button toggle. |
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
| Change STL header / credits text | `data_attribution`, topo2stl.py:1495 |
| Change the sidecar `.topo.json` schema | `meta = {...}` dict in `main()` — remember `viewer.py` reads `meta["argv"]` and `meta["bbox"]` specifically; `write_tiles()` builds its own (smaller) per-tile `meta` dict, keep the two in sync |
| Change the peg/socket seam keying (size, spacing, which walls carry pegs) | `_tile_seam_geoms`, topo2stl.py:863 — the convention ("pegs on south/east, sockets on north/west") is asserted in that function's neighbour checks, not documented elsewhere in code |
| Change tile splitting / bed-size check | `_split_range` + the bed check loop in `write_tiles`, topo2stl.py:809, 944 |
| Change viewer Regenerate/Save-as behaviour | `_run_regen`/`_area_args`/`_strip_area_args`, viewer.py:84-141, and `startRegen`/`pollRegen` in viewer.html:509-546 |
| Change the Area pan/zoom panel UI | viewer.html `#area` block (~104-119) + `refreshArea`/`panPend`/`zoomPend` (viewer.html:428-486) |
| Known accepted limitations (don't "fix" without asking) | [BACKLOG.md](BACKLOG.md) "Known issues" — sharp-peak stringing, minor non-manifold edges on flat-roof unions, a building footprint straddling a tile seam is clipped independently by each tile |

## Gotchas

- `manifold3d` is optional for a plain terrain print (only needed for
  `--emboss-coords` engraved text and OSM building union) but **mandatory**
  for `--tile` (the peg/socket seam keys are CSG booleans) — `write_tiles()`
  checks `_have_manifold()` up front and refuses to run without it. Elsewhere
  `_have_manifold()` (topo2stl.py:1192) gates optional fallback paths
  (unioned/separate shells) — check both branches when touching boolean-op
  code outside `write_tiles`.
- **Tiling shares one Z field on purpose.** `write_tiles()` calls
  `_grid_to_z_mm` *once* on the whole (pre-split) grid and slices the result
  per tile, rather than calling `build_mesh` per tile. Per-tile `build_mesh`
  calls would each pick their own lowest point as the `base_mm` reference,
  silently offsetting every tile's Z by a different amount — the seams would
  no longer line up even though the elevation data is identical. If you
  refactor `write_tiles`, keep the "one shared reference/lift, sliced after"
  shape.
- `--buildings`/`--trees`/`--building-roofs lidar` silently **force
  `--ign-res 5`** and require `--source ign` (raster methods and lidar roofs
  are Spain-only) — see topo2stl.py:1706-1717. A "why is my grid 5m when I
  asked for defaults" bug report likely starts here.
- `--smooth` and `--peak-smooth` run on the **decoded grid after the cache
  read**, never on what's stored in `cache/` — the cache is always raw
  server data regardless of smoothing flags used when it was written.
- The viewer's Regenerate/Save-as literally re-invokes `topo2stl.py` as a
  subprocess with a reconstructed argv — it does not call any Python
  function directly. Changing `main()`'s argument parsing can silently break
  the viewer's regen path.
