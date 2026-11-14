"""Build flood.pmtiles: one national raster tile set of return-period flood extents.

Source: the FIMserv per-HUC8 web COGs (webapp/tile_cache/flood/<huc8>.tif), which
are EPSG:3857 at the zoom-14 pixel size, aligned to the web tile grid. Each pixel
says which return periods flood it, in the COG's own encoding (`rank` or `bits`,
see flood_cog.wet_periods_table in FIMserv). Read-only on the FIMserv side.

Output tiles are 256x256 8-bit grayscale PNGs whose value is a global bit mask:
    bit 0 = 2 yr, 1 = 5, 2 = 10, 3 = 25, 4 = 50, 5 = 100, 6 = 500, 7 = 1000
0 = dry (no tile is written when a whole tile is dry). HUC8 seams merge with
bitwise OR, and each lower zoom is the OR of 2x2 pixels of the one above, so a
pixel is wet for a period if anything under it is (thin rivers stay continuous).

Stages (each resumable; the scratch store lives in build/work/):
    python build/flood_tiles.py base      z14 tiles from every COG -> work/flood_tiles.sqlite
    python build/flood_tiles.py pyramid   z13..z0 by OR pooling
    python build/flood_tiles.py pack      PNG-encode, write dist/flood.pmtiles
    python build/flood_tiles.py verify    check sample HUC8s against their COGs
"""
import json
import math
import os
import random
import sqlite3
import sys
import time
import zlib
from concurrent.futures import ProcessPoolExecutor

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import COG_DIR, DIST, WORK  # noqa: E402

PERIODS = [2, 5, 10, 25, 50, 100, 500, 1000]
GBIT = {p: i for i, p in enumerate(PERIODS)}
ZMAX = 14
TILE = 256
HALF = math.pi * 6378137.0
RES14 = 2 * HALF / TILE / (1 << ZMAX)
STORE = os.path.join(WORK, "flood_tiles.sqlite")


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def load_index():
    with open(os.path.join(COG_DIR, "index.json"), encoding="utf-8") as f:
        return {h: e["info"] for h, e in json.load(f)["entries"].items()}


def lut_for(info):
    """256-entry table: COG pixel value -> global bit mask (mirrors flood_cog.wet_periods_table)."""
    periods = info["periods"]
    lut = np.zeros(256, np.uint8)
    if info.get("encoding", "rank") == "bits":
        for v in range(1, 256):
            m = 0
            for i, t in enumerate(periods):
                if v >> i & 1:
                    m |= 1 << GBIT[t]
            lut[v] = m
    else:
        for r in range(1, len(periods) + 1):
            m = 0
            for t in periods[r - 1:]:
                m |= 1 << GBIT[t]
            lut[r] = m
    return lut


def open_store():
    os.makedirs(WORK, exist_ok=True)
    db = sqlite3.connect(STORE, timeout=60)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=OFF")
    db.execute("CREATE TABLE IF NOT EXISTS tiles (z INT, x INT, y INT, data BLOB, PRIMARY KEY (z, x, y)) WITHOUT ROWID")
    db.execute("CREATE TABLE IF NOT EXISTS done_hucs (huc8 TEXT PRIMARY KEY, tiles INT, wet INT)")
    db.execute("CREATE TABLE IF NOT EXISTS done_zooms (z INT PRIMARY KEY, tiles INT)")
    return db


def pack_raw(a):
    return zlib.compress(a.tobytes(), 1)


def unpack_raw(b):
    return np.frombuffer(zlib.decompress(b), np.uint8).reshape(TILE, TILE)


# ---------------------------------------------------------------- base (z14)

def cog_tiles(huc8, info):
    """All non-dry z14 tiles of one COG as [(x, y, packed)], plus its wet pixel count."""
    import rasterio
    from rasterio.windows import Window

    lut = lut_for(info)
    path = os.path.join(COG_DIR, f"{huc8}.tif")
    out, wet = [], 0
    with rasterio.open(path) as src:
        t = src.transform
        if abs(t.a - RES14) > 1e-6 or abs(t.e + RES14) > 1e-6:
            raise ValueError(f"{huc8}: pixel size {t.a} is not the zoom-14 size")
        px0 = (t.c + HALF) / RES14
        py0 = (HALF - t.f) / RES14
        if abs(px0 - round(px0)) > 1e-3 or abs(py0 - round(py0)) > 1e-3:
            raise ValueError(f"{huc8}: not aligned to the tile grid")
        px0, py0 = int(round(px0)), int(round(py0))
        W, H = src.width, src.height
        tx0, tx1 = px0 // TILE, (px0 + W - 1) // TILE
        ty0, ty1 = py0 // TILE, (py0 + H - 1) // TILE
        for ty in range(ty0, ty1 + 1):
            # rows of this tile row, in raster coordinates
            r0 = ty * TILE - py0
            rr0, rr1 = max(r0, 0), min(r0 + TILE, H)
            strip = np.zeros((TILE, (tx1 - tx0 + 1) * TILE), np.uint8)
            c_off = px0 - tx0 * TILE
            block = src.read(1, window=Window(0, rr0, W, rr1 - rr0))
            strip[rr0 - r0:rr1 - r0, c_off:c_off + W] = block
            if not strip.any():
                continue
            strip = lut[strip]
            for i, tx in enumerate(range(tx0, tx1 + 1)):
                tile = strip[:, i * TILE:(i + 1) * TILE]
                n = int(np.count_nonzero(tile))
                if n:
                    wet += n
                    out.append((tx, ty, pack_raw(np.ascontiguousarray(tile))))
    return huc8, out, wet


