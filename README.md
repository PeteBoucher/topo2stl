# topo2stl

Turn real-world terrain into a **3D-printable STL** — a watertight solid block
with a draped top surface, vertical side walls and a flat bottom, ready to slice.

Includes a live browser preview (`viewer.py`) so you can dial in vertical
exaggeration, size and base thickness without reloading the slicer, plus an
option to **engrave the tile's own lat/lon coordinates into its side walls**.

Built with a focus on Spain — it can pull the national IGN 5 m LiDAR terrain
model for free — but it works anywhere on Earth via a global fallback source.

---

## Quick start

```bash
git clone https://github.com/petebouch/topo2stl.git
cd topo2stl
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# a relief of Córdoba, Spain (free IGN data, no API key)
python topo2stl.py --center 37.884,-4.779 --width-km 12 \
  --model-width 180 --z-exaggeration 2.5 --base 5 -o cordoba.stl --view
```

`--view` opens the preview at <http://localhost:8731/>. Leave it open and re-run
`topo2stl.py` with different settings — it reloads automatically.

Core dependencies are just `numpy` and `requests`. `manifold3d` is only needed
for `--emboss-coords`.

---

## Elevation sources

| `--source` | Coverage | Resolution | Cost |
| --- | --- | --- | --- |
| `ign` *(default)* | Spain only | 25 m or 5 m (`--ign-res`), PNOA-LiDAR | **Free, no key** |
| `tessadem` | Global (−80°…84°) | ~30 m | Paid API key |

