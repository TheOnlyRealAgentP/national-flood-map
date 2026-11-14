"""Headless browser checks for the site (Playwright + Chromium).

    python tests/ui_check.py [base_url] [--shots DIR]

Default base_url starts build/serve.py on port 8091 and tests the local copy.
Checks: no page errors; the flood layer decodes and colors tiles (pixel values are
bit masks); globe <-> flat; single-period mode; HUC8 selection panel; stream search;
statistics dialog; PNG export; phone-width layout without horizontal scroll.
"""
import json
import os
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD_PY = os.path.join(os.path.expanduser("~"), "miniforge3", "envs", "nfm-build", "python.exe")

args = [a for a in sys.argv[1:] if not a.startswith("--")]
shots = None
if "--shots" in sys.argv:
    shots = sys.argv[sys.argv.index("--shots") + 1]
    args = [a for a in args if a != shots]
    os.makedirs(shots, exist_ok=True)
base = args[0] if args else None
srv = None
if not base:
    srv = subprocess.Popen([BUILD_PY, os.path.join(REPO, "build", "serve.py"), "8091"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    base = "http://localhost:8091/"

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail else ""), flush=True)


def shot(page, name):
    if shots:
        page.screenshot(path=os.path.join(shots, name + ".png"))


try:
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"])
        ctx = b.new_context(viewport={"width": 1440, "height": 900}, accept_downloads=True)
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.goto(base + "#map=7.5/40.9/-111.3", wait_until="domcontentloaded")
        page.wait_for_function("window.__nfm && window.__nfm.map && window.__nfm.map.loaded()", timeout=60000)
        page.wait_for_function("window.__nfm.map.areTilesLoaded()", timeout=90000)
        time.sleep(2)
        shot(page, "01_utah_z7")
        check("no page errors at load", not errors, "; ".join(errors[:3]))

        # flood tile decode: tile over the Weber River (16020101) at z10
        r = page.evaluate("""async () => {
            const z = 10, lng = -111.5, lat = 41.1, n = 2 ** z;
            const x = Math.floor((lng + 180) / 360 * n);
            const s = Math.sin(lat * Math.PI / 180);
            const y = Math.floor((0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)) * n);
            const v = await window.__nfm.tileValues(z, x, y);
            if (!v) return null;
            const counts = {}; let wet = 0;
            for (const a of v) if (a) { wet++; counts[a] = (counts[a] || 0) + 1; }
            return { wet, distinct: Object.keys(counts).length, top: Object.entries(counts).sort((a, b) => b[1] - a[1]).slice(0, 5) };
        }""")
        check("flood tile decodes to bit masks", r and r["wet"] > 0 and all(int(k) < 128 for k, _ in r["top"]), json.dumps(r))
        # a nested pixel's mask is 2^k - 2^j style (contiguous high bits): the most common should be 63 (all six periods)
        check("most common mask is 'all six periods' (63)", r and r["top"][0][0] == "63", json.dumps(r["top"] if r else None))

        # rendered pixels: the map canvas has blue flood color somewhere
        page.evaluate("window.__nfm.map.jumpTo({center:[-111.95, 41.2], zoom: 11})")
        page.wait_for_function("window.__nfm.map.areTilesLoaded()", timeout=60000)
        time.sleep(1.5)
        blue = page.evaluate("""() => {
            const c = window.__nfm.map.getCanvas(); const g = document.createElement('canvas');
            g.width = c.width; g.height = c.height; const x = g.getContext('2d'); x.drawImage(c, 0, 0);
            const d = x.getImageData(0, 0, g.width, g.height).data; let n = 0;
            for (let i = 0; i < d.length; i += 16) { const r = d[i], gg = d[i+1], b = d[i+2]; if (b > 90 && b > r + 50 && b > gg + 20) n++; }
            return n; }""")
        shot(page, "02_ogden_z11")
        check("flood color drawn on the map", blue > 200, f"{blue} blue samples")

        # single-period mode repaints
        page.click("[data-mode=single]")
        time.sleep(1.5)
        check("single-period legend has radios", page.locator("#legend input[type=radio]").count() == 7)
        page.click("[data-mode=stacked]")

        # globe <-> flat
        page.click("[data-proj=mercator]")
        time.sleep(0.5)
        proj = page.evaluate("window.__nfm.map.getProjection().type")
        page.click("[data-proj=globe]")
        time.sleep(0.5)
        proj2 = page.evaluate("window.__nfm.map.getProjection().type")
        check("globe/flat toggle", proj == "mercator" and proj2 == "globe", f"{proj} -> {proj2}")

        # HUC8 selection via list
        if page.evaluate("document.querySelectorAll('#huc-list button').length") > 0:
            page.fill("#f-text", "16020101")
            time.sleep(0.4)
            page.click("#huc-list button[data-h='16020101']")
            page.wait_for_selector("#panel-right:not([hidden])", timeout=10000)
            time.sleep(2)
            txt = page.inner_text("#details")
            shot(page, "03_huc_panel")
            check("HUC8 panel shows flooded area", "Flooded area" in txt and "Upper Weber" in txt, txt[:80].replace("\n", " | "))

            # search a river
            page.fill("#search", "weber")
            page.wait_for_selector("#search-results .res", timeout=10000)
            n = page.locator("#search-results .res").count()
            check("search finds Weber River", n > 0 and "Weber" in page.inner_text("#search-results"), f"{n} results")
            page.keyboard.press("Escape")

            # stats
            page.click("#btn-stats")
            page.wait_for_selector("#dlg-stats[open]")
            time.sleep(0.5)
            shot(page, "04_stats")
            check("statistics dialog renders tables", page.locator("#stats-table tbody tr").count() > 10)
            page.click("#dlg-stats [data-close]")

            # reach click: zoom to Weber near Ogden, click a stream
            page.evaluate("window.__nfm.map.jumpTo({center:[-111.93, 41.23], zoom: 12.5})")
            page.wait_for_function("window.__nfm.map.areTilesLoaded()", timeout=60000)
            time.sleep(1.5)
            pt = page.evaluate("""() => { const m = window.__nfm.map; const f = m.queryRenderedFeatures({layers:['streams']});
                f.sort((a,b)=>b.properties.o-a.properties.o); if (!f.length) return null;
                const g = f[0].geometry; const c = g.type === 'LineString' ? g.coordinates[Math.floor(g.coordinates.length/2)] : g.coordinates[0][0];
                const p = m.project(c); return {x: p.x, y: p.y, n: f.length}; }""")
            if pt:
                box = page.locator("#map").bounding_box()
                page.mouse.click(box["x"] + pt["x"], box["y"] + pt["y"])
                time.sleep(1)
                txt = page.inner_text("#details")
                shot(page, "05_reach")
                check("stream click opens reach panel with discharge", "Peak discharge" in txt, txt[:80].replace("\n", " | "))
            else:
                check("stream features rendered at z12.5", False, "no stream features")
        else:
            check("catalog loaded (HUC8 list)", False, "empty list; catalog.json missing?")

        # PNG export
        with page.expect_download(timeout=30000) as dl:
            page.click("#btn-export")
            page.click("[data-export=png]")
        path = dl.value.path()
        check("PNG export downloads", path and os.path.getsize(path) > 50000, f"{os.path.getsize(path) if path else 0} bytes")

        check("no page errors after interaction", not errors, "; ".join(errors[:3]))

        # phone width
        m = b.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True)
        mp = m.new_page()
        mp.goto(base, wait_until="domcontentloaded")
        mp.wait_for_function("window.__nfm && window.__nfm.map", timeout=60000)
        time.sleep(3)
        sw = mp.evaluate("document.documentElement.scrollWidth")
        shot(mp, "06_phone")
        check("phone width: no horizontal scroll", sw <= 390, f"scrollWidth {sw}")
        b.close()
finally:
    if srv:
        srv.terminate()

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