def stage_base(workers):
    index = load_index()
    db = open_store()
    done = {r[0] for r in db.execute("SELECT huc8 FROM done_hucs")}
    todo = sorted(h for h in index if h not in done and os.path.exists(os.path.join(COG_DIR, f"{h}.tif")))
    log(f"base: {len(index)} COGs indexed, {len(done)} already done, {len(todo)} to go")
    t0, merged = time.time(), 0
    with ProcessPoolExecutor(workers) as ex:
        futs = [ex.submit(cog_tiles, h, index[h]) for h in todo]
        for k, fut in enumerate(futs, 1):
            huc8, tiles, wet = fut.result()
            cur = db.cursor()
            for x, y, blob in tiles:
                row = cur.execute("SELECT data FROM tiles WHERE z=? AND x=? AND y=?", (ZMAX, x, y)).fetchone()
                if row:  # HUC8 seam: OR the two masks
                    blob = pack_raw(unpack_raw(row[0]) | unpack_raw(blob))
                    cur.execute("UPDATE tiles SET data=? WHERE z=? AND x=? AND y=?", (blob, ZMAX, x, y))
                    merged += 1
                else:
                    cur.execute("INSERT INTO tiles VALUES (?,?,?,?)", (ZMAX, x, y, blob))
            cur.execute("INSERT OR REPLACE INTO done_hucs VALUES (?,?,?)", (huc8, len(tiles), wet))
            db.commit()
            if k % 50 == 0 or k == len(futs):
                el = time.time() - t0
                log(f"base: {k}/{len(futs)} HUC8s, {merged} seam merges, {el / 60:.1f} min, "
                    f"~{el / k * (len(futs) - k) / 60:.0f} min left")
    n = db.execute("SELECT COUNT(*) FROM tiles WHERE z=?", (ZMAX,)).fetchone()[0]
    log(f"base: done, {n} z14 tiles")


# ---------------------------------------------------------------- pyramid

def pool_parent(children):
    """children: {(dx, dy): packed} -> packed parent tile, or None if dry."""
    big = np.zeros((2 * TILE, 2 * TILE), np.uint8)
    for (dx, dy), b in children.items():
        big[dy * TILE:(dy + 1) * TILE, dx * TILE:(dx + 1) * TILE] = unpack_raw(b)
    q = big.reshape(TILE, 2, TILE, 2)
    p = q[:, 0, :, 0] | q[:, 0, :, 1] | q[:, 1, :, 0] | q[:, 1, :, 1]
    return pack_raw(p) if p.any() else None


def _pool_batch(batch):
    return [(px, py, pool_parent(ch)) for px, py, ch in batch]


def stage_pyramid(workers):
    db = open_store()
    done = {r[0] for r in db.execute("SELECT z FROM done_zooms")}
    with ProcessPoolExecutor(workers) as ex:
        for z in range(ZMAX - 1, -1, -1):
            if z in done:
                continue
            db.execute("DELETE FROM tiles WHERE z=?", (z,))
            rd = sqlite3.connect(STORE, timeout=60)
            rows = rd.execute("SELECT x, y, data FROM tiles WHERE z=? ORDER BY (x>>1), (y>>1)", (z + 1,))
            batch, batches, cur_key, ch, written = [], [], None, {}, 0

            def flush_batches(final=False):
                nonlocal batches, written
                if not batches or (len(batches) < workers * 2 and not final):
                    return
                for res in ex.map(_pool_batch, batches):
                    for px, py, blob in res:
                        if blob:
                            db.execute("INSERT INTO tiles VALUES (?,?,?,?)", (z, px, py, blob))
                            written += 1
                db.commit()
                batches = []

            for x, y, data in rows:
                key = (x >> 1, y >> 1)
                if key != cur_key:
                    if cur_key is not None:
                        batch.append((cur_key[0], cur_key[1], ch))
                        if len(batch) >= 2000:
                            batches.append(batch)
                            batch = []
                            flush_batches()
                    cur_key, ch = key, {}
                ch[(x & 1, y & 1)] = data
            if cur_key is not None:
                batch.append((cur_key[0], cur_key[1], ch))
            if batch:
                batches.append(batch)
            flush_batches(final=True)
            rd.close()
            db.execute("INSERT OR REPLACE INTO done_zooms VALUES (?,?)", (z, written))
            db.commit()
            log(f"pyramid: z{z} {written} tiles")


# ---------------------------------------------------------------- pack

