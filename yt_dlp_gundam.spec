# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for yt-dlp Gundam Dashboard (portable Windows .exe).

Run:  pyinstaller yt_dlp_gundam.spec
Output: dist/yt_dlp_gundam/yt_dlp_gundam.exe
"""
import sys
import os
from pathlib import Path

block_cipher = None

# Use current working directory. The GitHub Actions workflow runs
# `Set-Location $env:GITHUB_WORKSPACE` before pyinstaller, so cwd = repo root.
PROJECT_ROOT = Path('.').resolve()

# When frozen (running as .exe), templates live under sys._MEIPASS.
# Normal dev: templates/ is next to main.py.
if getattr(sys, 'frozen', False):
    base_path = Path(sys._MEIPASS)
else:
    base_path = PROJECT_ROOT

# Locate imageio-ffmpeg binary on the build host (Windows runner or local).
# We bundle it so the .exe works without a system FFmpeg install.
# imageio-ffmpeg downloads the binary on first call to get_ffmpeg_exe().
# Run a subprocess to force-download the binary, then locate it.
import subprocess
_extra_binaries = []
try:
    # Force-download by calling get_ffmpeg_exe in a subprocess (which
    # triggers imageio's lazy download to %LOCALAPPDATA% on Windows).
    proc = subprocess.run(
        [sys.executable, "-c",
         "import imageio_ffmpeg, os; p = imageio_ffmpeg.get_ffmpeg_exe();"
         "print(p); import sys; sys.exit(0 if os.path.exists(p) and os.path.getsize(p) > 1000000 else 1)"],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode == 0:
        ffmpeg_path = proc.stdout.strip().splitlines()[-1]
        # PyInstaller binaries format: list of (src, dst_dir) tuples
        _extra_binaries.append((ffmpeg_path, 'imageio_ffmpeg'))
        print(f"[spec] Bundling FFmpeg binary ({Path(ffmpeg_path).stat().st_size // 1024 // 1024}MB): {ffmpeg_path}")
    else:
        print(f"[spec] FFmpeg download failed: {proc.stderr.strip()}")
except Exception as e:
    print(f"[spec] imageio-ffmpeg setup error: {e}")

# Icon for the .exe (Windows shows this in Taskbar / File Explorer)
ICON_PATH = PROJECT_ROOT / 'ntd_icon.ico'

a = Analysis(
    [str(PROJECT_ROOT / 'main.py')],
    pathex=[str(PROJECT_ROOT)],
    binaries=_extra_binaries,
    datas=[
        # Include templates/ as a data directory.
        # In frozen mode, main.py references BASE_DIR / "templates" / "index.html"
        # which resolves under sys._MEIPASS automatically.
        (str(base_path / 'templates'), 'templates'),
        # Include the tray icon so it loads on first run.
        (str(ICON_PATH), '.') if ICON_PATH.exists() else None,
    ],
    hiddenimports=[
        'yt_dlp',
        'yt_dlp.utils',
        'yt_dlp.options',
        'yt_dlp.downloader',
        'yt_dlp.extractor',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        'starlette',
        'starlette.routing',
        'starlette.responses',
        'starlette.middleware',
        'fastapi',
        'jinja2',
        'anyio',
        'click',
        'imageio_ffmpeg',
        'pystray',
        'pystray._win32',
        'pystray._macosx',
        'pystray._xorg',
        'PIL',
        'PIL.Image',
        'mutagen',
        'mutagen.id3',
        'mutagen.mp3',
    ],
    hookspath=[],
    hooksconfig={},
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='yt_dlp_gundam',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,         # Frozen exe: no console window (tray is the only UI)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON_PATH) if ICON_PATH.exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='yt_dlp_gundam',
)