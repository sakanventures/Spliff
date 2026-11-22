"""Library-first artist inventories and bounded, search-only upgrade batches."""
import re
import unicodedata
from collections import Counter
from contextlib import closing
from pathlib import Path, PureWindowsPath

from .catalog import connect, contains_pattern
from .nicotine import search
from .scanner import scan


def artist_key(value):
    return unicodedata.normalize('NFKC', value or '').strip().casefold()


def validate_artist(artist):
    if not isinstance(artist, str) or not 2 <= len(artist.strip()) <= 120 or not artist.isprintable():
        raise ValueError('Choose an artist name of 2–120 printable characters.')
    return artist.strip()


def release_folder(path):
    folder = Path(path).parent
    if re.fullmatch(r'(?:disc|disk|cd)[ ._-]*\d+', folder.name, re.I):
        folder = folder.parent
    return str(folder)


def words(value):
    return ' '.join(re.findall(r'\w+', unicodedata.normalize('NFKC', value).casefold()))


def candidate_matches(candidate, artist, title):
    # Conservative name matching only. Never allow "Be" to match "Believer",
    # or "Common" to match an unrelated "Common Courtesy" album.
    path = PureWindowsPath(candidate.get('folder') or candidate.get('path') or '')
    parts = [words(part) for part in path.parts]
    artist_name, release = words(artist), words(title)
    if not artist_name or not release:
        return False
    credit = artist_name + ' ' + release
    return (any(part == artist_name or part == credit or part.startswith(credit + ' ') for part in parts)
            and any(' ' + release + ' ' in ' ' + part + ' ' for part in parts))


def library(db, artist=None, after=0, limit=50):
    artist = validate_artist(artist)
    if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('Choose a valid artist inventory page.')
    if db is None:
        return {'artist': artist, 'status': 'catalog_unavailable', 'items': [], 'next': None,
                'action': 'Connect the catalog-enabled Spliff MCP server. Search-only mode cannot inspect owned music.'}
    key = artist_key(artist)
    pattern = contains_pattern(artist)
    with closing(connect(db)) as connection:
        roots = [Path(row['path']) for row in connection.execute('SELECT path FROM roots')]
        # Exact Python matching below prevents Common from matching Common Market,
        # titles containing "common", or compilation guest credits by substring.
        rows = connection.execute('''SELECT * FROM tracks WHERE present=1
            AND (artist LIKE ? ESCAPE '!' OR path LIKE ? ESCAPE '!'
                 OR root LIKE ? ESCAPE '!') ORDER BY id''',
            (pattern, pattern, pattern)).fetchall()
    groups = {}
    folders = set()
    for row in rows:
        path = Path(row['root']) / row['path']
        components = list(Path(row['path']).parts[:-1])
        root = Path(row['root'])
        # A scan may be rooted at the artist itself or a descendant album folder.
        components.extend(root.parts)
        folder_match = any(artist_key(part) == key for part in components)
        if artist_key(row['artist']) != key and not folder_match:
            continue
        if folder_match:
            for parent in path.parents:
                if artist_key(parent.name) == key:
                    folders.add(str(parent))
                    break
        title = (row['album'] or '').strip()
        group_folder = release_folder(row['path'])
        artist_level = artist_key((Path(row['root']) / group_folder).name) == key
        if artist_level or re.fullmatch(r'(?:https?://)?(?:www\.)?[^\s/]+\.(?:com|net|org|in)(?:/.*)?', title, re.I):
            title = ''
        # Untagged loose tracks remain separate; never invent an album from an artist folder.
        group_key = (row['root'], group_folder, artist_key(title), None if title else row['id'])
        groups.setdefault(group_key, []).append({**dict(row), '_release_title': title})
    releases = []
    for (_, folder, _, _), tracks in groups.items():
        title = tracks[0]['_release_title']
        loose = not title
        lossy = [track for track in tracks if track['quality'] == 'lossy' and not track['error']]
        eligible = [track for track in lossy if not track['protected']]
        unknown = sum(bool(track['error']) or track['quality'] not in ('lossless', 'lossy') for track in tracks)
        status = ('needs_inspection' if unknown else 'upgrade_available_to_search' if eligible
                  else 'protected' if lossy else 'already_lossless')
        release_title = title or tracks[0]['title'] or Path(tracks[0]['path']).stem
        releases.append({'id': min(track['id'] for track in tracks), 'title': release_title,
            'kind': 'track' if loose else 'album', 'folder': folder,
            'track_count': len(tracks), 'lossy_tracks': len(lossy),
            'lossless_tracks': sum(track['quality'] == 'lossless' and not track['error'] for track in tracks),
            'protected_tracks': sum(bool(track['protected']) for track in tracks),
            'codecs': dict(Counter(track['codec'] or 'unknown' for track in tracks)),
            'bitrates_kbps': sorted({round(track['bitrate'] / 1000) for track in lossy if track['bitrate']}),
            'status': status,
            'query': f'{artist} {release_title}',
            'track_ids': [track['id'] for track in tracks],
            'edition': 'Local tags and folder grouping; edition and recording matches require review.'})
    for release in releases:
        if release['status'] != 'upgrade_available_to_search':
            continue
        existing = [other for other in releases if other['status'] == 'already_lossless'
                    and other['kind'] == release['kind']
                    and artist_key(other['title']) == artist_key(release['title'])
                    and other['track_count'] >= release['track_count']]
        if existing:
            release['status'] = 'local_lossless_version_to_review'
            release['local_lossless_matches'] = [{key: other[key] for key in ('id', 'folder', 'track_count')}
                                                for other in existing]
    releases.sort(key=lambda item: (item['kind'] != 'album', item['id']))
    selected = releases[after:]
    items = selected[:limit]
    return {'artist': artist, 'status': 'indexed' if releases else 'not_indexed',
        'artist_folders': sorted(folders), 'indexed_roots': [str(root) for root in roots],
        'release_count': len(releases), 'track_count': sum(item['track_count'] for item in releases),
        'items': items, 'next': after + len(items) if len(selected) > limit else None,
        'action': ('Review owned releases, then ask which candidates to download.' if releases else
                   'Choose the artist folder and index it first. Do not substitute a Soulseek discography for owned music.'),
        'freshness': 'Indexed observations only. Rescan the chosen artist folder to include new or changed files.'}


