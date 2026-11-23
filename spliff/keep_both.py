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
from contextlib import ExitStack, closing
from pathlib import Path

from .catalog import connect, file_identity, replacement_destination, signature
from .nicotine_bridge.downloads import selection_parts
from .nicotine_bridge.wire import staging_root
from .scanner import AUDIO_FIELDS
from .staged_downloads import MATCH_ASSESSMENTS, review_download
from .spectral import analyze_file, compare_measurements

WORK_DIR = ".spliff-keep-both"
MP3_FOLDER = "MP3"
HIDDEN_ARCHIVE = ".spliff-originals"


def path_fingerprint(path):
    if path.resolve(strict=True) != path:
        raise ValueError("Keep both requires files and folders with no symbolic links.")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("An audio file is no longer a regular file.")
    return signature(info)


def get_plan(db, candidate, original, mode="keep"):
    if mode not in ("keep", "replace"):
        raise ValueError("Choose Keep both or Replace.")
    if sys.platform != "darwin":
        raise ValueError("Keep both currently requires macOS.")
    review = review_download(db, candidate, original, include_sources=True)
    if review["kept_both"]:
        raise ValueError("This album has already been updated from this download.")
    items = review["items"]
    if not items or review["complete"] != len(items) or not review["comparison"]:
        raise ValueError("Wait until the download finishes and Spliff identifies the original release.")
    skipped = []
    matched = [item for item in items if item.get("assessment") in MATCH_ASSESSMENTS]
    skipped.extend({"download": item.get("name") or Path(item["source"]).name, "reason": item.get("assessment", "Audio inspection failed")}
                   for item in items if item not in matched)
    pairs = []
    root = folder = None
    from .artist import release_folder
    with closing(connect(db)) as connection:
        original_mode = replacement_destination(connection) if mode == "replace" else "keep"
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
                info = root.stat()
                if file_identity(info) != (track["root_device"], track["root_inode"]):
                    raise ValueError("The library folder changed. Scan it again before continuing.")
            if track_root != root or track_root / release_folder(track["path"]) != folder or not mp3.is_relative_to(root):
                raise ValueError("Select originals from a single album folder.")

            staged = Path(item["source"])
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
                          "assessment": item["assessment"]})
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
            if evidence["status"] in ("Different audio envelope", "Download appears silent"):
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
    if not ready:
        raise ValueError(skipped[-1]["reason"])
    installed_ids = {pair["id"] for pair in ready}
    preserved = {track["path"] for track in review["originals_without_match"]
                 if track["id"] not in installed_ids}
    preserved.update(item["original"]["path"] for item in items
                     if item.get("original") and item["original"]["id"] not in installed_ids)
    return {"candidate": candidate, "root": str(root), "folder": str(folder),
            "album": review["comparison"]["album"] or Path(ready[0]["mp3"]).stem,
            "mode": mode, "pairs": ready, "skipped": skipped,
            "edition_evidence": review.get("edition_evidence"),
            "preserved": sorted(preserved),
            "original_mode": original_mode,
            "original_destination": str(folder / archive_name) if archive_name else "Mac Trash"}


