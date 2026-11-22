"""Queue a specific search offer and inspect its downloaded files for review."""
import os
import re
import stat
import subprocess
import unicodedata
from contextlib import closing
from pathlib import Path
from time import monotonic

from .catalog import connect, find_probe, read_audio, signature
from .nicotine import request
from .nicotine_bridge.downloads import SELECTION, read_job, selection_parts
from .nicotine_bridge.wire import staging_root


def queue(db, candidate_id=None):
    selection_parts(candidate_id)
    with closing(connect(db)) as connection:
        roots = [row["path"] for row in connection.execute("SELECT path FROM roots")]
    result = request("queue", selection=candidate_id, roots=roots)
    return public_status(result)


def public_status(result):
    return {
        **result,
        "items": [{key: value for key, value in item.items() if key != "file"}
                  for item in result["items"]],
    }


def list_downloads():
    root = staging_root()
    if not root.is_dir() or root.resolve() != root:
        return []
    folders = (folder for folder in root.iterdir() if SELECTION.fullmatch(folder.name) and not folder.is_symlink())
    jobs = []
    for folder in sorted(folders, key=lambda item: item.stat().st_mtime, reverse=True)[:30]:
        try:
            manifest = read_job(folder)
        except (ValueError, OSError):
            continue
        jobs.append({key: manifest[key] for key in ("id", "query", "kind", "created")})
    return jobs


def title_key(title):
    return "".join(character for character in unicodedata.normalize("NFKC", title).casefold()
                   if character.isalnum())


def recording_title(track):
    title = track["title"] or ""
    match = re.fullmatch(r"(.*?)\s+\(([^()]*)\)", title)
    if match:
        base, credit = match.groups()
        lead = re.split(r"\b(?:feat|ft|featuring)\.?", credit, maxsplit=1, flags=re.I)[0]
        key = title_key(lead)
        if not key or title_key(track["artist"] or "").startswith(key):
            return title_key(base)
    return title_key(title)


def filename_number(name):
    match = re.match(r"^(\d{1,3})[ ._-]+", Path(name).name)
    return int(match.group(1)) if match else None


def originals(db, track_id, kind):
    if track_id is None:
        return []
    if type(track_id) is not int or not 0 < track_id < 2**63:
        raise ValueError("Choose a valid original track ID.")
    fields = "id,path,artist,album,title,codec,quality,duration,protected"
    with closing(connect(db)) as connection:
        row = connection.execute(
            f"SELECT root,{fields} FROM tracks WHERE id=? AND present=1 AND error IS NULL",
            (track_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Choose an available, successfully indexed original track.")
        if kind == "track" or not row["album"]:
            return [{key: row[key] for key in row.keys() if key != "root"}]
        siblings = connection.execute(f"""
            SELECT {fields} FROM tracks WHERE root=? AND album=? AND present=1 AND error IS NULL
            ORDER BY path
        """, (row["root"], row["album"])).fetchall()
    folder = Path(row["path"]).parent
    return [dict(item) for item in siblings if Path(item["path"]).parent == folder]


def inspect_file(binary, directory, relative, timeout):
    candidate = directory / relative
    if not candidate.is_relative_to(directory) or candidate.resolve(strict=True) != candidate:
        raise ValueError("Unexpected staging file location.")
    info = candidate.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Unexpected staging file type.")
    parent = os.open(candidate.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        return read_audio(binary, parent, candidate.name, signature(info), timeout)
    finally:
        os.close(parent)


def match_item(item, indexed, kind, filename):
    if item["quality"] != "lossless":
        return "candidate is not verified lossless", None
    if not indexed:
        return "original not selected", None
    if kind == "track":
        candidates = indexed
    else:
        key = recording_title(item)
        candidates = [track for track in indexed if key and recording_title(track) == key]
        if not candidates:
            number = filename_number(filename)
            candidates = [track for track in indexed
                          if number is not None and filename_number(track["path"]) == number]
    if len(candidates) != 1:
        return ("ambiguous title" if candidates else "no matching title"), None
    original = candidates[0]
    if (not item["title"] or not original["title"]
            or recording_title(item) != recording_title(original)
            or not item["artist"] or not original["artist"]
            or title_key(item["artist"]) != title_key(original["artist"])):
        return "check title or artist", original
    if (not original["duration"] or not item["duration"]
            or abs(original["duration"] - item["duration"]) > 2):
        return "check duration", original
    if (not original["album"] or not item["album"]
            or title_key(item["album"]) != title_key(original["album"])):
        return "check release tags", original
    return "possible match", original


def review_download(db, candidate_id=None, original_track_id=None):
    selection_parts(candidate_id)
    result = request("download_status", selection=candidate_id)
    indexed = originals(db, original_track_id, result["kind"])
    directory = staging_root() / candidate_id
    binary = find_probe() if result["complete"] else None
    report = public_status(result)
    possible = set()
    deadline = monotonic() + 35
    for transfer, item in zip(result["items"], report["items"]):
        if not transfer["file"]:
            continue
        remaining = deadline - monotonic()
        if remaining <= 0:
            item["assessment"] = "inspection time limit reached"
            continue
        try:
            metadata = inspect_file(binary, directory, transfer["file"], min(remaining, 3))
        except (OSError, ValueError, IndexError, KeyError, subprocess.SubprocessError):
            item["assessment"] = "audio inspection failed"
            continue
        item["audio"] = metadata
        item["assessment"], original = match_item(metadata, indexed, result["kind"], item["name"])
        if original:
            item["original"] = original
            if item["assessment"] == "possible match":
                if original["id"] in possible:
                    item["assessment"] = "another candidate also matches this original"
                possible.add(original["id"])
    report["originals_without_match"] = [item for item in indexed if item["id"] not in possible]
    return report
