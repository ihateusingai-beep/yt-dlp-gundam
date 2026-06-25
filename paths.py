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

# downloads/ goes next to the .exe so users can find their files. If
# APP_DIR is read-only (frozen exe installed under Program Files, etc.),
# init_downloads_dir() falls back to ~/yt-dlp-gundam-downloads/ and
# rebinds the module-level DOWNLOADS in place.
DOWNLOADS = APP_DIR / "downloads"


def init_downloads_dir() -> Path:
    """Create DOWNLOADS if missing and return it.

    On a read-only APP_DIR (typical when a frozen .exe is installed under
    Program Files or similar protected path), the original mkdir raises
    PermissionError / OSError. In that case we fall back to the user's
    home directory and rebind the module-level ``DOWNLOADS`` so all
    subsequent uses (/api/files, /api/download, /api/folder) see the
    fallback location without each call site having to care.

    The fallback path is announced on stderr so the user knows where
    their files landed when the bundled location failed.
    """
    global DOWNLOADS
    try:
        DOWNLOADS.mkdir(exist_ok=True, parents=True)
        return DOWNLOADS
    except (PermissionError, OSError) as e:
        fallback = Path.home() / "yt-dlp-gundam-downloads"
        fallback.mkdir(exist_ok=True, parents=True)
        DOWNLOADS = fallback
        print(
            f"[paths] APP_DIR not writable ({APP_DIR}: {e}); "
            f"using fallback {fallback}",
            file=sys.stderr,
        )
        return fallback
