"""One bounded JSON request and response on a same-user Unix socket."""
import json
import os
import stat
from pathlib import Path, PureWindowsPath

VERSION = 1
NICOTINE_VERSION = "3.3.11"
REQUEST_BYTES = 64 * 1024
RESPONSE_BYTES = 512 * 1024
WINDOW_SECONDS = 8
FORMATS = frozenset({"flac", "wav", "aif", "aiff", "alac", "ape", "wv"})


def endpoint():
    directory = Path("/tmp") / f"spliff-{os.getuid()}"
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Spliff needs a private, owner-only socket directory.")
    return directory / "nicotine.sock"


def encode(payload, maximum):
    line = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(line) > maximum:
        raise ValueError("The Spliff bridge message is too large.")
    return line


def read(stream, maximum):
    line = stream.readline(maximum + 1)
    if not line.endswith(b"\n") or len(line) > maximum:
        raise ValueError("The Spliff bridge returned an incomplete message.")
    payload = json.loads(line)
    if not isinstance(payload, dict):
        raise ValueError("The Spliff bridge message must be an object.")
    return payload


def search_term(query):
    if not isinstance(query, str) or not 2 <= len(query.strip()) <= 180 or not query.isprintable():
        raise ValueError("Enter a search query of 2–180 printable characters.")
    return query.strip()


def staging_root():
    return Path.home() / "Library/Application Support/Spliff/Staged downloads"


def album_key(file):
    return file["peer"], str(PureWindowsPath(file["path"]).parent)


def track_stem(file):
    return PureWindowsPath(file["path"]).stem.casefold()


def album_tracks(files, anchor):
    """Select one advertised file per basename from the same peer and folder."""
    selected = {}
    key = album_key(anchor)
    for file in files:
        if album_key(file) != key:
            continue
        stem = track_stem(file)
        if stem not in selected or file["format"] == "flac":
            selected[stem] = file
    return sorted(selected.values(), key=track_stem)
