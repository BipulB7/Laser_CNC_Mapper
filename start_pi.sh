#!/usr/bin/env bash
# Bow Mapper on Linux / Raspberry Pi. First run sets up .venv (needs internet once).
#   ./start_pi.sh                 -> http://<pi-ip>:8765 from any PC on the network
#   ./start_pi.sh --port 8766     -> extra options go to the app
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "First run: setting up the Python environment..."
  python3 -m venv .venv || { echo "Run: sudo apt install -y python3-venv"; exit 1; }
  .venv/bin/pip install --upgrade pip
fi
.venv/bin/pip install -q -r requirements.txt
exec .venv/bin/python -m bowmapper --host 0.0.0.0 --no-browser "$@"
