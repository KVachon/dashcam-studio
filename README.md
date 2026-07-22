# Dashcam Studio

Turn dashcam footage plus a phone GPS track into finished drive videos with a
live map/HUD overlay — the road you're on, city/state, cumulative distance, a
heading-up mini-map of where you've driven — rendered **entirely offline**. No
geocoding APIs, no rate limits, nothing leaves your machine.

Point the app at a folder of clips and a folder of GPX files; it groups them
into drives, matches each to its GPS track, and renders full-speed or
content-aware timelapse videos.

<!-- add a screenshot/GIF here -->

## Status & platforms

Works today on **macOS** (developed on Apple Silicon). Written to be
**cross-platform** — the encoder, font, timezone and folder shortcuts all adapt
per OS, and every heavy dependency (Python, ffmpeg, Docker/Valhalla, pywebview)
runs on Windows and Linux too — but **Windows/Linux are not yet verified**. If
you run it there, issues and PRs are very welcome.

| Piece | State |
|---|---|
| Map-matching (Valhalla) | working |
| Per-frame data layer | working |
| Multi-clip drive pipeline | working |
| HUD renderer + timelapse | working |
| Desktop app + web UI | working |
| Auto-pull clips off the camera over WiFi | not started |

## Requirements

- **Python 3.11+**
- **ffmpeg** (with `ffprobe`)
- **Docker** — runs the Valhalla routing engine (offline map-matching)
- **osmium-tool** — one-time OSM road/boundary extraction
- Python libs in `requirements.txt` (Flask, pywebview, numpy, pycairo, shapely, timezonefinder)

### Installing the external tools

| Tool | macOS | Windows | Linux |
|---|---|---|---|
| ffmpeg | `brew install ffmpeg` | `winget install ffmpeg` | `apt install ffmpeg` |
| Docker | Docker Desktop | Docker Desktop | Docker Engine |
| osmium | `brew install osmium-tool` | `conda install -c conda-forge osmium-tool` | `apt install osmium-tool` |

## Install

```sh
git clone https://github.com/USER/dashcam-studio && cd dashcam-studio
python -m venv .venv
.venv/bin/pip install -r requirements.txt        # Windows: .venv\Scripts\pip
```

Then build the map data for your region (once) — see **Runbook** below to start
Valhalla and **Overlay renderer** for the `osmium` road/boundary extraction.

## Run

- **Desktop app**: double-click `Dashcam Studio.command` (macOS) or
  `Dashcam Studio.bat` (Windows), or `python scripts/app.py`.
- **Browser / headless**: `python scripts/webui.py`, then open
  <http://127.0.0.1:5151>.

The app checks its own dependencies on launch (green/red list with fixes) and
flags any GPX that leaves your loaded map data.

## Configuration (env vars)

| Var | Default | Meaning |
|---|---|---|
| `DASHCAM_TZ` | `America/Chicago` | the zone the camera's clock is set to |
| `DASHCAM_ENCODER` | VideoToolbox (mac) / `libx264` | H.264 encoder; set `h264_nvenc`/`h264_qsv` for GPU |
| `HUD_FONT` | Futura (mac) / Bahnschrift (win) | HUD font family |
| `HUD_SCALE` | `0.75` | overall HUD size |
| `HUD_SCRIM` | `0.35` | darkening behind the map disc |

## Runbook

Start Valhalla (tiles persist in `valhalla/custom_files`, so restarts are fast):

```sh
docker start valhalla          # if the container already exists
curl -s http://127.0.0.1:8002/status
```

First-time / rebuild:

```sh
docker run -d --name valhalla -p 8002:8002 \
  -v "$PWD/valhalla/custom_files:/custom_files" \
  -e serve_tiles=True -e build_elevation=False \
  -e build_admins=False -e build_time_zones=False \
  ghcr.io/gis-ops/docker-valhalla/valhalla:latest
```

