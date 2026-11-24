"""Queue a specific search offer and inspect its downloaded files for review."""
import json
import os
import re
import stat
import subprocess
import unicodedata
from contextlib import closing, nullcontext
from pathlib import Path, PureWindowsPath
from time import monotonic

from .candidates import saved_fingerprint
from .catalog import connect, find_probe, read_audio, signature
from .artist import release_folder
from .discogs import release_details, search_releases
from .nicotine import request
from .recording_match import compare_recordings
from .nicotine_bridge.downloads import SELECTION, read_job, selection_parts
from .nicotine_bridge.wire import staging_root


def queue(db, candidate_id=None, *, original_id=None, source="manual"):
    if source not in {"mcp", "dashboard", "manual"}:
        raise ValueError("Staging requires an explicit candidate selection.")
    kind, _, _ = selection_parts(candidate_id)
    try:
        read_job(staging_root() / candidate_id)
    except ValueError:
        existing = False
    else:
        existing = True
    if applied := applied_transfer(db, candidate_id):
        return applied
    if original_id is not None:
        indexed = originals(db, original_id, kind)
        if kind == "album" and not indexed[0]["album"]:
            raise ValueError("Choose an indexed album to compare.")
    with closing(connect(db)) as connection:
        roots = [row["path"] for row in connection.execute("SELECT path FROM roots")]
    receipt = None if existing else saved_fingerprint(db, candidate_id)
    if not existing and not receipt:
        raise ValueError("Search again and explicitly select a candidate with a saved peer/file receipt.")
    if original_id is not None:
        with closing(connect(db)) as connection, connection:
            connection.execute("""
                INSERT INTO download_comparisons(candidate, original_id) VALUES (?, ?)
                ON CONFLICT(candidate) DO UPDATE SET original_id=excluded.original_id
            """, (candidate_id, original_id))
    try:
        result = (request("download_status", selection=candidate_id) if existing else
                  request("queue", selection=candidate_id, roots=roots, fingerprint=receipt,
                          source=source, original_id=original_id))
    except ValueError as exc:
        if "did not confirm the action" not in str(exc):
            raise
        # The GUI action may already have started and written its manifest.
        # Never retry queueing on an uncertain acknowledgement.
        try:
            result = request("download_status", selection=candidate_id)
        except (ValueError, OSError):
            result = {"id": candidate_id, "items": [], "state": "confirmation_pending",
                      "requires_status_check": True,
                      "message": "Nicotine+ has not confirmed this action. Check nicotine_transfer_status before any retry; do not treat this as a definite failure."}
    return public_status(result)


def applied_transfer(db, candidate_id):
    with closing(connect(db)) as connection:
        applied = connection.execute("""
            SELECT k.mode,k.destination,r.result FROM kept_albums k LEFT JOIN upgrade_results r
            ON r.candidate=k.candidate WHERE k.candidate=?
        """, (candidate_id,)).fetchone()
    if applied:
        return {"id": candidate_id, "state": "applied", "mode": applied["mode"],
                "destination": applied["destination"],
                "completion": json.loads(applied["result"]) if applied["result"] else None}


def public_status(result):
    return {
        **result,
        "items": [{key: value for key, value in item.items() if key != "file"}
                  for item in result["items"]],
    }


def staged_path(folder, entry):
    directory = folder / entry["directory"]
    if directory.parent != folder or not directory.name.isdecimal() or directory.resolve() != directory:
        return None
    if not directory.is_dir():
        return None
    remote = PureWindowsPath(entry["remote"])
    matches = []
    for candidate in directory.iterdir():
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISREG(info.st_mode) and candidate.suffix.casefold() == remote.suffix.casefold()
                and info.st_size == entry["bytes"]):
            matches.append(candidate)
    if len(matches) == 1:
        return str(matches[0].relative_to(folder))
    return None


