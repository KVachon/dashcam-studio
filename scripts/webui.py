"""Local web UI for the dashcam pipeline.

Point it at a folder of clips and a folder of GPX files; it groups the clips
into drives, auto-matches each drive to the GPX that covers it, and runs
stitch -> map-match -> calibrate -> frame stream -> render, streaming progress.

    .venv/bin/python scripts/webui.py        # then open http://127.0.0.1:5151

Localhost only, single user, trusted machine -- no auth, and it will read/serve
paths you point it at. Do not expose it to a network.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, jsonify, request, send_file, send_from_directory

sys.path.insert(0, str(Path(__file__).resolve().parent))

import places
import preflight as _preflight
import us_states
from gpxtrack import CAMERA_TZ, load_gpx
from settings import folder_shortcuts
from stitch_fitcamx import VIDEO_EXTS, group_drives, load_clips, stitch

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out"
WEB = ROOT / "web"
PY = sys.executable
HERE = Path(__file__).resolve().parent

app = Flask(__name__, static_folder=None)

# job_id -> dict(status, steps[], log[], results[], error)
JOBS: dict = {}
_matched_cache: dict = {}  # gpx path -> matched.json path


# --------------------------------------------------------------------------
# gpx <-> drive matching
# --------------------------------------------------------------------------


def gpx_span(path: Path):
    """(t_start, t_end) epoch of a GPX file, or None if unreadable/empty."""
    try:
        tr = load_gpx(path)
        return tr.t_start.timestamp(), tr.t_end.timestamp()
    except Exception:
        return None


_admin_idx = None


def _admin():
    global _admin_idx
    if _admin_idx is None and (OUT / "admin.geojson").exists():
        _admin_idx = places.load(OUT / "admin.geojson")
    return _admin_idx


_gpx_cache: dict = {}


def _load_gpx_cached(path: Path):
    key = str(path)
    if key not in _gpx_cache:
        try:
            _gpx_cache[key] = load_gpx(path)
        except Exception:
            _gpx_cache[key] = None
    return _gpx_cache[key]


def drive_route(gpx_path: Path, t0: float, t1: float):
    """Reverse-geocode the drive's first and last in-window GPS fix.

    Returns 'Huntsville → Cleveland' style text, or a single place, or ''.
    """
    idx = _admin()
    tr = _load_gpx_cached(gpx_path) if gpx_path else None
    if not (idx and tr):
        return ""
    win = [p for p in tr.all_points() if t0 - 60 <= p.t_utc.timestamp() <= t1 + 60]
    if not win:
        return ""

    def place(p):
        pl, st = idx.lookup(p.lat, p.lon)
        if pl and st:
            return f"{pl}, {st}"
        return pl or (st or "")

    a, b = place(win[0]), place(win[-1])
    if a and b and a != b:
        # drop the trailing state on the first if same state
        if a.endswith(b.split(", ")[-1]):
            a = a.rsplit(",", 1)[0]
        return f"{a} → {b}"
    return a or b


def gpx_coverage(path: Path) -> dict:
    """Which states a GPX crosses, split into loaded vs needs-download."""
    try:
        tr = load_gpx(path)
    except Exception as e:
        return {"error": str(e)}
    idx = _admin()
    covered, missing = set(), {}
    pts = list(tr.all_points())
    for p in pts[:: max(1, len(pts) // 300)]:
        st = idx.lookup(p.lat, p.lon)[1] if idx else None
        if st:
            covered.add(st)
        else:
            m = us_states.state_at(p.lat, p.lon)
            if m and m not in missing:
                missing[m] = {"code": m, "name": us_states.name(m),
                              "pbf": us_states.geofabrik_url(m)}
    return {
        "covered": sorted(covered),
        "missing": list(missing.values()),
        "ok": len(missing) == 0,
    }


def scan(clips_dir: Path, gpx_dir: Path) -> dict:
    clips, skipped = load_clips(clips_dir)
    if not clips:
        return {"error": f"no readable video files in {clips_dir}"}
    drives = group_drives(clips)

    gpx_files = sorted(p for p in gpx_dir.iterdir() if p.suffix.lower() == ".gpx")
    spans = {p: gpx_span(p) for p in gpx_files}

    out_drives = []
    for i, drive in enumerate(drives):
        d0, d1 = drive[0].start.timestamp(), drive[-1].end.timestamp()
        # best GPX = largest temporal overlap with the drive window
        best, best_ov = None, 0.0
        for p, sp in spans.items():
            if not sp:
                continue
            ov = max(0.0, min(d1, sp[1]) - max(d0, sp[0]))
            if ov > best_ov:
                best, best_ov = p, ov
        start = drive[0].start.astimezone(CAMERA_TZ)
        out_drives.append({
            "index": i,
            "start_local": start.strftime("%Y-%m-%d %H:%M:%S"),
            "day": start.strftime("%a %b %-d"),           # Sun Jul 19
            "time": start.strftime("%-I:%M %p").lower(),   # 6:42 pm
            "clips": len(drive),
            "minutes": round((d1 - d0) / 60, 1),
            "gpx": best.name if best else None,
            "gpx_overlap_min": round(best_ov / 60, 1),
            "route": drive_route(best, d0, d1) if best else "",
        })
    # coverage per GPX that is actually matched to a drive
    used = {d["gpx"] for d in out_drives if d["gpx"]}
    coverage = {p.name: gpx_coverage(p) for p in gpx_files if p.name in used}

    return {
        "clips_dir": str(clips_dir),
        "gpx_dir": str(gpx_dir),
        "drives": out_drives,
        "gpx_files": [p.name for p in gpx_files],
        "coverage": coverage,
        "skipped": skipped,
    }


# --------------------------------------------------------------------------
# processing (background)
# --------------------------------------------------------------------------


def _run(cmd, log, scale=None):
    """Run a pipeline step, streaming its output into the job log.

    scale, when set, is passed as HUD_SCALE so render/timelapse pick up the
    UI's slider without a code change.
    """
    import os
    env = dict(os.environ)
    if scale is not None:
        env["HUD_SCALE"] = str(scale)
    log("$ " + " ".join(Path(str(c)).name if "/" in str(c) else str(c) for c in cmd))
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, env=env)
    for line in p.stdout:
        line = line.rstrip()
        if line:
            log(line)
    if p.wait() != 0:
        raise RuntimeError(f"step failed: {Path(str(cmd[1])).name if len(cmd) > 1 else cmd[0]}")


def _mapmatch(gpx: Path, log) -> Path:
    if gpx in _matched_cache:
        return _matched_cache[gpx]
    out = OUT / f"matched_{gpx.stem}.json"
    _run([PY, str(HERE / "mapmatch.py"), str(gpx), "--json", str(out)], log)
    _matched_cache[gpx] = out
    return out


def _prepare_drive(drive, gpx: Path, log, calibrate=True):
    """stitch -> map-match -> (calibrate) -> frame stream. Returns (video, frames, stamp)."""
    stamp = drive[0].start.astimezone(CAMERA_TZ).strftime("%Y%m%d%H%M%S")
    log("  stitching...")
    video = stitch(drive, OUT / "drives")
    log("  map-matching GPX...")
    matched = _mapmatch(gpx, log)

    sync = None
    if calibrate:
        log("  calibrating clock...")
        sync = OUT / f"sync_{stamp}.json"
        _run([PY, str(HERE / "calibrate.py"), "--clip", str(video),
              "--gpx", str(gpx), "--out", str(sync)], log)

    log("  building frame stream...")
    frames = OUT / f"frames_{stamp}.json"
    cmd = [PY, str(HERE / "framestream.py"), str(matched), "--clip", str(video),
           "--admin", str(OUT / "admin.geojson"), "--json", str(frames)]
    if sync:
        cmd += ["--sync", str(sync)]
    _run(cmd, log)
    return video, frames, stamp


def process_job(job_id: str, clips_dir: Path, gpx_dir: Path, opts: dict):
    job = JOBS[job_id]

    def log(msg):
        job["log"].append(msg)

    try:
        clips, _ = load_clips(clips_dir)
        drives = group_drives(clips)
        gpx_files = {p.name: p for p in gpx_dir.iterdir() if p.suffix.lower() == ".gpx"}
        sel = opts.get("gpx_by_drive", {})       # {drive_index: gpx_name}
        chosen = opts.get("drives")               # list of indices, or None = all
        todo = [i for i in range(len(drives)) if chosen is None or i in chosen]

        job["status"] = "running"
        for n, i in enumerate(todo):
            drive = drives[i]
            job["current"] = f"drive {i+1} ({n+1}/{len(todo)})"
            log(f"\n=== drive {i+1} ({len(drive)} clip, "
                f"{drive[0].start.astimezone(CAMERA_TZ):%a %H:%M} local) ===")

            gpx_name = sel.get(str(i)) or sel.get(i)
            if not gpx_name:
                log("  ! no GPX matched; skipping")
                continue
            gpx = gpx_files[gpx_name]

            video, frames, stamp = _prepare_drive(drive, gpx, log)

            scale = opts.get("hud_scale", 0.75)
            if opts.get("mode") == "timelapse":
                log("  rendering timelapse...")
                final = OUT / f"drive_{stamp}_timelapse.mp4"
                cmd = [PY, str(HERE / "timelapse.py"), "--clip", str(video),
                       "--frames", str(frames), "--roads", str(OUT / "roads.geojson"),
                       "--out", str(final)]
                if opts.get("target_duration"):
                    cmd += ["--target-duration", str(opts["target_duration"])]
            else:
                log("  rendering HUD...")
                final = OUT / f"drive_{stamp}_hud.mp4"
                cmd = [PY, str(HERE / "render.py"), "--clip", str(video),
                       "--frames", str(frames), "--roads", str(OUT / "roads.geojson"),
                       "--out", str(final)]
            _run(cmd, log, scale=scale)
            job["results"].append({"drive": i + 1, "file": final.name})
            log(f"  done -> {final.name}")

        job["status"] = "done"
        job["current"] = ""
    except Exception as e:  # surface, don't crash the server
        job["status"] = "error"
        job["error"] = str(e)
        log(f"ERROR: {e}")


def preview_job(job_id: str, clips_dir: Path, gpx_dir: Path, opts: dict):
    """Render ONE HUD frame from the middle of a drive, to preview the look
    without processing the whole thing. Skips calibration to stay quick."""
    job = JOBS[job_id]

    def log(msg):
        job["log"].append(msg)

    try:
        clips, _ = load_clips(clips_dir)
        drives = group_drives(clips)
        i = int(opts["drive"])
        drive = drives[i]
        gpx_name = (opts.get("gpx_by_drive", {}).get(str(i))
                    or opts.get("gpx_by_drive", {}).get(i))
        if not gpx_name:
            raise RuntimeError("no GPX matched to this drive")
        gpx = {p.name: p for p in gpx_dir.iterdir()
               if p.suffix.lower() == ".gpx"}[gpx_name]

        job["status"] = "running"
        job["current"] = f"preview drive {i+1}"
        video, frames, stamp = _prepare_drive(drive, gpx, log, calibrate=False)

        # middle of the drive
        import json as _json
        dur = len(_json.loads(frames.read_text())) / 30.0
        at = round(dur / 2)
        outdir = OUT / "previews" / stamp
        log(f"  rendering frame at {at}s...")
        _run([PY, str(HERE / "hud.py"), "--frames", str(frames),
              "--roads", str(OUT / "roads.geojson"), "--clip", str(video),
              "--at", str(at), "--outdir", str(outdir)],
             log, scale=opts.get("hud_scale", 0.75))
        png = f"previews/{stamp}/hud_{int(at):04d}s.png"
        job["results"].append({"drive": i + 1, "preview": png})
        job["status"] = "done"
        job["current"] = ""
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        log(f"ERROR: {e}")


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------


@app.get("/")
def index():
    return send_from_directory(WEB, "index.html")


@app.get("/api/browse")
def browse():
    raw = request.args.get("path") or str(Path.home())
    p = Path(raw).expanduser()
    if not p.is_dir():
        p = p.parent if p.parent.is_dir() else Path.home()
    try:
        # listing is cheap even for evicted iCloud items (metadata is local);
        # reading file *contents* is what triggers a download later.
        entries = list(p.iterdir())
    except PermissionError:
        entries = []
    dirs = sorted([d.name for d in entries if d.is_dir() and not d.name.startswith(".")])
    has_clips = any(x.suffix in VIDEO_EXTS for x in entries)
    has_gpx = any(x.suffix.lower() == ".gpx" for x in entries)
    return jsonify({"path": str(p), "parent": str(p.parent), "dirs": dirs,
                    "has_clips": has_clips, "has_gpx": has_gpx,
                    "shortcuts": folder_shortcuts()})


@app.post("/api/scan")
def api_scan():
    d = request.get_json()
    clips = Path(d["clips_dir"]).expanduser()
    gpx = Path(d["gpx_dir"]).expanduser()
    if not clips.is_dir():
        return jsonify({"error": f"not a folder: {clips}"}), 400
    if not gpx.is_dir():
        return jsonify({"error": f"not a folder: {gpx}"}), 400
    return jsonify(scan(clips, gpx))


def _start_job(fn, d):
    job_id = uuid.uuid4().hex[:8]
    JOBS[job_id] = {"status": "queued", "log": [], "results": [], "current": "", "error": None}
    threading.Thread(target=fn, args=(
        job_id, Path(d["clips_dir"]).expanduser(),
        Path(d["gpx_dir"]).expanduser(), d.get("opts", {})), daemon=True).start()
    return job_id


@app.post("/api/preview")
def api_preview():
    return jsonify({"job": _start_job(preview_job, request.get_json())})


@app.post("/api/process")
def api_process():
    d = request.get_json()
    job_id = _start_job(process_job, d)
    return jsonify({"job": job_id})


@app.get("/api/jobs/<job_id>")
def api_job(job_id):
    j = JOBS.get(job_id)
    if not j:
        return jsonify({"error": "unknown job"}), 404
    since = int(request.args.get("since", 0))
    return jsonify({"status": j["status"], "current": j["current"], "error": j["error"],
                    "results": j["results"], "log": j["log"][since:], "log_len": len(j["log"])})


@app.get("/outputs/<path:name>")
def outputs(name):
    return send_file(OUT / name)


@app.get("/api/preflight")
def api_preflight():
    return jsonify(_preflight.preflight())


if __name__ == "__main__":
    print("dashcam web UI -> http://127.0.0.1:5151")
    app.run(host="127.0.0.1", port=5151, threaded=True)