Alabama tiles build in ~80 s natively on the M1. Adding states = drop more
`.pbf` files into `valhalla/custom_files` and set `force_rebuild=True`.

Map-match a track:

```sh
/usr/bin/python3 scripts/mapmatch.py 2026-07-21.gpx            # road transitions
/usr/bin/python3 scripts/mapmatch.py 2026-07-21.gpx --all      # every point
/usr/bin/python3 scripts/mapmatch.py 2026-07-21.gpx --json out/matched.json
```

## Environment traps (both cost real time on 2026-07-21)

**Use `/usr/bin/python3`, not the Homebrew one.** Homebrew Python 3.14.5 has a
broken `pyexpat` (links against the system `libexpat`, missing a symbol), which
kills all stdlib XML parsing — fatal for GPX. Installing brew's `expat` does not
help; the path is baked into the `.so`. Fix properly later via a uv-managed
standalone Python.

**If Homebrew or `docker pull` fails inexplicably, test `curl https://ghcr.io/v2/`
first.** GitHub was blocked network-wide on 2026-07-21 (GitHub, X and Reddit all
failed in Chrome/curl but worked in Safari, because iCloud Private Relay bypassed
it). Homebrew serves bottles from ghcr.io, so the block broke every `brew install`
with a confusing curl(7). A reboot cleared it.

## Sample data

- `20260721102148_2026073.MP4` — first clip, starts **10:21:48 CDT**
- `2026-07-21.gpx` — first fix **10:22:33 CDT**, i.e. a **45 s cold-start gap**
- 2 segments (out 6.5 min / back 3.8 min) with a 9.4 min stop between
- Route: Bonnie Oaks Dr SW → Bell Rd SW → Hobbs Rd SW → parking lot, and back.
  All local roads — **no interstate on this track**, despite the brief's I-565
  example. Total 3.34 mi.

## Data-layer contract

`scripts/mapmatch.py` emits per GPS fix:

| Field | Status |
|---|---|
| `t_utc`, `t_local`, `lat`, `lon` | done (snapped position) |
| `road_name`, `road_ref`, `road_class`, `way_id` | done |
| `heading_deg` | done (derived from consecutive positions) |
| `cum_dist_m` | done — 1 m jitter gate, carries across segments |
| `match_type`, `off_trace_m` | done (match-quality diagnostics) |

`scripts/framestream.py` resamples that to **per video frame** and adds:

| Field | Notes |
|---|---|
| `frame`, `t_offset_s` | index and seconds into the clip |
| `speed_mps` | central-difference off `cum_dist`, 3-tap smoothed. Internal only — the HUD shows distance, not MPH |
| `zoom_radius_m` | speed x 20s lookahead, tightened by upcoming turn density and road class, EMA-smoothed (tau 2.5s), clamped 80-900m |
| `has_fix` | **real** — False before the first fix and across any logging gap >30s |
| `t_display`, `tz_abbr`, `tz_name` | wall-clock **at the position**, via `timezonefinder` |
| `place`, `state` | city limits (or county) + 2-letter state, via `scripts/places.py` |

### City / state

Point-in-polygon against OSM admin boundaries — not nearest-town guessing, so a
position outside city limits reports honestly rather than claiming the nearest
name. `admin_level` 4 = state, 6 = county, 8 = city/town. Outside any city the
place falls back to the county (`Morgan Co.`).

Build the boundary file once per state pbf:

```sh
osmium tags-filter valhalla/custom_files/alabama-latest.osm.pbf \
    r/boundary=administrative w/boundary=administrative \
    -o out/admin_raw.osm.pbf --overwrite
osmium export out/admin_raw.osm.pbf --geometry-types=polygon \
    --add-unique-id=type_id -o out/admin.geojson --overwrite
```

`osmium export` assembles multipolygon relations, so these come out as real
closed areas. For a multi-state trip, concatenate each state's features into one
GeoJSON. Alabama yields 1 state + 68 counties + 471 municipalities.

