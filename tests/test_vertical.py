import hashlib
from contextlib import closing
import json
import select
import shutil
import subprocess
import sys
import tempfile
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path, PureWindowsPath
from queue import Queue
from threading import Thread
from time import sleep, time
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import quote, urlencode

from spliff.catalog import connect, replacement_preference, signature, stats, tracks
from spliff import keep_both, mcp, sync, web
from spliff.review import protect, reviews, stage
from spliff.scanner import scan
from spliff.nicotine_bridge.downloads import Downloads, ENABLE_FILE
from spliff.nicotine_bridge.wire import fingerprint
from spliff.staged_downloads import cached_edition, list_downloads, match_item, queue, review_download, suggest_album

@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "Requires FFmpeg")
class VerticalSlice(unittest.TestCase):
    album_selection = "album-" + "a" * 32 + "-0"

    def test_unsigned_filesystem_identifier_fits_sqlite(self):
        info = SimpleNamespace(st_size=42, st_mtime_ns=100, st_dev=3, st_ino=2**63 + 7)
        with closing(connect(":memory:")) as connection:
            self.assertEqual(tuple(connection.execute("SELECT ?,?,?,?", signature(info)).fetchone()),
                             (42, 100, 3, -2**63 + 7))

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

    def test_overlapping_scans_preserve_track_ids_and_limit_missing_checks(self):
        album = self.root / "Album"
        album.mkdir()
        original = self.original.rename(album / self.original.name)
        scan(self.db, album)
        track_id = tracks(self.db)["items"][0]["id"]
        protect(self.db, track_id, True)
        stage(self.db, track_id, self.candidate)
        sibling = self.root / "Other.mp3"
        shutil.copyfile(original, sibling)
        self.assertEqual(scan(self.db, self.root)["cached"], 1)
        self.assertEqual(stats(self.db)["roots"], 1)
        self.assertEqual(tracks(self.db)["items"][0]["id"], track_id)
        self.assertEqual(reviews(self.db)[0]["original_id"], track_id)
        original.unlink()
        self.assertEqual(scan(self.db, album)["missing"], 1)
        self.assertEqual([item["path"] for item in tracks(self.db)["items"]], [sibling.name])
        self.assertEqual(tracks(self.db, scope="missing")["items"][0]["protected"], 1)

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
            transfers[peer + remote] = SimpleNamespace(username=peer, virtual_path=remote, folder_path=folder_path, size=size, status="Queued")

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
            with self.assertRaisesRegex(ValueError, "disabled"):
                bridge.queue(selection, searches, [str(self.root)])
            (root / ENABLE_FILE).touch(mode=0o600)
            approved = fingerprint([flac])
            with self.assertRaisesRegex(ValueError, "expired"):
                bridge.queue(selection, {identifier: (time() - 601, [flac], "Example")}, [], approved, "manual")
            job = bridge.queue(selection, searches, [str(self.root)], approved, "manual")
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
            with closing(connect(self.db)) as connection, connection:
                connection.execute("UPDATE tracks SET album='Example Album' WHERE id=?", (indexed["id"],))
            with patch("spliff.staged_downloads.request", side_effect=lambda operation, **args: bridge.status(args["selection"])):
                queue(self.db, selection, original_id=indexed["id"])
                remembered = review_download(self.db, selection)
            self.assertEqual(remembered["comparison"],
                             {"album": "Example Album", "original_track_id": indexed["id"], "suggested": False})

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
        staging = self.base.resolve() / "staging"
        staged = staging / self.album_selection / "001" / self.candidate.name
        staged.parent.mkdir(parents=True)
        shutil.copyfile(self.candidate, staged)
        (staged.parents[1] / "job.json").write_text(json.dumps({
            "id": self.album_selection, "kind": "album", "query": "Example Artist Example Album", "created": 1,
            "entries": [{"directory": "001", "peer": "test", "remote": r"Example Album\Candidate.flac",
                         "bytes": staged.stat().st_size}],
        }))
        patcher = patch("spliff.keep_both.staging_root", return_value=staging)
        patcher.start()
        self.addCleanup(patcher.stop)
        audio = dict(indexed, codec="flac", format="flac", quality="lossless")
        report = {"kept_both": None, "kind": "album", "complete": 1, "originals_without_match": [],
                  "comparison": {"album": "Example Album"},
                  "items": [{"assessment": "possible match", "original": indexed, "audio": audio,
                             "source": str(staged), "fingerprint": signature(staged.stat())}]}
        return indexed, report

    def test_discogs_tracklist_corroboration_is_saved_per_transfer(self):
        scan(self.db, self.root)
        listing = [{"title": "First", "position": "1", "duration": "3:02", "type_": "track"},
                   {"title": "Second", "position": "2", "duration": "4:01", "type_": "track"}]
        release = {"id": 42, "url": "https://www.discogs.com/release/42", "title": "Album",
                   "year": 2007, "artists": ["Common"], "tracklist": listing}
        items = [{"name": "02.flac", "audio": {"title": "Second", "duration": 241,
                      "artist": "Common", "album": "Album", "track_number": "2"}},
                 {"name": "01.flac", "audio": {"title": "First", "duration": 182,
                      "artist": "Common", "album": "Album", "track_number": "1"}}]
        with patch("spliff.staged_downloads.search_releases", return_value={"items": [{"id": 42}]}) as lookup, \
                patch("spliff.staged_downloads.release_details", return_value=release):
            evidence = cached_edition(self.db, self.album_selection, items)
            self.assertEqual(evidence["releases"][0]["year"], 2007)
            self.assertNotIn("url", evidence["releases"][0])
            self.assertEqual(cached_edition(self.db, self.album_selection, items), evidence)
            lookup.assert_called_once()

    def test_recording_review_separates_credit_and_recording_differences(self):
        original = {"title": "Drivin' Me Wild (ft. Lily Allen)", "artist": "Common",
                    "album": "Finding Forever", "duration": 222.54, "path": "06 Drivin.mp3"}
        download = {"title": "Drivin’ Me Wild", "artist": "Common feat. Lily Allen",
                    "album": "Finding Forever", "duration": 222.53, "quality": "lossless"}
        self.assertEqual(match_item(download, [original], "album", "04 Drivin.flac")[0], "check track order")
        self.assertEqual(match_item(download, [original], "album", "06 Drivin.flac")[0], "possible match")
        for suffix in ("Live", "Remix", "Instrumental", "Clean", "Explicit", "Alternate Version"):
            variant = dict(download, title=f"Drivin’ Me Wild (ft. Lily Allen) ({suffix})")
            self.assertNotEqual(match_item(variant, [original], "album", "06 Drivin.flac")[0], "possible match")

    @unittest.skipUnless(sys.platform == "darwin", "Requires exclusive macOS rename")
    def test_sync_waits_for_selection_and_applies_selected_partial_plan(self):
        for path, title in ((self.original, "Rare Cut"), (self.root / "Other.mp3", "Unmatched Cut"),
                            (self.root / "Protected.mp3", "Protected Cut"),
                            (self.candidate, "Rare Cut")):
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=duration=1",
                            "-metadata", f"title={title}", "-metadata", "album=Example Album",
                            "-metadata", "artist=Example Artist", str(path)], check=True, timeout=15)
        scan(self.db, self.root)
        protected = next(row for row in tracks(self.db)["items"] if row["title"] == "Protected Cut")
        protect(self.db, protected["id"], True)
        selection = self.album_selection
        staging = self.base.resolve() / "staging"
        folder = staging / selection
        entry = folder / "001"
        entry.mkdir(parents=True)
        staged = entry / "Rare Cut.flac"
        offer = {"candidate_id": selection, "matching_files": 2, "lossless_files": 2,
                 "filenames": ["Rare Cut.flac", "Protected Cut.flac"], "free_slot": True, "queue": 0}

        def bridge(operation, **_args):
            if operation != "queue":
                raise ValueError("Nicotine+ offline")
            (folder / "job.json").write_text(json.dumps({
                "id": selection, "kind": "album", "query": "Example Artist Example Album", "created": 1,
                "entries": [{"directory": "001", "peer": "test", "remote": r"Example Album\Rare Cut.flac",
                             "bytes": self.candidate.stat().st_size}]}))
            return {"id": selection, "items": [], "complete": 0}

        with patch("spliff.staged_downloads.staging_root", return_value=staging), \
                patch("spliff.keep_both.staging_root", return_value=staging), \
                patch("spliff.staged_downloads.request", side_effect=bridge) as request, \
                patch("spliff.sync.search", return_value={"candidates": [offer]}), \
                patch("spliff.staged_downloads.search_releases", return_value={"items": []}):
            self.assertEqual(sync.start(self.db, str(self.root))["targets"], 1)
            sync.reconcile(self.db)
            self.assertEqual(sync.status(self.db)["items"][0]["state"], "candidate_ready")
            request.assert_not_called()
            self.assertEqual(list_downloads(self.db), [])
            original_id = sync.status(self.db)["items"][0]["original_id"]
            from spliff.candidates import record
            record(self.db, "Example Album", "album", [offer],
                   [{"peer": "test", "path": r"Example Album\Rare Cut.flac",
                     "bytes": self.candidate.stat().st_size, "format": "flac"}])
            queue(self.db, selection, original_id=original_id)
            self.assertEqual(list_downloads(self.db)[0]["state"], "downloading")
            self.assertEqual(reviews(self.db), [])
            shutil.copyfile(self.candidate, staged)
            sync.reconcile(self.db)
            self.assertEqual(list_downloads(self.db)[0]["state"], "reviewed")
            decision = mcp.review_for_chat(self.db, selection)["album_decision"]
            old = decision["options"]["replace"]
            self.assertEqual(old["count"], 1)
            self.assertCountEqual(old["preserved"], ["Other.mp3", "Protected.mp3"])
            self.assertNotIn("token", old)
            self.assertIn(f"review_candidate={selection}", decision["approval_url"])
            self.assertTrue(self.original.exists())
            self.assertTrue(staged.is_file())
            self.assertTrue((folder / "job.json").is_file())
            replacement_preference(self.db, "hidden")
            plan = keep_both.preview(self.db, selection, decision["original_track_id"], "replace")
            result = keep_both.apply(self.db, selection, decision["original_track_id"], plan["token"], "replace")
            self.assertEqual(result["upgraded"], 1)
            self.assertEqual(result["preserved"], 2)
            self.assertTrue((self.root / "Protected.mp3").exists())
            self.assertTrue((self.root / "Other.mp3").exists())
            self.assertEqual(self.original.with_suffix(".flac").read_bytes(), self.candidate.read_bytes())
            self.assertEqual(list_downloads(self.db), [])
            self.assertEqual(mcp.review_for_chat(self.db, selection)["completion"], result)
            outcome = mcp.transfer_status(self.db, selection)
            self.assertEqual(outcome["completion"], result)
            request.reset_mock()
            self.assertEqual(queue(self.db, selection, original_id=original_id), outcome)
            request.assert_not_called()
            self.assertTrue(result["staging_cleared"])
            self.assertIsNone(result["staging_path"])
            self.assertFalse(folder.exists())

    @unittest.skipUnless(sys.platform == "darwin", "Requires exclusive macOS rename")
    def test_spectral_failure_blocks_destination_preview_without_moving_files(self):
        indexed, report = self.keep_both_fixture()
        staged = Path(report["items"][0]["source"])
        original_bytes = self.original.read_bytes()
        staged_bytes = staged.read_bytes()
        with patch("spliff.keep_both.review_download", return_value=report), \
                patch("spliff.keep_both.analyze_file", side_effect=ValueError("Decode failed")):
            with self.assertRaisesRegex(ValueError, "Decode failed"):
                keep_both.preview(self.db, self.album_selection, indexed["id"], "replace")
        self.assertEqual(self.original.read_bytes(), original_bytes)
        self.assertEqual(staged.read_bytes(), staged_bytes)
        self.assertFalse((self.root / "MP3").exists())
        self.assertFalse(self.original.with_suffix(".flac").exists())

    @unittest.skipUnless(sys.platform == "darwin", "Requires exclusive macOS rename")
    def test_keep_both_requires_confirmation_then_preserves_audio(self):
        original_bytes = self.original.read_bytes()
        candidate_bytes = self.candidate.read_bytes()
        indexed, report = self.keep_both_fixture()
        staged = Path(report["items"][0]["source"])
        unrelated = staged.with_name("Another copy.flac")
        unrelated.write_bytes(candidate_bytes)
        selection = self.album_selection
        with patch("spliff.keep_both.review_download", return_value=report):
            plan = keep_both.preview(self.db, selection, indexed["id"])
            self.assertTrue(self.original.exists())
            self.assertFalse((self.root / "MP3").exists())
            self.assertEqual(plan["archive"], str(self.root.resolve() / "MP3"))
            with self.assertRaisesRegex(ValueError, "comparison changed"):
                keep_both.apply(self.db, selection, indexed["id"], "incorrect")
            self.assertTrue(self.original.exists())
            self.assertTrue(staged.exists())
            result = keep_both.apply(self.db, selection, indexed["id"], plan["token"])
        self.assertEqual(result["staging_removed"], 1)
        self.assertFalse(result["staging_cleared"])
        self.assertEqual(result["staging_path"], str(staged.parents[1]))
        self.assertFalse(staged.exists())
        self.assertEqual(unrelated.read_bytes(), candidate_bytes)
        self.assertFalse(self.original.exists())
        self.assertEqual((self.root / "MP3" / self.original.name).read_bytes(), original_bytes)
        self.assertEqual(self.original.with_suffix(".flac").read_bytes(), candidate_bytes)
        self.assertEqual(self.candidate.read_bytes(), candidate_bytes)
        catalog = tracks(self.db)["items"]
        self.assertEqual(catalog[0]["id"], indexed["id"])
        self.assertTrue(catalog[0]["path"].startswith("MP3/"))
        self.assertEqual(catalog[1]["codec"], "flac")

    @unittest.skipUnless(sys.platform == "darwin", "Requires exclusive macOS rename")
    def test_replace_moves_original_to_trash_after_verification(self):
        original_bytes = self.original.read_bytes()
        indexed, report = self.keep_both_fixture()
        selection = self.album_selection
        with patch("spliff.keep_both.review_download", return_value=report):
            keep = keep_both.preview(self.db, selection, indexed["id"])
            replace = keep_both.preview(self.db, selection, indexed["id"], mode="replace")
            self.assertEqual(replace["archive"], "Mac Trash")
            with self.assertRaisesRegex(ValueError, "comparison changed"):
                keep_both.apply(self.db, selection, indexed["id"], keep["token"], mode="replace")
            self.assertTrue(self.original.exists())
            def trash(command, **_options):
                self.assertEqual(command[0], "/usr/bin/trash")
                self.assertEqual(self.original.with_suffix(".flac").read_bytes(), self.candidate.read_bytes())
                item = Path(command[1])
                self.assertTrue(item.is_file())
                self.assertEqual(item.name, self.original.name)
                test_trash = self.base / "Test Trash"
                test_trash.mkdir(exist_ok=True)
                item.rename(test_trash / item.name)

            with patch("spliff.keep_both.subprocess.run", side_effect=trash):
                result = keep_both.apply(self.db, selection, indexed["id"], replace["token"], mode="replace")
        self.assertEqual(result["staging_removed"], 1)
        self.assertTrue(result["staging_cleared"])
        self.assertFalse(Path(report["items"][0]["source"]).parents[1].exists())
        self.assertIsNone(result["trash_pending"])
        self.assertEqual((self.base / "Test Trash" / self.original.name).read_bytes(), original_bytes)
        self.assertFalse((self.root / ".spliff-originals").exists())
        self.assertEqual(self.original.with_suffix(".flac").read_bytes(), self.candidate.read_bytes())
        scan(self.db, self.root)
        self.assertEqual([item["codec"] for item in tracks(self.db)["items"]], ["flac"])
        with closing(connect(self.db)) as connection:
            saved = connection.execute("SELECT mode FROM kept_albums WHERE candidate=?", (selection,)).fetchone()
            self.assertEqual(saved["mode"], "replace")

    @unittest.skipUnless(sys.platform == "darwin", "Requires exclusive macOS rename")
    def test_replace_honors_hidden_recovery_preference(self):
        original_bytes = self.original.read_bytes()
        indexed, report = self.keep_both_fixture()
        selection = self.album_selection
        with patch("spliff.keep_both.review_download", return_value=report):
            previous = keep_both.preview(self.db, selection, indexed["id"], mode="replace")
            self.assertEqual(previous["archive"], "Mac Trash")
            replacement_preference(self.db, "hidden")
            self.assertEqual(replacement_preference(self.db)["destination"], "hidden")
            with self.assertRaisesRegex(ValueError, "comparison changed"):
                keep_both.apply(self.db, selection, indexed["id"], previous["token"], mode="replace")
            preview = keep_both.preview(self.db, selection, indexed["id"], mode="replace")
            result = keep_both.apply(self.db, selection, indexed["id"], preview["token"], mode="replace")
        archive = self.root / ".spliff-originals"
        self.assertEqual(Path(result["archive"]), archive.resolve())
        self.assertEqual((archive / self.original.name).read_bytes(), original_bytes)

    @unittest.skipUnless(sys.platform == "darwin", "Requires exclusive macOS rename")
    def test_keep_both_clears_staging_after_installing_a_recording_alignment_match(self):
        indexed, report = self.keep_both_fixture()
        staged = Path(report["items"][0]["source"])
        report["items"][0].update(assessment="aligned recording", recording_evidence={"matched": True})
        with patch.object(keep_both, "review_download", return_value=report), \
                patch.object(keep_both, "compare_measurements", return_value={"status": "Similar audio envelope"}):
            plan = keep_both.preview(self.db, self.album_selection, indexed["id"])
            self.assertTrue(staged.exists())
            result = keep_both.apply(self.db, self.album_selection, indexed["id"], plan["token"])
        self.assertTrue(result["staging_cleared"])
        self.assertFalse(staged.parents[1].exists())
        self.assertEqual(self.original.with_suffix(".flac").read_bytes(), self.candidate.read_bytes())
        self.assertTrue((self.root / "MP3" / self.original.name).is_file())

    @unittest.skipUnless(sys.platform == "darwin", "Requires exclusive macOS rename")
    def test_partial_install_keeps_the_skipped_download_in_staging(self):
        indexed, report = self.keep_both_fixture()
        staged = Path(report["items"][0]["source"])
        job = staged.parents[1]
        skipped = job / "002" / "Bonus track.flac"
        skipped.parent.mkdir()
        shutil.copyfile(self.candidate, skipped)
        receipt = job / "job.json"
        manifest = json.loads(receipt.read_text())
        manifest["entries"].append({"directory": "002", "peer": "test",
                                    "remote": skipped.name, "bytes": skipped.stat().st_size})
        receipt.write_text(json.dumps(manifest))
        report["items"].append({"name": skipped.name, "source": str(skipped), "assessment": "no matching title"})
        report["complete"] = 2
        with patch.object(keep_both, "review_download", return_value=report):
            plan = keep_both.preview(self.db, self.album_selection, indexed["id"])
            result = keep_both.apply(self.db, self.album_selection, indexed["id"], plan["token"])
        self.assertEqual(result["staging_removed"], 1)
        self.assertFalse(result["staging_cleared"])
        self.assertEqual(result["staging_path"], str(job))
        self.assertFalse(staged.parent.exists())
        self.assertEqual(skipped.read_bytes(), self.candidate.read_bytes())
        self.assertTrue(receipt.is_file())

    @unittest.skipUnless(sys.platform == "darwin", "Requires exclusive macOS rename")
    def test_trash_failure_keeps_the_verified_original_and_download_recoverable(self):
        indexed, report = self.keep_both_fixture()
        selection = self.album_selection
        original_bytes = self.original.read_bytes()
        with patch.object(keep_both, "review_download", return_value=report):
            plan = keep_both.preview(self.db, selection, indexed["id"], "replace")
            with patch.object(keep_both.subprocess, "run", side_effect=OSError("Trash unavailable")):
                result = keep_both.apply(self.db, selection, indexed["id"], plan["token"], "replace")
        self.assertEqual((Path(result["trash_pending"]) / self.original.name).read_bytes(), original_bytes)
        self.assertTrue((Path(result["recovery_path"]) / "plan.json").is_file())
        self.assertTrue(Path(report["items"][0]["source"]).is_file())
        self.assertEqual(self.original.with_suffix(".flac").read_bytes(), self.candidate.read_bytes())

    @unittest.skipUnless(sys.platform == "darwin", "Requires exclusive macOS rename")
    def test_keep_both_rolls_back_when_publication_fails(self):
        indexed, report = self.keep_both_fixture()
        original_bytes = self.original.read_bytes()
        selection = self.album_selection
        move = keep_both.move_exclusive

        def fail_on_flac(source, destination, **kwargs):
            if destination.suffix == ".flac":
                raise OSError("Simulated publication failure")
            return move(source, destination, **kwargs)

        with patch("spliff.keep_both.review_download", return_value=report):
            plan = keep_both.preview(self.db, selection, indexed["id"])
            with patch("spliff.keep_both.move_exclusive", side_effect=fail_on_flac):
                with self.assertRaisesRegex(OSError, "publication failure"):
                    keep_both.apply(self.db, selection, indexed["id"], plan["token"])
        self.assertEqual(self.original.read_bytes(), original_bytes)
        self.assertFalse((self.root / "MP3").exists())
        self.assertFalse((self.root / keep_both.WORK_DIR).exists())
        self.assertEqual(tracks(self.db)["items"][0]["path"], indexed["path"])
        self.assertEqual(Path(report["items"][0]["source"]).read_bytes(), self.candidate.read_bytes())

    @unittest.skipUnless(sys.platform == "darwin", "Requires exclusive macOS rename")
    def test_failed_flac_verification_keeps_the_download_and_mp3(self):
        indexed, report = self.keep_both_fixture()
        selection = self.album_selection
        source = Path(report["items"][0]["source"])
        original_bytes, staged_bytes = self.original.read_bytes(), source.read_bytes()
        move = keep_both.move_exclusive

        def corrupt_published_flac(temporary, final, **kwargs):
            fingerprint = move(temporary, final, **kwargs)
            if final.suffix == ".flac":
                final.write_bytes(b"\0" * len(staged_bytes))
            return fingerprint

        with patch("spliff.keep_both.review_download", return_value=report):
            plan = keep_both.preview(self.db, selection, indexed["id"])
            with patch("spliff.keep_both.move_exclusive", side_effect=corrupt_published_flac):
                with self.assertRaisesRegex(ValueError, "Check the recovery plan"):
                    keep_both.apply(self.db, selection, indexed["id"], plan["token"])
        self.assertEqual(self.original.read_bytes(), original_bytes)
        self.assertEqual(source.read_bytes(), staged_bytes)
        self.assertEqual(self.original.with_suffix(".flac").read_bytes(), b"\0" * len(staged_bytes))

    @unittest.skipUnless(sys.platform == "darwin", "Requires exclusive macOS rename")
    def test_archive_failure_keeps_original_and_rolls_back_replacement(self):
        indexed, report = self.keep_both_fixture()
        original_bytes = self.original.read_bytes()
        move = keep_both.move_exclusive

        def fail_archive(source, destination, **kwargs):
            if source == self.original.resolve():
                self.assertTrue(self.original.with_suffix(".flac").exists())
                raise OSError("Simulated archive failure")
            return move(source, destination, **kwargs)

        with patch("spliff.keep_both.review_download", return_value=report):
            plan = keep_both.preview(self.db, self.album_selection, indexed["id"], "replace")
            with patch("spliff.keep_both.move_exclusive", side_effect=fail_archive):
                with self.assertRaisesRegex(OSError, "archive failure"):
                    keep_both.apply(self.db, self.album_selection, indexed["id"], plan["token"], "replace")
        self.assertEqual(self.original.read_bytes(), original_bytes)
        self.assertFalse(self.original.with_suffix(".flac").exists())
        self.assertTrue(Path(report["items"][0]["source"]).exists())

    @unittest.skipUnless(sys.platform == "darwin", "Requires exclusive macOS rename")
    def test_replacement_preview_rejects_changed_original_and_destination_collision(self):
        indexed, report = self.keep_both_fixture()
        existing = self.original.with_suffix(".flac")
        existing.write_bytes(b"existing file")
        with patch("spliff.keep_both.review_download", return_value=report):
            with self.assertRaisesRegex(ValueError, "same name"):
                keep_both.preview(self.db, self.album_selection, indexed["id"], "replace")
            self.original.write_bytes(b"modified original")
            with self.assertRaisesRegex(ValueError, "audio file changed"):
                keep_both.preview(self.db, self.album_selection, indexed["id"], "replace")
        self.assertEqual(existing.read_bytes(), b"existing file")
        self.assertEqual(self.original.read_bytes(), b"modified original")

    @unittest.skipUnless(sys.platform == "darwin", "Requires exclusive macOS rename")
    def test_cleanup_failure_leaves_the_successful_import_in_place(self):
        indexed, report = self.keep_both_fixture()
        selection = self.album_selection
        source = Path(report["items"][0]["source"])
        original_bytes, staged_bytes = self.original.read_bytes(), source.read_bytes()
        unlink = keep_both.os.unlink

        def block_staged_unlink(path, **options):
            if path == source.name:
                raise PermissionError("Simulated staging cleanup failure")
            return unlink(path, **options)

        with patch("spliff.keep_both.review_download", return_value=report):
            plan = keep_both.preview(self.db, selection, indexed["id"])
            with patch("spliff.keep_both.os.unlink", side_effect=block_staged_unlink):
                result = keep_both.apply(self.db, selection, indexed["id"], plan["token"])
        self.assertEqual(result["staging_removed"], 0)
        self.assertEqual(source.read_bytes(), staged_bytes)
        self.assertEqual(self.original.with_suffix(".flac").read_bytes(), staged_bytes)
        self.assertEqual((self.root / "MP3" / self.original.name).read_bytes(), original_bytes)
        self.assertEqual([item["codec"] for item in tracks(self.db)["items"]], ["mp3", "flac"])

    @unittest.skipUnless(sys.platform == "darwin", "Requires exclusive macOS rename")
    def test_local_approval_is_required_and_bound_to_one_displayed_plan(self):
        indexed, report = self.keep_both_fixture()
        selection = self.album_selection
        replacement_preference(self.db, "hidden")
        started = Queue()

        def bind(address, handler):
            server = ThreadingHTTPServer(address, handler)
            started.put(server)
            return server

        with patch.object(keep_both, "review_download", return_value=report), \
                patch.object(web, "ThreadingHTTPServer", side_effect=bind), \
                patch.object(sync, "run_worker"):
            thread = Thread(target=web.serve, args=(self.db, 0), daemon=True)
            thread.start()
            server = started.get(timeout=5)
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                headers = {"Origin": base, "X-Spliff-Action": "dashboard"}
                comparison = {"candidate": selection, "original": indexed["id"]}
                options = base + "/api/upgrade-options?" + urlencode(comparison)

                def post(url, request_headers):
                    with urlopen(Request(url, data=b"", headers=request_headers), timeout=10) as response:
                        return json.load(response)

                plans = post(options, headers)["options"]
                token = plans["replace"]["token"]
                apply_url = base + "/api/keep-both?" + urlencode({**comparison, "mode": "replace", "token": token})
                approval = {"X-Spliff-Approval": plans["replace"]["approval"]}
                for denied, status in (
                    (headers, 400),
                    ({**headers, "X-Spliff-Approval": plans["keep"]["approval"]}, 400),
                    ({**headers, **approval, "Origin": "https://example.com"}, 403),
                ):
                    with self.assertRaises(HTTPError) as rejected:
                        post(apply_url, denied)
                    self.assertEqual(rejected.exception.code, status)
                    rejected.exception.close()
                    self.assertTrue(self.original.exists())
                result = post(apply_url, {**headers, **approval})
                self.assertEqual(result["upgraded"], 1)
                self.assertTrue((Path(result["archive"]) / self.original.name).is_file())
                self.assertTrue(result["staging_cleared"])
                self.assertFalse(Path(report["items"][0]["source"]).parents[1].exists())
                with self.assertRaises(HTTPError) as repeated:
                    post(apply_url, {**headers, **approval})
                self.assertIn("Open the local upgrade review again", json.load(repeated.exception)["error"])
                repeated.exception.close()
            finally:
                server.shutdown()
                thread.join(timeout=5)

    @unittest.skipUnless(sys.platform == "darwin", "Requires exclusive macOS rename")
    def test_upgrade_refuses_a_library_swapped_after_plan_validation(self):
        indexed, report = self.keep_both_fixture()
        selection = self.album_selection
        original_bytes = self.original.read_bytes()
        outside = self.base.resolve() / "Unrelated"
        outside.mkdir()
        sentinel = outside / self.original.name
        sentinel.write_bytes(b"unrelated")
        detached = self.base.resolve() / "Detached library"
        create = keep_both.create_directory

        def swap_before_work(path, identities):
            self.root.rename(detached)
            self.root.symlink_to(outside, target_is_directory=True)
            return create(path, identities)

        with patch.object(keep_both, "review_download", return_value=report):
            plan = keep_both.preview(self.db, selection, indexed["id"], "replace")
            with patch.object(keep_both, "create_directory", side_effect=swap_before_work):
                with self.assertRaises((OSError, ValueError)):
                    keep_both.apply(self.db, selection, indexed["id"], plan["token"], "replace")
        self.assertEqual(sentinel.read_bytes(), b"unrelated")
        self.assertEqual(list(outside.iterdir()), [sentinel])
        self.assertEqual((detached / self.original.name).read_bytes(), original_bytes)

    @unittest.skipUnless(sys.platform == "darwin", "Requires exclusive macOS rename")
    def test_cleanup_preserves_an_unexpected_workspace_file_and_recovery_plan(self):
        indexed, report = self.keep_both_fixture()
        selection = self.album_selection
        unexpected = self.root / keep_both.WORK_DIR / "audio" / "Personal notes"
        move = keep_both.move_exclusive

        def add_unexpected(source, destination, **kwargs):
            if destination.suffix == ".flac":
                unexpected.write_bytes(b"keep me")
            return move(source, destination, **kwargs)

        with patch.object(keep_both, "review_download", return_value=report):
            plan = keep_both.preview(self.db, selection, indexed["id"])
            with patch.object(keep_both, "move_exclusive", side_effect=add_unexpected):
                result = keep_both.apply(self.db, selection, indexed["id"], plan["token"])
        self.assertEqual(unexpected.read_bytes(), b"keep me")
        self.assertTrue(result["work_cleanup_pending"])
        self.assertTrue((Path(result["recovery_path"]) / "plan.json").is_file())

    @unittest.skipUnless(sys.platform == "darwin", "Requires exclusive macOS rename")
    def test_crash_after_first_original_move_leaves_a_recovery_plan(self):
        indexed, report = self.keep_both_fixture()
        selection = self.album_selection
        original_bytes = self.original.read_bytes()
        staged = Path(report["items"][0]["source"])
        with patch.object(keep_both, "review_download", return_value=report):
            plan = keep_both.preview(self.db, selection, indexed["id"], "replace")
        script = """
import json, os, sys
from pathlib import Path
from unittest.mock import patch
from spliff import keep_both
report = json.load(sys.stdin)
db, selection, original, token, staging = sys.argv[1:]
move = keep_both.move_exclusive
def interrupt(source, destination, **kwargs):
    fingerprint = move(source, destination, **kwargs)
    if source.suffix == ".mp3":
        os._exit(42)
    return fingerprint
with patch.object(keep_both, "review_download", return_value=report), \\
     patch.object(keep_both, "staging_root", return_value=Path(staging)), \\
     patch.object(keep_both, "move_exclusive", side_effect=interrupt):
    keep_both.apply(Path(db), selection, int(original), token, "replace")
"""
        unrelated = self.root / "Personal notes"
        unrelated.write_bytes(b"keep me")
        killed = subprocess.run(
            [sys.executable, "-c", script, str(self.db), selection, str(indexed["id"]),
             plan["token"], str(staged.parents[2])],
            input=json.dumps(report), text=True, capture_output=True, timeout=15)
        self.assertEqual(killed.returncode, 42, killed.stderr)
        journal = self.root / keep_both.WORK_DIR / "plan.json"
        pair = json.loads(journal.read_text())["pairs"][0]
        self.assertEqual(Path(pair["recovery"]).read_bytes(), original_bytes)
        self.assertEqual(Path(pair["installed"]).read_bytes(), self.candidate.read_bytes())
        self.assertEqual(unrelated.read_bytes(), b"keep me")
        self.assertTrue(staged.exists())
        self.assertFalse(self.original.exists())
        with patch.object(keep_both, "review_download", return_value=report):
            with self.assertRaises((OSError, ValueError)):
                keep_both.apply(self.db, selection, indexed["id"], plan["token"], "replace")

    @unittest.skipUnless(sys.platform == "darwin", "Exercises the macOS fallback")
    def test_keep_both_copy_fallback_refuses_conflicts(self):
        source = self.original.resolve()
        destination = source.parent / "copy.mp3"
        expected = source.read_bytes()
        destination.write_bytes(b"pre-existing")
        with keep_both.open_directory(source.parent, {}) as directory:
            fingerprint = keep_both.path_fingerprint(source)
            with self.assertRaises(FileExistsError):
                keep_both.copy_then_remove(directory, source.name, directory, destination.name, fingerprint)
            self.assertEqual(source.read_bytes(), expected)
            self.assertEqual(destination.read_bytes(), b"pre-existing")
            destination.unlink()
            keep_both.copy_then_remove(directory, source.name, directory, destination.name, fingerprint)
        self.assertFalse(source.exists())
        self.assertEqual(destination.read_bytes(), expected)

    def test_mcp_and_http(self):
        scan(self.db, self.root)
        with closing(connect(self.db)) as connection, connection:
            connection.execute("UPDATE tracks SET album='Example Album'")
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
        self.assertEqual(len(listing["result"]["tools"]), 8)
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
                with urlopen(base + "/api/albums?q=example", timeout=5) as result:
                    listing = json.load(result)
                self.assertTrue(listing["has_library"])
                self.assertEqual([(album["album"], album["mp3"]) for album in listing["items"]],
                                 [("Example Album", 1)])
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
                second = self.base / "Another music folder"
                second.mkdir()
                shutil.copyfile(self.candidate, second / "Lossless.flac")
                missing = self.base / "Missing folder"
                selections = [str(self.root), str(second), str(missing), str(second)]
                request = Request(base + "/api/scan", data=json.dumps(selections).encode(), headers=headers)
                with urlopen(request, timeout=5) as reply:
                    self.assertEqual(reply.status, 202)
                for _ in range(100):
                    with urlopen(base + "/api/scan-progress", timeout=5) as reply:
                        progress = json.load(reply)["scan"]
                    if progress["current"] is None:
                        break
                    sleep(.05)
                self.assertEqual((progress["completed"], progress["total"]), (3, 3))
                self.assertEqual([failure["folder"] for failure in progress["failures"]], [str(missing.resolve())])
                with urlopen(base + "/api/sync", timeout=5) as reply:
                    self.assertCountEqual(json.load(reply)["folders"], [str(folder.resolve()) for folder in (self.root, second, missing)])
                with urlopen(base + "/api/tracks?scope=lossless", timeout=5) as reply:
                    self.assertEqual(json.load(reply)["items"][0]["path"], "Lossless.flac")

            finally:
                server.terminate()
