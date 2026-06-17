"""
yt-dlp Gundam Dashboard - FastAPI Backend
"""
import asyncio
import io
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
from pydantic import BaseModel

# --------------------------------------------------------------------------- #
# Versioning
# --------------------------------------------------------------------------- #
# Bump this every time you ship a meaningful change. SemVer:
#   MAJOR — breaking UX change
#   MINOR — new feature
#   PATCH — bug fix / polish
# The CI workflow reads this and stamps it onto artifact + exe metadata.
__version__ = "0.7.2"

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
# FFmpeg detection — check frozen _MEIPASS first, then system PATH, then
# imageio-ffmpeg's cached path (broken in frozen), then Windows fallbacks.
# --------------------------------------------------------------------------- #
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

FFMPEG_PATH = find_ffmpeg()

# --------------------------------------------------------------------------- #
# FastAPI app
# --------------------------------------------------------------------------- #
app = FastAPI(title="yt-dlp Gundam Dashboard", version=__version__)

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
app = FastAPI(title="yt-dlp Gundam Dashboard", version=__version__)

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
    # Identify ffmpeg source for the user
    ffmpeg_source = "missing"
    if FFMPEG_PATH:
        try:
            if "imageio" in FFMPEG_PATH.lower() or "site-packages" in FFMPEG_PATH:
                ffmpeg_source = "bundled"
            else:
                ffmpeg_source = "system"
        except Exception:
            ffmpeg_source = "unknown"
    return {
        "status": "ok",
        "version": __version__,
        "ffmpeg": {
            "path": FFMPEG_PATH,
            "source": ffmpeg_source,
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

        # B2: audio-only detection. Combined formats (vcodec + acodec)
        # are pre-merged; only count true audio-only streams for has_audio
        # and audio_bitrates. Combined formats with audio track should NOT
        # make the dropdown think "audio extraction is available" — the
        # downloader handles the merge internally.
        def _is_audio_only(f):
            return f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")

        def _is_video_only(f):
            return f.get("vcodec") not in (None, "none") and (f.get("acodec") in (None, "none"))

        # Compute available qualities for the frontend dropdown
        video_heights = sorted({
            f.get("height") for f in formats
            if f.get("height") and f.get("vcodec") not in (None, "none")
        }, reverse=True)
        audio_bitrates = sorted({
            int(f.get("abr") or 0) for f in formats
            if _is_audio_only(f) and (f.get("abr") or 0) > 0
        }, reverse=True)
        has_mp4 = any(f.get("ext") == "mp4" and f.get("vcodec") not in (None, "none") for f in formats)
        has_webm = any(f.get("ext") == "webm" and f.get("vcodec") not in (None, "none") for f in formats)
        has_audio = any(_is_audio_only(f) for f in formats)

        return {
            "title":      raw.get("title", "Unknown"),
            "thumbnail":  raw.get("thumbnail", ""),
            "duration":   duration_str(raw.get("duration")),
            "resolution": resolution_label(best_video),
            "filesize":   fmt_filesize(best_video),
            "available": {
                "mp4":       has_mp4,
                "webm":      has_webm,
                "audio":     has_audio,
                "video_heights":  video_heights,
                "audio_bitrates": audio_bitrates,
            },
            # B1: per-format filesize + tbr so the frontend can show
            # "MP4 — 1080p (~250 MB)" labels in the format picker.
            "formats": [
                {
                    "format_id": f["format_id"],
                    "ext":       f.get("ext", ""),
                    "resolution": resolution_label(f),
                    "vcodec":    f.get("vcodec", "none"),
                    "acodec":    f.get("acodec", "none"),
                    "filesize":  f.get("filesize") or f.get("filesize_approx") or 0,
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
    def build_format(fmt: str, q: str) -> str:
        # Video heights
        if q in ("2160", "1080", "720", "480", "360", "240", "144"):
            h = q
            if fmt == "mp4":
                return f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/best[height<={h}][ext=mp4]/best"
            if fmt == "webm":
                return f"bestvideo[height<={h}][ext=webm]+bestaudio[ext=webm]/best[height<={h}][ext=webm]/best"
            if fmt == "video":
                return f"bestvideo[height<={h}]+bestaudio/best[height<={h}]"
        # Audio bitrates (use for MP3)
        if q in ("320", "192", "128", "96", "64"):
            br = q
            if fmt == "mp3" or fmt == "audio":
                return f"bestaudio[abr<={br}]/bestaudio/best"
        # Fallback (no quality constraint)
        if fmt == "mp3" or fmt == "audio":
            return "bestaudio/best"
        if fmt == "webm":
            return "bestvideo[ext=webm]+bestaudio[ext=webm]/best[ext=webm]/best"
        if fmt == "mp4":
            return "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
        # default 'best'
        return "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"

    ydl_format = build_format(fmt, q)
    is_audio_extract = fmt in ("mp3", "audio")

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
            if is_audio_extract:
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

    # Non-MP3 audio / non-audio files: no ID3 tags to read.
    # We use mutagen.id3 per spec; the POST handler also writes ID3 only,
    # so MP3 is the only format with persistent tags in this app.
    if file_path.suffix.lower() != ".mp3":
        return {"filename": filename, "tags": {}}

    try:
        from mutagen.id3 import ID3, ID3NoHeaderError
    except ImportError:
        raise HTTPException(status_code=500, detail="mutagen not installed")

    try:
        try:
            audio = ID3(str(file_path))
        except ID3NoHeaderError:
            return {"filename": filename, "tags": {}}

        tags = {}
        if "TIT2" in audio: tags["title"]  = str(audio["TIT2"])
        if "TPE1" in audio: tags["artist"] = str(audio["TPE1"])
        if "TALB" in audio: tags["album"]  = str(audio["TALB"])
        if "TDRC" in audio: tags["year"]   = str(audio["TDRC"])
        if "TCON" in audio: tags["genre"]  = str(audio["TCON"])

        return {"filename": filename, "tags": tags}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read tags: {e}")


@app.post("/api/tag")
async def tag_file(req: TagRequest):
    file_path = _validate_tag_filename(req.filename)

    if file_path.suffix.lower() not in (".mp3", ".m4a", ".flac", ".ogg"):
        raise HTTPException(
            status_code=415,
            detail=f"Tagging not supported for {file_path.suffix} (use .mp3 / .m4a / .flac / .ogg)",
        )

    try:
        from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1, TALB, TDRC, TCON
        from mutagen.mp3 import MP3
        from mutagen import File as MutagenFile
    except ImportError:
        raise HTTPException(status_code=500, detail="mutagen not installed")

    try:
        try:
            audio = ID3(str(file_path))
        except ID3NoHeaderError:
            audio = ID3()  # start fresh

        if req.title:  audio.delall("TIT2"); audio.add(TIT2(encoding=3, text=[req.title]))
        if req.artist: audio.delall("TPE1"); audio.add(TPE1(encoding=3, text=[req.artist]))
        if req.album:  audio.delall("TALB"); audio.add(TALB(encoding=3, text=[req.album]))
        if req.year:   audio.delall("TDRC"); audio.add(TDRC(encoding=3, text=[req.year]))
        if req.genre:  audio.delall("TCON"); audio.add(TCON(encoding=3, text=[req.genre]))

        audio.save(str(file_path))

        # Read back what we just wrote so the client can verify
        saved = {}
        if "TIT2" in audio: saved["title"]  = str(audio["TIT2"])
        if "TPE1" in audio: saved["artist"] = str(audio["TPE1"])
        if "TALB" in audio: saved["album"]  = str(audio["TALB"])
        if "TDRC" in audio: saved["year"]   = str(audio["TDRC"])
        if "TCON" in audio: saved["genre"]  = str(audio["TCON"])

        return {"ok": True, "filename": req.filename, "tags": saved}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Tagging failed: {e}")


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