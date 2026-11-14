"""Per-reach table for every mapped HUC8: geometry, attributes, return-period
discharge, name and flooded area per period.

Sources (all read-only):
  discharge   FIMSERV/output/flood_<huc>/discharge_returnperiods/RP<t>YR_<huc>.csv
  geometry    nwm_subset_streams.gpkg from the HUC8's HAND terrain: local
              output/flood_<huc>/<huc>/, else Box FIMserv_Terrain/<basin>/flood_<huc>/<huc>/,
              else the member inside Box's flood_<huc>_terrain.zip
  catchments  FIMSERV/USA/<State>/<huc>_<Name>/raw/nwm_catchments_proj_subset.gpkg
  flooding    the HUC8's web COG (same one flood_tiles.py reads)
  names       NHDPlusV2 value-added attributes (nhdplusTools' nhdplusVAA.parquet on
              HydroShare): in CONUS the NWM feature_id is the NHDPlusV2 COMID

    python build/reaches.py hucs [workers]    one GeoParquet per HUC8 -> work/reaches/
    python build/reaches.py merge             national table -> work/reaches_all.parquet
"""
import glob
import json
import math
import os
import shutil
import sys
import time
import zipfile
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import BOX_TERRAIN, COG_DIR, FIMSERV, USA_DIR, WORK  # noqa: E402
from flood_tiles import GBIT, PERIODS, RES14, HALF, load_index, lut_for  # noqa: E402

OUT = os.path.join(WORK, "reaches")
TMP = os.path.join(WORK, "tmp")
VAA = os.path.join(WORK, "src", "nhdplusVAA.parquet")
R = 6378137.0
STRIP = 1024


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def discharge_table(h):
    import pandas as pd
    files = glob.glob(os.path.join(FIMSERV, "output", f"flood_{h}", "discharge_returnperiods", f"RP*YR_{h}.csv"))
    if not files:  # organized copy
        files = glob.glob(os.path.join(USA_DIR, "*", f"{h}_*", "discharge", f"RP*YR_{h}.csv")) + \
            glob.glob(os.path.join(USA_DIR, "*", "*", f"{h}_*", "discharge", f"RP*YR_{h}.csv"))
    table = None
    for f in files:
        t = int(os.path.basename(f).split("YR")[0][2:])
        d = pd.read_csv(f, usecols=["feature_id", "discharge"]).drop_duplicates("feature_id")
        d = d.rename(columns={"discharge": f"q{t}"}).set_index("feature_id")
        table = d if table is None else table.join(d, how="outer")
    return table


def streams_path(h):
    local = os.path.join(FIMSERV, "output", f"flood_{h}", h, "nwm_subset_streams.gpkg")
    if os.path.exists(local):
        return local, None
    hits = glob.glob(os.path.join(BOX_TERRAIN, "*", f"flood_{h}", h, "nwm_subset_streams.gpkg"))
    if hits:
        return hits[0], None
    zips = glob.glob(os.path.join(BOX_TERRAIN, "*", f"flood_{h}_terrain.zip"))
    if zips:
        os.makedirs(TMP, exist_ok=True)
        dst = os.path.join(TMP, f"{h}_streams.gpkg")
        with zipfile.ZipFile(zips[0]) as z, z.open(f"{h}/nwm_subset_streams.gpkg") as src, open(dst, "wb") as out:
            shutil.copyfileobj(src, out, 1 << 20)
        return dst, dst
    return None, None


def catchments_path(h):
    hits = glob.glob(os.path.join(USA_DIR, "*", f"{h}_*", "raw", "nwm_catchments_proj_subset.gpkg")) + \
        glob.glob(os.path.join(USA_DIR, "*", "*", f"{h}_*", "raw", "nwm_catchments_proj_subset.gpkg"))
    return hits[0] if hits else None


def flooded_area(h, info, catch):
    """km2 flooded per catchment per period, true area (Mercator scale removed)."""
    import rasterio
    from rasterio.features import rasterize
    from rasterio.windows import Window

    lut = lut_for(info)
    catch = catch.to_crs(3857)
    ids = catch["ID"].to_numpy()
    shapes = [(g, i + 1) for i, g in enumerate(catch.geometry) if g is not None and not g.is_empty]
    sums = np.zeros((len(PERIODS), len(ids) + 1))
    with rasterio.open(os.path.join(COG_DIR, f"{h}.tif")) as src:
        for r0 in range(0, src.height, STRIP):
            rows = min(STRIP, src.height - r0)
            win = Window(0, r0, src.width, rows)
            bits = lut[src.read(1, window=win)]
            if not bits.any():
                continue
            zone = rasterize(shapes, out_shape=(rows, src.width), transform=src.window_transform(win),
                             fill=0, dtype="int32")
            y = src.transform.f - (r0 + np.arange(rows) + 0.5) * RES14
            lat = np.arctan(np.sinh(y / R))
            w_row = (RES14 * np.cos(lat)) ** 2 / 1e6  # km2 per pixel in each row
            w = np.broadcast_to(w_row[:, None], bits.shape)
            for b in range(len(PERIODS)):
                m = (bits >> b & 1).astype(bool)
                if m.any():
                    sums[b] += np.bincount(zone[m], weights=w[m], minlength=len(ids) + 1)
    out = {}
    for b, p in enumerate(PERIODS):
        if p in info["periods"]:
            out[f"a{p}"] = sums[b, 1:]
    return ids, out


