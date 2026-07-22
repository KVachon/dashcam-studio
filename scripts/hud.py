"""HUD overlay renderer -- the drawing layer.

Reads a frame record from the data layer and draws a transparent overlay:
a heading-up map inset, the current road bold white, surrounding roads thin
grey, a position indicator, and minimal type.

Everything visual is a constant at the top of this file. The data layer below
it does not care what any of this looks like.

Preview a single frame composited over real footage:

    .venv/bin/python scripts/hud.py --frames out/frames.json \
        --roads out/roads.geojson --clip 20260721102148_2026073.MP4 \
        --at 150 --out out/preview/hud_150.png
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import cairo

sys.path.insert(0, str(Path(__file__).resolve().parent))

from settings import hud_font  # noqa: E402

# ---------------------------------------------------------------- geometry --

W, H = 1920, 1080

# One knob for the whole HUD. Every dimension below derives from it, so
# resizing is a single edit rather than forty. Override without editing:
#     HUD_SCALE=0.625 .venv/bin/python scripts/render.py ...
SCALE = float(os.environ.get("HUD_SCALE", "0.75"))

# Orientation. North-up keeps the map steady and rotates the marker instead;
# heading-up rotates the world so travel direction is always up.
NORTH_UP = True

# Map inset sits over the car hood: dark, static, obscures nothing. Anchored to
# the bottom-left corner rather than a fixed centre, so changing SCALE grows the
# disc into frame instead of drifting it off the hood.
MAP_LEFT, MAP_BOTTOM = 80.0, 998.0
MAP_R = 140.0 * SCALE
MAP_CX = MAP_LEFT + MAP_R
MAP_CY = MAP_BOTTOM - MAP_R

# --------------------------------------------------------------------- type --

FONT = hud_font()  # geometric; Futura (mac) / Bahnschrift (win), HUD_FONT to override
LABEL_SIZE = 27.0 * SCALE
LABEL_TRACKING = 3.4 * SCALE
DIST_SIZE = 52.0 * SCALE
UNIT_SIZE = 20.0 * SCALE
META_SIZE = 16.0 * SCALE
META_TRACKING = 2.6 * SCALE
TIME_SIZE = 30.0 * SCALE
CITY_SIZE = 22.0 * SCALE
CITY_TRACKING = 3.0 * SCALE
CITY_A = 1.0   # full white; hierarchy comes from size and weight, not opacity

TEXT_X = MAP_CX + MAP_R + 42.0 * SCALE

# ------------------------------------------------------------------- colour --

WHITE = (1.0, 1.0, 1.0)
ACCENT = (0.62, 0.90, 1.00)  # restrained cyan, position indicator only

# The travelled route -- the HUD's only highlight.
TRAIL_A = 0.97
TRAIL_W = 4.2 * SCALE
CUR_GLOW_A = 0.13
CUR_GLOW_W = 13.0 * SCALE

# Weight of the whole secondary network.
ROAD_W_MULT = 2.0

# Nothing grey may reach the travelled-route highlight's weight. Without this
# cap, ROAD_W_MULT=2 makes motorways (2.8px) wider than the trail (2.1px) and
# inverts the hierarchy -- invisible around Madison, obvious on an interstate.
ROAD_W_CAP = 0.85 * TRAIL_W

# Surrounding network, by prominence: (alpha, width before scaling).
ROAD_STYLE = {
    k: (a, min(w * SCALE * ROAD_W_MULT, ROAD_W_CAP))
    for k, (a, w) in {
        "motorway": (0.80, 2.8),
        "trunk": (0.76, 2.6),
        "primary": (0.72, 2.3),
        "secondary": (0.66, 2.1),
        "tertiary": (0.62, 1.9),
        "unclassified": (0.52, 1.6),
        "residential": (0.52, 1.6),
        "service": (0.30, 1.1),
        "track": (0.24, 1.0),
    }.items()
}
SKIP_HIGHWAY = {"footway", "path", "cycleway", "steps", "bridleway", "corridor"}

# Legibility without a background box: dark under-stroke behind every line.
# Fixed, NOT scaled by the line's alpha -- faint lines need contrast most.
# With the scrim off this is the ONLY thing holding hairlines against sun.
SHADOW_A = 0.50
SHADOW_EXTRA = 3.0 * SCALE

# Two independent backgrounds, both soft-edged -- a hard-edged box would read
# as a UI panel bolted onto the footage rather than a HUD floating in it.
#   HUD_SCRIM: radial wash behind the map disc only.
#   HUD_PANEL: broad elliptical wash behind the whole block, text included.
# Both default off; override without editing, same as HUD_SCALE.
# Invisible against a dark hood, but insurance for night footage, a lighter
# car, or scaling the disc up into sunlit pavement.
SCRIM_A = float(os.environ.get("HUD_SCRIM", "0.35"))
PANEL_A = float(os.environ.get("HUD_PANEL", "0.0"))

RING_A = 0.34
EDGE_FADE = 0.14  # fraction of radius over which roads fade out

# Frames between retained trail points. Small enough to stay smooth at speed,
# large enough that the whole-drive trail stays cheap to draw.
TRAIL_DECIMATE = 5

M_PER_DEG_LAT = 111_320.0
MI = 1609.34


# --------------------------------------------------------------------------
# road data
# --------------------------------------------------------------------------


class RoadNet:
    """Local road geometry with a cheap bbox prefilter."""

    def __init__(self, path: Path):
        g = json.loads(path.read_text())
        self.feats = []
        for f in g["features"]:
            hw = f["properties"].get("highway")
            if hw in SKIP_HIGHWAY:
                continue
            coords = f["geometry"]["coordinates"]
            if len(coords) < 2:
                continue
            fid = str(f.get("id", ""))
            way_id = int(fid[1:]) if fid.startswith("w") and fid[1:].isdigit() else None
            lons = [c[0] for c in coords]
            lats = [c[1] for c in coords]
            self.feats.append(
                {
                    "way_id": way_id,
                    "highway": hw,
                    "name": f["properties"].get("name", ""),
                    "coords": coords,
                    "bbox": (min(lons), min(lats), max(lons), max(lats)),
                }
            )

    def near(self, lat: float, lon: float, radius_m: float) -> List[dict]:
        dlat = radius_m / M_PER_DEG_LAT
        dlon = radius_m / (M_PER_DEG_LAT * max(math.cos(math.radians(lat)), 1e-6))
        lo_x, lo_y, hi_x, hi_y = lon - dlon, lat - dlat, lon + dlon, lat + dlat
        return [
            f
            for f in self.feats
            if not (
                f["bbox"][2] < lo_x
                or f["bbox"][0] > hi_x
                or f["bbox"][3] < lo_y
                or f["bbox"][1] > hi_y
            )
        ]


# --------------------------------------------------------------------------
# projection
# --------------------------------------------------------------------------


def projector(lat0: float, lon0: float, heading_deg: float, scale: float):
    """Local equirectangular projection, rotated so travel direction is up."""
    mlat = M_PER_DEG_LAT
    mlon = M_PER_DEG_LAT * math.cos(math.radians(lat0))
    h = math.radians(heading_deg or 0.0)
    ch, sh = math.cos(h), math.sin(h)

    def to_screen(lon: float, lat: float) -> Tuple[float, float]:
        xe = (lon - lon0) * mlon  # metres east
        yn = (lat - lat0) * mlat  # metres north
        xr = xe * ch - yn * sh
        yr = xe * sh + yn * ch
        return MAP_CX + xr * scale, MAP_CY - yr * scale

    return to_screen


# --------------------------------------------------------------------------
# drawing helpers
# --------------------------------------------------------------------------


def stroke_path(ctx, pts: Sequence[Tuple[float, float]]) -> None:
    ctx.move_to(*pts[0])
    for p in pts[1:]:
        ctx.line_to(*p)


def draw_line(ctx, pts, rgb, alpha, width, shadow=True) -> None:
    if len(pts) < 2:
        return
    ctx.set_line_cap(cairo.LINE_CAP_ROUND)
    ctx.set_line_join(cairo.LINE_JOIN_ROUND)
    if shadow:
        stroke_path(ctx, pts)
        ctx.set_source_rgba(0, 0, 0, SHADOW_A)
        ctx.set_line_width(width + SHADOW_EXTRA)
        ctx.stroke()
    stroke_path(ctx, pts)
    ctx.set_source_rgba(*rgb, alpha)
    ctx.set_line_width(width)
    ctx.stroke()


def text_tracked(ctx, x, y, s, size, tracking, rgb, alpha, bold=False, shadow=True):
    """Letterspaced text -- cairo's toy API has no tracking."""
    ctx.select_font_face(
        FONT,
        cairo.FONT_SLANT_NORMAL,
        cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL,
    )
    ctx.set_font_size(size)
    cx = x
    for ch in s:
        if shadow:
            ctx.move_to(cx + 1.4 * SCALE, y + 1.4 * SCALE)
            ctx.set_source_rgba(0, 0, 0, 0.62 * alpha)
            ctx.show_text(ch)
        ctx.move_to(cx, y)
        ctx.set_source_rgba(*rgb, alpha)
        ctx.show_text(ch)
        cx += ctx.text_extents(ch).x_advance + tracking
    return cx - x


