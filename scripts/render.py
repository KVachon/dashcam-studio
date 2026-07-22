"""Burn the HUD onto a clip.

Streams cairo surfaces straight into ffmpeg as raw frames -- no intermediate
PNGs. Encodes with h264_videotoolbox (hardware, Apple Silicon).

    .venv/bin/python scripts/render.py \
        --clip 20260721102148_2026073.MP4 \
        --frames out/frames.json --roads out/roads.geojson \
        --out out/drive_hud.mp4

Add --seconds 20 to render just the opening, for a fast look at the motion.
"""

from __future__ import annotations

import argparse
import bisect
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cairo  # noqa: E402

from hud import TRAIL_DECIMATE, H, W, RoadNet, frame_callouts, render  # noqa: E402
from settings import encoder_args  # noqa: E402


def build_trail_index(
    frames: List[dict],
) -> Tuple[List[int], List[Tuple[float, float]], List[int]]:
    """Decimated trail points with their frame index and a run id.

    Run id increments whenever the fix drops, so the trail can be drawn as
    separate polylines rather than one line cheating across the gap.
    """
    idx, pts, runs = [], [], []
    run = 0
    had_fix = True
    for i, f in enumerate(frames):
        ok = bool(f.get("has_fix")) and f.get("lat") is not None
        if not ok:
            if had_fix:
                run += 1
            had_fix = False
            continue
        had_fix = True
        if i % TRAIL_DECIMATE:
            continue
        idx.append(i)
        pts.append((f["lat"], f["lon"]))
        runs.append(run)
    return idx, pts, runs


def trail_upto(
    idx: List[int], pts: List[Tuple[float, float]], runs: List[int],
    i: int, rec: dict,
) -> List[List[Tuple[float, float]]]:
    """Runs covering everywhere driven up to frame i, current position last."""
    hi = bisect.bisect_right(idx, i)
    out: List[List[Tuple[float, float]]] = []
    cur_run = None
    for k in range(hi):
        if runs[k] != cur_run:
            out.append([])
            cur_run = runs[k]
        out[-1].append(pts[k])
    # Decimation can leave the highlight up to TRAIL_DECIMATE frames behind the
    # marker; pin the live position on so the line always reaches it.
    if out and rec.get("has_fix") and rec.get("lat") is not None:
        out[-1].append((rec["lat"], rec["lon"]))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--clip", type=Path, required=True)
    ap.add_argument("--frames", type=Path, required=True)
    ap.add_argument("--roads", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--fps", type=float, default=30.0)
    ap.add_argument("--seconds", type=float, help="render only the first N seconds")
    ap.add_argument("--events", type=Path, help="landmarks.py output")
    ap.add_argument("--bitrate", default="12M")
    args = ap.parse_args()

    frames = json.loads(args.frames.read_text())
    net = RoadNet(args.roads)
    n = len(frames)
    if args.seconds:
        n = min(n, int(args.seconds * args.fps))

    callouts = frame_callouts(json.loads(args.events.read_text()), len(frames)) \
        if args.events and args.events.exists() else [None] * len(frames)
    t_idx, t_pts, t_runs = build_trail_index(frames)
    args.out.parent.mkdir(parents=True, exist_ok=True)

    cmd = ["ffmpeg", "-v", "error", "-stats", "-y"]
    cmd += ["-i", str(args.clip)]                       # input 0: footage
    cmd += ["-f", "rawvideo", "-pix_fmt", "bgra",       # input 1: overlay
            "-s", f"{W}x{H}", "-r", str(args.fps), "-i", "-"]
    # cairo ARGB32 is premultiplied; say so or the edges halo.
    cmd += ["-filter_complex", "[0:v][1:v]overlay=0:0:alpha=premultiplied"]
    cmd += ["-map", "0:a?"]                             # keep audio if present
    cmd += encoder_args(args.bitrate) + ["-c:a", "copy"]
    if args.seconds:
        cmd += ["-t", str(args.seconds)]
    cmd += [str(args.out)]

    print(f"rendering {n} frames ({n/args.fps:.1f}s) -> {args.out}")
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    assert proc.stdin

    t0 = time.time()
    try:
        for i in range(n):
            rec = frames[i]
            surf = render(rec, net, trail=trail_upto(t_idx, t_pts, t_runs, i, rec),
                          callout=callouts[i])
            surf.flush()
            data = surf.get_data()
            stride = surf.get_stride()
            if stride == W * 4:
                proc.stdin.write(data)
            else:  # defensive: cairo may pad rows
                mv = memoryview(data)
                for y in range(H):
                    proc.stdin.write(mv[y * stride : y * stride + W * 4])
            if i % 300 == 0 and i:
                el = time.time() - t0
                print(f"  {i}/{n}  {i/el:.1f} fps  eta {(n-i)/(i/el):.0f}s", flush=True)
    finally:
        proc.stdin.close()
        rc = proc.wait()

    dt = time.time() - t0
    if rc != 0:
        raise SystemExit(f"ffmpeg exited {rc}")
    size = args.out.stat().st_size / 1e6
    print(f"done in {dt:.0f}s ({n/dt:.1f} fps) -> {args.out} ({size:.0f} MB)")


if __name__ == "__main__":
    main()
