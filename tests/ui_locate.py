"""Browser walk-through of Find silicon on the simulated MI350 OAM. Usage: python tests/ui_locate.py <out dir>"""
import sys
import time

from playwright.sync_api import sync_playwright

OUT = sys.argv[1]
errors = []


def idle(page, t=400):
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
    pg.fill("#g-x", "217.2")
    pg.fill("#g-y", "216.7")
    pg.fill("#g-z", "-79.6")
    pg.locator("#g-go").click()
    idle(pg)
    pg.locator("button[data-zero=XY]").first.click()
    time.sleep(0.5)
    pg.locator("#tabs button[data-tab=laser]").click()
    pg.locator("#l-standoff").click()
    idle(pg)
    pg.locator("#tabs button[data-tab=scan]").click()
    time.sleep(0.8)
    pg.locator("#loc-go").click()
    time.sleep(8)
    pg.locator("#m-fitjob").click()
    pg.screenshot(path=f"{OUT}/loc_running.png")
    idle(pg, 600)
    time.sleep(1.5)
    print("badge:", pg.inner_text("#loc-badge"))
    print("result:", pg.inner_text("#loc-res").replace("\n", " | "))
    print("DRO work:", [pg.locator(f"#dro-{a} .v").inner_text().split(chr(10))[0] for a in "xy"])
    pg.locator("#loc-fill").click()
    time.sleep(1.5)
    print("rotation field:", pg.input_value("#r-rot"), "| estimate:", pg.inner_text("#r-est"))
    pg.fill("#r-px", "2")
    pg.fill("#r-py", "2")
    time.sleep(1.2)
    pg.locator("#r-preview").click()
    time.sleep(1.0)
    pg.screenshot(path=f"{OUT}/loc_plan.png")
    b.close()
print("errors:", errors)
