"""
yt-dlp Gundam Dashboard - FastAPI Backend

v0.8.1 — refactored helpers into focused modules:
  formats.py — format classification, projection, yt-dlp selector
  paths.py   — frozen-vs-dev path layout
  media.py   — FFmpeg detection, version, source label
  tags.py    — ID3 read/write
This file stays focused on the FastAPI surface and the download pipeline.
"""
import asyncio
import io
import json
import os
import re
import sys
from pathlib import Path

import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel

from formats import (
    available_qualities,
    best_video_format,
    build_format_selector,
    duration_str,
    filesize_of,
    is_audio_extract,
    resolution_label,
)
from media import FFMPEG_PATH, ffmpeg_source_label, get_ffmpeg_version
from paths import APP_DIR, BASE_DIR, DOWNLOADS, TEMPLATES, init_downloads_dir
from tags import read_id3, write_id3

# --------------------------------------------------------------------------- #
# Versioning
# --------------------------------------------------------------------------- #
# Bump this every time you ship a meaningful change. SemVer:
#   MAJOR — breaking UX change
#   MINOR — new feature
#   PATCH — bug fix / polish / refactor
# The CI workflow reads this and stamps it onto artifact + exe metadata.
__version__ = "0.8.1"

# Ensure DOWNLOADS exists on startup. Idempotent.
init_downloads_dir()

# --------------------------------------------------------------------------- #
# URL validation
# --------------------------------------------------------------------------- #
# Reject empty strings, javascript:, file://, garbage input before yt-dlp
# gets a chance to crash on it. Must be a non-empty http(s) URL with no
# whitespace.
URL_PATTERN = re.compile(r"^https?://[^\s]+$")


def _validate_url(url: str) -> None:
    """Raise HTTPException(400) if the URL is not a non-empty http(s) URL."""
    if not url or not URL_PATTERN.match(url):
        raise HTTPException(
            status_code=400,
            detail="URL must start with http:// or https://",
        )

# --------------------------------------------------------------------------- #
# FastAPI app
# --------------------------------------------------------------------------- #
app = FastAPI(title="yt-dlp Gundam Dashboard", version=__version__)

# Per-host download lock so we can't have two downloads racing.
download_lock = asyncio.Lock()

