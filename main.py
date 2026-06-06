"""
yt-dlp Gundam Dashboard - FastAPI Backend
"""
import asyncio
import json
import os
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
BASE_DIR   = Path(__file__).parent.resolve()
DOWNLOADS  = BASE_DIR / "downloads"
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

# Log on startup
if FFMPEG_PATH:
    print(f"[health] FFmpeg found: {FFMPEG_PATH}  ({get_ffmpeg_version()})")
else:
    print("[health] FFmpeg NOT found – some features may be limited")

# --------------------------------------------------------------------------- #
# FastAPI app
# --------------------------------------------------------------------------- #
app = FastAPI(title="yt-dlp Gundam Dashboard")

# --------------------------------------------------------------------------- #
# Static / HTML
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = BASE_DIR / "index.html"
    if html_path.exists():
        return FileResponse(str(html_path))
    return "<html><body><h1>yt-dlp Gundam Dashboard</h1><p>index.html not found.</p></body></html>"

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
# /api/info  – yt-dlp --dump-json
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
            info_dict = ydl.extract_info(url, download=False)
        return info_dict
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --------------------------------------------------------------------------- #
# /api/download – SSE progress stream via progress_hooks
# --------------------------------------------------------------------------- #
@app.get("/api/download")
async def download(url: str, request: Request):
    if not url:
        raise HTTPException(status_code=400, detail="url query parameter is required")

    queue: asyncio.Queue[str] = asyncio.Queue()

    def make_hook(queue: asyncio.Queue):
        def hook(d: dict):
            # Build a JSON-able dict (strip non-serialisable objects)
            out = {
                "status":       d.get("status", ""),
                "filename":     d.get("filename", ""),
                "tmpfilename":  d.get("tmpfilename", ""),
                "elapsed":      d.get("elapsed", 0),
                "speed":        d.get("speed"),
                "eta":          d.get("eta"),
                "total_bytes":  d.get("total_bytes"),
                "downloaded_bytes": d.get("downloaded_bytes", 0),
                "progress":     d.get("progress", 0),
                "_percent_str": d.get("_percent_str", ""),
                "_speed_str":   d.get("_speed_str", ""),
                "_eta_str":     d.get("_eta_str", ""),
            }
            # Avoid serialisation errors for extra keys we don't need
            # Send as ndjson line
            queue.put_nowait(json.dumps(out))
        return hook

    async def event_generator():
        # Start download in a thread pool to avoid blocking the ASGI thread
        ydl_opts = {
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "outtmpl": str(DOWNLOADS / "%(title)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [make_hook(queue)],
            "noprogress": False,
        }
        if FFMPEG_PATH:
            ydl_opts["ffmpeg_location"] = FFMPEG_PATH

        def do_download():
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])
            except Exception as e:
                queue.put_nowait(json.dumps({"status": "error", "error": str(e)}))
            finally:
                queue.put_nowait(json.dumps({"status": "done"}))

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, do_download)

        # Stream SSE frames
        while True:
            if await request.is_disconnected():
                break
            try:
                data = await asyncio.wait_for(queue.get(), timeout=30)
                yield f"data: {data}\n\n"
                if data.startswith('{"status":"done"'):
                    break
            except asyncio.TimeoutError:
                # Send a heartbeat to keep connection alive
                yield f": heartbeat\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("__main__:app", host="0.0.0.0", port=port, reload=True)