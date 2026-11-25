import json
import random
import shutil
import subprocess
import sys
import unittest
from contextlib import closing, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import test_library_import

from spliff import keep_both, mcp, spectral, staged_downloads
from spliff.catalog import connect
from spliff.recording_match import compare_fingerprints
from spliff.scanner import scan


class RecordingIdentification(unittest.TestCase):
    def setUp(self):
        self.original = {"id": 1, "title": "Suavé", "artist": "Roy Ayers", "album": "Wake Up",
                         "duration": 200.0, "track_number": "2", "disc_number": "1", "path": "02 Suave.mp3"}
        self.download = {"title": "", "artist": "", "album": "", "duration": 200.05, "quality": "lossless"}

    def test_missing_tags_and_accents_use_filename_and_release_context(self):
        actual, _ = staged_downloads.match_item(
            self.download, [self.original], "album", "Roy Ayers - Wake Up - 02 - Suave.flac")
        self.assertEqual(actual, "metadata fallback")
        actual, _ = staged_downloads.match_item(
            self.download, [self.original], "album", "02 Untagged.flac", parents=("Wake Up", "Disc 1"))
        self.assertEqual(actual, "metadata fallback")

    def test_explicit_remix_and_disc_mismatch_still_excluded(self):
        for version in ("Live", "Remix", "Radio Edit", "Extended", "Instrumental"):
            with self.subTest(version=version):
                actual, _ = staged_downloads.match_item(
                    self.download, [self.original], "album", f"02 - Suave ({version}).flac", parents=("Wake Up",))
                self.assertEqual(actual, "check recording version")
        actual, _ = staged_downloads.match_item(
            self.download, [self.original], "album", "02 - Suave.flac", parents=("Wake Up", "Disc 2"))
        self.assertEqual(actual, "check disc order")

    def test_rolloff_alone_does_not_flag_a_possible_low_pass(self):
        rolloff = b"lavfi.aspectralstats.1.rolloff=14600.0\n"
        self.assertEqual(spectral.measurements(rolloff)["possible_low_pass_hz"], [])
        bands = (b"[astats@band1 @ 0x123] RMS level dB: -36.0\n"
                 b"[astats@band2 @ 0x456] RMS level dB: -62.0\n")
        self.assertEqual(spectral.measurements(rolloff + bands)["possible_low_pass_hz"], [16000])

    def test_fingerprint_requires_full_consistent_coverage(self):
        rng = random.Random(47)
        source = tuple(rng.getrandbits(32) for _ in range(240))
        self.assertTrue(compare_fingerprints(source, (0,) * 3 + source)["matched"])
        edit = source[:105] + source[:35] + source[140:]
        self.assertFalse(compare_fingerprints(source, edit)["matched"])
        self.assertFalse(compare_fingerprints(source, source[:-22])["matched"])


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "Requires macOS and FFmpeg")
class AlbumVerification(unittest.TestCase):
    candidate = test_library_import.FirstTimeImport.candidate
    setUp = test_library_import.FirstTimeImport.setUp
    stage = test_library_import.FirstTimeImport.stage

    def test_untagged_flac_verified_and_batch_supports_partial_failures(self):
        self.stage((1, 1))
        staged = self.job / "001" / "Song 1.flac"
        original = self.root / "01 Song 1.mp3"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                        "anoisesrc=duration=12:color=pink:seed=17", str(staged)], check=True)
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(staged), "-q:a", "1",
                        "-metadata", "title=Song 1", "-metadata", "artist=Fixture Artist",
                        "-metadata", "album=Fixture Album", "-metadata", "track=1",
                        str(original)], check=True)
        receipt = self.job / "job.json"
        manifest = json.loads(receipt.read_text())
        manifest["entries"][0]["bytes"] = staged.stat().st_size
        other = self.job / "002" / "Song 2.flac"
        other.write_bytes(b"broken audio")
        manifest["entries"][1]["bytes"] = other.stat().st_size
        receipt.write_text(json.dumps(manifest))
        scan(self.db, self.root)
        with closing(connect(self.db)) as connection:
            identifier = connection.execute("SELECT id FROM tracks").fetchone()["id"]
        before = spectral._render.cache_info().hits
        with patch.object(keep_both, "staging_root", return_value=self.staging):
            plan = keep_both.preview(self.db, self.candidate, identifier, "replace", strict=True)
            self.assertEqual(plan["count"], 1)
            self.assertEqual(plan["skipped"][0]["reason_code"], "audio_inspection_failed")
            output = StringIO()
            jobs = spectral.AlbumJobs(self.db)

            def request(identifier, method, params):
                return json.dumps({"jsonrpc": "2.0", "id": identifier, "method": method, "params": params})

            def incoming():
                yield request(1, "initialize", {"protocolVersion": "2025-11-25"})
                yield request(2, "tools/call", {"name": "analyze_album_spectrum", "arguments": {
                    "candidate_id": self.candidate, "original_track_id": identifier}})
                started = json.loads(output.getvalue().splitlines()[-1])["result"]["structuredContent"]
                job_id = started["job_id"]
                jobs.jobs[job_id][0].result(timeout=15)
                yield request(3, "tools/call", {"name": "album_spectrum_status", "arguments": {"job_id": job_id}})

            with patch("sys.stdin", incoming()), patch.object(mcp, "AlbumJobs", return_value=jobs), redirect_stdout(output):
                mcp.serve(self.db, allow_downloads=True)
            jobs.executor.shutdown()
            album = json.loads(output.getvalue().splitlines()[-1])["result"]["structuredContent"]
            self.assertEqual(album["status"], "complete")
            second = spectral.analyze_download(self.db, self.candidate, 0, identifier)
            self.assertIn("image_base64", second["download"])
        self.assertEqual(album["analyzed_downloads"], 1)
        self.assertEqual(album["compared_originals"], 1)
        self.assertEqual(album["failed_or_incomplete"], 1)
        self.assertIn("download", album["tracks"][1]["errors"])
        self.assertNotIn("image_base64", json.dumps(album))
        self.assertGreaterEqual(spectral._render.cache_info().hits - before, 2)
        self.assertTrue(original.exists())
        self.assertTrue(staged.exists())
