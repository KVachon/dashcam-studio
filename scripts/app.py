"""Dashcam Studio as a native desktop window.

Same UI and backend as the web version, but in a real macOS window instead of a
browser tab -- no server to start by hand, no URL to remember. Flask runs in a
background thread on a private localhost port; pywebview shows it.

    .venv/bin/python scripts/app.py

Needs a graphical session (it opens a window). On a headless mini, use the
browser version instead: scripts/webui.py.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import webview  # pywebview

from webui import app  # the existing Flask app, unchanged


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_up(port: int, timeout: float = 10.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/preflight", timeout=1)
            return True
        except Exception:
            time.sleep(0.15)
    return False


def main() -> None:
    # Best-effort: bring the routing engine up so the first launch is green.
    try:
        subprocess.run(["docker", "start", "valhalla"], capture_output=True, timeout=15)
    except Exception:
        pass

    port = free_port()
    threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port, threaded=True),
        daemon=True,
    ).start()

    if not wait_up(port):
        print("backend did not start", file=sys.stderr)
        sys.exit(1)

    webview.create_window(
        "Dashcam Studio",
        f"http://127.0.0.1:{port}",
        width=1100, height=900, min_size=(800, 600),
    )
    webview.start()  # blocks until the window is closed


if __name__ == "__main__":
    main()
