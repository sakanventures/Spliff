import hashlib
from contextlib import closing
import json
import select
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path, PureWindowsPath
from time import time
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import quote

from spliff.catalog import connect, signature, stats, tracks
from spliff import keep_both
from spliff.review import protect, reviews, stage
from spliff.scanner import scan
from spliff.nicotine_bridge.downloads import Downloads
from spliff.staged_downloads import list_downloads, review_download, suggest_album

@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "Requires FFmpeg")
class VerticalSlice(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.root = self.base / "Music"
        self.root.mkdir()
        self.db = self.base / "state/catalog.sqlite"
        self.original = self.root / "Rare 100%.mp3"
        self.candidate = self.base / "Candidate.flac"
        for target, extra in ((self.original, ["-b:a", "320k"]), (self.candidate, [])):
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                            "-i", "sine=frequency=1100:duration=1", "-metadata",
                            "title=Rare 100%", "-metadata", "artist=Example Artist",
                            *extra, str(target)], check=True, timeout=15)

    def test_scan_stage_and_preserve(self):
        digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
        before = digest(self.original), digest(self.candidate)
        (self.root / "alias.flac").symlink_to(self.candidate)
        (self.root / "broken.mp3").write_text("invalid audio")
        counts = scan(self.db, self.root)
        self.assertEqual((counts["probed"], counts["errors"]), (1, 1))
        indexed = tracks(self.db, "Example", "lossy")["items"][0]
        self.assertEqual(indexed["title"], "Rare 100%")
        self.assertEqual(indexed["bitrate"], 320000)
        self.assertEqual(len(tracks(self.db, "%")["items"]), 1)
        protect(self.db, indexed["id"], True)
        stage(self.db, indexed["id"], self.candidate)
        review = reviews(self.db)[0]
        self.assertEqual(review["snapshot"]["codec"], "flac")
        self.assertAlmostEqual(review["snapshot"]["duration"], 1.0)
        self.assertEqual(review["protected"], 1)
        self.assertEqual(scan(self.db, self.root)["cached"], 1)
        self.assertEqual(before, (digest(self.original), digest(self.candidate)))
        self.original.rename(self.base / self.original.name)
        self.assertEqual(scan(self.db, self.root)["missing"], 1)
        self.assertEqual(stats(self.db)["protected"], 1)
        self.assertEqual(tracks(self.db, scope="missing")["items"][0]["id"], indexed["id"])

    def test_abort_when_root_is_replaced(self):
        scan(self.db, self.root)
        self.root.rename(self.base / "Unmounted")
        self.root.mkdir()
        with self.assertRaisesRegex(ValueError, "changed identity"):
            scan(self.db, self.root)
        self.assertEqual(stats(self.db)["tracks"], 1)

    def test_catalog_cannot_be_inside_library(self):
        never = self.root / "index/catalog.db"
        with self.assertRaises(ValueError):
            scan(never, self.root)
        self.assertFalse(never.parent.exists())

    def test_download_job_is_idempotent_and_compares_only_staged_audio(self):
        scan(self.db, self.root)
        root = self.base.resolve() / "staging"
        incomplete = self.base / "incomplete"
        indexed = tracks(self.db)["items"][0]
        protect(self.db, indexed["id"], True)
        staged_bytes = self.candidate.read_bytes()
        original_bytes = self.original.read_bytes()
        queued = []

        def enqueue(peer, remote, folder_path, size):
            queued.append((peer, remote))
            transfers[peer + remote] = SimpleNamespace(folder_path=folder_path, status="Queued")

        def completed(_peer, remote, size, folder):
            path = Path(folder) / PureWindowsPath(remote).name
            return str(path), path.is_file() and path.stat().st_size == size

        transfers = {}
        core = SimpleNamespace(downloads=SimpleNamespace(
            transfers=transfers, enqueue_download=enqueue, get_complete_download_file_path=completed,
            get_incomplete_download_folder=lambda: str(incomplete),
        ))
        config = SimpleNamespace(sections={"transfers": {"afterfinish": "", "afterfolder": ""}})
        bridge = Downloads(core, config)
        identifier = "a" * 32
        selection = f"album-{identifier}-0"
        flac = {"peer": "test", "path": r"Artist\Album\01.flac", "format": "flac", "bytes": len(staged_bytes)}
        duplicate = dict(flac, path=r"Artist\Album\01.wav", format="wav")
        searches = {identifier: (time(), [flac, duplicate], "Example Artist")}
        with patch("spliff.nicotine_bridge.downloads.staging_root", return_value=root), patch(
                "spliff.staged_downloads.staging_root", return_value=root):
            with self.assertRaisesRegex(ValueError, "outside indexed"):
                bridge.queue(selection, searches, [str(self.base)])
            self.assertEqual(queued, [])
            with self.assertRaisesRegex(ValueError, "expired"):
                bridge.queue(selection, {identifier: (time() - 601, [flac], "Example")}, [])
            job = bridge.queue(selection, searches, [str(self.root)])
            self.assertEqual(job["complete"], 0)
            self.assertEqual(list_downloads(), [])
            self.assertEqual(job["items"][0]["status"], "Queued")
            self.assertEqual(len(queued), 1)
            self.assertEqual(bridge.queue(selection, {}, [str(self.root)]), job)
            self.assertEqual(len(queued), 1)
            folder = root / selection / "001"
            (folder / "01.flac").write_bytes(staged_bytes[:-1])
            self.assertEqual(list_downloads(), [])
            (folder / "01.flac").write_bytes(staged_bytes)
            self.assertEqual([job["id"] for job in list_downloads()], [selection])
            transfers.clear()  # Nicotine+ can auto-clear completed transfers.
            with patch("spliff.staged_downloads.staging_root", return_value=root), patch(
                    "spliff.staged_downloads.request", side_effect=lambda operation, **args: bridge.status(args["selection"])):
                # The selected original is a single-track comparison here because its album tag is empty.
                report = review_download(self.db, selection, indexed["id"])
            self.assertEqual(report["complete"], 1)
            self.assertEqual(report["items"][0]["audio"]["codec"], "flac")
            self.assertNotIn("file", report["items"][0])
            self.assertTrue(report["items"][0]["original"]["protected"])
            self.assertEqual(report["items"][0]["assessment"], "check release tags")
            self.assertEqual(self.original.read_bytes(), original_bytes)
            self.assertEqual(self.candidate.read_bytes(), staged_bytes)

    def test_suggest_album_requires_one_indexed_edition(self):
        scan(self.db, self.root)
        indexed = tracks(self.db)["items"][0]
        with closing(connect(self.db)) as connection, connection:
            connection.execute("UPDATE tracks SET album='Example Album' WHERE id=?", (indexed["id"],))
        sample = {"name": "01.flac", "audio": {
            "quality": "lossless", "title": indexed["title"], "artist": indexed["artist"],
            "duration": indexed["duration"], "album": "Example Album",
        }}
        self.assertEqual(suggest_album(self.db, [sample])[0]["id"], indexed["id"])
        with closing(connect(self.db)) as connection, connection:
            connection.execute("""
                INSERT INTO tracks(root,path,artist,album,title,duration)
                SELECT root,'Other edition/01.mp3',artist,album,title,duration
                FROM tracks WHERE id=?
            """, (indexed["id"],))
        self.assertEqual(suggest_album(self.db, [sample]), [])

    def keep_both_fixture(self):
        scan(self.db, self.root)
        indexed = tracks(self.db)["items"][0]
        staged = self.candidate.resolve()
        audio = dict(indexed, codec="flac", format="flac", quality="lossless")
        report = {"kept_both": None, "kind": "album", "complete": 1, "originals_without_match": [],
                  "comparison": {"album": "Example Album"},
                  "items": [{"assessment": "possible match", "original": indexed, "audio": audio,
                             "source": str(staged), "fingerprint": signature(staged.stat())}]}
        return indexed, report

    @unittest.skipUnless(sys.platform == "darwin", "Requires exclusive macOS rename")
    def test_keep_both_requires_confirmation_then_preserves_audio(self):
        original_bytes = self.original.read_bytes()
        candidate_bytes = self.candidate.read_bytes()
        indexed, report = self.keep_both_fixture()
        with patch("spliff.keep_both.review_download", return_value=report):
            plan = keep_both.preview(self.db, "example", indexed["id"])
            self.assertTrue(self.original.exists())
            self.assertFalse((self.root / "MP3").exists())
            self.assertEqual(plan["archive"], str(self.root.resolve() / "MP3"))
            with self.assertRaisesRegex(ValueError, "comparison changed"):
                keep_both.apply(self.db, "example", indexed["id"], "incorrect")
            self.assertTrue(self.original.exists())
            keep_both.apply(self.db, "example", indexed["id"], plan["token"])
        self.assertFalse(self.original.exists())
        self.assertEqual((self.root / "MP3" / self.original.name).read_bytes(), original_bytes)
        self.assertEqual(self.original.with_suffix(".flac").read_bytes(), candidate_bytes)
        self.assertEqual(self.candidate.read_bytes(), candidate_bytes)
        catalog = tracks(self.db)["items"]
        self.assertEqual(catalog[0]["id"], indexed["id"])
        self.assertTrue(catalog[0]["path"].startswith("MP3/"))
        self.assertEqual(catalog[1]["codec"], "flac")

    @unittest.skipUnless(sys.platform == "darwin", "Requires exclusive macOS rename")
    def test_keep_both_rolls_back_when_publication_fails(self):
        indexed, report = self.keep_both_fixture()
        original_bytes = self.original.read_bytes()
        move = keep_both.move_exclusive

        def fail_on_flac(source, destination):
            if destination.suffix == ".flac":
                raise OSError("Simulated publication failure")
            move(source, destination)

        with patch("spliff.keep_both.review_download", return_value=report):
            plan = keep_both.preview(self.db, "example", indexed["id"])
            with patch("spliff.keep_both.move_exclusive", side_effect=fail_on_flac):
                with self.assertRaisesRegex(OSError, "publication failure"):
                    keep_both.apply(self.db, "example", indexed["id"], plan["token"])
        self.assertEqual(self.original.read_bytes(), original_bytes)
        self.assertFalse((self.root / "MP3").exists())
        self.assertFalse((self.root / keep_both.WORK_DIR).exists())
        self.assertEqual(tracks(self.db)["items"][0]["path"], indexed["path"])

    @unittest.skipUnless(sys.platform == "darwin", "Exercises the macOS fallback")
    def test_keep_both_copy_fallback_refuses_conflicts(self):
        source = self.original.resolve()
        destination = source.parent / "copy.mp3"
        expected = source.read_bytes()
        destination.write_bytes(b"pre-existing")
        with self.assertRaises(FileExistsError):
            keep_both.copy_then_remove(source, destination)
        self.assertEqual(source.read_bytes(), expected)
        self.assertEqual(destination.read_bytes(), b"pre-existing")
        destination.unlink()
        keep_both.copy_then_remove(source, destination)
        self.assertFalse(source.exists())
        self.assertEqual(destination.read_bytes(), expected)

    def test_mcp_and_http(self):
        scan(self.db, self.root)
        command = [sys.executable, "-m", "spliff", "--db", str(self.db)]
        messages = [
            {"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25",
                                                        "clientInfo": {"name": "test", "version": "1"},
                                                        "capabilities": {}}},
            {"method": "notifications/initialized"},
            {"id": 2, "method": "tools/list"},
            {"id": 3, "method": "tools/call", "params": {"name": "search_library",
                                                       "arguments": {"scope": "lossy"}}},
        ]
        data = "".join(json.dumps(dict(message, jsonrpc="2.0")) + "\n" for message in messages)
        output = subprocess.run(command + ["mcp"], input=data, capture_output=True,
                                check=True, text=True, timeout=10)
        initialized, listing, called = map(json.loads, output.stdout.splitlines())
        self.assertEqual(initialized["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual(initialized["result"]["serverInfo"]["title"], "Spliff")
        self.assertEqual(len(listing["result"]["tools"]), 4)
        payload = json.loads(called["result"]["content"][0]["text"])
        self.assertEqual(payload["items"][0]["title"], "Rare 100%")
        with subprocess.Popen(command + ["serve", "--port", "0"], stdout=subprocess.PIPE, text=True) as server:
            try:
                self.assertTrue(select.select([server.stdout], [], [], 10)[0])
                base = server.stdout.readline().strip()
                self.assertTrue(base, "Local HTTP server failed to start")
                with urlopen(base, timeout=5) as page:
                    self.assertIn(b"Spliff", page.read())
                with urlopen(base + "/api/tracks?scope=lossy", timeout=5) as result:
                    self.assertEqual(json.load(result), payload)
                with self.assertRaises(HTTPError) as error:
                    urlopen(base + "/api/tracks?scope=bad", timeout=5)
                self.assertEqual(error.exception.code, 400)
                error.exception.close()
                track_id = payload["items"][0]["id"]
                endpoint = base + f"/api/tracks/{track_id}"
                headers = {"Origin": base, "X-Spliff-Action": "dashboard"}
                with self.assertRaises(HTTPError) as rejected:
                    urlopen(Request(endpoint + "/protect", data=b""), timeout=5)
                self.assertEqual(rejected.exception.code, 403)
                rejected.exception.close()
                with urlopen(Request(endpoint + "/protect", data=b"", headers=headers), timeout=5) as reply:
                    self.assertTrue(json.load(reply)["protected"])
                candidate_bytes = self.candidate.read_bytes()
                original_bytes = self.original.read_bytes()
                for name, body in (("../Candidate.flac", candidate_bytes),
                                   ("lossy.flac", original_bytes)):
                    rejected_headers = dict(headers, **{"X-Spliff-Filename": quote(name, safe="")})
                    with self.assertRaises(HTTPError) as rejected:
                        urlopen(Request(endpoint + "/stage", data=body, headers=rejected_headers), timeout=5)
                    self.assertEqual(rejected.exception.code, 400)
                    rejected.exception.close()
                upload_headers = dict(headers, **{"X-Spliff-Filename": quote("Candidate #1.flac")})
                with urlopen(Request(endpoint + "/stage", data=candidate_bytes,
                                     headers=upload_headers), timeout=5) as reply:
                    self.assertEqual(reply.status, 201)
                    review_id = json.load(reply)["id"]
                with urlopen(base + "/api/reviews", timeout=5) as reply:
                    result = json.load(reply)["items"][0]
                self.assertEqual(result["id"], review_id)
                self.assertEqual(result["candidate"], "Candidate #1.flac")
                self.assertEqual(result["snapshot"]["codec"], "flac")
                self.assertTrue(result["protected"])
                self.assertEqual(self.original.read_bytes(), original_bytes)
                self.assertEqual(self.candidate.read_bytes(), candidate_bytes)

            finally:
                server.terminate()
