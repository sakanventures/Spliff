"""Spliff's client and candidate ranking for the local Nicotine+ bridge."""
import shutil
import socket
from collections import defaultdict
from pathlib import Path, PureWindowsPath

from .nicotine_bridge.wire import (
    REQUEST_BYTES, RESPONSE_BYTES, VERSION, WINDOW_SECONDS, encode, endpoint, read, search_term,
)


def install_plugin(plugins):
    """Install the packaged bridge without replacing another installed plugin."""
    target = plugins.expanduser() / "spliff_search"
    source = Path(__file__).with_name("nicotine_bridge")
    target.mkdir(parents=True)
    try:
        for name in ("wire.py", "plugin.py", "PLUGININFO", "entrypoint.py"):
            shutil.copyfile(source / name, target / ("__init__.py" if name == "entrypoint.py" else name))
    except OSError:
        shutil.rmtree(target)
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


def album_folders(files):
    groups = defaultdict(list)
    for file in files:
        path = PureWindowsPath(file["path"])
        groups[(file["peer"], str(path.parent))].append(file)
    candidates = []
    for (peer, folder), entries in groups.items():
        names = {PureWindowsPath(file["path"]).name for file in entries}
        stems = {PureWindowsPath(name).stem.casefold() for name in names}
        candidates.append({
            "peer": peer, "folder": folder, "matching_files": len(stems),
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
    if kind == "album":
        candidates = album_folders(files)
    else:
        candidates = sorted(files, key=availability)
    return {
        "query": query, "kind": kind, "candidates": candidates[:limit],
        "observed_matches": len(candidates), "search_window_seconds": WINDOW_SECONDS,
        "truncated": response["truncated"] or len(candidates) > limit,
        "verification": ("Peer-reported filenames, formats, and availability. "
                         "Album counts describe matching files returned during the search window. "
                         "Edition, completeness, and fidelity require verification."),
    }
