"""Expiring, scoped MCP reviews backed by Spliff's existing file transactions."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from time import time
from uuid import uuid4

from . import keep_both, library_import
from .catalog import connect
from .nicotine_bridge.downloads import selection_parts
from .spectral import validate_decode

PLAN_TTL = 900


def prepare_import(db, candidate, original_id, library_root):
    original_snapshot = None
    with closing(connect(db)) as connection:
        roots = library_import.catalog_roots(connection)
        if original_id is not None:
            original = connection.execute(
                "SELECT root,path,size,mtime,device,inode FROM tracks WHERE id=? AND present=1 AND error IS NULL",
                (original_id,)).fetchone()
            if not original or (library_root and library_root != original["root"]):
                raise ValueError("Choose the indexed library containing the original release.")
            library_root = original["root"]
            path = Path(library_root) / original["path"]
            if not path.is_relative_to(library_root) or ".." in path.parts:
                raise ValueError("The original is outside the indexed library.")
            with keep_both.open_directory(path.parent, {library_root: roots[library_root]}) as directory:
                fingerprint = keep_both.file_signature(directory, path.name)
            if fingerprint != tuple(original[key] for key in ("size", "mtime", "device", "inode")):
                raise ValueError("The selected original changed. Rescan and review the release.")
            original_snapshot = [str(path), fingerprint]
    if library_root is None and len(roots) == 1:
        library_root = next(iter(roots))
    if library_root not in roots:
        raise ValueError("Select an existing indexed library root before importing.")
    report = library_import.source_report(db, candidate)
    details = library_import.seed(report)
    details["category"] = details["category"] or ("Albums" if report["kind"] == "album" else "Singles")
    plan = library_import.prepare(db, candidate, library_root, roots[library_root], details)
    for pair in plan["pairs"]:
        validate_decode(pair["flac"], pair["flac_sig"])
    return plan, original_snapshot


def preview(db, candidate_id, mode, original_track_id=None, library_root=None, override_track_ids=None):
    selection_parts(candidate_id)
    if original_track_id is not None and (type(original_track_id) is not int or not 0 < original_track_id < 2**63):
        raise ValueError("Choose an indexed original release.")
    overrides = override_track_ids if override_track_ids is not None else []
    if (not isinstance(overrides, list) or len(overrides) > 50
            or any(type(track) is not int or track < 1 for track in overrides)
            or len(overrides) != len(set(overrides)) or (overrides and mode != "replace")):
        raise ValueError("Select distinct indexed track IDs for an advanced replacement exception.")
    if library_root is not None and not isinstance(library_root, str):
        raise ValueError("Select an indexed library root.")
    if mode == "keep":
        imported, snapshot = prepare_import(db, candidate_id, original_track_id, library_root)
        reviewed = library_import.preview(imported)
        plan = {"import": imported, "original": snapshot}
    elif mode == "replace" and original_track_id is not None and library_root is None:
        reviewed = keep_both.preview(db, candidate_id, original_track_id, mode, strict=True, overrides=overrides)
        plan = {"token": reviewed["token"], "overrides": overrides}
    else:
        raise ValueError("Choose Keep both or Replace with an indexed original.")
    expires = int(time()) + PLAN_TTL
    plan_id = uuid4().hex
    review = {key: value for key, value in reviewed.items() if key != "token"}
    review.update(plan_id=plan_id, candidate_id=candidate_id, original_track_id=original_track_id,
                  mode=mode, expires_at=expires, override_track_ids=overrides)
    with closing(connect(db)) as connection, connection:
        connection.execute("DELETE FROM mcp_album_plans WHERE expires <= ?", (int(time()),))
        connection.execute("INSERT INTO mcp_album_plans VALUES (?,?,?,?,?,?,?)",
                           (plan_id, candidate_id, original_track_id, mode, expires,
                            json.dumps(plan), json.dumps(review)))
    return review


def status(db, candidate_id):
    selection_parts(candidate_id)
    with closing(connect(db)) as connection:
        row = connection.execute("SELECT result FROM upgrade_results WHERE candidate=?",
                                 (candidate_id,)).fetchone()
        if not row:
            plans = connection.execute("SELECT plan,review,mode FROM mcp_album_plans WHERE candidate=?",
                                       (candidate_id,)).fetchall()
    if not row:
        for saved in plans:
            if saved["mode"] == "keep":
                work = Path(json.loads(saved["plan"])["import"]["work"])
            else:
                work = Path(json.loads(saved["review"])["destination"]) / keep_both.WORK_DIR
            if work.is_dir() and not work.is_symlink():
                return {"candidate_id": candidate_id, "status": "recovery_needed",
                        "recovery_path": str(work), "message": "Inspect the recovery journal before retrying."}
        return {"candidate_id": candidate_id, "status": "no_committed_action"}
    result = json.loads(row["result"])
    status_name = "recovery_needed" if result.get("failed") or result.get("recovery_path") else "completed"
    return {"candidate_id": candidate_id, "status": status_name, **result}


def apply(db, candidate_id, mode, plan_id, *, confirm, original_track_id=None):
    selection_parts(candidate_id)
    if original_track_id is not None and (type(original_track_id) is not int or not 0 < original_track_id < 2**63):
        raise ValueError("Choose an indexed original release.")
    if not isinstance(plan_id, str) or len(plan_id) != 32:
        raise ValueError("Choose a reviewed action plan.")
    current = status(db, candidate_id)
    if current.get("transaction_id") == plan_id:
        if (current["operation"], current["original_track_id"]) != (mode, original_track_id):
            raise ValueError("The action does not match the committed transaction.")
        return current
    if current["status"] == "recovery_needed":
        raise ValueError(f'Inspect the recovery path before retrying: {current["recovery_path"]}')
    if current["status"] != "no_committed_action":
        raise ValueError("This download already has a committed library action.")
    with closing(connect(db)) as connection:
        row = connection.execute("SELECT * FROM mcp_album_plans WHERE id=?", (plan_id,)).fetchone()
    if (not row or row["expires"] <= time()
            or (row["candidate"], row["mode"], row["original_id"]) != (candidate_id, mode, original_track_id)):
        raise ValueError("The action plan expired or does not match this request. Review both options again.")
    review = json.loads(row["review"])
    if not confirm(review):
        raise ValueError("The host did not authorize this reviewed action. No library files were changed.")
    if row["expires"] <= time():
        raise ValueError("The action plan expired during confirmation. Review the options again.")
    plan = json.loads(row["plan"])
    if mode == "keep":
        refreshed, snapshot = prepare_import(db, candidate_id, original_track_id, plan["import"]["root"])
        if keep_both.plan_token({"import": refreshed, "original": snapshot}) != keep_both.plan_token(plan):
            raise ValueError("The reviewed import changed. Review the options again.")
        plan = refreshed
    files = review["files"]
    receipt = {"transaction_id": plan_id, "operation": mode, "original_track_id": original_track_id,
               "files_imported": [file["install"] for file in files],
               "files_replaced": [file["original"] for file in files] if mode == "replace" else [],
               "files_archived": [file["archive"] for file in files] if mode == "replace" else [],
               "verification": [{"destination": file["install"], "integrity": "full decode and byte verification",
                                  "recording_evidence": file.get("recording_evidence")} for file in files],
               "rollback_available": False}
    try:
        if mode == "keep":
            library_import.apply(db, plan, receipt=receipt)
        else:
            keep_both.apply(db, candidate_id, original_track_id, plan["token"], mode, receipt=receipt,
                            trusted_review=True, overrides=plan["overrides"])
    except (ValueError, OSError, sqlite3.Error):
        committed = status(db, candidate_id)
        if committed.get("transaction_id") != plan_id:
            raise
        return committed
    return status(db, candidate_id)
