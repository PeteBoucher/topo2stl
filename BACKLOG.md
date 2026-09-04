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
  - [ ] B polish: a few non-manifold edges survive the union at exact precision
    (slicer-repairable, but chase it); optional gabled roofs from `roof:shape`;
    OSM buildings currently seat on terrain+tree overlay (minor, buildings on
    canopy cells float ~tree height).
  - [ ] Catastro footprints (100% Spain coverage + floor counts, needs UTM).
  - [ ] `--landmark "lat,lon,file.stl"` — drop a custom monument mesh.
  - [ ] Water mask — recess rivers/coast as a channel (Micropolitan style).
- **viewer.py lifecycle** — `--view` starts the server detached
  (`start_new_session=True`) so it outlives the shell and a later
  `viewer.py X.stl` just hits "port busy". Add:
  - `/quit` POST endpoint → server calls `srv.shutdown()` on itself.
  - `viewer.py --kill` — POST `/quit`, exit; "nothing running" if free.
  - `viewer.py X.stl --replace` — kill any running viewer, then start fresh
    on X.stl; make `topo2stl --view` use this so re-runs always land on the
    right file.
  - maybe `viewer.py --status` — print the running viewer's current STL.
- Optional hillshade / contour bake into the printed surface itself.
- `--preset` shelf (e.g. `wall-tile`, `desk`, `keyring`) bundling size + base +
  exaggeration.
- Web front-end (pick an area on a map -> download STL / order a print).

## Known issues

- **Surface corrugation / blockiness from WCS resampling.** IGN's WCS resamples
  its native grid server-side; downsampling leaves a fine weave, upsampling
  leaves native-post blocks. Handled: alternating triangulation diagonal,
  smooth normals in the viewer, `--smooth auto` (sigma scales to the
  up/downsample ratio), README guidance to raise `--grid`. Largely resolved;
  revisit only if specific cases still look bad.
