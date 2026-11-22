import io
import json
import socket
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from threading import Thread
from unittest.mock import patch

from spliff import mcp, nicotine
from spliff.nicotine_bridge.wire import REQUEST_BYTES, RESPONSE_BYTES, VERSION, encode, read


class NicotineSearch(unittest.TestCase):
    def test_mcp_album_search_over_socket(self):
        files = [
            {"peer": "peer", "path": r"Artist\Album\01.flac",
             "format": "flac", "free_slot": True, "queue": 0},
            {"peer": "peer", "path": r"Artist\Album\02.flac",
             "format": "flac", "free_slot": True, "queue": 0},
            {"peer": "peer", "path": r"Artist\Album\02.wav",
             "format": "wav", "free_slot": True, "queue": 0},
            {"peer": "other", "path": r"Album\01.wav",
             "format": "wav", "free_slot": False, "queue": 4},
        ]
        messages = [
            {"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
            {"id": 2, "method": "tools/list"},
            {"id": 3, "method": "tools/call", "params": {"name": "search_nicotine",
                                                       "arguments": {"query": "Artist Album", "kind": "album"}}},
        ]
        incoming = io.StringIO("".join(json.dumps(dict(msg, jsonrpc="2.0")) + "\n" for msg in messages))
        output = io.StringIO()
        requests = []
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            endpoint = str(Path(directory) / "bridge.sock")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(endpoint)
                listener.listen()
                listener.settimeout(3)

                def bridge():
                    connection, _ = listener.accept()
                    with connection, connection.makefile("rb") as stream:
                        requests.append(read(stream, REQUEST_BYTES))
                        connection.sendall(encode({"version": VERSION, "files": files, "truncated": False},
                                                  RESPONSE_BYTES))

                worker = Thread(target=bridge, daemon=True)
                worker.start()
                with patch.object(nicotine, "endpoint", return_value=endpoint), patch("sys.stdin", incoming):
                    with redirect_stdout(output):
                        mcp.serve(None, search_only=True)
                worker.join(timeout=3)
        self.assertEqual(requests, [{"version": VERSION, "operation": "search", "query": "Artist Album"}])
        _, listing, call = map(json.loads, output.getvalue().splitlines())
        tools = listing["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], ["search_nicotine"])
        self.assertTrue(tools[0]["annotations"]["openWorldHint"])
        result = json.loads(call["result"]["content"][0]["text"])
        self.assertEqual(result["candidates"][0]["matching_files"], 2)
        self.assertEqual(result["candidates"][1]["formats"], ["wav"])

    def test_missing_bridge_and_input_validation(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            with patch.object(nicotine, "endpoint", return_value=Path(directory) / "absent.sock"):
                with self.assertRaisesRegex(ValueError, "enable the Spliff Search plugin"):
                    nicotine.search(None, "Artist Album")
                with self.assertRaisesRegex(ValueError, "printable"):
                    nicotine.search(None, "x")

    def test_plugin_installer_preserves_existing_install(self):
        with tempfile.TemporaryDirectory() as directory:
            target = nicotine.install_plugin(Path(directory))
            self.assertEqual({item.name for item in target.iterdir()},
                             {"__init__.py", "plugin.py", "downloads.py", "wire.py", "PLUGININFO"})
            with self.assertRaisesRegex(ValueError, "already exists"):
                nicotine.install_plugin(Path(directory))
            self.assertEqual(nicotine.install_plugin(Path(directory), upgrade=True), target)
