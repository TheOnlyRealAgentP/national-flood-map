"""Paths the build scripts read from and write to.

Override the FIMserv project and Box locations with environment variables when
building on another machine. Everything under FIMSERV and BOX is read-only here.
"""
import os
import sys

# The build env is called by absolute path, not activated: point GDAL/PROJ at its data.
for _var, _sub in (("GDAL_DATA", ("Library", "share", "gdal")), ("PROJ_DATA", ("Library", "share", "proj"))):
    _p = os.path.join(sys.prefix, *_sub)
    if os.path.isdir(_p):
        os.environ.setdefault(_var, _p)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIMSERV = os.environ.get("NFM_FIMSERV", r"D:\FIMserv_Project")
BOX_TERRAIN = os.environ.get("NFM_BOX_TERRAIN", os.path.join(os.path.expanduser("~"), "Box", "FIMserv_Terrain"))

COG_DIR = os.path.join(FIMSERV, "webapp", "tile_cache", "flood")
USA_DIR = os.path.join(FIMSERV, "USA")
BOUNDARIES = os.path.join(FIMSERV, "boundaries")
JOBS_STORE = os.path.join(FIMSERV, "webapp", "jobs_store.json")

WORK = os.path.join(REPO, "build", "work")   # scratch, gitignored
DIST = os.path.join(REPO, "dist")            # big outputs for Hugging Face, gitignored
SITE_DATA = os.path.join(REPO, "data")       # small outputs served by GitHub Pages