`ign` uses the [IGN](https://www.ign.es/) INSPIRE **WCS** service
(`servicios.idee.es/wcs-inspire/mdt`) — the official national terrain model,
resampled to your grid server-side in one request. It's the same LiDAR-derived
data the Junta de Andalucía / REDIAM publishes, at finer resolution and
nationwide, so for anywhere in Andalucía `--source ign` is the best free option.

`tessadem` needs `TESSADEM_API_KEY` in the environment (or `--key`):

```bash
export TESSADEM_API_KEY=your_key
python topo2stl.py --source tessadem \
  --center 45.976,7.658 --width-km 12 -o matterhorn.stl
```

Other free options if you work outside Spain: OpenTopography (Copernicus GLO-30,
free key), opentopodata.org's public API, or the ESA Copernicus DEM directly.

---

## Usage

By centre + size, or by bounding box (`min_lat,min_lon,max_lat,max_lon`):

```bash
python topo2stl.py --center 37.879,-4.779 --width-km 8 --grid 300 -o cordoba.stl

python topo2stl.py --bbox 36.98,-3.45,37.10,-3.28 --ign-res 5 \
  --grid 500 --model-width 180 -o sierra-nevada.stl
```

### Key options

| Option | Meaning |
| --- | --- |
| `--source` | `ign` (free, Spain) or `tessadem` (global, paid). |
| `--ign-res` | `25` (default) or `5` metres. Use `5` only for small areas. |
| `--grid N` / `--grid ROWSxCOLS` | Output sampling. `N` auto-picks rows/cols from the area's real aspect ratio. ~`300` ≈ 180k triangles ≈ 9 MB STL. |
| `--model-width` | Printed width in mm (E–W). Depth and height follow to true scale. |
| `--z-exaggeration` | Vertical multiplier. `1.0` = true scale (usually too flat). `1.5`–`3` for mountains, more for lowlands. |
| `--smooth` | Gaussian-blur the elevation grid, sigma in cells. `~1` cleans the fine resampling weave that shows up on large areas / high exaggeration. Applied after download — the cache is untouched, so trying values is instant. |
| `--base` | Solid mm beneath the lowest terrain point. |
| `--sea-level` | Height measured from 0 m rather than the tile minimum. |
| `--emboss-coords` | Engrave each side wall's edge coordinate (see below). |
| `--emboss-style` | `engraved` (cut in, default — needs `manifold3d`) or `raised` (stands proud). |
| `--emboss-height` / `--emboss-depth` | Text cap height (mm, default 4) and engraving/relief depth (mm, default 0.6). |
| `--view` | Launch / refresh the live `viewer.py` preview after writing. |
| `--no-cache` | Ignore any cached elevation grid and re-download. |

Run `python topo2stl.py -h` for the full list.

---

## Live preview

`viewer.py` is a standard-library web server + a three.js page. Run it directly
against any STL, or let `--view` start it for you:

```bash
python viewer.py cordoba.stl        # http://localhost:8731/
```

Controls: orbit / zoom, **Fit**, **Wireframe**, **Spin**, a north compass, a
**Coords** toggle (labels the SW / NE base corners with their real lat/lon), and
a **Relief** toggle (hypsometric tint + elevation contour lines at a real-metre
interval). It polls the STL file and reloads the mesh on change, keeping your
camera.

Each `topo2stl.py` run writes a small `<name>.topo.json` sidecar next to the STL
(bounding box, source, vertical exaggeration, metres-per-mm). The viewer reads
it for the corner labels and contour spacing. It's harmless to delete; slicers
ignore it.

> The viewer loads three.js from a CDN, so it needs an internet connection the
> first time a browser caches it.

---

## Embossed edge coordinates

`--emboss-coords` marks each edge's own coordinate on its wall, so a printed
tile identifies itself:

| Wall | Text | Anchored at |
| --- | --- | --- |
| South | min latitude — `37.8301°N` | SW corner |
| North | max latitude — `37.9379°N` | NE corner |
| West | min longitude — `4.8473°W` | SW corner |
| East | max longitude — `4.7107°W` | NE corner |

The two labels meeting at a corner give its full lat/lon. Each reads
left-to-right viewed square-on. Labels auto-shrink to fit the wall width and its
shortest backing height; a wall with no room is skipped with a note.

`--emboss-style engraved` (default) cuts the text in with a boolean
(`manifold3d`), producing one clean watertight solid. `--emboss-style raised`
stands it proud — also one clean solid with `manifold3d` installed, otherwise
added as separate shells (slicers union them).

A taller `--base` (4–6 mm) keeps the text on the flat plinth rather than the
sloping terrain, so it prints crisply.

---

## How it works

1. Resolve a lat/lon bounding box from `--bbox` or `--center` + `--width-km`.
2. Download a grid of elevations. IGN resamples server-side in one WCS request;
   TessaDEM is auto-tiled to stay under its per-request limits. Grids are cached
   under `cache/` keyed by area + resolution, so re-meshing with different model
   settings is instant and free.
3. Build the mesh: triangulated top surface + four vertical walls + flat bottom,
   consistently wound outward. Longitude is `cos(latitude)`-corrected so
   proportions stay true.
4. Optionally engrave the coordinates via CSG.
5. Write a binary STL in millimetres, sitting on the `z = 0` plane.

```text
topo2stl.py        # the generator (CLI)
viewer.py          # live browser preview (stdlib only)
requirements.txt
```

---

## Print tips

- Print with the base flat on the bed; terrain needs no supports.
- 0.12–0.16 mm layers bring out ridgelines.
- Keep `--emboss-depth` well under your wall thickness.
- For a large area, prefer a higher `--grid` (closer to the source resolution)
  and a touch of `--smooth` over a coarse grid — the coarse grid keeps the
  server's resampling weave.

---

## Data sources & attribution

The **code** is MIT-licensed (see [LICENSE](LICENSE)). The **elevation data** is
not — it belongs to its providers and carries attribution requirements. If you
publish or **sell** anything made with this tool, credit the data source:

- **IGN (`--source ign`)** — data © [Instituto Geográfico Nacional de España](https://www.ign.es/),
  used under the CNIG free-use licence (CC-BY 4.0 compatible). Attribution is
  **mandatory** — e.g. *"Elevation data © Instituto Geográfico Nacional de
  España (CNIG)"* in the credits, on the packaging, or on the base of the model.
  See the [IGN data policy](https://www.ign.es/web/ign/portal/politica-datos).
- **TessaDEM (`--source tessadem`)** — a commercial API; follow
  [their terms of service](https://tessadem.com/) for the plan you're on.

This project is not affiliated with or endorsed by the IGN, the CNIG, the Junta
de Andalucía, or TessaDEM.

---

## License

MIT — see [LICENSE](LICENSE).
