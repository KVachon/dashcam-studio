"""Recover the video<->GPS time offset without trusting the camera clock.

Both streams are proxies for the same thing: how fast the car is moving. The
video's frame-to-frame motion rises and falls with speed; so does the GPX. The
lag that best aligns the two IS the camera-clock error -- so the camera only
needs to be within the search window (default +/-40s), not second-accurate.

    .venv/bin/python scripts/calibrate.py \
        --clip 20260721102148_2026073.MP4 --gpx 2026-07-21.gpx

Writes out/sync.json (offset in seconds); framestream.py picks it up
automatically. --dry-run reports without writing.

Robust to the 45s cold-start gap: the correlation locks onto the overlapping
window where both streams have signal and ignores the rest.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gpxtrack import clip_start_utc, haversine_m, load_gpx  # noqa: E402

GRID_DT = 0.5          # seconds per correlation bin
MAX_LAG_S = 40.0       # camera clock assumed within this of truth
MIN_OVERLAP_BINS = 40  # refuse to trust a lag found on too little shared signal

# Stop-anchor: a stop is scene-independent, so it beats the motion proxy when
# one exists in the shared window. A true stop reads ~0.1 on the frame-diff
# proxy while even open-road cruising reads ~1.8, so a strict absolute floor
# cleanly separates the two.
V_STOP_MOTION = 0.8    # below this the video is genuinely stationary
G_STOP_SPEED = 0.7     # m/s
MIN_STOP_S = 2.0       # ignore momentary dips


# --------------------------------------------------------------------------
# signals
# --------------------------------------------------------------------------


def video_motion(clip: Path, fps: float) -> np.ndarray:
    """Per-frame motion magnitude.

    Decode a small grayscale of the road surface just ahead (a band above the
    hood, below the horizon) and take mean abs frame diff. That patch streams
    past at a rate set by forward speed; using the whole frame instead lets
    roadside parallax and turn rotation dominate, which correlate with the
    *scene*, not the speed. Even so this is a coarse proxy -- see confidence.
    """
    w, h = 96, 32
    cmd = [
        "ffmpeg", "-v", "error", "-i", str(clip),
        "-vf", "crop=iw*0.6:ih*0.18:iw*0.2:ih*0.55,scale=96:32,format=gray",
        "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    assert proc.stdout
    fsz = w * h
    prev = None
    out = []
    while True:
        buf = proc.stdout.read(fsz)
        if len(buf) < fsz:
            break
        cur = np.frombuffer(buf, np.uint8).astype(np.float32)
        out.append(0.0 if prev is None else float(np.mean(np.abs(cur - prev))))
        prev = cur
    proc.stdout.close()
    proc.wait()
    return np.array(out)


def gpx_speed_grid(gpx_path: Path, t0: float, n_bins: int) -> np.ndarray:
    """GPX speed (m/s) sampled on the correlation grid; 0 where no fix."""
    track = load_gpx(gpx_path)
    pts = list(track.all_points())
    ep = np.array([p.epoch for p in pts])
    spd = np.zeros(len(pts))
    for i in range(1, len(pts)):
        dt = ep[i] - ep[i - 1]
        if 0 < dt <= 10:  # don't bridge Arc's sleep gaps
            spd[i] = haversine_m(pts[i - 1].lat, pts[i - 1].lon,
                                 pts[i].lat, pts[i].lon) / dt

    grid = t0 + np.arange(n_bins) * GRID_DT
    out = np.zeros(n_bins)
    for k, tc in enumerate(grid):
        j = np.searchsorted(ep, tc)
        if 0 < j < len(ep) and (ep[j] - ep[j - 1]) <= 10:
            f = (tc - ep[j - 1]) / (ep[j] - ep[j - 1])
            out[k] = spd[j - 1] * (1 - f) + spd[j] * f
    return out


def bin_video(motion: np.ndarray, fps: float, n_bins: int) -> np.ndarray:
    out = np.zeros(n_bins)
    idx = (np.arange(len(motion)) / fps / GRID_DT).astype(int)
    for b in range(n_bins):
        m = motion[idx == b]
        if len(m):
            out[b] = m.mean()
    return out


# --------------------------------------------------------------------------
# stop anchor (primary when a shared stop exists)
# --------------------------------------------------------------------------


def _runs(mask: np.ndarray, min_bins: int):
    """Contiguous True runs of at least min_bins, as (start, end) bin pairs."""
    out, i, n = [], 0, len(mask)
    while i < n:
        if mask[i]:
            j = i
            while j < n and mask[j]:
                j += 1
            if j - i >= min_bins:
                out.append((i, j))
            i = j
        else:
            i += 1
    return out


def video_stopped(vid: np.ndarray) -> np.ndarray:
    return vid < V_STOP_MOTION


def gpx_stopped(gpx: np.ndarray, cover: np.ndarray) -> np.ndarray:
    """Stopped = low-speed-with-fix, plus internal coverage gaps.

    Arc stops logging when stationary, so a gap *between* two fixed regions is a
    park (a light, lunch, the destination) -- the cleanest anchor there is. The
    leading cold-start blackout and any trailing gap are still 'unknown', since
    those can be moving-without-a-fix.
    """
    stopped = cover & (gpx < G_STOP_SPEED)
    idx = np.where(cover)[0]
    if len(idx):
        gap = ~cover
        gap[: idx[0]] = False       # leading blackout: unknown, not parked
        gap[idx[-1] + 1 :] = False   # trailing: unknown
        stopped = stopped | gap
    return stopped


def stop_anchor(vid: np.ndarray, gpx: np.ndarray, cover: np.ndarray):
    """Offset from overlap of 'stopped' indicators. None if no shared stop.

    Scores stop-overlap specifically (not whole-signal agreement), so the long
    both-moving stretches -- and the open-road frames the proxy misreads as slow
    -- do not drive the result.
    """
    min_bins = max(1, int(MIN_STOP_S / GRID_DT))
    sv = np.zeros(len(vid))
    for a, b in _runs(video_stopped(vid), min_bins):
        sv[a:b] = 1.0
    sg = np.zeros(len(gpx))
    for a, b in _runs(gpx_stopped(gpx, cover), min_bins):
        sg[a:b] = 1.0

    if sv.sum() == 0 or sg.sum() == 0:
        return None  # no stop in one of the streams -> not applicable

    max_lag = int(MAX_LAG_S / GRID_DT)
    best = (-1.0, 0)
    for s in range(-max_lag, max_lag + 1):
        ov = float(np.dot(np.roll(sv, s), sg))
        if ov > best[0]:
            best = (ov, s)
    overlap, s = best
    if overlap <= 0:
        return None  # stops exist but never align within the window
    # Jaccard-style confidence: shared stop mass over the smaller stop set.
    conf = overlap / min(sv.sum(), sg.sum())
    return s * GRID_DT, conf, overlap * GRID_DT


# --------------------------------------------------------------------------
# correlation (fallback)
# --------------------------------------------------------------------------


def best_lag(vid: np.ndarray, gpx: np.ndarray) -> Tuple[float, float, float]:
    """Return (offset_s, peak_corr, sharpness).

    offset_s: add to the clip start so frame times land on true GPS time.
    sharpness: peak height over the curve's std -- a flat curve means no real
    lock and should not be trusted.
    """
    cover = gpx > 0
    max_lag = int(MAX_LAG_S / GRID_DT)
    lags = list(range(-max_lag, max_lag + 1))
    corrs = []
    for s in lags:
        vs = np.roll(vid, s)
        mask = cover.copy()
        if s > 0:
            mask[:s] = False
        elif s < 0:
            mask[s:] = False
        if mask.sum() < MIN_OVERLAP_BINS:
            corrs.append(0.0)
            continue
        a, b = vs[mask], gpx[mask]
        a = a - a.mean()
        b = b - b.mean()
        denom = np.linalg.norm(a) * np.linalg.norm(b)
        corrs.append(float(np.dot(a, b) / denom) if denom else 0.0)

    corrs = np.array(corrs)
    k = int(np.argmax(corrs))
    peak = corrs[k]
    others = np.delete(corrs, k)
    sharp = (peak - others.mean()) / (others.std() + 1e-9)
    # np.roll(vid, s) shifts video later by s bins to match GPS, i.e. the video
    # was s bins early -> the clip start needs +s*dt to land on true GPS time.
    offset = lags[k] * GRID_DT
    return offset, peak, sharp


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def calibrate(clip: Path, gpx: Path, fps: float = 30.0):
    """Returns a result dict. Prefers a shared stop; falls back to speed xcorr."""
    t0 = clip_start_utc(clip.name).timestamp()
    motion = video_motion(clip, fps)
    n_bins = int(len(motion) / fps / GRID_DT) + 1
    vid = bin_video(motion, fps, n_bins)
    gpx_g = gpx_speed_grid(gpx, t0, n_bins)
    cover = gpx_g > 0

    anchor = stop_anchor(vid, gpx_g, cover)
    if anchor is not None:
        offset, conf, shared_s = anchor
        # Overlap confidence maps onto the same gate as the xcorr path: a solid
        # stop match (conf ~1, >=4s shared) reads as sharp/peak well past thresh.
        return {
            "method": "stop", "offset": offset,
            "peak_corr": round(min(1.0, conf), 3),
            "sharpness": round(3.0 + 4.0 * conf, 2) if shared_s >= 3.0 else 1.0,
            "detail": f"{shared_s:.1f}s of stop overlap, conf {conf:.2f}",
            "cover": int(cover.sum()),
        }

    offset, peak, sharp = best_lag(vid, gpx_g)
    return {
        "method": "xcorr", "offset": offset,
        "peak_corr": round(peak, 3), "sharpness": round(sharp, 2),
        "detail": "no shared stop; speed cross-correlation",
        "cover": int(cover.sum()),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--clip", type=Path, required=True)
    ap.add_argument("--gpx", type=Path, required=True)
    ap.add_argument("--fps", type=float, default=30.0)
    ap.add_argument("--out", type=Path, default=Path("out/sync.json"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    r = calibrate(args.clip, args.gpx, args.fps)
    offset, peak, sharp = r["offset"], r["peak_corr"], r["sharpness"]

    trust = sharp >= 3.0 and peak >= 0.4
    print(f"method      : {r['method']}  ({r['detail']})")
    print(f"offset      : {offset:+.2f} s  (add to clip start)")
    print(f"confidence  : peak {peak:.3f}, sharpness {sharp:.1f}  "
          f"({'trustworthy' if trust else 'WEAK - eyeball it'})")
    print(f"gps overlap : {r['cover']} bins ({r['cover']*GRID_DT:.0f}s)")
    if abs(offset) < 1.0:
        print("-> camera clock within 1s; sync already good.")
    else:
        print(f"-> camera clock ~{abs(offset):.0f}s "
              f"{'fast' if offset < 0 else 'slow'}.")

    if not args.dry_run:
        if not trust:
            print("! low confidence: writing anyway, but framestream will "
                  "ignore it until you raise confidence or use --sync-offset.")
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(
            {"offset_s": round(offset, 3), "peak_corr": peak,
             "sharpness": sharp, "method": r["method"],
             "clip": args.clip.name}, indent=2))
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
