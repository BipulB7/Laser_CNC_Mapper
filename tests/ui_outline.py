"""Browser: Find silicon -> outline on map -> trace with corner stops. Usage: python tests/ui_outline.py <out dir>"""
import sys
import time

from playwright.sync_api import sync_playwright

OUT = sys.argv[1]
errors = []


def idle(page, t=600):
    t0 = time.time()
    time.sleep(0.5)
    while time.time() - t0 < t:
        if page.evaluate("S.m && S.m.state") == "Idle" and page.evaluate("S.m && S.m.job && S.m.job.status") not in ("running", "paused"):
            return
        time.sleep(0.3)


with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge", headless=True)
    pg = b.new_page(viewport={"width": 1600, "height": 950})
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    pg.goto("http://localhost:8765/")
    time.sleep(2)
    pg.locator("#banners [data-act=home]").first.click()
    idle(pg)
    pg.locator("#goto-coords button[data-v=machine]").click()
    pg.fill("#g-x", "218.5")
    pg.fill("#g-y", "214.0")
    pg.fill("#g-z", "-79.6")
    pg.locator("#g-go").click()
    idle(pg)
    pg.locator("button[data-zero=XY]").first.click()
    pg.locator("#tabs button[data-tab=scan]").click()
    time.sleep(0.8)
    pg.locator("#loc-go").click()
    idle(pg)
    time.sleep(1.5)
    print("badge:", pg.inner_text("#loc-badge"))
    print("result:", pg.inner_text("#loc-res").replace("\n", " | "))
    pg.locator("#m-clear").click()
    pg.locator("#m-fitjob").click()
    time.sleep(0.6)
    pg.screenshot(path=f"{OUT}/outline.png")
    pg.fill("#tr-feed", "3000")
    pg.locator("#tr-go").click()
    stops = 0
    t0 = time.time()
    while time.time() - t0 < 120:
        if pg.locator("#banners [data-act=jresume]").count():
            stops += 1
            if stops == 2:
                pg.screenshot(path=f"{OUT}/trace_stop.png")
                print("banner at stop 2:", pg.inner_text("#banners").replace("\n", " | ")[:220])
            pg.locator("#banners [data-act=jresume]").click()
            time.sleep(0.8)
        if pg.evaluate("S.m.job && S.m.job.status") in ("done", "failed", "stopped") and stops >= 1:
            break
        time.sleep(0.3)
    idle(pg)
    print("stops:", stops, "| last job:", pg.evaluate("S.m.job && S.m.job.status"))
    b.close()
print("errors:", errors)