Needs `shapely`; without it `framestream.py` warns and leaves the fields blank
rather than failing.

**The sample drive is in Huntsville, not Madison.** Huntsville's city limits
extend well past the airport, and the road names confirm it — `Bell Road
Southwest` uses Huntsville's NW/NE/SW/SE quadrant convention.

### Two clocks, deliberately

The camera has no GPS, so its clock stays on whatever zone it was set to. That
fixed zone (`CAMERA_TZ`, America/Chicago) is what decodes the filename into UTC
and must never change — otherwise video/GPS alignment breaks the moment a drive
crosses a boundary.

The *displayed* time is separate: resolved per position from lat/lon. Driving
east on I-24 the readout steps 12:02 CDT -> 13:03 EDT while UTC stays
continuous (the boundary is west of Chattanooga, so the city is already
Eastern). The zone abbreviation is shown so that jump reads as correct rather
than as a bug. Verified against a synthetic crossing; the Madison sample never
leaves Central.

If `timezonefinder` is missing the code falls back to `CAMERA_TZ` rather than
failing.

Continuous quantities are interpolated; road name/ref/class are held from the
preceding fix (averaging a road name is meaningless).

```sh
/usr/bin/python3 scripts/framestream.py out/matched.json \
    --clip 20260721102148_2026073.MP4 --json out/frames.json
```

Sample clip: 9000 frames, 7650 with fix (85%), 1350 blind (the 45s cold start).
~3 MB of JSON per 5-minute clip.

## Overlay renderer

Road geometry for the map inset comes from the `.pbf`, not Valhalla (which
gives the road you are *on*, not the network around it):

```sh
osmium extract -b <W,S,E,N> valhalla/custom_files/alabama-latest.osm.pbf -o out/area.osm.pbf
osmium tags-filter out/area.osm.pbf w/highway -o out/roads.osm.pbf
osmium export out/roads.osm.pbf --geometry-types=linestring \
    --add-unique-id=type_id -o out/roads.geojson
```

`--add-unique-id=type_id` is required: the renderer matches the current road by
OSM way id, not by name (Hobbs Road alone is 5 separate ways).

Preview stills, then render:

```sh
.venv/bin/python scripts/hud.py --frames out/frames.json --roads out/roads.geojson \
    --clip 20260721102148_2026073.MP4 --at 60 --at 150 --at 215

.venv/bin/python scripts/render.py --clip 20260721102148_2026073.MP4 \
    --frames out/frames.json --roads out/roads.geojson --out out/drive_hud.mp4
```

~128 fps (4x realtime); a 5-minute clip renders in ~70 s to ~455 MB. Frames are
streamed into ffmpeg as raw BGRA — no intermediate PNGs. cairo's ARGB32 is
premultiplied, hence `overlay=alpha=premultiplied`; without it the edges halo.

### HUD design knobs

All at the top of `scripts/hud.py`. `SCALE` drives every dimension, so resizing
the whole HUD is one number.

| Constant | Current | Meaning |
|---|---|---|
| `SCALE` | `0.75` | overall size — env `HUD_SCALE` |
| `FONT` | `Futura` | family name, resolved via fontconfig (`fc-list : family`) |
| `SCRIM_A` | `0.35` | radial wash behind the disc — env `HUD_SCRIM` |
| `PANEL_A` | `0.0` | broad wash behind the whole block — env `HUD_PANEL` |
| `NORTH_UP` | `True` | north-up; marker rotates. False = heading-up, map rotates |
| `SCRIM_A` | `0.0` | radial scrim behind the disc; off = fully transparent |
| `MAP_CX/CY` | `150, 928` | disc centre — kept on the car hood |
| `ROAD_W_MULT` | `2.0` | weight of the grey network |
| `ROAD_W_CAP` | `0.85 * TRAIL_W` | ceiling on grey width — see below |

