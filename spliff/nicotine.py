"""Spliff's client and candidate ranking for the local Nicotine+ bridge."""
import shutil
import socket
from collections import defaultdict
from pathlib import Path, PureWindowsPath
from tempfile import TemporaryDirectory

from .nicotine_bridge.downloads import ENABLE_FILE, audit, enabled, private_folder
from .nicotine_bridge.wire import (
    album_key, album_tracks, track_stem,
    REQUEST_BYTES, RESPONSE_BYTES, VERSION, WINDOW_SECONDS, encode, endpoint, read, search_term, staging_root,
)


def install_plugin(plugins, upgrade=False):
    """Install the packaged bridge, upgrading only a known Spliff installation."""
    target = plugins.expanduser() / "spliff_search"
    source = Path(__file__).with_name("nicotine_bridge")
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = target.exists() or target.is_symlink()
    if existing:
        info = target / "PLUGININFO"
        if (not upgrade or target.is_symlink() or not info.is_file()
                or 'Name = "Spliff Search"' not in info.read_text(encoding="utf-8")):
            raise ValueError("A plugin already exists at this destination. Use --upgrade only for Spliff Search.")
    with TemporaryDirectory(prefix=".spliff-install-", dir=target.parent) as staging:
        prepared = Path(staging) / "new"
        prepared.mkdir()
        for name in ("wire.py", "plugin.py", "downloads.py", "PLUGININFO", "entrypoint.py"):
            shutil.copyfile(source / name, prepared / ("__init__.py" if name == "entrypoint.py" else name))
        previous = Path(staging) / "previous"
        if existing:
            target.rename(previous)
        try:
            prepared.rename(target)
        except OSError:
            if existing:
                previous.rename(target)
            raise
    return target


def request(operation, **arguments):
    payload = encode({"version": VERSION, "operation": operation, **arguments}, REQUEST_BYTES)
    path = endpoint()
    dispatched = False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(2)
            connection.connect(str(path))
            dispatched = True
            connection.sendall(payload)
            connection.settimeout(WINDOW_SECONDS + 6)
            with connection.makefile("rb") as stream:
                response = read(stream, RESPONSE_BYTES)
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        raise ValueError("Open Nicotine+ and enable the Spliff Search plugin in its preferences.") from exc
    except (OSError, ValueError) as exc:
        if operation == "queue" and dispatched:
            raise ValueError("Nicotine+ did not confirm the action. Check the staged download before retrying.") from exc
        if isinstance(exc, ValueError):
            raise
        raise ValueError("Spliff could not reach Nicotine+. Try again shortly.") from exc
    if response.get("version") != VERSION:
        raise ValueError("The Spliff app and Nicotine+ plugin use different bridge versions.")
    if "error" in response:
        raise ValueError(response["error"])
    return response


def availability(candidate):
    queue = candidate["queue"]
    return not candidate["free_slot"], queue if queue is not None else float("inf")


def album_folders(files, search_id=None):
    groups = defaultdict(list)
    for index, file in enumerate(files):
        groups[album_key(file)].append((index, file))
    candidates = []
    for (peer, folder), indexed in groups.items():
        index = indexed[0][0]
        entries = [file for _, file in indexed]
        selected = album_tracks(entries, entries[0])
        names = {PureWindowsPath(file["path"]).name for file in entries}
        stems = {track_stem(file) for file in entries}
        candidates.append({
            "peer": peer, "folder": folder, "matching_files": len(stems),
            **({"candidate_id": f"album-{search_id}-{index}"} if search_id else {}),
            "formats": sorted({file["format"] for file in entries}),
            "lossless_files": len(selected),
            "flac_files": sum(file["format"] == "flac" for file in selected),
            "total_bytes": sum(file.get("bytes") or 0 for file in selected),
            "quality": [dict(sample_rate=rate, bit_depth=depth) for rate, depth in sorted(
                {(file.get("sample_rate") or 0, file.get("bit_depth") or 0) for file in selected})],
            "free_slot": entries[0]["free_slot"], "queue": entries[0]["queue"],
            "filenames": sorted(names, key=lambda name: (name.casefold(), name))[:40],
        })
    return sorted(candidates, key=lambda item: (-item["total_bytes"], *availability(item)))


def search(_db, query=None, kind="track", limit=3):
    query = search_term(query)
    if kind not in ("track", "album"):
        raise ValueError("Choose a track or album search.")
    if limit is not None and (type(limit) is not int or not 1 <= limit <= 10):
        raise ValueError("Choose between 1 and 10 candidates.")
    response = request("search", query=query)
    files = response["files"]
    search_id = response.get("search_id")
    if kind == "album":
        candidates = album_folders(files, search_id)
    else:
        candidates = sorted(
            (dict(file, candidate_id=f"track-{search_id}-{index}") if search_id else file
             for index, file in enumerate(files)), key=availability,
        )
    from .candidates import record
    record(_db, query, kind, candidates[:limit], files)
    return {
        "query": query, "kind": kind, "candidates": candidates[:limit],
        "observed_matches": len(candidates), "search_window_seconds": WINDOW_SECONDS,
        "truncated": response["truncated"] or (limit is not None and len(candidates) > limit),
        "verification": ("Peer-reported filenames, formats, and availability. "
                         "Album counts describe matching files returned during the search window. "
                         "Edition, completeness, and fidelity require verification."),
    }


def transfer_control(action):
    root = staging_root()
    private_folder(root)
    flag = root / ENABLE_FILE
    if action == "enable":
        audit(root, "local_enable")
        try:
            flag.touch(mode=0o600, exist_ok=False)
        except FileExistsError:
            if not enabled(root):
                raise ValueError("The Spliff transfer enable file is invalid.")
        return {"enabled": True}
    flag.unlink(missing_ok=True)
    try:
        return request("stop_downloads")
    except (ValueError, OSError) as exc:
        audit(root, "stop_unconfirmed", error=str(exc))
        return {"enabled": False, "paused": None,
                "message": "New staging is disabled. Existing transfer pauses were not confirmed. "
                           "Disconnect Nicotine+ before inspecting its queue."}
