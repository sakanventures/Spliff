"""Restore hidden-archive originals and retain installed files for recovery."""
import json
import os
from contextlib import closing
from pathlib import Path

from . import keep_both
from .catalog import connect
from .nicotine_bridge.downloads import selection_parts


def manifest(connection, candidate, transaction):
    row = connection.execute("SELECT manifest FROM upgrade_recovery WHERE candidate=?", (candidate,)).fetchone()
    if not row:
        raise ValueError("Automatic rollback requires originals in the hidden recovery folder.")
    result = connection.execute("SELECT result FROM upgrade_results WHERE candidate=?", (candidate,)).fetchone()
    if not result or json.loads(result["result"]).get("work_cleanup_pending"):
        raise ValueError("The replacement work folder still needs cleanup. Check the transaction recovery path.")
    recovery = json.loads(row["manifest"])
    if recovery["transaction_id"] != transaction:
        raise ValueError("Select the committed replacement transaction.")
    recovery["directories"] = {path: tuple(value) for path, value in recovery["directories"].items()}
    return recovery


def rollback(db, candidate_id, transaction_id, *, confirm):
    selection_parts(candidate_id)
    with closing(connect(db)) as connection:
        recovery = manifest(connection, candidate_id, transaction_id)
    work = Path(recovery["folder"]) / f".spliff-rollback-{transaction_id}"
    files = recovery["files"]
    for index, file in enumerate(files):
        file["recover_download"] = str(work / f'{index:03d}{Path(file["install"]).suffix}')
    if not confirm({"mode": "rollback", "files": files}):
        raise ValueError("The host did not authorize this rollback.")
    root = Path(recovery["root"])
    identities = recovery["directories"]
    restored, retained = [], []
    with closing(connect(db)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        manifest(connection, candidate_id, transaction_id)
        for file in files:
            original, archive, installed = (Path(file[key]) for key in ("original", "archive", "install"))
            if any(not path.is_relative_to(root) for path in (original, archive, installed)):
                raise ValueError("The rollback files are outside the indexed library.")
            for path in (original, archive, installed):
                with keep_both.open_directory(path.parent, identities):
                    pass
            if os.path.lexists(original):
                raise ValueError(f"The original destination is occupied: {original}")
            archived = connection.execute("SELECT root,path,present,protected FROM tracks WHERE id=?",
                                          (file["id"],)).fetchone()
            if not archived or tuple(archived) != (str(root), str(archive.relative_to(root)), 0, 0):
                raise ValueError("The archived original's catalog entry changed.")
            current = connection.execute("SELECT present,protected FROM tracks WHERE root=? AND path=?",
                                         (str(root), str(installed.relative_to(root)))).fetchone()
            if not current or tuple(current) != (1, 0):
                raise ValueError("The installed recording is protected or its catalog entry changed.")
            for path, kind in ((archive, "original"), (installed, "installed")):
                if keep_both.verified_digest(path, tuple(file[f"{kind}_signature"])) != file[f"{kind}_digest"]:
                    raise ValueError(f"A reviewed rollback file changed: {path}")
        keep_both.create_directory(work, identities)
        journal = None
        try:
            journal = keep_both.write_journal(work, recovery, identities)
            for file in files:
                original, archive, installed, saved = (Path(file[key]) for key in
                                                       ("original", "archive", "install", "recover_download"))
                fingerprint = keep_both.move_exclusive(archive, original, identities=identities,
                                                       expected=tuple(file["original_signature"]))
                restored.append((original, archive, fingerprint))
                fingerprint = keep_both.move_exclusive(installed, saved, identities=identities,
                                                       expected=tuple(file["installed_signature"]))
                retained.append((saved, installed, fingerprint))
                connection.execute("DELETE FROM tracks WHERE root=? AND path=?",
                                   (str(root), str(installed.relative_to(root))))
                connection.execute("UPDATE tracks SET path=?,size=?,mtime=?,device=?,inode=?,present=1 WHERE id=?",
                                   (str(original.relative_to(root)), *restored[-1][2], file["id"]))
            folders = {path.parent for source, dest, _ in (*restored, *retained) for path in (source, dest)}
            for folder in folders:
                with keep_both.open_directory(folder, identities) as descriptor:
                    os.fsync(descriptor)
            row = connection.execute("SELECT result FROM upgrade_results WHERE candidate=?", (candidate_id,)).fetchone()
            result = json.loads(row["result"])
            result.update(status="rolled_back", rollback_available=False, recovery_path=str(work),
                          files_restored=[str(path) for path, _, _ in restored],
                          downloads_retained=[str(path) for path, _, _ in retained])
            connection.execute("UPDATE upgrade_results SET result=? WHERE candidate=?", (json.dumps(result), candidate_id))
            connection.execute("DELETE FROM upgrade_recovery WHERE candidate=?", (candidate_id,))
            connection.commit()
        except BaseException:
            if not connection.in_transaction:
                raise ValueError(f"Inspect the rollback journal at {work} before retrying.")
            connection.rollback()
            try:
                for source, dest, fingerprint in reversed((*restored, *retained)):
                    keep_both.move_exclusive(source, dest, identities=identities, expected=fingerprint)
                keep_both.clean_work(work, identities, (), {}, journal)
            except (OSError, ValueError) as exc:
                raise ValueError(f"Inspect the rollback journal at {work} before retrying.") from exc
            raise
    return result
