"""Drive summary: compute stats, render a card, optionally append it as an
end-slate to a rendered drive video.

    python summary.py --frames frames.json --events events.json --card card.png
    python summary.py ... --card card.png --video drive.mp4 --out drive_final.mp4
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter, OrderedDict
from pathlib import Path

import cairo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from settings import encoder_args, hud_font  # noqa: E402

FONT = hud_font()
MI = 1609.34
CY = (0.62, 0.90, 1.0)
WH = (0.92, 0.95, 0.98)
DIM = (0.55, 0.60, 0.68)
BARS = [(0.62, 0.90, 1.0), (0.5, 0.75, 0.9), (0.4, 0.6, 0.75),
        (0.35, 0.5, 0.62), (0.3, 0.4, 0.5)]


def stats(frames, events, fps=30.0) -> dict:
    fix = [r for r in frames if r.get("has_fix")]
    if not fix:
        return {}
    dist = max((r["cum_dist_m"] or 0) for r in fix) / MI
    elapsed = len(frames) / fps
    moving = sum(1 for r in fix if (r.get("speed_mps") or 0) > 1.0) / fps
    spd = [(r.get("speed_mps") or 0) * 2.23694 for r in fix]
    mv = [s for s in spd if s > 1]
    places = list(OrderedDict.fromkeys(
        (r["place"], r["state"]) for r in fix if r.get("place")))
    rc, roads, prev = Counter(), Counter(), None
    for r in fix:
        if prev and r.get("cum_dist_m") is not None and prev.get("cum_dist_m") is not None:
            d = r["cum_dist_m"] - prev["cum_dist_m"]
            rc[r.get("road_class") or "other"] += d
            nm = r.get("road_name") or r.get("road_ref")
            if nm:
                roads[nm] += d
        prev = r
    return {
        "dist": dist, "elapsed": elapsed, "moving": moving,
        "avg": sum(mv) / max(1, len(mv)), "top": max(spd) if spd else 0,
        "places": places, "n_events": len(events),
        "t0": fix[0].get("t_display", ""), "t1": fix[-1].get("t_display", ""),
        "road_class": rc.most_common(5), "top_roads": roads.most_common(4),
    }


def render_card(st: dict, out_png: Path, W=940, H=690):
    s = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    c = cairo.Context(s)
    c.set_source_rgb(0.05, 0.06, 0.08)
    c.paint()

    def txt(x, y, t, sz, rgb, tr=0, bold=False):
        c.select_font_face(FONT, 0, cairo.FONT_WEIGHT_BOLD if bold else 0)
        c.set_font_size(sz)
        cx = x
        for ch in str(t):
            c.move_to(cx, y)
            c.set_source_rgb(*rgb)
            c.show_text(ch)
            cx += c.text_extents(ch).x_advance + tr
        return cx - x

    pl = st["places"]
    route = f"{pl[0][0]} → {pl[-1][0]}" if len(pl) > 1 else (pl[0][0] if pl else "Drive")
    state = pl[0][1] if pl else ""
    txt(40, 58, "DRIVE SUMMARY", 20, DIM, 4, True)
    txt(40, 100, f"{route}, {state}".rstrip(", "), 30, WH, 1, True)
    txt(40, 128, f"{st['t0']} – {st['t1']}", 16, DIM, 1)
    c.set_source_rgba(1, 1, 1, 0.1)
    c.rectangle(40, 150, W - 80, 1)
    c.fill()

    def tile(x, y, val, unit, lbl):
        w = txt(x, y, val, 52, WH, 0, True)
        txt(x + w + 8, y, unit, 20, DIM, 1)
        txt(x, y + 26, lbl, 14, DIM, 2, True)

    e, mv = st["elapsed"], st["moving"]
    tile(40, 220, f"{st['dist']:.1f}", "MI", "DISTANCE")
    tile(280, 220, f"{e/60:.0f}", "MIN", "ELAPSED")
    tile(480, 220, f"{st['avg']:.0f}", "MPH", "AVG SPEED")
    tile(700, 220, f"{st['top']:.0f}", "MPH", "TOP SPEED")
    tile(40, 320, f"{mv/60:.0f}", "MIN", "MOVING")
    tile(280, 320, f"{max(0,(e-mv))/60:.0f}", "MIN", "STOPPED")
    tile(480, 320, f"{st['n_events']}", "", "LANDMARKS")
    tile(700, 320, f"{len(pl)}", "", "AREAS")

    rc = st["road_class"]
    tot = sum(v for _, v in rc) or 1
    y = 400
    txt(40, y, "ROAD TYPES", 14, DIM, 2, True)
    x = 40
    for i, (k, v) in enumerate(rc[:5]):
        seg = (W - 80) * v / tot
        c.set_source_rgb(*BARS[i % 5])
        c.rectangle(x, y + 14, max(2, seg - 3), 18)
        c.fill()
        x += seg
    lx = 40
    for i, (k, v) in enumerate(rc[:4]):
        c.set_source_rgb(*BARS[i % 5])
        c.rectangle(lx, y + 46, 11, 11)
        c.fill()
        lx += txt(lx + 18, y + 56, f"{k} {v/tot*100:.0f}%", 14, DIM, 0) + 28

    y = 520
    txt(40, y, "TOP ROADS", 14, DIM, 2, True)
    for i, (nm, v) in enumerate(st["top_roads"]):
        txt(40, y + 28 + i * 22, nm, 15, WH, 0)
        txt(560, y + 28 + i * 22, f"{v/MI:.2f} mi", 15, CY, 0)
    s.write_to_png(str(out_png))
    return out_png


def append_slate(video: Path, card: Path, out: Path, secs=4.0):
    """Re-encode: drive video + a fade-in card slate. Hardware-encoded, so the
    extra pass is quick; concat-filter is used so any format mismatch is moot."""
    # probe the video geometry so the slate matches
    probe = subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(video)]
    ).decode().strip()
    w, h = probe.split("x")
    cmd = ["ffmpeg", "-v", "error", "-stats", "-y", "-i", str(video),
           "-loop", "1", "-t", str(secs), "-i", str(card),
           "-filter_complex",
           # scale to ~80% then pad to full frame -> even margin around the card
           f"[1:v]scale={int(int(w)*0.8)}:{int(int(h)*0.8)}:force_original_aspect_ratio=decrease,"
           f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:0x0D0F14,setsar=1,fps=30,"
           f"fade=t=in:st=0:d=0.5[s];[0:v][s]concat=n=2:v=1[v]"]
    cmd += ["-map", "[v]", "-map", "0:a?"] + encoder_args("12M") + [str(out)]
    subprocess.run(cmd, check=True)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--frames", type=Path, required=True)
    ap.add_argument("--events", type=Path)
    ap.add_argument("--card", type=Path, required=True)
    ap.add_argument("--video", type=Path)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    frames = json.loads(args.frames.read_text())
    events = json.loads(args.events.read_text()) if args.events and args.events.exists() else []
    st = stats(frames, events)
    render_card(st, args.card)
    print(f"card -> {args.card}  ({st.get('dist',0):.1f}mi)")
    if args.video and args.out:
        append_slate(args.video, args.card, args.out)
        print(f"slate -> {args.out}")


if __name__ == "__main__":
    main()
