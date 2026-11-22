import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from spliff.catalog import initialize
from spliff.staged_downloads import queue


class StagingConfirmation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'catalog.sqlite'
        initialize(self.db)
        self.selection = 'album-' + 'a' * 32 + '-0'

    def test_uncertain_queue_is_reconciled_without_duplicate_queue(self):
        status = {'id': self.selection, 'items': [], 'complete': 0}
        with patch('spliff.staged_downloads.request', side_effect=[
                ValueError('Nicotine+ did not confirm the action. Check the staged download before retrying.'),
                status]) as request:
            result = queue(self.db, self.selection)
        self.assertEqual(result['id'], self.selection)
        self.assertEqual([call.args[0] for call in request.call_args_list], ['queue', 'download_status'])

    def test_failed_reconciliation_reports_uncertainty_not_a_success(self):
        with patch('spliff.staged_downloads.request', side_effect=[
                ValueError('Nicotine+ did not confirm the action.'), ValueError('Unknown staged download.')]) as request:
            result = queue(self.db, self.selection)
        self.assertEqual(result['state'], 'confirmation_pending')
        self.assertTrue(result['requires_status_check'])
        self.assertEqual([call.args[0] for call in request.call_args_list], ['queue', 'download_status'])
