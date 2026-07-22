"""Check the pipeline's dependencies and map data, with fix-it instructions.

Used by the web UI (a red/green checklist with collapsible install steps) and
runnable standalone:

    .venv/bin/python scripts/preflight.py
"""

from __future__ import annotations

import shutil
import subprocess
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out"
TILES = ROOT / "valhalla" / "custom_files"


def _check(name, ok, detail="", fix=""):
    return {"name": name, "ok": bool(ok), "detail": detail, "fix": fix}


def _import_ok(mod):
    try:
        __import__(mod)
        return True
    except Exception:
        return False


def check_python():
    libs = [
        ("numpy", "numpy"), ("pycairo", "cairo"), ("shapely", "shapely"),
        ("timezonefinder", "timezonefinder"), ("flask", "flask"),
    ]
    out = []
    for pip_name, mod in libs:
        out.append(_check(
            f"python: {pip_name}", _import_ok(mod),
            "importable" if _import_ok(mod) else "missing",
            f"uv pip install --python .venv/bin/python {pip_name}"))
    return out


def check_binaries():
    out = []
    for tool, brew in [("ffmpeg", "ffmpeg"), ("ffprobe", "ffmpeg"),
                       ("osmium", "osmium-tool"), ("docker", "docker")]:
        path = shutil.which(tool)
        out.append(_check(f"binary: {tool}", path is not None,
                          path or "not on PATH", f"brew install {brew}"))
    return out


def check_valhalla():
    running = False
    try:
        ps = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                            capture_output=True, text=True, timeout=8)
        running = "valhalla" in ps.stdout
    except Exception:
        pass
    responding = False
    try:
        urllib.request.urlopen("http://127.0.0.1:8002/status", timeout=4)
        responding = True
    except Exception:
        pass
    fix = ("docker start valhalla   # container exists\n"
           "# first time, see README 'Runbook' to build tiles")
    return [_check("Valhalla routing service", responding,
                   "responding on :8002" if responding
                   else ("container up, not ready" if running else "not running"),
                   fix)]


def check_data():
    tiles = (TILES / "valhalla_tiles.tar").exists() or (TILES / "valhalla_tiles").is_dir()
    return [
        _check("map tiles (Valhalla)", tiles,
               "built" if tiles else "no tiles in valhalla/custom_files",
               "see README 'Runbook' — drop a Geofabrik .pbf and start the container"),
        _check("road geometry (roads.geojson)", (OUT / "roads.geojson").exists(),
               "present" if (OUT / "roads.geojson").exists() else "missing",
               "osmium tags-filter <pbf> w/highway | osmium export … (README)"),
        _check("admin boundaries (admin.geojson)", (OUT / "admin.geojson").exists(),
               "present" if (OUT / "admin.geojson").exists() else "missing",
               "osmium tags-filter <pbf> r/boundary=administrative … (README)"),
    ]


def preflight() -> dict:
    checks = check_python() + check_binaries() + check_valhalla() + check_data()
    return {"ok": all(c["ok"] for c in checks), "checks": checks}


if __name__ == "__main__":
    r = preflight()
    for c in r["checks"]:
        print(f"  [{'ok' if c['ok'] else 'XX'}] {c['name']:32} {c['detail']}")
        if not c["ok"]:
            print(f"        fix: {c['fix'].splitlines()[0]}")
    print("\nALL GOOD" if r["ok"] else "\nSOME CHECKS FAILED")
