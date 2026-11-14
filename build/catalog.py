"""Site metadata: per-HUC8 catalog, national statistics, dataset metadata, HUC8 tiles.

Reads (read-only): FIMserv boundaries/, flood COG index.json, jobs_store.json, job
logs (HAND version, CIROH coverage), output/ rasters (dates); the reach table from
reaches.py; the HAND bucket listing in work/src/.

Writes data/catalog.json, data/stats.json, data/metadata.json, data/huc8.pmtiles.
Nothing private goes out: no hostnames, user names, local paths, job or batch IDs.

    python build/catalog.py
"""
import datetime as dt
import glob
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import BOUNDARIES, COG_DIR, FIMSERV, JOBS_STORE, SITE_DATA, WORK  # noqa: E402
from flood_tiles import PERIODS  # noqa: E402

SHOWN = [2, 5, 10, 25, 50, 100, 500]
METHOD_NAMES = {"ciroh": "CIROH", "lp3": "LP-III", "hybrid": "Hybrid"}
CONUS_HUC2 = {f"{i:02d}" for i in range(1, 19)}


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def r(x, n=2):
    return None if x is None else round(float(x), n)


def flood_index():
    with open(os.path.join(COG_DIR, "index.json"), encoding="utf-8") as f:
        return {h: e["info"] for h, e in json.load(f)["entries"].items()}


def hand_available():
    txt = open(os.path.join(WORK, "src", "hand_4_9_9_0_hucs.txt"), encoding="utf-8").read()
    return set(re.findall(r"PRE (\d{8})/", txt))


def job_facts(index):
    """Per HUC8: discharge method, HAND version and CIROH coverage from the job that
    made the rasters on disk (the latest finished job whose periods match them)."""
    with open(JOBS_STORE, encoding="utf-8") as f:
        jobs = list(json.load(f)["pipeline"].values())
    by_huc = {}
    for j in jobs:
        if j.get("finished_at") and j.get("huc8") in index:
            by_huc.setdefault(j["huc8"], []).append(j)
    facts = {}
    for h, info in index.items():
        js = sorted(by_huc.get(h, []), key=lambda j: j["finished_at"], reverse=True)
        disk = sorted(info["periods"])
        match = [j for j in js if sorted(j["params"].get("return_periods") or []) == disk and j["params"].get("mode")]
        pick = (match or [j for j in js if j["params"].get("mode")] or [None])[0]
        mode = pick["params"]["mode"] if pick else None
        if 500 in disk and mode == "ciroh":
            mode = None  # CIROH has no 500 yr; the job record cannot be the one that made these
        if mode is None:
            mode = "lp3" if 500 in disk else "ciroh"
        hand, cov = None, None
        logs = [os.path.join(FIMSERV, j["log_path"]) for j in js if j.get("log_path")]
        for lp in ([os.path.join(FIMSERV, pick["log_path"])] if pick and pick.get("log_path") else []) + logs:
            if not os.path.exists(lp):
                continue
            with open(lp, encoding="utf-8", errors="replace") as f:
                text = f.read()
            m = re.search(r"hand_fim_(\d+)_(\d+)_(\d+)_(\d+)", text)
            if m and not hand:
                hand = ".".join(m.groups())
            c = re.findall(r"CIROH covered (\d+)/(\d+) reaches", text)
            if c and cov is None and mode in ("ciroh", "hybrid"):
                a, b = map(int, c[-1])
                cov = r(100 * a / b, 1) if b else None
            if hand and (cov is not None or mode == "lp3"):
                break
        facts[h] = {"mode": mode, "hand": hand, "cov": cov}
    return facts


def raster_date(h):
    hits = glob.glob(os.path.join(FIMSERV, "output", f"flood_{h}", f"{h}_inundation", "RP100YR_*_inundation.tif"))
    if not hits:
        return None
    return dt.datetime.fromtimestamp(os.path.getmtime(hits[0]), dt.timezone.utc).strftime("%Y-%m-%d")


def primary_state(h):
    hits = glob.glob(os.path.join(FIMSERV, "USA", "*", f"{h}_*", "fim")) + \
        glob.glob(os.path.join(FIMSERV, "USA", "*", "*", f"{h}_*", "fim"))
    if not hits:
        return None
    parts = os.path.relpath(hits[0], os.path.join(FIMSERV, "USA")).split(os.sep)
    s = parts[0]
    if s == "USVirginIslands":
        return "US Virgin Islands"
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)  # NewYork -> New York


