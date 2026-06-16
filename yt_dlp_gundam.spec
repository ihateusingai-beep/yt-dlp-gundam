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

# Resolve at build time — works on Mac, Windows, Linux, and GitHub Actions runners.
# The workflow calls Set-Location $env:GITHUB_WORKSPACE before pyinstaller,
# so os.getcwd() gives us the repo root.
PROJECT_ROOT = Path(os.getcwd()).resolve()

# When frozen (running as .exe), templates live under sys._MEIPASS.
# Normal dev: templates/ is next to main.py.
if getattr(sys, 'frozen', False):
    base_path = Path(sys._MEIPASS)
else:
    base_path = PROJECT_ROOT

a = Analysis(
    [str(PROJECT_ROOT / 'main.py')],
    pathex=[str(PROJECT_ROOT)],
    binaries=[
        # Bundle yt-dlp's bundled ffmpeg binary if present
        # (yt-dlp ships a static ffmpeg; PyInstaller will find it automatically
        # via its hook mechanism — no extra entry needed here.)
    ],
    datas=[
        # Include templates/ as a data directory.
        # In frozen mode, main.py references BASE_DIR / "templates" / "index.html"
        # which resolves under sys._MEIPASS automatically.
        (str(base_path / 'templates'), 'templates'),
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
    console=True,          # keep console so FFmpeg errors are visible
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
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