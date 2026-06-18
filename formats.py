"""
Format helpers for yt-dlp Gundam dashboard.

Extracted from main.py v0.8.1 — pure functions, no side effects, no FastAPI
imports. Used by the /api/info and /api/download endpoints to project raw
yt-dlp dicts into the minimal shape the frontend needs.
"""
from __future__ import annotations

# --------------------------------------------------------------------------- #
# Constants — single source of truth for format-related enumerations.
# Frontend dropdowns are populated from these sets; if you change one, change
# the matching JS code in templates/index.html too.
# --------------------------------------------------------------------------- #

# Video heights the dashboard exposes in the quality dropdown.
# Order is significant: best → worst.
VIDEO_HEIGHTS = ("2160", "1080", "720", "480", "360", "240", "144")

# Audio bitrates the dashboard exposes in the MP3 quality dropdown.
# Order is significant: best → worst.
AUDIO_BITRATES = ("320", "192", "128", "96", "64")

# Format strings that mean "audio extraction" (no video stream kept).
AUDIO_FORMATS = ("mp3", "audio")


# --------------------------------------------------------------------------- #
# Format classification
# --------------------------------------------------------------------------- #

def is_audio_only(f: dict) -> bool:
    """True if `f` is a true audio-only stream (acodec present, no video)."""
    return f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")


def is_video_only(f: dict) -> bool:
    """True if `f` is a true video-only stream (vcodec present, no audio)."""
    return f.get("vcodec") not in (None, "none") and (f.get("acodec") in (None, "none"))


# --------------------------------------------------------------------------- #
# Per-format projection
# --------------------------------------------------------------------------- #

def best_video_format(formats: list) -> dict:
    """Pick the best video format by (max height, max tbr). Returns {} if
    no video-bearing format is present."""
    def _sort_key(f):
        h = f.get("height") or 0
        tbr = f.get("tbr") or 0
        return (h, tbr)

    return max(
        (f for f in formats if f.get("vcodec") and f.get("vcodec") != "none"),
        key=_sort_key,
        default={},
    )


def resolution_label(f: dict) -> str:
    """Human-readable label: '1080p', '720p', 'audio_xyz', or '<w>x<h>'."""
    h = f.get("height")
    if h:
        return f"{h}p"
    note = f.get("format_note")
    if note:
        return note
    w = f.get("width")
    return f"{w}x{f.get('height')}" if w else "N/A"


def filesize_of(f: dict) -> int:
    """Return the best-known filesize for `f`: filesize → filesize_approx → 0."""
    return f.get("filesize") or f.get("filesize_approx") or 0


def duration_str(seconds) -> str:
    """Format seconds as 'H:MM:SS' (or 'M:SS' for <1h), or 'N/A' if unknown."""
    if not seconds:
        return "N/A"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# --------------------------------------------------------------------------- #
# Aggregated "available" block for /api/info
# --------------------------------------------------------------------------- #

def available_qualities(formats: list) -> dict:
    """Compute the {mp4, webm, audio, video_heights, audio_bitrates} block
    the frontend dropdowns consume. `audio_bitrates` is sorted descending
    (best quality first) so the dropdown reads top-down naturally."""
    video_heights = sorted({
        f.get("height") for f in formats
        if f.get("height") and f.get("vcodec") not in (None, "none")
    }, reverse=True)
    audio_bitrates = sorted({
        int(f.get("abr") or 0) for f in formats
        if is_audio_only(f) and (f.get("abr") or 0) > 0
    }, reverse=True)
    has_mp4 = any(f.get("ext") == "mp4" and f.get("vcodec") not in (None, "none") for f in formats)
    has_webm = any(f.get("ext") == "webm" and f.get("vcodec") not in (None, "none") for f in formats)
    has_audio = any(is_audio_only(f) for f in formats)
    return {
        "mp4": has_mp4,
        "webm": has_webm,
        "audio": has_audio,
        "video_heights": video_heights,
        "audio_bitrates": audio_bitrates,
    }


# --------------------------------------------------------------------------- #
# yt-dlp format selector
# --------------------------------------------------------------------------- #

def build_format_selector(fmt: str, q: str) -> str:
    """Map a (fmt, q) frontend pair to a yt-dlp format selector string.

    fmt: "mp4" / "webm" / "video" (container hint) or "mp3"/"audio" (extract).
    q:   "2160"/"1080"/"720"/... (max video height) or
         "320"/"192"/"128"/... (max audio kbps) or "best" (no constraint).
    """
    # Video heights
    if q in VIDEO_HEIGHTS:
        h = q
        if fmt == "mp4":
            return f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/best[height<={h}][ext=mp4]/best"
        if fmt == "webm":
            return f"bestvideo[height<={h}][ext=webm]+bestaudio[ext=webm]/best[height<={h}][ext=webm]/best"
        if fmt == "video":
            return f"bestvideo[height<={h}]+bestaudio/best[height<={h}]"
    # Audio bitrates (use for MP3)
    if q in AUDIO_BITRATES:
        br = q
        if fmt in AUDIO_FORMATS:
            return f"bestaudio[abr<={br}]/bestaudio/best"
    # Fallback (no quality constraint)
    if fmt in AUDIO_FORMATS:
        return "bestaudio/best"
    if fmt == "webm":
        return "bestvideo[ext=webm]+bestaudio[ext=webm]/best[ext=webm]/best"
    if fmt == "mp4":
        return "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
    # default 'best'
    return "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"


def is_audio_extract(fmt: str) -> bool:
    """True if the user wants audio extracted (no video stream kept)."""
    return fmt in AUDIO_FORMATS
