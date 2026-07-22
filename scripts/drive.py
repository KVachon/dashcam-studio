"""End-to-end drive pipeline: a folder of clips + a GPX -> finished videos.

Ties the pieces together at the level a road trip actually works in -- the
*drive*, not the individual 5-minute clip:

  1. group clips into drives and losslessly stitch each   (stitch_fitcamx)
  2. calibrate the clock ONCE per drive                    (calibrate)
  3. build one continuous per-frame stream for the drive   (framestream)
     -> cumulative distance and the travelled trail span the whole drive,
        not just one clip, because it is a single resample over the drive.
  4. burn the HUD onto the stitched drive                  (render)

    .venv/bin/python scripts/drive.py --clips clips/testdrive \
        --gpx 2026-07-21.gpx --matched out/matched.json \
        --roads out/roads.geojson --admin out/admin.geojson --out out

Prerequisites (built once, not per drive): matched.json from mapmatch.py, and
roads/admin GeoJSON from osmium. This script owns only the per-drive assembly.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gpxtrack import CAMERA_TZ
from stitch_fitcamx import group_drives, load_clips, stitch

PY = sys.executable
HERE = Path(__file__).resolve().parent


def run(cmd: list, **kw) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        sys.stderr.write(r.stdout + r.stderr)
        raise SystemExit(f"step failed: {' '.join(str(c) for c in cmd)}")
    return r.stdout


def process_drive(drive, args, idx: int) -> Path:
    # Local-time stamp, matching the stitched filename stitch() produces.
    stamp = drive[0].start.astimezone(CAMERA_TZ).strftime("%Y%m%d%H%M%S")
    tag = f"drive {idx+1}"
    span = (drive[-1].end - drive[0].start).total_seconds()
    print(f"\n=== {tag}: {len(drive)} clip(s), {span/60:.1f} min ===")

    # 1. stitch (lossless; a 1-clip drive just copies through)
    video = stitch(drive, args.out / "drives")
    print(f"  stitched -> {video.name}")

    # 2. optionally calibrate the clock (off by default: the filename timestamp
    #    is accurate on a CFR camera, and a spurious sync offset drifts the map).
    sync = None
    if args.calibrate:
        sync = args.out / f"sync_{stamp}.json"
        out = run([PY, str(HERE / "calibrate.py"), "--clip", str(video),
                   "--gpx", str(args.gpx), "--out", str(sync)])
        for line in out.splitlines():
            if any(k in line for k in ("method", "offset", "confidence")):
                print(f"  {line.strip()}")

    # 3. one continuous frame stream for the whole drive
    frames = args.out / f"frames_{stamp}.json"
    cmd = [PY, str(HERE / "framestream.py"), str(args.matched),
           "--clip", str(video), "--admin", str(args.admin), "--json", str(frames)]
    if sync:
        cmd += ["--sync", str(sync)]
    run(cmd)
    print(f"  frames   -> {frames.name}")

    # 4. render the HUD onto the stitched drive
    final = args.out / f"drive_{stamp}_hud.mp4"
    run([PY, str(HERE / "render.py"), "--clip", str(video),
         "--frames", str(frames), "--roads", str(args.roads),
         "--out", str(final)])
    print(f"  rendered -> {final}")
    return final


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--clips", type=Path, required=True)
    ap.add_argument("--gpx", type=Path, required=True)
    ap.add_argument("--matched", type=Path, default=Path("out/matched.json"))
    ap.add_argument("--roads", type=Path, default=Path("out/roads.geojson"))
    ap.add_argument("--admin", type=Path, default=Path("out/admin.geojson"))
    ap.add_argument("--out", type=Path, default=Path("out"))
    ap.add_argument("--gap", type=float, default=600.0)
    ap.add_argument("--calibrate", action="store_true",
                    help="auto-correct the camera clock (off by default; only for a mis-set clock)")
    args = ap.parse_args()

    clips, skipped = load_clips(args.clips)
    if not clips:
        raise SystemExit(f"no readable video files in {args.clips}")
    if skipped:
        print(f"! skipped {len(skipped)} unreadable file(s)")
    drives = group_drives(clips, args.gap)
    print(f"{len(clips)} clip(s) -> {len(drives)} drive(s)")

    finals = [process_drive(d, args, i) for i, d in enumerate(drives)]
    print(f"\ndone: {len(finals)} drive video(s)")
    for f in finals:
        print(f"  {f}")


if __name__ == "__main__":
    main()
