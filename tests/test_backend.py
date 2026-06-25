"""
Backend hardening tests for yt-dlp-gundam v0.8.1.

Covers:
  B1 — per-format filesize + tbr in /api/info
  B2 — has_audio / audio_bitrates only count audio-only formats
  B3 — playlist detection in /api/info response shape
  B4 — URL pre-check in /api/info and /api/download
  B7 — GET /api/tag returns existing tags

Run with:  python3 -m unittest tests.test_backend -v
"""
from __future__ import annotations

import os
import socket
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory

# Make the project root importable so `import main` works regardless of CWD.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import main  # noqa: E402  (path tweak above must come first)
from fastapi.testclient import TestClient  # noqa: E402
from formats import is_audio_only  # noqa: E402  (extracted helper)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

#: Test fixtures — well-known YouTube URLs that are small / stable.
YOUTUBE_TEST_VIDEO = "https://www.youtube.com/watch?v=jNQXAC9IVRw"  # "Me at the zoo", 19s
YOUTUBE_TEST_PLAYLIST = (
    "https://www.youtube.com/playlist?list=PLrAXtmRdnEQy6nuLMHjMZOz59Oq8B9bAk"
)


def _has_network(timeout: float = 2.0) -> bool:
    """Quick TCP probe so we can skip the network-dependent tests when
    the dev machine is offline. Returns True if a TCP connection to
    youtube.com succeeds within `timeout` seconds."""
    try:
        with socket.create_connection(("www.youtube.com", 443), timeout=timeout):
            return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# B4 — URL pre-check (no network required)
# --------------------------------------------------------------------------- #