def local_transfer(candidate):
    folder = staging_root() / candidate
    manifest = read_job(folder)
    if manifest["id"] != candidate or manifest["kind"] != selection_parts(candidate)[0]:
        raise ValueError("The transfer receipt does not match its staging folder.")
    items = []
    for entry in manifest["entries"]:
        path = staged_path(folder, entry)
        items.append({"name": PureWindowsPath(entry["remote"]).name, "bytes": entry["bytes"],
                      "status": "Finished" if path else "Waiting for Nicotine+",
                      "file": path})
    return {**manifest, "items": items, "complete": sum(bool(item["file"]) for item in items)}


def transfer_status(candidate):
    selection_parts(candidate)
    try:
        result = request("download_status", selection=candidate)
    except (ValueError, OSError):
        return local_transfer(candidate)
    if not result["complete"] or result["complete"] < len(result["items"]):
        try:
            local = local_transfer(candidate)
        except (OSError, ValueError):
            return result
        if local["complete"] > result["complete"]:
            return local
    return result


def list_downloads(db=None):
    root = staging_root()
    if not root.is_dir() or root.resolve() != root:
        return []
    with closing(connect(db)) if db else nullcontext() as connection:
        prepared = ({row["candidate"] for row in
                     connection.execute("SELECT candidate FROM transfer_preparations")} if connection else set())
        applied = ({row["candidate"] for row in
                    connection.execute("SELECT candidate FROM kept_albums")} if connection else set())
        associations = ({row["candidate"]: row["original_id"] for row in
                         connection.execute("SELECT * FROM download_comparisons")} if connection else {})
    jobs = []
    for folder in root.iterdir():
        if not SELECTION.fullmatch(folder.name) or folder.is_symlink():
            continue
        try:
            manifest = local_transfer(folder.name)
            entries = manifest["entries"]
            if not entries:
                continue
        except (ValueError, OSError, KeyError, TypeError):
            continue
        entry = entries[0]
        remote = PureWindowsPath(entry["remote"])
        title = remote.parent.name if manifest["kind"] == "album" else remote.stem
        complete = manifest["complete"]
        if db is None and complete != len(entries):
            continue
        state = ("applied" if folder.name in applied else
                 "reviewed" if folder.name in prepared and complete == len(entries) else
                 "downloaded" if complete == len(entries) else "downloading")
        jobs.append({**{key: manifest[key] for key in ("id", "query", "kind", "created")},
                     "title": title or manifest["query"], "peer": entry["peer"],
                     "total": len(entries), "complete": complete, "state": state,
                     "original_track_id": associations.get(folder.name)})
    return sorted(jobs, key=lambda job: (job["created"], job["id"]), reverse=True)



def title_key(title):
    return "".join(character for character in unicodedata.normalize("NFKC", title).casefold()
                   if character.isalnum())


FEATURE = re.compile(r"\s+(?:[\[(]\s*)?(?:feat(?:uring)?|ft)\.?\s+", re.I)
VERSION = re.compile(r"\b(?:live|remix|instrumental|clean|explicit|censored|uncensored|acoustic|demo|alternate|"
                     r"radio edit|extended|remaster(?:ed)?|version|edit(?:ed)?|mono|stereo)\b", re.I)


def recording_credit(value):
    value = value or ""
    split = FEATURE.search(value)
    if not split:
        return title_key(value), ""
    base = value[:split.start()]
    guest = value[split.end():].rstrip(")] ")
    # Version modifiers anywhere in the suffix remain part of recording identity.
    if VERSION.search(guest):
        return title_key(value), ""
    return title_key(base), title_key(guest)


def recording_title(track):
    return recording_credit(track["title"])[0]


def same_credit(left, right):
    return left[0] and left[0] == right[0] and (not left[1] or not right[1] or left[1] == right[1])


def track_position(track, filename):
    tag = str(track.get("track_number") or "").split("/")[0]
    return int(tag) if tag.isdecimal() else filename_number(filename)



