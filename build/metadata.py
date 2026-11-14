"""data/metadata.json: dataset-level metadata, sources, methods, data dictionary.

Reads build/site_config.json (title, authors, URLs, licenses) and data/stats.json.
Also writes dist/metadata.json (same content) and dist/README.md for the
Hugging Face dataset card.

    python build/metadata.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import DIST, REPO, SITE_DATA  # noqa: E402


def main():
    cfg = json.load(open(os.path.join(REPO, "build", "site_config.json"), encoding="utf-8"))
    stats = json.load(open(os.path.join(SITE_DATA, "stats.json"), encoding="utf-8"))
    cat = json.load(open(os.path.join(SITE_DATA, "catalog.json"), encoding="utf-8"))["hucs"]
    hand = sorted({c["hv"] for c in cat if c.get("hv")})
    cov = stats["coverage"]
    citation = (f"{cfg['authors']} ({cfg['year']}). {cfg['title']} (version {cfg['version']}) [Data set]. "
                f"{cfg['site_url']}")
    meta = {
        "title": cfg["title"],
        "version": cfg["version"],
        "generated": stats["generated"],
        "description": (
            "Modeled inundation extents for the 2-, 5-, 10-, 25-, 50- and 100-year floods (and 500-year where the "
            "LP-III method was used) for US HUC8 watersheds, from NOAA OWP HAND terrain and rating curves driven by "
            "National Water Model return-period discharge, plus per-reach discharge and flooded-area statistics."),
        "license": {"code": cfg["code_license"], "data": cfg["data_license"]},
        "citation": citation,
        "disclaimer": ("Research product. Not a FEMA Flood Insurance Rate Map; not for insurance, permitting, property "
                       "or emergency decisions. Extents only (no depth). Levees, culverts, dams and local studies are "
                       "not represented."),
        "coverage": {
            "conus_huc8_mapped": cov["conus_mapped"], "conus_huc8_total": cov["conus_huc8"],
            "huc8_status_counts": cov["status"],
            "status_meanings": {
                "mapped": "flood extents produced",
                "dry": "HAND exists and the run completed, but no return-period discharge was available (CIROH covers the lower 48 only); rasters are empty",
                "notrun": "NOAA HAND exists but the watershed was not run (Ontario)",
                "nohand": "no NOAA HAND for this watershed (open water, Canada, Mexico, most of Alaska and the Pacific)",
            },
            "periods_hucs": {p: v["hucs"] for p, v in stats["periods"].items()},
        },
        "sources": {
            "hand_release": ", ".join(hand) if hand else None,
            "hand": "NOAA Office of Water Prediction HAND FIM inputs, public CIROH bucket s3://ciroh-owp-hand-fim/",
            "fim_engine": "FIMserv (https://github.com/sdmlua/FIMserv) wrapping the OWP inundation-mapping engine",
            "discharge_ciroh": "CIROH NWM return-period flows API (2-100 yr); reaches it does not cover are absent from the extent",
            "discharge_lp3": "Log-Pearson Type III fit to annual peaks of the NWM v3.0 retrospective (1979-2023), 2-500 yr",
            "streams": "NWM v3 channel network (nwm_subset_streams from the HAND inputs)",
            "names": "NHDPlus V2 value-added attributes (gnis_name, drainage area, reach code), joined on COMID = NWM feature_id (lower 48)",
            "watersheds": "USGS Watershed Boundary Dataset",
            "basemap": "OpenFreeMap (OpenStreetMap), USGS The National Map imagery and shaded relief",
        },
        "files": {
            "flood.pmtiles": "raster PMTiles z0-14, 256 px 8-bit grayscale PNG; value = return-period bit mask; 0 = dry. "
                             "z14 = 9.554 m source pixels; lower zooms are the bitwise OR of 2x2 pixels",
            "streams.pmtiles": "vector PMTiles z3-12 (overzoomable), layer 'streams'; order bands appear at z3 (order 7+), z6 (5-6), z8 (3-4), z10 (1-2)",
            "cogs/<huc8>.tif": "per-watershed Cloud-Optimized GeoTIFF, EPSG:3857 9.554 m, same bit mask, with a color table",
            "reaches/<huc8>.csv": "per-watershed reach table (fields below)",
            "metadata.json": "this file",
        },
        "encoding": {
            "bit_mask": {"0": "2-year", "1": "5-year", "2": "10-year", "3": "25-year", "4": "50-year",
                         "5": "100-year", "6": "500-year", "7": "1000-year (one watershed, 16020201)"},
            "example": "value 62 = bits 1-5 = flooded at 5, 10, 25, 50 and 100 years but not at 2 years",
            "crs": "EPSG:3857 (Web Mercator); source rasters were EPSG:5070 at 10 m, resampled nearest-neighbour",
            "seams": "where two watersheds overlap, the bit masks are OR-ed (wet if either side is wet)",
            "nesting": "extents for longer return periods normally contain shorter ones; where they do not "
                       "(mostly LP-III runs) each period is kept exactly as modeled",
        },
        "dictionary": {
            "Reach table (CSV) and stream popups": {
                "feature_id (id)": "NWM v3 reach ID; equals the NHDPlus V2 COMID in the lower 48",
                "name (n)": "GNIS name from NHDPlus V2; blank if none",
                "huc8 (h)": "HUC8 watershed the reach was mapped in",
                "order (o)": "Strahler stream order (NWM)",
                "length_m / L": "reach length, metres (tiles: km)",
                "slope (s)": "channel slope, m/m",
                "drainage_km2 (da)": "total upstream drainage area, km² (NHDPlus V2)",
                "gage (g)": "USGS gage ID on the reach, if any",
                "lake (lk)": "reach is a lake/reservoir reach in NWM",
                "reachcode": "NHD reach code (NHDPlus V2)",
                "q<T>yr_cms (q<T>)": "peak discharge for the T-year return period, m³/s",
                "flooded<T>yr_km2 (a<T>)": "area flooded at the T-year return period inside the reach's NWM catchment, km² "
                                           "(true area, computed on the 9.554 m grid)",
            },
            "Watershed catalog (catalog.json / CSV)": {
                "h / huc8": "8-digit hydrologic unit code",
                "n / name": "watershed name (WBD)",
                "st / states": "states or countries the watershed touches",
                "a / area_km2": "watershed area, km²",
                "s / status": "mapped, dry, notrun or nohand (see coverage.status_meanings)",
                "m / method": "CIROH, LP-III or Hybrid (LP-III with CIROH values preferred where present)",
                "p / periods_yr": "return periods mapped",
                "w / flooded_<T>yr_km2": "total area flooded at each return period, km²",
                "hv / hand_release": "NOAA OWP HAND release used",
                "cov / ciroh_coverage_pct": "share of the watershed's reaches CIROH had flows for",
                "d / mapped_on": "date the 100-year raster was written",
                "r, km, nm, g": "reach count, stream length (km), named reaches, reaches with a USGS gage",
                "nb": "pixels wet at a shorter return period but dry at a longer one",
                "b": "bounding box, WGS84 [west, south, east, north]",
            },
        },
    }
    for path in (os.path.join(SITE_DATA, "metadata.json"), os.path.join(DIST, "metadata.json")):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=1, ensure_ascii=False)

    card = f"""---
license: cc-by-4.0
pretty_name: {cfg['short_title']} data
tags: [geospatial, flood, hydrology, pmtiles, hand, national-water-model]
---
# {cfg['title']}

Tile sets and downloads behind the {cfg['short_title']} web map: {cfg['site_url']}

{meta['description']}

**{meta['disclaimer']}**

| File | Contents |
|---|---|
""" + "\n".join(f"| `{k}` | {v} |" for k, v in meta["files"].items()) + f"""

Pixel encoding: {meta['encoding']['example']}. Bits: """ + ", ".join(f"{k} = {v}" for k, v in meta["encoding"]["bit_mask"].items()) + f""".

Coverage: {cov['conus_mapped']:,} of {cov['conus_huc8']:,} lower-48 HUC8 watersheds.
Full metadata and data dictionary: `metadata.json`.

## Citation

{citation}

## License

Data {cfg['data_license']}. Code ({cfg['site_url']}) {cfg['code_license']}.
"""
    with open(os.path.join(DIST, "README.md"), "w", encoding="utf-8") as f:
        f.write(card)
    print("wrote data/metadata.json, dist/metadata.json, dist/README.md")


if __name__ == "__main__":
    main()