def upgrades(db, artist=None, after=0, limit=3, candidates=5):
    # Three eight-second searches keep each chat call bounded and avoid evicting
    # the bridge's eight remembered searches before the user can select an offer.
    if type(limit) is not int or not 1 <= limit <= 3:
        raise ValueError('Search 1–3 owned releases per batch.')
    if type(candidates) is not int or not 1 <= candidates <= 10:
        raise ValueError('Choose 1–10 candidates per release.')
    result = library(db, artist, after, limit)
    for item in result['items']:
        if item['status'] != 'upgrade_available_to_search':
            item['search_status'] = 'skipped'
            continue
        if len(item['query']) > 180:
            item['search_status'] = 'query_needs_review'
            continue
        try:
            offers = search(db, item['query'], item['kind'], None)
        except (ValueError, OSError) as exc:
            item['search_status'] = 'search_failed'
            item['error'] = str(exc)
            continue
        matching = [offer for offer in offers['candidates']
                    if candidate_matches(offer, result['artist'], item['title'])]
        item['search'] = {**offers, 'candidates': matching[:candidates],
            'observed_release_matches': len(matching),
            'excluded_by_name': len(offers['candidates']) - len(matching),
            'truncated': offers.get('truncated', False) or len(matching) > candidates,
            'match_basis': 'Artist and release words in peer paths only; recording identity and edition are unverified.'}
        item['search_status'] = 'candidates_for_review' if matching else 'no_candidate_found'
    result['workflow'] = {
        'next': 'Show owned releases, local formats, and candidate IDs. Ask for explicit selections before staging downloads.',
        'pagination': 'Continue using next only when the user asks to review more. Candidate IDs can expire; refresh the chosen release if needed.',
        'verification': 'Peer claims are untrusted. Do not claim the edition, completeness, or fidelity is verified.',
        'after_download': 'Match recordings and review spectra, then ask Keep both or Replace and confirm exact destinations.',
        'fallback': 'This version searches lossless formats. Higher-bitrate lossy fallback is not implemented; leave originals untouched.'}
    return result


def search_for_chat(db, query=None, kind='auto', limit=5):
    """Route exact owned artist names through inventory before any remote search."""
    if kind not in ('auto', 'artist', 'track', 'album'):
        raise ValueError('Choose an artist, track, or album search.')
    if kind == 'artist':
        return upgrades(db, query, candidates=limit)
    if db is not None:
        inventory = library(db, query, limit=1) if isinstance(query, str) and 2 <= len(query.strip()) <= 120 else None
        if inventory and inventory['release_count']:
            return upgrades(db, query, candidates=limit)
        if kind == 'auto':
            return inventory or library(db, query)
    elif kind == 'auto':
        return library(db, query)
    return search(db, query, kind, limit)


def index_folder(db, folder=None):
    if db is None:
        raise ValueError('Use the catalog-enabled MCP connection to index a selected folder.')
    if not isinstance(folder, str) or not Path(folder).is_absolute():
        raise ValueError('Choose an absolute artist or music folder path on this Mac.')
    return {'folder': folder, 'scan': scan(Path(db), Path(folder).expanduser())}