def filename_number(name):
    match = re.match(r"^(\d{1,3})[ ._-]+", Path(name).name)
    return int(match.group(1)) if match else None


ORIGINAL_FIELDS = "id,path,artist,album,title,codec,quality,duration,protected,track_number,disc_number"


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
            return [dict(row)]
        siblings = connection.execute(f"""
            SELECT root,{fields} FROM tracks WHERE root=? AND album=? COLLATE NOCASE AND present=1 AND error IS NULL
            ORDER BY path
        """, (row["root"], row["album"])).fetchall()
    folder = release_folder(row["path"])
    return [dict(item) for item in siblings if release_folder(item["path"]) == folder]


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


MATCH_ASSESSMENTS = {"possible match", "check track order", "check featured credits", "aligned recording"}


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
            or recording_credit(item["title"])[0] != recording_credit(original["title"])[0]
            or not recording_credit(item["artist"])[0]
            or recording_credit(item["artist"])[0] != recording_credit(original["artist"])[0]):
        return "check title or artist", original
    if (not original["duration"] or not item["duration"]
            or abs(original["duration"] - item["duration"]) > 2):
        return "check duration", original
    if (kind == "album" and (not original["album"] or not item["album"])
            or original["album"] and item["album"] and title_key(item["album"]) != title_key(original["album"])):
        return "check release tags", original
    # Explicit version cues in a source filename still matter when audio tags omit them.
    filename_variant = re.search(r"[\[(]([^]()]*)[\])]", Path(filename).stem)
    if filename_variant and VERSION.search(filename_variant[1]) and title_key(filename_variant[1]) not in title_key(original["title"]):
        return "check recording version", original
    guests = [{guest for _, guest in (recording_credit(track[field]) for field in ("title", "artist")) if guest}
              for track in (item, original)]
    if all(guests) and guests[0] != guests[1]:
        return "check featured credits", original
    position = track_position(item, filename)
    original_position = track_position(original, original["path"])
    if kind == "album" and position and original_position and position != original_position:
        return "check track order", original
    disc = str(item.get("disc_number") or "").split("/")[0]
    original_disc = str(original.get("disc_number") or "").split("/")[0]
    if kind == "album" and disc.isdecimal() and original_disc.isdecimal() and int(disc) != int(original_disc):
        return "check disc order", original
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
        key = (row["root"], release_folder(row["path"]))
        groups.setdefault(key, []).append(dict(row))
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


def cached_edition(db, candidate, items):
    audio = [item["audio"] for item in items if item.get("audio")]
    if len(audio) < 2 or len(audio) != len(items):
        return None
    first = audio[0]
    if not first.get("artist") or not first.get("album"):
        return None
    signature_key = json.dumps([(track.get("artist"), track.get("album"), track.get("title"),
                                 track.get("duration"), track_position(track, item["name"]))
                                for track, item in zip(audio, items)])
    with closing(connect(db)) as connection:
        cached = connection.execute(
            "SELECT evidence FROM edition_evidence WHERE candidate=? AND metadata_key=?",
            (candidate, signature_key)).fetchone()
    if cached:
        return music_edition_evidence(json.loads(cached["evidence"]))
    evidence = {"status": "No corroborating edition found", "releases": []}
    try:
        release_candidates = search_releases(db, first["artist"], first["album"], limit=3)["items"]
        if release_candidates:
            ordered = sorted(zip(audio, items), key=lambda pair: (
                str(pair[0].get("disc_number") or "1"), track_position(pair[0], pair[1]["name"]) or 0))
            for offer in release_candidates:
                edition = release_details(db, offer["id"])
                if (title_key(edition.get("title") or "") != title_key(first["album"])
                        or not any(same_credit(recording_credit(artist), recording_credit(first["artist"]))
                                   for artist in edition["artists"])):
                    continue
                listing = [track for track in edition["tracklist"] if track["type_"] == "track"]
                if len(listing) != len(ordered):
                    continue
                if all(edition_track_matches(track, item[0]) for track, item in zip(listing, ordered)):
                    evidence["releases"].append({key: edition[key] for key in ("id", "title", "year")})
            if evidence["releases"]:
                evidence["status"] = "Discogs tracklist corroborates title, order and available durations"
    except (OSError, ValueError):
        evidence["status"] = "Discogs lookup unavailable"
        # A transient lookup failure must not prevent later enrichment.
        return evidence
    with closing(connect(db)) as connection, connection:
        connection.execute("INSERT OR REPLACE INTO edition_evidence VALUES (?,?,?)",
                           (candidate, signature_key, json.dumps(evidence)))
    return evidence


