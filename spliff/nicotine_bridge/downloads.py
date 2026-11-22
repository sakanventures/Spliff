"""Resolve a selected search offer and reconcile its private staging folder."""
import json
import os
import re
import shutil
import stat
from pathlib import Path, PureWindowsPath
from time import time

from .wire import album_tracks, staging_root

SELECTION = re.compile(r"(track|album)-([0-9a-f]{32})-([0-9]{1,3})")
MAX_TRACKS = 60
MAX_BYTES = 8 * 1024**3


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

    def queue(self, selection, searches, roots):
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
        search = searches.get(search_id)
        if not search or time() - search[0] > 600 or index >= len(search[1]):
            raise ValueError("This candidate has expired. Search again and select a fresh result.")
        files = search[1]
        anchor = files[index]
        selected = album_tracks(files, anchor) if kind == "album" else [anchor]
        size = sum(file["bytes"] for file in selected)
        if len(selected) > MAX_TRACKS or size > MAX_BYTES:
            raise ValueError("This candidate exceeds the staging limit of 60 audio files or 8 GiB.")
        if size > min(shutil.disk_usage(root).free, shutil.disk_usage(incomplete.parent).free):
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
                "entries": entries}
        pending = folder / "job.tmp"
        with pending.open("x", encoding="utf-8") as output:
            json.dump(data, output)
            output.flush()
            os.fsync(output.fileno())
        pending.replace(manifest)
        for entry in entries:
            self.core.downloads.enqueue_download(
                entry["peer"], entry["remote"], folder_path=str(folder / entry["directory"]),
                size=entry["bytes"],
            )
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
