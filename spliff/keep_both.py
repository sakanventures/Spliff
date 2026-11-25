"""Preview and explicitly apply a non-overwriting MP3 archive and FLAC import."""
import ctypes
import errno
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import unicodedata
from contextlib import closing, contextmanager
from pathlib import Path

from .catalog import connect, file_identity, replacement_destination, signature
from .nicotine_bridge.downloads import selection_parts
from .nicotine_bridge.wire import staging_root
from .scanner import AUDIO_FIELDS
from .recording_match import compare_recordings
from .staged_downloads import MATCH_ASSESSMENTS, review_download
from .spectral import analyze_file, compare_measurements

WORK_DIR = ".spliff-keep-both"
MP3_FOLDER = "MP3"
HIDDEN_ARCHIVE = ".spliff-originals"
TRASH = "/usr/bin/trash"
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
RECEIPT_NAME = "job.json"


class RecordingReviewBlocked(ValueError):
    def __init__(self, skipped):
        super().__init__(skipped[-1]["reason"])
        self.skipped = skipped


@contextmanager
def open_directory(path, identities):
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Upgrade paths must stay inside the reviewed folders.")
    current = Path(path.anchor)
    descriptor = os.open(current, DIRECTORY_FLAGS)
    try:
        if str(current) in identities and file_identity(os.fstat(descriptor)) != identities[str(current)]:
            raise ValueError(f"The reviewed folder or volume changed: {current}")
        for part in path.parts[1:]:
            child = os.open(part, DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            current /= part
            expected = identities.get(str(current))
            if expected is not None and file_identity(os.fstat(descriptor)) != expected:
                raise ValueError(f"The reviewed folder or volume changed: {current}")
        yield descriptor
    finally:
        os.close(descriptor)


def remember_directory(path, identities):
    with open_directory(path, identities) as descriptor:
        identities[str(path)] = file_identity(os.fstat(descriptor))


def create_directory(path, identities):
    with open_directory(path.parent, identities) as parent:
        os.mkdir(path.name, mode=0o700, dir_fd=parent)
        descriptor = os.open(path.name, DIRECTORY_FLAGS, dir_fd=parent)
        try:
            identities[str(path)] = file_identity(os.fstat(descriptor))
        finally:
            os.close(descriptor)
        os.fsync(parent)


def file_signature(directory, name):
    info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("An upgrade file is no longer a regular file.")
    return signature(info)


def unlink_verified(path, expected, identities):
    with open_directory(path.parent, identities) as directory:
        if file_signature(directory, path.name) != expected:
            raise ValueError(f"An upgrade file changed before cleanup: {path}")
        os.unlink(path.name, dir_fd=directory)


def remove_empty_directory(path, identities):
    with open_directory(path.parent, identities) as parent:
        info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or file_identity(info) != identities[str(path)]:
            raise ValueError(f"An upgrade folder changed before cleanup: {path}")
        os.rmdir(path.name, dir_fd=parent)


def path_fingerprint(path):
    if path.resolve(strict=True) != path:
        raise ValueError("Keep both requires files and folders with no symbolic links.")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("An audio file is no longer a regular file.")
    return signature(info)


def staging_receipt_signature(job, pairs, identities):
    expected = {Path(pair["flac"]).parent.name: pair["flac_sig"][0] for pair in pairs}
    try:
        with open_directory(job, identities) as directory:
            descriptor = os.open(RECEIPT_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(descriptor, "rb") as receipt:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
                    return None
                fingerprint = signature(info)
                data = json.loads(receipt.read(1024 * 1024 + 1))
            entries = data["entries"]
            if (data["id"] == job.name and len(entries) == len(pairs)
                    and {entry["directory"]: entry["bytes"] for entry in entries} == expected
                    and file_signature(directory, RECEIPT_NAME) == fingerprint):
                return fingerprint
    except (OSError, ValueError, KeyError, TypeError):
        pass


def get_plan(db, candidate, original, mode="keep", *, strict=False, overrides=()):
    if mode not in ("keep", "replace"):
        raise ValueError("Choose Keep both or Replace.")
    if sys.platform != "darwin":
        raise ValueError("Keep both currently requires macOS.")
    selection_parts(candidate)
    job = staging_root() / candidate
    review = review_download(db, candidate, original, include_sources=True)
    if review["kept_both"]:
        raise ValueError("This album has already been updated from this download.")
    items = review["items"]
    if not items or review["complete"] != len(items) or not review["comparison"]:
        raise ValueError("Wait until the download finishes and Spliff identifies the original release.")
    skipped = []
    matched = [item for item in items if item.get("assessment") in MATCH_ASSESSMENTS or (
        strict and item.get("assessment") in {"check duration", "check release tags"}
        and item.get("original") and item["audio"]["quality"] == "lossless")]
    if strict:
        counts = {}
        for item in items:
            if item.get("original"):
                key = item["original"]["id"]
                counts[key] = counts.get(key, 0) + 1
        matched = [item for item in matched if counts[item["original"]["id"]] == 1]
    skipped.extend({"download": item.get("name") or Path(item["source"]).name, "reason": item.get("assessment", "Audio inspection failed")}
                   for item in items if item not in matched)
    pairs = []
    root = folder = None
    identities = {}
    from .artist import release_folder
    with closing(connect(db)) as connection:
        original_mode = replacement_destination(connection) if mode == "replace" else "keep"
        if original_mode == "trash" and not os.access(TRASH, os.X_OK):
            raise ValueError("Mac Trash is unavailable. Choose the hidden recovery folder or Keep both.")
        sync_folders = [Path(row["root"]) for row in connection.execute(
            "SELECT root FROM sync_targets WHERE candidate=?", (candidate,))]
        for item in matched:
            track = connection.execute("""
                SELECT t.*, r.device AS root_device, r.inode AS root_inode
                FROM tracks t JOIN roots r ON r.path=t.root
                WHERE t.id=? AND t.present=1 AND t.error IS NULL
            """, (item["original"]["id"],)).fetchone()
            if not track or track["quality"] != "lossy" or track["protected"]:
                skipped.append({"download": item.get("name") or Path(item["source"]).name, "reason": "Original is unavailable, protected, or already lossless"})
                continue
            track_root = Path(track["root"])
            mp3 = track_root / track["path"]
            if folder is None:
                root, folder = track_root, track_root / release_folder(track["path"])
                identities[str(root)] = (track["root_device"], track["root_inode"])
                remember_directory(folder, identities)
            if (track_root != root or track_root / release_folder(track["path"]) != folder
                    or not mp3.is_relative_to(folder) or ".." in mp3.parts):
                raise ValueError("Select originals from a single album folder.")

            staged = Path(item["source"])
            if (job.is_relative_to(root) or root.is_relative_to(job)
                    or staged.parent.parent != job or not staged.parent.name.isdigit()
                    or ".." in staged.parts):
                raise ValueError("The download must be inside Spliff's separate staging folder.")
            for directory in (mp3.parent, job, staged.parent):
                remember_directory(directory, identities)
            mp3_sig = tuple(track[key] for key in ("size", "mtime", "device", "inode"))
            if any(not folder.is_relative_to(selected) or not mp3.is_relative_to(selected)
                   for selected in sync_folders):
                raise ValueError("The upgrade or recovery destination is outside the selected sync folder.")
            if path_fingerprint(mp3) != mp3_sig or path_fingerprint(staged) != tuple(item["fingerprint"]):
                raise ValueError("An audio file changed. Scan or review it again.")
            suffix = staged.suffix.lower()
            allowed = {"flac", "wav", "wave", "aif", "aiff", "alac", "m4a", "ape", "wv", "dsf"}
            if suffix.lstrip(".") not in allowed:
                skipped.append({"download": item.get("name") or Path(item["source"]).name, "reason": "Unsupported lossless file extension"})
                continue
            final = mp3.with_suffix(suffix)
            if final == mp3:
                final = mp3.with_name(f"{mp3.stem} (Lossless){suffix}")
            pairs.append({"id": track["id"], "mp3": str(mp3), "mp3_sig": mp3_sig, "codec": track["codec"],
                          "flac": str(staged), "flac_sig": tuple(item["fingerprint"]), "name": final.name,
                          "relative": str(final.relative_to(folder)), "audio": item["audio"],
                          "assessment": item["assessment"], "recording_evidence": item.get("recording_evidence")})
    if not pairs:
        raise ValueError("No confident, unprotected lossless upgrades are ready to apply.")
    if folder.name.casefold() in {"mp3", ".spliff-originals"}:
        raise ValueError("Choose the main album folder instead of an MP3 archive.")

    def name_key(name):
        return unicodedata.normalize("NFC", name).casefold()

    names = {name_key(path.name) for path in folder.iterdir()}
    available = []
    destinations = set()
    for pair in pairs:
        final = folder / pair["relative"]
        key = name_key(pair["relative"])
        if key in destinations or any(name_key(path.name) == name_key(final.name)
                                      for path in final.parent.iterdir()):
            skipped.append({"download": Path(pair["flac"]).name, "reason": "Destination already exists"})
            continue
        destinations.add(key)
        available.append(pair)
    pairs = available
    if not pairs:
        raise ValueError("A lossless file with the same name already exists in the album folder.")
    archive_name = {"keep": MP3_FOLDER, "hidden": HIDDEN_ARCHIVE}.get(original_mode)
    if archive_name and name_key(archive_name) in names:
        archive_name = str(Path(archive_name) / candidate)
        archive_path = folder / archive_name
        if archive_path.parent.resolve() != archive_path.parent or not archive_path.parent.is_dir() or archive_path.exists():
            raise ValueError("The originals destination conflicts with an existing file.")
        remember_directory(archive_path.parent, identities)
    if name_key(WORK_DIR) in names:
        raise ValueError("A previous operation needs attention. Check the album folder before retrying.")
    copies = [pair["flac_sig"][0] for pair in pairs]
    largest = max(max(copies), *(pair["mp3_sig"][0] for pair in pairs))
    if sum(copies) + largest > shutil.disk_usage(folder).free:
        raise ValueError("There is not enough free space to copy the FLAC album.")
    # Decode and render every pair before offering confirmation, and revalidate on apply.
    # This establishes readable spectral evidence, never an authenticity verdict.
    ready = []
    for pair in pairs:
        try:
            original = analyze_file(pair["mp3"], pair["mp3_sig"])
            download = analyze_file(pair["flac"], pair["flac_sig"])
            evidence = compare_measurements(original, download)
            if evidence["status"] == "Download appears silent":
                raise ValueError(evidence["status"])
            if strict:
                try:
                    recording = compare_recordings(pair["mp3"], pair["mp3_sig"], pair["flac"], pair["flac_sig"])
                except ValueError as exc:
                    recording = {"matched": False, "reason": str(exc)}
                verified = recording["matched"] and evidence["status"] != "Different audio envelope"
                overridden = not verified and pair["id"] in overrides
                if not verified and not overridden:
                    skipped.append({"download": Path(pair["flac"]).name, "original_track_id": pair["id"],
                                    "reason": "Recording identity is uncertain or conflicting",
                                    "recording_evidence": recording, "audio_evidence": evidence})
                    continue
                pair["recording_evidence"] = {**recording, "confidence": "verified match" if verified else
                                              "uncertain match", "overridden": overridden}
            else:
                if evidence["status"] == "Different audio envelope":
                    raise ValueError(evidence["status"])
                if pair["assessment"] != "possible match" and evidence["status"] != "Similar audio envelope":
                    raise ValueError(pair["assessment"].capitalize() + ": audio did not resolve the discrepancy.")
            pair["evidence"] = evidence
            pair["original_digest"] = verified_digest(Path(pair["mp3"]), pair["mp3_sig"])
            pair["download_digest"] = verified_digest(Path(pair["flac"]), pair["flac_sig"])
        except (OSError, ValueError) as exc:
            skipped.append({"download": Path(pair["flac"]).name, "reason": str(exc)})
        else:
            ready.append(pair)
    if overrides and set(overrides) != {pair["id"] for pair in ready
                                        if pair["recording_evidence"].get("overridden")}:
        raise ValueError("Every exception must select an eligible track with unresolved recording evidence.")
    if not ready:
        raise RecordingReviewBlocked(skipped)
    receipt_signature = staging_receipt_signature(job, ready, identities) if len(ready) == len(items) else None
    installed_ids = {pair["id"] for pair in ready}
    preserved = {track["path"] for track in review["originals_without_match"]
                 if track["id"] not in installed_ids}
    preserved.update(item["original"]["path"] for item in items
                     if item.get("original") and item["original"]["id"] not in installed_ids)
    return {"candidate": candidate, "root": str(root), "folder": str(folder), "directories": identities,
            "staging": str(job), "receipt_signature": receipt_signature,
            "album": review["comparison"]["album"] or Path(ready[0]["mp3"]).stem,
            "mode": mode, "pairs": ready, "skipped": skipped,
            "edition_evidence": review.get("edition_evidence"),
            "preserved": sorted(preserved),
            "original_mode": original_mode,
            "original_destination": str(folder / archive_name) if archive_name else "Mac Trash"}


def plan_token(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()


def preview(db, candidate, original, mode="keep", *, strict=False, overrides=()):
    plan = get_plan(db, candidate, original, mode, strict=strict, overrides=overrides)
    destination = Path(plan["folder"])
    token = plan_token(plan)
    archive = plan["original_destination"]
    preview = {"token": token, "album": plan["album"], "destination": str(destination),
               "archive": archive, "count": len(plan["pairs"]), "skipped": plan["skipped"],
               "preserved": plan["preserved"],
               "files": [{"original": pair["mp3"], "install": str(destination / pair["relative"]),
                          "audio_evidence": pair["evidence"], "recording_evidence": pair["recording_evidence"],
                          "archive": str(Path(archive) / Path(pair["mp3"]).relative_to(destination))
                          if plan["original_mode"] != "trash" else "Mac Trash"}
                         for pair in plan["pairs"]],
               "edition_evidence": plan["edition_evidence"],
               "audio_review": "Full decode and spectrum generated. Lossless provenance remains unknown."}
    if strict:
        return preview
    with closing(connect(db)) as connection, connection:
        connection.execute("DELETE FROM upgrade_plans WHERE candidate=? AND mode=? AND original_id=?",
                           (candidate, mode, original))
        connection.execute("INSERT INTO upgrade_plans VALUES (?,?,?,?,?)",
                           (token, candidate, mode, original, json.dumps(preview)))
    return preview


def review_plans(db, candidate, original, *, refresh=False):
    plans, blocked = {}, {}
    if not refresh:
        with closing(connect(db)) as connection:
            plans = {row["mode"]: json.loads(row["preview"]) for row in connection.execute(
                "SELECT preview,mode FROM upgrade_plans WHERE candidate=? AND original_id=?",
                (candidate, original))}

    for mode in ("keep", "replace"):
        if mode in plans:
            continue
        try:
            plans[mode] = preview(db, candidate, original, mode)
        except (OSError, ValueError) as exc:
            blocked[mode] = str(exc)
    return {"options": plans, "blocked": blocked}


def move_exclusive(source, destination, *, identities, expected):
    # RENAME_EXCL fails on an existing destination, including a racing writer.
    rename = ctypes.CDLL(None, use_errno=True).renameatx_np
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    with open_directory(source.parent, identities) as source_dir, \
            open_directory(destination.parent, identities) as destination_dir:
        if file_signature(source_dir, source.name) != expected:
            raise ValueError("An upgrade file changed before moving.")
        if not rename(source_dir, os.fsencode(source.name), destination_dir,
                      os.fsencode(destination.name), 0x00000004):
            if file_signature(destination_dir, destination.name) != expected:
                raise ValueError("An upgrade file changed during the move.")
            return expected
        error = ctypes.get_errno()
        if error not in (errno.ENOTSUP, errno.ENOSYS, errno.EXDEV):
            raise OSError(error, os.strerror(error), str(destination))
        # Some exFAT volumes lack exclusive rename. Use exclusive creation
        # and verify the copy before touching the source.
        return copy_then_remove(source_dir, source.name, destination_dir, destination.name, expected)


def audio_digest(audio):
    digest = hashlib.sha256()
    for block in iter(lambda: audio.read(1024 * 1024), b""):
        digest.update(block)
    return digest.digest()


def verified_digest(path, expected):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        if signature(os.fstat(source.fileno())) != expected:
            raise ValueError("An audio file changed before verification.")
        digest = audio_digest(source).hex()
        if signature(os.fstat(source.fileno())) != expected or path_fingerprint(path) != expected:
            raise ValueError("An audio file changed during verification.")
    return digest


def copy_then_remove(source_dir, source, destination_dir, destination, expected):
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=source_dir)
    with os.fdopen(descriptor, "rb") as original:
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        output = os.open(destination, flags, 0o600, dir_fd=destination_dir)
        with os.fdopen(output, "wb+") as copy:
            identity = file_identity(os.fstat(output))
            digest = hashlib.sha256()
            try:
                if signature(os.fstat(descriptor)) != expected:
                    raise ValueError("The source file changed before copying.")
                for block in iter(lambda: original.read(1024 * 1024), b""):
                    digest.update(block)
                    copy.write(block)
                copy.flush()
                copy.seek(0)
                if digest.digest() != audio_digest(copy) or signature(os.fstat(descriptor)) != expected:
                    raise ValueError("The copied audio could not be verified.")
                source_info = os.fstat(descriptor)
                os.utime(output, ns=(source_info.st_atime_ns, source_info.st_mtime_ns))
                os.fsync(output)
                os.fsync(destination_dir)
                saved = signature(os.fstat(output))
                if (file_signature(source_dir, source) != expected
                        or file_signature(destination_dir, destination) != saved):
                    raise ValueError("An upgrade file changed before removing its duplicate.")
                os.unlink(source, dir_fd=source_dir)
            except BaseException:
                # The original is still present unless removal completed.
                # Retain the copy if either name changed or the source is missing.
                if (file_signature(source_dir, source) == expected
                        and file_signature(destination_dir, destination)[2:] == identity):
                    os.unlink(destination, dir_fd=destination_dir)
                raise
            return saved


def copy_verified(source, destination, expected, identities):
    with open_directory(source.parent, identities) as source_dir, \
            open_directory(destination.parent, identities) as destination_dir:
        descriptor = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=source_dir)
        with os.fdopen(descriptor, "rb") as audio:
            if signature(os.fstat(descriptor)) != expected:
                raise ValueError("A staged file changed. Review the download again.")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            output = os.open(destination.name, flags, 0o600, dir_fd=destination_dir)
            with os.fdopen(output, "wb") as copy:
                digest = hashlib.sha256()
                for block in iter(lambda: audio.read(1024 * 1024), b""):
                    digest.update(block)
                    copy.write(block)
                copy.flush()
                os.fsync(output)
                saved = signature(os.fstat(output))
                if saved[0] != expected[0] or signature(os.fstat(descriptor)) != expected:
                    raise ValueError("A staged file changed while copying.")
            return digest.digest(), saved


def verify_published(path, expected, digest):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as audio:
        if (signature(os.fstat(audio.fileno())) != expected or audio_digest(audio) != digest
                or path_fingerprint(path) != expected):
            raise ValueError("The saved FLAC could not be verified.")


def remove_staged_copies(plan, published):
    job = Path(plan["staging"])
    identities = plan["directories"]
    removed = 0
    for pair, (final, final_sig) in zip(plan["pairs"], published):
        source = Path(pair["flac"])
        try:
            with open_directory(final.parent, identities) as directory:
                if file_signature(directory, final.name) != final_sig:
                    continue
            unlink_verified(source, pair["flac_sig"], identities)
        except (OSError, ValueError):
            continue
        removed += 1
        try:
            remove_empty_directory(source.parent, identities)
        except (OSError, ValueError):
            # Only an empty, reviewed transfer directory can be removed.
            pass
    receipt = plan["receipt_signature"]
    if removed != len(plan["pairs"]) or receipt is None:
        return removed, False
    try:
        with open_directory(job, identities) as directory:
            if set(os.listdir(directory)) != {RECEIPT_NAME}:
                return removed, False
        unlink_verified(job / RECEIPT_NAME, receipt, identities)
        remove_empty_directory(job, identities)
    except (OSError, ValueError):
        return removed, False
    return removed, True


def clean_work(work, identities, directories, copies, journal_sig):
    for path, fingerprint in copies.items():
        unlink_verified(path, fingerprint, identities)
    for path in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        remove_empty_directory(path, identities)
    with open_directory(work, identities) as directory:
        if journal_sig is None or set(os.listdir(directory)) != {"plan.json"}:
            raise ValueError("The recovery folder needs inspection.")
    unlink_verified(work / "plan.json", journal_sig, identities)
    remove_empty_directory(work, identities)


def write_recovery_plan(work, archive, plan, identities):
    folder = Path(plan["folder"])
    manifest = {**plan, "original_recovery_folder": str(archive), "pairs": [
        {**pair, "installed": str(folder / pair["relative"]),
         "recovery": str(archive / Path(pair["mp3"]).relative_to(folder))}
        for pair in plan["pairs"]
    ]}
    return write_journal(work, manifest, identities)


def write_journal(work, manifest, identities):
    with open_directory(work, identities) as directory:
        output = os.open("plan.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
        with os.fdopen(output, "w", encoding="utf-8") as journal:
            json.dump(manifest, journal, indent=2)
            journal.flush()
            os.fsync(output)
            fingerprint = signature(os.fstat(output))
        os.fsync(directory)
    return fingerprint


def ensure_upgrade_directory(path, base, identities, created):
    for directory in reversed((path, *path.parents)):
        if directory.is_relative_to(base) and directory not in created:
            create_directory(directory, identities)
            created.add(directory)


def apply(db, candidate, original, token, mode="keep", *, receipt=None, trusted_review=False, overrides=()):
    plan = get_plan(db, candidate, original, mode, strict=trusted_review, overrides=overrides)
    with closing(connect(db)) as connection:
        issued = connection.execute("SELECT 1 FROM upgrade_plans WHERE token=? AND candidate=? AND mode=? AND original_id=?",
                                    (token, candidate, mode, original)).fetchone()
    if (not issued and not trusted_review) or token != plan_token(plan):
        raise ValueError("The comparison changed. Review the destination again before confirming.")
    folder, root = Path(plan["folder"]), Path(plan["root"])
    work = folder / WORK_DIR
    original_mode = plan["original_mode"]
    archive = work / MP3_FOLDER if original_mode == "trash" else Path(plan["original_destination"])
    identities = dict(plan["directories"])
    moved, published = [], []
    work_dirs, archive_dirs = set(), set()
    copies = {}
    journal_sig = None
    with closing(connect(db)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM kept_albums WHERE candidate=?", (candidate,)).fetchone():
            raise ValueError("This download already has a committed library action.")
        if mode == "replace" and replacement_destination(connection) != original_mode:
            raise ValueError("The replacement setting changed. Review the destinations again.")
        for pair in plan["pairs"]:
            row = connection.execute(
                "SELECT root,path,protected,codec,present,error FROM tracks WHERE id=?", (pair["id"],)
            ).fetchone()
            if not row or tuple(row) != (str(root), str(Path(pair["mp3"]).relative_to(root)), 0, pair["codec"], 1, None):
                raise ValueError("An original changed in the library. Review the destination again.")
        create_directory(work, identities)
        try:
            journal_sig = write_recovery_plan(work, archive, plan, identities)
            digests = []
            for pair in plan["pairs"]:
                temporary = work / "audio" / pair["relative"]
                ensure_upgrade_directory(temporary.parent, work / "audio", identities, work_dirs)
                digest, copies[temporary] = copy_verified(
                    Path(pair["flac"]), temporary, pair["flac_sig"], identities)
                if digest.hex() != pair["download_digest"]:
                    raise ValueError("The downloaded audio changed after review.")
                digests.append(digest)
            for pair, digest in zip(plan["pairs"], digests):
                temporary = work / "audio" / pair["relative"]
                final = folder / pair["relative"]
                fingerprint = move_exclusive(temporary, final, identities=identities,
                                             expected=copies[temporary])
                del copies[temporary]
                published.append((final, fingerprint))
                verify_published(final, fingerprint, digest)
                if pair["recording_evidence"]:
                    analyze_file(final, fingerprint)
                columns = ("root", "path", *AUDIO_FIELDS, "size", "mtime", "device", "inode")
                connection.execute(
                    f"INSERT INTO tracks({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                    (str(root), str(final.relative_to(root)),
                     *(pair["audio"].get(key) for key in AUDIO_FIELDS), *fingerprint),
                )
            for pair, (final, final_sig) in zip(plan["pairs"], published):
                if path_fingerprint(final) != final_sig:
                    raise ValueError("An installed lossless file changed before archiving its original.")
                source = Path(pair["mp3"])
                if verified_digest(source, pair["mp3_sig"]) != pair["original_digest"]:
                    raise ValueError("An original changed during the operation.")
                saved = archive / source.relative_to(folder)
                ensure_upgrade_directory(saved.parent, archive, identities, archive_dirs)
                saved_sig = move_exclusive(source, saved, identities=identities,
                                           expected=pair["mp3_sig"])
                moved.append((source, saved, saved_sig))
                if verified_digest(saved, saved_sig) != pair["original_digest"]:
                    raise ValueError("The preserved MP3 failed verification.")
                connection.execute(
                    "UPDATE tracks SET path=?,size=?,mtime=?,device=?,inode=?,present=? WHERE id=?",
                    (str(saved.relative_to(root)), *saved_sig, int(mode == "keep"), pair["id"]),
                )
            changed_dirs = {path.parent for source, saved, _ in moved for path in (source, saved)}
            changed_dirs.update(directory.parent for directory in archive_dirs)
            for directory in sorted(changed_dirs):
                with open_directory(directory, identities) as descriptor:
                    os.fsync(descriptor)
            connection.execute("INSERT INTO kept_albums(candidate,destination,mode) VALUES (?,?,?)",
                               (candidate, str(folder), mode))
            result = {"destination": str(folder), "archive": plan["original_destination"],
                      "count": len(plan["pairs"]), "upgraded": len(plan["pairs"]),
                      "preserved": len(plan["preserved"]) + (len(plan["pairs"]) if mode == "keep" else 0),
                      "skipped": plan["skipped"], "failed": int(original_mode == "trash"),
                      "trash_pending": str(archive) if original_mode == "trash" else None,
                      "staging_removed": 0, "staging_cleared": False, "staging_path": plan["staging"],
                      "work_cleanup_pending": True, "recovery_path": str(work)}
            result.update(receipt or {})
            if trusted_review and original_mode == "hidden":
                recovery = {"root": str(root), "folder": str(folder), "directories": identities,
                            "candidate": candidate, "transaction_id": result["transaction_id"], "files": []}
                for (source, saved, saved_sig), (final, final_sig), pair in zip(moved, published, plan["pairs"]):
                    recovery["files"].append({"original": str(source), "archive": str(saved),
                                              "install": str(final), "id": pair["id"],
                                              "original_signature": saved_sig, "installed_signature": final_sig,
                                              "original_digest": pair["original_digest"],
                                              "installed_digest": pair["download_digest"]})
                connection.execute("INSERT INTO upgrade_recovery VALUES (?,?)",
                                   (candidate, json.dumps(recovery)))
                result["rollback_available"] = True
            connection.execute("INSERT INTO upgrade_results VALUES (?,?)", (candidate, json.dumps(result)))
            connection.commit()
        except BaseException:
            if not connection.in_transaction:
                raise ValueError(f"Check the recovery plan in {work} before retrying.")
            connection.rollback()
            try:
                for source, saved, fingerprint in reversed(moved):
                    move_exclusive(saved, source, identities=identities, expected=fingerprint)
                for final, fingerprint in reversed(published):
                    unlink_verified(final, fingerprint, identities)
                for directory in sorted(archive_dirs, key=lambda path: len(path.parts), reverse=True):
                    remove_empty_directory(directory, identities)
                clean_work(work, identities, work_dirs, copies, journal_sig)
            except (OSError, ValueError) as exc:
                raise ValueError(f"The upgrade stopped. Check the recovery plan in {work} before retrying.") from exc
            raise
    trash_pending = None
    if original_mode == "trash":
        try:
            trash_originals(moved, published, identities)
        except (OSError, ValueError, subprocess.SubprocessError):
            trash_pending = str(archive)
    staging_removed, staging_cleared = (0, False) if trash_pending else remove_staged_copies(plan, published)
    work_cleanup_pending = True
    if not trash_pending:
        try:
            if original_mode == "trash":
                work_dirs.update(archive_dirs)
            clean_work(work, identities, work_dirs, copies, journal_sig)
        except (OSError, ValueError):
            pass
        else:
            work_cleanup_pending = False
    result.update(trash_pending=trash_pending, staging_removed=staging_removed,
                  staging_cleared=staging_cleared, staging_path=None if staging_cleared else plan["staging"],
                  work_cleanup_pending=work_cleanup_pending, failed=int(bool(trash_pending)),
                  recovery_path=str(work) if work_cleanup_pending else None)
    with closing(connect(db)) as connection, connection:
        connection.execute("UPDATE upgrade_results SET result=? WHERE candidate=?",
                           (json.dumps(result), candidate))
    return result


def trash_originals(moved, published, identities):
    for (_, saved, saved_sig), (final, final_sig) in zip(moved, published):
        if path_fingerprint(final) != final_sig:
            raise ValueError("The installed lossless file changed before moving its original to Trash.")
        # Trash accepts paths. Revalidate the private recovery directory and
        # pass one verified regular file at a time.
        with open_directory(saved.parent, identities) as directory:
            if file_signature(directory, saved.name) != saved_sig:
                raise ValueError("An original changed before moving to Trash.")
            subprocess.run([TRASH, str(saved)], check=True, capture_output=True, timeout=30)
            try:
                os.stat(saved.name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise ValueError("An original is still in the recovery folder.")
