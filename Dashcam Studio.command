#!/bin/bash
# Double-click to launch Dashcam Studio (native window) on macOS.
cd "$(dirname "$0")"
if [ -x ".venv/bin/python" ]; then
  exec .venv/bin/python scripts/app.py
else
  exec python3 scripts/app.py
fi
