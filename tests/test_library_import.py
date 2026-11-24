import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from queue import Queue
from threading import Thread
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from spliff import flac_metadata, library_import, mcp, staged_downloads, web
from spliff.catalog import file_metadata_preference, initialize, tracks


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "Requires macOS and FFmpeg")
class FirstTimeImport(unittest.TestCase):
    candidate = "album-" + "b" * 32 + "-0"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.db = self.base / "catalog.sqlite"
        initialize(self.db)
        self.root = self.base / "Music"
        self.root.mkdir()
        self.staging = self.base / "stage"
        self.job = self.staging / self.candidate
        self.details = {"artist": "Fixture Artist", "title": "Fixture Album", "year": "2024",
                        "category": "Albums", "posthumous": False}
        for module in (library_import, staged_downloads):
            patcher = patch.object(module, "staging_root", return_value=self.staging)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(staged_downloads, "request", side_effect=ValueError("Use the saved transfer"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def stage(self, discs=(1,)):
        entries, files = [], []
        for index, disc in enumerate(discs, 1):
            directory = self.job / f"{index:03d}"
            directory.mkdir(parents=True)
            file = directory / f"Song {index}.flac"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=1",
                            "-metadata", f"title=Song {index}", "-metadata", "artist=Fixture Artist",
                            "-metadata", "album=Fixture Album", "-metadata", "date=2024",
                            "-metadata", f"disc={disc}", "-metadata", "track=1", str(file)], check=True, timeout=10)
            files.append(file)
            entries.append({"remote": f"Fixture Album\\{file.name}", "directory": directory.name,
                            "peer": "fixture", "bytes": file.stat().st_size})
        (self.job / "job.json").write_text(json.dumps({
            "id": self.candidate, "kind": self.candidate.split("-", 1)[0], "query": "Fixture Artist Fixture Album",
            "created": 1, "entries": entries}))
        return files

    def plan(self, edition=None):
        root, identity = library_import.root_identity(self.db, str(self.root))
        return library_import.prepare(self.db, self.candidate, root, identity, self.details, edition)

    def test_preserves_bytes_indexes_and_clears_multidisc_import(self):
        sources = self.stage((1, 2))
        expected_bytes = [file.read_bytes() for file in sources]
        decision = mcp.review_for_chat(self.db, self.candidate)["add_to_library"]
        self.assertIn(f"import_candidate={self.candidate}", decision["approval_url"])
        plan = self.plan()
        self.assertFalse(Path(plan["destination"]).exists())
        self.assertTrue(self.job.is_dir())
        result = library_import.apply(self.db, plan)
        destination = self.root / "Fixture Artist/Albums/[2024] Fixture Album"
        self.assertEqual(Path(result["destination"]), destination)
        for index in (1, 2):
            saved = destination / f"Disc {index}/01 Song {index}.flac"
            self.assertEqual(saved.read_bytes(), expected_bytes[index - 1])
        self.assertTrue(result["staging_cleared"])
        self.assertFalse(self.job.exists())
        self.assertEqual(len(tracks(self.db)["items"]), 2)
        self.assertEqual(mcp.transfer_status(self.db, self.candidate)["completion"], result)
        self.assertEqual(mcp.review_for_chat(self.db, self.candidate)["completion"], result)

    def test_first_time_track_can_be_filed_as_a_single(self):
        self.candidate = self.candidate.replace("album-", "track-")
        self.job = self.staging / self.candidate
        source, = self.stage()
        expected = source.read_bytes()
        self.details.update(category="Singles", title="Song 1")
        result = library_import.apply(self.db, self.plan())
        self.assertEqual(result["destination"], str(self.root / "Fixture Artist/Singles/[2024] Song 1"))
        self.assertEqual((Path(result["destination"]) / "01 Song 1.flac").read_bytes(), expected)
        self.assertTrue(result["staging_cleared"])

    def test_discogs_edition_reuses_verified_alias_and_writes_only_music_tags(self):
        source, = self.stage()
        original = source.read_bytes()
        (self.root / "Former Name").mkdir()
        release = {"id": 35, "url": "https://discogs.example/release/35", "title": "Fixture Album", "year": 2024,
                   "artists": ["Fixture Artist"], "artist_credits": [{"id": 15, "name": "Fixture Artist"}],
                   "formats": [{"descriptions": ["Album", "Deluxe Edition"]}],
                   "labels": [{"name": "Fixture Label", "catno": "FL-01"}],
                   "credits": [{"name": "Fixture Producer", "role": "Producer"}],
                   "tracklist": [{"position": "1", "title": "Song 1", "duration": "0:01", "type_": "track"}]}
        with patch.object(library_import.discogs, "search_releases", return_value={"items": [{"id": 35}]}), \
                patch.object(library_import.discogs, "release_details", return_value=release), \
                patch.object(library_import.discogs, "artist_aliases", return_value=["Former Name"]):
            edition, = library_import.options(self.db, self.candidate)["editions"]
        self.assertEqual(edition["category"], "Albums")
        self.assertNotIn("https://", json.dumps(edition))
        file_metadata_preference(self.db, "write")
        plan = self.plan(edition)
        self.assertIn("Former Name", plan["destination"])
        preview = library_import.preview(plan)
        file, = preview["files"]
        self.assertEqual(file["tags"]["LABEL"], "Fixture Label")
        self.assertEqual(file["tags"]["PRODUCER"], "Fixture Producer")
        result = library_import.apply(self.db, plan)
        self.assertNotIn("https://", json.dumps(result))
        with Path(file["install"]).open("rb") as output:
            tags = flac_metadata.music_tags(output)
            self.assertEqual(tags["ALBUMARTIST"], "Fixture Artist")
            self.assertEqual(tags["CATALOGNUMBER"], "FL-01")
            self.assertEqual(tags["PRODUCER"], "Fixture Producer")
            self.assertTrue(all("https://" not in value for value in tags.values()))
        self.assertNotEqual(Path(file["install"]).read_bytes(), original)
        self.assertFalse(self.job.exists())

    def test_changed_preference_and_occupied_destination_leave_staging_intact(self):
        source, = self.stage()
        plan = self.plan()
        file_metadata_preference(self.db, "write")
        with self.assertRaisesRegex(ValueError, "metadata setting changed"):
            library_import.apply(self.db, plan)
        self.assertFalse(Path(plan["destination"]).exists())
        file_metadata_preference(self.db, "preserve")
        occupied = Path(plan["destination"])
        occupied.mkdir(parents=True)
        sentinel = occupied / "Keep me.txt"
        sentinel.write_bytes(b"keep")
        with self.assertRaisesRegex(ValueError, "already exists"):
            library_import.apply(self.db, plan)
        self.assertTrue(source.exists())
        self.assertEqual(sentinel.read_bytes(), b"keep")
        self.assertEqual(tracks(self.db)["items"], [])

    def test_publish_failure_rolls_back_only_the_import_and_keeps_the_transfer(self):
        sources = self.stage((1, 2))
        plan = self.plan()
        move = library_import.keep_both.move_exclusive

        def fail_on_second(source, target, **kwargs):
            if target.parent.name == "Disc 2":
                raise OSError("Publication interrupted")
            return move(source, target, **kwargs)

        with patch.object(library_import.keep_both, "move_exclusive", side_effect=fail_on_second):
            with self.assertRaisesRegex(OSError, "Publication interrupted"):
                library_import.apply(self.db, plan)
        self.assertTrue(all(file.is_file() for file in sources))
        self.assertFalse(Path(plan["destination"]).exists())
        self.assertFalse(Path(plan["work"]).exists())
        self.assertEqual(tracks(self.db)["items"], [])

    def test_local_route_requires_reviewed_approval_and_consumes_it(self):
        self.stage()
        started = Queue()

        def bind(address, handler):
            server = ThreadingHTTPServer(address, handler)
            started.put(server)
            return server

        run = subprocess.run
        picker = subprocess.CompletedProcess([], 0, stdout=json.dumps([str(self.root)]), stderr="")

        def subprocess_with_picker(command, **kwargs):
            return picker if "osascript" in command[0] else run(command, **kwargs)

        with patch.object(web, "ThreadingHTTPServer", side_effect=bind), patch.object(web.sync, "run_worker"), \
                patch.object(web.subprocess, "run", side_effect=subprocess_with_picker):
            thread = Thread(target=web.serve, args=(self.db, 0), daemon=True)
            thread.start()
            server = started.get(timeout=5)
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                headers = {"Origin": base, "X-Spliff-Action": "dashboard"}

                def post(path, body=b"", extra=None):
                    with urlopen(Request(base + path, data=body, headers={**headers, **(extra or {})}),
                                 timeout=10) as response:
                        return json.load(response)

                root = post("/api/import-root")["root"]
                data = json.dumps({**self.details, "candidate": self.candidate, "root": root}).encode()
                plan = post("/api/import-plan", data)
                self.assertFalse(Path(plan["destination"]).exists())
                url = "/api/import-apply?" + urlencode({"candidate": self.candidate, "token": plan["token"]})
                with self.assertRaises(HTTPError) as missing:
                    post(url)
                missing.exception.close()
                self.assertTrue(self.job.exists())
                approval = {"X-Spliff-Approval": plan["approval"]}
                result = post(url, extra=approval)
                self.assertTrue(result["staging_cleared"])
                with self.assertRaises(HTTPError) as replay:
                    post(url, extra=approval)
                replay.exception.close()
            finally:
                server.shutdown()
                thread.join(timeout=5)
