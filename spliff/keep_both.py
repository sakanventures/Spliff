"""Preview and explicitly apply a non-overwriting MP3 archive and FLAC import."""
import ctypes
import errno
import hashlib
import json
import os
import shutil
import stat
import sys
import unicodedata
from contextlib import closing
from pathlib import Path

from .catalog import connect, signature
from .scanner import AUDIO_FIELDS
from .staged_downloads import review_download

WORK_DIR = ".spliff-keep-both"


def path_fingerprint(path):
    if path.resolve(strict=True) != path:
        raise ValueError("Keep both requires files and folders with no symbolic links.")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("An audio file is no longer a regular file.")
    return signature(info)


def get_plan(db, candidate, original):
    if sys.platform != "darwin":
        raise ValueError("Keep both currently requires macOS.")
    review = review_download(db, candidate, original, include_sources=True)
    if review["kept_both"]:
        raise ValueError("Both versions have already been saved for this download.")
    items = review["items"]
    if (review["kind"] != "album" or not items or review["complete"] != len(items)
            or review["originals_without_match"]
            or any(item.get("assessment") != "possible match"
                   or item.get("audio", {}).get("codec") != "flac" for item in items)):
        raise ValueError("Keep both requires a complete FLAC album with one metadata match per original.")
    pairs = []
    root = folder = None
    with closing(connect(db)) as connection:
        for item in items:
            track = connection.execute("""
                SELECT t.*, r.device AS root_device, r.inode AS root_inode
                FROM tracks t JOIN roots r ON r.path=t.root
                WHERE t.id=? AND t.present=1 AND t.error IS NULL
            """, (item["original"]["id"],)).fetchone()
            if not track or track["codec"] != "mp3" or track["protected"]:
                raise ValueError("Keep both requires unprotected MP3 originals. Protected files stay in place.")
            track_root = Path(track["root"])
            mp3 = track_root / track["path"]
            if folder is None:
                root, folder = track_root, mp3.parent
                info = root.stat()
                if (info.st_dev, info.st_ino) != (track["root_device"], track["root_inode"]):
                    raise ValueError("The library folder changed. Scan it again before continuing.")
            if track_root != root or mp3.parent != folder or not mp3.is_relative_to(root):
                raise ValueError("Select originals from a single album folder.")
            if mp3.suffix.lower() != ".mp3":
                raise ValueError("The original file must have an MP3 extension.")
            staged = Path(item["source"])
            mp3_sig = tuple(track[key] for key in ("size", "mtime", "device", "inode"))
            if path_fingerprint(mp3) != mp3_sig or path_fingerprint(staged) != item["fingerprint"]:
                raise ValueError("An audio file changed. Scan or review it again.")
            pairs.append({"id": track["id"], "mp3": str(mp3), "mp3_sig": mp3_sig,
                          "flac": str(staged), "flac_sig": item["fingerprint"],
                          "name": mp3.with_suffix(".flac").name, "audio": item["audio"]})
    assert folder is not None
    if folder.name.casefold() == "mp3":
        raise ValueError("Choose the main album folder instead of an MP3 archive.")

    def name_key(name):
        return unicodedata.normalize("NFC", name).casefold()

    names = {name_key(path.name) for path in folder.iterdir()}
    destination_names = {name_key(pair["name"]) for pair in pairs}
    if len(destination_names) != len(pairs) or names & destination_names:
        raise ValueError("A FLAC with the same name already exists in the album folder.")
    if "mp3" in names:
        raise ValueError("An MP3 folder already exists here. Spliff will not combine archives.")
    if WORK_DIR in names:
        raise ValueError("A previous Keep both operation needs attention. Check the album folder before retrying.")
    copies = [pair["flac_sig"][0] for pair in pairs]
    largest = max(max(copies), *(pair["mp3_sig"][0] for pair in pairs))
    if sum(copies) + largest > shutil.disk_usage(folder).free:
        raise ValueError("There is not enough free space to copy the FLAC album.")
    return {"candidate": candidate, "root": str(root), "folder": str(folder),
            "album": review["comparison"]["album"], "pairs": pairs}


