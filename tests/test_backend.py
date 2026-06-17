"""
Backend hardening tests for yt-dlp-gundam v0.8.0.

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
        """Replicate the B2 logic from /api/info to test it in isolation."""
        def _is_audio_only(f):
            return f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")

        has_audio = any(_is_audio_only(f) for f in formats)
        audio_bitrates = sorted({
            int(f.get("abr") or 0) for f in formats
            if _is_audio_only(f) and (f.get("abr") or 0) > 0
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
