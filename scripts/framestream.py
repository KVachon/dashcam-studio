"""Resample the matched per-fix stream to a per-frame stream, and derive zoom.

This completes the data-layer contract. Everything downstream of here is just
"draw this record onto this frame", so the HUD design can change freely without
touching any GPS or road logic.

Usage:
    python3 scripts/framestream.py out/matched.json \
        --clip 20260721102148_2026073.MP4 --fps 30 --json out/frames.json

Stdlib only.
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Sequence
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gpxtrack import CAMERA_TZ, clip_start_utc

# Displayed wall-clock follows the *position*, not the camera. The camera has no
# GPS, so its clock stays on whatever zone it was set to -- that fixed zone is
# still what decodes the filename into UTC (see gpxtrack.clip_start_utc), but it
# must not be what we show once the drive crosses a boundary.
try:
    from timezonefinder import TimezoneFinder

    _TF = TimezoneFinder()
except Exception:  # optional; falls back to the camera zone
    _TF = None

try:
    import places as _places  # needs shapely; optional
except Exception:
    _places = None

_ZONE_CACHE: dict = {}


def zone_for(lat: float, lon: float) -> Optional[str]:
    """IANA zone at a position, cached to ~110m."""
    if _TF is None:
        return None
    key = (round(lat, 3), round(lon, 3))
    if key not in _ZONE_CACHE:
        _ZONE_CACHE[key] = _TF.timezone_at(lat=lat, lng=lon)
    return _ZONE_CACHE[key]


_ZI_CACHE: dict = {}


def _zoneinfo(name: Optional[str]):
    if not name:
        return CAMERA_TZ
    if name not in _ZI_CACHE:
        try:
            _ZI_CACHE[name] = ZoneInfo(name)
        except Exception:
            _ZI_CACHE[name] = CAMERA_TZ
    return _ZI_CACHE[name]

# --- resampling -----------------------------------------------------------

# Arc samples every 1-2s while moving. Bridge gaps up to this; anything longer
# means it stopped logging (a stop, or the cold-start lag) and we must not
# pretend to know where we were.
MAX_BRIDGE_GAP_S = 30.0

# --- zoom -----------------------------------------------------------------

# Show roughly this many seconds of travel ahead.
ZOOM_LOOKAHEAD_S = 20.0
# Floor is a legibility constraint, not a physical one: below ~140m the inset
# shows so few streets it reads as broken rather than minimal.
ZOOM_MIN_M = 140.0
ZOOM_MAX_M = 900.0

# Window over which upcoming turniness is measured.
TURN_WINDOW_S = 20.0

# Low-pass time constant. The view should ease, never snap.
ZOOM_TAU_S = 2.5

ROAD_CLASS_ZOOM = {
    "motorway": 1.35,
    "trunk": 1.25,
    "primary": 1.10,
    "secondary": 1.00,
    "tertiary": 0.90,
    "unclassified": 0.85,
    "residential": 0.75,
    "service_other": 0.65,
}


@dataclass
class FrameRecord:
    frame: int
    t_offset_s: float
    t_utc: str
    t_local: str          # camera zone -- the video's own clock reference
    t_display: str        # wall-clock where we actually are
    tz_abbr: str          # CDT / EDT ...
    tz_name: Optional[str]
    place: str            # city limits, or county when outside them
    state: str            # 2-letter
    lat: Optional[float]
    lon: Optional[float]
    cum_dist_m: Optional[float]
    road_name: str
    road_ref: str
    road_class: str
    way_id: Optional[int]
    heading_deg: Optional[float]
    speed_mps: Optional[float]
    zoom_radius_m: Optional[float]
    has_fix: bool


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _iso(s: str) -> datetime:
    return datetime.fromisoformat(s).astimezone(timezone.utc)


def lerp(a: float, b: float, f: float) -> float:
    return a + (b - a) * f


def lerp_angle(a: float, b: float, f: float) -> float:
    """Interpolate bearings the short way round the compass."""
    d = ((b - a + 180.0) % 360.0) - 180.0
    return (a + d * f) % 360.0


def angle_delta(a: float, b: float) -> float:
    """Smallest absolute difference between two bearings, degrees."""
    return abs(((b - a + 180.0) % 360.0) - 180.0)


def point_speeds(pts: Sequence[dict]) -> List[float]:
    """Central-difference speed (m/s) at each matched fix.

    Derived from cumulative distance rather than raw fix-to-fix displacement so
    it inherits the jitter gating already applied upstream.
    """
    n = len(pts)
    ts = [_iso(p["t_utc"]).timestamp() for p in pts]
    cum = [p["cum_dist_m"] for p in pts]
    out = []
    for i in range(n):
        lo, hi = max(0, i - 1), min(n - 1, i + 1)
        dt = ts[hi] - ts[lo]
        out.append((cum[hi] - cum[lo]) / dt if dt > 0 else 0.0)
    # Light 3-tap smoothing; GPS-derived speed is noisy even off cum_dist.
    sm = []
    for i in range(n):
        lo, hi = max(0, i - 1), min(n - 1, i + 1)
        sm.append(sum(out[lo : hi + 1]) / (hi - lo + 1))
    return sm


# --------------------------------------------------------------------------
# resampling
# --------------------------------------------------------------------------


def resample(
    pts: Sequence[dict], times: Sequence[datetime], place_idx=None
) -> List[FrameRecord]:
    """Interpolate the matched stream onto arbitrary timestamps.

    Continuous quantities (position, distance, heading, speed) are interpolated.
    Categorical ones (road name/ref/class) are held from the preceding fix --
    averaging a road name is meaningless.
    """
    ts = [_iso(p["t_utc"]).timestamp() for p in pts]
    speeds = point_speeds(pts)
    zones = [zone_for(p["lat"], p["lon"]) for p in pts]
    if place_idx is not None and _places is not None:
        places_ = [_places.cached_lookup(place_idx, p["lat"], p["lon"]) for p in pts]
    else:
        places_ = [(None, None)] * len(pts)
    t0, t1 = ts[0], ts[-1]

    def clock(when: datetime, tzname: Optional[str]):
        loc = when.astimezone(_zoneinfo(tzname))
        # 12-hour, no leading zero. Built by hand rather than %-I/%#I so it is
        # identical on macOS, Linux and Windows.
        h = loc.hour % 12 or 12
        return f"{h}:{loc.minute:02d}:{loc.second:02d} {loc.strftime('%p')}", loc.strftime("%Z")

    out: List[FrameRecord] = []
    for i, when in enumerate(times):
        w = when.timestamp()
        # Before the first fix we do not know where we are, so the camera zone
        # is the only honest default.
        disp, abbr = clock(when, None)
        rec = dict(
            frame=i,
            t_offset_s=round(w - times[0].timestamp(), 4),
            t_utc=when.isoformat(),
            t_local=when.astimezone(CAMERA_TZ).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            t_display=disp,
            tz_abbr=abbr,
            tz_name=None,
            place="",
            state="",
            lat=None,
            lon=None,
            cum_dist_m=None,
            road_name="",
            road_ref="",
            road_class="",
            way_id=None,
            heading_deg=None,
            speed_mps=None,
            zoom_radius_m=None,
            has_fix=False,
        )

        # Outside the logged track entirely -- e.g. the ~45s cold-start lag.
        if w < t0 or w > t1:
            out.append(FrameRecord(**rec))
            continue

        j = bisect.bisect_right(ts, w) - 1
        j = min(max(j, 0), len(pts) - 2)
        a, b = pts[j], pts[j + 1]
        gap = ts[j + 1] - ts[j]

        # A long gap means Arc slept (we were parked). Hold the last known
        # position so the map does not teleport, but flag the fix as stale.
        if gap > MAX_BRIDGE_GAP_S:
            d_, ab_ = clock(when, zones[j])
            pl_, st_ = places_[j]
            rec.update(
                t_display=d_,
                tz_abbr=ab_,
                tz_name=zones[j],
                place=pl_ or "",
                state=st_ or "",
                lat=a["lat"],
                lon=a["lon"],
                cum_dist_m=a["cum_dist_m"],
                road_name=a["road_name"],
                road_ref=a["road_ref"],
                road_class=a["road_class"],
                way_id=a.get("way_id"),
                heading_deg=a["heading_deg"],
                speed_mps=0.0,
                has_fix=False,
            )
            out.append(FrameRecord(**rec))
            continue

        f = (w - ts[j]) / gap if gap > 0 else 0.0
        ha, hb = a["heading_deg"], b["heading_deg"]
        d_, ab_ = clock(when, zones[j])
        pl_, st_ = places_[j]
        rec.update(
            t_display=d_,
            tz_abbr=ab_,
            tz_name=zones[j],
            place=pl_ or "",
            state=st_ or "",
            lat=round(lerp(a["lat"], b["lat"], f), 7),
            lon=round(lerp(a["lon"], b["lon"], f), 7),
            cum_dist_m=round(lerp(a["cum_dist_m"], b["cum_dist_m"], f), 2),
            road_name=a["road_name"],
            road_ref=a["road_ref"],
            road_class=a["road_class"],
            way_id=a.get("way_id"),
            heading_deg=(
                round(lerp_angle(ha, hb, f), 1) if ha is not None and hb is not None else (hb if ha is None else ha)
            ),
            speed_mps=round(lerp(speeds[j], speeds[j + 1], f), 3),
            has_fix=True,
        )
        out.append(FrameRecord(**rec))

    return out


# --------------------------------------------------------------------------
# zoom
# --------------------------------------------------------------------------


def apply_zoom(frames: List[FrameRecord]) -> None:
    """Set zoom_radius_m in place.

    Wide when fast and straight, tight when slow or twisty, then low-passed so
    the view eases rather than snapping.
    """
    n = len(frames)
    if n == 0:
        return
    dt = frames[1].t_offset_s - frames[0].t_offset_s if n > 1 else 1 / 30
    window = max(1, int(TURN_WINDOW_S / dt)) if dt > 0 else 1

    # Target radius per frame, before smoothing.
    targets: List[Optional[float]] = []
    for i, fr in enumerate(frames):
        if fr.speed_mps is None:
            targets.append(None)
            continue

        r = fr.speed_mps * ZOOM_LOOKAHEAD_S

        # Upcoming turniness: total heading change over the next window.
        turn = 0.0
        prev_h = fr.heading_deg
        for k in range(i + 1, min(n, i + window)):
            h = frames[k].heading_deg
            if h is not None and prev_h is not None:
                turn += angle_delta(prev_h, h)
            if h is not None:
                prev_h = h
        r *= 1.0 / (1.0 + turn / 180.0)

        r *= ROAD_CLASS_ZOOM.get(fr.road_class, 1.0)
        targets.append(min(max(r, ZOOM_MIN_M), ZOOM_MAX_M))

    # Time-based EMA so the constant holds regardless of frame rate.
    alpha = 1.0 - math.exp(-dt / ZOOM_TAU_S) if dt > 0 else 1.0
    cur: Optional[float] = None
    for i, fr in enumerate(frames):
        tgt = targets[i]
        if tgt is None:
            fr.zoom_radius_m = round(cur, 1) if cur is not None else None
            continue
        cur = tgt if cur is None else cur + alpha * (tgt - cur)
        fr.zoom_radius_m = round(cur, 1)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def frame_times(start: datetime, duration_s: float, fps: float) -> List[datetime]:
    n = int(round(duration_s * fps))
    return [start + timedelta(seconds=i / fps) for i in range(n)]


def frame_times_segmented(segments, offset: float, fps: float) -> List[datetime]:
    """Real UTC of every frame of a stitched drive, clip by clip.

    The stitched video is gapless, but its source clips are not: each clip's
    frames map to that clip's own wall-clock window. Concatenating them gives the
    true time of each stitched frame, so the map stays synced across gaps.
    """
    out: List[datetime] = []
    for seg in segments:
        base = datetime.fromtimestamp(seg["start_epoch"] + offset, tz=timezone.utc)
        out.extend(base + timedelta(seconds=j / fps) for j in range(int(seg["n_frames"])))
    return out


def load_sync_offset(path: Path, clip_name: str) -> float:
    """Calibrated clip-start correction (seconds), only if trustworthy.

    scripts/calibrate.py writes this. A weak lock is ignored rather than
    trusted, so a bad auto-calibration can never silently desync a render.
    """
    if not path or not path.exists():
        return 0.0
    try:
        d = json.loads(path.read_text())
    except Exception:
        return 0.0
    if d.get("clip") and d["clip"] != clip_name:
        return 0.0  # calibration was for a different clip
    if d.get("sharpness", 0) < 3.0 or d.get("peak_corr", 0) < 0.4:
        return 0.0  # low confidence -- leave sync on the camera clock
    offset = float(d.get("offset_s", 0.0))
    # Dead-band at the filename's own resolution: the anchor is whole-seconds,
    # so a sub-0.5s "correction" is false precision. Don't touch a good sync.
    if abs(offset) < 0.5:
        return 0.0
    return offset


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("matched", type=Path, help="output of mapmatch.py --json")
    ap.add_argument("--clip", type=Path, required=True, help="Fitcamx .MP4 (for start time)")
    ap.add_argument("--fps", type=float, default=30.0)
    ap.add_argument("--duration", type=float, help="seconds; probed from clip if omitted")
    ap.add_argument("--admin", type=Path, default=Path("out/admin.geojson"),
                    help="OSM admin boundaries for city/state (optional)")
    ap.add_argument("--sync", type=Path, default=Path("out/sync.json"),
                    help="calibrate.py output; applied only if high-confidence")
    ap.add_argument("--sync-offset", type=float,
                    help="manual clip-start correction (s); overrides --sync")
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()

    pts = json.loads(args.matched.read_text())
    start = clip_start_utc(args.clip.name)

    if args.sync_offset is not None:
        offset = args.sync_offset
        print(f"sync         : {offset:+.2f}s (manual)")
    else:
        offset = load_sync_offset(args.sync, args.clip.name)
        if offset:
            print(f"sync         : {offset:+.2f}s (calibrated)")
    start = start + timedelta(seconds=offset)

    duration = args.duration
    if duration is None:
        import shutil, subprocess

        if shutil.which("ffprobe"):
            duration = float(
                subprocess.check_output(
                    [
                        "ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(args.clip),
                    ]
                ).strip()
            )
        else:
            raise SystemExit("ffprobe not found; pass --duration")

    place_idx = _places.load(args.admin) if _places else None
    if place_idx is None:
        print(f"! no city/state ({args.admin} missing or shapely unavailable)")

    # A stitched drive's clips are not gapless, so each frame's real time comes
    # from its clip (segments sidecar), not drive_start + i/fps. Single clips
    # (no sidecar) fall back to the simple timeline.
    seg_path = args.clip.with_suffix(".segments.json")
    if seg_path.exists():
        segs = json.loads(seg_path.read_text())
        times = frame_times_segmented(segs, offset, args.fps)
        print(f"timeline     : {len(segs)} clip(s), {len(times)} frames (gap-aware)")
    else:
        times = frame_times(start, duration, args.fps)
    frames = resample(pts, times, place_idx=place_idx)
    apply_zoom(frames)

    # Freeze the map while parked: Arc keeps logging jittery positions at a
    # stop, and map-matching snaps them to nearby roads, so the inset would
    # wander while the footage sits still. Instantaneous speed is fooled by the
    # jitter (points jump several metres), so gate on speed averaged over a few
    # seconds and hold the last moving position when it's below a crawl.
    STATIONARY = 1.6  # m/s (~3.6 mph)
    win = max(1, int(2.5 * args.fps))
    sp = [(f.speed_mps or 0.0) for f in frames]
    smooth = [sum(sp[max(0, i - win):i + win + 1]) / len(sp[max(0, i - win):i + win + 1])
              for i in range(len(frames))]
    hold = None
    for i, f in enumerate(frames):
        if f.has_fix and f.lat is not None:
            if smooth[i] < STATIONARY and hold is not None:
                f.lat, f.lon = hold
                f.speed_mps = 0.0
            else:
                hold = (f.lat, f.lon)

    # Cumulative distance reads from 0 at the start of THIS drive. mapmatch
    # counts from the beginning of the whole GPX, so a drive that starts mid-day
    # would otherwise open at the day's running total.
    base = next((f.cum_dist_m for f in frames if f.cum_dist_m is not None), None)
    if base:
        for f in frames:
            if f.cum_dist_m is not None:
                f.cum_dist_m = round(max(0.0, f.cum_dist_m - base), 2)

    with_fix = sum(1 for f in frames if f.has_fix)
    print(f"clip start   : {start.astimezone(CAMERA_TZ):%H:%M:%S} local")
    print(f"duration/fps : {duration:.2f}s @ {args.fps:g}fps = {len(frames)} frames")
    print(f"has_fix      : {with_fix}/{len(frames)} ({with_fix/len(frames)*100:.1f}%)")
    blind = len(frames) - with_fix
    if blind:
        print(f"no fix       : {blind} frames ({blind/args.fps:.1f}s)")

    print(f"\n{'FRAME':>7} {'T+':>7} {'ROAD':26} {'MPH':>5} {'ZOOM':>6} FIX")
    print("-" * 62)
    step = max(1, len(frames) // 24)
    for fr in frames[::step]:
        road = (fr.road_ref or fr.road_name or "—")[:26]
        mph = f"{fr.speed_mps*2.23694:5.1f}" if fr.speed_mps is not None else "    -"
        zm = f"{fr.zoom_radius_m:6.0f}" if fr.zoom_radius_m is not None else "     -"
        print(f"{fr.frame:7d} {fr.t_offset_s:7.1f} {road:26} {mph} {zm} {'Y' if fr.has_fix else '.'}")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps([asdict(f) for f in frames]))
        print(f"\nwrote {args.json} ({args.json.stat().st_size/1e6:.1f} MB)")


if __name__ == "__main__":
    main()