def plan_token(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()


def preview(db, candidate, original):
    plan = get_plan(db, candidate, original)
    destination = Path(plan["folder"])
    return {"token": plan_token(plan), "album": plan["album"], "destination": str(destination),
            "archive": str(destination / "MP3"), "count": len(plan["pairs"])}


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
            verified = hashlib.sha256()
            for block in iter(lambda: copy.read(1024 * 1024), b""):
                verified.update(block)
            now = destination.lstat()
            if (digest.digest() != verified.digest() or path_fingerprint(source) != expected
                    or signature(now) != signature(os.fstat(copy.fileno()))):
                raise ValueError("The copied audio could not be verified.")
            os.utime(destination, ns=(os.fstat(original.fileno()).st_atime_ns, expected[1]))
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
        shutil.copyfileobj(audio, copy, length=1024 * 1024)
        copy.flush()
        os.fsync(copy.fileno())
        if os.fstat(copy.fileno()).st_size != expected[0] or signature(os.fstat(audio.fileno())) != expected:
            raise ValueError("A staged file changed while copying.")


def clean_work(folder):
    def ignore_gone(_operation, _path, failure):
        # macOS can remove an exFAT AppleDouble sidecar with its primary file.
        if not isinstance(failure[1], FileNotFoundError):
            raise failure[1]

    shutil.rmtree(folder, onerror=ignore_gone)


def apply(db, candidate, original, token):
    plan = get_plan(db, candidate, original)
    if token != plan_token(plan):
        raise ValueError("The comparison changed. Review the destination again before confirming.")
    folder, root = Path(plan["folder"]), Path(plan["root"])
    archive, work = folder / "MP3", folder / WORK_DIR
    moved, published = [], []
    archive_created = False
    with closing(connect(db)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        for pair in plan["pairs"]:
            row = connection.execute(
                "SELECT root,path,protected,codec,present,error FROM tracks WHERE id=?", (pair["id"],)
            ).fetchone()
            if not row or tuple(row) != (str(root), str(Path(pair["mp3"]).relative_to(root)), 0, "mp3", 1, None):
                raise ValueError("An original changed in the library. Review the destination again.")
        work.mkdir(mode=0o700)
        try:
            (work / "plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
            for pair in plan["pairs"]:
                copy_verified(Path(pair["flac"]), work / pair["name"], pair["flac_sig"])
            archive.mkdir()
            archive_created = True
            for pair in plan["pairs"]:
                source = Path(pair["mp3"])
                if path_fingerprint(source) != pair["mp3_sig"]:
                    raise ValueError("An original changed during the operation.")
                saved = archive / source.name
                move_exclusive(source, saved)
                saved_sig = path_fingerprint(saved)
                moved.append((source, saved, saved_sig))
                connection.execute(
                    "UPDATE tracks SET path=?,size=?,mtime=?,device=?,inode=? WHERE id=?",
                    (str(saved.relative_to(root)), *saved_sig, pair["id"]),
                )
            for pair in plan["pairs"]:
                temporary, final = work / pair["name"], folder / pair["name"]
                move_exclusive(temporary, final)
                fingerprint = path_fingerprint(final)
                published.append((final, fingerprint))
                columns = ("root", "path", *AUDIO_FIELDS, "size", "mtime", "device", "inode")
                connection.execute(
                    f"INSERT INTO tracks({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                    (str(root), str(final.relative_to(root)),
                     *(pair["audio"].get(key) for key in AUDIO_FIELDS), *fingerprint),
                )
            connection.execute("INSERT INTO kept_albums(candidate,destination) VALUES (?,?)",
                               (candidate, str(folder)))
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
                    archive.rmdir()
            except (OSError, ValueError) as exc:
                raise ValueError(f"Keep both stopped. Check the recovery plan in {work} before retrying.") from exc
            clean_work(work)
            raise
        clean_work(work)
    return {"destination": str(folder), "archive": str(archive), "count": len(plan["pairs"])}