def main():
    import geopandas as gpd
    import pandas as pd

    index = flood_index()
    hand = hand_available()
    huc8 = gpd.read_file(os.path.join(BOUNDARIES, "us_huc8s.geojson"))
    huc8["HUC8"] = huc8["HUC8"].astype(str).str.zfill(8)
    huc2 = gpd.read_file(os.path.join(BOUNDARIES, "us_huc2s.geojson"), ignore_geometry=True).set_index("HUC2")["name"].to_dict()
    huc6 = gpd.read_file(os.path.join(BOUNDARIES, "us_huc6s.geojson"), ignore_geometry=True).set_index("HUC6")["name"].to_dict()
    with open(os.path.join(BOUNDARIES, "us_huc4_names.json"), encoding="utf-8") as f:
        huc4 = {k: v["name"] for k, v in json.load(f).items()}
    log("reading job facts and logs")
    facts = job_facts(index)

    reaches = gpd.read_parquet(os.path.join(WORK, "reaches_all.parquet"))
    reaches = pd.DataFrame(reaches.drop(columns="geometry"))
    per_huc = reaches.groupby("huc8").agg(
        reaches=("feature_id", "size"), km=("length_m", lambda s: s.sum() / 1000),
        gages=("gage", lambda s: int((s.astype(str) != "").sum())),
        named=("name", lambda s: int((s != "").sum())), max_order=("order", "max"))

    cat = []
    for row in huc8.itertuples():
        h = row.HUC8
        info = index.get(h)
        e = {"h": h, "n": row.name, "st": row.states, "a": r(row.areasqkm, 0),
             "h2": huc2.get(h[:2]), "h4": huc4.get(h[:4]), "h6": huc6.get(h[:6])}
        if info:
            wet = {int(k): v for k, v in info["wet_km2"].items()}
            if h not in per_huc.index:
                e["s"] = "dry"   # ran, but no return-period discharge (CIROH covers CONUS only)
            else:
                e["s"] = "mapped"  # includes two coastal HUC8s whose modeled floods are all dry
            f = facts[h]
            e.update({
                "m": METHOD_NAMES.get(f["mode"], f["mode"]),
                "p": [p for p in info["periods"] if p in SHOWN],
                "w": {str(p): r(wet[p]) for p in info["periods"] if p in SHOWN},
                "d": raster_date(h), "hv": f["hand"], "cov": f["cov"],
                "nb": int(info.get("violations", 0)), "b": [r(x, 4) for x in info["bounds"]],
                "ps": primary_state(h),
            })
            if 1000 in info["periods"]:
                e["extra"] = {"1000": r(wet[1000])}
            if h in per_huc.index:
                ph = per_huc.loc[h]
                e.update({"r": int(ph.reaches), "km": r(ph.km, 1), "g": int(ph.gages), "nm": int(ph.named),
                          "mo": int(ph.max_order)})
        elif h in hand:
            e["s"] = "notrun"   # NOAA HAND exists; not run (Ontario, no US state)
        else:
            e["s"] = "nohand"   # no NOAA HAND to map from
        cat.append(e)
    os.makedirs(SITE_DATA, exist_ok=True)
    with open(os.path.join(SITE_DATA, "catalog.json"), "w", encoding="utf-8") as f:
        json.dump({"generated": dt.date.today().isoformat(), "hucs": cat}, f, separators=(",", ":"))
    log(f"catalog: {len(cat)} HUC8s")

    # ------------------------------------------------------------ stats
    c = pd.DataFrame(cat)
    mapped = c[c.s == "mapped"]

    def area_by(df):
        return {str(p): r(sum(w.get(str(p), 0) for w in df["w"]), 1) for p in SHOWN}

    def hucs_with(df, p):
        return int(sum(1 for w in df["w"] if str(p) in w))

    status_counts = c.s.value_counts().to_dict()
    conus = c[c.h.str[:2].isin(CONUS_HUC2)]
    us = c[~c.st.fillna("").isin(["CN", "MX", "CN,MX"])]
    stats = {
        "generated": dt.date.today().isoformat(),
        "coverage": {
            "huc8_total": len(c), "status": status_counts,
            "conus_huc8": len(conus), "conus_mapped": int((conus.s == "mapped").sum()),
            "us_huc8": len(us), "us_mapped": int((us.s == "mapped").sum()),
            "mapped_area_km2": r(mapped.a.sum(), 0), "conus_area_km2": r(conus.a.sum(), 0),
        },
        "methods": mapped.m.value_counts().to_dict(),
        "periods": {str(p): {"hucs": hucs_with(mapped, p)} for p in SHOWN},
        "flooded_km2": area_by(mapped),
        "network": {
            "reaches": int(len(reaches)), "unique_reaches": int(reaches.feature_id.nunique()),
            "km": r(reaches.length_m.sum() / 1000, 0),
            "named": int((reaches.name != "").sum()),
            "gaged": int((reaches.gage.astype(str) != "").sum()),
            "lakes": int(reaches.lake.sum()),
        },
        "by_order": [], "by_state": [], "by_huc2": [],
    }
    acols = [f"a{p}" for p in SHOWN if f"a{p}" in reaches]
    for o, g in reaches.groupby("order"):
        stats["by_order"].append({"order": int(o), "reaches": int(len(g)), "km": r(g.length_m.sum() / 1000, 0),
                                  "q100_median": r(g.q100.median(), 2),
                                  "flooded_km2": {c_[1:]: r(g[c_].sum(), 1) for c_ in acols}})
    for s_, g in mapped.groupby("ps"):
        ids = set(g.h)
        rr = reaches[reaches.huc8.isin(ids)]
        stats["by_state"].append({"state": s_, "hucs": int(len(g)), "reaches": int(len(rr)),
                                  "km": r(rr.length_m.sum() / 1000, 0), "flooded_km2": area_by(g)})
    for code, g in c.groupby(c.h.str[:2]):
        gm = g[g.s == "mapped"]
        rr = reaches[reaches.huc8.isin(set(gm.h))]
        stats["by_huc2"].append({"huc2": code, "name": huc2.get(code), "hucs": int(len(g)), "mapped": int(len(gm)),
                                 "reaches": int(len(rr)), "km": r(rr.length_m.sum() / 1000, 0),
                                 "flooded_km2": area_by(gm) if len(gm) else {}})
    with open(os.path.join(SITE_DATA, "stats.json"), "w", encoding="utf-8") as f:
        json.dump(stats, f, separators=(",", ":"))
    log(f"stats: {json.dumps(stats['coverage'])}")

    huc8_tiles(huc8, cat)