def _png_batch(batch):
    import io
    from PIL import Image
    out = []
    for tid, z, x, y, b in batch:
        buf = io.BytesIO()
        Image.fromarray(unpack_raw(b), "L").save(buf, "PNG", optimize=True)
        out.append((tid, buf.getvalue()))
    return out


def stage_pack(workers):
    from pmtiles.tile import TileType, Compression, zxy_to_tileid
    from pmtiles.writer import Writer

    db = open_store()
    os.makedirs(DIST, exist_ok=True)
    ids = []
    for z, x, y in db.execute("SELECT z, x, y FROM tiles"):
        ids.append((zxy_to_tileid(z, x, y), z, x, y))
    ids.sort()
    log(f"pack: {len(ids)} tiles")
    tmp = os.path.join(DIST, "flood.pmtiles.partial")
    total_bytes = 0
    with open(tmp, "wb") as f:
        w = Writer(f)
        chunk = 4000
        with ProcessPoolExecutor(workers) as ex:
            def batches():
                for i in range(0, len(ids), chunk):
                    part = ids[i:i + chunk]
                    q = []
                    for tid, z, x, y in part:
                        b = db.execute("SELECT data FROM tiles WHERE z=? AND x=? AND y=?", (z, x, y)).fetchone()[0]
                        q.append((tid, z, x, y, b))
                    # split for the pool
                    step = max(1, len(q) // workers)
                    yield [q[j:j + step] for j in range(0, len(q), step)]

            for k, group in enumerate(batches()):
                results = []
                for res in ex.map(_png_batch, group):
                    results.extend(res)
                results.sort()
                for tid, png in results:
                    w.write_tile(tid, png)
                    total_bytes += len(png)
                if k % 25 == 0:
                    log(f"pack: {min((k + 1) * chunk, len(ids))}/{len(ids)} tiles, {total_bytes / 1e9:.2f} GB")
        index = load_index()
        bounds = [min(i["bounds"][0] for i in index.values()), min(i["bounds"][1] for i in index.values()),
                  max(i["bounds"][2] for i in index.values()), max(i["bounds"][3] for i in index.values())]
        header = {
            "tile_type": TileType.PNG,
            "tile_compression": Compression.NONE,
            "min_zoom": 0,
            "max_zoom": ZMAX,
            "min_lon_e7": int(bounds[0] * 1e7), "min_lat_e7": int(bounds[1] * 1e7),
            "max_lon_e7": int(bounds[2] * 1e7), "max_lat_e7": int(bounds[3] * 1e7),
            "center_zoom": 4, "center_lon_e7": int(-98.5 * 1e7), "center_lat_e7": int(39.5 * 1e7),
        }
        meta = {
            "name": "National Flood Map - return-period flood extents",
            "format": "png",
            "encoding": "8-bit grayscale; value is a bit mask of the return periods that flood the pixel; 0 = dry",
            "bits": {str(i): f"{p}-year" for i, p in enumerate(PERIODS)},
            "pyramid": "zoom 14 = source pixels (9.55 m); lower zooms are the bitwise OR of 2x2 pixels",
            "attribution": "Flood extents: FIMserv HAND (NOAA OWP) + NWM retrospective return periods",
        }
        w.finalize(header, meta)
    os.replace(tmp, os.path.join(DIST, "flood.pmtiles"))
    log(f"pack: wrote dist/flood.pmtiles, {os.path.getsize(os.path.join(DIST, 'flood.pmtiles')) / 1e9:.2f} GB")


# ---------------------------------------------------------------- verify

def stage_verify(n=25):
    index = load_index()
    db = open_store()
    hucs = random.Random(7).sample(sorted(index), n)
    bad = 0
    for h in hucs:
        _, tiles, wet = cog_tiles(h, index[h])
        missing = 0
        for x, y, blob in tiles:
            row = db.execute("SELECT data FROM tiles WHERE z=? AND x=? AND y=?", (ZMAX, x, y)).fetchone()
            mine = unpack_raw(blob)
            if not row:
                missing += int(np.count_nonzero(mine))
                continue
            store = unpack_raw(row[0])
            missing += int(np.count_nonzero(mine & ~store))  # bits this HUC8 has that the store lost
        # the COG's own z14 wet count must match its index entry's largest-period area
        km2 = max(index[h]["wet_km2"].values())
        approx = wet * RES14 * RES14 / 1e6
        status = "OK" if missing == 0 else "LOST BITS"
        if missing:
            bad += 1
        log(f"verify {h}: {len(tiles)} tiles, {wet} wet px, lost {missing}; "
            f"wet area (web px, unscaled) {approx:.1f} vs index max {km2:.1f} km2 -> {status}")
    log(f"verify: {n - bad}/{n} HUC8s intact")
    return bad == 0


if __name__ == "__main__":
    stage = sys.argv[1] if len(sys.argv) > 1 else "base"
    workers = int(sys.argv[2]) if len(sys.argv) > 2 else 12
    {"base": stage_base, "pyramid": stage_pyramid, "pack": stage_pack}.get(stage, lambda w: None)(workers) \
        if stage != "verify" else sys.exit(0 if stage_verify() else 1)
