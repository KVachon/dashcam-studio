"""Cross-platform configuration, resolved per host with env overrides.

Keeping every platform-specific choice here means the rest of the code has no
`if macOS` branches. Stdlib only, so gpxtrack (which runs on a bare interpreter)
can import it.
"""

from __future__ import annotations

import os
import platform

SYSTEM = platform.system()  # 'Darwin' | 'Windows' | 'Linux'


# --------------------------------------------------------------------------
# video encoder
# --------------------------------------------------------------------------

def video_encoder() -> str:
    """H.264 encoder for this host. Override with DASHCAM_ENCODER.

    VideoToolbox is hardware-accelerated on Apple Silicon. Elsewhere libx264
    (software) is the safe default that exists in every ffmpeg build; users with
    an NVIDIA/Intel GPU can set DASHCAM_ENCODER=h264_nvenc / h264_qsv.
    """
    env = os.environ.get("DASHCAM_ENCODER")
    if env:
        return env
    return "h264_videotoolbox" if SYSTEM == "Darwin" else "libx264"


def encoder_args(bitrate: str) -> list:
    enc = video_encoder()
    if enc == "libx264":
        return ["-c:v", enc, "-preset", "veryfast", "-b:v", bitrate]
    return ["-c:v", enc, "-b:v", bitrate]


# --------------------------------------------------------------------------
# HUD font
# --------------------------------------------------------------------------

def hud_font() -> str:
    """Font family for the HUD. Override with HUD_FONT.

    Defaults to a geometric face that ships with each OS -- Futura on macOS,
    Bahnschrift (a DIN cut) on Windows. cairo falls back to the platform default
    sans if the family is absent, so this never errors.
    """
    env = os.environ.get("HUD_FONT")
    if env:
        return env
    return {"Darwin": "Futura", "Windows": "Bahnschrift"}.get(SYSTEM, "sans-serif")


# --------------------------------------------------------------------------
# camera clock timezone
# --------------------------------------------------------------------------

# The dashcam stamps filenames in local wall-clock with no zone. This is that
# zone; set DASHCAM_TZ to wherever the camera's clock is set.
CAMERA_TZ_NAME = os.environ.get("DASHCAM_TZ", "America/Chicago")


# --------------------------------------------------------------------------
# folder-picker shortcuts (web UI)
# --------------------------------------------------------------------------

def folder_shortcuts():
    from pathlib import Path
    home = Path.home()
    if SYSTEM == "Darwin":
        cloud = [("iCloud Drive", home / "Library/Mobile Documents/com~apple~CloudDocs")]
    elif SYSTEM == "Windows":
        cloud = [("OneDrive", home / "OneDrive")]
    else:
        cloud = []
    candidates = cloud + [
        ("Desktop", home / "Desktop"),
        ("Documents", home / "Documents"),
        ("Downloads", home / "Downloads"),
        ("Home", home),
    ]
    return [{"name": n, "path": str(p)} for n, p in candidates if p.is_dir()]
