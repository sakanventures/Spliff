"""One bounded JSON request and response on a same-user Unix socket."""
import json
import os
import stat
from pathlib import Path

VERSION = 1
NICOTINE_VERSION = "3.3.11"
REQUEST_BYTES = 2048
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