`ROAD_W_CAP` exists because at `ROAD_W_MULT = 2.0` a motorway would be 2.8px
against the trail's 2.1px, making the background wider than the highlight. The
cap currently flattens motorway/trunk/primary/secondary/tertiary to one width,
so those classes differ only by alpha. Harmless around Madison (no motorways);
on interstate footage, raise `TRAIL_W` to give the grey more headroom rather
than lifting the cap.

The **travelled route** is the only highlight: bold white, persisting for the
whole drive, so roads already behind you stay lit. Nothing is highlighted by OSM
way id — doing that lights up the whole of e.g. Bell Road including the part you
never drove. The trail is drawn as separate runs split on loss of fix, so a GPS
gap breaks the line rather than cheating a straight segment across it, and the
live position is appended each frame so the highlight always reaches the marker.

Everything else is thin grey with no under-stroke, and there is no rim circle.
With the scrim off, the only thing holding hairlines against bright sun is the
dark under-stroke on the trail — expect this to be marginal on a light-coloured
car or at night.

`UNNAMED ROAD` is not a bug: plenty of commercial access roads and parking
aisles are tagged `highway=service` with no `name` in OSM.

## The app

Two front doors to the same backend:

```
Dashcam Studio.command      # double-click -> native window (scripts/app.py)
.venv/bin/python scripts/webui.py   # browser at http://127.0.0.1:5151
```

The native window (**pywebview**) is the everyday one: Flask runs invisibly on a
private localhost port in a background thread and a real macOS window shows the
UI — no browser, no URL, no server to start by hand. It also tries to
`docker start valhalla` on launch. Needs a graphical session; on a **headless**
mini use the browser version (`webui.py`) and reach it from another machine.

Point it at a clips folder and a GPX folder; it groups clips into drives,
auto-matches each drive to the GPX that overlaps it in time, and runs the whole
pipeline with live progress. Localhost only, no auth — don't expose it. The HTML
in `web/` is a template served by the app, **not** a standalone file (opening it
directly shows a "start the app" message).

**Folders**: the Browse dialog has quick-picks — iCloud Drive, Desktop,
Documents, Downloads, Home — or paste a path. iCloud Drive lives at
`~/Library/Mobile Documents/com~apple~CloudDocs`. Caveat: with "Optimize Mac
Storage" on, iCloud files may be *evicted* (placeholders). Listing folders is
fine, but the first read of an evicted clip/GPX downloads it on demand, which
can be slow (a 257 MB clip is a real wait). Before processing a drive from
iCloud, right-click the folder in Finder → **Download Now** (or `brctl download
<path>`) so nothing stalls mid-render.

- **System check** — `scripts/preflight.py` verifies every dependency (python
  libs, ffmpeg/ffprobe/osmium/docker, the Valhalla service, tiles + roads/admin
  GeoJSON) with collapsible fix-it instructions. Also runnable standalone.
- **Map-data coverage** — each matched GPX is checked against loaded coverage;
  if it crosses into a state we lack data for, the plan flags it by name
  (`scripts/us_states.py`) with a direct Geofabrik `.pbf` link and steps to add
  it. Covered states are confirmed green.
- **Named drives** — each detected drive is labelled by its route
  (start → end city, reverse-geocoded from the GPX) plus day/time/length/clips,
  not "drive 1".
- **Per-drive selection** — checkboxes pick which drives to process; Process
  runs only those. Handy when a card holds many drives but you want a few.
- **Auto road extraction** — the map inset's road geometry is extracted per
  drive for the GPX's own bounding box (from any `.pbf` in
  `valhalla/custom_files`), so the mini-map covers wherever you actually drove
  without a hand-picked region. `admin.geojson` (city/state) stays whole-region
  because state polygons must not be clipped.
- **Frame preview** — a per-drive button renders one HUD frame from the middle
  of the drive (~10s; skips calibration and reuses the cached map-match) so you
  can check the look before committing to a full render. Click the thumbnail to
  open it full-size in a lightbox (Esc or click to dismiss).

