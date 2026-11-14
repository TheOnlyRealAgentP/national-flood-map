# National Flood Map

An interactive web map of modeled flood extents for US watersheds: the 2-, 5-, 10-, 25-,
50- and 100-year floods for every mapped HUC8 in the lower 48, and the 500-year flood
where the LP-III method was used. Click any stream for its return-period discharge and
flooded area, open any watershed for its metadata and downloads, or browse national
statistics.

**Live map:** https://theonlyrealagentp.github.io/national-flood-map/
**Data (tiles, GeoTIFFs, reach tables):** https://huggingface.co/datasets/TheOnlyRealAgentP/national-flood-map-data

> Research product. Not a FEMA regulatory flood map; not for insurance, permitting,
> property or emergency decisions. Extents only, no depth.

## How it works

- The page is static (GitHub Pages): `index.html`, `style.css`, `app.js`, and small
  JSON / PMTiles files in `data/`. No build step.
- [MapLibre GL JS](https://maplibre.org) 5 draws the map, as a globe or flat.
  The basemap is [OpenFreeMap](https://openfreemap.org) vector tiles under a custom
  style, with USGS imagery and shaded relief as alternatives.
- The flood layer is one national raster [PMTiles](https://protomaps.com/docs/pmtiles)
  file whose pixels hold a bit mask of the return periods that flood them. The browser
  colors each tile for the current legend selection, so one tile set serves every view.
- Stream reaches are a vector PMTiles file with discharge and flooded area for each period.
- The large files are served from a Hugging Face dataset, which supports the HTTP range
  requests PMTiles needs.

## Rebuilding the data

The `build/` scripts read the outputs of the FIMserv flood-mapping project (paths in
`build/config.py`, overridable with `NFM_FIMSERV` and `NFM_BOX_TERRAIN`) and never write
to them. They need a conda env with GDAL >= 3.8, rasterio, geopandas, pyarrow, pillow,
pmtiles and huggingface_hub.

```
python build/flood_tiles.py base|pyramid|pack|verify   # dist/flood.pmtiles
python build/reaches.py hucs && python build/reaches.py merge
python build/streams.py tiles|search|csv               # dist/streams.pmtiles, data/search/, dist/reaches/
python build/downloads.py                              # dist/cogs/
python build/catalog.py                                # data/catalog.json, stats.json, huc8.pmtiles
python build/metadata.py                               # data/metadata.json, dist/README.md
python build/serve.py                                  # local preview with range requests
```

`PLAN.md` records the design decisions and checks.

## Credits

NOAA Office of Water Prediction (HAND, National Water Model), CIROH,
[FIMserv](https://github.com/sdmlua/FIMserv), USGS (WBD, NHDPlus V2, The National Map),
OpenStreetMap contributors and OpenFreeMap.

## License

Code: MIT (`LICENSE`). Data: CC BY 4.0.
