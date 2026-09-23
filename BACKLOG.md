# Backlog

Loose list of things to do, roughly in priority order.

## Ideas

- **Buildings and monuments** — scoped in
  [docs/buildings-scope.md](docs/buildings-scope.md).
  - [x] Strategy A: `--building-source raster` — IGN LiDAR heights added to the
    terrain grid. `--smooth auto` on by default to de-block the terrain.
  - [x] Strategy B: `--building-source osm` (now the default) — OSM footprints
    extruded to prisms via manifold3d, unioned onto the terrain. Crisp walls,
    per-building heights from `building:levels`.
  - [x] B: multipolygon relations (courtyard buildings — the Mezquita) with
    holes; monument height fallback (14 m for churches/mosques); bogus
    `height=0.1` tags ignored.
  - [x] `--trees` — IGN vegetation DSM overlaid as canopy mounds (Spain).
  - [x] OSM footprints clipped to the base plate (no overhang); canopy /
    raster buildings masked over OSM water (no tree-line on the Puente Romano).
  - [x] `--building-roofs lidar` — clip OSM prisms to the IGN LiDAR surface for
    the real roofscape, floored at the tag height so small/open structures the
    5 m LiDAR misses (watermills, gates) stay visible. Water masking now only
    clears the tree canopy over open water, not building roofs.
  - [ ] B polish: flat-roof union still leaves a few non-manifold edges at
    exact precision (slicer-repairable); OSM buildings seat on terrain+tree
    overlay (buildings on canopy cells float ~tree height).
  - [ ] Catastro footprints (100% Spain coverage + floor counts, needs UTM).
  - [ ] `--landmark "lat,lon,file.stl"` — drop a custom monument mesh.
  - [ ] Water mask — recess rivers/coast as a channel (Micropolitan style).
- **viewer Area picker follow-ups** — done: pan/zoom bbox + Regenerate in the
  viewer; **Save as** to build the pending area to a new named file instead of
  overwriting the current one. Next: drag the blue rect directly; a 2D map
  thumbnail; remember panel state (pend/camera) across a page reload.
- **viewer.py lifecycle** — done: `/quit` POST endpoint (`srv.shutdown()` from
  a fresh thread, since calling it from the handler thread that received the
  request would otherwise race the response); `viewer.py --kill` (POST
  `/quit`, "nothing running" if the port's free); `viewer.py X.stl --replace`
  (kill whatever's running first, then start fresh); `/status` (pid/started
  timestamp/current stl - backs `--kill`/`--replace` and a manual check).
  `launch_viewer()` (used by both `topo2stl.py --view` and
  `tileset_preview.py --view`) now also compares a running server's `started`
  time against viewer.py/topo2stl.py/tileset_preview.py's on-disk mtimes and
  auto-replaces (rather than just retargets) a server that predates the
  current code - otherwise a viewer left running from before a code change
  just keeps serving its old behaviour forever, which looks exactly like
  it's ignoring new input (this is what caused "regenerated tileset always
  the same, ignores the new area box" - a stale pre-tiling-support viewer.py
  was still bound to the port).
- **Multi-tile printing** — done: `--tile ROWSxCOLS` splits a model too big
  for one bed into a grid of tiles cut from one continuous elevation field,
  keyed with peg/socket seams molded into the base (`--bed-size`,
  `--tile-peg-diameter/-length/-spacing`, `--tile-clearance`), plus a
  `.tileset.json` manifest and a small row-col ID engraved low on each tile's
  south wall. `tileset_preview.py` merges the tiles into one non-manifold
  preview STL at their assembled positions so `viewer.py` can show the whole
  map (`python tileset_preview.py NAME.tileset.json --view`), with a
  toggleable red "Seams" overlay tracing each cut along the real terrain
  edge (viewer.html's `rebuildSeams`). Regenerate/Save-as work from that
  merged preview too - pan/zoom/save reruns the whole `--tile` command then
  re-merges automatically and switches the viewer to the fresh preview;
  opening a bare tile file disables Regenerate with a pointer to the merged
  preview instead. Next:
  - fold the merge into `viewer.py` directly (open a `.tileset.json` as the
    target, assemble in memory - no `.preview.stl` written to disk).
  - drop the "one bare peg centred" fallback for a very short seam in favour
    of a slightly larger minimum tile size check.
  - a building footprint straddling a seam is clipped independently by each
    tile it touches (like the base plate's edge) rather than split with a
    matching cut on both sides - fine for most footprints, can leave a
    visible mismatch for a building that spans most of a tile.
- Optional hillshade / contour bake into the printed surface itself.
- `--preset` shelf (e.g. `wall-tile`, `desk`, `keyring`) bundling size + base +
  exaggeration.
- Web front-end (pick an area on a map -> download STL / order a print).

## Known issues

- **Sharp peaks string in the print.** Alpine summits print as sub-mm islands.
  `--peak-smooth 0..1` (morphological opening blended in) rounds the knife tips;
  broad terrain untouched. Real fix is dry filament + slicer combing/retraction.
- **Surface corrugation / blockiness from WCS resampling.** IGN's WCS resamples
  its native grid server-side; downsampling leaves a fine weave, upsampling
  leaves native-post blocks. Handled: alternating triangulation diagonal,
  smooth normals in the viewer, `--smooth auto` (sigma scales to the
  up/downsample ratio), README guidance to raise `--grid`. Largely resolved;
  revisit only if specific cases still look bad.