ExFAT SD cards carry AppleDouble junk (`._name.MP4`); the scanner filters those
by name and skips any file ffprobe can't read, so one bad file never fails a
whole card.

## Drives (multi-clip)

A road trip is not single clips — it's *drives*, each a run of back-to-back
5-minute clips. `drive.py` is the top-level entry point:

```sh
.venv/bin/python scripts/drive.py --clips <clips_dir> --gpx <day>.gpx \
    --matched out/matched.json --roads out/roads.geojson --admin out/admin.geojson
```

Per drive it: groups clips by filename gap (`stitch_fitcamx.py`, new drive when
the gap exceeds 10 min) → losslessly stitches (`-c copy`, seconds, no re-encode)
→ calibrates the clock **once** → builds one continuous frame stream → renders.

Because the frame stream is a single resample over the whole drive, cumulative
distance and the travelled trail span the entire drive, not one clip. Validated
by splitting the sample into two Fitcamx clips, running the pipeline, and
confirming distance crosses the seam monotonically and reproduces the one-clip
result to 0.000 m.

The stitched drive is named with the first clip's 14-digit timestamp, so
`clip_start_utc` reads the drive start straight from the filename — no special
casing downstream. Prerequisites (`matched.json`, `roads`/`admin` GeoJSON) are
built once, not per drive.

## Timelapse

```sh
.venv/bin/python scripts/timelapse.py --clip <clip>.MP4 \
    --frames out/frames.json --roads out/roads.geojson \
    --out out/drive_timelapse_30s.mp4 --target-duration 30
```

`--dry-run` prints the plan (rates, output rotation percentiles) without
rendering — use it to explore before spending the encode.

Speed is driven by **how fast the view rotates**, not how fast the car moved:
nausea tracks angular velocity. On this sample 86% of frames turn at <5 deg/s
and can run flat out; the tail is what needs restraining.

| Knob | Value | Note |
|---|---|---|
| `MAX_RATE` | 60x | straights |
| `MIN_RATE` | 3x | floor at the sharpest turns |
| `STATIONARY_RATE` | 120x | dead time collapses |
| `OUT_ROT_TARGET` | 120 deg/s | soft target used to derive the rate |
| `OUT_ROT_HARD` | 260 deg/s | absolute ceiling, applied last |
| `MAX_BLEND` | 6 frames | motion blur, scaled to output rotation |

### Three ordering traps, all of which bit during development

1. **Enforce the rotation ceiling AFTER smoothing.** Every smoothing step pulls
   a turn's low rate back up toward the fast straights either side. An early
   version applied the cap first and peaked at **2683 deg/s**.
2. **`MIN_RATE` must be consistent with `OUT_ROT_HARD`.** `MIN_RATE * max turn
   rate` has to sit under the ceiling or the two rules fight and the floor
   wins. 3x against a 78 deg/s turn gives 234 deg/s — just inside 260.
3. **Take a minimum envelope before the EMA.** Otherwise a 1-second turn is
   averaged away by its 6-second neighbourhood and never slows at all.

### Two render paths

Default is **fast** (~20s for a 30s output, ~4x quicker than the blended path).
The win: the blended path pipes all 9000 source frames into Python (34s of raw
I/O) and composites the HUD over the whole frame (35s). The fast path instead
has ffmpeg emit *only the frames each output frame uses* — the per-frame rate is
quantized into piecewise-constant segments and turned into an ffmpeg `select`
expression (`between × mod` per segment, merged to <=80 terms because the parser
fails past ~90) — and composites only the HUD's bounding box. One source frame
per output frame, so no motion blur.

`--blur` restores the blended path: it averages several source frames per output
frame, so fast turns smear instead of strobe. Strobing, not speed, is what makes
fast turns sickening, so blur is the quality option when a drive has sharp turns.
Either way the HUD is composited *after* averaging, so map and type stay sharp.
Zoom easing is re-smoothed in **output** time (`ZOOM_TAU_OUT_S`); the source-time
constant would become 0.04s at 60x and the map would snap.

