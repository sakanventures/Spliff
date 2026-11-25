import io
import json
import subprocess
import sys
import tempfile
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from queue import Queue
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from spliff import mcp, nicotine, web
from spliff.nicotine_bridge.downloads import ENABLE_FILE, enabled


class TransferSettings(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name).resolve()
        self.root = self.home / "Library/Application Support/Spliff/Staged downloads"
        self.enter_context_for("spliff.nicotine.staging_root", return_value=self.root)
        self.bridge = self.enter_context_for("spliff.nicotine.request", side_effect=self.bridge_request)
        self.connected = True
        self.upgraded = True
        self.post_commands = False

    def enter_context_for(self, target, **options):
        scope = patch(target, **options)
        self.addCleanup(scope.stop)
        return scope.start()

    def bridge_request(self, operation, **_):
        if operation == "status":
            return {"nicotine_version": "3.3.11", "soulseek_connected": self.connected,
                    "persistent_permission": self.upgraded, "staging": enabled(self.root),
                    "post_download_commands": self.post_commands}
        if operation == "pause_downloads":
            return {"enabled": enabled(self.root), "paused": 2}
        self.fail(f"Unexpected bridge operation {operation}")

    def test_readiness_tracks_live_states_without_writing(self):
        self.assertEqual(nicotine.transfer_readiness()["code"], "permission_off")
        self.assertFalse(self.root.exists())
        self.connected = False
        self.assertEqual(nicotine.transfer_readiness()["code"], "soulseek_offline")
        self.connected = True
        self.post_commands = True
        self.assertEqual(nicotine.transfer_readiness()["code"], "post_download_commands")
        self.upgraded = False
        self.assertEqual(nicotine.transfer_readiness()["code"], "bridge_update_required")
        with self.assertRaises(nicotine.TransferError):
            nicotine.transfer_control("enable")
        self.assertFalse(enabled(self.root))

    def test_invalid_marker_fails_closed(self):
        nicotine.transfer_control("enable")
        marker = self.root / ENABLE_FILE
        marker.chmod(0o644)
        self.assertEqual(nicotine.transfer_readiness()["code"], "permission_unavailable")
        self.assertFalse(enabled(self.root))

    def test_revocation_persists_when_bridge_unreachable(self):
        nicotine.transfer_control("enable")
        self.bridge.side_effect = nicotine.TransferError("bridge_unavailable", nicotine.BRIDGE_UNAVAILABLE)
        result = nicotine.transfer_control("stop")
        self.assertFalse(result["enabled"])
        self.assertIsNone(result["paused"])
        self.bridge.side_effect = self.bridge_request
        self.assertEqual(nicotine.transfer_readiness()["code"], "permission_off")

    def test_dashboard_settings_and_new_process_share_permission(self):
        def read_in_new_process():
            script = ("from spliff.nicotine_bridge.downloads import enabled; "
                      "from spliff.nicotine_bridge.wire import staging_root; print(enabled(staging_root()))")
            return subprocess.check_output(
                [sys.executable, "-c", script], env={"HOME": str(self.home)}, text=True, timeout=5
            ).strip() == "True"

        server_ready = Queue()

        def bind(address, handler):
            server = ThreadingHTTPServer(address, handler)
            server_ready.put(server)
            return server

        with patch.object(web, "ThreadingHTTPServer", side_effect=bind):
            thread = Thread(target=web.serve, args=(self.home / "test.sqlite", 0), daemon=True)
            thread.start()
            server = server_ready.get(timeout=5)
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        origin = f"http://127.0.0.1:{server.server_port}"

        def get():
            with urlopen(origin + "/api/transfer-readiness", timeout=5) as response:
                return json.load(response)

        def post(action, headers):
            request = Request(origin + "/api/transfer-permission?action=" + action, data=b"", headers=headers)
            with urlopen(request, timeout=5) as response:
                return json.load(response)

        headers = {"Origin": origin, "X-Spliff-Action": "dashboard"}
        with self.assertRaises(HTTPError) as error:
            post("enable", {**headers, "Origin": "https://example.invalid"})
        self.assertEqual(error.exception.code, 403)
        error.exception.close()
        self.assertFalse(read_in_new_process())
        self.assertEqual(get()["code"], "permission_off")
        self.assertTrue(post("enable", headers)["enabled"])
        self.assertTrue(read_in_new_process())
        self.assertTrue(get()["ready"])
        self.assertEqual(post("stop", headers), {"enabled": False, "paused": 2})
        self.assertFalse(read_in_new_process())
        self.assertEqual(get()["code"], "permission_off")

    def test_mcp_readiness_and_staging_error_include_local_recovery(self):
        calls = [
            {"method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"method": "tools/list"},
            {"method": "tools/call", "params": {"name": "spliff_transfer_readiness"}},
            {"method": "tools/call", "params": {"name": "stage_nicotine_download",
                                               "arguments": {"candidate_id": "album-" + "a" * 32 + "-0"}}},
        ]
        stream = io.StringIO("\n".join(json.dumps({"jsonrpc": "2.0", "id": i, **call})
                                       for i, call in enumerate(calls)))
        output = io.StringIO()
        with patch("sys.stdin", stream), patch("sys.stdout", output), patch.object(
                mcp, "queue", side_effect=nicotine.TransferError("permission_off", nicotine.DISABLED_MESSAGE)):
            mcp.serve(self.home / "test.sqlite", allow_downloads=True, dashboard_port=9876)
        _, listing, readiness, stage = [json.loads(line)["result"] for line in output.getvalue().splitlines()]
        tool = next(tool for tool in listing["tools"] if tool["name"] == "spliff_transfer_readiness")
        self.assertTrue(tool["annotations"]["readOnlyHint"])
        self.assertTrue(stage["isError"])
        for result in (readiness, stage):
            payload = json.loads(result["content"][0]["text"])
            self.assertEqual(payload["code"], "permission_off")
            self.assertEqual(payload["settings_url"], "http://127.0.0.1:9876/?settings=1")
