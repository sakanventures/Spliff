"""Local, read-only full-track spectrograms using the existing FFmpeg toolchain."""
import base64
import os
import shutil
import math
import re
import stat
import subprocess
from contextlib import closing
from functools import lru_cache
from pathlib import Path
from threading import Lock

from .catalog import connect, find_probe, probe, signature

# Serialize decoding so opening an album cannot spawn many FFmpeg processes.
_DECODE_LOCK = Lock()
METHOD = "Full track · first audio stream · channels combined · Hann window · linear 0–22,050 Hz · −120 to 0 dBFS"



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


def validate_decode(path, expected):
    binary = shutil.which("ffmpeg")
    if not binary:
        raise ValueError("Install FFmpeg to verify downloaded audio.")
    if Path(path).resolve(strict=True) != Path(path):
        raise ValueError("Choose an audio file without symbolic links.")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as source:
        descriptor = source.fileno()
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or signature(before) != tuple(expected):
            raise ValueError("The staged audio changed before integrity verification.")
        try:
            subprocess.run([
                binary, "-hide_banner", "-v", "error", "-nostdin", "-xerror",
                "-protocol_whitelist", "file", "-format_whitelist",
                "mp3,flac,wav,aiff,mov,aac,ogg,ape,wv,asf,dsf,iff",
                "-i", f"/dev/fd/{descriptor}", "-map", "0:a:0", "-f", "null", "-",
            ], check=True, capture_output=True, pass_fds=(descriptor,), timeout=120)
        except subprocess.SubprocessError as exc:
            raise ValueError(f"Integrity verification failed for {Path(path).name}. Stage a fresh copy.") from exc
        if signature(os.fstat(descriptor)) != tuple(expected) or signature(Path(path).stat()) != tuple(expected):
            raise ValueError("The staged audio changed during integrity verification.")


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
            "[0:a:0]asplit[image][metrics];"
            "[image]aresample=44100,showspectrumpic=s=1000x480:legend=1:mode=combined:"
            "color=intensity:scale=log:fscale=lin:win_func=hann:drange=120:limit=0[v];"
            "[metrics]aresample=44100,aformat=channel_layouts=mono,atrim=duration=300,"
            "asetnsamples=n=4096:p=0,aspectralstats=win_size=4096:overlap=0:measure=rolloff,"
            "astats=metadata=1:reset=1:measure_perchannel=none:measure_overall=RMS_level,"
            "ametadata=mode=print:file='pipe\\:2'[m]",
            "-map", "[v]", "-frames:v", "1", "-threads", "1",
            "-c:v", "png", "-f", "image2pipe", "pipe:1", "-map", "[m]", "-f", "null", "-",
        ], capture_output=True, timeout=120, pass_fds=(descriptor,))
        if result.returncode or not result.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("The audio could not be fully decoded for spectral analysis.")
        if signature(os.fstat(descriptor)) != fingerprint:
            raise ValueError("The audio changed during analysis.")
        return {"name": Path(path).name, "audio": audio, "method": METHOD,
                "status": "visual review", "conclusion": "Spectral content alone cannot establish source authenticity.",
                "measurements": measurements(result.stderr),
                "image_base64": base64.b64encode(result.stdout).decode("ascii")}
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Spectral analysis timed out. No library files were changed.") from exc
    except (subprocess.SubprocessError, IndexError, KeyError) as exc:
        raise ValueError("The audio could not be decoded for spectral analysis.") from exc
    finally:
        os.close(descriptor)


def measurements(output):
    bands = [float(value) for value in re.findall(rb"lavfi.aspectralstats.1.rolloff=([0-9.]+)", output)]
    levels = [max(-90.0, float(value)) for value in re.findall(
        rb"lavfi.astats.Overall.RMS_level=(-inf|-?[0-9.]+)", output)]
    sorted_bands = sorted(bands)
    return {"rolloff_p95_hz": round(sorted_bands[int(.95 * (len(sorted_bands) - 1))]) if sorted_bands else None,
            "levels": levels}


def compare_measurements(original, candidate):
    if not original.get("measurements") or not candidate.get("measurements"):
        return {"status": "Acoustic comparison unavailable"}
    left, right = (item["measurements"]["levels"] for item in (original, candidate))
    evidence = {"original_rolloff_p95_hz": original["measurements"]["rolloff_p95_hz"],
                "download_rolloff_p95_hz": candidate["measurements"]["rolloff_p95_hz"]}
    if not right or max(right) <= -85:
        return {**evidence, "status": "Download appears silent" if right else "Acoustic comparison unavailable"}
    score = None
    # About 93 ms per frame. Search shifts up to two seconds to tolerate encoder
    # delay and different leading silence. Compare the same temporal passage.
    if min(len(left), len(right)) >= 215:
        for offset in range(-21, 22):
            start_left, start_right = max(0, -offset), max(0, offset)
            length = min(len(left) - start_left, len(right) - start_right)
            a = left[start_left:start_left + length]
            b = right[start_right:start_right + length]
            ma, mb = sum(a) / length, sum(b) / length
            av = [level - ma for level in a]
            bv = [level - mb for level in b]
            energy_a, energy_b = (sum(level * level for level in values) for values in (av, bv))
            if min(energy_a, energy_b) < length:
                continue
            correlation = sum(x * y for x, y in zip(av, bv)) / math.sqrt(energy_a * energy_b)
            score = max(score, correlation) if score is not None else correlation
    if score is None:
        return {**evidence, "status": "Insufficient dynamics or duration for acoustic comparison"}
    return {**evidence, "envelope_similarity": round(score, 3),
            "status": ("Similar audio envelope" if score >= .82 else "Different audio envelope"
                       if score < .55 else "Inconclusive audio envelope")}


def analyze_track(db, track_id=None):
    if type(track_id) is not int or not 0 < track_id < 2**63:
        raise ValueError("Choose an indexed track.")
    with closing(connect(db)) as connection:
        row = connection.execute("SELECT * FROM tracks WHERE id=? AND present=1 AND error IS NULL",
                                 (track_id,)).fetchone()
    if row is None:
        raise ValueError("Choose an available, indexed track.")
    result = analyze_file(Path(row["root"]) / row["path"],
                          tuple(row[key] for key in ("size", "mtime", "device", "inode")))
    result["measurements"] = {key: value for key, value in result["measurements"].items() if key != "levels"}
    return result


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
    result["download"]["measurements"] = {key: value for key, value in
                                          result["download"]["measurements"].items() if key != "levels"}
    if item.get("original"):
        result["original"] = analyze_track(db, item["original"]["id"])
    return result
