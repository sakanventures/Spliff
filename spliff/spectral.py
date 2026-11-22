"""Local, read-only full-track spectrograms using the existing FFmpeg toolchain."""
import base64
import os
import shutil
import stat
import subprocess
from contextlib import closing
from functools import lru_cache
from pathlib import Path
from threading import Lock

from .catalog import connect, find_probe, probe, signature

# Serialize decoding so opening an album cannot spawn many FFmpeg processes.
_DECODE_LOCK = Lock()
METHOD = "Full track · first audio stream · channels combined · Hann window · linear Hz · −120 to 0 dBFS"


def analyze_file(path, expected=None):
    path = Path(path)
    if path.resolve(strict=True) != path:
        raise ValueError("Choose an audio file without symbolic links.")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Choose a regular audio file.")
    fingerprint = signature(info)
    if expected is not None and tuple(expected) != fingerprint:
        raise ValueError("The audio changed. Scan or review it again.")
    with _DECODE_LOCK:
        result = _render(str(path), fingerprint)
    if signature(path.stat()) != fingerprint:
        raise ValueError("The audio changed during analysis.")
    return dict(result)


@lru_cache(maxsize=128)
def _render(path, fingerprint):
    binary = shutil.which("ffmpeg")
    if not binary:
        raise ValueError("Install FFmpeg to use spectral analysis.")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or signature(info) != fingerprint:
            raise ValueError("The audio changed before analysis.")
        audio = probe(find_probe(), descriptor)
        if not audio.get("duration") or audio["duration"] > 3600:
            raise ValueError("Spectral analysis supports tracks up to one hour.")
        os.lseek(descriptor, 0, os.SEEK_SET)
        result = subprocess.run([
            binary, "-hide_banner", "-v", "error", "-nostdin", "-xerror",
            "-protocol_whitelist", "file", "-format_whitelist",
            "mp3,flac,wav,aiff,mov,aac,ogg,ape,wv,asf,dsf,iff",
            "-i", f"/dev/fd/{descriptor}", "-filter_complex",
            "[0:a:0]showspectrumpic=s=1000x480:legend=1:mode=combined:"
            "color=intensity:scale=log:fscale=lin:win_func=hann:drange=120:limit=0[v]",
            "-map", "[v]", "-frames:v", "1", "-threads", "1",
            "-c:v", "png", "-f", "image2pipe", "pipe:1",
        ], capture_output=True, timeout=120, pass_fds=(descriptor,))
        if result.returncode or not result.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("The audio could not be fully decoded for spectral analysis.")
        if signature(os.fstat(descriptor)) != fingerprint:
            raise ValueError("The audio changed during analysis.")
        return {"name": Path(path).name, "audio": audio, "method": METHOD,
                "status": "visual review", "conclusion": "Spectral content alone cannot establish source authenticity.",
                "image_base64": base64.b64encode(result.stdout).decode("ascii")}
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Spectral analysis timed out. No library files were changed.") from exc
    except (subprocess.SubprocessError, IndexError, KeyError) as exc:
        raise ValueError("The audio could not be decoded for spectral analysis.") from exc
    finally:
        os.close(descriptor)


def analyze_track(db, track_id=None):
    if type(track_id) is not int or not 0 < track_id < 2**63:
        raise ValueError("Choose an indexed track.")
    with closing(connect(db)) as connection:
        row = connection.execute("SELECT * FROM tracks WHERE id=? AND present=1 AND error IS NULL",
                                 (track_id,)).fetchone()
    if row is None:
        raise ValueError("Choose an available, indexed track.")
    return analyze_file(Path(row["root"]) / row["path"],
                        tuple(row[key] for key in ("size", "mtime", "device", "inode")))


def analyze_download(db, candidate_id=None, index=0, original_track_id=None):
    from .staged_downloads import review_download
    if type(index) is not int or index < 0:
        raise ValueError("Choose a downloaded track.")
    report = review_download(db, candidate_id, original_track_id, include_sources=True)
    if index >= len(report["items"]):
        raise ValueError("Choose a downloaded track.")
    item = report["items"][index]
    if not item.get("source"):
        raise ValueError("Wait for this track to finish downloading and pass inspection.")
    result = {"download": analyze_file(item["source"], item["fingerprint"])}
    if item.get("original"):
        result["original"] = analyze_track(db, item["original"]["id"])
    return result
