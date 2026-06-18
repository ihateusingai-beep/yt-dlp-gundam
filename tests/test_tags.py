"""
Tests for the ID3 read/write helpers in tags.py.

These exercise the extracted module directly (no FastAPI). The /api/tag
endpoints in main.py are covered by tests.test_backend.TestGetTag; this
file covers the unit-level contract of read_id3 and write_id3.

Run with:  python3 -m unittest tests.test_tags -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

# Make the project root importable so `import tags` works regardless of CWD.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tags import read_id3, write_id3  # noqa: E402


class TestReadId3(unittest.TestCase):
    """read_id3 must return {} for non-MP3 / untagged / unreadable files,
    and the populated {title, artist, ...} dict for tagged MP3s."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_read_id3_returns_empty_for_non_mp3(self) -> None:
        """A .mp4 file should return {} without even attempting to read
        — the function must not raise on non-MP3 paths."""
        path = self.tmpdir / "video.mp4"
        path.write_bytes(b"\x00" * 16)
        result = read_id3(path)
        self.assertEqual(result, {})

    def test_read_id3_returns_empty_for_untagged_mp3(self) -> None:
        """An MP3 with no ID3 header (zero bytes is enough) should return
        {} — the ID3NoHeaderError path inside mutagen is swallowed."""
        path = self.tmpdir / "empty.mp3"
        path.write_bytes(b"")  # zero-byte file → ID3NoHeaderError
        result = read_id3(path)
        self.assertEqual(result, {})

    def test_read_id3_returns_dict_for_tagged_mp3(self) -> None:
        """A tagged MP3 should return {title, artist, album, year, genre}
        populated for every frame that was written. The mapping is
        {TIT2→title, TPE1→artist, TALB→album, TDRC→year, TCON→genre}."""
        from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1, TALB, TDRC, TCON

        path = self.tmpdir / "tagged.mp3"
        path.touch()  # ID3 needs the file to exist
        try:
            audio = ID3(str(path))
        except ID3NoHeaderError:
            audio = ID3()
        audio.add(TIT2(encoding=3, text=["Cruel Angel"]))
        audio.add(TPE1(encoding=3, text=["Yoko Takahashi"]))
        audio.add(TALB(encoding=3, text=["Evangelion"]))
        audio.add(TDRC(encoding=3, text=["1995"]))
        audio.add(TCON(encoding=3, text=["Anime"]))
        audio.save(str(path))

        result = read_id3(path)
        self.assertEqual(result.get("title"), "Cruel Angel")
        self.assertEqual(result.get("artist"), "Yoko Takahashi")
        self.assertEqual(result.get("album"), "Evangelion")
        self.assertEqual(result.get("year"), "1995")
        self.assertEqual(result.get("genre"), "Anime")


class TestWriteId3(unittest.TestCase):
    """write_id3 must persist tags to MP3 files and round-trip cleanly.
    It must raise ValueError on unsupported extensions (matching the
    415 check that the /api/tag POST handler uses)."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_write_id3_round_trip(self) -> None:
        """Write tags, then read them back — values must match. This is
        the contract /api/tag POST relies on (it returns the saved dict
        so the client can verify the write)."""
        path = self.tmpdir / "round.mp3"
        path.touch()
        payload = {
            "title":  "A Cruel Angel's Thesis",
            "artist": "Yoko Takahashi",
            "album":  "Neon Genesis Evangelion",
            "year":   "1995",
            "genre":  "Anime",
        }
        saved = write_id3(path, payload)

        # The in-memory read-back (what the route handler returns) should
        # reflect what was sent.
        for key, value in payload.items():
            self.assertEqual(saved.get(key), value, f"saved[{key}] mismatch")

        # The on-disk read should also match — read_id3 walks the same
        # ID3_FIELDS map, so the round-trip is verified end-to-end.
        on_disk = read_id3(path)
        for key, value in payload.items():
            self.assertEqual(on_disk.get(key), value, f"on-disk {key} mismatch")

    def test_write_id3_rejects_unsupported_ext(self) -> None:
        """A .txt file must raise ValueError so the route handler can
        convert to HTTP 415. We never want mutagen to attempt a save
        on a non-audio file."""
        path = self.tmpdir / "notes.txt"
        path.write_text("not audio")
        with self.assertRaises(ValueError):
            write_id3(path, {"title": "should not stick"})

    def test_write_id3_partial_tags(self) -> None:
        """write_id3 should accept a partial payload — only the provided
        keys are written, others are left alone. This matches the
        /api/tag POST semantics where every field is optional."""
        path = self.tmpdir / "partial.mp3"
        path.touch()
        write_id3(path, {"title": "Only Title"})

        on_disk = read_id3(path)
        self.assertEqual(on_disk.get("title"), "Only Title")
        # Other fields are absent (not set, not cleared).
        self.assertNotIn("artist", on_disk)
        self.assertNotIn("album", on_disk)
        self.assertNotIn("year", on_disk)
        self.assertNotIn("genre", on_disk)

    def test_write_id3_overwrites_existing_tags(self) -> None:
        """A second write to the same file with different values must
        replace the previous tags (delall + add). This is the same
        behavior the original main.py POST handler had."""
        path = self.tmpdir / "overwrite.mp3"
        path.touch()
        write_id3(path, {"title": "Old Title", "artist": "Old Artist"})
        write_id3(path, {"title": "New Title"})

        on_disk = read_id3(path)
        self.assertEqual(on_disk.get("title"), "New Title")
        # artist was set in the first write but not the second; the
        # second write should NOT have removed it (only the keys it
        # was given are touched).
        self.assertEqual(on_disk.get("artist"), "Old Artist")


if __name__ == "__main__":
    unittest.main(verbosity=2)
