"""Map-match a GPX track against a local Valhalla instance.

Session goal: prove we can recover the road actually driven -- name, ref and
class -- for every GPS fix, entirely offline.

Usage:
    python3 scripts/mapmatch.py 2026-07-21.gpx
    python3 scripts/mapmatch.py 2026-07-21.gpx --json out/matched.json

Stdlib only (urllib, not requests) so it runs on the system interpreter.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gpxtrack import CAMERA_TZ, Segment, Track, bearing_deg, haversine_m, load_gpx

# 127.0.0.1, not "localhost": this box resolves localhost to ::1 first and the
# IPv6 loopback path returns EADDRNOTAVAIL instead of a clean refusal.
DEFAULT_URL = "http://127.0.0.1:8002"

# Valhalla marks an unmatched point with uint64 max.
UNMATCHED = 18446744073709551615

# Route designations that belong in road_ref rather than road_name.
# e.g. "I 565", "US 72", "AL 20", "CR-47"
REF_RE = re.compile(
    r"^(?:I|US|SR|AL|CR|CO|FM|RM|A|M|B)[-\s]?\d+[A-Z]?$", re.IGNORECASE
)

ATTRIBUTES = [
    "edge.names",
    "edge.road_class",
    "edge.use",
    "edge.way_id",
    "edge.length",
    "edge.speed",
    "matched.point",
    "matched.type",
    "matched.edge_index",
    "matched.distance_from_trace_point",
]


@dataclass
class MatchedPoint:
    """One record of the data-layer contract (road fields populated here)."""

    t_utc: str
    t_local: str
    lat: float
    lon: float
    road_name: str
    road_ref: str
    road_class: str
    way_id: Optional[int]
    heading_deg: Optional[float]
    cum_dist_m: float
    match_type: str
    off_trace_m: Optional[float]
    has_fix: bool = True


def split_names(names: List[str]) -> tuple:
    """Separate route refs from street names.

    Valhalla returns both in edge.names; the HUD wants them in different slots
    (a shield vs a label).
    """
    refs, plain = [], []
    for n in names or []:
        (refs if REF_RE.match(n.strip()) else plain).append(n.strip())
    return (plain[0] if plain else ""), (refs[0] if refs else "")


def build_request(seg: Segment, costing: str = "auto") -> Dict[str, Any]:
    shape = []
    for i, p in enumerate(seg.points):
        shape.append(
            {
                "lat": p.lat,
                "lon": p.lon,
                "time": p.epoch,
                "type": "break" if i in (0, len(seg.points) - 1) else "via",
            }
        )
    return {
        "shape": shape,
        "costing": costing,
        "shape_match": "map_snap",
        "filters": {"attributes": ATTRIBUTES, "action": "include"},
    }


def call_valhalla(payload: Dict[str, Any], url: str) -> Dict[str, Any]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{url.rstrip('/')}/trace_attributes",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:600]
        raise SystemExit(f"Valhalla HTTP {e.code}: {body}") from None
    except urllib.error.URLError as e:
        raise SystemExit(
            f"Cannot reach Valhalla at {url} ({e.reason}).\n"
            "Is the container running?  docker ps"
        ) from None


def match_segment(
    seg: Segment, url: str, cum_start_m: float = 0.0
) -> List[MatchedPoint]:
    resp = call_valhalla(build_request(seg), url)
    edges = resp.get("edges", []) or []
    mps = resp.get("matched_points", []) or []

    out: List[MatchedPoint] = []
    cum = cum_start_m
    prev = None

    for i, p in enumerate(seg.points):
        mp = mps[i] if i < len(mps) else {}
        idx = mp.get("edge_index", UNMATCHED)
        edge = edges[idx] if (idx != UNMATCHED and idx < len(edges)) else {}

        name, ref = split_names(edge.get("names", []))

        # Snapped position when Valhalla gives us one, else the raw fix.
        lat = mp.get("lat", p.lat)
        lon = mp.get("lon", p.lon)

        if prev is not None:
            step = haversine_m(prev[0], prev[1], lat, lon)
            # Gate out GPS jitter while stationary so the tally doesn't creep.
            if step >= 1.0:
                cum += step
                heading = bearing_deg(prev[0], prev[1], lat, lon)
            else:
                heading = out[-1].heading_deg if out else None
        else:
            heading = None

        out.append(
            MatchedPoint(
                t_utc=p.t_utc.isoformat(),
                t_local=p.t_local.strftime("%Y-%m-%d %H:%M:%S"),
                lat=lat,
                lon=lon,
                road_name=name,
                road_ref=ref,
                road_class=edge.get("road_class", ""),
                way_id=edge.get("way_id"),
                heading_deg=round(heading, 1) if heading is not None else None,
                cum_dist_m=round(cum, 1),
                match_type=mp.get("type", "unmatched"),
                off_trace_m=(
                    round(mp["distance_from_trace_point"], 1)
                    if "distance_from_trace_point" in mp
                    else None
                ),
            )
        )
        prev = (lat, lon)

    return out


def road_label(m: MatchedPoint) -> str:
    if m.road_ref and m.road_name:
        return f"{m.road_ref} ({m.road_name})"
    return m.road_ref or m.road_name or "—"


def print_stream(points: List[MatchedPoint]) -> None:
    print(f"{'LOCAL':10} {'ROAD':34} {'CLASS':12} {'MI':>6} {'HDG':>5} {'OFF':>5}")
    print("-" * 78)
    for m in points:
        hdg = f"{m.heading_deg:5.0f}" if m.heading_deg is not None else "    -"
        off = f"{m.off_trace_m:5.1f}" if m.off_trace_m is not None else "    -"
        print(
            f"{m.t_local[11:]:10} {road_label(m)[:34]:34} {m.road_class[:12]:12} "
            f"{m.cum_dist_m/1609.34:6.2f} {hdg} {off}"
        )


def print_transitions(points: List[MatchedPoint]) -> None:
    """Collapse to one line per road change -- the readable version."""
    print(f"\n{'LOCAL':10} {'ROAD':38} {'CLASS':12} {'FOR':>7}")
    print("-" * 72)
    run_start = 0
    for i in range(1, len(points) + 1):
        changed = i == len(points) or (
            road_label(points[i]) != road_label(points[run_start])
        )
        if changed:
            a, b = points[run_start], points[i - 1]
            dist = (b.cum_dist_m - a.cum_dist_m) / 1609.34
            print(
                f"{a.t_local[11:]:10} {road_label(a)[:38]:38} "
                f"{a.road_class[:12]:12} {dist:6.2f}mi"
            )
            run_start = i


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("gpx", type=Path)
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--json", type=Path, help="write full record stream here")
    ap.add_argument("--all", action="store_true", help="print every point")
    args = ap.parse_args()

    track: Track = load_gpx(args.gpx)
    print(
        f"{args.gpx.name}: {len(track.segments)} segment(s), "
        f"{sum(len(s) for s in track.segments)} points, "
        f"{track.t_start.astimezone(CAMERA_TZ):%H:%M:%S}"
        f" -> {track.t_end.astimezone(CAMERA_TZ):%H:%M:%S} local\n"
    )

    everything: List[MatchedPoint] = []
    cum = 0.0
    for seg in track.segments:
        print(
            f"=== segment {seg.index}: {len(seg)} pts, "
            f"{seg.t_start.astimezone(CAMERA_TZ):%H:%M:%S}"
            f"-{seg.t_end.astimezone(CAMERA_TZ):%H:%M:%S} ==="
        )
        pts = match_segment(seg, args.url, cum_start_m=cum)
        cum = pts[-1].cum_dist_m
        if args.all:
            print_stream(pts)
        print_transitions(pts)
        everything.extend(pts)
        print()

    matched = sum(1 for m in everything if m.road_class)
    print(
        f"matched {matched}/{len(everything)} points "
        f"({matched/len(everything)*100:.1f}%), "
        f"total {cum/1609.34:.2f} mi"
    )

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps([asdict(m) for m in everything], indent=2))
        print(f"wrote {args.json}")


if __name__ == "__main__":
    main()