def one_huc(h, info):
    import geopandas as gpd
    import pandas as pd

    q = discharge_table(h)
    if q is None:
        return h, "no discharge"
    if q.empty:  # CIROH covered 0 reaches (outside CONUS): the rasters are dry
        return h, "no discharge rows"
    path, tmp = streams_path(h)
    if not path:
        return h, "no streams"
    try:
        import pyogrio
        fields = set(pyogrio.read_info(path)["fields"])
        slope_col = "Slope" if "Slope" in fields else "So"  # CONUS files say Slope, Alaska's say So
        cols = [c for c in ["ID", "order_", "Length", slope_col, "gages", "Lake"] if c in fields]
        s = gpd.read_file(path, columns=cols, engine="pyogrio")
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)
    s = s.rename(columns={"ID": "feature_id", "order_": "order", "Length": "length_m", slope_col: "slope", "gages": "gage"})
    for c in ("slope", "gage", "Lake", "length_m"):
        if c not in s:
            s[c] = None
    s["feature_id"] = s["feature_id"].astype("int64")
    s = s.drop_duplicates("feature_id")
    s = s[s.feature_id.isin(q.index)]
    s = s.join(q, on="feature_id")
    s["gage"] = s["gage"].fillna("").astype(str).str.strip()
    s["lake"] = (pd.to_numeric(s["Lake"], errors="coerce").fillna(-9999) > 0)
    s = s.drop(columns=["Lake"])
    cp = catchments_path(h)
    if cp:
        c = gpd.read_file(cp, columns=["ID"], engine="pyogrio")
        c["ID"] = c["ID"].astype("int64")
        ids, areas = flooded_area(h, info, c)
        a = pd.DataFrame(areas, index=ids)
        a = a.groupby(level=0).sum()
        s = s.join(a, on="feature_id")
    s["huc8"] = h
    s = s.to_crs(4326)
    os.makedirs(OUT, exist_ok=True)
    tmp_out = os.path.join(OUT, f"{h}.parquet.partial")
    s.to_parquet(tmp_out)
    os.replace(tmp_out, os.path.join(OUT, f"{h}.parquet"))
    return h, f"{len(s)} reaches of {len(q)} discharge rows"


def stage_hucs(workers, only=None):
    index = load_index()
    hucs = only or sorted(index)
    todo = [h for h in hucs if not os.path.exists(os.path.join(OUT, f"{h}.parquet"))]
    log(f"hucs: {len(hucs)} total, {len(todo)} to build")
    t0, bad = time.time(), []
    with ProcessPoolExecutor(workers) as ex:
        futs = {ex.submit(one_huc, h, index[h]): h for h in todo}
        for k, fut in enumerate(as_completed(futs), 1):
            h = futs[fut]
            try:
                _, msg = fut.result()
            except Exception as e:  # keep going; report at the end
                msg = f"FAILED {type(e).__name__}: {e}"
            if not msg[0].isdigit():
                bad.append((h, msg))
            if k % 25 == 0 or k == len(todo) or not msg[0].isdigit():
                el = time.time() - t0
                log(f"hucs: {k}/{len(todo)} {h}: {msg} ({el / 60:.1f} min, ~{el / k * (len(todo) - k) / 60:.0f} min left)")
    log(f"hucs: done; {len(bad)} problems: {bad[:20]}")


def stage_merge():
    import geopandas as gpd
    import pandas as pd
    import pyarrow.parquet as pq

    parts = sorted(glob.glob(os.path.join(OUT, "*.parquet")))
    gdf = pd.concat([gpd.read_parquet(p) for p in parts], ignore_index=True)
    log(f"merge: {len(gdf)} rows from {len(parts)} HUC8s, {gdf.feature_id.nunique()} unique reaches")
    vaa = pq.read_table(VAA, columns=["comid", "gnis_name", "totdasqkm", "reachcode", "levelpathi", "streamorde"]).to_pandas()
    vaa = vaa.rename(columns={"comid": "feature_id", "gnis_name": "name", "totdasqkm": "drainage_km2",
                              "levelpathi": "levelpath", "streamorde": "nhd_order"})
    vaa["feature_id"] = vaa["feature_id"].astype("int64")
    vaa = vaa.drop_duplicates("feature_id")
    gdf = gdf.merge(vaa, on="feature_id", how="left")
    gdf["name"] = gdf["name"].fillna("").astype(str).str.strip()
    named = (gdf["name"] != "").sum()
    log(f"merge: {named} reaches named ({100 * named / len(gdf):.1f}%), "
        f"{gdf['drainage_km2'].notna().sum()} with NHDPlusV2 attributes")
    gdf = gpd.GeoDataFrame(gdf, geometry="geometry", crs=4326)
    gdf.to_parquet(os.path.join(WORK, "reaches_all.parquet"))
    log("merge: wrote work/reaches_all.parquet")


if __name__ == "__main__":
    stage = sys.argv[1] if len(sys.argv) > 1 else "hucs"
    if stage == "hucs":
        workers = int(sys.argv[2]) if len(sys.argv) > 2 else 8
        only = sys.argv[3].split(",") if len(sys.argv) > 3 else None
        stage_hucs(workers, only)
    elif stage == "merge":
        stage_merge()
