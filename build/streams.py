"""Stream products from work/reaches_all.parquet (built by reaches.py merge).

    python build/streams.py tiles     dist/streams.pmtiles (GDAL MVT/PMTiles)
    python build/streams.py search    data/search/n_<k>.json (names) + i_<nn>.json (feature IDs)
    python build/streams.py csv       dist/reaches/<huc8>.csv (downloads)

Stream tiles are one MVT layer, "streams", fed from four order bands with their own
minimum zoom, so the national view shows big rivers and small streams appear as you
zoom in. Every reach carries its full attribute set (see data/metadata.json).
"""
import json
import os
import re
import sys
import time
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import DIST, SITE_DATA, WORK  # noqa: E402

SHOWN = [2, 5, 10, 25, 50, 100, 500]
BANDS = [("s7", 7, 99, 3), ("s5", 5, 6, 6), ("s3", 3, 4, 8), ("s1", 0, 2, 10)]  # name, min order, max order, minzoom
MAXZOOM = 12


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def load():
    import geopandas as gpd
    g = gpd.read_parquet(os.path.join(WORK, "reaches_all.parquet"))
    return g


def tile_frame(g):
    """Deduplicated reaches with short, typed tile properties."""
    import numpy as np
    g = g.drop_duplicates("feature_id").copy()
    out = g[["geometry"]].copy()
    out["id"] = g["feature_id"].astype("int64")
    out["n"] = g["name"].fillna("")
    out["o"] = g["order"].fillna(0).astype("int32")
    out["h"] = g["huc8"]
    out["L"] = (g["length_m"] / 1000).round(2)
    out["s"] = g["slope"].astype(float).round(5)
    out["g"] = g["gage"].fillna("").astype(str)
    out["lk"] = g["lake"].astype("int32")
    if "drainage_km2" in g:
        out["da"] = g["drainage_km2"].round(1)
    for p in SHOWN:
        if f"q{p}" in g:
            out[f"q{p}"] = g[f"q{p}"].astype(float).round(2)
        if f"a{p}" in g:
            out[f"a{p}"] = g[f"a{p}"].astype(float).round(4)
    for c in out.columns:
        if c != "geometry" and out[c].dtype.kind == "f":
            out[c] = out[c].replace([np.inf, -np.inf], np.nan)
    return out


def stage_tiles():
    from osgeo import gdal
    gdal.UseExceptions()
    g = tile_frame(load())
    log(f"tiles: {len(g)} unique reaches")
    os.makedirs(os.path.join(WORK, "bands"), exist_ok=True)
    srcs, conf = [], {}
    for name, lo, hi, minz in BANDS:
        b = g[(g.o >= lo) & (g.o <= hi)]
        p = os.path.join(WORK, "bands", f"{name}.fgb")
        b.to_file(p, driver="FlatGeobuf", layer=name)
        srcs.append(p)
        conf[name] = {"target_name": "streams", "minzoom": minz, "maxzoom": MAXZOOM}
        log(f"tiles: band {name} (order {lo}-{hi}, from z{minz}): {len(b)} reaches")
    vrt = os.path.join(WORK, "bands", "streams.vrt")
    with open(vrt, "w", encoding="utf-8") as f:
        f.write("<OGRVRTDataSource>\n")
        for (name, *_), p in zip(BANDS, srcs):
            f.write(f'  <OGRVRTLayer name="{name}"><SrcDataSource>{p}</SrcDataSource>'
                    f'<SrcLayer>{name}</SrcLayer></OGRVRTLayer>\n')
        f.write("</OGRVRTDataSource>\n")
    os.makedirs(DIST, exist_ok=True)
    out = os.path.join(DIST, "streams.pmtiles")
    tmp = out + ".partial.pmtiles"
    if os.path.exists(tmp):
        os.remove(tmp)
    t0 = time.time()
    gdal.VectorTranslate(tmp, vrt, format="PMTiles", datasetCreationOptions=[
        "MINZOOM=3", f"MAXZOOM={MAXZOOM}", "MAX_SIZE=2000000", "MAX_FEATURES=400000",
        "NAME=NWM stream reaches", "CONF=" + json.dumps(conf)])
    os.replace(tmp, out)
    log(f"tiles: wrote dist/streams.pmtiles {os.path.getsize(out) / 1e6:.0f} MB in {(time.time() - t0) / 60:.1f} min")


def norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]+", " ", s).strip()


def stage_search():
    import numpy as np
    g = load()
    g = g.drop_duplicates("feature_id")
    b = g.geometry.bounds
    g = g.assign(minx=b.minx, miny=b.miny, maxx=b.maxx, maxy=b.maxy)
    named = g[g["name"].fillna("") != ""].copy()
    named["lp"] = named["levelpath"].fillna(-1).astype("int64")
    groups = named.groupby(["name", "lp"]).agg(
        minx=("minx", "min"), miny=("miny", "min"), maxx=("maxx", "max"), maxy=("maxy", "max"),
        n=("feature_id", "size"), km=("length_m", lambda s: s.sum() / 1000),
        hucs=("huc8", lambda s: sorted(set(s))), o=("order", "max")).reset_index()
    out_dir = os.path.join(SITE_DATA, "search")
    os.makedirs(out_dir, exist_ok=True)
    shards = {}
    for row in groups.itertuples():
        key = norm(row.name)[:1] or "_"
        if not key.isalnum():
            key = "_"
        shards.setdefault(key, []).append([row.name, int(row.o), round(row.km, 1), int(row.n), row.hucs[:12],
                                           [round(row.minx, 4), round(row.miny, 4), round(row.maxx, 4), round(row.maxy, 4)]])
    for k, rows in shards.items():
        rows.sort(key=lambda r: (-r[1], -r[2]))
        with open(os.path.join(out_dir, f"n_{k}.json"), "w", encoding="utf-8") as f:
            json.dump(rows, f, separators=(",", ":"))
    log(f"search: {len(groups)} named rivers/streams in {len(shards)} name shards")
    # feature-ID lookup (served from the data host, dist/search/): id -> reach bbox centre, sharded by
    # id % 256. Compact: sorted ids as deltas, lon/lat as integers of 1e-4 degrees.
    ids = g["feature_id"].astype("int64").to_numpy()
    cx = np.round((g.minx.to_numpy() + g.maxx.to_numpy()) / 2 * 1e4).astype("int64")
    cy = np.round((g.miny.to_numpy() + g.maxy.to_numpy()) / 2 * 1e4).astype("int64")
    id_dir = os.path.join(DIST, "search")
    os.makedirs(id_dir, exist_ok=True)
    shard = ids % 256
    for s in range(256):
        m = shard == s
        order = np.argsort(ids[m])
        si, sx, sy = ids[m][order], cx[m][order], cy[m][order]
        d = {"d": np.diff(si, prepend=0).tolist(), "x": sx.tolist(), "y": sy.tolist()}
        with open(os.path.join(id_dir, f"i_{s:02x}.json"), "w", encoding="utf-8") as f:
            json.dump(d, f, separators=(",", ":"))
    for old in os.listdir(out_dir):  # earlier builds kept these with the site
        if old.startswith("i_"):
            os.remove(os.path.join(out_dir, old))
    log(f"search: {len(ids)} feature IDs in 256 shards (dist/search)")


def stage_csv():
    g = load()
    cols = ["feature_id", "name", "huc8", "order", "length_m", "slope", "drainage_km2", "gage", "lake", "reachcode"] + \
        [f"q{p}" for p in SHOWN] + [f"a{p}" for p in SHOWN]
    out_dir = os.path.join(DIST, "reaches")
    os.makedirs(out_dir, exist_ok=True)
    df = g.drop(columns="geometry")
    df = df[[c for c in cols if c in df]]
    ren = {f"q{p}": f"q{p}yr_cms" for p in SHOWN}
    ren.update({f"a{p}": f"flooded{p}yr_km2" for p in SHOWN})
    df = df.rename(columns=ren)
    for h, part in df.groupby("huc8"):
        part = part.dropna(axis=1, how="all")
        part.to_csv(os.path.join(out_dir, f"{h}.csv"), index=False, float_format="%.6g")
    log(f"csv: {df.huc8.nunique()} HUC8 reach tables")


if __name__ == "__main__":
    {"tiles": stage_tiles, "search": stage_search, "csv": stage_csv}[sys.argv[1]]()
