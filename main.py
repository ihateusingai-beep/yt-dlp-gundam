"""
yt-dlp Gundam Dashboard - FastAPI Backend
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
# Frozen (PyInstaller): executable lives in dist/yt_dlp_gundam/yt_dlp_gundam.exe
# downloads/ goes next to the .exe so users can find their files.
if getattr(sys, 'frozen', False):
    # sys.executable is <dist>/yt_dlp_gundam/yt_dlp_gundam.exe
    APP_DIR   = Path(sys.executable).parent.resolve()
    TEMPLATES = Path(sys._MEIPASS) / 'templates'
else:
    APP_DIR   = Path(__file__).parent.resolve()
    TEMPLATES = APP_DIR / 'templates'

BASE_DIR   = APP_DIR
DOWNLOADS  = APP_DIR / 'downloads'
DOWNLOADS.mkdir(exist_ok=True)

# --------------------------------------------------------------------------- #
# FFmpeg detection
# --------------------------------------------------------------------------- #
def find_ffmpeg() -> str | None:
    """Return the path to ffmpeg, or None if not found."""
    path = shutil.which("ffmpeg")
    if path:
        return path
    # Windows fallback – search common install locations
    for candidate in [
        Path("C:/ffmpeg/bin/ffmpeg.exe"),
        Path("C:/Program Files/ffmpeg/bin/ffmpeg.exe"),
        Path("C:/Program Files (x86)/ffmpeg/bin/ffmpeg.exe"),
    ]:
        if candidate.exists():
            return str(candidate)
    return None

FFMPEG_PATH = find_ffmpeg()

# --------------------------------------------------------------------------- #
# FastAPI app
# --------------------------------------------------------------------------- #
app = FastAPI(title="yt-dlp Gundam Dashboard")

# Per-host download lock so we can't have two downloads racing.
download_lock = asyncio.Lock()

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def get_ffmpeg_version() -> str:
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

# Log on startup (after app is created so logs are visible)
print(f"[health] FFmpeg {'found: ' + FFMPEG_PATH + ' (' + get_ffmpeg_version() + ')' if FFMPEG_PATH else 'NOT found – some features may be limited'}")

# --------------------------------------------------------------------------- #
# FastAPI app
# --------------------------------------------------------------------------- #
app = FastAPI(title="yt-dlp Gundam Dashboard")

# --------------------------------------------------------------------------- #
# Static / HTML
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = TEMPLATES / "index.html"
    if html_path.exists():
        return FileResponse(str(html_path))
    return "<html><body><h1>yt-dlp Gundam Dashboard</h1><p>templates/index.html not found.</p></body></html>"

# --------------------------------------------------------------------------- #
# /api/health
# --------------------------------------------------------------------------- #
@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "ffmpeg": {
            "path": FFMPEG_PATH,
            "version": get_ffmpeg_version(),
        },
        "python": sys.version,
        "yt_dlp": yt_dlp.version.__version__,
        "downloads_dir": str(DOWNLOADS),
    }

# --------------------------------------------------------------------------- #
# /api/info  – yt-dlp --dump-json  (projected to minimal set)
# --------------------------------------------------------------------------- #
@app.get("/api/info")
async def info(url: str):
    if not url:
        raise HTTPException(status_code=400, detail="url query parameter is required")

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "dump_single_json": True,
        "flat_playlist": False,
    }
    if FFMPEG_PATH:
        ydl_opts["ffmpeg_location"] = FFMPEG_PATH

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            raw = ydl.extract_info(url, download=False)

        if raw is None:
            raise HTTPException(status_code=422, detail="Could not extract video info")

        # Project to the fields the frontend needs.
        # Handle both single-video and playlist-entry dicts.
        formats = raw.get("formats") or []

        # Pick the highest-quality video format (max height → max tbr).
        # Don't require both vcodec and acodec — combined formats are
        # pre-merged; many videos only have separate streams.
        def _sort_key(f):
            h = f.get("height") or 0
            tbr = f.get("tbr") or 0
            return (h, tbr)

        best_video = max(
            (f for f in formats if f.get("vcodec") and f.get("vcodec") != "none"),
            key=_sort_key,
            default={},
        )

        def fmt_filesize(f):
            fs = f.get("filesize") or f.get("filesize_approx") or 0
            return fs

        def resolution_label(f):
            h = f.get("height")
            if h:
                return f"{h}p"
            note = f.get("format_note")
            if note:
                return note
            w = f.get("width")
            return f"{w}x{f.get('height')}" if w else "N/A"

        def duration_str(seconds):
            if not seconds:
                return "N/A"
            h, rem = divmod(int(seconds), 3600)
            m, s = divmod(rem, 60)
            return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

        return {
            "title":      raw.get("title", "Unknown"),
            "thumbnail":  raw.get("thumbnail", ""),
            "duration":   duration_str(raw.get("duration")),
            "resolution": resolution_label(best_video),
            "filesize":   fmt_filesize(best_video),
            "formats": [
                {
                    "format_id": f["format_id"],
                    "ext":       f.get("ext", ""),
                    "resolution": resolution_label(f),
                    "vcodec":    f.get("vcodec", "none"),
                    "acodec":    f.get("acodec", "none"),
                }
                for f in formats
                if f.get("format_id")
            ],
        }
    except yt_dlp.utils.DownloadError as e:
        # Clean the error: yt-dlp prefixes with "ERROR: [site] " which the
        # browser console mis-renders as an ANSI color escape (e.g. "[0;31, error]").
        msg = str(e).strip()
        msg = re.sub(r"^ERROR:\s*\[[^\]]+\]\s*", "", msg)
        raise HTTPException(status_code=422, detail=msg or "Download error")
    except Exception as e:
        msg = str(e).strip()
        msg = re.sub(r"^ERROR:\s*\[[^\]]+\]\s*", "", msg)
        raise HTTPException(status_code=500, detail=msg or "Internal server error")


# --------------------------------------------------------------------------- #
# /api/download – SSE progress stream via progress_hooks
# --------------------------------------------------------------------------- #
@app.get("/api/download")
async def download(url: str, request: Request, fmt: str = "best"):
    if not url:
        raise HTTPException(status_code=400, detail="url query parameter is required")

    # Concurrent download guard
    if download_lock.locked():
        raise HTTPException(status_code=409, detail="A download is already in progress")

    queue: asyncio.Queue[dict] = asyncio.Queue()

    # Capture the running event loop NOW (on the async side) so the worker
    # thread (which has no event loop of its own) can use call_soon_threadsafe.
    main_loop = asyncio.get_running_loop()

    def make_hook(q: asyncio.Queue[dict]):
        def hook(d: dict):
            out = {
                "status":            d.get("status", ""),
                "filename":          d.get("filename", ""),
                "elapsed":           d.get("elapsed", 0),
                "speed":             d.get("speed"),
                "eta":               d.get("eta"),
                "total_bytes":       d.get("total_bytes"),
                "downloaded_bytes":  d.get("downloaded_bytes", 0),
                "progress":          d.get("progress", 0),
                "_percent_str":      d.get("_percent_str", ""),
                "_speed_str":        d.get("_speed_str", ""),
                "_eta_str":          d.get("_eta_str", ""),
            }
            # Closure-captured loop (not asyncio.get_event_loop) avoids
            # RuntimeError in worker threads on Python 3.10+.
            main_loop.call_soon_threadsafe(q.put_nowait, out)
        return hook

    # Map frontend "best" / "mp4" / "webm" / etc. to yt-dlp format strings.
    FORMAT_MAP = {
        "best":              "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "bestvideo+bestaudio": "bestvideo+bestaudio/best",
        "bestvideo":         "bestvideo/best",
        "bestaudio":         "bestaudio/best",
        "mp4":               "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "webm":              "bestvideo[ext=webm]+bestaudio[ext=webm]/best[ext=webm]/best",
    }
    ydl_format = FORMAT_MAP.get(fmt, "best")

    async def event_generator():
        async with download_lock:
            ydl_opts = {
                "format": ydl_format,
                "outtmpl": str(DOWNLOADS / "%(title)s.%(ext)s"),
                "quiet": True,
                "no_warnings": True,
                "progress_hooks": [make_hook(queue)],
                "noprogress": False,
            }
            if FFMPEG_PATH:
                ydl_opts["ffmpeg_location"] = FFMPEG_PATH

            # main_loop was captured up top (closure for make_hook). Reuse it
            # here to avoid a second asyncio.get_running_loop() call.
            def do_download():
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        ydl.download([url])
                except Exception as e:
                    main_loop.call_soon_threadsafe(
                        queue.put_nowait,
                        {"status": "error", "error": str(e)},
                    )
                finally:
                    main_loop.call_soon_threadsafe(
                        queue.put_nowait,
                        {"status": "done"},
                    )

            await main_loop.run_in_executor(None, do_download)

            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {json.dumps(data)}\n\n"
                    if data.get("status") in ("done", "error"):
                        break
                except asyncio.TimeoutError:
                    # Heartbeat every 15 s to keep proxy connections alive
                    yield f": heartbeat\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    # reload=True only works in dev (non-frozen). Frozen exe must not reload.
    uvicorn.run(
        "__main__:app",
        host="0.0.0.0",
        port=port,
        reload=not getattr(sys, "frozen", False),
    )