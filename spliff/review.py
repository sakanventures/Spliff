"""Rare-recording holds and explicitly staged, metadata-only comparisons."""
import json
import os
from contextlib import closing
from pathlib import Path

from .catalog import connect, find_probe, read_audio, signature
from .scanner import inside

def protect(db, track_id, enabled):
    with closing(connect(db)) as connection, connection:
        if not connection.execute("UPDATE tracks SET protected=? WHERE id=?",
                                  (enabled, track_id)).rowcount:
            raise ValueError("Unknown track ID")

def stage(db, track_id, candidate):
    if candidate.is_symlink():
        raise ValueError("Choose a regular candidate file")
    candidate = candidate.resolve(strict=True)
    with closing(connect(db)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("""
            SELECT * FROM tracks WHERE id=? AND present=1 AND error IS NULL
        """, (track_id,)).fetchone()
        if not row:
            raise ValueError("Choose an available, successfully indexed original")
        if any(inside(candidate, Path(item["path"])) for item in connection.execute("SELECT path FROM roots")):
            raise ValueError("Keep the candidate in a separate staging folder")
        source = Path(row["root"]) / row["path"]
        expected = tuple(row[key] for key in ("size", "mtime", "device", "inode"))
        if source.resolve(strict=True) != source or signature(source.stat()) != expected:
            raise ValueError("The original changed. Rescan the library first.")
        fingerprint = signature(candidate.stat())
        if fingerprint[2:] == expected[2:]:
            raise ValueError("Choose a candidate distinct from the original")
        directory = os.open(candidate.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            metadata = read_audio(find_probe(), directory, candidate.name, fingerprint)
        finally:
            os.close(directory)
        if metadata["quality"] != "lossless":
            raise ValueError("Only candidates with a recognized lossless codec can be staged")
        metadata["signature"] = fingerprint
        result = connection.execute("""
            INSERT INTO reviews(original_id,candidate,snapshot,original_snapshot) VALUES (?,?,?,?)
        """, (track_id, str(candidate), json.dumps(metadata), json.dumps(dict(row))))
        return result.lastrowid

def reviews(db):
    with closing(connect(db)) as connection:
        rows = connection.execute("""
            SELECT reviews.*, tracks.protected, tracks.present FROM reviews
            JOIN tracks ON tracks.id=original_id ORDER BY reviews.id DESC LIMIT 100
        """).fetchall()
    results = []
    for row in rows:
        item = dict(row)
        item["candidate"] = os.path.basename(item["candidate"])
        item["snapshot"] = json.loads(item["snapshot"])
        item["original_snapshot"] = json.loads(item["original_snapshot"])
        item["original_snapshot"].pop("root")
        results.append(item)
    return results
