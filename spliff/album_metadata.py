"""Review and write music tags for an indexed album's native FLAC files."""
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.error import URLError
from urllib.request import build_opener
from uuid import uuid4

from . import discogs, flac_metadata, keep_both, library_import
from .catalog import connect, metadata_album_folder, signature


def tracks_for_album(connection, track_id):
    seed = connection.execute("SELECT root, path, album FROM tracks WHERE id=? AND present=1",
                              (track_id,)).fetchone()
    if seed is None or not seed["album"]:
        raise ValueError("Choose an indexed album.")
    folder = metadata_album_folder(seed["path"])
    rows = connection.execute("""
        SELECT * FROM tracks WHERE root=? AND album=? COLLATE NOCASE AND present=1
        ORDER BY path
    """, (seed["root"], seed["album"])).fetchall()
    return [row for row in rows if metadata_album_folder(row["path"]) == folder]


def options(db, track_id):
    with closing(connect(db)) as connection:
        tracks = tracks_for_album(connection, track_id)
    artist = next((track["artist"] for track in tracks if track["artist"]), "")
    if not artist:
        raise ValueError("The indexed album needs an artist before Spliff can search Discogs.")
    return discogs.search_releases(db, artist, tracks[0]["album"])


def prepare(db, track_id, release_id, artwork):
    release = discogs.release_details(db, release_id)
    artist = ", ".join(name for name in release["artists"] if name)
    date = release["released"] or str(release["year"] or "")
    tags = {key: value for key, value in
            {"ARTIST": artist, "ALBUMARTIST": artist, "ALBUM": release["title"], "DATE": date}.items() if value}
    if not artist or not release["title"]:
        raise ValueError("Discogs did not provide the artist and release title.")
    picture = None
    cover = discogs.release_cover(release_id) if artwork else None
    if cover:
        try:
            with build_opener(discogs.NoRedirect()).open(cover, timeout=10) as response:
                image = response.read(8 * 1024 * 1024 + 1)
        except URLError as exc:
            raise ValueError("Discogs artwork could not be loaded. Review again with artwork turned off.") from exc
        if len(image) > 8 * 1024 * 1024:
            raise ValueError("The Discogs artwork is too large.")
        picture = flac_metadata.cover_block(image)
    identities, files = {}, []
    with closing(connect(db)) as connection:
        tracks = tracks_for_album(connection, track_id)
        roots = library_import.catalog_roots(connection)
        for row in tracks:
            path = Path(row["root"]) / row["path"]
            if row["protected"] or row["error"] or row["codec"] != "flac" or path.suffix.lower() != ".flac":
                continue
            root = Path(row["root"])
            if root not in path.parents:
                raise ValueError("A track is outside its indexed library.")
            root_identity = roots.get(str(root))
            if not root_identity or None in root_identity:
                raise ValueError("Rescan the library folder before editing metadata.")
            library_import.check_root(db, root, root_identity)
            identities[str(root)] = root_identity
            keep_both.remember_directory(path.parent, identities)
            expected = tuple(row[key] for key in ("size", "mtime", "device", "inode"))
            with library_import.read_pinned(path, expected, identities) as source:
                before = flac_metadata.music_tags(source)
            files.append((row["id"], path, expected, {key: before.get(key) for key in tags}))
    if not files:
        raise ValueError("This folder has no editable native FLAC tracks.")
    return {"track": track_id, "identities": identities, "files": files, "tags": tags, "picture": picture,
            "skipped": len(tracks) - len(files)}


def install(db, plan):
    copies = []
    updated = 0
    try:
        with closing(connect(db)) as connection:
            for track_id, path, expected, _ in plan["files"]:
                copy = path.with_name(f".spliff-tags-{uuid4().hex}.tmp")
                _, written = library_import.write_tagged_copy(
                    path, expected, copy, plan["tags"], plan["identities"], plan["picture"], preserve_mode=True)
                copies.append((track_id, path, expected, copy, written))
            for track_id, path, expected, copy, written in copies:
                with connection, keep_both.open_directory(path.parent, plan["identities"]) as parent:
                    connection.execute("BEGIN IMMEDIATE")
                    row = connection.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()
                    if (not row or row["protected"] or not row["present"] or Path(row["root"]) / row["path"] != path
                            or keep_both.file_signature(parent, path.name) != expected
                            or keep_both.file_signature(parent, copy.name) != written):
                        raise ValueError("A reviewed track changed. Review this album again.")
                    os.replace(copy.name, path.name, src_dir_fd=parent, dst_dir_fd=parent)
                    updated += 1
                    info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
                    connection.execute("""
                        UPDATE tracks SET artist=?, album=?, size=?, mtime=?, device=?, inode=? WHERE id=?
                    """, (plan["tags"]["ARTIST"], plan["tags"]["ALBUM"], *signature(info), track_id))
    except (ValueError, OSError, sqlite3.Error):
        if not updated:
            raise
        return {"updated": updated, "error": "The update stopped partway through. Review the album again."}
    finally:
        for _, _, _, copy, written in copies:
            try:
                keep_both.unlink_verified(copy, written, plan["identities"])
            except (FileNotFoundError, ValueError, OSError):
                pass
    return {"updated": updated}