# Log on startup (after app is created so logs are visible)
print(f"[health] FFmpeg {'found: ' + FFMPEG_PATH + ' (' + get_ffmpeg_version() + ')' if FFMPEG_PATH else 'NOT found – some features may be limited'}")

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
        "version": __version__,
        "ffmpeg": {
            "path": FFMPEG_PATH,
            "source": ffmpeg_source_label(FFMPEG_PATH) if FFMPEG_PATH else "missing",
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
    # B4: URL pre-check — reject empty / javascript: / file:// / garbage
    # before yt-dlp gets to it.
    _validate_url(url)

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

        # B3: playlist detection — return a different shape for playlists
        # so the frontend can show a friendly "this is a playlist" message.
        # Downloading individual playlist entries is a v0.9.0 feature.
        raw_type = raw.get("_type")
        entries = raw.get("entries")
        if raw_type == "playlist" or (isinstance(entries, list) and len(entries) > 0):
            entries_list = entries if isinstance(entries, list) else []
            return {
                "type":    "playlist",
                "title":   raw.get("title", "Playlist"),
                "entries": [
                    {
                        "title":    e.get("title") if isinstance(e, dict) else None,
                        "url":      (e.get("url") or e.get("webpage_url")) if isinstance(e, dict) else None,
                        "duration": e.get("duration") if isinstance(e, dict) else None,
                        "id":       e.get("id") if isinstance(e, dict) else None,
                    }
                    for e in entries_list[:50]
                    if isinstance(e, dict)
                ],
                "count":   len(entries_list),
            }

        # Project to the fields the frontend needs.
        # Handle both single-video and playlist-entry dicts.
        formats = raw.get("formats") or []
        best_video = best_video_format(formats)

        return {
            "title":      raw.get("title", "Unknown"),
            "thumbnail":  raw.get("thumbnail", ""),
            "duration":   duration_str(raw.get("duration")),
            "resolution": resolution_label(best_video),
            "filesize":   filesize_of(best_video),
            "available":  available_qualities(formats),
            # B1: per-format filesize + tbr so the frontend can show
            # "MP4 — 1080p (~250 MB)" labels in the format picker.
            "formats": [
                {
                    "format_id": f["format_id"],
                    "ext":       f.get("ext", ""),
                    "resolution": resolution_label(f),
                    "vcodec":    f.get("vcodec", "none"),
                    "acodec":    f.get("acodec", "none"),
                    "filesize":  filesize_of(f),
                    "tbr":       f.get("tbr") or 0,
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
    except HTTPException:
        # Re-raise FastAPI's own HTTPException (e.g. from _validate_url) so
        # the global 500 fallback below doesn't swallow the original status.
        raise
    except Exception as e:
        msg = str(e).strip()
        msg = re.sub(r"^ERROR:\s*\[[^\]]+\]\s*", "", msg)
        raise HTTPException(status_code=500, detail=msg or "Internal server error")


# --------------------------------------------------------------------------- #
# /api/download – SSE progress stream via progress_hooks
# --------------------------------------------------------------------------- #
@app.get("/api/download")
async def download(url: str, request: Request, fmt: str = "best", q: str = "best"):
    # B4: URL pre-check — same validation as /api/info.
    _validate_url(url)

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

    # Map frontend fmt string to yt-dlp format selector. The `q` param
    # further refines the selector: video heights (480/720/1080/2160) or
    # audio bitrates (128/192/320) for MP3 extraction.
    # fmt=q (default): yt-dlp picks best available
    ydl_format = build_format_selector(fmt, q)
    extract_audio = is_audio_extract(fmt)

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
            if extract_audio:
                # Extract best audio → transcode to MP3 at user-requested kbps.
                # Requires FFmpeg in PATH (or set ffmpeg_location above).
                audio_quality = q if q in ("320", "192", "128", "96", "64") else "192"
                ydl_opts["postprocessors"] = [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": audio_quality,
                }]

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


# --------------------------------------------------------------------------- #
# /api/files – list downloaded files
# --------------------------------------------------------------------------- #
@app.get("/api/files")
async def list_files():
    files = []
    for p in sorted(DOWNLOADS.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if p.is_file() and not p.name.startswith('.'):
            stat = p.stat()
            files.append({
                "name":     p.name,
                "size":     stat.st_size,
                "modified": stat.st_mtime,
            })
    return {"files": files, "downloads_dir": str(DOWNLOADS)}


# --------------------------------------------------------------------------- #
# /api/folder – return the absolute downloads directory path. The dashboard's
# "Open Folder" button calls this so it can show the user where their files
# landed (and copy the path to the clipboard). We don't try to spawn a file
# manager from the server — the browser handles the user-visible action.
# --------------------------------------------------------------------------- #
@app.get("/api/folder")
async def get_folder():
    return {"path": str(DOWNLOADS)}


# --------------------------------------------------------------------------- #
# /api/files/{name} – download a specific file
# --------------------------------------------------------------------------- #
@app.get("/api/files/{filename}")
async def get_file(filename: str):
    # Reject path traversal: no slashes, no parent refs
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    file_path = DOWNLOADS / filename
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(
        str(file_path),
        filename=filename,
        media_type="application/octet-stream",
    )


# --------------------------------------------------------------------------- #
# /api/tag – read + write ID3 metadata for an audio file
# --------------------------------------------------------------------------- #
class TagRequest(BaseModel):
    filename: str
    title:    str | None = None
    artist:   str | None = None
    album:    str | None = None
    year:     str | None = None
    genre:    str | None = None


def _validate_tag_filename(filename: str) -> Path:
    """Validate the filename and return the resolved path inside DOWNLOADS.

    Rejects path traversal (slashes, parent refs) and 404s on missing files.
    """
    if not filename:
        raise HTTPException(status_code=400, detail="filename is required")
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    file_path = DOWNLOADS / filename
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return file_path


# B7: GET /api/tag?filename=<name> — return existing ID3 tags so the
# frontend can prefill the re-tag form (F12). Non-audio files or files
# without tags return `{"tags": {}}`.
@app.get("/api/tag")
async def get_tag(filename: str):
    file_path = _validate_tag_filename(filename)

    # Surface a clear 500 if mutagen is missing at runtime — the rest of
    # the read path is delegated to tags.read_id3 which is best-effort.
    try:
        import mutagen.id3  # noqa: F401
    except ImportError:
        raise HTTPException(status_code=500, detail="mutagen not installed")

    return {"filename": filename, "tags": read_id3(file_path)}


@app.post("/api/tag")
async def tag_file(req: TagRequest):
    file_path = _validate_tag_filename(req.filename)

    # Surface a clear 500 if mutagen is missing at runtime.
    try:
        import mutagen.id3  # noqa: F401
    except ImportError:
        raise HTTPException(status_code=500, detail="mutagen not installed")

    try:
        saved = write_id3(
            file_path,
            {
                "title":  req.title,
                "artist": req.artist,
                "album":  req.album,
                "year":   req.year,
                "genre":  req.genre,
            },
        )
    except ValueError as e:
        # Unsupported extension — convert to 415 with the same message
        # the original handler used.
        raise HTTPException(
            status_code=415,
            detail=f"Tagging not supported for {file_path.suffix} (use .mp3 / .m4a / .flac / .ogg)"
            if not str(e)
            else str(e),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Tagging failed: {e}")

    return {"ok": True, "filename": req.filename, "tags": saved}


if __name__ == "__main__":
    import uvicorn
    import threading
    import time
    port = int(os.environ.get("PORT", 8000))
    is_frozen = getattr(sys, "frozen", False)

    # Windows frozen exe (PyInstaller `console=False`) starts with
    # `sys.stdout` / `sys.stderr` set to None on some Python/PyInstaller
    # combinations. Uvicorn's default `ColourizedFormatter.__init__` calls
    # `sys.stdout.isatty()` when initializing, which crashes with
    #     AttributeError: 'NoneType' object has no attribute 'isatty'
    # leading to
    #     ValueError: Unable to configure formatter 'default'
    # In dev mode (non-frozen), sys.stdout/stderr are real streams and
    # we keep the normal print() output. In frozen mode, we replace them
    # with an in-memory stream whose `isatty()` returns False — same
    # effect for uvicorn, but the user's dashboard + tray icon still work
    # as the only UI surfaces.
    if is_frozen and (sys.stdout is None or not hasattr(sys.stdout, "isatty")):
        sys.stdout = io.StringIO()
    if is_frozen and (sys.stderr is None or not hasattr(sys.stderr, "isatty")):
        sys.stderr = io.StringIO()

    def open_browser_when_ready(url: str, delay: float = 1.5):
        """Wait for uvicorn to bind, then open the dashboard in the user's
        default browser. Daemon thread so it doesn't block process exit."""
        time.sleep(delay)
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception as e:
            print(f"[health] Could not auto-open browser: {e}")

    if is_frozen:
        # Frozen exe: auto-open browser after server starts
        threading.Thread(
            target=open_browser_when_ready,
            args=(f"http://localhost:{port}",),
            daemon=True,
        ).start()

        # Frozen exe: also start a system tray icon so the user can quit
        # the server cleanly without Task Manager.
        try:
            from tray import run_tray
            stop_event = threading.Event()
            tray_thread = threading.Thread(
                target=run_tray,
                args=(port, stop_event),
                daemon=True,
            )
            tray_thread.start()
        except Exception as e:
            print(f"[health] Could not start tray: {e}")
            stop_event = None
    else:
        stop_event = None

    # reload=True only works in dev (non-frozen). Frozen exe must not reload.
    # `use_colors=False` is belt-and-suspenders for the stdout/stderr
    # redirect above — even if a future uvicorn version checks isatty()
    # elsewhere, this guarantees the formatter stays color-free.
    try:
        uvicorn.run(
            "__main__:app",
            host="0.0.0.0",
            port=port,
            reload=not is_frozen,
            use_colors=False,
        )
    except KeyboardInterrupt:
        pass
    finally:
        # When uvicorn exits (e.g. tray "Quit" caused the loop to end),
        # signal the tray thread to stop so the process can exit cleanly.
        if stop_event is not None:
            stop_event.set()