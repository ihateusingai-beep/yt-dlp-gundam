"""
Application paths for yt-dlp Gundam dashboard.

Extracted from main.py v0.8.1. Centralizes the frozen-vs-dev layout
decision so main.py can stay focused on the FastAPI surface.
"""
from __future__ import annotations

import sys
from pathlib import Path

# --------------------------------------------------------------------------- #
# Frozen (PyInstaller) vs dev layout
# --------------------------------------------------------------------------- #
# Frozen exe layout: dist/yt_dlp_gundam/yt_dlp_gundam.exe lives next to
#   dist/yt_dlp_gundam/_internal/ which holds bundled ffmpeg, templates, etc.
#   sys._MEIPASS is the _internal/ root during frozen execution.
# Dev layout: `python3 main.py` from the project root — everything lives
#   in the source tree.
# --------------------------------------------------------------------------- #
if getattr(sys, "frozen", False):
    # sys.executable is <dist>/yt_dlp_gundam/yt_dlp_gundam.exe
    APP_DIR = Path(sys.executable).parent.resolve()
    TEMPLATES = Path(sys._MEIPASS) / "templates"
else:
    APP_DIR = Path(__file__).parent.resolve()
    TEMPLATES = APP_DIR / "templates"

# Alias for templates bundling in frozen mode.
BASE_DIR = APP_DIR

# downloads/ goes next to the .exe so users can find their files. We
# create the directory on import so /api/files and /api/download can
# write to it without an extra startup check.
DOWNLOADS = APP_DIR / "downloads"


def init_downloads_dir() -> Path:
    """Create DOWNLOADS if missing and return it. Idempotent."""
    DOWNLOADS.mkdir(exist_ok=True)
    return DOWNLOADS
