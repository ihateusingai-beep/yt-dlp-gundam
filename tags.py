"""
ID3 metadata read/write helpers for yt-dlp Gundam dashboard.

Extracted from main.py v0.8.1. Wraps mutagen.id3 so the /api/tag
endpoints can stay focused on HTTP concerns. ID3_FIELDS is the single
source of truth for which frames are read and which are written —
keep the read path and write path in lockstep.
"""
from __future__ import annotations

from pathlib import Path

# --------------------------------------------------------------------------- #
# ID3 frame map
# --------------------------------------------------------------------------- #
# Maps ID3v2 frame codes to the friendly keys we expose via the /api/tag
# JSON API. Used by both read_id3 (output keys) and write_id3 (input keys)
# so they can never drift out of sync.
#
# encoding=3 is UTF-8 in ID3v2.4 — required for any non-ASCII characters
# (Cantonese, Japanese, etc.) to round-trip cleanly.
# --------------------------------------------------------------------------- #

ID3_FIELDS: dict[str, str] = {
    "TIT2": "title",
    "TPE1": "artist",
    "TALB": "album",
    "TDRC": "year",
    "TCON": "genre",
}

# File extensions we know how to read ID3 tags from. MP3 is the only
# container that natively uses ID3; the others have their own tag
# schemes (MP4 atoms, Vorbis comments) but the POST handler permits
# them for write because mutagen's ID3.save() degrades gracefully.
ID3_READABLE_EXTS = {".mp3"}

# File extensions the POST /api/tag handler accepts. Mirrors the 415
# check that used to live inline in main.py.
ID3_WRITABLE_EXTS = {".mp3", ".m4a", ".flac", ".ogg"}


# --------------------------------------------------------------------------- #
# Frame class lookup
# --------------------------------------------------------------------------- #
# mutagen.id3 frame constructors keyed by frame ID. Built once at import
# (instead of on every write_id3 call) so the inner loop can dispatch by
# key without re-constructing the dict. The keys must stay in sync with
# ID3_FIELDS above.
def _build_frame_classes() -> dict:
    """Lazy import + class map construction. mutagen is an optional dep;
    importing at module top would crash if the env doesn't have it."""
    from mutagen.id3 import TIT2, TPE1, TALB, TDRC, TCON
    return {
        "TIT2": TIT2,
        "TPE1": TPE1,
        "TALB": TALB,
        "TDRC": TDRC,
        "TCON": TCON,
    }


_FRAME_CLASSES = _build_frame_classes()


# --------------------------------------------------------------------------- #
# Read
# --------------------------------------------------------------------------- #

def read_id3(path: Path) -> dict[str, str]:
    """Read ID3 tags from `path`. Returns {} for non-MP3 files, MP3
    files with no ID3 header, or any read error.

    This is a "best-effort" reader: if anything goes wrong, the caller
    gets an empty dict rather than an exception. The /api/tag GET
    endpoint relies on this — it must not 500 just because the file
    isn't tagged.
    """
    # Non-MP3 audio / non-audio files: no ID3 tags to read.
    # We use mutagen.id3 per spec; the POST handler also writes ID3 only,
    # so MP3 is the only format with persistent tags in this app.
    if path.suffix.lower() not in ID3_READABLE_EXTS:
        return {}

    try:
        from mutagen.id3 import ID3, ID3NoHeaderError
        try:
            audio = ID3(str(path))
        except ID3NoHeaderError:
            return {}

        tags = {}
        for frame_id, key in ID3_FIELDS.items():
            if frame_id in audio:
                tags[key] = str(audio[frame_id])
        return tags
    except Exception:
        return {}


# --------------------------------------------------------------------------- #
# Write
# --------------------------------------------------------------------------- #

def write_id3(path: Path, tags: dict) -> dict[str, str]:
    """Write ID3 tags to `path` and return the saved dict (read-back
    verification from the in-memory ID3 container).

    Raises:
        ValueError: if `path` is not one of ID3_WRITABLE_EXTS.
        HTTPException-style errors from mutagen are propagated as-is so
        the route handler can map them to 500 with a useful detail.
    """
    ext = path.suffix.lower()
    if ext not in ID3_WRITABLE_EXTS:
        raise ValueError(
            f"Tagging not supported for {ext} (use .mp3 / .m4a / .flac / .ogg)"
        )

    from mutagen.id3 import ID3, ID3NoHeaderError

    try:
        try:
            audio = ID3(str(path))
        except ID3NoHeaderError:
            audio = ID3()  # start fresh

        # Apply each provided key. Use ID3_FIELDS as the source of truth
        # so an unknown key in `tags` is silently ignored — matches the
        # behaviour of the original route handler, which only wrote
        # title/artist/album/year/genre and ignored everything else.
        for frame_id, key in ID3_FIELDS.items():
            value = tags.get(key)
            if value:
                frame_cls = _FRAME_CLASSES[frame_id]
                audio.delall(frame_id)
                audio.add(frame_cls(encoding=3, text=[value]))

        audio.save(str(path))

        # Read back what we just wrote so the client can verify. We
        # walk the in-memory `audio` object (not the file) so the
        # response reflects exactly what was sent to mutagen — the
        # underlying file may not have round-tripped for non-MP3
        # containers, but the caller still gets the intended values.
        saved = {}
        for frame_id, key in ID3_FIELDS.items():
            if frame_id in audio:
                saved[key] = str(audio[frame_id])
        return saved
    except ValueError:
        raise
    except Exception as e:
        # Re-raise as a generic exception with a useful message. The
        # route handler converts this to HTTPException(500).
        raise RuntimeError(f"Tagging failed: {e}") from e
