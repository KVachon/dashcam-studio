"""Offline reverse geocoding: position -> city, state.

Point-in-polygon against OSM administrative boundaries, not nearest-town
guessing, so a position just outside city limits reports honestly.

Build the boundary file (once per state pbf):

    osmium tags-filter <state>-latest.osm.pbf \
        r/boundary=administrative w/boundary=administrative \
        -o out/admin_raw.osm.pbf --overwrite
    osmium export out/admin_raw.osm.pbf --geometry-types=polygon \
        --add-unique-id=type_id -o out/admin.geojson --overwrite

`osmium export` assembles multipolygon relations, so the boundaries come out as
real closed areas. Concatenate several states' features into one file for a
multi-state trip.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional, Tuple

from shapely.geometry import Point, shape
from shapely.strtree import STRtree

# admin_level in the US: 4 = state, 6 = county, 8 = city/town/village.
LEVEL_STATE = "4"
LEVEL_COUNTY = "6"
LEVEL_CITY = "8"


class PlaceIndex:
    """Spatial index over admin polygons, one tree per level."""

    def __init__(self, path: Path):
        feats = json.loads(Path(path).read_text())["features"]
        self._levels = {}
        for lvl in (LEVEL_STATE, LEVEL_COUNTY, LEVEL_CITY):
            geoms, props = [], []
            for f in feats:
                if f["properties"].get("admin_level") != lvl:
                    continue
                try:
                    g = shape(f["geometry"])
                except Exception:
                    continue
                if g.is_empty:
                    continue
                geoms.append(g)
                props.append(f["properties"])
            self._levels[lvl] = (STRtree(geoms) if geoms else None, geoms, props)

    def _hit(self, lvl: str, pt: Point) -> Optional[dict]:
        tree, geoms, props = self._levels.get(lvl, (None, [], []))
        if tree is None:
            return None
        for i in tree.query(pt):
            # STRtree query is bbox-only; confirm with a real containment test.
            if geoms[i].contains(pt):
                return props[i]
        return None

    @staticmethod
    def _state_abbr(p: dict) -> Optional[str]:
        ref = (p.get("ref") or "").strip()
        if len(ref) == 2:
            return ref.upper()
        iso = (p.get("ISO3166-2") or "").strip()
        if "-" in iso:
            return iso.split("-")[-1].upper()
        return (p.get("name") or None)

    def lookup(self, lat: float, lon: float) -> Tuple[Optional[str], Optional[str]]:
        """(place, state_abbr). Place falls back to county outside city limits."""
        pt = Point(lon, lat)

        state = None
        s = self._hit(LEVEL_STATE, pt)
        if s:
            state = self._state_abbr(s)

        place = None
        c = self._hit(LEVEL_CITY, pt)
        if c:
            place = c.get("name")
        else:
            co = self._hit(LEVEL_COUNTY, pt)
            if co:
                n = (co.get("name") or "").strip()
                # "Madison County" -> "Madison Co." keeps it short on the HUD
                place = n[:-7].strip() + " Co." if n.endswith(" County") else n or None

        return place, state


_CACHE: dict = {}


def cached_lookup(idx: Optional[PlaceIndex], lat: float, lon: float):
    """Resolve to ~110m granularity; boundaries do not move between frames."""
    if idx is None:
        return None, None
    key = (round(lat, 3), round(lon, 3))
    if key not in _CACHE:
        _CACHE[key] = idx.lookup(lat, lon)
    return _CACHE[key]


def load(path) -> Optional[PlaceIndex]:
    p = Path(path)
    if not p.exists():
        return None
    return PlaceIndex(p)


if __name__ == "__main__":
    import sys

    idx = load(sys.argv[1] if len(sys.argv) > 1 else "out/admin.geojson")
    for lat, lon, label in [
        (34.6033, -86.5839, "home (Madison)"),
        (34.6145, -86.5686, "the parking lot"),
        (34.7304, -86.5861, "downtown Huntsville"),
        (34.4000, -86.9000, "rural, between towns"),
    ]:
        print(f"{label:24} -> {idx.lookup(lat, lon)}")
