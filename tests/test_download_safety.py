import json
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from threading import Event
from time import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

from spliff.nicotine_bridge.downloads import Downloads, ENABLE_FILE, private_folder
from spliff.nicotine_bridge.wire import fingerprint


class DownloadSafety(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve() / "staging"
        scope = patch("spliff.nicotine_bridge.downloads.staging_root", return_value=self.root)
        scope.start()
        self.addCleanup(scope.stop)
        private_folder(self.root)
        (self.root / ENABLE_FILE).touch(mode=0o600)
        self.transfers = {}
        self.calls = []
        self.fail_at = None
        engine = SimpleNamespace(
            transfers=self.transfers, enqueue_download=self.enqueue, abort_downloads=self.pause,
            get_incomplete_download_folder=lambda: str(self.root.parent / "incomplete"),
            get_complete_download_file_path=lambda _peer, _remote, _size, dest: (dest + "/file", False),
        )
        config = SimpleNamespace(sections={"transfers": {"afterfinish": "", "afterfolder": ""}})
        self.bridge = Downloads(SimpleNamespace(downloads=engine), config)

    def enqueue(self, peer, remote, folder_path, size):
        self.calls.append((peer, remote))
        self.transfers[peer + remote] = SimpleNamespace(
            username=peer, virtual_path=remote, folder_path=folder_path, size=size, status="Queued",
        )
        if self.fail_at == len(self.calls):
            raise OSError("lost acknowledgement after append")

    def pause(self, transfers):
        for transfer in transfers:
            transfer.status = "Paused"

    @staticmethod
    def offer(peer, folder, count, size=1024):
        return [{"peer": peer, "path": f"{folder}\\{index}.flac", "bytes": size, "format": "flac"}
                for index in range(count)]

    def submit(self, search_id, files, selected, original_id=None):
        return self.bridge.queue(f"album-{search_id}-0", {search_id: (time(), files, "Release")},
                                 [], fingerprint(selected), "mcp", original_id)

    def test_two_manual_selections_enqueue_only_39_exact_files(self):
        first = self.offer("one", "Release One", 18)
        second = self.offer("two", "Release Two", 21)
        distractors = self.offer("other", "Release One", 40) + self.offer("one", "Another Release", 50)
        for search_id, selected, original in (("a" * 32, first, 2975), ("b" * 32, second, 2976)):
            with self.assertRaisesRegex(ValueError, "allowlist"):
                self.submit(search_id, selected + distractors, selected[:-1], original)
            self.submit(search_id, selected + distractors, selected, original)
        self.assertCountEqual(self.calls, [(item["peer"], item["path"]) for item in first + second])
        audit = [json.loads(line) for line in (self.root / "transfer-audit.jsonl").read_text().splitlines()]
        self.assertEqual([row["event"] for row in audit],
                         ["queue_intent", "queue_submitted", "queue_intent", "queue_submitted"])
        self.assertEqual([row["original_id"] for row in audit if "original_id" in row], [2975, 2976])
        extra = self.offer("three", "Release Three", 22)
        with self.assertRaisesRegex(ValueError, "outstanding"):
            self.submit("c" * 32, extra, extra)
        self.assertEqual(len(self.calls), 39)

    def test_oversized_files_and_aggregate_size_fail_before_enqueue(self):
        huge = self.offer("one", "Large", 1, 275 * 1024**3)
        with self.assertRaisesRegex(ValueError, "allowlist"):
            self.submit("a" * 32, huge, huge)
        with patch("spliff.nicotine_bridge.downloads.MAX_BYTES", 4096):
            first = self.offer("one", "Release One", 3)
            second = self.offer("two", "Release Two", 2)
            self.submit("a" * 32, first, first)
            with self.assertRaisesRegex(ValueError, "outstanding"):
                self.submit("b" * 32, second, second)
        self.assertEqual(len(self.calls), 3)

    def test_partial_failure_pauses_attempts_and_does_not_replay(self):
        files = self.offer("one", "Release One", 3)
        self.fail_at = 2
        with self.assertRaisesRegex(OSError, "acknowledgement"):
            self.submit("a" * 32, files, files)
        self.assertEqual([transfer.status for transfer in self.transfers.values()], ["Paused", "Paused"])
        self.assertFalse(self.bridge.allowed)
        self.submit("a" * 32, files, files)
        self.assertEqual(len(self.calls), 2)

    def test_emergency_stop_pauses_only_matching_jobs_and_closes_gate(self):
        files = self.offer("one", "Release One", 2)
        self.submit("a" * 32, files, files)
        staged, cleaned = self.transfers.values()
        cleaned.status = "Finished"
        unrelated = SimpleNamespace(username="other", virtual_path=staged.virtual_path,
                                    folder_path=staged.folder_path, size=1024, status="Queued")
        self.transfers["other" + staged.virtual_path] = unrelated
        self.assertTrue(self.bridge.permit_transfer(staged, 1024))
        self.assertFalse(self.bridge.permit_transfer(staged, 1025))
        self.assertFalse(Downloads(self.bridge.core, self.bridge.config).permit_transfer(staged, 1024))
        self.assertEqual(self.bridge.stop(), {"enabled": False, "paused": 1})
        self.assertEqual((staged.status, cleaned.status, unrelated.status), ("Paused", "Finished", "Queued"))
        self.assertFalse(self.bridge.permit_transfer(staged, 1024))
        with self.assertRaisesRegex(ValueError, "disabled"):
            self.submit("b" * 32, files, files)

    def test_bridge_shutdown_pauses_without_revoking_saved_permission(self):
        files = self.offer("one", "Release One", 1)
        self.submit("a" * 32, files, files)
        transfer, = self.transfers.values()
        transfer_count = len(self.calls)
        upstream = {
            "pynicotine": SimpleNamespace(__version__="3.3.11"),
            "pynicotine.events": SimpleNamespace(events=Mock()),
            "pynicotine.pluginsystem": SimpleNamespace(BasePlugin=object),
            "pynicotine.slskmessages": SimpleNamespace(FileAttribute=None, TransferResponse=None, UserStatus=None),
        }
        with patch.dict(sys.modules, upstream):
            plugin_type = runpy.run_module("spliff.nicotine_bridge.plugin")["Plugin"]
        plugin = plugin_type()
        plugin.downloads = self.bridge
        plugin.bridge, plugin.lock, plugin.path = Mock(), Mock(), Mock()
        plugin.restore_transfer_guard = Mock()
        plugin.stopped, plugin.active = Event(), None

        plugin.disable()

        self.assertTrue((self.root / ENABLE_FILE).is_file())
        self.assertEqual(transfer.status, "Paused")
        self.assertFalse(self.bridge.permit_transfer(transfer, transfer.size))

        restarted = Downloads(self.bridge.core, self.bridge.config)
        self.assertFalse(restarted.permit_transfer(transfer, transfer.size))
        self.assertEqual(len(self.calls), transfer_count)
        files = self.offer("two", "Release Two", 1)
        self.bridge = restarted
        self.submit("b" * 32, files, files)
        self.assertEqual(len(self.calls), transfer_count + 1)

    def test_recovery_authorizes_exact_paused_files_without_queueing(self):
        first = self.offer("one", "Release One", 18)
        second = self.offer("one", "Release Two", 21)
        unrelated = self.offer("one", "Unrelated", 2)
        for letter, files in (("a", first), ("b", second), ("c", unrelated)):
            self.submit(letter * 32, files, files)
        for transfer in list(self.transfers.values())[:6]:
            transfer.status = "Finished"
        self.bridge.stop()
        selections = [f"album-{letter * 32}-0" for letter in "ab"]
        self.assertEqual(self.bridge.recover(selections)["authorized_paused_files"], 33)
        self.assertEqual(len(self.calls), 41)
        self.assertFalse((self.root / ENABLE_FILE).exists())
        self.assertEqual(sum(self.bridge.permit_transfer(t, t.size) for t in self.transfers.values()), 33)
        transfer = self.transfers[second[0]["peer"] + second[0]["path"]]
        self.assertEqual(transfer.status, "Paused")
        self.assertFalse(self.bridge.permit_transfer(transfer, transfer.size + 1))
        self.bridge.stop()
        self.assertFalse(self.bridge.permit_transfer(transfer, transfer.size))
