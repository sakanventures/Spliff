"""Artist-name routing must inventory owned recordings before any remote search."""
import io
import json
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from spliff import artist, mcp
from spliff.catalog import connect, initialize


class ArtistWorkflow(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name) / 'catalog.sqlite'
        initialize(self.db)
        with closing(connect(self.db)) as connection, connection:
            connection.execute('INSERT INTO roots VALUES (?,1,1)', ('/Music/Rap',))

    def add(self, path, title='Test', album='Be', name='Common', codec='mp3', quality='lossy', protected=0):
        with closing(connect(self.db)) as connection, connection:
            return connection.execute('''INSERT INTO tracks(root,path,title,album,artist,codec,quality,bitrate,
                protected,present) VALUES (?,?,?,?,?,?,?,192000,?,1)''',
                ('/Music/Rap', path, title, album, name, codec, quality, protected)).lastrowid

    def offers(self, *_args):
        return {'candidates': [{'candidate_id': 'album-test-0', 'formats': ['flac'],
                               'folder': r'Music\Common\Be'}], 'observed_matches': 1}

    def test_common_routes_to_owned_albums_not_artist_discography(self):
        self.add('Common/Be/01.mp3')
        self.add('Common/Be/02.mp3')
        self.add('Common Market/Be/01.mp3', name='Common Market')
        with patch.object(artist, 'search', side_effect=self.offers) as search:
            result = artist.search_for_chat(self.db, 'Common')
        self.assertEqual(result['track_count'], 2)
        self.assertEqual(result['release_count'], 1)
        self.assertEqual(result['items'][0]['codecs'], {'mp3': 2})
        search.assert_called_once_with(self.db, 'Common Be', 'album', None)
        self.assertEqual(result['items'][0]['search']['candidates'][0]['candidate_id'], 'album-test-0')

    def test_disc_grouping_loose_tracks_and_protected_or_lossless_skips(self):
        self.add('Common/Albums/Be/Disc 1/01.mp3')
        self.add('Common/Albums/Be/Disc 2/01.mp3')
        self.add('Common/Rare Remix.mp3', title='Rare Remix', album=None)
        self.add('Common/Albums/Finding Forever/01.flac', album='Finding Forever', codec='flac', quality='lossless')
        self.add('Common/Rare Protected.mp3', title='Protected', album=None, protected=1)
        inventory = artist.library(self.db, 'common')
        self.assertEqual(inventory['release_count'], 4)
        self.assertEqual(inventory['items'][0]['track_count'], 2)
        self.assertEqual(inventory['items'][2]['kind'], 'track')
        with patch.object(artist, 'search', side_effect=self.offers) as search:
            result = artist.upgrades(self.db, 'Common', limit=3)
        self.assertEqual(search.call_count, 2)
        self.assertEqual(result['items'][1]['search_status'], 'skipped')
        self.assertIsNotNone(result['next'])
        last = artist.upgrades(self.db, 'Common', after=result['next'])
        self.assertEqual(last['items'][0]['status'], 'protected')
        self.assertIsNone(last['next'])

    def test_unindexed_artist_never_silently_becomes_remote_discography(self):
        self.add('Common Market/Be/01.mp3', name='Common Market')
        with patch.object(artist, 'search') as search:
            result = artist.search_for_chat(self.db, 'Common')
        search.assert_not_called()
        self.assertEqual(result['status'], 'not_indexed')
        self.assertEqual(result['items'], [])

    def test_no_match_and_bridge_failure_are_distinct(self):
        self.add('Common/Be/01.mp3')
        with patch.object(artist, 'search', return_value={'candidates': []}):
            self.assertEqual(artist.upgrades(self.db, 'Common')['items'][0]['search_status'], 'no_candidate_found')
        with patch.object(artist, 'search', side_effect=ValueError('Disconnected')):
            self.assertEqual(artist.upgrades(self.db, 'Common')['items'][0]['search_status'], 'search_failed')

    def test_existing_local_lossless_release_is_reviewed_before_remote_search(self):
        self.add('Common/MP3/Be/01.mp3')
        self.add('Common/FLAC/Be/01.flac', codec='flac', quality='lossless')
        with patch.object(artist, 'search') as search:
            result = artist.upgrades(self.db, 'Common')
        search.assert_not_called()
        self.assertEqual(result['items'][0]['status'], 'local_lossless_version_to_review')
        self.assertEqual(len(result['items'][0]['local_lossless_matches']), 1)

    def test_root_at_artist_folder_can_match_untagged_audio(self):
        with closing(connect(self.db)) as connection, connection:
            connection.execute('INSERT INTO roots VALUES (?,1,2)', ('/External/Rap/Common',))
            connection.execute('''INSERT INTO tracks(root,path,title,quality,codec,present)
                VALUES ('/External/Rap/Common','Rare.mp3','Rare','lossy','mp3',1)''')
        result = artist.library(self.db, 'Common')
        self.assertEqual(result['track_count'], 1)
        self.assertEqual(result['items'][0]['kind'], 'track')

    def test_candidate_names_exclude_other_artists_and_projects(self):
        self.assertTrue(artist.candidate_matches({'folder': r'Music\Common\Be (2005)'}, 'Common', 'Be'))
        self.assertTrue(artist.candidate_matches({'folder': r'Music\Common - Be (2005)'}, 'Common', 'Be'))
        self.assertFalse(artist.candidate_matches({'folder': r'Music\Common\The Dreamer The Believer'}, 'Common', 'Be'))
        self.assertFalse(artist.candidate_matches({'folder': r'Music\A Day to Remember\Common Courtesy'}, 'Common', 'Be'))
        self.assertFalse(artist.candidate_matches({'folder': r'Music\Common Market\Be'}, 'Common', 'Be'))

    def test_artist_level_album_tags_are_loose_tracks_not_fake_albums(self):
        self.add('Common/Announcement.mp3', title='Announcement', album='www.example.com')
        self.add('Common/The Food (Studio).mp3', title='The Food (Studio)', album='Be')
        result = artist.library(self.db, 'Common')
        self.assertEqual([item['kind'] for item in result['items']], ['track', 'track'])
        self.assertEqual(result['items'][0]['title'], 'Announcement')
        self.assertEqual(result['items'][1]['title'], 'The Food (Studio)')

    def test_search_only_does_not_expose_local_catalog(self):
        messages = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                     'params': {'protocolVersion': '2025-11-25'}},
                    {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}]
        output = io.StringIO()
        with patch('sys.stdin', io.StringIO('\n'.join(map(json.dumps, messages)))), redirect_stdout(output):
            mcp.serve(None, search_only=True)
        initialized, listing = map(json.loads, output.getvalue().splitlines())
        self.assertIn('search-only', initialized['result']['instructions'])
        self.assertEqual([tool['name'] for tool in listing['result']['tools']], ['search_nicotine'])

    def test_mcp_artist_request_returns_inventory_then_search_candidates(self):
        self.add('Common/Be/01.mp3')
        messages = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                     'params': {'protocolVersion': '2025-11-25'}},
                    {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {
                        'name': 'search_nicotine', 'arguments': {'query': 'Common'}}}]
        output = io.StringIO()
        with patch('sys.stdin', io.StringIO('\n'.join(map(json.dumps, messages)))), \
                patch.object(artist, 'search', side_effect=self.offers), redirect_stdout(output):
            mcp.serve(self.db)
        response = json.loads(output.getvalue().splitlines()[1])['result']
        result = json.loads(response['content'][0]['text'])
        self.assertEqual(result['items'][0]['title'], 'Be')
        self.assertEqual(result['items'][0]['search_status'], 'candidates_for_review')
