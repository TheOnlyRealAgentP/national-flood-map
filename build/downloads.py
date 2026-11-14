"""Per-HUC8 download files -> dist/cogs/<huc8>.tif

One Cloud-Optimized GeoTIFF per mapped HUC8, EPSG:3857 at 9.554 m (the web zoom-14
grid the site draws from), 8-bit, value = bit mask of the return periods that flood
the pixel (bit 0 = 2 yr, 1 = 5, 2 = 10, 3 = 25, 4 = 50, 5 = 100, 6 = 500, 7 = 1000),
0 = dry. Same encoding as the map tiles, whatever encoding the source COG used.
A color table shows each pixel in the color of the most frequent period that floods it.

    python build/downloads.py [workers]
"""
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import COG_DIR, DIST, WORK  # noqa: E402
from flood_tiles import PERIODS, load_index, lut_for  # noqa: E402

OUT = os.path.join(DIST, "cogs")
# light-theme flood ramp, 2 yr (darkest) .. 500 yr; 1000 yr reuses the lightest
RAMP = ["#0d366b", "#104281", "#184f95", "#1c5cab", "#256abf", "#3987e5", "#6da7ec", "#86b6ef"]


def hex_rgb(h):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def color_table():
    from osgeo import gdal
    ct = gdal.ColorTable()
    ct.SetColorEntry(0, (0, 0, 0, 0))
    for v in range(1, 256):
        low = (v & -v).bit_length() - 1  # most frequent period present
        ct.SetColorEntry(v, hex_rgb(RAMP[low]) + (255,))
    return ct


def one(h, info):
    import numpy as np
    from osgeo import gdal
    gdal.UseExceptions()
    out = os.path.join(OUT, f"{h}.tif")
    if os.path.exists(out):
        return h, "exists"
    lut = lut_for(info)
    src = gdal.Open(os.path.join(COG_DIR, f"{h}.tif"))
    arr = lut[src.GetRasterBand(1).ReadAsArray()]
    tmp = os.path.join(WORK, "cog_tmp", f"{h}.tif")
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(tmp, src.RasterXSize, src.RasterYSize, 1, gdal.GDT_Byte,
                    ["TILED=YES", "COMPRESS=ZSTD", "ZSTD_LEVEL=1", "SPARSE_OK=YES"])
    ds.SetGeoTransform(src.GetGeoTransform())
    ds.SetProjection(src.GetProjection())
    b = ds.GetRasterBand(1)
    b.SetColorTable(color_table())  # must precede the pixel data (photometric tag)
    b.SetNoDataValue(0)
    b.SetDescription("return-period flood bit mask")
    b.WriteArray(arr)
    periods = [p for p in info["periods"]]
    ds.SetMetadata({
        "HUC8": h,
        "PERIODS_YR": ",".join(str(p) for p in periods),
        "ENCODING": "bit mask; bit i set = flooded at period i of 2,5,10,25,50,100,500,1000 yr; 0 = dry",
        "PIXEL": "EPSG:3857, 9.554 m (web zoom 14), nearest-neighbour from the 10 m EPSG:5070 FIM rasters",
        "SOURCE": "FIMserv HAND FIM (NOAA OWP HAND) driven by NWM return-period discharge",
        "LICENSE": "CC BY 4.0",
    })
    ds = None
    part = out + ".partial.tif"
    gdal.Translate(part, tmp, format="COG", creationOptions=[
        "COMPRESS=DEFLATE", "LEVEL=9", "PREDICTOR=NO", "OVERVIEW_RESAMPLING=NEAREST", "BLOCKSIZE=512"])
    os.remove(tmp)
    os.replace(part, out)
    return h, f"{os.path.getsize(out) / 1e6:.1f} MB"


def main(workers):
    index = load_index()
    os.makedirs(OUT, exist_ok=True)
    mapped = {h: i for h, i in index.items() if max(i["wet_km2"].values()) > 0}
    t0 = time.time()
    with ProcessPoolExecutor(workers) as ex:
        futs = {ex.submit(one, h, i): h for h, i in mapped.items()}
        for k, f in enumerate(as_completed(futs), 1):
            h, msg = f.result()
            if k % 100 == 0 or k == len(futs):
                print(time.strftime("%H:%M:%S"), f"{k}/{len(futs)} {h} {msg} ({(time.time() - t0) / 60:.1f} min)", flush=True)
    total = sum(os.path.getsize(os.path.join(OUT, f)) for f in os.listdir(OUT) if f.endswith(".tif"))
    print(f"cogs: {len(mapped)} files, {total / 1e9:.2f} GB")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 8)
