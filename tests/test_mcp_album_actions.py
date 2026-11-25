import base64
import json
import os
import re
import shutil
import sys
import subprocess
import unittest
from contextlib import closing, redirect_stdout
from io import StringIO
from pathlib import Path
from queue import Queue
from threading import Thread
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4
from unittest.mock import patch

import test_library_import
from spliff import album_actions, album_rollback, keep_both, mcp, web
from spliff.catalog import connect, replacement_preference
from spliff.scanner import scan


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "Requires macOS and FFmpeg")
class MCPAlbumActions(unittest.TestCase):
    candidate = test_library_import.FirstTimeImport.candidate
    setUp = test_library_import.FirstTimeImport.setUp
    stage = test_library_import.FirstTimeImport.stage

    def original(self, *, protected=False, filename="01 Song 1.mp3"):
        folder = self.root / "Fixture Artist" / "Albums" / "[1995] Fixture Album"
        folder.mkdir(parents=True)
        path = folder / filename
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=220:duration=3.5",
                        "-metadata", "title=Song 1", "-metadata", "artist=Fixture Artist",
                        "-metadata", "album=Fixture Album", "-metadata", "date=1995", str(path)], check=True)
        scan(self.db, self.root)
        with closing(connect(self.db)) as connection, connection:
            track = connection.execute("SELECT id FROM tracks").fetchone()["id"]
            if protected:
                connection.execute("UPDATE tracks SET protected=1 WHERE id=?", (track,))
        return path, track

    def test_stdio_review_images_denial_and_keep_both_commit(self):
        self.stage((1, 2))
        original, track = self.original()
        original_bytes = original.read_bytes()
        output = StringIO()

        def packet(request_id, method, params):
            return json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})

        def call(request_id, name, **arguments):
            return packet(request_id, "tools/call", {"name": name, "arguments": arguments})

        def response(request_id):
            return next(value["result"] for value in map(json.loads, output.getvalue().splitlines())
                        if value.get("id") == request_id)

        def approval(action):
            request = json.loads(output.getvalue().splitlines()[-1])
            self.assertEqual(request["method"], "elicitation/create")
            self.assertIn("[2024] Fixture Album", request["params"]["message"])
            return json.dumps({"jsonrpc": "2.0", "id": request["id"],
                               "result": {"action": action, "content": {"authorize": True}}})

        def incoming():
            yield packet(1, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {"elicitation": {}}})
            yield packet(2, "tools/list", {})
            tools = {item["name"]: item for item in response(2)["tools"]}
            self.assertTrue(tools["apply_album_action"]["annotations"]["destructiveHint"])
            yield call(3, "review_nicotine_download", candidate_id=self.candidate, original_track_id=track)
            options = response(3)["structuredContent"]["album_decision"]
            plan = options["options"]["keep"]
            self.assertIn("replace", options["blocked"])
            yield call(4, "analyze_download_spectrum", candidate_id=self.candidate, original_track_id=track)
            images = [item for item in response(4)["content"] if item["type"] == "image"]
            self.assertEqual(len(images), 2)
            for image in images:
                self.assertTrue(base64.b64decode(image["data"]).startswith(b"\x89PNG"))
            args = dict(candidate_id=self.candidate, original_track_id=track, mode="keep", plan_id=plan["plan_id"])
            yield call(5, "apply_album_action", **args, authorize=True)
            yield call(6, "apply_album_action", **args)
            yield approval("decline")
            self.assertTrue(response(6)["isError"])
            self.assertFalse(Path(plan["destination"]).exists())
            yield call(7, "apply_album_action", **args)
            yield approval("accept")
            committed = response(7)["structuredContent"]
            self.assertEqual(committed["status"], "completed")
            self.assertEqual(len(committed["files_imported"]), 2)
            self.assertEqual(original.read_bytes(), original_bytes)
            self.assertTrue(all(Path(path).exists() for path in committed["files_imported"]))
            yield call(8, "apply_album_action", **args)
            self.assertEqual(response(8)["structuredContent"], committed)

        with patch("sys.stdin", incoming()), redirect_stdout(output), \
                patch("spliff.keep_both.staging_root", return_value=self.staging):
            mcp.serve(self.db, allow_downloads=True)

    def test_override_and_hidden_rollback(self):
        self.stage()
        original, track = self.original()
        expected = original.read_bytes()
        replacement_preference(self.db, "hidden")
        with patch("spliff.keep_both.staging_root", return_value=self.staging):
            with self.assertRaisesRegex(ValueError, "uncertain"):
                album_actions.preview(self.db, self.candidate, "replace", track)
            plan = album_actions.preview(self.db, self.candidate, "replace", track, override_track_ids=[track])
            evidence = plan["files"][0]["recording_evidence"]
            self.assertTrue(evidence["overridden"])
            self.assertFalse(evidence["matched"])
            outcome = album_actions.apply(self.db, self.candidate, "replace", plan["plan_id"],
                                          original_track_id=track, confirm=lambda _: True)
        self.assertFalse(original.exists())
        self.assertTrue(outcome["rollback_available"])
        rolled_back = album_rollback.rollback(self.db, self.candidate, outcome["transaction_id"],
                                               confirm=lambda _: True)
        self.assertEqual(original.read_bytes(), expected)
        self.assertFalse(original.with_suffix(".flac").exists())
        self.assertEqual(rolled_back["status"], "rolled_back")
        with self.assertRaises(ValueError):
            album_rollback.rollback(self.db, self.candidate, outcome["transaction_id"], confirm=lambda _: True)

    def test_expired_wrong_scope_and_changed_destination(self):
        self.stage()
        _, track = self.original()
        plan = album_actions.preview(self.db, self.candidate, "keep", track)
        with self.assertRaisesRegex(ValueError, "does not match"):
            album_actions.apply(self.db, self.candidate, "replace", plan["plan_id"],
                                original_track_id=track, confirm=lambda _: self.fail("Unexpected authorization"))
        with patch("spliff.album_actions.time", return_value=plan["expires_at"]):
            with self.assertRaisesRegex(ValueError, "expired"):
                album_actions.apply(self.db, self.candidate, "keep", plan["plan_id"],
                                    original_track_id=track, confirm=lambda _: self.fail("Unexpected authorization"))
        Path(plan["destination"]).mkdir()
        with self.assertRaisesRegex(ValueError, "already exists"):
            album_actions.apply(self.db, self.candidate, "keep", plan["plan_id"],
                                original_track_id=track, confirm=lambda _: True)
        self.assertTrue(self.job.exists())

    def test_protected_original_cannot_be_overridden(self):
        self.stage()
        original, track = self.original(protected=True)
        replacement_preference(self.db, "hidden")
        with patch("spliff.keep_both.staging_root", return_value=self.staging):
            with self.assertRaises(ValueError):
                album_actions.preview(self.db, self.candidate, "replace", track, override_track_ids=[track])
        self.assertTrue(original.exists())

    def test_verified_replacement_reindexes_real_audio(self):
        source, = self.stage()
        original, track = self.original()
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                        "anoisesrc=duration=12:color=pink:seed=27", "-metadata", "title=Song 1",
                        "-metadata", "artist=Fixture Artist", "-metadata", "album=Fixture Album",
                        "-metadata", "date=2024", str(source)], check=True)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(source), "-q:a", "1", str(original)],
                       check=True)
        receipt = self.job / "job.json"
        job = json.loads(receipt.read_text())
        job["entries"][0]["bytes"] = source.stat().st_size
        receipt.write_text(json.dumps(job))
        scan(self.db, self.root)
        replacement_preference(self.db, "hidden")
        with patch("spliff.keep_both.staging_root", return_value=self.staging):
            plan = album_actions.preview(self.db, self.candidate, "replace", track)
            self.assertEqual(plan["files"][0]["recording_evidence"]["confidence"], "verified match")
            result = album_actions.apply(self.db, self.candidate, "replace", plan["plan_id"],
                                         original_track_id=track, confirm=lambda _: True)
        self.assertEqual(result["files_replaced"], [str(original)])
        self.assertTrue(original.with_suffix(".flac").is_file())
        with closing(connect(self.db)) as connection:
            indexed = connection.execute("SELECT codec,present FROM tracks ORDER BY id").fetchall()
        self.assertEqual([tuple(row) for row in indexed], [("mp3", 0), ("flac", 1)])

    def test_filesystem_failures_leave_original_and_staging(self):
        self.stage()
        original, track = self.original()
        with patch("spliff.library_import.shutil.disk_usage", return_value=type("Usage", (), {"free": 0})()):
            with self.assertRaisesRegex(ValueError, "free space"):
                album_actions.preview(self.db, self.candidate, "keep", track)
        plan = album_actions.preview(self.db, self.candidate, "keep", track)
        with patch("spliff.keep_both.verify_published", side_effect=ValueError("Injected publish failure")):
            with self.assertRaisesRegex(ValueError, "publish failure"):
                album_actions.apply(self.db, self.candidate, "keep", plan["plan_id"],
                                    original_track_id=track, confirm=lambda _: True)
        self.assertTrue(original.exists())
        self.assertTrue(self.job.exists())
        self.assertFalse(Path(plan["destination"]).exists())
        with self.assertRaisesRegex(ValueError, "already used"):
            album_actions.apply(self.db, self.candidate, "keep", plan["plan_id"],
                                original_track_id=track, confirm=lambda _: self.fail("Replayed authorization"))
        plan = album_actions.preview(self.db, self.candidate, "keep", track)
        symlink_plan = album_actions.preview(self.db, self.candidate, "keep", track)
        offline = self.root.with_name("offline")
        self.root.rename(offline)
        with self.assertRaises(OSError):
            album_actions.apply(self.db, self.candidate, "keep", plan["plan_id"],
                                original_track_id=track, confirm=lambda _: True)
        self.root.symlink_to(offline, target_is_directory=True)
        with self.assertRaises(OSError):
            album_actions.apply(self.db, self.candidate, "keep", symlink_plan["plan_id"],
                                original_track_id=track, confirm=lambda _: True)

    def test_unavailable_transfer_offers_search_without_new_queue(self):
        status = {"id": self.candidate, "complete": 0, "items": [
            {"name": "Song.flac", "status": "User logged off", "file": None}]}
        with patch("spliff.mcp.read_transfer_status", return_value=status), \
                patch("spliff.mcp.nicotine_request") as request:
            result = mcp.transfer_status(self.db, self.candidate)
        self.assertEqual(result["state"], "peer_offline")
        self.assertEqual(result["recovery"]["find_alternatives"], "find_nicotine_alternatives")
        request.assert_not_called()

    def test_til_shiloh_regression_shape(self):
        # Generated tones represent the reported 16-track library and its 17-track reissue.
        self.candidate = "album-b26e0b46920a40a68943fd4597dfa60d-132"
        self.job = self.staging / self.candidate
        folder = self.root / "Buju Banton" / "Albums" / "[1995] 'Til Shiloh"
        folder.mkdir(parents=True)
        entries = []
        originals = {}
        special = {4: "Champion", 14: "What Ya Gonna Do?", 17: "Sensemilia Persecution"}
        for position in range(1, 18):
            title = special.get(position, f"Fixture track {position}")
            directory = self.job / f"{position:03d}"
            directory.mkdir(parents=True)
            source = directory / f"{position:02d}.flac"
            metadata = ["-metadata", f"title={title}", "-metadata", "artist=Buju Banton",
                        "-metadata", "album='Til Shiloh", "-metadata", f"track={position}"]
            generate = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i"]
            subprocess.run([*generate, "sine=duration=0.25", *metadata, "-metadata", "date=2002", str(source)],
                           check=True)
            entries.append({"remote": f"Fixture Album\\{position:02d} {title}.flac", "directory": directory.name,
                            "peer": "fixture", "bytes": source.stat().st_size})
            if position < 17:
                lossy = position in {4, 14}
                original = folder / f"{position:02d}.{('mp3' if lossy else 'flac')}"
                subprocess.run([*generate, f"sine=frequency=220:duration={3.2 if lossy else 0.25}", *metadata,
                                "-metadata", "date=1995", str(original)], check=True)
                originals[original] = original.read_bytes()
        (self.job / "job.json").write_text(json.dumps(
            {"id": self.candidate, "kind": "album", "query": "Buju Banton 'Til Shiloh",
             "created": 1, "entries": entries}))
        scan(self.db, self.root)
        with closing(connect(self.db)) as connection:
            track = connection.execute("SELECT id FROM tracks WHERE title='Champion'").fetchone()["id"]
        replacement_preference(self.db, "hidden")
        with patch("spliff.keep_both.staging_root", return_value=self.staging):
            review = mcp.review_for_chat(self.db, self.candidate, track, host_confirmation=True)
            decision = review["album_decision"]
            self.assertIn("replace", decision["blocked"])
            self.assertEqual(review["review_summary"]["existing_formats"], {"flac": 14, "mp3": 2})
            self.assertTrue(any("Sensemilia Persecution" in name
                                for name in review["review_summary"]["additional_track_candidates"]))
            plan = decision["options"]["keep"]
            self.assertTrue(plan["destination"].endswith("[2002] 'Til Shiloh"))
            result = album_actions.apply(self.db, self.candidate, "keep", plan["plan_id"],
                                         original_track_id=track, confirm=lambda _: True)
        self.assertEqual(len(result["files_imported"]), 17)
        self.assertTrue(any(path.endswith("17 Sensemilia Persecution.flac") for path in result["files_imported"]))
        for original, expected in originals.items():
            self.assertEqual(original.read_bytes(), expected)

    def test_tunnel_rediscovery_resets_form_capability(self):
        def request(request_id, method, **params):
            return json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})

        version = {"protocolVersion": "2025-11-25"}
        requests = [
            request(1, "initialize", **version, capabilities={"elicitation": {"form": {}}}),
            request(2, "tools/list"),
            request(3, "initialize", **version, capabilities={}),
            request(4, "tools/list"),
            request(5, "tools/call", name="apply_album_action",
                    arguments={"candidate_id": self.candidate, "mode": "keep", "plan_id": "1" * 32}),
        ]
        output = StringIO()
        with patch("sys.stdin", StringIO("\n".join(requests))), redirect_stdout(output):
            mcp.serve(self.db, allow_downloads=True)
        first, tools, second, updated, blocked = [json.loads(line)["result"] for line in output.getvalue().splitlines()]
        self.assertEqual(first["serverInfo"], second["serverInfo"])
        self.assertIn("apply_album_action", {tool["name"] for tool in tools["tools"]})
        names = {tool["name"] for tool in updated["tools"]}
        self.assertIn("apply_album_action", names)
        self.assertIn("get_album_action_status", names)
        self.assertTrue(blocked["isError"])
        self.assertIn("expired", blocked["structuredContent"]["message"])

    def test_openai_extension_negotiates_its_legacy_method(self):
        caps = {"extensions": {"openai/elicitation": {"form": {}}}}
        self.assertEqual(mcp.form_method("2025-11-25", caps), "openai/elicitation/create")
        self.assertIsNone(mcp.form_method("2026-07-28", caps))
        self.assertIsNone(mcp.form_method("2025-11-25", {"extensions": {"openai/elicitation": {"url": {}}}}))
        self.assertIsNone(mcp.form_method("2025-11-25", {}))

    def test_local_http_approval_replaces_exact_review_and_reports_to_mcp(self):
        source, = self.stage()
        original, track = self.original()
        expected = original.read_bytes()
        replacement_preference(self.db, "hidden")
        started = Queue()

        def bind(address, handler):
            server = ThreadingHTTPServer(address, handler)
            started.put(server)
            return server

        with patch.object(web, "ThreadingHTTPServer", side_effect=bind), patch.object(web.sync, "run_worker"), \
                patch.object(keep_both, "staging_root", return_value=self.staging):
            thread = Thread(target=web.serve, args=(self.db, 0), daemon=True)
            thread.start()
            server = started.get(timeout=5)
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                plan = album_actions.preview(self.db, self.candidate, "replace", track, override_track_ids=[track])
                url = mcp.local_approval_url(server.server_port, plan["plan_id"])

                def get_form():
                    with urlopen(url, timeout=10) as response:
                        page = response.read().decode()
                    self.assertIn(str(source), page)
                    self.assertIn(str(original.with_suffix(".flac")), page)
                    self.assertIn("Recording exception", page)
                    return re.search(r'name="approval" value="([^"]+)"', page)[1]

                def post(approval, decision, origin=base):
                    form = urlencode({"plan": plan["plan_id"], "approval": approval, "decision": decision})
                    return urlopen(Request(base + "/album-approval", data=form.encode(),
                                           headers={"Origin": origin}), timeout=15)

                token = get_form()
                with self.assertRaises(HTTPError) as denied:
                    post(token, "approve", origin="https://example.invalid")
                self.assertEqual(denied.exception.code, 403)
                with post(token, "decline") as response:
                    self.assertIn(b"No files were changed", response.read())
                with self.assertRaises(HTTPError):
                    post(token, "approve")
                self.assertEqual(original.read_bytes(), expected)
                with post(get_form(), "approve") as response:
                    self.assertIn(b"Completed", response.read())
                result = album_actions.status(self.db, self.candidate)
                self.assertEqual(result["transaction_id"], plan["plan_id"])
                self.assertTrue(result["rollback_available"])
                self.assertEqual(Path(result["files_archived"][0]).read_bytes(), expected)
                self.assertFalse(original.exists())
            finally:
                server.shutdown()
                thread.join(timeout=5)

    def test_interrupted_cleanup_leaves_readable_journal_and_preserves_original(self):
        self.stage()
        original, track = self.original()
        replacement_preference(self.db, "hidden")
        with patch.object(keep_both, "staging_root", return_value=self.staging):
            plan = album_actions.preview(self.db, self.candidate, "replace", track, override_track_ids=[track])
            with patch.object(keep_both, "verify_published", side_effect=ValueError("interrupted publication")), \
                    patch.object(keep_both, "clean_work", side_effect=OSError("interrupted cleanup")):
                with self.assertRaisesRegex(ValueError, "recovery plan"):
                    album_actions.apply(self.db, self.candidate, "replace", plan["plan_id"],
                                        original_track_id=track, confirm=lambda _: True)
        journal, = album_actions.inspect_recovery(self.db, self.candidate)["journals"]
        self.assertEqual(journal["files"][0]["mp3"], str(original))
        before = Path(journal["journal"]).read_bytes()
        self.assertEqual(album_actions.status(self.db, self.candidate)["status"], "recovery_needed")
        self.assertTrue(original.exists())
        self.assertTrue(self.job.exists())
        self.assertEqual(Path(journal["journal"]).read_bytes(), before)

    @unittest.skipUnless(os.getenv("SPLIFF_TEST_MAC_TRASH") == "1", "Opt-in test writes one disposable file to Mac Trash")
    def test_real_mac_trash_after_verified_flac_install(self):
        source, = self.stage()
        original, track = self.original(filename=f"Spliff-test-{uuid4().hex}.mp3")
        expected = original.read_bytes()
        download = source.read_bytes()
        run = subprocess.run

        def observe(command, **kwargs):
            if command[0] == keep_both.TRASH:
                self.assertEqual(original.with_suffix(".flac").read_bytes(), download)
                self.assertEqual(Path(command[1]).read_bytes(), expected)
            return run(command, **kwargs)

        with patch.object(keep_both, "staging_root", return_value=self.staging), \
                patch.object(keep_both.subprocess, "run", side_effect=observe):
            plan = album_actions.preview(self.db, self.candidate, "replace", track, override_track_ids=[track])
            result = album_actions.apply(self.db, self.candidate, "replace", plan["plan_id"],
                                         original_track_id=track, confirm=lambda _: True)
        self.assertEqual(result["status"], "completed")
        self.assertIsNone(result["trash_pending"])
        self.assertFalse(original.exists())
        self.assertEqual(original.with_suffix(".flac").read_bytes(), download)

    def test_plan_changed_inside_confirmation_requires_fresh_approval(self):
        source, = self.stage()
        original, track = self.original()
        replacement_preference(self.db, "hidden")
        with patch.object(keep_both, "staging_root", return_value=self.staging):
            plan = album_actions.preview(self.db, self.candidate, "replace", track, override_track_ids=[track])

            def approve_and_change_destination(_):
                original.with_suffix(".flac").write_bytes(b"new destination occupant")
                return True

            with self.assertRaises(ValueError):
                album_actions.apply(self.db, self.candidate, "replace", plan["plan_id"], original_track_id=track,
                                    confirm=approve_and_change_destination)
            with self.assertRaisesRegex(ValueError, "already used"):
                album_actions.apply(self.db, self.candidate, "replace", plan["plan_id"], original_track_id=track,
                                    confirm=lambda _: self.fail("Authorization replayed"))
        self.assertTrue(source.exists())
        self.assertTrue(original.exists())
        self.assertEqual(original.with_suffix(".flac").read_bytes(), b"new destination occupant")

    def test_process_crash_leaves_recovery_journal_and_original(self):
        self.stage()
        original, track = self.original()
        replacement_preference(self.db, "hidden")
        with patch.object(keep_both, "staging_root", return_value=self.staging):
            plan = album_actions.preview(self.db, self.candidate, "replace", track, override_track_ids=[track])
        program = """
import os
import sys
from pathlib import Path
from unittest.mock import patch
from spliff import album_actions, keep_both, staged_downloads
db, staging, candidate, plan, original = sys.argv[1:]
with patch.object(keep_both, "staging_root", return_value=Path(staging)), \
     patch.object(staged_downloads, "staging_root", return_value=Path(staging)), \
     patch.object(staged_downloads, "request", side_effect=ValueError("Read staged files")), \
     patch.object(keep_both, "verify_published", side_effect=lambda *_: os._exit(41)):
    album_actions.apply(Path(db), candidate, "replace", plan, original_track_id=int(original), confirm=lambda _: True)
"""
        crashed = subprocess.run([sys.executable, "-c", program, str(self.db), str(self.staging), self.candidate,
                                  plan["plan_id"], str(track)], capture_output=True, text=True, timeout=30)
        self.assertEqual(crashed.returncode, 41, crashed.stderr)
        self.assertTrue(original.exists())
        self.assertTrue(self.job.exists())
        journal, = album_actions.inspect_recovery(self.db, self.candidate)["journals"]
        self.assertEqual(journal["files"][0]["installed"], str(original.with_suffix(".flac")))
        self.assertEqual(album_actions.status(self.db, self.candidate)["status"], "recovery_needed")

    def test_openai_extension_probe_uses_host_response(self):
        output = StringIO()

        def messages():
            yield json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {"extensions": {"openai/elicitation": {"form": {}}}},
            }})
            yield json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "spliff_confirmation_diagnostics", "arguments": {"probe": True},
            }})
            dialog = json.loads(output.getvalue().splitlines()[-1])
            self.assertEqual(dialog["method"], "openai/elicitation/create")
            yield json.dumps({"jsonrpc": "2.0", "id": dialog["id"], "result": {
                "action": "accept", "content": {"authorize": True},
            }})
        with patch("sys.stdin", messages()), redirect_stdout(output):
            mcp.serve(self.db, allow_downloads=True)
        response = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(json.loads(response["result"]["content"][0]["text"])["probe"], "accepted")
