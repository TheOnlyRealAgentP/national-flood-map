# National Flood Map -- build plan

A public, read-only web map of the return-period flood extents produced by the
FIMserv project (`D:\FIMserv_Project`). People look at the maps; nothing is
computed for them. Export is limited to prebuilt files and a picture of the view.

Written 2026-11-13. Decisions from the owner the same day:

| Decision | Choice |
|---|---|
| Where the site lives | new repo `national-flood-map` in the owner's personal GitHub, served by GitHub Pages |
| Where the big tiles live | a public Hugging Face dataset (free, CDN, range requests + CORS) |
| 500-year | shown where it exists (171 LP-III HUC8s), labeled as partial coverage |
| GitHub access | GitHub CLI installed with winget; owner runs `gh auth login` once |
| Purpose | public viewer; export of prebuilt files and the current view only |
| Metadata | everything we have, per HUC8 and per reach, minus anything private |

## What exists (measured 2026-11-13)

- 2,145 finished HUC8s: 1,974 CIROH (2, 5, 10, 25, 50, 100 yr), 171 LP-III (+500 yr);
  one HUC8 (16020201) also has 1000 yr. 0 partial. Of 2,326 US HUC8s, the 181 not
  mapped have no NOAA HAND (145 Alaska, 25 Pacific, 11 open water / bays).
- `webapp/tile_cache/flood/<huc8>.tif`: one web COG per HUC8, EPSG:3857 at 9.554 m
  (= web zoom 14), aligned to the tile grid (`targetAlignedPixels`), OR-pooled
  overviews. Pixel = which periods flood it: 1,670 "rank" encoded (all with 0
  nesting breaks, so lossless), 475 "bits" encoded. `index.json` holds periods,
  wet km2 per period and bounds for every one. 4.0 GB total.
- Per-reach discharge: `USA/<State>/<huc>_<Name>/discharge/RP<t>YR_<huc>.csv`
  (feature_id, discharge m3/s).
- Reach geometry + attributes (order, length, slope, USGS gage, channel shape):
  `~/Box/FIMserv_Terrain/<basin>/flood_<huc>/<huc>/nwm_subset_streams.gpkg`
  (Box Drive, read on demand). No stream names in NWM.
- Names: in CONUS the NWM feature_id is the NHDPlusV2 COMID, so GNIS names join
  from NHDPlusV2. Alaska / Hawaii / PR / Pacific reaches stay unnamed (searchable by ID).
- Catchments: `USA/<State>/<huc>_<Name>/raw/nwm_catchments_proj_subset.gpkg`.
- Job records: `webapp/jobs_store.json` (mode, periods, start/finish times).

## Invariants

