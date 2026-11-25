"""Drive the UI in Edge against the simulator; print checks, save screenshots.

Run with the app started as:  python -m bowmapper --sim --no-browser
Usage: python tests/ui_drive.py <screenshot folder>
"""
import json
import sys
import time

from playwright.sync_api import sync_playwright

OUT = sys.argv[1]
URL = "http://localhost:8765/"
errors = []


def snap(page, name):
    page.screenshot(path=f"{OUT}/{name}.png")


def dro(page):
    return [page.locator(f"#dro-{a} .v").inner_text().split("\n")[0] for a in "xyz"]


def wait_text(page, sel, txt, t=30):
    t0 = time.time()
    while time.time() - t0 < t:
        if txt in page.locator(sel).inner_text():
            return True
        time.sleep(0.2)
    return False


def wait_idle(page, t=60):
    t0 = time.time()
    time.sleep(0.4)
    while time.time() - t0 < t:
        st = page.evaluate("S.m && S.m.state")
        job = page.evaluate("S.m && S.m.job && S.m.job.status")
        if st == "Idle" and job not in ("running", "paused"):
            return True
        time.sleep(0.2)
    return False


with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge", headless=True)
    page = b.new_page(viewport={"width": 1600, "height": 950})
    page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.on("dialog", lambda d: d.accept())
    page.goto(URL)
    time.sleep(2.0)
    snap(page, "01_start")
    print("state pill:", page.locator("#p-state").inner_text(), "| homed:", page.locator("#p-homed").inner_text())
    print("banner:", page.locator("#banners").inner_text()[:160].replace("\n", " | "))
    page.locator("#banners [data-act=home]").first.click()
    ok = wait_text(page, "#p-homed", "HOMED", 40)
    wait_idle(page)
    print("homed:", ok, page.locator("#p-homed").inner_text(), "DRO:", dro(page))

    # keyboard tap = 1 mm step
    page.mouse.click(800, 500)
    page.locator("#target-x").click()
    page.keyboard.press("3")
    page.keyboard.press("ArrowRight")
    time.sleep(0.8)
    print("after ArrowRight tap (1 mm):", dro(page))
    page.keyboard.down("Shift")
    page.keyboard.down("ArrowUp")
    time.sleep(1.2)
    mid = dro(page)
    page.keyboard.up("ArrowUp")
    page.keyboard.up("Shift")
    time.sleep(0.6)
    end = dro(page)
    time.sleep(0.6)
    end2 = dro(page)
    print("shift-hold up: during", mid, "after release", end, "0.6 s later", end2)

    # pad press-and-hold X+ with step 0.1 -> continuous after 420 ms
    page.locator("#steps button[data-step='0.1']").click()
    bx = page.locator("#jogpad .jb[data-d='1,0,0']").bounding_box()
    page.mouse.move(bx["x"] + bx["width"] / 2, bx["y"] + bx["height"] / 2)
    page.mouse.down()
    time.sleep(1.0)
    page.mouse.up()
    time.sleep(0.5)
    print("pad hold X+ 1 s:", dro(page))

    # laser sim
    page.locator("#tabs button[data-tab=laser]").click()
    page.locator("#l-driver button[data-v=sim]").click()
    page.locator("#l-connect").click()
    time.sleep(1.5)
    print("laser badge:", page.locator("#l-badge").inner_text())
    page.locator("#goto-coords button[data-v=machine]").click()
    page.fill("#g-x", "216")
    page.fill("#g-y", "217")
    page.fill("#g-z", "-84")
    page.locator("#g-go").click()
    wait_idle(page, 90)
    time.sleep(0.5)
    print("at die, laser:", page.locator("#l-dist").inner_text().replace("\n", " "), page.locator("#l-badge").inner_text())
    page.locator("#l-standoff").click()
    time.sleep(0.5)
    wait_idle(page, 60)
    time.sleep(0.6)
    print("after standoff:", page.locator("#l-dist").inner_text().replace("\n", " "), "Z", dro(page)[2])
    page.locator("#m-go").click()
    time.sleep(1.5)
    print("spot table rows:", page.locator("#spots tbody tr").count())
    snap(page, "02_laser")
    page.locator("button[data-zero=XY]").first.click()
    time.sleep(0.6)
    print("DRO after zero XY:", dro(page))

    # Z floor protection
    page.locator("#b-floor-here").click()
    time.sleep(0.5)
    page.locator("#steps button[data-step='1']").click()
    page.mouse.click(800, 500)
    page.locator("#target-x").click()
    page.keyboard.press("PageDown")
    time.sleep(0.8)
    print("PageDown with Z floor here ->", dro(page)[2], "| last msg:", page.locator("#sb-last").inner_text())
    page.locator("#b-floor-clear").click()
    time.sleep(0.3)

    # scan
    page.locator("#tabs button[data-tab=scan]").click()
    time.sleep(0.4)
    page.locator("#r-addreg").click()
    time.sleep(0.2)
    for k, v in (("x0", "-12"), ("y0", "-14"), ("x1", "12"), ("y1", "14")):
        page.fill(f"#regions [data-k={k}]", v)
    page.fill("#r-px", "3")
    page.fill("#r-py", "3.5")
    page.fill("#r-samples", "50")
    page.fill("#r-settle", "80")
    page.fill("#r-drift", "20")
    page.fill("#r-name", "UI test die")
    time.sleep(1.0)
    print("estimate:", page.locator("#r-est").inner_text())
    page.locator("#r-preview").click()
    time.sleep(0.6)
    snap(page, "03_scan_preview")
    page.locator("#r-start").click()
    time.sleep(3.0)
    snap(page, "04_scan_running")
    print("job banner:", page.locator("#banners").inner_text()[:200].replace("\n", " | "))
    wait_idle(page, 300)
    time.sleep(1.0)
    snap(page, "05_scan_done")
    page.locator("#tabs button[data-tab=results]").click()
    time.sleep(1.0)
    page.locator("#s-table tbody tr").first.click()
    time.sleep(1.2)
    print("session detail:", page.locator("#s-detail").inner_text()[:420].replace("\n", " | "))
    page.locator("#sd-plot").click()
    time.sleep(2.0)
    snap(page, "06_results_2d")
    page.locator("#rv-kind button[data-v='3d']").click()
    time.sleep(2.0)
    snap(page, "07_results_3d")
    page.locator("#centerview button[data-v=map]").click()
    time.sleep(0.3)

    # programs
    page.locator("#tabs button[data-tab=programs]").click()
    time.sleep(0.8)
    page.locator("#p-table tr[data-prog='example_square_20mm.nc']").click()
    time.sleep(1.0)
    print("program detail:", page.locator("#p-detail").inner_text()[:300].replace("\n", " | "))
    page.locator("#pd-run").click()
    time.sleep(1.0)
    wait_idle(page, 120)
    print("after program, DRO:", dro(page))
    page.locator("#p-table tr[data-prog='example_five_point_check.json']").click()
    time.sleep(1.0)
    print("json program:", page.locator("#p-detail").inner_text()[:250].replace("\n", " | "))
    page.locator("#pd-run").click()
    time.sleep(1.0)
    wait_idle(page, 120)
    snap(page, "08_programs")

    # machine + console
    page.locator("#tabs button[data-tab=machine]").click()
    time.sleep(1.2)
    snap(page, "09_machine")
    print("grbl rows:", page.locator("#gs-table tbody tr").count(), "| reco:", page.locator("#gs-reco").inner_text()[:80])
    page.locator("#tabs button[data-tab=console]").click()
    time.sleep(0.3)
    page.fill("#cmd", "$G")
    page.keyboard.press("Enter")
    time.sleep(0.8)
    page.fill("#cmd", "M3 S1000")
    page.keyboard.press("Enter")
    time.sleep(0.8)
    print("console tail:", page.locator("#log").inner_text()[-300:].replace("\n", " | "))
    snap(page, "10_console")

    # Esc during a long go-to
    page.locator("#goto-coords button[data-v=machine]").click()
    page.fill("#g-x", "20")
    page.fill("#g-y", "20")
    page.fill("#g-z", "")
    page.locator("#g-go").click()
    time.sleep(1.0)
    page.mouse.click(800, 500)
    page.keyboard.press("Escape")
    time.sleep(1.5)
    print("after Esc during go-to: state", page.evaluate("S.m.state"), "homed", page.evaluate("S.m.homed"),
          "job", page.evaluate("S.m.job && S.m.job.status"), "DRO", dro(page))
    page.emulate_media(color_scheme="light")
    time.sleep(0.6)
    snap(page, "11_light")
    b.close()
print("ERRORS:", json.dumps(errors[:30], indent=1))
