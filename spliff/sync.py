"""Drive a user-selected folder's upgrade searches and review in the local dashboard service."""
import logging
import re
import sqlite3
from contextlib import closing
from pathlib import Path, PureWindowsPath

from .artist import release_folder
from .catalog import connect
from .nicotine import availability, search
from .scanner import scan
from .staged_downloads import list_downloads, originals, recording_credit, review_download

LOG = logging.getLogger(__name__)


def start(db, folder=None):
    if not isinstance(folder, str) or not folder.strip() or not Path(folder).expanduser().is_absolute():
        raise ValueError("Choose an absolute path to the local music folder.")
    root = Path(folder).expanduser().resolve(strict=True)
    scanned = scan(db, root)
    with closing(connect(db)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT OR REPLACE INTO sync_folders(root) VALUES (?)", (str(root),))
        rows = connection.execute("""
            SELECT id,root,path,artist,album,title FROM tracks
            WHERE quality='lossy' AND present=1 AND error IS NULL AND protected=0
              AND (root=? OR substr(?,1,length(root)+1)=root||'/')
            ORDER BY id
        """, (str(root), str(root))).fetchall()
        targets = {}
        for row in rows:
            path = Path(row["root"]) / row["path"]
            if not path.is_relative_to(root) or not row["artist"] or not row["title"]:
                continue
            if "MP3" in path.relative_to(root).parts[:-1]:
                continue
            kind = "album" if row["album"] else "track"
            key = (kind, path.parent, row["id"])
            if kind == "album":
                key = (kind, Path(row["root"]) / release_folder(row["path"]), row["album"].casefold())
            label = row["album"] or row["title"]
            query = " ".join(f'{row["artist"]} {label}'.split())[:180]
            targets.setdefault(key, (str(root), row["id"], kind, query))
        for target in targets.values():
            connection.execute("""
                INSERT INTO sync_targets(root,original_id,kind,query) VALUES (?,?,?,?)
                ON CONFLICT(root,original_id) DO UPDATE SET state='queued',detail=NULL
                WHERE sync_targets.state IN ('no_candidate','search_failed')
            """, target)
    return {"folder": str(root), "targets": len(targets), **scanned}


def status(db):
    with closing(connect(db)) as connection:
        return {"folders": [row["root"] for row in connection.execute(
                    "SELECT root FROM sync_folders ORDER BY selected DESC, root")],
                "items": [dict(row) for row in connection.execute("""
            SELECT s.*,CASE WHEN k.candidate IS NULL THEN s.state ELSE 'applied' END AS current_state
            FROM sync_targets s LEFT JOIN kept_albums k ON k.candidate=s.candidate
            ORDER BY s.original_id
        """)]}


def update(db, target, state, detail=None, candidate=None):
    with closing(connect(db)) as connection, connection:
        connection.execute("""
            UPDATE sync_targets SET state=?,detail=?,candidate=coalesce(?,candidate)
            WHERE root=? AND original_id=?
        """, (state, detail, candidate, target["root"], target["original_id"]))


def remote_title(filename):
    name = PureWindowsPath(filename).stem
    return recording_credit(re.sub(r"^\d{1,3}[ ._-]+", "", name))[0]


def choose_offer(candidates, indexed, kind):
    titles = {recording_credit(track["title"])[0] for track in indexed if track["title"]}
    titles.discard("")
    ranked = []
    for offer in candidates:
        if not offer.get("candidate_id"):
            continue
        if kind == "track":
            if remote_title(offer["path"]) in titles:
                ranked.append((-1, *availability(offer), offer["candidate_id"]))
        elif offer["matching_files"] == offer["lossless_files"]:
            overlap = titles & {remote_title(name) for name in offer["filenames"]}
            if len(overlap) >= min(2, len(titles)) and len(overlap) * 2 >= len(titles) and overlap:
                ranked.append((-len(overlap), *availability(offer), offer["candidate_id"]))
    return min(ranked)[-1] if ranked else None


def reconcile(db):
    jobs = {job["id"]: job for job in list_downloads(db)}
    for job in jobs.values():
        if job["state"] != "downloaded":
            continue
        try:
            report = review_download(db, job["id"], include_sources=True, research=True)
            if report.get("comparison") and not report["comparison"].get("album_mismatch"):
                from .keep_both import review_plans
                review_plans(db, job["id"], report["comparison"]["original_track_id"], refresh=True)
            with closing(connect(db)) as connection, connection:
                connection.execute("INSERT OR IGNORE INTO transfer_preparations(candidate) VALUES (?)",
                                   (job["id"],))
            job["state"] = "reviewed"
        except (ValueError, OSError, sqlite3.Error):
            LOG.exception("Could not prepare transfer %s", job["id"])

    jobs_by_original = {job["original_track_id"]: job for job in reversed(list(jobs.values()))
                        if job["original_track_id"] is not None and job["state"] != "applied"}
    pending = status(db)["items"]
    for target in pending:
        if target["state"] in ("queueing", "downloading", "reviewing", "queue_uncertain", "candidate_ready"):
            job = jobs.get(target["candidate"])
            if not job and target["state"] == "candidate_ready":
                job = jobs_by_original.get(target["original_id"])
            if job and job["state"] == "reviewed":
                update(db, target, "reviewed", candidate=job["id"])
            elif job and job["state"] == "downloaded":
                update(db, target, "reviewing", candidate=job["id"])
            elif job and target["state"] == "candidate_ready":
                update(db, target, "downloading", candidate=job["id"])
            elif target["state"] == "queueing" and not job:
                update(db, target, "queue_uncertain", "Check Nicotine+ before retrying this transfer.")
    active = {target["original_id"] for target in pending if target["candidate"]}
    target = next((target for target in pending if target["state"] == "queued"), None)
    if target is None:
        return
    try:
        index = originals(db, target["original_id"], target["kind"])
    except ValueError as exc:
        update(db, target, "search_failed", str(exc))
        return
    if not index or any(not (Path(track["root"]) / track["path"]).is_relative_to(target["root"])
                        for track in index):
        update(db, target, "search_failed", "The original release extends outside this sync folder.")
        return
    if target["original_id"] in active:
        update(db, target, "search_failed", "This recording already has a transfer.")
        return
    existing = next((job for job in jobs.values() if job["original_track_id"] in {track["id"] for track in index}
                     and job["state"] != "applied"), None)
    if existing:
        update(db, target, "reviewed" if existing["state"] == "reviewed" else "downloading",
               candidate=existing["id"])
        return
    try:
        offers = search(db, target["query"], kind=target["kind"], limit=None)["candidates"]
    except (ValueError, OSError) as exc:
        if "Nicotine+" not in str(exc):
            update(db, target, "search_failed", str(exc))
        return
    candidate = choose_offer(offers, index, target["kind"])
    if not candidate:
        update(db, target, "no_candidate", "No suitable peer offer found.")
        return
    update(db, target, "candidate_ready", "Search the release in Advanced and select a candidate to download.", candidate)


def run_worker(db, stop):
    # Multiple dashboard instances may read the same catalog. Only one queues work.
    import fcntl

    with db.with_suffix(".sync.lock").open("a") as lock:
        while not stop.is_set():
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                stop.wait(5)
                continue
            try:
                reconcile(db)
            except (ValueError, OSError, sqlite3.Error):
                LOG.exception("Folder sync could not advance")
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
            stop.wait(5)
