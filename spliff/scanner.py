"""Incremental probing with an atomic catalog update per scan."""
import os
import stat
import subprocess
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from .catalog import EXTENSIONS, connect, file_identity, find_probe, initialize, read_audio, signature

AUDIO_FIELDS = ("title", "artist", "album", "codec", "format", "quality",
                "duration", "bitrate", "sample_rate", "bits", "track_number", "disc_number")

def inside(path, root):
    return path == root or root in path.parents

def scan(db, root, progress=None):
    db = db.resolve()
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("The music folder is unavailable")
    if inside(db, root):
        raise ValueError("The SQLite catalog must be outside the music library")
    binary = find_probe()
    initialize(db)
    token = uuid4().hex
    counts = dict(probed=0, cached=0, errors=0, missing=0)
    scan_name = str(root)

    def walk_error(error):
        raise error

    with closing(connect(db)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        root_info = root.stat()
        identity = file_identity(root_info)
        roots = connection.execute("SELECT path,device,inode FROM roots").fetchall()
        library = root
        children = []
        for prior in roots:
            prior_path = Path(prior["path"])
            if inside(root, prior_path) or inside(prior_path, root):
                info = prior_path.stat()
                if file_identity(info) != tuple(prior)[1:]:
                    raise ValueError("The library root changed identity. Verify the mounted drive.")
                if inside(root, prior_path):
                    library = prior_path
                else:
                    children.append(prior_path)
        root_name = str(library)
        connection.execute("INSERT OR IGNORE INTO roots VALUES (?,?,?)", (root_name, *identity))
        for child in children:
            prefix = str(child.relative_to(library)) + "/"
            connection.execute("UPDATE tracks SET root=?, path=? || path WHERE root=?",
                               (root_name, prefix, str(child)))
            connection.execute("DELETE FROM roots WHERE path=?", (str(child),))
        for folder, directories, files, folder_fd in os.fwalk(root, onerror=walk_error, follow_symlinks=False):
            folder_info = os.fstat(folder_fd)
            if folder == scan_name and file_identity(folder_info) != identity:
                raise ValueError("The library root changed during scan")
            directories[:] = sorted(name for name in directories if not name.startswith("."))
            for name in sorted(files):
                if name.startswith(".") or Path(name).suffix.lower() not in EXTENSIONS:
                    continue
                info = os.stat(name, dir_fd=folder_fd, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    continue
                path = str((Path(folder) / name).relative_to(library))
                path.encode("utf-8")
                fingerprint = signature(info)
                previous = connection.execute("""
                    SELECT size,mtime,device,inode,error FROM tracks WHERE root=? AND path=?
                """, (root_name, path)).fetchone()
                if previous and tuple(previous) == (*fingerprint, None):
                    connection.execute("UPDATE tracks SET seen=?,present=1 WHERE root=? AND path=?",
                                       (token, root_name, path))
                    counts["cached"] += 1
                    if progress and counts["cached"] % 1000 == 0:
                        progress(dict(counts))
                    continue
                error = None
                try:
                    metadata = read_audio(binary, folder_fd, name, fingerprint)
                    counts["probed"] += 1
                except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError) as exc:
                    metadata = {}
                    error = "Audio inspection failed (" + type(exc).__name__ + ")"
                    counts["errors"] += 1
                columns = ("root", "path", *AUDIO_FIELDS, "size", "mtime", "device",
                           "inode", "seen", "error", "present")
                updates = ",".join(f"{key}=excluded.{key}" for key in columns[2:])
                connection.execute(
                    f"INSERT INTO tracks({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) "
                    f"ON CONFLICT(root,path) DO UPDATE SET {updates}",
                    (root_name, path, *(metadata.get(key) for key in AUDIO_FIELDS),
                     *fingerprint, token, error, 1),
                )
                examined = counts["probed"] + counts["errors"]
                if progress and examined % 100 == 0:
                    progress(dict(counts))
        ending = root.stat()
        if file_identity(ending) != identity:
            raise ValueError("The library root changed during scan")
        missing_sql = "UPDATE tracks SET present=0 WHERE root=? AND present=1 AND seen IS NOT ?"
        parameters = (root_name, token)
        if root != library:
            prefix = str(root.relative_to(library)) + "/"
            missing_sql += " AND substr(path,1,?)=?"
            parameters += (len(prefix), prefix)
        counts["missing"] = connection.execute(missing_sql, parameters).rowcount
    return counts