(Fast-path note: integer-rate snapping drifts the final length a few percent from
`--target-duration`; the printed frame count is the truth.)

## Time sync / clock calibration

Each frame's time is reconstructed, not read from the video: `clip_start_utc`
(from the filename, via the fixed camera zone) `+ i/fps`. So the camera clock
sets exactly one anchor — the clip start — and its error shifts the *entire*
clip's GPS lookup by that amount. At 45 mph, **1 s ≈ 20 m** of map lag.

`scripts/calibrate.py` removes the dependence on the camera clock being right.
The camera then only needs to be within the search window (±40 s), not
second-accurate.

```sh
.venv/bin/python scripts/calibrate.py --clip <clip>.MP4 --gpx <day>.gpx
# writes out/sync.json; framestream.py applies it only if high-confidence
```

Two methods, tried in order:

1. **Stop anchor (primary).** A stop is scene-independent and unambiguous:
   video motion and GPS speed both hit ~zero together. It detects stops in each
   stream — video by a strict near-zero motion floor (a true stop reads ~0.1 vs
   ~1.8 for open-road cruising, so they separate cleanly), GPX by low-speed-
   with-fix *and* Arc's sleep gaps (a lunch/gas stop is the clearest anchor of
   all) — and aligns them. Used whenever a stop is shared across both streams.
2. **Speed cross-correlation (fallback).** When no stop is shared, correlate the
   video's road-surface frame-diff against the GPX speed profile. Coarser: open
   highway is fast but visually stable, turns are slow but visually violent, so
   the proxy partly fights the speed signal.

**Two gates, so a good sync is never disturbed.** `framestream.py` applies an
offset only if it is (a) confident — `sharpness >= 3.0`, `peak >= 0.4` — and
(b) at least 0.5 s in magnitude. The second is a dead-band at the filename's own
whole-second resolution: a sub-0.5 s "correction" is false precision. So
calibration acts only on a real, multi-second error (a mis-set clock), and is a
no-op when the clock is already fine. Override by hand with `--sync-offset`.

The camera itself does not drift within a clip: it records constant 30.000 fps
(verified — frame jitter 0.0007 ms), and each clip re-anchors on its own
second-accurate filename, so error never accumulates across a trip. The residual
is a constant per-clip offset bounded by the +/-0.5 s filename quantization,
already below the 1-2 s GPX sample spacing. (A 29.97 fps camera *would* drift
~0.3 s per 5-min clip; worth an ffprobe check on any new hardware.)

Validation:

- Both methods recover known injected offsets exactly, correct sign (synthetic).
- Video stop-detection finds the real warm-up stop and rejects open-road frames;
  GPX stop-detection finds the parking-lot sleep gap — both on real data.
- **The sample clip cannot be calibrated end-to-end** and is correctly rejected:
  it has no shared stop (destination stop is past the clip end; the start-from-
  rest is inside the 45 s GPS blackout), and the fallback xcorr locks weakly
  (~−1 s, sharpness 1.1 — consistent with an already-accurate camera). A real
  multi-clip drive with any stop under GPS will lock hard. If it stays weak,
  `--sync-offset` and eyeball a preview frame.

## Python

Use the project venv's interpreter (`.venv/bin/python`, or `.venv\Scripts\python`
on Windows). Only `gpxtrack.py` is stdlib-only; everything else needs the deps
in `requirements.txt` (pycairo, numpy, shapely, …).

## Contributing

Cross-platform testing (especially Windows/Linux) and the not-yet-built
camera-pull piece are the most useful areas. Platform-specific choices live in
`scripts/settings.py`; keep new ones there rather than branching inline.

## License

GPL-3.0-or-later — see [LICENSE](LICENSE). No sample footage or GPS tracks are
included; bring your own dashcam clips and phone GPS export.
