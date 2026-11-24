from array import array
import random
import tempfile
import unittest
from pathlib import Path

from spliff.recording_match import RATE, compare_pcm, decode
from spliff.catalog import signature


class RecordingMatch(unittest.TestCase):
    def setUp(self):
        randomizer = random.Random(42)
        self.audio = array("h", (randomizer.randrange(-8000, 8000) for _ in range(12 * RATE)))

    def test_trailing_silence_and_small_offset(self):
        delayed = array("h", [0] * 16) + self.audio
        padded = self.audio + array("h", [0] * (2 * RATE))
        result = compare_pcm(padded, delayed)
        self.assertTrue(result["matched"], result)
        self.assertEqual(result["offset_ms"], 16)

    def test_altered_middle_and_audible_tail(self):
        edit = self.audio[:]
        edit[5 * RATE:6 * RATE] = self.audio[:RATE]
        self.assertFalse(compare_pcm(self.audio, edit)["matched"])
        self.assertFalse(compare_pcm(self.audio + self.audio[:RATE], self.audio)["matched"])

    def test_wrong_remix(self):
        wrong = self.audio[6 * RATE:] + self.audio[:6 * RATE]
        self.assertFalse(compare_pcm(self.audio, wrong)["matched"])

    def test_invalid_audio_and_stale_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder).resolve() / "bad.flac"
            path.write_bytes(b"fLaC\x00\x00\x00")
            original = signature(path.stat())
            with self.assertRaisesRegex(ValueError, "could not be decoded"):
                decode(str(path), original)
            path.write_bytes(b"changed file")
            with self.assertRaisesRegex(ValueError, "changed before"):
                decode(str(path), original)

    def test_single_and_album_remix_need_same_version_and_aligned_audio(self):
        from contextlib import closing
        from unittest.mock import patch
        from spliff.staged_downloads import align_selected_recording

        original = {"id": 2976, "root": "/library", "path": "remix.mp3", "protected": 0,
                    "quality": "lossy", "artist": "Major Lazer/Guest", "album": "Remix Single",
                    "title": "Bubble Butt [Remix]"}
        audio = {"title": "Bubble Butt (Remix)", "artist": "Major Lazer",
                 "album": "Extended Album", "quality": "lossless"}
        item = {"name": "Extended Album (Extended Version) - 21 - Bubble Butt (Remix).flac",
                "audio": audio, "source": "/staging/remix.flac", "fingerprint": (1, 2, 3, 4)}
        import sqlite3
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.execute("CREATE TABLE tracks(id,present,size,mtime,device,inode)")
            connection.execute("INSERT INTO tracks VALUES (2976,1,1,2,3,4)")
            with patch("spliff.staged_downloads.connect", return_value=connection), patch(
                    "spliff.staged_downloads.compare_recordings", return_value={"matched": True}) as compare:
                self.assertTrue(align_selected_recording(None, item, original))
                self.assertEqual(item["recording_evidence"]["original_release"], "Remix Single")
                self.assertEqual(item["recording_evidence"]["download_release"], "Extended Album")
                item["audio"] = dict(audio, title="Bubble Butt")
                self.assertFalse(align_selected_recording(None, item, original))
                compare.assert_called_once()
