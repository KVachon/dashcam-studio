"""Group Fitcamx loop clips into drives and losslessly concatenate each.

A drive is a run of clips recorded back-to-back; a gap longer than
NEW_DRIVE_GAP_S between one clip ending and the next starting means the engine
was off in between, i.e. a new drive.

    python3 scripts/stitch_fitcamx.py <clips_dir> [--out drives]

Concatenation is stream-copy (`-c copy`) -- no re-encode, so a drive stitches in
seconds and loses no quality. Each output is named with the first clip's
14-digit timestamp, so the rest of the pipeline (clip_start_utc) reads the drive
start straight from the filename.

Reads the timestamp from the filename, falling back to file mtime, so it
survives iCloud/copy mangling of file dates.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gpxtrack import CAMERA_TZ, FITCAMX_RE

# Engine-off gap that separates one drive from the next.
NEW_DRIVE_GAP_S = 600.0  # 10 min

VIDEO_EXTS = {".mp4", ".MP4", ".mov", ".MOV"}


@dataclass
class Clip:
    path: Path
    start: datetime          # aware UTC
    duration: float          # seconds
    from_name: bool          # start parsed from filename vs mtime fallback

    @property
    def end(self) -> datetime:
        from datetime import timedelta
        return self.start + timedelta(seconds=self.duration)


def probe_duration(path: Path) -> float:
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)]
    )
    return float(out.strip())


def clip_paths(d: Path) -> List[Path]:
    """Video files in d, minus macOS junk.

    exFAT cards accumulate AppleDouble sidecars ('._name.MP4') and hidden
    dotfiles; they share the .MP4 suffix but are not videos, so filter by name,
    not just extension.
    """
    return sorted(
        p for p in d.iterdir()
        if p.suffix in VIDEO_EXTS and not p.name.startswith("._")
        and not p.name.startswith(".")
    )


def load_clips(d: Path):
    """Build Clips from a folder, skipping anything ffprobe can't read.

    Returns (clips, skipped_names) so a single unreadable file degrades to a
    warning instead of a 500.
    """
    clips, skipped = [], []
    for p in clip_paths(d):
        try:
            clips.append(clip_from_path(p))
        except Exception:
            skipped.append(p.name)
    return clips, skipped


def clip_from_path(path: Path) -> Clip:
    m = FITCAMX_RE.search(path.name)
    if m:
        naive = datetime.strptime(m.group("stamp"), "%Y%m%d%H%M%S")
        start = naive.replace(tzinfo=CAMERA_TZ).astimezone(timezone.utc)
        from_name = True
    else:
        start = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        from_name = False
    return Clip(path=path, start=start, duration=probe_duration(path), from_name=from_name)


def group_drives(clips: List[Clip], gap_s: float = NEW_DRIVE_GAP_S) -> List[List[Clip]]:
    """Split time-sorted clips wherever the inter-clip gap exceeds gap_s."""
    clips = sorted(clips, key=lambda c: c.start)
    drives: List[List[Clip]] = []
    for c in clips:
        if drives and (c.start - drives[-1][-1].end).total_seconds() <= gap_s:
            drives[-1].append(c)
        else:
            drives.append([c])
    return drives


def stitch(drive: List[Clip], out_dir: Path) -> Path:
    """Lossless concat. A single-clip drive is just copied through the same path."""
    stamp = drive[0].start.astimezone(CAMERA_TZ).strftime("%Y%m%d%H%M%S")
    out = out_dir / f"drive_{stamp}.mp4"
    out_dir.mkdir(parents=True, exist_ok=True)

    listfile = out_dir / f".concat_{stamp}.txt"
    listfile.write_text("".join(f"file '{c.path.resolve()}'\n" for c in drive))
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0",
         "-i", str(listfile), "-c", "copy", str(out)],
        check=True,
    )
    listfile.unlink(missing_ok=True)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("clips_dir", type=Path)
    ap.add_argument("--out", type=Path, default=Path("drives"))
    ap.add_argument("--gap", type=float, default=NEW_DRIVE_GAP_S,
                    help="seconds between clips that starts a new drive")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    clips, skipped = load_clips(args.clips_dir)
    if not clips:
        raise SystemExit(f"no readable video files in {args.clips_dir}")
    drives = group_drives(clips, args.gap)

    if skipped:
        print(f"! skipped {len(skipped)} unreadable file(s), e.g. {skipped[0]}")
    guessed = [c for c in clips if not c.from_name]
    if guessed:
        print(f"! {len(guessed)} clip(s) had no filename timestamp; used mtime")

    print(f"{len(clips)} clip(s) -> {len(drives)} drive(s)")
    for i, drive in enumerate(drives):
        span = (drive[-1].end - drive[0].start).total_seconds()
        t0 = drive[0].start.astimezone(CAMERA_TZ)
        print(f"  drive {i+1}: {len(drive):2d} clip(s)  {t0:%Y-%m-%d %H:%M:%S} "
              f"+ {span/60:.1f} min")
        if not args.dry_run:
            out = stitch(drive, args.out)
            print(f"           -> {out}")


if __name__ == "__main__":
    main()
