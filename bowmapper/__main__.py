"""python -m bowmapper [--sim] [--port 8765] [--no-browser]"""

from __future__ import annotations

import argparse
import threading
import time
import webbrowser


def main() -> None:
    ap = argparse.ArgumentParser(prog="bowmapper", description="LUNYEE 4040 + ILD1320 bow mapper")
    ap.add_argument("--host", default="127.0.0.1", help="127.0.0.1 = this PC only (default)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--sim", action="store_true", help="start with the simulated CNC and laser connected")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()

    import socket
    import urllib.request

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((a.host, a.port))
    except OSError:
        url = f"http://localhost:{a.port}/"
        try:
            urllib.request.urlopen(url + "api/state", timeout=2)
            print(f"\n  Bow Mapper is already running at {url} - opening it.")
            print("  To start fresh (e.g. reset the simulator): close the other Bow Mapper window,")
            print("  or run this in PowerShell, then start again:")
            print(f"    Get-NetTCPConnection -LocalPort {a.port} -State Listen | ForEach-Object {{ Stop-Process -Id $_.OwningProcess -Force }}\n")
            if not a.no_browser:
                webbrowser.open(url)
        except Exception:
            print(f"\n  Port {a.port} is used by another program. Start with a different port, e.g.:")
            print(f"    START_BowMapper.bat --port {a.port + 1}\n")
        raise SystemExit(1)
    finally:
        probe.close()

    import uvicorn

    from .server import S, app

    if a.sim:
        S.machine.connect("sim")
        S.set_laser("sim")

    url = f"http://{'localhost' if a.host in ('127.0.0.1', '0.0.0.0') else a.host}:{a.port}/"
    if not a.no_browser:
        threading.Thread(target=lambda: (time.sleep(1.2), webbrowser.open(url)), daemon=True).start()
    print(f"\n  Bow Mapper running at {url}\n  Close this window to stop it.\n")
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
