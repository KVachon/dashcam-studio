"""Content-aware timelapse.

Speed is driven by how fast the *view rotates*, not by how fast the car moved:
nausea tracks angular velocity. Straights blast, turns hold back, dead time
collapses. Remaining harshness on turns is absorbed by frame blending rather
than by crushing the rate to real-time, which would read as constant braking.

    .venv/bin/python scripts/timelapse.py \
        --clip 20260721102148_2026073.MP4 \
        --frames out/frames.json --roads out/roads.geojson \
        --out out/drive_timelapse.mp4 --target-duration 45

Omit --target-duration to let the rate rules decide the length.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from pathlib import Path
from typing import List, Sequence, Tuple

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hud  # noqa: E402
from hud import H, W, RoadNet  # noqa: E402
from render import build_trail_index, trail_upto  # noqa: E402
from settings import encoder_args  # noqa: E402

# ---------------------------------------------------------------- tuning ---

MAX_RATE = 60.0        # straights
# The floor must be consistent with OUT_ROT_HARD: MIN_RATE * (max turn rate)
# has to stay under it, or the two rules fight and the cap loses. At 3x a
# 78 deg/s turn yields 234 deg/s out, just inside the ceiling.
MIN_RATE = 3.0
STATIONARY_RATE = 120.0
NOFIX_RATE = 90.0      # nothing to show and nothing to learn

# Target rotation in the OUTPUT. Above ~120 deg/s a first-person drive view
# starts to feel like a whip-pan.
OUT_ROT_TARGET = 120.0
# Absolute ceiling, enforced AFTER smoothing. Without this the EMA averages a
# turn's low rate back up toward the straights either side and the brake never
# actually applies -- which is how an early version hit 2683 deg/s.
OUT_ROT_HARD = 260.0

RATE_ENV_S = 1.2       # widen slow zones before smoothing so short turns
                       # survive the EMA instead of being averaged away
RATE_TAU_S = 2.5       # EMA on the rate, in source time
RATE_SLEW_PER_S = 25.0 # max change in rate per second of source -- time should
                       # accelerate gently, not just move fast

MAX_BLEND = 6          # frames averaged per output frame (motion blur)
BLEND_ROT_FULL = 200.0 # output deg/s at which we use the full blend budget

ZOOM_TAU_OUT_S = 1.2   # zoom easing re-smoothed in OUTPUT time


# --------------------------------------------------------------------------
# rate curve
# --------------------------------------------------------------------------


def angle_delta(a: float, b: float) -> float:
    return abs(((b - a + 180.0) % 360.0) - 180.0)


def turn_rate_series(frames: Sequence[dict], fps: float, win_s: float = 1.0) -> np.ndarray:
    """Heading change in deg per second of source time, smoothed over win_s."""
    n = len(frames)
    step = np.zeros(n, dtype=np.float64)
    for i in range(n - 1):
        a, b = frames[i].get("heading_deg"), frames[i + 1].get("heading_deg")
        if a is not None and b is not None:
            step[i] = angle_delta(a, b)
    w = max(1, int(win_s * fps))
    k = np.ones(w) / w
    return np.convolve(step, k, mode="same") * fps


def base_rates(frames: Sequence[dict], fps: float) -> np.ndarray:
    turn = turn_rate_series(frames, fps)
    n = len(frames)
    r = np.full(n, MAX_RATE)

    # The core rule: cap output rotation.
    with np.errstate(divide="ignore"):
        r = np.minimum(r, OUT_ROT_TARGET / np.maximum(turn, 1e-3))

    for i, f in enumerate(frames):
        if not f.get("has_fix"):
            r[i] = NOFIX_RATE
        elif (f.get("speed_mps") or 0.0) < 1.0:
            r[i] = STATIONARY_RATE

    return np.clip(r, MIN_RATE, max(MAX_RATE, STATIONARY_RATE, NOFIX_RATE))


def min_envelope(r: np.ndarray, w: int) -> np.ndarray:
    """Rolling minimum -- widens each slow zone so smoothing cannot erase it."""
    if w < 2:
        return r.copy()
    half = w // 2
    pad = np.pad(r, (half, half), mode="edge")
    return np.array([pad[i : i + w].min() for i in range(len(r))])


def smooth_rates(r: np.ndarray, turn: np.ndarray, fps: float) -> np.ndarray:
    """Envelope, EMA both directions, slew limit, then the hard cap.

    Order matters: the cap goes last because every smoothing step can only
    push the rate back up toward its neighbours.
    """
    dt = 1.0 / fps
    out = min_envelope(r, max(1, int(RATE_ENV_S * fps)))

    alpha = 1.0 - math.exp(-dt / RATE_TAU_S)
    cur = float(out[0])
    fwd = np.empty_like(out)
    for i, v in enumerate(out):
        cur += alpha * (float(v) - cur)
        fwd[i] = cur
    cur = float(out[-1])
    bwd = np.empty_like(out)
    for i in range(len(out) - 1, -1, -1):
        cur += alpha * (float(out[i]) - cur)
        bwd[i] = cur
    # Take the slower of the two passes so we are already slow *entering* a
    # turn, not only recovering after it.
    out = np.minimum(fwd, bwd)

    slew = RATE_SLEW_PER_S * dt
    for i in range(1, len(out)):
        out[i] = np.clip(out[i], out[i - 1] - slew, out[i - 1] + slew)

    out = np.minimum(out, OUT_ROT_HARD / np.maximum(turn, 1e-3))
    return np.maximum(out, MIN_RATE)


def output_frame_count(rates: np.ndarray) -> float:
    return float(np.sum(1.0 / rates))


def fit_to_duration(
    rates: np.ndarray, turn: np.ndarray, target_s: float, fps: float
) -> Tuple[np.ndarray, bool]:
    """Scale the whole curve to hit a target length, re-clamping each time.

    Clamping perturbs the total, so bisect on the scale factor. The rotation
    ceiling is re-applied inside the loop -- a target duration must never be
    allowed to buy its way past the comfort limit.
    """
    want = target_s * fps
    ceiling = OUT_ROT_HARD / np.maximum(turn, 1e-3)
    lo, hi = 0.05, 40.0
    best = rates
    for _ in range(40):
        k = (lo + hi) / 2
        cand = np.maximum(np.minimum(rates * k, ceiling), MIN_RATE)
        got = output_frame_count(cand)
        best = cand
        if got > want:
            lo = k          # too long -> speed up
        else:
            hi = k
        if abs(got - want) / want < 0.005:
            break
    return best, abs(output_frame_count(best) - want) / want < 0.05


# --------------------------------------------------------------------------
# schedule
# --------------------------------------------------------------------------


def build_schedule(rates: np.ndarray, n: int) -> List[Tuple[float, float]]:
    """Source-frame span covered by each output frame."""
    spans, pos = [], 0.0
    while pos < n - 1:
        r = float(rates[min(int(pos), n - 1)])
        nxt = min(pos + r, float(n))
        spans.append((pos, nxt))
        pos = nxt
    return spans


def blend_indices(span: Tuple[float, float], n: int) -> List[int]:
    """Evenly spaced source frames to average -- approximates a long exposure."""
    a, b = span
    count = int(np.clip(round(b - a), 1, MAX_BLEND))
    if count <= 1:
        return [int(np.clip(round((a + b) / 2 - 0.5), 0, n - 1))]
    xs = np.linspace(a, b - 1e-6, count)
    return sorted({int(np.clip(round(x), 0, n - 1)) for x in xs})


# --------------------------------------------------------------------------
# fast path: let ffmpeg emit only the frames we keep
# --------------------------------------------------------------------------

# Geometric so a handful of levels cover 1x..120x; snapping to these keeps the
# number of rate SEGMENTS (hence ffmpeg select terms) small.
RATE_LEVELS = [1, 2, 3, 4, 5, 6, 8, 10, 13, 16, 20, 26, 34, 45, 60, 90, 120]

# ffmpeg's select-expression parser fails past ~90 terms, so the segment list
# (one term each) is merged down to this before building the expression.
MAX_SEGMENTS = 80


def _merge_to_cap(segs, cap):
    """Merge the shortest segments into their nearest-rate neighbour until the
    count fits. Merged rate is length-weighted, so total output length holds."""
    segs = [list(s) for s in segs]
    while len(segs) > cap:
        i = min(range(len(segs)), key=lambda k: segs[k][1] - segs[k][0])
        a, b, r = segs[i]
        left = segs[i - 1] if i > 0 else None
        right = segs[i + 1] if i + 1 < len(segs) else None
        into_left = left is not None and (right is None or abs(left[2] - r) <= abs(right[2] - r))
        if into_left:
            na, nb = left[0], b
            nr = round((left[2] * (left[1] - left[0]) + r * (b - a)) / (nb - na))
            segs[i - 1] = [na, nb, max(1, nr)]
            del segs[i]
        else:
            na, nb = a, right[1]
            nr = round((r * (b - a) + right[2] * (right[1] - right[0])) / (nb - na))
            segs[i] = [na, nb, max(1, nr)]
            del segs[i + 1]
    return [tuple(s) for s in segs]


def rate_segments(rates: np.ndarray, n: int):
    """Piecewise-constant integer-rate runs: [(src_a, src_b, rate), ...]."""
    snapped = [min(RATE_LEVELS, key=lambda L: abs(L - max(1.0, rates[i]))) for i in range(n)]
    segs, i = [], 0
    while i < n:
        r = snapped[i]
        j = i
        while j < n and snapped[j] == r:
            j += 1
        segs.append((i, j, r))
        i = j
    return _merge_to_cap(segs, MAX_SEGMENTS)


def selected_source_frames(segs) -> List[int]:
    """One source frame per output frame: a, a+r, a+2r, ... per segment."""
    out = []
    for a, b, r in segs:
        out.extend(range(a, b, r))
    return out


def select_expr(segs) -> str:
    # keep frame n iff it is in a segment and on that segment's stride
    terms = [f"between(n\\,{a}\\,{b-1})*not(mod(n-{a}\\,{r}))" for a, b, r in segs]
    return "+".join(terms)


def hud_bbox(layer, W: int, H: int):
    """Nonzero-alpha bounding box of a HUD layer, padded and clamped.

    The HUD lives in one corner, so compositing only this box instead of the
    whole 1920x1080 frame is the bulk of the per-frame speedup.
    """
    a = np.frombuffer(layer.get_data(), np.uint8).reshape(H, -1, 4)[:, :W, 3]
    ys, xs = np.where(a > 0)
    if len(ys) == 0:
        return (0, 1, 0, 1)
    pad = 4
    return (max(0, ys.min() - pad), min(H, ys.max() + pad + 1),
            max(0, xs.min() - pad), min(W, xs.max() + pad + 1))


def resmooth_zoom(recs: List[dict], fps: float) -> None:
    """Zoom easing was tuned in source time; redo it in output time.

    Without this, a 2.5s source-time constant becomes 0.04s at 60x and the map
    snaps between views instead of easing.
    """
    alpha = 1.0 - math.exp(-(1.0 / fps) / ZOOM_TAU_OUT_S)
    cur = None
    for r in recs:
        z = r.get("zoom_radius_m")
        if z is None:
            continue
        cur = z if cur is None else cur + alpha * (z - cur)
        r["zoom_radius_m"] = cur


# --------------------------------------------------------------------------
# render
# --------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--clip", type=Path, required=True)
    ap.add_argument("--frames", type=Path, required=True)
    ap.add_argument("--roads", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--fps", type=float, default=30.0)
    ap.add_argument("--target-duration", type=float, help="seconds of output")
    ap.add_argument("--bitrate", default="14M")
    ap.add_argument("--blur", action="store_true",
                    help="motion-blend turns (slower; reads every source frame)")
    ap.add_argument("--dry-run", action="store_true", help="report the plan only")
    args = ap.parse_args()

    frames = json.loads(args.frames.read_text())
    n = len(frames)

    turn = turn_rate_series(frames, args.fps)
    rates = smooth_rates(base_rates(frames, args.fps), turn, args.fps)
    if args.target_duration:
        rates, ok = fit_to_duration(rates, turn, args.target_duration, args.fps)
        if not ok:
            print(
                f"! cannot hit {args.target_duration:.0f}s within the "
                f"{MIN_RATE:g}x-{STATIONARY_RATE:g}x limits; nearest is "
                f"{output_frame_count(rates)/args.fps:.1f}s"
            )

    spans = build_schedule(rates, n)
    out_n = len(spans)
    eff = np.array([rates[min(int(a), n - 1)] for a, _ in spans])
    out_rot = np.array([turn[min(int(a), n - 1)] * rates[min(int(a), n - 1)] for a, _ in spans])
    print(f"source     : {n} frames ({n/args.fps:.1f}s)")
    print(f"output     : {out_n} frames ({out_n/args.fps:.1f}s)  = {n/max(out_n,1):.1f}x overall")
    print(f"rate       : min {eff.min():.1f}x  median {np.median(eff):.1f}x  max {eff.max():.1f}x")
    print(f"output rot : median {np.median(out_rot):.0f} deg/s  "
          f"p95 {np.percentile(out_rot,95):.0f}  max {out_rot.max():.0f}")
    if args.dry_run:
        return

    if args.blur:
        render_blended(args, frames, spans, n)
    else:
        render_fast(args, frames, rates, n)
    print(f"-> {args.out} ({args.out.stat().st_size/1e6:.0f} MB)")


def _encoder(args):
    return subprocess.Popen(
        ["ffmpeg", "-v", "error", "-stats", "-y",
         "-f", "rawvideo", "-pix_fmt", "bgra", "-s", f"{W}x{H}",
         "-r", str(args.fps), "-i", "-", "-an"]
        + encoder_args(args.bitrate) + [str(args.out)],
        stdin=subprocess.PIPE)


def _recs(frames, indices, fps):
    recs = []
    for i in indices:
        r = dict(frames[int(i)])
        r["_src"] = int(i)
        if r.get("t_display"):
            r["t_display"] = r["t_display"][:5]  # seconds are noise at speed
        recs.append(r)
    resmooth_zoom(recs, fps)
    return recs


def render_fast(args, frames, rates, n):
    """Default path: ffmpeg emits only the kept frames; composite HUD bbox only.

    One source frame per output frame (no blur), so nothing is read and thrown
    away and the per-frame numpy work is confined to the HUD's corner.
    """
    segs = rate_segments(rates, n)
    idx = selected_source_frames(segs)
    recs = _recs(frames, idx, args.fps)
    print(f"fast path  : {len(segs)} rate segments, piping {len(idx)} of {n} frames")

    net = RoadNet(args.roads)
    t_idx, t_pts, t_runs = build_trail_index(frames)

    # Size the composite box from a SAMPLE of frames, not just the first: the
    # opening frame reads "ACQUIRING GPS" (narrow) while a long road name is
    # much wider, and a box sized to the first would clip it.
    y0 = y1 = x0 = x1 = None
    for r in recs[:: max(1, len(recs) // 24)]:
        lyr = hud.render(r, net, trail=trail_upto(t_idx, t_pts, t_runs, r["_src"], r))
        lyr.flush()
        a0, a1, b0, b1 = hud_bbox(lyr, W, H)
        y0 = a0 if y0 is None else min(y0, a0)
        y1 = a1 if y1 is None else max(y1, a1)
        x0 = b0 if x0 is None else min(x0, b0)
        x1 = b1 if x1 is None else max(x1, b1)
    bbox = (y0, y1, x0, x1)

    dec = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", str(args.clip),
         "-vf", f"select='{select_expr(segs)}'", "-vsync", "0",
         "-f", "rawvideo", "-pix_fmt", "bgra", "-"],
        stdout=subprocess.PIPE)
    enc = _encoder(args)
    assert dec.stdout and enc.stdin
    fsz = W * H * 4
    try:
        for k, rec in enumerate(recs):
            buf = dec.stdout.read(fsz)
            if len(buf) < fsz:
                break
            base = np.frombuffer(buf, np.uint8).reshape(H, W, 4).copy()
            layer = hud.render(rec, net,
                               trail=trail_upto(t_idx, t_pts, t_runs, rec["_src"], rec))
            layer.flush()
            y0, y1, x0, x1 = bbox
            la = np.frombuffer(layer.get_data(), np.uint8).reshape(H, -1, 4)[:, :W, :]
            reg = la[y0:y1, x0:x1].astype(np.float32)
            a = reg[:, :, 3:4] / 255.0  # premultiplied -> straight over
            base[y0:y1, x0:x1] = np.clip(
                reg + base[y0:y1, x0:x1].astype(np.float32) * (1.0 - a), 0, 255).astype(np.uint8)
            enc.stdin.write(base.tobytes())
            if k % 200 == 0 and k:
                print(f"  {k}/{len(recs)}", flush=True)
    finally:
        enc.stdin.close()
        enc.wait()
        try:
            dec.stdout.close()
        except Exception:
            pass
        dec.wait()


def render_blended(args, frames, spans, n):
    """--blur path: average several source frames per output frame (smoother
    fast turns), at the cost of reading the whole clip."""
    net = RoadNet(args.roads)
    t_idx, t_pts, t_runs = build_trail_index(frames)
    mids = [int(np.clip(round((a + b) / 2 - 0.5), 0, n - 1)) for a, b in spans]
    recs = _recs(frames, mids, args.fps)

    dec = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", str(args.clip),
         "-f", "rawvideo", "-pix_fmt", "bgra", "-"], stdout=subprocess.PIPE)
    enc = _encoder(args)
    assert dec.stdout and enc.stdin
    fsz = W * H * 4
    src_i, cur = -1, None

    def read_next():
        nonlocal src_i, cur
        b = dec.stdout.read(fsz)
        if len(b) < fsz:
            return False
        src_i += 1
        cur = np.frombuffer(b, np.uint8).reshape(H, W, 4)
        return True

    try:
        for k, span in enumerate(spans):
            acc = np.zeros((H, W, 4), np.float32)
            got = 0
            for target in blend_indices(span, n):
                while src_i < target:
                    if not read_next():
                        break
                if cur is None:
                    break
                acc += cur
                got += 1
            if got == 0:
                break
            base = acc / got
            layer = hud.render(recs[k], net,
                               trail=trail_upto(t_idx, t_pts, t_runs, recs[k]["_src"], recs[k]))
            layer.flush()
            la = np.frombuffer(layer.get_data(), np.uint8).reshape(H, -1, 4)[:, :W, :]
            a = la[:, :, 3:4].astype(np.float32) / 255.0
            outf = la.astype(np.float32) + base * (1.0 - a)
            enc.stdin.write(np.clip(outf, 0, 255).astype(np.uint8).tobytes())
            if k % 100 == 0 and k:
                print(f"  {k}/{len(spans)}", flush=True)
    finally:
        enc.stdin.close()
        enc.wait()
        try:
            dec.stdout.close()
        except Exception:
            pass
        dec.wait()


if __name__ == "__main__":
    main()
