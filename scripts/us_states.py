"""Coarse US-state lookup for naming map-data gaps and building Geofabrik links.

Bounding boxes are approximate (lat_min, lat_max, lon_min, lon_max) -- enough to
answer "which state extracts does this GPX need?", not for geocoding. When a
point lands in several overlapping boxes, the smallest (most specific) wins.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

# code: (name, lat_min, lat_max, lon_min, lon_max)  -- lower 48 + DC
BOXES = {
    "AL": ("Alabama", 30.2, 35.0, -88.5, -84.9),
    "AZ": ("Arizona", 31.3, 37.0, -114.8, -109.0),
    "AR": ("Arkansas", 33.0, 36.5, -94.6, -89.6),
    "CA": ("California", 32.5, 42.0, -124.4, -114.1),
    "CO": ("Colorado", 37.0, 41.0, -109.1, -102.0),
    "CT": ("Connecticut", 40.9, 42.1, -73.7, -71.8),
    "DE": ("Delaware", 38.4, 39.8, -75.8, -75.0),
    "DC": ("District of Columbia", 38.8, 39.0, -77.1, -76.9),
    "FL": ("Florida", 24.4, 31.0, -87.6, -80.0),
    "GA": ("Georgia", 30.4, 35.0, -85.6, -80.8),
    "ID": ("Idaho", 42.0, 49.0, -117.2, -111.0),
    "IL": ("Illinois", 36.9, 42.5, -91.5, -87.0),
    "IN": ("Indiana", 37.8, 41.8, -88.1, -84.8),
    "IA": ("Iowa", 40.4, 43.5, -96.6, -90.1),
    "KS": ("Kansas", 37.0, 40.0, -102.1, -94.6),
    "KY": ("Kentucky", 36.5, 39.1, -89.6, -81.9),
    "LA": ("Louisiana", 28.9, 33.0, -94.0, -88.8),
    "ME": ("Maine", 43.0, 47.5, -71.1, -66.9),
    "MD": ("Maryland", 37.9, 39.7, -79.5, -75.0),
    "MA": ("Massachusetts", 41.2, 42.9, -73.5, -69.9),
    "MI": ("Michigan", 41.7, 48.3, -90.4, -82.4),
    "MN": ("Minnesota", 43.5, 49.4, -97.2, -89.5),
    "MS": ("Mississippi", 30.1, 35.0, -91.7, -88.1),
    "MO": ("Missouri", 36.0, 40.6, -95.8, -89.1),
    "MT": ("Montana", 44.4, 49.0, -116.1, -104.0),
    "NE": ("Nebraska", 40.0, 43.0, -104.1, -95.3),
    "NV": ("Nevada", 35.0, 42.0, -120.0, -114.0),
    "NH": ("New Hampshire", 42.7, 45.3, -72.6, -70.6),
    "NJ": ("New Jersey", 38.9, 41.4, -75.6, -73.9),
    "NM": ("New Mexico", 31.3, 37.0, -109.1, -103.0),
    "NY": ("New York", 40.5, 45.0, -79.8, -71.9),
    "NC": ("North Carolina", 33.8, 36.6, -84.3, -75.5),
    "ND": ("North Dakota", 45.9, 49.0, -104.1, -96.6),
    "OH": ("Ohio", 38.4, 42.0, -84.8, -80.5),
    "OK": ("Oklahoma", 33.6, 37.0, -103.0, -94.4),
    "OR": ("Oregon", 42.0, 46.3, -124.6, -116.5),
    "PA": ("Pennsylvania", 39.7, 42.3, -80.5, -74.7),
    "RI": ("Rhode Island", 41.1, 42.0, -71.9, -71.1),
    "SC": ("South Carolina", 32.0, 35.2, -83.4, -78.5),
    "SD": ("South Dakota", 42.5, 45.9, -104.1, -96.4),
    "TN": ("Tennessee", 35.0, 36.7, -90.3, -81.6),
    "TX": ("Texas", 25.8, 36.5, -106.6, -93.5),
    "UT": ("Utah", 37.0, 42.0, -114.1, -109.0),
    "VT": ("Vermont", 42.7, 45.0, -73.4, -71.5),
    "VA": ("Virginia", 36.5, 39.5, -83.7, -75.2),
    "WA": ("Washington", 45.5, 49.0, -124.8, -116.9),
    "WV": ("West Virginia", 37.2, 40.6, -82.6, -77.7),
    "WI": ("Wisconsin", 42.5, 47.1, -92.9, -86.8),
    "WY": ("Wyoming", 41.0, 45.0, -111.1, -104.0),
}


def _slug(name: str) -> str:
    return name.lower().replace(" ", "-")


def geofabrik_url(code: str) -> str:
    name = BOXES[code][0]
    return f"https://download.geofabrik.de/north-america/us/{_slug(name)}-latest.osm.pbf"


def state_at(lat: float, lon: float) -> Optional[str]:
    """Most specific state whose bbox contains the point, or None."""
    hits = []
    for code, (_, la0, la1, lo0, lo1) in BOXES.items():
        if la0 <= lat <= la1 and lo0 <= lon <= lo1:
            area = (la1 - la0) * (lo1 - lo0)
            hits.append((area, code))
    return min(hits)[1] if hits else None


def name(code: str) -> str:
    return BOXES.get(code, (code,))[0]
