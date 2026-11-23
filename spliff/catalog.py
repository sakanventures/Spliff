"""SQLite catalog and local audio inspection."""
import json
import os
import shutil
import sqlite3
import stat
import subprocess
from contextlib import closing
from pathlib import Path

EXTENSIONS = {".mp3", ".flac", ".wav", ".aiff", ".aif", ".m4a", ".aac", ".ogg",
              ".oga", ".opus", ".alac", ".ape", ".wv", ".wma", ".dsf", ".dff"}
LOSSLESS = {"flac", "alac", "ape", "wavpack", "tta", "wmalossless", "truehd"}
LOSSY = {"mp3", "mp2", "aac", "vorbis", "opus", "wmav1", "wmav2", "wmapro"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS roots(path TEXT PRIMARY KEY, device INTEGER, inode INTEGER);
CREATE TABLE IF NOT EXISTS tracks(
 id INTEGER PRIMARY KEY, root TEXT NOT NULL REFERENCES roots(path), path TEXT NOT NULL,
 title TEXT, artist TEXT, album TEXT, codec TEXT, format TEXT, quality TEXT,
 duration REAL, bitrate INTEGER, sample_rate INTEGER, bits INTEGER,
 size INTEGER, mtime INTEGER, device INTEGER, inode INTEGER, seen TEXT, error TEXT,
 protected INTEGER NOT NULL DEFAULT 0, present INTEGER NOT NULL DEFAULT 1,
 UNIQUE(root,path));
CREATE INDEX IF NOT EXISTS tracks_album ON tracks(album COLLATE NOCASE, present);
CREATE INDEX IF NOT EXISTS tracks_review ON tracks(present,quality,protected,id);
CREATE TABLE IF NOT EXISTS kept_albums(
 candidate TEXT PRIMARY KEY, destination TEXT NOT NULL,
 mode TEXT NOT NULL DEFAULT 'keep',
 created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS download_comparisons(
 candidate TEXT PRIMARY KEY, original_id INTEGER NOT NULL REFERENCES tracks(id));
CREATE TABLE IF NOT EXISTS transfer_reviews(
 candidate TEXT PRIMARY KEY, report TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS transfer_preparations(
 candidate TEXT PRIMARY KEY, prepared TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS upgrade_plans(
 token TEXT PRIMARY KEY, candidate TEXT NOT NULL, mode TEXT NOT NULL,
 original_id INTEGER NOT NULL, preview TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS upgrade_results(
 candidate TEXT PRIMARY KEY, result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS edition_evidence(
 candidate TEXT PRIMARY KEY, metadata_key TEXT NOT NULL, evidence TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sync_folders(
 root TEXT PRIMARY KEY, selected TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS sync_targets(
 root TEXT NOT NULL, original_id INTEGER NOT NULL REFERENCES tracks(id),
 kind TEXT NOT NULL, query TEXT NOT NULL, candidate TEXT,
 state TEXT NOT NULL DEFAULT 'queued', detail TEXT,
 PRIMARY KEY(root,original_id));
CREATE TABLE IF NOT EXISTS preferences(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reviews(
 id INTEGER PRIMARY KEY, original_id INTEGER NOT NULL REFERENCES tracks(id),
 candidate TEXT NOT NULL, snapshot TEXT NOT NULL, original_snapshot TEXT NOT NULL,
 created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
"""

def connect(db):
    connection = sqlite3.connect(db, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def replacement_destination(connection):
    row = connection.execute("SELECT value FROM preferences WHERE key='replace_originals'").fetchone()
    return row["value"] if row else "trash"


def replacement_preference(db, destination=None):
    if destination is not None and destination not in ("trash", "hidden"):
        raise ValueError("Choose Mac Trash or Hidden recovery folder.")
    with closing(connect(db)) as connection, connection:
        if destination is not None:
            connection.execute("""
                INSERT INTO preferences(key,value) VALUES ('replace_originals',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """, (destination,))
        return {"destination": replacement_destination(connection)}

def initialize(db):
    db.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(db)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript(SCHEMA)
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(kept_albums)")}
            track_columns = {row["name"] for row in connection.execute("PRAGMA table_info(tracks)")}
            for column in ("track_number", "disc_number"):
                if column not in track_columns:
                    connection.execute(f"ALTER TABLE tracks ADD COLUMN {column} TEXT")
            if "mode" not in columns:
                connection.execute("ALTER TABLE kept_albums ADD COLUMN mode TEXT NOT NULL DEFAULT 'keep'")

def sqlite_integer(value):
    # SQLite stores signed integers, while filesystem identifiers can be unsigned.
    return (value + 2**63) % 2**64 - 2**63


def file_identity(info):
    return sqlite_integer(info.st_dev), sqlite_integer(info.st_ino)


def signature(info):
    return sqlite_integer(info.st_size), sqlite_integer(info.st_mtime_ns), *file_identity(info)

def probe(binary, descriptor, timeout=30):
    result = subprocess.run(
        [binary, "-v", "error", "-protocol_whitelist", "file", "-format_whitelist",
         "mp3,flac,wav,aiff,mov,aac,ogg,ape,wv,asf,dsf,iff", "-select_streams", "a:0",
         "-show_entries", "stream=codec_name,duration,bit_rate,sample_rate,bits_per_raw_sample,bits_per_sample:"
         "stream_tags=title,artist,album,track,disc:format=format_name,duration,bit_rate:format_tags=title,artist,album,track,disc",
         "-of", "json", "-i", f"/dev/fd/{descriptor}"],
        capture_output=True, check=True, timeout=timeout, pass_fds=(descriptor,),
    )
    data = json.loads(result.stdout)
    stream = data["streams"][0]
    container = data.get("format", {})
    tags = {key.lower(): value for source in (container, stream)
            for key, value in source.get("tags", {}).items()}
    codec = stream.get("codec_name", "")
    if codec in LOSSLESS or codec.startswith(("pcm_s", "pcm_u", "pcm_f", "dsd_")):
        quality = "lossless"
    elif codec in LOSSY:
        quality = "lossy"
    else:
        quality = "unknown"

    def number(key, cast=int, fallback=None):
        for value in (stream.get(key), container.get(key), fallback):
            try:
                converted = cast(value)
                if 0 < converted < 10**15:
                    return converted
            except (ValueError, TypeError):
                pass
        return None

    return dict(title=tags.get("title"), artist=tags.get("artist"), album=tags.get("album"),
                track_number=tags.get("track"), disc_number=tags.get("disc"),
                codec=codec, format=container.get("format_name"), quality=quality,
                duration=number("duration", float), bitrate=number("bit_rate"),
                sample_rate=number("sample_rate"),
                bits=number("bits_per_raw_sample", fallback=stream.get("bits_per_sample")))

def read_audio(binary, parent_fd, name, expected, timeout=30):
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or signature(info) != expected:
            raise ValueError("File changed during inspection")
        metadata = probe(binary, descriptor, timeout)
        if signature(os.fstat(descriptor)) != expected:
            raise ValueError("File changed during inspection")
        return metadata
    finally:
        os.close(descriptor)

def find_probe():
    binary = shutil.which("ffprobe")
    if not binary:
        raise ValueError("ffprobe is required. Install FFmpeg and add it to PATH.")
    return binary


def contains_pattern(query):
    return "%" + query.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"


def albums(db, query=""):
    if not isinstance(query, str) or len(query) > 300:
        raise ValueError("Invalid album search")
    pattern = contains_pattern(query.strip())
    with closing(connect(db)) as connection:
        connection.create_function("album_folder", 1, lambda path: path.rpartition("/")[0])
        rows = connection.execute("""
            SELECT min(id) AS id, root, album, min(nullif(artist,'')) AS artist, album_folder(path) AS folder,
                   count(*) AS total, sum(codec='mp3') AS mp3, sum(protected) AS protected
            FROM tracks
            WHERE present=1 AND error IS NULL AND nullif(trim(album),'') IS NOT NULL
            GROUP BY root, album COLLATE NOCASE, album_folder(path)
            HAVING sum(codec='mp3') > 0 AND (album LIKE ? ESCAPE '!'
                OR max(coalesce(artist,'') LIKE ? ESCAPE '!') OR folder LIKE ? ESCAPE '!')
            ORDER BY album COLLATE NOCASE, id
            LIMIT 26
        """, (pattern, pattern, pattern)).fetchall()
        has_library = bool(connection.execute("SELECT 1 FROM roots LIMIT 1").fetchone())
    return {"items": [dict(row) for row in rows[:25]], "more": len(rows) > 25, "has_library": has_library}


def tracks(db, query="", scope="all", limit=40, after=0):
    if scope not in ("all", "lossy", "lossless", "protected", "errors", "missing"):
        raise ValueError("Unknown library filter")
    if (not isinstance(query, str) or len(query) > 300 or type(limit) is not int
            or not 1 <= limit <= 100 or type(after) is not int or not 0 <= after < 2**63 - 1):
        raise ValueError("Invalid search parameters")
    conditions = {"all": "present=1", "lossy": "present=1 AND quality='lossy' AND error IS NULL",
                  "lossless": "present=1 AND quality='lossless' AND error IS NULL",
                  "protected": "protected=1", "errors": "present=1 AND error IS NOT NULL",
                  "missing": "present=0"}
    phrase = contains_pattern(query)
    with closing(connect(db)) as connection:
        rows = connection.execute(f"""
            SELECT id,path,title,artist,album,codec,format,quality,duration,bitrate,
                   sample_rate,bits,protected,present,error
            FROM tracks WHERE {conditions[scope]} AND id>?
            AND (title LIKE ? ESCAPE '!' OR artist LIKE ? ESCAPE '!'
                 OR album LIKE ? ESCAPE '!' OR path LIKE ? ESCAPE '!')
            ORDER BY id LIMIT ?
        """, (after, phrase, phrase, phrase, phrase, limit + 1)).fetchall()
    return {"items": [dict(row) for row in rows[:limit]],
            "next": rows[limit-1]["id"] if len(rows) > limit else None}

def stats(db):
    with closing(connect(db)) as connection:
        return dict(connection.execute("""
            SELECT count(CASE WHEN present=1 THEN 1 END) AS tracks,
                   count(CASE WHEN present=1 AND quality='lossless' AND error IS NULL THEN 1 END) AS lossless,
                   count(CASE WHEN present=1 AND quality='lossy' AND error IS NULL THEN 1 END) AS lossy,
                   count(CASE WHEN protected=1 THEN 1 END) AS protected,
                   count(CASE WHEN present=1 AND error IS NOT NULL THEN 1 END) AS errors,
                   count(CASE WHEN present=0 THEN 1 END) AS missing,
                   (SELECT count(*) FROM roots) AS roots FROM tracks
        """).fetchone())
