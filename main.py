"""
yt-dlp Gundam Dashboard - FastAPI Backend

v0.8.4 — AUDIO_BITRATES source-of-truth, lock leak safety net, read-only
         APP_DIR fallback, ffmpeg version UTF-8, osascript argv, .part file
         filter, dead-code cleanup
v0.8.3 — security: host binding (default 127.0.0.1) + frontend SSE onerror guard
v0.8.2 — Windows tray toast, Pydantic length validation, lint cleanup
v0.8.1 — refactored helpers into focused modules:
  formats.py    — format classification, projection, yt-dlp selector
  paths.py      — frozen-vs-dev path layout
  media.py      — FFmpeg detection, version, source label
  tags.py       — ID3 read/write
  concurrency.py — asyncio.Lock try-acquire helper (atomic check-and-take)
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
from pydantic import BaseModel, Field

from concurrency import try_acquire_lock
from formats import (
    AUDIO_BITRATES,
    available_qualities,
    best_video_format,
    build_format_selector,
    duration_str,
    filesize_of,
    is_audio_extract,
    resolution_label,
)
from media import FFMPEG_PATH, ffmpeg_source_label, get_ffmpeg_version
from paths import DOWNLOADS, TEMPLATES, init_downloads_dir
from tags import read_id3, write_id3

# --------------------------------------------------------------------------- #
# Versioning
# --------------------------------------------------------------------------- #
# Bump this every time you ship a meaningful change. SemVer:
#   MAJOR — breaking UX change
#   MINOR — new feature
#   PATCH — bug fix / polish / refactor
# The CI workflow reads this and stamps it onto artifact + exe metadata.
__version__ = "0.8.4"

# Ensure DOWNLOADS exists on startup. Idempotent.
init_downloads_dir()

# --------------------------------------------------------------------------- #
# Network bind (security)
# --------------------------------------------------------------------------- #
# v0.8.3 — default to loopback. v0.8.2 and earlier bound 0.0.0.0, which
# exposed the dashboard to the LAN/internet with no auth — anyone on the
# network could trigger downloads on the user's machine. Single-user local
# apps should bind loopback by default. Power users can opt back in to LAN
# exposure by setting YT_DLP_GUNDAM_HOST=0.0.0.0 before launching.
DEFAULT_HOST = os.environ.get("YT_DLP_GUNDAM_HOST", "127.0.0.1")

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

# Log on startup (after app is created so logs are visible). v0.8.4 —
# split into two readable lines instead of one nested f-string with
# string concatenation that required a triple-take to parse.
if FFMPEG_PATH:
    print(f"[health] FFmpeg found: {FFMPEG_PATH} ({get_ffmpeg_version()})")
else:
    print("[health] FFmpeg NOT found – some features may be limited")

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
        "host": DEFAULT_HOST,
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

        # Note: extract_info raises DownloadError on failure rather than
        # returning None, so no None-check is needed here.

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
        raise HTTPException(
            status_code=422,
            detail=_clean_ytdlp_msg(e) or "Download error",
        )
    except HTTPException:
        # Re-raise FastAPI's own HTTPException (e.g. from _validate_url) so
        # the global 500 fallback below doesn't swallow the original status.
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=_clean_ytdlp_msg(e) or "Internal server error",
        )


def _clean_ytdlp_msg(e: Exception | str) -> str:
    """Strip the yt-dlp ``ERROR: [site] `` prefix from a DownloadError
    message so the browser console doesn't mis-render it as an ANSI color
    escape (e.g. ``[0;31, error]``).

    Used by both the DownloadError and generic Exception handlers in
    /api/info — single source of truth for the cleanup regex.
    """
    msg = str(e).strip()
    return re.sub(r"^ERROR:\s*\[[^\]]+\]\s*", "", msg)


# --------------------------------------------------------------------------- #
# /api/download – SSE progress stream via progress_hooks
# --------------------------------------------------------------------------- #
async def _run_download(
    url: str,
    ydl_opts: dict,
    queue: "asyncio.Queue[dict]",
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Async wrapper around the blocking yt-dlp call.

    Runs yt-dlp in a thread executor so the event loop stays responsive
    (the progress_hooks write to ``queue`` via ``call_soon_threadsafe``).
    Cancellation propagation: if the enclosing ``asyncio.Task`` is
    cancelled, the ``await run_in_executor`` raises ``CancelledError``;
    the underlying thread may keep running (Python can't kill threads),
    but the SSE generator can release the download_lock immediately so a
    new request isn't blocked by a half-finished download.
    """
    def do_ydl() -> None:
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
        except Exception as e:
            # Surface unexpected errors as a typed SSE message so the
            # client gets a clean error instead of a silent hang.
            try:
                loop.call_soon_threadsafe(
                    queue.put_nowait,
                    {"status": "error", "error": str(e)},
                )
            except Exception:
                # Queue may be torn down during shutdown; nothing useful
                # we can do from the worker thread.
                pass

    try:
        await loop.run_in_executor(None, do_ydl)
    finally:
        # Always emit a terminal "done" — even on cancellation — so any
        # consumer that hasn't yet observed disconnect can exit. The
        # queue is unbounded, so put_nowait can't fail with QueueFull.
        try:
            loop.call_soon_threadsafe(queue.put_nowait, {"status": "done"})
        except Exception:
            pass