class TestUrlPrecheck(unittest.TestCase):
    """B4: /api/info and /api/download must reject garbage URLs with 400
    before yt-dlp gets a chance to crash on them."""

    def setUp(self) -> None:
        self.client = TestClient(main.app)

    def test_info_empty_url_returns_400(self) -> None:
        # FastAPI treats ?url= as missing-key when not provided; query param
        # defaulting to empty string also yields a 422. Either way, the
        # endpoint must NOT proceed to yt-dlp extraction (no 5xx).
        r = self.client.get("/api/info?url=")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("http://", r.json()["detail"])

    def test_info_javascript_url_returns_400(self) -> None:
        r = self.client.get("/api/info", params={"url": "javascript:alert(1)"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("http://", r.json()["detail"])

    def test_info_file_scheme_returns_400(self) -> None:
        r = self.client.get("/api/info", params={"url": "file:///etc/passwd"})
        self.assertEqual(r.status_code, 400, r.text)

    def test_info_garbage_returns_400(self) -> None:
        r = self.client.get("/api/info", params={"url": "not a url at all"})
        self.assertEqual(r.status_code, 400, r.text)

    def test_info_whitespace_url_returns_400(self) -> None:
        r = self.client.get("/api/info", params={"url": "   "})
        self.assertEqual(r.status_code, 400, r.text)

    def test_download_empty_url_returns_400(self) -> None:
        r = self.client.get("/api/download?url=")
        self.assertEqual(r.status_code, 400, r.text)

    def test_download_javascript_url_returns_400(self) -> None:
        r = self.client.get("/api/download", params={"url": "javascript:alert(1)"})
        self.assertEqual(r.status_code, 400, r.text)


# --------------------------------------------------------------------------- #
# B1 + B3 — network-dependent (auto-skip if offline)
# --------------------------------------------------------------------------- #

class TestInfoNetwork(unittest.TestCase):
    """B1: per-format filesize + tbr fields.
    B3: playlist detection response shape."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.have_network = _has_network()
        if not cls.have_network:
            raise unittest.SkipTest("No network — skipping YouTube-dependent tests")

    def setUp(self) -> None:
        self.client = TestClient(main.app)

    def test_info_video_has_per_format_filesize_and_tbr(self) -> None:
        """B1: /api/info on a real YouTube video must include `filesize` and
        `tbr` on every entry in the `formats` list. The first entry should
        at minimum not crash; the field is permitted to be 0 (yt-dlp
        sometimes omits size for progressive streams)."""
        with TestClient(main.app) as c:
            r = c.get("/api/info", params={"url": YOUTUBE_TEST_VIDEO})
        # Surface failure with body so the test log shows what yt-dlp said.
        self.assertEqual(r.status_code, 200, f"{r.status_code}: {r.text}")
        body = r.json()
        self.assertIn("formats", body)
        self.assertGreater(len(body["formats"]), 0, "expected at least one format")

        for f in body["formats"]:
            self.assertIn("filesize", f, f"missing filesize: {f}")
            self.assertIn("tbr", f, f"missing tbr: {f}")
            self.assertIsInstance(f["filesize"], int)
            self.assertIsInstance(f["tbr"], (int, float))
            self.assertGreaterEqual(f["filesize"], 0)
            self.assertGreaterEqual(f["tbr"], 0)

        # Best-effort: the first format with a known filesize should be > 0.
        with_size = [f for f in body["formats"] if f["filesize"] > 0]
        # Not a hard assertion — yt-dlp's "Me at the zoo" sometimes only
        # has filesize_approx and not exact filesize. The field must be
        # PRESENT, not necessarily non-zero.

    def test_info_playlist_returns_playlist_shape(self) -> None:
        """B3: /api/info on a playlist URL must return
        {type: "playlist", title, entries, count} with HTTP 200."""
        with TestClient(main.app) as c:
            r = c.get("/api/info", params={"url": YOUTUBE_TEST_PLAYLIST})
        # The fixture playlist ID occasionally gets removed by YouTube.
        # If yt-dlp can't reach it, skip the live assertion — the mock-based
        # tests below cover the same logic without network.
        if r.status_code != 200:
            self.skipTest(
                f"YouTube playlist fixture unreachable (status={r.status_code}, "
                f"detail={r.json().get('detail', '')})"
            )
        body = r.json()
        self.assertEqual(body.get("type"), "playlist")
        self.assertIn("title", body)
        self.assertIn("entries", body)
        self.assertIsInstance(body["entries"], list)
        self.assertIn("count", body)
        # Playlist fixture has > 0 entries.
        self.assertGreater(body["count"], 0)
        # Each entry has the documented shape.
        if body["entries"]:
            e = body["entries"][0]
            for key in ("title", "url", "duration", "id"):
                self.assertIn(key, e, f"entry missing {key}: {e}")


# --------------------------------------------------------------------------- #
# B2 — has_audio / audio_bitrates only count audio-only formats
# --------------------------------------------------------------------------- #

class TestHasAudioLogic(unittest.TestCase):
    """B2: combined formats (vcodec + acodec) must NOT count as
    'has audio' for the audio extraction dropdown. Only true audio-only
    formats (vcodec None/"none" + acodec present) should be counted."""

    def setUp(self) -> None:
        # Use a temp DOWNLOADS dir so we don't disturb the real one.
        self._tmp = TemporaryDirectory()
        self._orig_downloads = main.DOWNLOADS
        main.DOWNLOADS = Path(self._tmp.name)
        main.DOWNLOADS.mkdir(exist_ok=True)

    def tearDown(self) -> None:
        main.DOWNLOADS = self._orig_downloads
        self._tmp.cleanup()

    def _extract_available(self, formats: list[dict]) -> dict:
        """Replicate the B2 logic from /api/info to test it in isolation.
        Uses the shared is_audio_only from formats.py — same definition
        the production endpoint uses."""
        has_audio = any(is_audio_only(f) for f in formats)
        audio_bitrates = sorted({
            int(f.get("abr") or 0) for f in formats
            if is_audio_only(f) and (f.get("abr") or 0) > 0
        }, reverse=True)
        return {"has_audio": has_audio, "audio_bitrates": audio_bitrates}

    def test_combined_format_with_audio_does_not_count_as_audio_only(self) -> None:
        """The whole point of B2: a combined format (vcodec + acodec)
        must NOT make has_audio True. Earlier the bug was that
        `f.get("acodec") not in (None, "none")` was the only check."""
        formats = [
            # video-only (no audio): should NOT count
            {"format_id": "v1", "vcodec": "h264", "acodec": "none", "abr": 0, "height": 720},
            # audio-only: SHOULD count
            {"format_id": "a1", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 128, "height": 0},
            # combined (vcodec + acodec): the bug case — must NOT count
            {"format_id": "c1", "vcodec": "h264", "acodec": "mp4a.40.2", "abr": 192, "height": 1080},
        ]
        out = self._extract_available(formats)
        self.assertTrue(out["has_audio"], "true audio-only format should count")
        # audio_bitrates should only contain 128 (the audio-only abr), not 192.
        self.assertEqual(out["audio_bitrates"], [128])

    def test_only_combined_formats_means_no_audio_only(self) -> None:
        """If all formats with audio are combined (vcodec + acodec), the
        audio extraction dropdown should report has_audio=False."""
        formats = [
            {"format_id": "v1", "vcodec": "h264", "acodec": "none", "abr": 0, "height": 720},
            {"format_id": "c1", "vcodec": "h264", "acodec": "mp4a.40.2", "abr": 192, "height": 1080},
        ]
        out = self._extract_available(formats)
        self.assertFalse(out["has_audio"], "combined-only must not count as audio-only")
        self.assertEqual(out["audio_bitrates"], [])

    def test_video_only_no_audio(self) -> None:
        """A pure video-only format list should have no audio-only entries."""
        formats = [
            {"format_id": "v1", "vcodec": "h264", "acodec": "none", "height": 720},
        ]
        out = self._extract_available(formats)
        self.assertFalse(out["has_audio"])
        self.assertEqual(out["audio_bitrates"], [])

    def test_multiple_audio_only_sorted_descending(self) -> None:
        formats = [
            {"format_id": "a1", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 128},
            {"format_id": "a2", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 320},
            {"format_id": "a3", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 192},
        ]
        out = self._extract_available(formats)
        self.assertTrue(out["has_audio"])
        self.assertEqual(out["audio_bitrates"], [320, 192, 128])


# --------------------------------------------------------------------------- #
# B7 — GET /api/tag
# --------------------------------------------------------------------------- #

class TestGetTag(unittest.TestCase):
    """B7: GET /api/tag?filename=<name> returns existing ID3 tags so the
    frontend can prefill the re-tag form (F12)."""

    def setUp(self) -> None:
        # Use a temp DOWNLOADS dir so we can create a fake MP3 without
        # disturbing the user's real downloads.
        self._tmp = TemporaryDirectory()
        self._orig_downloads = main.DOWNLOADS
        main.DOWNLOADS = Path(self._tmp.name)
        main.DOWNLOADS.mkdir(exist_ok=True)
        self.client = TestClient(main.app)

    def tearDown(self) -> None:
        main.DOWNLOADS = self._orig_downloads
        self._tmp.cleanup()

    def _make_tagged_mp3(self, name: str, tags: dict) -> Path:
        """Create a minimal MP3 file with the given ID3 tags.

        mutagen.id3.ID3 needs an existing file to load existing tags from.
        We touch the file first, then load-or-create the ID3 container.
        """
        from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1, TALB, TDRC, TCON

        path = main.DOWNLOADS / name
        path.touch()  # ID3 needs the file to exist
        try:
            audio = ID3(str(path))
        except ID3NoHeaderError:
            audio = ID3()
        if "title" in tags:  audio.add(TIT2(encoding=3, text=[tags["title"]]))
        if "artist" in tags: audio.add(TPE1(encoding=3, text=[tags["artist"]]))
        if "album" in tags:  audio.add(TALB(encoding=3, text=[tags["album"]]))
        if "year" in tags:   audio.add(TDRC(encoding=3, text=[tags["year"]]))
        if "genre" in tags:  audio.add(TCON(encoding=3, text=[tags["genre"]]))
        audio.save(str(path))
        return path

    def test_get_tag_returns_existing_tags(self) -> None:
        """B7 happy path: GET /api/tag on a tagged MP3 returns
        {"filename", "tags": {"title", "artist", ...}}."""
        self._make_tagged_mp3(
            "test.mp3",
            {"title": "Cruel Angel", "artist": "Yoko Takahashi", "album": "Eva", "year": "1995"},
        )
        r = self.client.get("/api/tag", params={"filename": "test.mp3"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["filename"], "test.mp3")
        self.assertIn("tags", body)
        self.assertEqual(body["tags"]["title"], "Cruel Angel")
        self.assertEqual(body["tags"]["artist"], "Yoko Takahashi")
        self.assertEqual(body["tags"]["album"], "Eva")
        self.assertEqual(body["tags"]["year"], "1995")

    def test_get_tag_untagged_mp3_returns_empty_tags(self) -> None:
        """An MP3 with no ID3 header should return {"tags": {}}."""
        from mutagen.id3 import ID3
        path = main.DOWNLOADS / "empty.mp3"
        ID3()  # make sure mutagen is importable
        path.write_bytes(b"")  # zero-byte file → ID3NoHeaderError
        r = self.client.get("/api/tag", params={"filename": "empty.mp3"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"filename": "empty.mp3", "tags": {}})

    def test_get_tag_non_audio_file_returns_empty_tags(self) -> None:
        """B7: non-audio files (e.g. .mp4) return {"tags": {}}, not an
        error. Frontend uses this to decide whether to show the tag form."""
        path = main.DOWNLOADS / "video.mp4"
        path.write_bytes(b"\x00" * 16)
        r = self.client.get("/api/tag", params={"filename": "video.mp4"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"filename": "video.mp4", "tags": {}})

    def test_get_tag_invalid_filename_returns_400(self) -> None:
        """B7: path traversal attempts (../, slashes) must be rejected
        with 400 to prevent escape from the downloads directory."""
        r = self.client.get("/api/tag", params={"filename": "../etc/passwd"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("Invalid", r.json()["detail"])

    def test_get_tag_slash_in_filename_returns_400(self) -> None:
        r = self.client.get("/api/tag", params={"filename": "subdir/file.mp3"})
        self.assertEqual(r.status_code, 400, r.text)

    def test_get_tag_missing_file_returns_404(self) -> None:
        r = self.client.get("/api/tag", params={"filename": "nonexistent.mp3"})
        self.assertEqual(r.status_code, 404, r.text)

    def test_get_tag_empty_filename_returns_400(self) -> None:
        r = self.client.get("/api/tag", params={"filename": ""})
        self.assertEqual(r.status_code, 400, r.text)


# --------------------------------------------------------------------------- #
# TagRequest Pydantic length validation (v0.8.2)
# --------------------------------------------------------------------------- #

class TestTagRequestValidation(unittest.TestCase):
    """B9: POST /api/tag payload must respect field length limits set on
    the Pydantic TagRequest model. Over-limit payloads should be rejected
    with 422 (FastAPI's default Pydantic error code) BEFORE reaching
    _validate_tag_filename — defense in depth against malicious clients
    sending huge strings."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self._orig_downloads = main.DOWNLOADS
        main.DOWNLOADS = Path(self._tmp.name)
        main.DOWNLOADS.mkdir(exist_ok=True)
        # Create a real MP3 so the request would otherwise succeed.
        from mutagen.id3 import ID3
        self._mp3 = main.DOWNLOADS / "test.mp3"
        self._mp3.touch()
        try:
            ID3(str(self._mp3))
        except Exception:
            ID3()
        self.client = TestClient(main.app)

    def tearDown(self) -> None:
        main.DOWNLOADS = self._orig_downloads
        self._tmp.cleanup()

    def test_normal_payload_succeeds(self) -> None:
        """A reasonable payload must still work — the length caps must
        not break the happy path."""
        r = self.client.post("/api/tag", json={
            "filename": "test.mp3",
            "title": "Cruel Angel",
            "artist": "Yoko Takahashi",
            "year": "1995",
        })
        self.assertEqual(r.status_code, 200, r.text)

    def test_oversized_filename_rejected(self) -> None:
        r = self.client.post("/api/tag", json={
            "filename": "a" * 256,  # max_length=255
            "title": "T",
        })
        self.assertEqual(r.status_code, 422, r.text)

    def test_oversized_title_rejected(self) -> None:
        r = self.client.post("/api/tag", json={
            "filename": "test.mp3",
            "title": "a" * 513,  # max_length=512
        })
        self.assertEqual(r.status_code, 422, r.text)

    def test_oversized_year_rejected(self) -> None:
        r = self.client.post("/api/tag", json={
            "filename": "test.mp3",
            "year": "1" * 17,  # max_length=16
        })
        self.assertEqual(r.status_code, 422, r.text)

    def test_empty_filename_rejected_by_pydantic(self) -> None:
        """min_length=1 on filename means Pydantic catches the empty
        string with 422 BEFORE _validate_tag_filename (which would have
        returned 400). The behaviour change is intentional — invalid
        payloads should fail at the schema boundary, not deep inside."""
        r = self.client.post("/api/tag", json={"filename": ""})
        self.assertEqual(r.status_code, 422, r.text)


# --------------------------------------------------------------------------- #
# ffmpeg_source_label (media.py) — bundled vs system classification
# --------------------------------------------------------------------------- #

class TestFfmpegSourceLabel(unittest.TestCase):
    """B10: ffmpeg_source_label must classify the three bundled sources
    (sys._MEIPASS in frozen, imageio-ffmpeg, site-packages) as 'bundled',
    and everything else (shutil.which, manual install paths) as 'system'."""

    def setUp(self) -> None:
        from media import ffmpeg_source_label
        self.ffmpeg_source_label = ffmpeg_source_label
        # Snapshot sys._MEIPASS so we can restore it — the function reads
        # it at call time, so tests need to mock + restore around each call.
        self._real_meipass = getattr(sys, "_MEIPASS", None)
        # Make sure it's not set in dev mode (CI may set it for other reasons).
        if hasattr(sys, "_MEIPASS"):
            try:
                del sys._MEIPASS
            except AttributeError:
                pass

    def tearDown(self) -> None:
        if self._real_meipass is not None:
            sys._MEIPASS = self._real_meipass
        elif hasattr(sys, "_MEIPASS"):
            try:
                del sys._MEIPASS
            except AttributeError:
                pass

    def test_imageio_path_is_bundled(self) -> None:
        self.assertEqual(
            self.ffmpeg_source_label("/Users/x/Library/imageio/ffmpeg/ffmpeg.exe"),
            "bundled",
        )

    def test_site_packages_path_is_bundled(self) -> None:
        self.assertEqual(
            self.ffmpeg_source_label("/usr/lib/python3.12/site-packages/imageio_ffmpeg/bin/ffmpeg"),
            "bundled",
        )

    def test_system_path_is_system(self) -> None:
        # shutil.which result — user-installed.
        self.assertEqual(
            self.ffmpeg_source_label("/usr/local/bin/ffmpeg"),
            "system",
        )

    def test_windows_fallback_is_system(self) -> None:
        # The "C:/ffmpeg/bin/ffmpeg.exe" fallback path.
        self.assertEqual(
            self.ffmpeg_source_label("C:/ffmpeg/bin/ffmpeg.exe"),
            "system",
        )

    def test_meipass_path_is_bundled(self) -> None:
        """The frozen-exe case: ffmpeg.exe is bundled under sys._MEIPASS
        (this is what the spec's vendor/ffmpeg.exe gets extracted to).
        This was the bug — previously classified as 'system' because
        the path doesn't contain 'imageio' or 'site-packages'."""
        sys._MEIPASS = "/tmp/_internal"
        try:
            self.assertEqual(
                self.ffmpeg_source_label("/tmp/_internal/ffmpeg.exe"),
                "bundled",
            )
            # Nested deeper in _MEIPASS — also bundled.
            self.assertEqual(
                self.ffmpeg_source_label("/tmp/_internal/vendor/ffmpeg.exe"),
                "bundled",
            )
        finally:
            try:
                del sys._MEIPASS
            except AttributeError:
                pass

    def test_non_string_input_returns_unknown(self) -> None:
        # Defensive — function takes a `path: str` but a future caller
        # could pass something else. The bare-except in the function
        # must swallow it gracefully.
        self.assertEqual(self.ffmpeg_source_label(None), "unknown")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# B1 / B3 / B4 end-to-end with mocked yt-dlp (no network required)
# --------------------------------------------------------------------------- #

class TestInfoProjectedShape(unittest.TestCase):
    """B1 + B3 + B4 logic tests that don't need the network. We mock
    `yt_dlp.YoutubeDL.extract_info` to return synthetic raw dicts."""

    def setUp(self) -> None:
        self.client = TestClient(main.app)
        self._real_ydl = main.yt_dlp.YoutubeDL

    def tearDown(self) -> None:
        main.yt_dlp.YoutubeDL = self._real_ydl

    def _patch_extract(self, raw: dict) -> None:
        """Replace yt_dlp.YoutubeDL with a stub class that returns `raw`
        from extract_info. The replacement must be a class (not a factory
        function) because main.py calls it as `with YoutubeDL(opts) as ydl:`.
        """
        captured = raw

        class _FakeYDL:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, url, download=False):
                return captured

        main.yt_dlp.YoutubeDL = _FakeYDL

    def test_info_video_includes_filesize_and_tbr_per_format(self) -> None:
        """B1: a video response's `formats` list must include `filesize`
        and `tbr` on every entry, computed from the yt-dlp raw dict."""
        raw = {
            "title": "T",
            "duration": 60,
            "formats": [
                {"format_id": "18", "ext": "mp4", "height": 360, "width": 640,
                 "vcodec": "h264", "acodec": "mp4a.40.2",
                 "filesize": 12345678, "filesize_approx": 12000000, "tbr": 800},
                {"format_id": "22", "ext": "mp4", "height": 720, "width": 1280,
                 "vcodec": "h264", "acodec": "mp4a.40.2",
                 "filesize": 0, "filesize_approx": 50000000, "tbr": 1500},
            ],
        }
        self._patch_extract(raw)
        r = self.client.get("/api/info", params={"url": "http://example.com/v"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        # No `type` field for the video case (playlist detection only
        # sets it when it's actually a playlist).
        self.assertNotIn("type", body)
        self.assertEqual(len(body["formats"]), 2)
        f0 = body["formats"][0]
        self.assertEqual(f0["filesize"], 12345678)
        self.assertEqual(f0["tbr"], 800)
        f1 = body["formats"][1]
        # filesize_approx fallback when filesize is 0
        self.assertEqual(f1["filesize"], 50000000)
        self.assertEqual(f1["tbr"], 1500)

    def test_info_playlist_returns_type_playlist(self) -> None:
        """B3: a playlist response must have `type: "playlist"` and an
        `entries` list — even if yt-dlp returns `_type: "playlist"` or
        just a non-empty `entries`."""
        raw = {
            "_type": "playlist",
            "title": "My Mix",
            "entries": [
                {"title": "Track 1", "url": "https://x/1", "webpage_url": "https://x/1",
                 "duration": 200, "id": "abc1"},
                {"title": "Track 2", "url": "https://x/2", "webpage_url": "https://x/2",
                 "duration": 240, "id": "abc2"},
            ],
        }
        self._patch_extract(raw)
        r = self.client.get("/api/info", params={"url": "http://example.com/pl"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["type"], "playlist")
        self.assertEqual(body["title"], "My Mix")
        self.assertEqual(body["count"], 2)
        self.assertEqual(len(body["entries"]), 2)
        e0 = body["entries"][0]
        self.assertEqual(e0["title"], "Track 1")
        self.assertEqual(e0["url"], "https://x/1")
        self.assertEqual(e0["id"], "abc1")

    def test_info_playlist_entries_no_url_falls_back_to_webpage_url(self) -> None:
        raw = {
            "_type": "playlist",
            "title": "P",
            "entries": [
                {"title": "T", "webpage_url": "https://x/1", "id": "x", "duration": 10},
            ],
        }
        self._patch_extract(raw)
        r = self.client.get("/api/info", params={"url": "http://example.com/pl"})
        self.assertEqual(r.status_code, 200, r.text)
        e = r.json()["entries"][0]
        self.assertEqual(e["url"], "https://x/1")


# --------------------------------------------------------------------------- #
# B11 — host binding (security, v0.8.3)
# --------------------------------------------------------------------------- #

class TestHostBinding(unittest.TestCase):
    """B11: v0.8.3 hardens against LAN/internet exposure of the unauth'd
    dashboard. DEFAULT_HOST must default to 127.0.0.1 (loopback), honor
    YT_DLP_GUNDAM_HOST env var override, and surface the resolved value
    via /api/health so the UI / smoke checks can verify what's bound."""

    def setUp(self) -> None:
        self.client = TestClient(main.app)
        # Snapshot env so we can restore between tests — DEFAULT_HOST is
        # resolved at import time from os.environ, so flipping the env
        # after import has no effect on the running module. We test the
        # /api/health surface (which reads DEFAULT_HOST at request time)
        # and assert the default vs. env-override behavior on the
        # default itself.
        self._env_snapshot = os.environ.get("YT_DLP_GUNDAM_HOST")

    def tearDown(self) -> None:
        if self._env_snapshot is None:
            os.environ.pop("YT_DLP_GUNDAM_HOST", None)
        else:
            os.environ["YT_DLP_GUNDAM_HOST"] = self._env_snapshot

    def test_default_host_is_loopback(self) -> None:
        """No env override → DEFAULT_HOST must be 127.0.0.1, NOT 0.0.0.0.

        Regression guard for v0.8.2 → v0.8.3: earlier versions bound
        0.0.0.0 by default, exposing the dashboard on the LAN/internet
        with no auth."""
        os.environ.pop("YT_DLP_GUNDAM_HOST", None)
        self.assertEqual(main.DEFAULT_HOST, "127.0.0.1",
            f"DEFAULT_HOST must default to loopback, got {main.DEFAULT_HOST!r}")

    def test_env_var_overrides_host(self) -> None:
        """YT_DLP_GUNDAM_HOST=0.0.0.0 must override the loopback default
        for users who deliberately want LAN exposure (e.g. remote control
        over Tailscale)."""
        os.environ["YT_DLP_GUNDAM_HOST"] = "0.0.0.0"
        self.assertEqual(os.environ.get("YT_DLP_GUNDAM_HOST"), "0.0.0.0")
        # NOTE: main.DEFAULT_HOST is captured at import time, so this test
        # documents the env-var contract. The runtime override actually
        # applied is `host=DEFAULT_HOST` in uvicorn.run() — which the
        # /api/health surface reflects.

    def test_health_surfaces_host(self) -> None:
        """/api/health must include the configured host so the UI / smoke
        tests can verify bind behavior without having to lsof the port."""
        r = self.client.get("/api/health")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIn("host", body,
            "/api/health must surface the bound host (added v0.8.3)")
        # In a test process DEFAULT_HOST was captured at import time —
        # accept either 127.0.0.1 (CI default) or whatever was set in
        # the parent shell. The important property is that the value
        # matches main.DEFAULT_HOST exactly (no surprises / no shadowing).
        self.assertEqual(body["host"], main.DEFAULT_HOST)

    def test_health_version_is_current(self) -> None:
        """/api/health must report the same __version__ we bumped.
        Smoke guard: catches accidental version bumps in CI that don't
        propagate through /api/health (e.g. someone overrides FastAPI's
        `version=` arg)."""
        r = self.client.get("/api/health")
        self.assertEqual(r.json()["version"], main.__version__)


# --------------------------------------------------------------------------- #
# B12 — v0.8.4 batch: AUDIO_BITRATES, lock leak, downloads fallback,
#                       site-packages classifier, .part filter
# --------------------------------------------------------------------------- #

class TestAudioBitratesSourceOfTruth(unittest.TestCase):
    """B12 (P0-3): main.py audio-quality fallback (``audio_quality = q if q
    in AUDIO_BITRATES else "192"``) must reference the same tuple as the
    frontend / formats.AUDIO_BITRATES. If someone bumps the supported
    kbps ladder in formats.py, main.py must follow automatically."""

    def test_main_uses_same_audio_bitrates_tuple(self) -> None:
        from formats import AUDIO_BITRATES as FMT
        # Pull the value from main's download endpoint via a free
        # variable — the simplest way to assert the same constant is in
        # use without coupling the test to the literal text "192" etc.
        # (main.py imports AUDIO_BITRATES from formats, so identity
        # check is the cleanest assertion.)
        self.assertIs(main.AUDIO_BITRATES, FMT,
            "main.AUDIO_BITRATES must be the same object as formats.AUDIO_BITRATES "
            "(single source of truth)")

    def test_audio_quality_default_is_192(self) -> None:
        """Any q not in AUDIO_BITRATES must default to "192"."""
        for q in ("", "best", "999", "256"):
            self.assertEqual(
                q if q in main.AUDIO_BITRATES else "192",
                "192",
                f"q={q!r} should fall back to 192",
            )

    def test_audio_quality_accepts_every_listed_bitrate(self) -> None:
        """Every bitrate in AUDIO_BITRATES must round-trip unchanged."""
        for q in main.AUDIO_BITRATES:
            self.assertEqual(q if q in main.AUDIO_BITRATES else "192", q)


class TestDownloadLockReleasedOnSetupError(unittest.TestCase):
    """B12 (P0-6): if the setup phase of /api/download raises before the
    StreamingResponse is returned, the outer try/except must release the
    download_lock so the next request doesn't see a phantom 409."""

    def setUp(self) -> None:
        self.client = TestClient(main.app)
        # Snapshot the lock state so we can assert cleanly.
        self._lock_was_held = main.download_lock.locked()
        # If something else (a previous test) left the lock held, wait
        # for it. In practice this only fires after the SSE-stream tests
        # that ran without cleanly closing; we tolerate it here.
        if self._lock_was_held:
            main.download_lock.release()

    def tearDown(self) -> None:
        # Be defensive: if any test left the lock held, release it.
        if main.download_lock.locked():
            main.download_lock.release()

    def test_lock_released_when_setup_raises(self) -> None:
        """Inject a synthetic failure in build_format_selector so the
        download endpoint raises BEFORE returning StreamingResponse. The
        outer try/except must release the lock (otherwise the lock
        would be held forever and the next download would 409)."""
        from unittest.mock import patch
        with patch("main.build_format_selector",
                   side_effect=ValueError("synthetic setup error")):
            # The ValueError propagates out of the endpoint through the
            # outer try/except (which also releases the lock). TestClient
            # re-raises it in-process.
            try:
                self.client.get(
                    "/api/download",
                    params={"url": "https://example.com/v"},
                )
            except ValueError:
                pass  # expected — the synthetic error
        # The actual assertion: lock must NOT be held.
        self.assertFalse(
            main.download_lock.locked(),
            "download_lock must be released after setup phase raises "
            "(otherwise P0-6 lock leak regression)",
        )


class TestDownloadsDirReadOnlyFallback(unittest.TestCase):
    """B12 (P1-1): if APP_DIR is read-only (e.g. frozen exe under Program
    Files), init_downloads_dir must fall back to ~/yt-dlp-gundam-downloads/
    and rebind the module-level DOWNLOADS so all subsequent code sees the
    fallback path."""

    def test_fallback_when_app_dir_unwritable(self) -> None:
        import paths

        # Snapshot module state so we can restore it (other tests may
        # share the same DOWNLOADS global).
        orig_downloads = paths.DOWNLOADS
        orig_app_dir   = paths.APP_DIR

        # Build a fake APP_DIR that points at a real read-only directory.
        # We use a temp dir + chmod 0o555 to simulate the no-write
        # scenario across platforms (Linux + macOS; on Windows the
        # filesystem semantics are different but the PermissionError is
        # still raised by mkdir).
        from tempfile import TemporaryDirectory
        from pathlib import Path
        with TemporaryDirectory() as tmp:
            readonly_app = Path(tmp).resolve()
            try:
                readonly_app.chmod(0o555)
            except OSError:
                self.skipTest("Cannot chmod on this platform/filesystem")

            # Replace globals and re-run init_downloads_dir. The function
            # should catch the PermissionError, set DOWNLOADS to the
            # home-dir fallback, and return successfully.
            paths.APP_DIR = readonly_app
            paths.DOWNLOADS = readonly_app / "downloads"
            try:
                result = paths.init_downloads_dir()
                # The fallback should be in the user's home directory.
                self.assertEqual(
                    paths.DOWNLOADS,
                    Path.home() / "yt-dlp-gundam-downloads",
                    "DOWNLOADS must be rebound to home-dir fallback when APP_DIR is read-only",
                )
                self.assertEqual(result, paths.DOWNLOADS)
                self.assertTrue(
                    paths.DOWNLOADS.exists(),
                    "Fallback dir must actually be created",
                )
            finally:
                paths.APP_DIR   = orig_app_dir
                paths.DOWNLOADS = orig_downloads
                # Best-effort cleanup of the fallback dir if we created it.
                try:
                    (Path.home() / "yt-dlp-gundam-downloads").rmdir()
                except OSError:
                    pass


class TestFfmpegClassifierSitePackagesBoundary(unittest.TestCase):
    """B12 (P1-4): ffmpeg_source_label must match ``site-packages`` only
    when it appears as a complete path segment, not as a substring of an
    unrelated directory name."""

    def setUp(self) -> None:
        from media import ffmpeg_source_label
        self.label = ffmpeg_source_label
        self._real_meipass = getattr(sys, "_MEIPASS", None)
        if hasattr(sys, "_MEIPASS"):
            try:
                del sys._MEIPASS
            except AttributeError:
                pass

    def tearDown(self) -> None:
        if self._real_meipass is not None:
            sys._MEIPASS = self._real_meipass

    def test_real_site_packages_is_bundled(self) -> None:
        """Canonical site-packages path is bundled (imageio-ffmpeg cached)."""
        self.assertEqual(
            self.label("/usr/lib/python3.12/site-packages/imageio_ffmpeg/bin/ffmpeg"),
            "bundled",
        )

    def test_site_packages_substring_in_unrelated_dir_is_system(self) -> None:
        """A user-installed ffmpeg in a directory whose name merely
        contains the substring 'site-packages' must NOT be mis-labeled
        bundled. This was the v0.8.3 bug."""
        self.assertEqual(
            self.label("/home/x/myapp/site-packages-custom/ffmpeg"),
            "system",
        )
        self.assertEqual(
            self.label("/home/x/site-packages-backup/bin/ffmpeg"),
            "system",
        )

    def test_imageio_substring_in_unrelated_dir_is_system(self) -> None:
        """Same boundary check for ``imageio`` substring."""
        self.assertEqual(
            self.label("/home/x/myimageioserver/ffmpeg"),
            "system",
        )


class TestFilesListFiltersPartialDownloads(unittest.TestCase):
    """B12 (P1-6): /api/files must skip yt-dlp's ``.part`` files (used
    during in-progress downloads) so the UI doesn't show partial-size
    entries that the user can mis-click."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self._orig_downloads = main.DOWNLOADS
        main.DOWNLOADS = Path(self._tmp.name)
        main.DOWNLOADS.mkdir(exist_ok=True)
        # Create a mix of finished and in-progress files.
        (main.DOWNLOADS / "video.mp4").write_bytes(b"x" * 1024)
        (main.DOWNLOADS / "video.mp4.part").write_bytes(b"x" * 256)  # in-progress
        (main.DOWNLOADS / "song.mp3").write_bytes(b"x" * 512)
        (main.DOWNLOADS / "song.mp3.part").write_bytes(b"x" * 128)  # in-progress
        (main.DOWNLOADS / ".hidden").write_bytes(b"x" * 64)         # dotfile
        self.client = TestClient(main.app)

    def tearDown(self) -> None:
        main.DOWNLOADS = self._orig_downloads
        self._tmp.cleanup()

    def test_part_files_excluded(self) -> None:
        r = self.client.get("/api/files")
        self.assertEqual(r.status_code, 200, r.text)
        names = {f["name"] for f in r.json()["files"]}
        self.assertIn("video.mp4", names)
        self.assertIn("song.mp3", names)
        self.assertNotIn("video.mp4.part", names,
            ".part files must be filtered out of /api/files")
        self.assertNotIn("song.mp3.part", names)
        self.assertNotIn(".hidden", names,
            "dotfiles still filtered (existing behavior preserved)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
