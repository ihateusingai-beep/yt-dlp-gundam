"""
FFmpeg detection for yt-dlp Gundam dashboard.

Extracted from main.py v0.8.1. Resolution order:
  0. Frozen exe: sys._MEIPASS/ffmpeg.exe (bundled at build time)
  1. System PATH (`shutil.which("ffmpeg")`)
  2. imageio-ffmpeg's cached binary (works in dev, broken in frozen)
  3. Windows common install locations (fallback)

The bundled path is the one that works on the user's machine after
install — imageio-ffmpeg caches to a build-machine-specific path that
doesn't exist on the user's disk, so we must check _MEIPASS first.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def find_ffmpeg() -> str | None:
    """Return the path to ffmpeg, or None if not found."""
    # 0. Frozen exe (PyInstaller one-dir): ffmpeg.exe is bundled at
    #    sys._MEIPASS/ffmpeg.exe (i.e. next to the .exe in _internal/).
    #    The imageio-ffmpeg cached path points to the build machine and
    #    does not exist on the user's machine, so we must check _MEIPASS
    #    first.
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            for name in ("ffmpeg.exe", "ffmpeg"):
                candidate = Path(meipass) / name
                if candidate.exists() and candidate.stat().st_size > 1_000_000:
                    return str(candidate)
    # 1. System PATH
    path = shutil.which("ffmpeg")
    if path:
        return path
    # 2. Bundled ffmpeg from imageio-ffmpeg (works in dev, broken in frozen)
    try:
        import imageio_ffmpeg
        bundled = imageio_ffmpeg.get_ffmpeg_exe()
        if bundled and Path(bundled).exists():
            return bundled
    except ImportError:
        pass
    # 3. Windows fallback – search common install locations
    for candidate in [
        Path("C:/ffmpeg/bin/ffmpeg.exe"),
        Path("C:/Program Files/ffmpeg/bin/ffmpeg.exe"),
        Path("C:/Program Files (x86)/ffmpeg/bin/ffmpeg.exe"),
    ]:
        if candidate.exists():
            return str(candidate)
    return None


# Module-level constant — computed once at import. Tests can monkey-patch
# `media.FFMPEG_PATH` if they need a different ffmpeg location.
FFMPEG_PATH: str | None = find_ffmpeg()


def get_ffmpeg_version() -> str:
    """Return the first line of `ffmpeg -version`, or 'not found' / 'error'."""
    if not FFMPEG_PATH:
        return "not found"
    try:
        result = subprocess.run(
            [FFMPEG_PATH, "-version"],
            capture_output=True, text=True, timeout=10,
        )
        first_line = result.stdout.splitlines()[0] if result.stdout else "unknown"
        return first_line
    except Exception:
        return "error"


def ffmpeg_source_label(path: str) -> str:
    """Classify `path` as 'bundled' (shipped with the app) or 'system' (the
    user's install). Two bundled sources:
      - PyInstaller frozen exe: ffmpeg.exe lives under sys._MEIPASS (this is
        the spec-bundled `vendor/ffmpeg.exe`).
      - Dev mode: imageio-ffmpeg's cached binary.
    Anything else (shutil.which, C:\\ffmpeg, manual install) is 'system'."""
    try:
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass and (path == meipass or path.startswith(meipass + os.sep)):
            return "bundled"  # spec-bundled at _MEIPASS (vendor/ffmpeg.exe)
        if "imageio" in path.lower() or "site-packages" in path:
            return "bundled"  # imageio-ffmpeg cached
        return "system"
    except Exception:
        return "unknown"
