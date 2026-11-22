"""Queue a specific search offer and inspect its downloaded files for review."""
import os
import re
import stat
import subprocess
import unicodedata
from contextlib import closing
from pathlib import Path, PureWindowsPath
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


def staged_file_present(folder, entry):
    directory = folder / entry["directory"]
    if directory.parent != folder or directory.is_symlink() or not directory.is_dir():
        return False
    suffix = PureWindowsPath(entry["remote"]).suffix.casefold()
    for candidate in directory.iterdir():
        info = candidate.lstat()
        if (stat.S_ISREG(info.st_mode) and candidate.suffix.casefold() == suffix
                and info.st_size == entry["bytes"]):
            return True
    return False


def list_downloads():
    root = staging_root()
    if not root.is_dir() or root.resolve() != root:
        return []
    folders = (folder for folder in root.iterdir() if SELECTION.fullmatch(folder.name) and not folder.is_symlink())
    jobs = []
    for folder in sorted(folders, key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            manifest = read_job(folder)
            entries = manifest["entries"]
            if not entries or not all(staged_file_present(folder, entry) for entry in entries):
                continue
        except (ValueError, OSError):
            continue
        entry = entries[0]
        remote = PureWindowsPath(entry["remote"])
        title = (remote.parent.name if manifest["kind"] == "album" else remote.stem)
        jobs.append({**{key: manifest[key] for key in ("id", "query", "kind", "created")},
                     "title": title or manifest["query"], "peer": entry["peer"],
                     "total": len(entries)})
        if len(jobs) >= 30:
            break
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


ORIGINAL_FIELDS = "id,path,artist,album,title,codec,quality,duration,protected"


def originals(db, track_id, kind):
    if track_id is None:
        return []
    if type(track_id) is not int or not 0 < track_id < 2**63:
        raise ValueError("Choose a valid original track ID.")
    fields = ORIGINAL_FIELDS
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
        return read_audio(binary, parent, candidate.name, signature(info), timeout), signature(info)
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


def suggest_album(db, items):
    completed = [item for item in items if item.get("audio", {}).get("quality") == "lossless"]
    albums = {item["audio"]["album"] for item in completed if item["audio"]["album"]}
    if len(albums) != 1:
        return []
    with closing(connect(db)) as connection:
        rows = connection.execute(f"""
            SELECT root,{ORIGINAL_FIELDS} FROM tracks
            WHERE album=? COLLATE NOCASE AND present=1 AND error IS NULL ORDER BY path
        """, (next(iter(albums)),)).fetchall()
    groups = {}
    for row in rows:
        key = (row["root"], Path(row["path"]).parent)
        groups.setdefault(key, []).append({name: row[name] for name in row.keys() if name != "root"})
    ranked = []
    for group in groups.values():
        matches = set()
        for item in completed:
            assessment, original = match_item(item["audio"], group, "album", item["name"])
            if assessment == "possible match":
                matches.add(original["id"])
        if len(matches) >= min(2, len(completed)):
            ranked.append(group)
    return ranked[0] if len(ranked) == 1 else []


def review_download(db, candidate_id=None, original_track_id=None, *, include_sources=False):
    selection_parts(candidate_id)
    result = request("download_status", selection=candidate_id)
    indexed = originals(db, original_track_id, result["kind"])
    directory = staging_root() / candidate_id
    binary = find_probe() if result["complete"] else None
    report = public_status(result)
    with closing(connect(db)) as connection:
        kept = connection.execute("SELECT destination FROM kept_albums WHERE candidate=?",
                                  (candidate_id,)).fetchone()
    report["kept_both"] = dict(kept) if kept else None
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
            metadata, fingerprint = inspect_file(binary, directory, transfer["file"], min(remaining, 3))
        except (OSError, ValueError, IndexError, KeyError, subprocess.SubprocessError):
            item["assessment"] = "audio inspection failed"
            continue
        item["audio"] = metadata
        if include_sources:
            item["source"] = str(directory / transfer["file"])
            item["fingerprint"] = fingerprint
    suggested = original_track_id is None and result["kind"] == "album"
    if suggested:
        indexed = suggest_album(db, report["items"])
    report["comparison"] = ({"album": indexed[0]["album"], "suggested": suggested,
                              "original_track_id": indexed[0]["id"]} if indexed else None)
    tagged_albums = {title_key(item["audio"]["album"]) for item in report["items"]
                     if item.get("audio", {}).get("album")}
    selected_album = indexed[0]["album"] if indexed else None
    if result["kind"] == "album" and selected_album and tagged_albums and title_key(selected_album) not in tagged_albums:
        report["comparison"]["album_mismatch"] = True
        report["originals_without_match"] = []
        return report
    for item in report["items"]:
        if "audio" not in item:
            continue
        metadata = item["audio"]
        item["assessment"], original = match_item(metadata, indexed, result["kind"], item["name"])
        if original:
            item["original"] = original
            if item["assessment"] == "possible match":
                if original["id"] in possible:
                    item["assessment"] = "another candidate also matches this original"
                possible.add(original["id"])
    report["originals_without_match"] = [item for item in indexed if item["id"] not in possible]
    return report
