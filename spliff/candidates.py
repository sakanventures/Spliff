"""Persist peer/file observations so an expired selection can be refreshed conservatively."""
import sqlite3
from contextlib import closing
from pathlib import Path

from .nicotine_bridge.wire import album_tracks, fingerprint


def cache_path(db):
    return Path(db).with_name('candidate-receipts.sqlite')


def saved_fingerprint(db, candidate):
    path = cache_path(db)
    if path.is_file():
        with closing(sqlite3.connect(path)) as connection:
            row = connection.execute("SELECT fingerprint FROM offers WHERE id=?", (candidate,)).fetchone()
            if row:
                return row[0]
    return None


def record(db, query, kind, candidates, files):
    if db is None:
        return
    with closing(sqlite3.connect(cache_path(db))) as connection, connection:
        connection.execute('''CREATE TABLE IF NOT EXISTS offers(
            id TEXT PRIMARY KEY, query TEXT, kind TEXT, fingerprint TEXT,
            created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
        for candidate in candidates:
            selection = candidate.get('candidate_id')
            if not selection:
                continue
            index = int(selection.rsplit('-', 1)[1])
            selected = album_tracks(files, files[index]) if kind == 'album' else [files[index]]
            connection.execute('INSERT OR REPLACE INTO offers(id,query,kind,fingerprint) VALUES (?,?,?,?)',
                               (selection, query, kind, fingerprint(selected)))
        connection.execute('''DELETE FROM offers WHERE id NOT IN
            (SELECT id FROM offers ORDER BY created DESC, rowid DESC LIMIT 3000)''')


def refresh(db, candidate_id=None):
    from .nicotine_bridge.downloads import selection_parts
    from .nicotine import search
    selection_parts(candidate_id)
    path = cache_path(db)
    if not path.is_file():
        return {'status': 'requires_selection', 'candidate_id': candidate_id,
                'reason': 'No saved peer/file receipt exists for this ID. A fresh result must be reviewed; nothing was queued.'}
    with closing(sqlite3.connect(path)) as connection:
        saved = connection.execute('SELECT query,kind,fingerprint FROM offers WHERE id=?', (candidate_id,)).fetchone()
    if not saved:
        return {'status': 'requires_selection', 'candidate_id': candidate_id,
                'reason': 'The original peer/file observation is unavailable. Review a fresh result; nothing was queued.'}
    result = search(db, saved[0], saved[1], None)
    with closing(sqlite3.connect(path)) as connection:
        matches = []
        for offer in result['candidates']:
            receipt = connection.execute('SELECT fingerprint FROM offers WHERE id=?', (offer.get('candidate_id'),)).fetchone()
            if receipt and receipt[0] == saved[2]:
                matches.append(offer)
    return {'status': 'same_peer_and_files' if len(matches) == 1 else 'requires_selection',
            'previous_candidate_id': candidate_id, 'candidates': matches,
            'reason': 'Exact advertised peer, paths, sizes, formats and quality fields compared. Audio and edition remain unverified. Nothing was queued.'}
