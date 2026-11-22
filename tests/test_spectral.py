import base64
import json
import shutil
import subprocess
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from spliff import mcp
from spliff.catalog import connect, initialize, signature
from spliff.scanner import scan
from spliff.spectral import analyze_file, analyze_track, _render


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Requires FFmpeg')
class SpectralAnalysis(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / 'Music'
        self.root.mkdir()
        self.audio = self.root / 'tone.flac'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                        'sine=frequency=12000:sample_rate=48000:duration=2',
                        '-c:a', 'flac', str(self.audio)], check=True)
        self.db = self.base / 'catalog.sqlite'
        scan(self.db, self.root)
        with closing(connect(self.db)) as connection:
            self.track = connection.execute('SELECT id FROM tracks').fetchone()['id']
        _render.cache_clear()

    def test_real_full_track_render_and_unchanged_source(self):
        before = self.audio.read_bytes()
        result = analyze_track(self.db, self.track)
        png = base64.b64decode(result['image_base64'])
        self.assertTrue(png.startswith(b'\x89PNG\r\n\x1a\n'))
        self.assertEqual(result['audio']['sample_rate'], 48000)
        self.assertAlmostEqual(result['audio']['duration'], 2)
        self.assertEqual(result['status'], 'visual review')
        self.assertEqual(self.audio.read_bytes(), before)
        analyze_track(self.db, self.track)
        self.assertEqual(_render.cache_info().hits, 1)

    def test_modified_original_and_symlink_rejected(self):
        expected = signature(self.audio.stat())
        self.audio.write_bytes(self.audio.read_bytes() + b'changed')
        with self.assertRaisesRegex(ValueError, 'changed'):
            analyze_file(self.audio, expected)
        with self.assertRaisesRegex(ValueError, 'changed'):
            analyze_track(self.db, self.track)
        link = self.root / 'link.flac'
        link.symlink_to(self.audio)
        with self.assertRaisesRegex(ValueError, 'symbolic'):
            analyze_file(link)

    def test_timeout_and_decode_error_never_return_cached_success(self):
        with patch('spliff.spectral.subprocess.run', side_effect=subprocess.TimeoutExpired('ffmpeg', 120)):
            with self.assertRaisesRegex(ValueError, 'timed out'):
                analyze_file(self.audio)
        self.assertEqual(_render.cache_info().currsize, 0)

    def test_mcp_returns_image_and_metadata(self):
        messages = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
             'params': {'protocolVersion': '2025-11-25'}},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {
                'name': 'analyze_library_spectrum', 'arguments': {'track_id': self.track}}},
        ]
        output = StringIO()
        with patch('sys.stdin', StringIO('\n'.join(map(json.dumps, messages)))), redirect_stdout(output):
            mcp.serve(self.db)
        response = json.loads(output.getvalue().splitlines()[1])['result']
        self.assertNotIn('isError', response)
        text, image = response['content']
        self.assertNotIn('image_base64', json.loads(text['text']))
        self.assertEqual(image['mimeType'], 'image/png')
        self.assertTrue(base64.b64decode(image['data']).startswith(b'\x89PNG'))

    def test_corrupt_audio_reports_failure(self):
        self.audio.write_bytes(b'not audio')
        with self.assertRaisesRegex(ValueError, 'decoded'):
            analyze_file(self.audio)
        self.assertEqual(_render.cache_info().currsize, 0)
