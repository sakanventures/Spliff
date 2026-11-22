"""Spliff's client and candidate ranking for the local Nicotine+ bridge."""
import shutil
import socket
from collections import defaultdict
from pathlib import Path, PureWindowsPath
from tempfile import TemporaryDirectory

from .nicotine_bridge.wire import (
    album_key, track_stem,
    REQUEST_BYTES, RESPONSE_BYTES, VERSION, WINDOW_SECONDS, encode, endpoint, read, search_term,
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
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(2)
            connection.connect(str(path))
            connection.sendall(payload)
            connection.settimeout(WINDOW_SECONDS + 6)
            with connection.makefile("rb") as stream:
                response = read(stream, RESPONSE_BYTES)
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        raise ValueError("Open Nicotine+ and enable the Spliff Search plugin in its preferences.") from exc
    except OSError as exc:
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
        names = {PureWindowsPath(file["path"]).name for file in entries}
        stems = {track_stem(file) for file in entries}
        candidates.append({
            "peer": peer, "folder": folder, "matching_files": len(stems),
            **({"candidate_id": f"album-{search_id}-{index}"} if search_id else {}),
            "formats": sorted({file["format"] for file in entries}),
            "free_slot": entries[0]["free_slot"], "queue": entries[0]["queue"],
            "filenames": sorted(names, key=lambda name: (name.casefold(), name))[:40],
        })
    return sorted(candidates, key=lambda item: (-item["matching_files"], *availability(item)))


def search(_db, query=None, kind="track", limit=3):
    query = search_term(query)
    if kind not in ("track", "album"):
        raise ValueError("Choose a track or album search.")
    if type(limit) is not int or not 1 <= limit <= 10:
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
    return {
        "query": query, "kind": kind, "candidates": candidates[:limit],
        "observed_matches": len(candidates), "search_window_seconds": WINDOW_SECONDS,
        "truncated": response["truncated"] or len(candidates) > limit,
        "verification": ("Peer-reported filenames, formats, and availability. "
                         "Album counts describe matching files returned during the search window. "
                         "Edition, completeness, and fidelity require verification."),
    }
