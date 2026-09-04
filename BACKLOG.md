# Backlog

Loose list of things to do, roughly in priority order.

## Ideas

- **Buildings and monuments** — scoped in
  [docs/buildings-scope.md](docs/buildings-scope.md).
  - [x] Strategy A: `--buildings` — IGN MDSn building-height raster added to
    the terrain surface (Spain, neighbourhood scale).
  - [ ] Strategy B: OSM / Catastro vector footprints for crisp edges.
  - [ ] `--landmark "lat,lon,file.stl"` — drop a custom monument mesh.
  - [ ] Water mask — recess rivers/coast as a channel (Micropolitan style).
- Optional hillshade / contour bake into the printed surface itself.
- `--preset` shelf (e.g. `wall-tile`, `desk`, `keyring`) bundling size + base +
  exaggeration.
- Web front-end (pick an area on a map -> download STL / order a print).

## Known issues

- **Surface corrugation on large-scale models.** IGN's WCS resamples its native
  grid server-side when `SCALESIZE` asks for far fewer samples than the source
  has, leaving a fine weave in the data; Z-exaggeration makes it obvious.
  Handled: alternating triangulation diagonal, smooth normals in the viewer,
  `--smooth SIGMA` (Gaussian on the grid, sigma ~1 clears it), README guidance
  to raise `--grid`. Possible next step: auto-pick a default `--smooth` from the
  download vs. native-resolution ratio.