def music_edition_evidence(evidence):
    if evidence:
        for release in evidence.get("releases", []):
            release.pop("url", None)
    return evidence


def edition_track_matches(edition, audio):
    if not same_credit(recording_credit(edition["title"]), recording_credit(audio["title"])):
        return False
    duration = re.fullmatch(r"(\d+):(\d{2})", edition["duration"] or "")
    return (not duration or not audio["duration"]
            or abs(int(duration[1]) * 60 + int(duration[2]) - audio["duration"]) <= 3)


def review_download(db, candidate_id=None, original_track_id=None, *, include_sources=False, research=False):
    selection_parts(candidate_id)
    selected_original_id = original_track_id
    with closing(connect(db)) as connection:
        kept = connection.execute("SELECT destination,mode FROM kept_albums WHERE candidate=?",
                                  (candidate_id,)).fetchone()
        saved = connection.execute("SELECT report FROM transfer_reviews WHERE candidate=?", (candidate_id,)).fetchone()
        if kept and saved:
            outcome = connection.execute("SELECT result FROM upgrade_results WHERE candidate=?", (candidate_id,)).fetchone()
            report = json.loads(saved["report"])
            report["kept_both"] = dict(kept)
            report["completion"] = json.loads(outcome["result"]) if outcome else None
            report["edition_evidence"] = music_edition_evidence(report.get("edition_evidence"))
            for item in report["items"]:
                item.pop("source", None)
                item.pop("fingerprint", None)
            return report
        if original_track_id is None and not kept:
            selected = connection.execute("""
                SELECT tracks.id FROM download_comparisons
                JOIN tracks ON tracks.id=download_comparisons.original_id
                WHERE candidate=? AND tracks.present=1 AND tracks.error IS NULL
            """, (candidate_id,)).fetchone()
            if selected:
                original_track_id = selected["id"]
    result = transfer_status(candidate_id)
    directory = staging_root() / candidate_id
    binary = find_probe() if result["complete"] else None
    report = public_status({key: value for key, value in result.items() if key != "entries"})
    indexed = originals(db, original_track_id, result["kind"])
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
        item["source"] = str(directory / transfer["file"])
        item["fingerprint"] = fingerprint
    if saved:
        report["edition_evidence"] = music_edition_evidence(json.loads(saved["report"]).get("edition_evidence"))
    if research and result["kind"] == "album" and len(report["items"]) == report["complete"]:
        report["edition_evidence"] = cached_edition(db, candidate_id, report["items"])
    suggested = original_track_id is None and result["kind"] == "album"
    if suggested:
        indexed = suggest_album(db, report["items"])
    explicit = next((track for track in indexed if track["id"] == original_track_id), None)
    comparison = explicit or (indexed[0] if indexed else None)
    report["comparison"] = ({"album": comparison["album"], "suggested": suggested,
                              "original_track_id": comparison["id"]} if comparison else None)
    tagged_albums = {title_key(item["audio"]["album"]) for item in report["items"]
                     if item.get("audio", {}).get("album")}
    selected_album = indexed[0]["album"] if indexed else None
    album_mismatch = (result["kind"] == "album" and selected_album and tagged_albums
                      and title_key(selected_album) not in tagged_albums)
    if album_mismatch:
        report["comparison"]["album_mismatch"] = True
    # Only one uniquely identified item may use the stronger alignment path.
    exact = [item for item in report["items"] if selected_original_id and explicit and item.get("audio")
             and recording_title(item["audio"]) == recording_title(explicit)]
    for item in report["items"]:
        if "audio" not in item:
            continue
        metadata = item["audio"]
        assessment, original = match_item(metadata, [explicit] if album_mismatch and explicit else indexed,
                                          result["kind"], item["name"])
        if album_mismatch:
            assessment = "check release tags"
        if (assessment in {"check duration", "check release tags"} and len(exact) == 1
                and item is exact[0] and metadata["quality"] == "lossless"
                and align_selected_recording(db, item, explicit)):
            assessment, original = "aligned recording", explicit
        item["assessment"] = assessment
        if original:
            item["original"] = original
            if assessment in MATCH_ASSESSMENTS:
                possible.add(original["id"])
    for original_id in possible:
        matches = [item for item in report["items"] if item.get("assessment") in MATCH_ASSESSMENTS
                   and item["original"]["id"] == original_id]
        if len(matches) > 1:
            for item in matches:
                item["assessment"] = "ambiguous duplicate recording"
    possible = {item["original"]["id"] for item in report["items"]
                if item.get("assessment") in {"possible match", "aligned recording"}}
    report["originals_without_match"] = [item for item in indexed if item["id"] not in possible]
    return save_review(db, report, include_sources)