def text_width(ctx, s, size, tracking, bold=False) -> float:
    ctx.select_font_face(
        FONT,
        cairo.FONT_SLANT_NORMAL,
        cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL,
    )
    ctx.set_font_size(size)
    return sum(ctx.text_extents(c).x_advance + tracking for c in s) - tracking


# --------------------------------------------------------------------------
# the HUD
# --------------------------------------------------------------------------


def render(
    rec: dict,
    net: RoadNet,
    trail: Optional[List[Tuple[float, float]]] = None,
) -> cairo.ImageSurface:
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    ctx = cairo.Context(surf)

    has_fix = rec.get("has_fix") and rec.get("lat") is not None
    dim = 1.0 if has_fix else 0.35

    # ---- panel: broad wash under the whole HUD, feathered to nothing ----
    if PANEL_A > 0:
        ctx.save()
        ctx.translate(MAP_CX + 150.0 * SCALE, MAP_CY)
        ctx.scale(420.0 * SCALE, MAP_R * 1.55)
        g = cairo.RadialGradient(0, 0, 0, 0, 0, 1)
        g.add_color_stop_rgba(0.00, 0, 0, 0, PANEL_A)
        g.add_color_stop_rgba(0.55, 0, 0, 0, PANEL_A * 0.72)
        g.add_color_stop_rgba(1.00, 0, 0, 0, 0.0)
        ctx.set_source(g)
        ctx.arc(0, 0, 1, 0, 2 * math.pi)
        ctx.fill()
        ctx.restore()

    # ---- map inset ----
    ctx.save()
    ctx.arc(MAP_CX, MAP_CY, MAP_R, 0, 2 * math.pi)
    ctx.clip()

    if SCRIM_A > 0:
        # Soft scrim, densest at the centre, gone by the rim -- no hard edge.
        scrim = cairo.RadialGradient(MAP_CX, MAP_CY, 0, MAP_CX, MAP_CY, MAP_R)
        scrim.add_color_stop_rgba(0.0, 0, 0, 0, SCRIM_A)
        scrim.add_color_stop_rgba(0.62, 0, 0, 0, SCRIM_A * 0.80)
        scrim.add_color_stop_rgba(1.0, 0, 0, 0, 0.0)
        ctx.set_source(scrim)
        ctx.paint()

    if has_fix:
        radius_m = rec.get("zoom_radius_m") or 200.0
        scale = MAP_R / radius_m
        view_h = 0.0 if NORTH_UP else (rec.get("heading_deg") or 0.0)
        to_screen = projector(rec["lat"], rec["lon"], view_h, scale)

        near = net.near(rec["lat"], rec["lon"], radius_m * 1.5)

        # The whole network is grey. Nothing is special-cased by way id --
        # highlighting a whole OSM way would light up road we have not driven.
        for f in near:
            a, w = ROAD_STYLE.get(
                f["highway"], (0.18, min(1.1 * SCALE * ROAD_W_MULT, ROAD_W_CAP))
            )
            pts = [to_screen(c[0], c[1]) for c in f["coords"]]
            # No under-stroke on the secondary network: outlining every grey
            # hairline reads as clutter at this size.
            draw_line(ctx, pts, WHITE, a * dim, w, shadow=False)

        # The travelled route is the only highlight, and it persists for the
        # whole drive: roads already behind us stay lit, road ahead never is.
        # `trail` is a list of runs so a GPS gap breaks the line instead of
        # drawing a straight cheat across it.
        for run in trail or []:
            if len(run) < 2:
                continue
            pts = [to_screen(lon, lat) for lat, lon in run]
            draw_line(ctx, pts, WHITE, CUR_GLOW_A * dim, CUR_GLOW_W, shadow=False)
            draw_line(ctx, pts, WHITE, TRAIL_A * dim, TRAIL_W)

    # Fade the network out at the rim instead of hard-clipping it.
    g = cairo.RadialGradient(
        MAP_CX, MAP_CY, MAP_R * (1.0 - EDGE_FADE), MAP_CX, MAP_CY, MAP_R
    )
    g.add_color_stop_rgba(0, 0, 0, 0, 0)
    g.add_color_stop_rgba(1, 0, 0, 0, 0)
    ctx.set_operator(cairo.OPERATOR_DEST_OUT)
    g2 = cairo.RadialGradient(
        MAP_CX, MAP_CY, MAP_R * (1.0 - EDGE_FADE), MAP_CX, MAP_CY, MAP_R
    )
    g2.add_color_stop_rgba(0, 0, 0, 0, 0.0)
    g2.add_color_stop_rgba(1, 0, 0, 0, 1.0)
    ctx.set_source(g2)
    ctx.paint()
    ctx.set_operator(cairo.OPERATOR_OVER)
    ctx.restore()

    # No rim and no cardinal ticks -- the roads simply fade out at the edge.
    # (Ticks only read as deliberate when they sit on a ring; floating, they
    # look like stray marks. Restore both together if the disc needs an edge.)

    # ---- position indicator ----
    if has_fix:
        ctx.save()
        ctx.translate(MAP_CX, MAP_CY)
        # North-up: the marker carries the heading instead of the map.
        if NORTH_UP and rec.get("heading_deg") is not None:
            ctx.rotate(math.radians(rec["heading_deg"]))
        s = SCALE
        ctx.move_to(0, -11.5 * s)
        ctx.line_to(8.0 * s, 8.5 * s)
        ctx.line_to(0, 4.2 * s)
        ctx.line_to(-8.0 * s, 8.5 * s)
        ctx.close_path()
        ctx.set_source_rgba(0, 0, 0, 0.6)
        ctx.set_line_width(4.0 * s)
        ctx.stroke_preserve()
        ctx.set_source_rgba(*ACCENT, 1.0)
        ctx.fill()
        ctx.restore()
    else:
        ctx.arc(MAP_CX, MAP_CY, 6.0 * SCALE, 0, 2 * math.pi)
        ctx.set_source_rgba(*WHITE, 0.30)
        ctx.fill()

    # ---- type ----
    if has_fix:
        ref = (rec.get("road_ref") or "").strip()
        name = (rec.get("road_name") or "").strip()
        label = (ref or name or "UNNAMED ROAD").upper()
    else:
        label = "ACQUIRING GPS"

    # City/state sits above the road as a context header. Full white: at this
    # size, dimming it just made it unreadable. The road name still dominates
    # because it is larger and bold.
    where = ", ".join(x for x in ((rec.get("place") or ""), (rec.get("state") or "")) if x)
    if has_fix and where:
        text_tracked(
            ctx, TEXT_X, MAP_CY - 46.0 * SCALE, where.upper(), CITY_SIZE,
            CITY_TRACKING, WHITE, CITY_A,
        )

    text_tracked(
        ctx, TEXT_X, MAP_CY - 18.0 * SCALE, label, LABEL_SIZE, LABEL_TRACKING,
        WHITE, 1.0 if has_fix else 0.55, bold=True,
    )

    # Secondary line: the street name when a shield-style ref took the headline.
    ref = (rec.get("road_ref") or "").strip()
    name = (rec.get("road_name") or "").strip()
    if has_fix and ref and name:
        text_tracked(
            ctx, TEXT_X, MAP_CY + 8.0 * SCALE, name.upper(), META_SIZE,
            META_TRACKING, WHITE, 0.55,
        )

    dist_mi = (rec.get("cum_dist_m") or 0.0) / MI
    w = text_tracked(
        ctx, TEXT_X, MAP_CY + 62.0 * SCALE, f"{dist_mi:.2f}", DIST_SIZE,
        1.0 * SCALE, WHITE, 0.95 if has_fix else 0.4, bold=False,
    )
    w += text_tracked(
        ctx, TEXT_X + w + 10.0 * SCALE, MAP_CY + 62.0 * SCALE, "MI", UNIT_SIZE,
        2.0 * SCALE, WHITE, 0.55 if has_fix else 0.3,
    ) + 10.0 * SCALE

    # Wall-clock at our actual position, not the camera's fixed zone -- so the
    # readout steps an hour when the drive crosses into Eastern. The zone
    # abbreviation is shown so that jump reads as correct, not broken.
    t_disp = rec.get("t_display") or ""
    if t_disp:
        x = TEXT_X + w + 26.0 * SCALE
        tw = text_tracked(
            ctx, x, MAP_CY + 62.0 * SCALE, t_disp, TIME_SIZE, 1.0 * SCALE,
            WHITE, 0.80 if has_fix else 0.4,
        )
        abbr = rec.get("tz_abbr") or ""
        if abbr:
            text_tracked(
                ctx, x + tw + 8.0 * SCALE, MAP_CY + 62.0 * SCALE, abbr,
                UNIT_SIZE, 2.0 * SCALE, WHITE, 0.50 if has_fix else 0.3,
            )

    return surf


