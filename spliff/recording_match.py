"""Bounded PCM comparison for already selected tracks with a metadata discrepancy."""
from array import array
from functools import lru_cache
from math import sqrt
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

from .catalog import signature

RATE = 1000
MAX_OFFSET = 3 * RATE
MAX_DURATION = 600


def decode(path, expected):
    binary = shutil.which("ffmpeg")
    if not binary:
        raise ValueError("FFmpeg is required for recording comparison.")
    if Path(path).resolve(strict=True) != Path(path):
        raise ValueError("Recording comparison requires a regular file without symbolic links.")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or signature(info) != expected:
            raise ValueError("The audio changed before recording comparison.")
        try:
            result = subprocess.run([
                binary, "-nostdin", "-v", "error", "-xerror", "-protocol_whitelist", "file",
                "-format_whitelist", "mp3,flac,wav,aiff,mov,aac,ogg,ape,wv,asf,dsf,iff",
                "-i", f"/dev/fd/{fd}", "-map", "0:a:0", "-ac", "1", "-ar", str(RATE),
                "-t", str(MAX_DURATION + 1), "-f", "s16le", "-",
            ], capture_output=True, timeout=45, check=True, pass_fds=(fd,))
        except subprocess.SubprocessError as exc:
            raise ValueError("The audio could not be decoded for recording comparison.") from exc
        if signature(os.fstat(fd)) != expected or signature(Path(path).stat()) != expected:
            raise ValueError("The audio changed during recording comparison.")
    finally:
        os.close(fd)
    samples = array("h", result.stdout)
    if sys.byteorder != "little":
        samples.byteswap()
    if not 8 * RATE <= len(samples) <= MAX_DURATION * RATE:
        raise ValueError("Recording comparison supports tracks between 8 seconds and 10 minutes.")
    return samples


def correlation(left, right):
    product = sum(a * b for a, b in zip(left, right))
    power = sum(a * a for a in left) * sum(b * b for b in right)
    return product / sqrt(power) if power else 0.0


def rms(samples):
    return sqrt(sum(sample * sample for sample in samples) / len(samples)) if samples else 0.0


def compare_pcm(original, candidate):
    length = min(len(original), len(candidate))
    if length < 8 * RATE or abs(len(original) - len(candidate)) > MAX_OFFSET:
        return {"matched": False, "reason": "Duration exceeds the recording alignment window."}

    # Estimate the offset independently at several points. A median resists an
    # isolated match on a repeated beat. Validate the offset over the full overlap.
    offsets = []
    for fraction in (0.15, 0.35, 0.55, 0.75, 0.90):
        start = max(MAX_OFFSET, min(int(length * fraction), length - MAX_OFFSET - RATE))
        reference = original[start:start + RATE:4]
        offsets.append(max(range(-MAX_OFFSET, MAX_OFFSET + 1), key=lambda offset:
                           correlation(reference, candidate[start + offset:start + offset + RATE:4])))
    offset = sorted(offsets)[len(offsets) // 2]
    start = max(0, -offset)
    end = min(len(original), len(candidate) - offset)
    level = min(rms(original[start:end]), rms(candidate[start + offset:end + offset]))
    scores = []
    for position in range(start, end, RATE):
        stop = min(position + RATE, end)
        left = original[position:stop]
        right = candidate[position + offset:stop + offset]
        if len(left) >= RATE // 4 and max(rms(left), rms(right)) > max(20, level * 0.03):
            scores.append(correlation(left, right))
    scores.sort()
    mean = sum(scores) / len(scores) if scores else 0
    worst = scores[0] if scores else 0
    p05 = scores[len(scores) // 20] if scores else 0
    edges = (original[:start], original[end:], candidate[:start + offset], candidate[end + offset:])
    edge_peak = max((rms(edge[i:i + 100]) / max(1, level)
                     for edge in edges for i in range(0, len(edge), 100)), default=0)
    return {
        "matched": (level >= 50 and len(scores) >= max(6, (end - start) // (2 * RATE))
                    and mean >= 0.94 and p05 >= 0.88 and worst >= 0.70 and edge_peak <= 0.025),
        "offset_ms": offset, "compared_windows": len(scores), "mean_correlation": round(mean, 5),
        "p05_correlation": round(p05, 5), "minimum_correlation": round(worst, 5),
        "unmatched_edge_relative_rms": round(edge_peak, 5),
        "original_ms": len(original), "download_ms": len(candidate),
    }


@lru_cache(maxsize=24)
def compare_recordings(original, original_signature, candidate, candidate_signature):
    return compare_pcm(decode(original, original_signature), decode(candidate, candidate_signature))
