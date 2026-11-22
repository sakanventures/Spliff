import hashlib
import json
import select
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import quote

from spliff.catalog import stats, tracks
from spliff.review import protect, reviews, stage
from spliff.scanner import scan

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
        self.assertEqual(len(listing["result"]["tools"]), 3)
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