# --------------------------------------------------------------------------
# preview
# --------------------------------------------------------------------------


def grab_frame(clip: Path, t: float) -> cairo.ImageSurface:
    """Pull one video frame as a cairo surface via ffmpeg -> PNG."""
    tmp = Path("out/preview/_grab.png")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(clip),
         "-frames:v", "1", "-y", str(tmp)],
        check=True,
    )
    return cairo.ImageSurface.create_from_png(str(tmp))


def build_trail(frames: List[dict], upto: int, fps: float = 30.0) -> List[List[Tuple[float, float]]]:
    """Everywhere driven up to `upto`, as runs split on loss of fix.

    Whole-drive history, deliberately unbounded: roads already behind us stay
    highlighted. O(n) per call, which is fine for stills -- render.py uses an
    indexed version for the per-frame path.
    """
    runs: List[List[Tuple[float, float]]] = []
    cur: List[Tuple[float, float]] = []
    for i, f in enumerate(frames[: upto + 1]):
        if i % TRAIL_DECIMATE and i != upto:
            continue
        if f.get("has_fix") and f.get("lat") is not None:
            cur.append((f["lat"], f["lon"]))
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    return runs


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--frames", type=Path, required=True)
    ap.add_argument("--roads", type=Path, required=True)
    ap.add_argument("--clip", type=Path)
    ap.add_argument("--at", type=float, action="append", required=True,
                    help="seconds into the clip; repeatable")
    ap.add_argument("--fps", type=float, default=30.0)
    ap.add_argument("--outdir", type=Path, default=Path("out/preview"))
    args = ap.parse_args()

    frames = json.loads(args.frames.read_text())
    net = RoadNet(args.roads)
    args.outdir.mkdir(parents=True, exist_ok=True)

    for t in args.at:
        i = min(int(round(t * args.fps)), len(frames) - 1)
        rec = frames[i]
        hud = render(rec, net, trail=build_trail(frames, i, fps=args.fps))

        out = args.outdir / f"hud_{int(t):04d}s.png"
        if args.clip:
            base = grab_frame(args.clip, t)
            ctx = cairo.Context(base)
            ctx.set_source_surface(hud, 0, 0)
            ctx.paint()
            base.write_to_png(str(out))
        else:
            hud.write_to_png(str(out))

        road = rec.get("road_name") or rec.get("road_ref") or "—"
        print(f"t={t:6.1f}s frame={i:5d}  {road[:28]:28} "
              f"fix={'Y' if rec.get('has_fix') else '.'}  "
              f"zoom={rec.get('zoom_radius_m')}  -> {out}")


if __name__ == "__main__":
    main()
