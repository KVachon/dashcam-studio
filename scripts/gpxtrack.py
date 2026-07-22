"""GPX parsing and geo helpers for the dashcam pipeline.

Deliberately stdlib-only so it runs on any interpreter with a working expat
(notably /usr/bin/python3). No third-party deps.
"""

from __future__ import annotations

import math
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, List, Optional
from zoneinfo import ZoneInfo

GPX_NS = {"g": "http://www.topografix.com/GPX/1/1"}

# The camera writes local wall-clock time into the filename and nothing else.
# Never hardcode a UTC offset here -- it silently breaks across DST.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from settings import CAMERA_TZ_NAME  # noqa: E402

CAMERA_TZ = ZoneInfo(CAMERA_TZ_NAME)

# Fitcamx: YYYYMMDDHHMMSS_sequence.MP4
FITCAMX_RE = re.compile(r"(?P<stamp>\d{14})(?:_(?P<seq>\d+))?")

EARTH_R_M = 6_371_008.8


# --------------------------------------------------------------------------
# geo
# --------------------------------------------------------------------------


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_M * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing from point 1 to point 2, degrees clockwise from north."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


# --------------------------------------------------------------------------
# model
# --------------------------------------------------------------------------


@dataclass
class TrackPoint:
    t_utc: datetime
    lat: float
    lon: float
    ele: Optional[float] = None

    @property
    def t_local(self) -> datetime:
        return self.t_utc.astimezone(CAMERA_TZ)

    @property
    def epoch(self) -> float:
        return self.t_utc.timestamp()


@dataclass
class Segment:
    """One <trkseg>. Arc emits a new segment after it sleeps at a stop."""

    index: int
    points: List[TrackPoint] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.points)

    @property
    def t_start(self) -> datetime:
        return self.points[0].t_utc

    @property
    def t_end(self) -> datetime:
        return self.points[-1].t_utc

    @property
    def duration_s(self) -> float:
        return (self.t_end - self.t_start).total_seconds()

    def raw_length_m(self) -> float:
        return sum(
            haversine_m(a.lat, a.lon, b.lat, b.lon)
            for a, b in zip(self.points, self.points[1:])
        )


@dataclass
class Waypoint:
    t_utc: datetime
    lat: float
    lon: float
    name: str


@dataclass
class Track:
    segments: List[Segment]
    waypoints: List[Waypoint]

    def all_points(self) -> Iterator[TrackPoint]:
        for seg in self.segments:
            yield from seg.points

    @property
    def t_start(self) -> datetime:
        return self.segments[0].t_start

    @property
    def t_end(self) -> datetime:
        return self.segments[-1].t_end


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def _parse_time(raw: str) -> datetime:
    """Arc writes Zulu times. Normalise to an aware UTC datetime."""
    dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_gpx(path: Path) -> Track:
    root = ET.parse(str(path)).getroot()

    waypoints: List[Waypoint] = []
    for w in root.findall("g:wpt", GPX_NS):
        t = w.findtext("g:time", namespaces=GPX_NS)
        if t is None:
            continue
        waypoints.append(
            Waypoint(
                t_utc=_parse_time(t),
                lat=float(w.get("lat")),
                lon=float(w.get("lon")),
                name=w.findtext("g:name", default="", namespaces=GPX_NS) or "",
            )
        )

    segments: List[Segment] = []
    for i, seg_el in enumerate(root.iter(f"{{{GPX_NS['g']}}}trkseg")):
        pts: List[TrackPoint] = []
        for p in seg_el.findall("g:trkpt", GPX_NS):
            t = p.findtext("g:time", namespaces=GPX_NS)
            if t is None:
                continue
            ele = p.findtext("g:ele", namespaces=GPX_NS)
            pts.append(
                TrackPoint(
                    t_utc=_parse_time(t),
                    lat=float(p.get("lat")),
                    lon=float(p.get("lon")),
                    ele=float(ele) if ele else None,
                )
            )
        if pts:
            pts.sort(key=lambda q: q.t_utc)
            segments.append(Segment(index=i, points=pts))

    if not segments:
        raise ValueError(f"no track points found in {path}")

    segments.sort(key=lambda s: s.t_start)
    return Track(segments=segments, waypoints=waypoints)


# --------------------------------------------------------------------------
# clip timing
# --------------------------------------------------------------------------


def clip_start_utc(filename: str) -> datetime:
    """Decode a Fitcamx filename's local wall-clock stamp into aware UTC.

    Falls back to the caller's problem (raises) rather than guessing -- the
    stitcher's mtime fallback lives at a different layer.
    """
    m = FITCAMX_RE.search(Path(filename).name)
    if not m:
        raise ValueError(f"no 14-digit timestamp in filename: {filename}")
    naive = datetime.strptime(m.group("stamp"), "%Y%m%d%H%M%S")
    # fold=0 picks the first occurrence of an ambiguous DST-fallback hour.
    return naive.replace(tzinfo=CAMERA_TZ).astimezone(timezone.utc)


def describe_clip(path: Path) -> str:
    start = clip_start_utc(path.name)
    return f"{path.name}  start_local={start.astimezone(CAMERA_TZ):%Y-%m-%d %H:%M:%S %Z}  start_utc={start:%H:%M:%S}Z"