def huc8_tiles(huc8=None, cat=None):
    """data/huc8.pmtiles: HUC8 polygons with code, name, states, status and f = percent of the
    watershed's area inside the 100-year floodplain (mapped watersheds only)."""
    import geopandas as gpd
    from osgeo import gdal
    if huc8 is None:
        huc8 = gpd.read_file(os.path.join(BOUNDARIES, "us_huc8s.geojson"))
        huc8["HUC8"] = huc8["HUC8"].astype(str).str.zfill(8)
    if cat is None:
        with open(os.path.join(SITE_DATA, "catalog.json"), encoding="utf-8") as f:
            cat = json.load(f)["hucs"]
    g8 = huc8[["HUC8", "name", "states", "geometry"]].rename(columns={"HUC8": "h", "name": "n", "states": "st"})
    by = {e["h"]: e for e in cat}
    g8["s"] = g8.h.map(lambda h: by[h]["s"])

    def share(h):
        e = by[h]
        if e["s"] != "mapped" or not e.get("a"):
            return None
        return round(100 * e["w"].get("100", 0) / e["a"], 2)
    g8["f"] = g8.h.map(share)
    g8["geometry"] = g8.geometry.simplify(0.0005)
    tmp = os.path.join(WORK, "huc8_web.fgb")
    g8.to_file(tmp, driver="FlatGeobuf")
    out = os.path.join(SITE_DATA, "huc8.pmtiles")
    if os.path.exists(out):
        os.remove(out)
    gdal.UseExceptions()
    gdal.VectorTranslate(out, tmp, format="PMTiles", layerName="huc8",
                         datasetCreationOptions=["MINZOOM=0", "MAXZOOM=10", "NAME=HUC8 watersheds"])
    log(f"huc8.pmtiles: {os.path.getsize(out) / 1e6:.1f} MB")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "tiles":
        huc8_tiles()   # rebuild data/huc8.pmtiles from data/catalog.json only
    else:
        main()