1. **The FIMserv project, its conda env and Box are read-only.** Build scripts read
   from them and write only under `D:\national-flood-map\`. The one exception is a
   dated entry appended to `D:\FIMserv_Project\HISTORY.md` at the end.
2. **Nothing private is published**: no hostnames, user names, e-mail, local paths,
   log text or job IDs. A privacy check scans every file before upload.
3. Separate build env `nfm-build` (conda-forge). The `fimserve` env and its
   numpy pin are never touched.
4. Merges at HUC8 seams are bitwise OR (same rule as FIMserv hard rule 14).
5. Code MIT-licensed. Data CC BY 4.0 (owner may change). The site states it is a
   research product, not a FEMA regulatory map.
6. Big files (> 50 MB) go to Hugging Face only. The GitHub repo stays under 200 MB.

## Architecture

```
GitHub Pages (repo national-flood-map)          Hugging Face dataset (national-flood-map-data)
  index.html, app.js, style.css (no build)        flood.pmtiles      raster, z0-14, 8-bit PNG bitmask
  data/huc8.pmtiles   HUC8 polygons + status     streams.pmtiles    NWM reaches + attributes, z3-14
  data/catalog.json   per-HUC8 metadata          cogs/<huc8>.tif    per-HUC8 bitmask COG (download)
  data/stats.json     national/state/HUC2/order  reaches/<huc8>.csv per-reach table (download)
  data/search/*.json  stream-name index shards   metadata.json      dataset metadata + dictionary
  data/metadata.json  dataset metadata
```

- **Map**: MapLibre GL JS 5 (globe and flat projection toggle, same layers on both).
- **Basemap**: OpenFreeMap vector tiles (free, no key, OSM) under our own style,
  written for this map: muted land, blue hydrography, labels drawn above the flood
  layer. Alternates: USGS National Map imagery and shaded relief (public domain).
  A self-hosted OSM basemap was considered and rejected: a US extract is tens of GB
  to host and would not draw better than a custom style on OpenFreeMap.
- **Flood layer**: one raster PMTiles. Each pixel is a global 8-bit mask
  (bit 0 = 2 yr, 1 = 5, 2 = 10, 3 = 25, 4 = 50, 5 = 100, 6 = 500, 7 = 1000).
  A MapLibre custom protocol decodes each tile and colors it in the browser, so one
  tile set serves every view: a single period, or all periods stacked (each pixel
  colored by the most frequent period that floods it). Overviews are OR-pooled, so
  thin rivers stay continuous when zoomed out.
- **Streams**: vector PMTiles written by GDAL's PMTiles/MVT driver (in `nfm-build`;
  tippecanoe would need system packages in WSL, which we don't install). Reaches
  are split into stream-order bands, each its own layer with its own minimum zoom,
  so small streams appear only when zoomed in. Click a reach: name, ID, order, length, slope,
  gage, discharge per period, flooded area per period, HUC8.

## Site features

- Legend with per-period color swatches and toggles; single / stacked mode; opacity.
- Globe / flat toggle; basemap switcher; light/dark aware.
- HUC8 selector: type a code, name or state, or click the map. Panel: metadata,
  method, periods, flooded km2 per period (chart), reach count, stream km, gages,
  downloads.
- Stream search: name or feature ID, client-side index, fly to the river.
- Stats view: national totals, by state, by HUC2 region, by stream order, coverage
  map (mapped / no NOAA HAND / not run).
- Export: PNG of the current view with legend and attribution; per-HUC8 downloads
  (COG, reach CSV, metadata JSON); stats tables as CSV.
- About page: methods (HAND, NWM retrospective, LP-III vs CIROH, why 500 yr is
  partial), data dictionary, coverage gaps, disclaimer, citation, licenses, credits.
- Works at phone width.

## Phases and checks

0. **Setup.** Repo, LICENSE, `nfm-build` env, GitHub CLI. Owner: `gh auth login`,
   Hugging Face account + write token (`hf auth login`).
1. **Hosting spike (gate).** Upload a small test PMTiles to the HF dataset and load
   it in MapLibre from localhost. Pass = tiles draw and range requests return 206
   through HF's redirect. Fail -> stop and bring options back to the owner.
2. **Flood tiles.** `build/flood_tiles.py`: z14 tiles from every COG (LUT to global
   bits, OR at seams) into a scratch SQLite store, OR-pooled pyramid to z0,
   PNG-encode, write `flood.pmtiles`.
   Checks: per-HUC8 wet pixel count at z14 equals the COG's own count for 25
   random HUC8s; a seam tile matches the OR of its neighbors; z0-z14 visual pass.
3. **Streams.** `build/reaches.py`: reach table (Box geometry + discharge + names +
   flooded area per catchment from the COGs) -> GeoParquet -> GDAL PMTiles ->
   `streams.pmtiles`; per-HUC8 CSVs; search shards.
   Checks: reach count per HUC8 equals the discharge CSV row count; names joined
   for most CONUS reaches; 10 hand-checked reaches.
4. **Metadata and stats.** `build/catalog.py`: catalog.json, stats.json,
   metadata.json, huc8.pmtiles. Checks: totals equal `project_status.py`
   (2,145 / 1,974 / 171); privacy scan clean.
5. **Site.** Frontend per the feature list. Checks: Playwright run against a local
   server (globe/flat, every period, HUC8 select, stream search, export, phone width).
6. **Publish.** Upload data to HF, push to GitHub, enable Pages, rerun the Playwright
   checks against the live URL.
7. **Docs.** README, dated HISTORY.md entry in FIMserv, memory note.

## Progress log

- 2026-11-13: plan written; repo created at `D:\national-flood-map`.
- 2026-11-13: `nfm-build` env created (python 3.12, GDAL 3.13.3 with the PMTiles driver);
  GitHub CLI 2.104 installed with winget. Installing tippecanoe's build deps with sudo in WSL
  was refused by the permission system, so stream tiles use GDAL's MVT writer instead.
- 2026-11-13: **flood.pmtiles built**: 1,601,064 z14 tiles (55,738 HUC8-seam merges) in 4.5 min,
  pyramid to z0 (2,259,182 tiles total), PNG pack 5.00 GB. Verify: 25/25 random HUC8s lost
  no bits at seams; Mercator-corrected wet area matches the COG index (16020101: 23.2 km2).
- 2026-11-13: **finding: 36 "complete" HUC8s are dry.** All 19 Alaska, 8 Hawaii, 7 PR/USVI and
  2 Pacific HUC8s logged "CIROH covered 0/N reaches" and wrote empty discharge CSVs, then
  all-dry rasters, which count as complete on disk. Real flood coverage = 2,109 HUC8s, all in
  the lower 48. Logged in FIMserv BUGS.md as `ciroh-zero-coverage-counted-complete` (open).
  The site shows them as "Run, no discharge".
- 2026-11-13: 71 HUC8s' terrain is still a zip in Box (the 11-08 `--unzip-existing` hit
  transient Box Drive "not a zip file" reads). One reopened fine (123 members = files.json);
  reaches.py reads `nwm_subset_streams.gpkg` out of the zip without touching it.
- 2026-11-13: stream names: NHDPlusV2 VAA parquet from HydroShare resource
  6092c8a62fac45be97a09bfd0b0bf726 (2,691,339 COMIDs, MD5 matches HydroShare's checksum).
- 2026-11-13: **palette**: dataviz `validateOrdinal` on the basemap land colors. No 7-step
  (or 6-step, on light) single-hue blue ramp reaches the 0.06 adjacent-lightness gap; blue
  steps are 0.047 apart. Chose the best passing-contrast steps (light 300-700, dark 100-600,
  every step >= 2:1 vs the land color) and added a second channel: click any flooded spot for
  its exact periods; per-period toggles and single-period mode stay available.
- 2026-11-13: owner: personal GitHub (TheOnlyRealAgentP, edu account) and the same HF account. Hosting
  spike passed (302 -> us.aws.cdn.hf.co, 206 with ACAO on both hops, Content-Range exposed).
  `huggingface_hub` 2.4 dropped `upload_large_folder`; publish_hf.py uses `upload_folder` with globs.
- 2026-11-14: UI pass after a look at flood viewers and map-UI guidance (slider beats drop-down for
  flood hazard maps, MDPI 2026; plain-language period meanings; bottom sheets on phones): period
  slider + play, period descriptions, Photon place search, hillshade + optional 3D terrain (AWS
  Terrain Tiles), collapsible panel with mini legend, loading bar, share link, intro card, 40 px
  touch targets. Zoomed out (< z6.5) the OR-pooled raster read as a solid sheet, so watersheds are
  shaded by % of area in the 100-yr floodplain (breaks 1/2.5/5/10/20%, median 4.1%) and the raster
  fades in from z5.5 to z7. `idle` never fires while a source fails, so loading/export/ID search
  wait on rendered tiles with a timeout instead.
- 2026-11-14: reaches 2,109/2,109 HUC8s (2,150,677 unique reaches, 99.7% NHDPlusV2 match, 42.7% named);
  10230006's organized raw/ files are 0 bytes -> fall back to Box; zips that read as "not a zip" are
  read through once to make Box download them, then retried. Catalog: 2,109 mapped, 36 dry, 10 not
  run, 244 no HAND. Two coastal HUC8s model no flooding (no GeoTIFF offered for them).
- 2026-11-14: published: flood.pmtiles, cogs/ (2,107, 4.09 GB), reaches/ (2,109 CSVs), search/ ID shards
  (42 MB, moved off Pages), metadata.json, dataset card on HF; site commits 09bd11b, fa17fc0, cbd3521.
  Live UI check 12/15 (the 3 failures are the stream tiles, still building).
