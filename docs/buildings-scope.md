# Scope: buildings & monuments on city-centre maps

Status: **Strategy A shipped** (`--buildings`). Strategy B / landmarks / water
mask still open. Parent: BACKLOG.md "Buildings and monuments".

## Goal

Add 3D building massing to small-area models so a city-centre print reads as a
place, not just topography — e.g. the Mezquita quarter of Córdoba, the Albaicín
and Alhambra hill in Granada.

## When it applies

Buildings only make sense at **city scale**. Rule of thumb: a building must come
out ≥ ~0.6 mm tall to print.

```
building_mm = height_m * model_width_mm / (bbox_width_m)      (no z-exaggeration)
```

For a 200 mm model, a 10 m building is:

| Area width | m per model-mm | 10 m building |
| --- | --- | --- |
| 800 m | 4.0 | 2.5 mm  ✅ |
| 2 km | 10 | 1.0 mm  ✅ |
| 5 km | 25 | 0.4 mm  ⚠️ marginal |
| 40 km (Granada tile) | 200 | 0.05 mm ❌ |

→ the feature should **refuse or warn** when `bbox_width` / `model_width` implies
sub-0.5 mm buildings, and suggest a tighter `--bbox`.

## Data sources

### Building heights — IGN MDSn (Spain, raster)  ✅ verified working

`https://wcs-mds.idee.es/mds`, coverage **`mdsn_e025`** — the *normalized* digital
surface model, **building class**, 2.5 m grid: height-above-ground of buildings,
0 elsewhere. Same WCS mechanics as the MDT service we already use, with one
extra param:

```
?service=WCS&version=2.0.1&request=GetCoverage&coverageId=mdsn_e025
 &subsettingCrs=http://www.opengis.net/def/crs/EPSG/0/4326
 &subset=Lat(a,b)&subset=Long(c,d)
 &format=application/asc&SCALESIZE=Long(cols),Lat(rows)
```

Test over central Córdoba (0.01° box): 45 % of cells are buildings, mean 9.6 m,
max 28.6 m (Cathedral tower). Looks correct. Data © IGN/CNIG, same licence as
the MDT — attribution already handled by `data_attribution()`.

### Building footprints — vector (for crisp edges / styling)

| Source | Coverage | Heights | Access | Notes |
| --- | --- | --- | --- | --- |
| **OpenStreetMap** (Overpass API) | global, excellent in Spanish cities | `height` / `building:levels` tags, patchy | `way["building"](bbox); out geom;` → JSON, EPSG:4326 | ODbL, attribution |
| **Overture Maps** buildings | global | ML height estimates, good coverage | GeoParquet by bbox (DuckDB) | CDLA; heavier tooling |
| **Catastro INSPIRE** buildings | all Spain | `numberOfFloorsAboveGround`, estimated `heightAboveGround` | per-municipality GML ZIP, ATOM service, **EPSG:25830 (UTM)** | very complete; needs GML parse + UTM→lon/lat |

## Implementation strategies

### Strategy A — raster add (MVP, Spain-only, small)

1. Fetch `mdsn_e025` for the bbox, resampled to the **same grid** as the terrain
   (reuse the IGN download path, parameterised by base URL + coverage +
   `subsettingCrs`).
2. `surface = terrain_z + building_height` (building height **not**
   z-exaggerated, or a separate `--building-exaggeration`).
3. Mesh as now. Buildings come out as 2.5 m-pixel blocky extrusions following
   roof shape — reads like a physical SimCity model. No projection math, no
   polygon triangulation, no Overpass.

Cost: ~½ session. Mostly a second `download_grid_*` + a grid add + a scale
guard + `--buildings` flag + cache-key/sidecar plumbing.

Limitations: soft/stepped edges at 2.5 m; can't filter or style; merges
adjacent buildings into blocks; Spain only.

### Strategy B — vector footprints (crisper, later)

1. Fetch footprints (OSM first — already lon/lat).
2. Height per building: `height` tag → `levels × 3 m` → `--building-default`.
   Optionally sample the MDSn raster over the footprint for a measured height.
3. Project footprints into model-mm space (same local equirectangular transform
   the terrain uses).
4. Simplify footprints (Douglas–Peucker, ~0.3 m) to keep triangle count sane.
5. Extrude each: `manifold3d.CrossSection(polygon).extrude(h)` — we already
   depend on manifold3d. Place the base at the terrain height under the
   footprint (min-z under footprint; let it embed slightly for a clean union).
6. `terrain + batch_union(buildings)` → one watertight solid.

Cost: ~1½–2 sessions. Risks: thousands of prisms per city centre (union speed —
manifold batch handles it but test); footprints spanning steep slopes; thin
towers below the printable minimum (enforce a min footprint / min prism width).

### Monuments

- **Phase 1**: nothing special — the Mezquita, Cathedral, Alcázar etc. are in
  OSM/Catastro/MDSn and extrude into recognisable blocks by their footprint.
- **Phase 2**: `--landmark "lat,lon,model.stl[,scale][,rotate]"` (repeatable) —
  drop a user-supplied 3D mesh (own scan, CC model from Sketchfab/Wikimedia) at
  a coordinate, seated on the terrain, unioned in. Lets you put a real Giralda
  or Alhambra on the map without solving heritage 3D acquisition.
- **Phase 3** (maybe never): a small curated landmark library keyed by name.

## Proposed CLI

```
--buildings                     add building massing (implies a scale check)
--buildings-source {mdsn,osm}   default mdsn (Spain); osm elsewhere
--building-min-height FLOAT     drop anything shorter (m, default 2)
--building-default-height FLOAT fallback when a footprint has no height (m)
--building-exaggeration FLOAT   separate from --z-exaggeration (default 1.0)
--building-simplify FLOAT       footprint simplification tolerance (m; osm only)
--landmark "lat,lon,file[,scale][,deg]"   repeatable; drop a custom mesh
```

## Decisions needed before starting

1. **A or B first?** Recommend **A** — ships value fast, Spain-focused which is
   the near-term use, and B can layer on later without rework (both end at
   "extra height on the surface" / "prisms unioned to the terrain").
2. **Exaggerate building height with the terrain, or not?** Recommend a separate
   factor defaulting to 1.0 — exaggerated terrain with true-height buildings
   looks right; exaggerated buildings look like a bar chart.
3. **Blend at the terrain join** — hard step (buildings sit on ground) vs. a
   1–2 px skirt. Hard step is fine for A.
4. Footprint source default for `osm` vs adding Catastro (better data, UTM
   transform cost).

## Reuse from the current codebase

- `download_grid_ign` / `_split_multipart_asc` / `_parse_asc` — generalise to
  any IGN WCS (base URL + coverage + optional `subsettingCrs`).
- `cached_grid` + `CACHE_VERSION` — cache the building raster / footprints too.
- `gaussian_blur` — optional light smoothing of the MDSn raster.
- `manifold3d` batch boolean — already used for `--emboss-coords`; reuse for B.
- `data_attribution` — extend the IGN string to mention MDSn if buildings used.
- sidecar `.topo.json` — record building source + params.