def align_selected_recording(db, item, original):
    """Resolve a duration or edition discrepancy for one indexed selection."""
    audio = item["audio"]
    # Some taggers serialize multi-value artist credits with a slash.
    def artist_credit(track):
        lead, separator, guests = (track["artist"] or "").partition("/")
        return recording_credit(lead + (" feat. " + guests if separator else ""))
    if (original["protected"] or original["quality"] != "lossy"
            or not recording_title(original) or recording_title(original) != recording_title(audio)
            or not same_credit(artist_credit(original), artist_credit(audio))):
        return False
    if set(VERSION.findall(audio["title"].casefold())) != set(VERSION.findall(original["title"].casefold())):
        return False
    filename_variant = re.search(r"[\[(]([^]()]*)[\])]\s*$", Path(item["name"]).stem)
    if (filename_variant and VERSION.search(filename_variant[1])
            and title_key(filename_variant[1]) not in title_key(original["title"])):
        return False
    with closing(connect(db)) as connection:
        row = connection.execute("SELECT size,mtime,device,inode FROM tracks WHERE id=? AND present=1",
                                 (original["id"],)).fetchone()
    if row is None:
        return False
    try:
        evidence = compare_recordings(str(Path(original["root"]) / original["path"]), tuple(row),
                                      item["source"], tuple(item["fingerprint"]))
    except (OSError, ValueError) as exc:
        item["recording_evidence"] = {"matched": False, "reason": str(exc)}
        return False
    item["recording_evidence"] = {
        **evidence, "original_release": original["album"], "download_release": audio["album"],
    }
    return evidence["matched"]

def save_review(db, report, include_sources):
    if report["items"] and report["complete"] == len(report["items"]):
        with closing(connect(db)) as connection, connection:
            connection.execute("""
                INSERT INTO transfer_reviews(candidate,report) VALUES (?,?)
                ON CONFLICT(candidate) DO UPDATE SET report=excluded.report
            """, (report["id"], json.dumps(report)))
            comparison = report["comparison"]
            if comparison and not comparison.get("album_mismatch") and any(
                    item.get("assessment") in MATCH_ASSESSMENTS for item in report["items"]):
                connection.execute("""
                    INSERT INTO download_comparisons(candidate,original_id) VALUES (?,?)
                    ON CONFLICT(candidate) DO NOTHING
                """, (report["id"], comparison["original_track_id"]))
    if not include_sources:
        for item in report["items"]:
            item.pop("source", None)
            item.pop("fingerprint", None)
    return report
