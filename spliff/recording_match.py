"""Bounded local waveform and optional Chromaprint comparison."""
from array import array
from functools import lru_cache
import json
from math import sqrt
from statistics import mean
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


def decode_audio(path, expected, rate):
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
                "-i", f"/dev/fd/{fd}", "-map", "0:a:0", "-ac", "1", "-ar", str(rate),
                "-t", str(MAX_DURATION + 1), "-f", "s16le", "-",
            ], capture_output=True, timeout=45, check=True, pass_fds=(fd,))
        except subprocess.SubprocessError as exc:
            raise ValueError("The audio could not be decoded for recording comparison.") from exc
        if signature(os.fstat(fd)) != expected or signature(Path(path).stat()) != expected:
            raise ValueError("The audio changed during recording comparison.")
    finally:
        os.close(fd)
    if not 8 * rate <= len(result.stdout) // 2 <= MAX_DURATION * rate:
        raise ValueError("Recording comparison supports tracks between 8 seconds and 10 minutes.")
    return result.stdout


def decode(path, expected):
    samples = array("h", decode_audio(path, expected, RATE))
    if sys.byteorder != "little":
        samples.byteswap()
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
                           abs(correlation(reference, candidate[start + offset:start + offset + RATE:4]))))
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
    inverted = sum(scores) < 0
    if inverted:
        scores = [-score for score in scores]
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
        "polarity_inverted": inverted, "offset_ms": offset, "compared_windows": len(scores), "mean_correlation": round(mean, 5),
        "p05_correlation": round(p05, 5), "minimum_correlation": round(worst, 5),
        "unmatched_edge_relative_rms": round(edge_peak, 5),
        "original_ms": len(original), "download_ms": len(candidate),
    }


@lru_cache(maxsize=128)
def chromaprint(binary, path, expected, changed_ns):
    # Only raw PCM from our own pinned, protocol-restricted decoder reaches fpcalc.
    rate = 11025
    audio = decode_audio(path, expected, rate)
    if Path(path).stat().st_ctime_ns != changed_ns:
        raise ValueError("The audio changed before fingerprinting.")
    try:
        response = subprocess.run([
            binary, "-format", "s16le", "-rate", str(rate), "-channels", "1",
            "-length", str(MAX_DURATION + 1), "-raw", "-json", "-",
        ], input=audio, capture_output=True, check=True, timeout=30)
        values = json.loads(response.stdout)["fingerprint"]
        if not isinstance(values, list) or any(type(value) is not int for value in values):
            raise ValueError("Unexpected Chromaprint result.")
    except (OSError, subprocess.SubprocessError, KeyError, ValueError) as exc:
        raise ValueError("Chromaprint could not fingerprint this audio.") from exc
    return tuple(value & 0xFFFFFFFF for value in values)


def compare_fingerprints(left, right):
    if min(len(left), len(right)) < 80:
        return {"matched": False, "reason": "insufficient_fingerprint_coverage"}
    if min(len(set(left)), len(set(right))) < 32:
        return {"matched": False, "reason": "insufficient_fingerprint_variation"}

    def aligned(offset):
        start_left, start_right = max(0, -offset), max(0, offset)
        scores = [1 - bin(a ^ b).count("1") / 32 for a, b in zip(left[start_left:], right[start_right:])]
        return mean(scores), abs(offset), offset, scores

    score, _, offset, scores = max(map(aligned, range(-24, 25)), key=lambda result: (result[0], -result[1]))
    coverage = len(scores) / max(len(left), len(right))
    # Require consistent similarity in every approximately four-second window.
    weakest = min(mean(scores[index:index + 32]) for index in range(0, len(scores) - 31, 8))
    return {"matched": score >= .90 and weakest >= .81 and coverage >= .97
            and max(len(left), len(right)) - len(scores) <= 18,
            "similarity": round(score, 4), "weakest_window": round(weakest, 4),
            "coverage": round(coverage, 4), "offset_frames": offset}


def compare_recordings(original, original_signature, candidate, candidate_signature):
    binary = shutil.which("fpcalc") or shutil.which("fpcalc", path=str(Path(sys.executable).parent))
    stamps = tuple(Path(path).stat().st_ctime_ns for path in (original, candidate))
    result = compare_audio(original, original_signature, candidate, candidate_signature, binary, stamps)
    if tuple(Path(path).stat().st_ctime_ns for path in (original, candidate)) != stamps:
        raise ValueError("The audio changed during recording comparison.")
    return result


@lru_cache(maxsize=128)
def compare_audio(original, original_signature, candidate, candidate_signature, fingerprint_binary, stamps):
    waveform = compare_pcm(decode(original, original_signature), decode(candidate, candidate_signature))
    if waveform["matched"]:
        return {**waveform, "method": "waveform"}
    if not fingerprint_binary:
        return {**waveform, "method": "waveform", "fingerprint_status": "fpcalc_unavailable"}
    try:
        fingerprint = compare_fingerprints(chromaprint(fingerprint_binary, original, original_signature, stamps[0]),
                                           chromaprint(fingerprint_binary, candidate, candidate_signature, stamps[1]))
    except ValueError as exc:
        return {**waveform, "method": "waveform", "fingerprint_status": str(exc)}
    return {**waveform, "matched": fingerprint["matched"], "method": "waveform_and_chromaprint",
            "waveform_matched": False, "fingerprint": fingerprint}
