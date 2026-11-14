"""Hugging Face dataset: hosting check and upload.

    python build/publish_hf.py spike <hf_user>    upload a small PMTiles file, then check that range
                                                  requests return 206 with CORS through HF's redirect
    python build/publish_hf.py upload <hf_user>   upload dist/ (resumable; large files go through
                                                  upload_large_folder)

Needs `hf auth login` with a write token first.
"""
import os
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import DIST, SITE_DATA  # noqa: E402

REPO_NAME = "national-flood-map-data"


def repo_id(user):
    return f"{user}/{REPO_NAME}"


def resolve_url(user, path):
    return f"https://huggingface.co/datasets/{repo_id(user)}/resolve/main/{path}"


def check_range(url, origin="https://theonlyrealagentp.github.io"):
    """What a browser's PMTiles reader does: a Range GET with an Origin header, following redirects."""
    r = requests.get(url, headers={"Range": "bytes=0-16383", "Origin": origin}, allow_redirects=True, timeout=60)
    hops = [h.status_code for h in r.history] + [r.status_code]
    acao = r.headers.get("Access-Control-Allow-Origin")
    ok = r.status_code == 206 and len(r.content) == 16384 and acao in ("*", origin) and r.content[:7] == b"PMTiles"
    print(f"  {url}\n  hops {hops}, final host {requests.utils.urlparse(r.url).netloc}, "
          f"{len(r.content)} bytes, ACAO={acao}, magic={r.content[:7]!r} -> {'OK' if ok else 'FAIL'}")
    # the redirect itself must also allow CORS, or the browser stops at the first hop
    first = requests.get(url, headers={"Range": "bytes=0-1", "Origin": origin}, allow_redirects=False, timeout=60)
    acao0 = first.headers.get("Access-Control-Allow-Origin")
    print(f"  first hop {first.status_code}, ACAO={acao0}, expose={first.headers.get('Access-Control-Expose-Headers')}")
    return ok and (first.status_code in (200, 206) or acao0 in ("*", origin))


def spike(user):
    from huggingface_hub import HfApi
    api = HfApi()
    api.create_repo(repo_id(user), repo_type="dataset", exist_ok=True, private=False)
    test = os.path.join(os.path.dirname(DIST), "build", "work", "spike_states.pmtiles")
    api.upload_file(path_or_fileobj=test, path_in_repo="test/spike.pmtiles", repo_id=repo_id(user), repo_type="dataset",
                    commit_message="hosting check: small PMTiles file")
    time.sleep(3)
    ok = check_range(resolve_url(user, "test/spike.pmtiles"))
    print("SPIKE", "PASSED" if ok else "FAILED")
    return ok


def upload(user, only=None):
    """Upload dist/ (or only the files matching `only`, comma-separated globs). Content already on
    HF is deduplicated by its storage, so re-running after more files are built is cheap."""
    from huggingface_hub import HfApi
    api = HfApi()
    api.create_repo(repo_id(user), repo_type="dataset", exist_ok=True, private=False)
    api.upload_folder(repo_id=repo_id(user), repo_type="dataset", folder_path=DIST,
                      allow_patterns=only.split(",") if only else None,
                      ignore_patterns=["*.partial*", "*.tmp", ".cache/*"],
                      commit_message=f"data: {only or 'all files'}")
    if only:
        return
    try:
        api.delete_file("test/spike.pmtiles", repo_id=repo_id(user), repo_type="dataset", commit_message="remove hosting test file")
    except Exception:
        pass
    for f in ("flood.pmtiles", "streams.pmtiles"):
        check_range(resolve_url(user, f))


if __name__ == "__main__":
    {"spike": spike, "upload": upload}[sys.argv[1]](*sys.argv[2:])
