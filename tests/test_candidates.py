import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from spliff import candidates, mcp, nicotine


class CandidateReceipts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'catalog.sqlite'
        self.id = 'album-' + 'a' * 32 + '-0'
        self.file = dict(peer='approved', path=r'Common\Be\01.flac', bytes=100,
                         format='flac', sample_rate=44100, bit_depth=16, free_slot=True, queue=0)

    def test_refresh_matches_exact_peer_and_files(self):
        candidates.record(self.db, 'Common Be', 'album', [{'candidate_id': self.id}], [self.file])
        with patch.object(nicotine, 'request', return_value={
                'files': [self.file], 'search_id': 'b' * 32, 'truncated': False}):
            result = candidates.refresh(self.db, self.id)
        self.assertEqual(result['status'], 'same_peer_and_files')
        self.assertEqual(result['candidates'][0]['candidate_id'], 'album-' + 'b' * 32 + '-0')

    def test_changed_file_cannot_inherit_prior_approval(self):
        candidates.record(self.db, 'Common Be', 'album', [{'candidate_id': self.id}], [self.file])
        for change in ({'peer': 'other'}, {'bytes': 101}, {'path': r'Common\Be\02.flac'}, {'bit_depth': 24}):
            with patch.object(nicotine, 'request', return_value={
                    'files': [{**self.file, **change}], 'search_id': 'b' * 32, 'truncated': False}):
                self.assertEqual(candidates.refresh(self.db, self.id)['status'], 'requires_selection')

    def test_missing_receipt_does_not_search_or_queue(self):
        with patch.object(nicotine, 'request') as request:
            result = candidates.refresh(self.db, self.id)
        request.assert_not_called()
        self.assertEqual(result['status'], 'requires_selection')

    def test_download_tools_cannot_apply_even_with_a_confirmed_flag(self):
        incoming = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
             'params': {'protocolVersion': '2025-11-25'}},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
             'params': {'name': 'confirm_album_action', 'arguments': {
                 'candidate_id': self.id, 'original_track_id': 1,
                 'mode': 'replace', 'token': 'token', 'confirmed': True}}},
        ]
        output = io.StringIO()
        with patch('sys.stdin',io.StringIO('\n'.join(map(json.dumps,incoming)))), redirect_stdout(output):
            mcp.serve(self.db, allow_downloads=True)
        tools = {t['name']:t for t in json.loads(output.getvalue().splitlines()[1])['result']['tools']}
        for name in ['stage_nicotine_download','nicotine_transfer_status','refresh_nicotine_candidate',
                     'analyze_download_spectrum','preview_album_action',
                     'sync_music_folder','music_sync_status','list_nicotine_transfers']:
            self.assertIn(name, tools)
        self.assertNotIn('confirm_album_action', tools)
        self.assertEqual(json.loads(output.getvalue().splitlines()[2])['error']['code'], -32602)
        self.assertFalse(tools['preview_album_action']['annotations']['readOnlyHint'])