def plan_token(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()


def preview(db, candidate, original, mode="keep"):
    plan = get_plan(db, candidate, original, mode)
    destination = Path(plan["folder"])
    token = plan_token(plan)
    archive = plan["original_destination"]
    preview = {"token": token, "album": plan["album"], "destination": str(destination),
               "archive": archive, "count": len(plan["pairs"]), "skipped": plan["skipped"],
               "preserved": plan["preserved"],
               "files": [{"original": pair["mp3"], "install": str(destination / pair["relative"]),
                          "audio_evidence": pair["evidence"],
                          "archive": str(Path(archive) / Path(pair["mp3"]).relative_to(destination))
                          if plan["original_mode"] != "trash" else "Mac Trash"}
                         for pair in plan["pairs"]],
               "edition_evidence": plan["edition_evidence"],
               "audio_review": "Full decode and spectrum generated. Lossless provenance remains unknown."}
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


def move_exclusive(source, destination):
    # Darwin's RENAME_EXCL fails on an existing destination, including a racing writer.
    rename = ctypes.CDLL(None, use_errno=True).renamex_np
    rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if not rename(os.fsencode(source), os.fsencode(destination), 0x00000004):
        return destination
    error = ctypes.get_errno()
    if error not in (errno.ENOTSUP, errno.ENOSYS):
        raise OSError(error, os.strerror(error), str(destination))
    # Some Mac exFAT volumes cannot rename exclusively. Reserve the destination
    # with O_EXCL, then verify the copy before touching the source.
    copy_then_remove(source, destination)
    return destination


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


def copy_then_remove(source, destination):
    expected = path_fingerprint(source)
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as original, destination.open("xb+") as copy:
        identity = os.fstat(copy.fileno())
        digest = hashlib.sha256()
        try:
            if signature(os.fstat(original.fileno())) != expected:
                raise ValueError("The source file changed before copying.")
            for block in iter(lambda: original.read(1024 * 1024), b""):
                digest.update(block)
                copy.write(block)
            copy.flush()
            os.fsync(copy.fileno())
            copy.seek(0)
            verified = audio_digest(copy)
            now = destination.lstat()
            if (digest.digest() != verified or path_fingerprint(source) != expected
                    or signature(now) != signature(os.fstat(copy.fileno()))):
                raise ValueError("The copied audio could not be verified.")
            source_info = os.fstat(original.fileno())
            os.utime(destination, ns=(source_info.st_atime_ns, source_info.st_mtime_ns))
            os.fsync(copy.fileno())
            source.unlink()
        except BaseException:
            current = destination.lstat()
            if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                destination.unlink()
            raise


def copy_verified(source, destination, expected):
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as audio, destination.open("xb") as copy:
        if signature(os.fstat(audio.fileno())) != expected:
            raise ValueError("A staged file changed. Review the download again.")
        digest = hashlib.sha256()
        for block in iter(lambda: audio.read(1024 * 1024), b""):
            digest.update(block)
            copy.write(block)
        copy.flush()
        os.fsync(copy.fileno())
        if os.fstat(copy.fileno()).st_size != expected[0] or signature(os.fstat(audio.fileno())) != expected:
            raise ValueError("A staged file changed while copying.")
        return digest.digest()


def verify_published(path, expected, digest):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as audio:
        if (signature(os.fstat(audio.fileno())) != expected or audio_digest(audio) != digest
                or path_fingerprint(path) != expected):
            raise ValueError("The saved FLAC could not be verified.")


def remove_staged_copies(plan, published):
    removed = 0
    try:
        selection_parts(plan["candidate"])
        job = staging_root() / plan["candidate"]
        root = Path(plan["root"])
        if job.is_relative_to(root) or root.is_relative_to(job) or job.resolve(strict=True) != job:
            return removed
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        with ExitStack() as directories:
            job_fd = os.open(job, flags)
            directories.callback(os.close, job_fd)
            entries = []
            for pair, (final, final_sig) in zip(plan["pairs"], published):
                source = Path(pair["flac"])
                if source.parent.parent != job or not source.parent.name.isdigit():
                    return removed
                directory = os.open(source.parent.name, flags, dir_fd=job_fd)
                directories.callback(os.close, directory)
                if signature(os.stat(source.name, dir_fd=directory, follow_symlinks=False)) != pair["flac_sig"]:
                    return removed
                entries.append((directory, source.name, pair["flac_sig"], final, final_sig))
            for directory, name, source_sig, final, final_sig in entries:
                if (path_fingerprint(final) != final_sig
                        or signature(os.stat(name, dir_fd=directory, follow_symlinks=False)) != source_sig):
                    break
                os.unlink(name, dir_fd=directory)
                removed += 1
    except (OSError, ValueError):
        # The album is already committed. Leave the remaining duplicates for recovery.
        pass
    return removed


def clean_work(folder):
    def ignore_gone(_operation, _path, failure):
        # macOS can remove an exFAT AppleDouble sidecar with its primary file.
        if not isinstance(failure[1], FileNotFoundError):
            raise failure[1]

    shutil.rmtree(folder, onerror=ignore_gone)


def apply(db, candidate, original, token, mode="keep"):
    plan = get_plan(db, candidate, original, mode)
    with closing(connect(db)) as connection:
        issued = connection.execute("SELECT 1 FROM upgrade_plans WHERE token=? AND candidate=? AND mode=? AND original_id=?",
                                    (token, candidate, mode, original)).fetchone()
    if not issued or token != plan_token(plan):
        raise ValueError("The comparison changed. Review the destination again before confirming.")
    folder, root = Path(plan["folder"]), Path(plan["root"])
    work = folder / WORK_DIR
    original_mode = plan["original_mode"]
    archive = work / MP3_FOLDER if original_mode == "trash" else Path(plan["original_destination"])
    moved, published = [], []
    archive_created = False
    with closing(connect(db)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if mode == "replace" and replacement_destination(connection) != original_mode:
            raise ValueError("The replacement setting changed. Review the destinations again.")
        for pair in plan["pairs"]:
            row = connection.execute(
                "SELECT root,path,protected,codec,present,error FROM tracks WHERE id=?", (pair["id"],)
            ).fetchone()
            if not row or tuple(row) != (str(root), str(Path(pair["mp3"]).relative_to(root)), 0, pair["codec"], 1, None):
                raise ValueError("An original changed in the library. Review the destination again.")
        work.mkdir(mode=0o700)
        try:
            (work / "plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
            digests = []
            for pair in plan["pairs"]:
                temporary = work / "audio" / pair["relative"]
                temporary.parent.mkdir(parents=True, exist_ok=True)
                digest = copy_verified(Path(pair["flac"]), temporary, pair["flac_sig"])
                if digest.hex() != pair["download_digest"]:
                    raise ValueError("The downloaded audio changed after review.")
                digests.append(digest)
            for pair, digest in zip(plan["pairs"], digests):
                temporary = work / "audio" / pair["relative"]
                final = folder / pair["relative"]
                move_exclusive(temporary, final)
                fingerprint = path_fingerprint(final)
                published.append((final, fingerprint))
                verify_published(final, fingerprint, digest)
                columns = ("root", "path", *AUDIO_FIELDS, "size", "mtime", "device", "inode")
                connection.execute(
                    f"INSERT INTO tracks({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                    (str(root), str(final.relative_to(root)),
                     *(pair["audio"].get(key) for key in AUDIO_FIELDS), *fingerprint),
                )
            archive.mkdir()
            archive_created = True
            for pair in plan["pairs"]:
                source = Path(pair["mp3"])
                if verified_digest(source, pair["mp3_sig"]) != pair["original_digest"]:
                    raise ValueError("An original changed during the operation.")
                saved = archive / source.relative_to(folder)
                saved.parent.mkdir(parents=True, exist_ok=True)
                move_exclusive(source, saved)
                saved_sig = path_fingerprint(saved)
                moved.append((source, saved, saved_sig))
                connection.execute(
                    "UPDATE tracks SET path=?,size=?,mtime=?,device=?,inode=?,present=? WHERE id=?",
                    (str(saved.relative_to(root)), *saved_sig, int(mode == "keep"), pair["id"]),
                )
            connection.execute("INSERT INTO kept_albums(candidate,destination,mode) VALUES (?,?,?)",
                               (candidate, str(folder), mode))
            result = {"destination": str(folder), "archive": plan["original_destination"],
                      "count": len(plan["pairs"]), "upgraded": len(plan["pairs"]),
                      "preserved": len(plan["preserved"]) + (len(plan["pairs"]) if mode == "keep" else 0),
                      "skipped": plan["skipped"], "failed": 0,
                      "trash_pending": str(archive) if original_mode == "trash" else None,
                      "staging_removed": 0, "work_cleanup_pending": True}
            connection.execute("INSERT INTO upgrade_results VALUES (?,?)", (candidate, json.dumps(result)))
            connection.commit()
        except BaseException:
            connection.rollback()
            try:
                for final, fingerprint in reversed(published):
                    if path_fingerprint(final) == fingerprint:
                        final.unlink()
                    else:
                        raise ValueError("A copied FLAC changed before rollback.")
                for source, saved, fingerprint in reversed(moved):
                    if path_fingerprint(saved) != fingerprint:
                        raise ValueError("An archived MP3 changed before rollback.")
                    move_exclusive(saved, source)
                if archive_created:
                    for directory, _, _ in os.walk(archive, topdown=False):
                        Path(directory).rmdir()
            except (OSError, ValueError) as exc:
                raise ValueError(f"Keep both stopped. Check the recovery plan in {work} before retrying.") from exc
            clean_work(work)
            raise
    trash_pending = None
    if original_mode == "trash":
        try:
            subprocess.run(["/usr/bin/trash", str(archive)], check=True, capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            trash_pending = str(archive) if archive.exists() else None
        else:
            if archive.exists():
                trash_pending = str(archive)
    staging_removed = remove_staged_copies(plan, published)
    try:
        if not trash_pending:
            clean_work(work)
    except OSError:
        work_cleanup_pending = True
    else:
        work_cleanup_pending = bool(trash_pending)
    result.update(trash_pending=trash_pending, staging_removed=staging_removed,
                  work_cleanup_pending=work_cleanup_pending, failed=int(bool(trash_pending)))
    with closing(connect(db)) as connection, connection:
        connection.execute("UPDATE upgrade_results SET result=? WHERE candidate=?",
                           (json.dumps(result), candidate))
    return result
