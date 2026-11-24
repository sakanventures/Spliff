"""Resolve a selected search offer and reconcile its private staging folder."""
import json
import os
import re
import shutil
import stat
from pathlib import Path, PureWindowsPath
from time import time

from .wire import FORMATS, album_tracks, fingerprint, staging_root

SELECTION = re.compile(r"(track|album)-([0-9a-f]{32})-([0-9]{1,3})")
MAX_TRACKS = 60
MAX_BYTES = 8 * 1024**3
MAX_FILE_BYTES = 1024**3
INACTIVE = {"Finished", "Paused", "Filtered"}
ENABLE_FILE = "transfers-enabled"


def enabled(root):
    flag = root / ENABLE_FILE
    try:
        info = flag.lstat()
    except FileNotFoundError:
        return False
    return (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and not info.st_mode & 0o077)


def audit(root, event, **details):
    path = root / "transfer-audit.jsonl"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as output:
        info = os.fstat(output.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Spliff needs a private transfer audit file.")
        output.write(json.dumps({"event": event, "time": int(time()), **details}) + "\n")
        output.flush()
        os.fsync(output.fileno())


def valid_file(file):
    path = file["path"]
    peer = file["peer"]
    parts = path.split("\\")
    return (isinstance(peer, str) and 0 < len(peer) <= 64 and peer.isprintable()
            and len(path.encode("utf-8")) <= 480 and path.isprintable()
            and "/" not in path and ":" not in path
            and all(part and part not in {".", ".."} for part in parts)
            and PureWindowsPath(path).suffix.lower().lstrip(".") == file["format"]
            and file["format"] in FORMATS and type(file["bytes"]) is int
            and 0 < file["bytes"] <= MAX_FILE_BYTES)



def selection_parts(selection):
    match = SELECTION.fullmatch(selection) if isinstance(selection, str) else None
    if not match:
        raise ValueError("Choose a candidate ID returned by Spliff Search.")
    kind, search_id, index = match.groups()
    return kind, search_id, int(index)


def private_folder(folder):
    if folder.resolve() != folder:
        raise ValueError("Spliff staging must point to a regular local directory.")
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = folder.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Spliff needs an owner-only staging directory.")


def read_job(folder):
    if not folder.is_dir() or folder.resolve() != folder:
        raise ValueError("Unknown staged download.")
    manifest = folder / "job.json"
    if manifest.is_symlink():
        raise ValueError("Unexpected staging manifest location.")
    try:
        with manifest.open(encoding="utf-8") as source:
            return json.load(source)
    except FileNotFoundError as exc:
        raise ValueError("Unknown staged download.") from exc


class Downloads:
    def __init__(self, core, config):
        self.core = core
        self.config = config
        self.allowed = {}
        self.recovery = {}

    @staticmethod
    def is_staged(transfer, root):
        return Path(transfer.folder_path).is_relative_to(root)

    def permit_transfer(self, transfer, claimed_size):
        root = staging_root()
        if not self.is_staged(transfer, root):
            return True
        key = (transfer.username, transfer.virtual_path, transfer.folder_path)
        size = self.recovery.get(key)
        if size is None and enabled(root):
            size = self.allowed.get(key)
        return size is not None and transfer.size == size and claimed_size in (0, size)

    @staticmethod
    def transfer_key(transfer):
        return transfer.username, transfer.virtual_path, transfer.folder_path

    def outstanding(self, root):
        return {self.transfer_key(transfer): transfer.size
                for transfer in self.core.downloads.transfers.values()
                if self.is_staged(transfer, root) and transfer.status not in INACTIVE}

    def recover(self, selections):
        """Permit manual retry of exact paused rows, without queueing or retrying."""
        if (not isinstance(selections, list) or not 1 <= len(selections) <= 10
                or any(not isinstance(selection, str) for selection in selections)
                or len(set(selections)) != len(selections)):
            raise ValueError("Provide one or more different staged candidate IDs.")
        root = staging_root()
        private_folder(root)
        transfers = self.core.downloads.transfers
        approved = {}
        for selection in selections:
            selection_parts(selection)
            folder = root / selection
            job = read_job(folder)
            if job.get("id") != selection:
                raise ValueError("The staged job ID does not match its manifest.")
            for entry in job["entries"]:
                peer, remote, size = entry["peer"], entry["remote"], entry["bytes"]
                directory = entry["directory"]
                if not re.fullmatch(r"[0-9]{3}", directory):
                    raise ValueError("The staged job has an invalid download directory.")
                destination = folder / directory
                if destination.resolve() != destination or not destination.is_dir():
                    raise ValueError("The staged download directory is unavailable.")
                key = (peer, remote, str(destination))
                transfer = transfers.get(peer + remote)
                if transfer is None:
                    continue
                if (self.transfer_key(transfer) != key or transfer.size != size
                        or not valid_file({"peer": peer, "path": remote, "bytes": size,
                                           "format": PureWindowsPath(remote).suffix.lower().lstrip(".")})):
                    raise ValueError("A saved transfer differs from its staging manifest.")
                if transfer.status == "Paused":
                    approved[key] = size
        if not approved:
            raise ValueError("No matching paused transfers were found.")
        reserved = {**self.outstanding(root), **self.recovery, **approved}
        if len(reserved) > MAX_TRACKS or sum(reserved.values()) > MAX_BYTES:
            raise ValueError("Recovery would exceed 60 files or 8 GiB.")
        audit(root, "manual_recovery_authorized", selections=selections,
              entries=[{"peer": peer, "remote": remote, "directory": directory, "bytes": size}
                       for (peer, remote, directory), size in approved.items()])
        self.recovery.update(approved)
        return {"authorized_paused_files": len(approved), "selections": selections}


    def stop(self):
        root = staging_root()
        private_folder(root)
        (root / ENABLE_FILE).unlink(missing_ok=True)
        self.allowed.clear()
        self.recovery.clear()
        transfers = self.core.downloads.transfers
        matches = []
        for folder in root.iterdir():
            if not SELECTION.fullmatch(folder.name):
                continue
            try:
                job = read_job(folder)
                for entry in job["entries"]:
                    peer, remote = entry["peer"], entry["remote"]
                    directory = entry["directory"]
                    if not re.fullmatch(r"[0-9]{3}", directory):
                        continue
                    transfer = transfers.get(peer + remote)
                    if (transfer and transfer.username == peer and transfer.virtual_path == remote
                            and transfer.folder_path == str(folder / directory)
                            and transfer.status not in {"Finished", "Paused"}):
                        matches.append(transfer)
            except (ValueError, OSError, KeyError, TypeError):
                continue
        if matches:
            self.core.downloads.abort_downloads(matches)
        audit(root, "emergency_stop", paused=len(matches))
        return {"enabled": False, "paused": len(matches)}

    def queue(self, selection, searches, roots, approved=None, source=None, original_id=None):
        kind, search_id, index = selection_parts(selection)
        root = staging_root()
        folder = root / selection
        manifest = folder / "job.json"
        settings = self.config.sections["transfers"]
        if settings["afterfinish"] or settings["afterfolder"]:
            raise ValueError("Turn off Nicotine+ post-download commands before staging through Spliff.")
        incomplete = Path(self.core.downloads.get_incomplete_download_folder()).resolve()
        if (not isinstance(roots, list) or any(not isinstance(path, str) for path in roots)
                or any(location.is_relative_to(Path(path).resolve())
                       for location in (folder, incomplete) for path in roots)):
            raise ValueError("Keep Nicotine+ staging and incomplete downloads outside indexed music folders.")
        private_folder(root)
        if manifest.is_file():
            return self.status(selection)
        if not enabled(root):
            raise ValueError("Spliff transfers are disabled. Enable them locally after reviewing the Nicotine+ queue.")
        if source not in {"mcp", "dashboard", "manual"} or (
                original_id is not None and (type(original_id) is not int or original_id < 1)):
            raise ValueError("Staging requires an explicit candidate selection.")
        search = searches.get(search_id)
        if not search or time() - search[0] > 600 or index >= len(search[1]):
            raise ValueError("This candidate has expired. Search again and select a fresh result.")
        files = search[1]
        anchor = files[index]
        selected = album_tracks(files, anchor) if kind == "album" else [anchor]
        if (not selected or not all(valid_file(file) for file in selected)
                or len({file["peer"] + file["path"] for file in selected}) != len(selected)
                or fingerprint(selected) != approved):
            raise ValueError("The selected peer files do not match a valid saved allowlist.")
        size = sum(file["bytes"] for file in selected)
        if len(selected) > MAX_TRACKS or size > MAX_BYTES:
            raise ValueError("This candidate exceeds the staging limit of 60 audio files or 8 GiB.")
        transfers = self.core.downloads.transfers
        self.allowed = {
            key: size for key, size in self.allowed.items()
            if (transfer := transfers.get(key[0] + key[1])) is not None
            and transfer.folder_path == key[2] and transfer.status not in INACTIVE
        }
        outstanding = {**self.outstanding(root), **self.recovery}
        reserved = sum(outstanding.values())
        if len(outstanding) + len(selected) > MAX_TRACKS or reserved + size > MAX_BYTES:
            raise ValueError("The outstanding Spliff queue would exceed 60 audio files or 8 GiB.")
        if reserved + size > min(shutil.disk_usage(root).free, shutil.disk_usage(incomplete.parent).free):
            raise ValueError("There is insufficient space to stage this candidate.")
        if any(file["peer"] + file["path"] in self.core.downloads.transfers for file in selected):
            raise ValueError("A selected file is already in the Nicotine+ transfer list. Choose another candidate.")
        private_folder(folder)
        entries = []
        for index, file in enumerate(selected, 1):
            destination = folder / f"{index:03d}"
            private_folder(destination)
            entries.append({"peer": file["peer"], "remote": file["path"], "bytes": file["bytes"],
                            "directory": destination.name})
        data = {"id": selection, "query": search[2], "kind": kind, "created": int(time()),
                "entries": entries, "source": source, "original_id": original_id, "fingerprint": approved}
        pending = folder / "job.tmp"
        with pending.open("x", encoding="utf-8") as output:
            json.dump(data, output)
            output.flush()
            os.fsync(output.fileno())
        pending.replace(manifest)
        audit(root, "queue_intent", **data)
        attempted = []
        try:
            for entry in entries:
                if not enabled(root):
                    raise ValueError("Spliff transfers were disabled during queueing.")
                peer, remote = entry["peer"], entry["remote"]
                destination = str(folder / entry["directory"])
                key = (peer, remote, destination)
                attempted.append(key)
                self.allowed[key] = entry["bytes"]
                self.core.downloads.enqueue_download(peer, remote, folder_path=destination, size=entry["bytes"])
                transfer = transfers.get(peer + remote)
                if (transfer is None or transfer.username != peer or transfer.virtual_path != remote
                        or transfer.folder_path != destination or transfer.size != entry["bytes"]):
                    raise ValueError("Nicotine+ did not accept the exact staged transfer.")
            audit(root, "queue_submitted", id=selection, files=len(entries), bytes=size)
        except Exception:
            rollback = []
            for peer, remote, destination in attempted:
                self.allowed.pop((peer, remote, destination), None)
                transfer = transfers.get(peer + remote)
                if (transfer and transfer.username == peer and transfer.virtual_path == remote
                        and transfer.folder_path == destination):
                    rollback.append(transfer)
            if rollback:
                self.core.downloads.abort_downloads(rollback)
            audit(root, "queue_interrupted", id=selection, attempted=len(attempted))
            raise
        return self.status(selection)

    def status(self, selection):
        selection_parts(selection)
        folder = staging_root() / selection
        job = read_job(folder)
        items = []
        for entry in job["entries"]:
            destination = folder / entry["directory"]
            remote, peer = entry["remote"], entry["peer"]
            actual, found = self.core.downloads.get_complete_download_file_path(
                peer, remote, entry["bytes"], str(destination),
            )
            transfer = self.core.downloads.transfers.get(peer + remote)
            path = Path(actual)
            complete = (found and path.is_file() and path.resolve() == path
                        and path.parent == destination)
            state = "Finished" if complete else "Unavailable"
            if not complete and transfer and Path(transfer.folder_path) == destination:
                state = "Awaiting staged file" if transfer.status == "Finished" else transfer.status
            items.append({"name": PureWindowsPath(remote).name, "status": state,
                          "bytes": entry["bytes"], "file": str(Path(actual).relative_to(folder)) if complete else None})
        return {"id": selection, "query": job["query"], "kind": job["kind"], "items": items,
                "complete": sum(item["file"] is not None for item in items)}
