"""Detect landmark callouts along a drive, offline from OSM data.

Three kinds of "moment":
  - boundary crossings  (state / county / city)      -> "ENTERING ..."
  - water crossings     (route crosses a named waterway) -> "CROSSING ..."
  - passing near a notable feature (peak, lake, park,
    university, historic site, airport, attraction)  -> "PASSING ..."

Extraction mirrors the road extraction: pull named features for the track's
bbox from any .pbf, then test each frame against them. Output is a sparse list
of events keyed by frame; the HUD shows each for a few seconds.

    python landmarks.py --frames frames.json --admin admin.geojson \
        --landmarks landmarks.geojson --out events.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from shapely.geometry import LineString, Point, shape
from shapely.strtree import STRtree

sys.path.insert(0, str(Path(__file__).resolve().parent))

import us_states
from gpxtrack import haversine_m

FPS = 30.0
CALLOUT_TTL = int(3.2 * FPS)      # how long a banner stays up
PASSBY_MIN_GAP = int(18 * FPS)    # min spacing between "passing" callouts
SAMPLE = 8                        # test every Nth frame (speed)

# category -> (proximity metres to trigger, priority). Water uses 0 (crossing).
CATS = {
    "state":      (0, 5), "county": (0, 4), "city": (0, 4),
    "water":      (0, 3),
    "peak":       (2500, 2), "lake": (900, 2), "park": (500, 1),
    "university": (700, 2), "historic": (350, 1), "airport": (3500, 2),
    "poi":        (400, 1),
}


def classify(p: dict):
    """Map OSM tags to a landmark category, or None."""
    if p.get("waterway") in ("river", "stream", "canal"):
        return "water"
    if p.get("natural") == "peak":
        return "peak"
    if p.get("natural") == "water":
        return "lake"
    if (p.get("leisure") in ("park", "nature_reserve")
            or p.get("boundary") in ("protected_area", "national_park")):
        return "park"
    if p.get("amenity") in ("university", "college"):
        return "university"
    if p.get("historic"):
        return "historic"
    if p.get("aeroway") == "aerodrome":
        return "airport"
    if p.get("tourism") in ("attraction", "viewpoint", "museum"):
        return "poi"
    return None


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------

OSM_FILTERS = [
    "nwr/waterway=river,stream,canal", "nwr/natural=peak,water",
    "nwr/leisure=park,nature_reserve", "nwr/boundary=protected_area,national_park",
    "nwr/amenity=university,college", "nwr/historic", "nwr/aeroway=aerodrome",
    "nwr/tourism=attraction,viewpoint,museum",
]


def extract(bbox: str, pbf_dir: Path, out_path: Path, log=print) -> Path:
    feats = []
    for i, pbf in enumerate(sorted(pbf_dir.glob("*.pbf"))):
        area = out_path.parent / f"_lm_area_{i}.osm.pbf"
        filt = out_path.parent / f"_lm_filt_{i}.osm.pbf"
        gj = out_path.parent / f"_lm_{i}.geojson"
        try:
            subprocess.run(["osmium", "extract", "-b", bbox, str(pbf),
                            "-o", str(area), "--overwrite"], check=True, capture_output=True)
            subprocess.run(["osmium", "tags-filter", str(area), *OSM_FILTERS,
                            "-o", str(filt), "--overwrite"], check=True, capture_output=True)
            subprocess.run(["osmium", "export", str(filt), "-o", str(gj), "--overwrite"],
                           check=True, capture_output=True)
            for f in json.loads(gj.read_text()).get("features", []):
                if f["properties"].get("name") and classify(f["properties"]):
                    feats.append(f)
        except subprocess.CalledProcessError as e:
            log(f"  ! landmark extract on {pbf.name}: {e}")
        finally:
            for x in (area, filt, gj):
                x.unlink(missing_ok=True)
    out_path.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    return out_path


# --------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------


def _county_index(admin_path: Path):
    """Level-6 (county) polygons, for crossing detection."""
    geoms, names = [], []
    for f in json.loads(admin_path.read_text())["features"]:
        if f["properties"].get("admin_level") == "6":
            try:
                geoms.append(shape(f["geometry"]))
                names.append(f["properties"].get("name", ""))
            except Exception:
                pass
    return (STRtree(geoms) if geoms else None), geoms, names


def _county_at(idx, lat, lon):
    tree, geoms, names = idx
    if tree is None:
        return None
    pt = Point(lon, lat)
    for i in tree.query(pt):
        if geoms[i].contains(pt):
            return names[i]
    return None




def detect(frames, landmarks_path: Path, admin_path: Path):
    fixed = [(i, f) for i, f in enumerate(frames) if f.get("has_fix") and f.get("lat")]
    if not fixed:
        return []

    lm = json.loads(landmarks_path.read_text())["features"]
    waters, water_names = [], []
    pts, pt_meta = [], []
    for f in lm:
        cat = classify(f["properties"])
        name = f["properties"]["name"]
        try:
            g = shape(f["geometry"])
        except Exception:
            continue
        if cat == "water" and g.geom_type in ("LineString", "MultiLineString"):
            waters.append(g)
            water_names.append(name)
        else:
            c = g.centroid
            pts.append(c)
            pt_meta.append((name, cat))
    water_tree = STRtree(waters) if waters else None
    pt_tree = STRtree(pts) if pts else None
    counties = _county_index(admin_path)

    events = []

    def debounced_change(get):
        """Yield (frame_index, new_value) when `get(f)` settles on a new value."""
        prev = None
        for i, f in fixed[::SAMPLE]:
            v = get(f)
            if v and v != prev:
                yield i, v, prev
                prev = v
            elif v:
                prev = v

    # boundary crossings ----------------------------------------------------
    for i, city, old in debounced_change(lambda f: f.get("place")):
        if old is not None:  # skip the very first (opening, not a crossing)
            events.append({"frame": i, "category": "city", "title": city.upper(),
                           "subtitle": "", "priority": CATS["city"][1]})
    for i, st, old in debounced_change(lambda f: f.get("state")):
        if old is not None:
            events.append({"frame": i, "category": "state",
                           "title": us_states.name(st).upper(),
                           "subtitle": "ENTERING", "priority": CATS["state"][1]})
    if counties[0] is not None:
        prevc = None
        for i, f in fixed[::SAMPLE]:
            c = _county_at(counties, f["lat"], f["lon"])
            if c and c != prevc:
                if prevc is not None:
                    events.append({"frame": i, "category": "county", "title": c.upper(),
                                   "subtitle": "", "priority": CATS["county"][1]})
                prevc = c

    # water crossings -------------------------------------------------------
    if water_tree is not None:
        crossed = set()
        for k in range(0, len(fixed) - SAMPLE, SAMPLE):
            i0, f0 = fixed[k]
            _, f1 = fixed[k + SAMPLE]
            seg = LineString([(f0["lon"], f0["lat"]), (f1["lon"], f1["lat"])])
            for j in water_tree.query(seg):
                if waters[j].intersects(seg) and water_names[j] not in crossed:
                    crossed.add(water_names[j])
                    events.append({"frame": i0, "category": "water",
                                   "title": water_names[j].upper(), "subtitle": "CROSSING",
                                   "priority": CATS["water"][1]})

    # passing near notable features ----------------------------------------
    if pt_tree is not None:
        seen = set()
        passby = []
        for i, f in fixed[::SAMPLE]:
            lat, lon = f["lat"], f["lon"]
            dlat = 3600 / 111320.0
            box = Point(lon, lat).buffer(dlat)
            for j in pt_tree.query(box):
                name, cat = pt_meta[j]
                if name in seen:
                    continue
                prox = CATS[cat][0]
                d = haversine_m(lat, lon, pts[j].y, pts[j].x)
                if d <= prox:
                    seen.add(name)
                    passby.append({"frame": i, "category": cat, "title": name.upper(),
                                   "subtitle": "PASSING", "priority": CATS[cat][1], "dist": d})
        events += passby

    # throttle: boundary/water always kept; pass-by spaced out --------------
    events.sort(key=lambda e: e["frame"])
    kept, last_passby = [], -10 ** 9
    for e in events:
        if e["category"] in ("state", "county", "city", "water"):
            kept.append(e)
        elif e["frame"] - last_passby >= PASSBY_MIN_GAP:
            kept.append(e)
            last_passby = e["frame"]
    for e in kept:
        e.pop("dist", None)
        e.pop("priority", None)
        e["ttl"] = CALLOUT_TTL
    return kept


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--frames", type=Path, required=True)
    ap.add_argument("--landmarks", type=Path, required=True)
    ap.add_argument("--admin", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    frames = json.loads(args.frames.read_text())
    events = detect(frames, args.landmarks, args.admin)
    args.out.write_text(json.dumps(events))
    print(f"{len(events)} callout(s)")
    for e in events[:30]:
        print(f"  frame {e['frame']:6d}  {e['category']:9} {e.get('subtitle',''):9} {e['title']}")


if __name__ == "__main__":
    main()