@app.get("/api/download")
async def download(url: str, request: Request, fmt: str = "best", q: str = "best"):
    # B4: URL pre-check — same validation as /api/info.
    _validate_url(url)

    # Bug 1 (TOCTOU) fix: atomic try-acquire. The old
    #   if download_lock.locked(): raise 409
    #   async with download_lock: ...
    # had a race window where two concurrent requests could both see
    # locked()==False and both reach the async-with; the second would
    # hang forever waiting on the first. ``try_acquire_lock`` collapses
    # the check and the take into one operation (timeout=0 = non-blocking).
    if not await try_acquire_lock(download_lock):
        raise HTTPException(status_code=409, detail="A download is already in progress")
    # v0.8.4 lock-leak safety net: if anything in the setup phase
    # (build_format_selector, ydl_opts construction, make_hook closure,
    # etc.) raises BEFORE we return the StreamingResponse, the
    # event_generator's finally block never runs and the lock is held
    # forever. ``_lock_held`` is the truth-source — the generator
    # clears it before releasing the lock; the outer try/except also
    # clears + releases if setup itself fails.
    _lock_held = True
    try:
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

        # Pre-build ydl_opts OUTSIDE the generator so the dict is constructed
        # once. The progress_hooks closure captures `queue` and `main_loop`
        # from the request-handler scope.
        ydl_opts: dict = {
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
            # v0.8.4 — use the AUDIO_BITRATES constant from formats.py as the
            # single source of truth; the hardcoded tuple here was a
            # divergence waiting to happen.
            audio_quality = q if q in AUDIO_BITRATES else "192"
            ydl_opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": audio_quality,
            }]

        # Bug 2 (lock-held-on-disconnect) fix:
        # The lock is acquired above (atomic try-acquire) and released in the
        # generator's ``finally``. The SSE loop is the consumer of both the
        # queue and the client connection: when ``request.is_disconnected()``
        # is true (browser tab closed, network dropped, EventSource closed),
        # we cancel the worker task and exit, which triggers the ``finally``
        # that releases the lock. The next /api/download request can then
        # start immediately instead of waiting for the underlying yt-dlp
        # thread to finish on its own.
        task: "asyncio.Task[None] | None" = None

        async def event_generator():
            nonlocal task, _lock_held
            task = asyncio.create_task(
                _run_download(url, ydl_opts, queue, main_loop),
            )
            try:
                while True:
                    if await request.is_disconnected():
                        # Client gone — cancel the worker so its
                        # ``await run_in_executor`` returns. The finally
                        # below releases the lock.
                        task.cancel()
                        break
                    try:
                        # v0.8.4 — heartbeat tightened from 15s to 5s so
                        # the disconnect-detection loop is more responsive
                        # (worst-case lock-held-after-disconnect window
                        # drops from ~15s to ~5s). Bandwidth cost is
                        # trivial: ``: heartbeat\n\n`` is ~14 bytes.
                        data = await asyncio.wait_for(queue.get(), timeout=5)
                        yield f"data: {json.dumps(data)}\n\n"
                        if data.get("status") in ("done", "error"):
                            break
                    except asyncio.TimeoutError:
                        # Heartbeat every 5 s — keeps proxy connections
                        # alive and gives the disconnect check above a
                        # chance to fire frequently.
                        yield ": heartbeat\n\n"
            finally:
                # Make sure the worker is fully reaped — even on the normal
                # exit path (``done`` message) we await it to surface any
                # unexpected exceptions in logs.
                if task is not None and not task.done():
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        # CancelledError is expected; any other exception was
                        # already routed to the queue as an "error" message.
                        pass
                # Release the lock whether we exited via done/error/disconnect.
                if _lock_held and download_lock.locked():
                    _lock_held = False
                    download_lock.release()

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )
    except BaseException:
        # Setup phase failed (e.g. invalid fmt/q crashed build_format_selector).
        # Release the lock ourselves since the generator never ran. Without
        # this, a failing request would block every subsequent download
        # until process restart.
        if _lock_held and download_lock.locked():
            _lock_held = False
            download_lock.release()
        raise


# --------------------------------------------------------------------------- #
# /api/files – list downloaded files
# --------------------------------------------------------------------------- #
@app.get("/api/files")
async def list_files():
    files = []
    for p in sorted(DOWNLOADS.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if not p.is_file() or p.name.startswith('.'):
            continue
        # v0.8.4 — skip in-progress yt-dlp downloads. yt-dlp writes to
        # ``<final-name>.part`` while streaming; without this filter the
        # file list shows partial-size files (5 MB then jumping to 120 MB
        # mid-stream) and the user clicks them expecting a finished file.
        if p.name.endswith(".part"):
            continue
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
# Field length limits are defense-in-depth: the path-traversal guard in
# _validate_tag_filename already rejects "../" and slashes, and these
# caps stop a malicious client from POSTing a 10MB title or a 1KB year
# string. The numbers are generous (ID3v2 frames are 256MB-capable in
# spec, but anything past a few hundred bytes is almost certainly junk).
class TagRequest(BaseModel):
    filename: str = Field(..., min_length=1, max_length=255)
    title:    str | None = Field(None, max_length=512)
    artist:   str | None = Field(None, max_length=512)
    album:    str | None = Field(None, max_length=512)
    year:     str | None = Field(None, max_length=16)   # "1995" / "2024-Q1" / etc.
    genre:    str | None = Field(None, max_length=128)


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
        # Unsupported extension — ValueError message from tags.write_id3
        # is already user-friendly ("Tagging not supported for .xyz
        # (use .mp3 / .m4a / .flac / .ogg)"), so just pass it through.
        raise HTTPException(status_code=415, detail=str(e))
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
    # `host=DEFAULT_HOST` binds loopback by default (security, v0.8.3);
    # set YT_DLP_GUNDAM_HOST=0.0.0.0 to expose on LAN/internet.
    try:
        uvicorn.run(
            "__main__:app",
            host=DEFAULT_HOST,
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