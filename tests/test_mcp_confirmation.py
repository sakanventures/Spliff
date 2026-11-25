import json
import shutil
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import test_library_import

from spliff import album_actions, mcp, mcp_confirmation
from spliff.scanner import scan


class ModernConfirmation(unittest.TestCase):
    def test_modern_stdio_openai_form_decline_accept_and_replay(self):
        output = StringIO()
        meta = {mcp_confirmation.PREFIX + "protocolVersion": mcp_confirmation.VERSION,
                mcp_confirmation.PREFIX + "clientCapabilities": {
                    "elicitation": {"form": {}}, "extensions": {"openai/elicitation": {"form": {}}}}}
        probe = {"name": "spliff_confirmation_diagnostics", "arguments": {"probe": True}}

        def request(identifier, method, params):
            return json.dumps({"jsonrpc": "2.0", "id": identifier, "method": method,
                               "params": {**params, "_meta": meta}})

        def last():
            return json.loads(output.getvalue().splitlines()[-1])["result"]

        def incoming():
            yield request(1, "server/discover", {})
            self.assertIn(mcp_confirmation.VERSION, last()["supportedVersions"])
            for initial_id, decision, probe_status in [(2, "decline", "declined_or_unsupported"),
                                                        (5, "accept", "accepted")]:
                yield request(initial_id, "tools/call", probe)
                prompt = last()
                self.assertEqual(prompt["resultType"], "input_required")
                form = prompt["inputRequests"]["approval"]["params"]
                self.assertEqual(form["requestedSchema"]["properties"], {})
                self.assertIn("authorize", form["_meta"]["openai/elicitation"]["requestedSchema"]["properties"])
                retry = {**probe, "requestState": prompt["requestState"], "inputResponses": {
                    "approval": {"action": decision, "content": {"authorize": True}}}}
                yield request(initial_id + 1, "tools/call", retry)
                self.assertEqual(json.loads(last()["content"][0]["text"])["probe"], probe_status)
                yield request(initial_id + 2, "tools/call", retry)
                self.assertTrue(last()["isError"])

        with patch("sys.stdin", incoming()), redirect_stdout(output):
            mcp.serve(None, allow_downloads=True)

    def test_core_form_continuation_requires_identical_plan_and_expires(self):
        confirmations = mcp_confirmation.Confirmations()
        arguments = {"name": "apply_album_action", "arguments": {"plan_id": "plan"}}
        review = {"mode": "replace", "files": [{"install": "/library/track.flac"}]}
        form = {"requestedSchema": {"type": "object"}}
        with patch.object(mcp_confirmation, "monotonic", return_value=100):
            with self.assertRaises(mcp_confirmation.InputRequired) as required:
                confirmations.confirm(1, arguments, "core", review, form)
            self.assertEqual(required.exception.result["inputRequests"]["approval"]["params"], form)
            retry = {**arguments, "requestState": required.exception.result["requestState"],
                     "inputResponses": {"approval": {"action": "accept", "content": {"authorize": True}}}}
            with self.assertRaisesRegex(ValueError, "does not match"):
                confirmations.confirm(2, retry, "core", {**review, "files": []}, form)
        with patch.object(mcp_confirmation, "monotonic", return_value=100 + mcp_confirmation.TTL):
            with self.assertRaisesRegex(ValueError, "expired"):
                confirmations.confirm(3, retry, "core", review, form)

    def test_modern_request_does_not_inherit_legacy_form(self):
        output = StringIO()
        calls = [
            ("initialize", {"protocolVersion": "2025-11-25", "capabilities": {"elicitation": {}}}),
            ("tools/call", {"name": "spliff_confirmation_diagnostics", "arguments": {"probe": True},
                            "_meta": {mcp_confirmation.PREFIX + "protocolVersion": mcp_confirmation.VERSION,
                                      mcp_confirmation.PREFIX + "clientCapabilities": {}}}),
        ]
        incoming = "\n".join(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params})
                             for i, (method, params) in enumerate(calls, 1))
        with patch("sys.stdin", StringIO(incoming)), redirect_stdout(output):
            mcp.serve(None, allow_downloads=True)
        result = json.loads(output.getvalue().splitlines()[-1])["result"]
        self.assertEqual(json.loads(result["content"][0]["text"])["probe"], "no_negotiated_form")


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "Requires macOS and FFmpeg")
class ModernAlbumApproval(unittest.TestCase):
    candidate = test_library_import.FirstTimeImport.candidate
    setUp = test_library_import.FirstTimeImport.setUp
    stage = test_library_import.FirstTimeImport.stage

    def test_album_action_waits_for_native_form_and_commits_once(self):
        source, = self.stage()
        expected = source.read_bytes()
        scan(self.db, self.root)
        plan = album_actions.preview(self.db, self.candidate, "keep")
        output = StringIO()
        params = {"name": "apply_album_action",
                  "arguments": {"candidate_id": self.candidate, "mode": "keep", "plan_id": plan["plan_id"]},
                  "_meta": {mcp_confirmation.PREFIX + "protocolVersion": mcp_confirmation.VERSION,
                            mcp_confirmation.PREFIX + "clientCapabilities": {"elicitation": {"form": {}}}}}

        def call(identifier, values):
            return json.dumps({"jsonrpc": "2.0", "id": identifier, "method": "tools/call", "params": values})

        def last():
            return json.loads(output.getvalue().splitlines()[-1])["result"]

        def incoming():
            # An approval value alone cannot start a transaction.
            answer = {"approval": {"action": "accept", "content": {"authorize": True}}}
            yield call(1, {**params, "inputResponses": answer})
            self.assertTrue(last()["isError"])
            yield call(2, params)
            prompt = last()
            self.assertEqual(prompt["resultType"], "input_required")
            self.assertIn(plan["files"][0]["install"], prompt["inputRequests"]["approval"]["params"]["message"])
            self.assertFalse(Path(plan["destination"]).exists())
            yield call(3, {**params, "requestState": prompt["requestState"], "inputResponses": answer})
            result = last()["structuredContent"]
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["transaction_id"], plan["plan_id"])
            yield call(4, params)
            self.assertEqual(last()["structuredContent"], result)

        with patch("sys.stdin", incoming()), redirect_stdout(output):
            mcp.serve(self.db, allow_downloads=True)
        self.assertEqual(Path(plan["files"][0]["install"]).read_bytes(), expected)
