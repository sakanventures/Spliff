"""Local, read-only full-track spectrograms using the existing FFmpeg toolchain."""
import base64
import os
import shutil
import sqlite3
import math
import re
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from functools import lru_cache
from pathlib import Path
from threading import BoundedSemaphore
from uuid import uuid4

from .catalog import connect, find_probe, probe, signature

# Share the decoding limit across simultaneous dashboard and MCP requests.
SPECTRAL_WORKERS = 2
_DECODE_LIMIT = BoundedSemaphore(SPECTRAL_WORKERS)
BAND_EDGES = (12000, 16000, 18000, 20000)
METHOD = "Full track · first audio stream · channels combined · Hann window · linear 0–22,050 Hz · −120 to 0 dBFS"



def analyze_file(path, expected=None, *, render_image=True):
    path = Path(path)
    if path.resolve(strict=True) != path:
        raise ValueError("Choose an audio file without symbolic links.")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Choose a regular audio file.")
    fingerprint = signature(info)
    if expected is not None and tuple(expected) != fingerprint:
        raise ValueError("The audio changed. Scan or review it again.")
    with _DECODE_LIMIT:
        result = _render(str(path), fingerprint, info.st_ctime_ns, render_image)
    after = path.stat()
    if signature(after) != fingerprint or after.st_ctime_ns != info.st_ctime_ns:
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
def _render(path, fingerprint, changed_ns, render_image):
    binary = shutil.which("ffmpeg")
    if not binary:
        raise ValueError("Install FFmpeg to use spectral analysis.")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or signature(info) != fingerprint or info.st_ctime_ns != changed_ns:
            raise ValueError("The audio changed before analysis.")
        audio = probe(find_probe(), descriptor)
        if not audio.get("duration") or audio["duration"] > 3600:
            raise ValueError("Spectral analysis supports tracks up to one hour.")
        os.lseek(descriptor, 0, os.SEEK_SET)
        graph, outputs = spectrum_filters(render_image)
        result = subprocess.run([
            binary, "-hide_banner", "-v", "info" if render_image else "error", "-nostats",
            "-nostdin", "-xerror", "-filter_complex_threads", "1",
            "-protocol_whitelist", "file", "-format_whitelist",
            "mp3,flac,wav,aiff,mov,aac,ogg,ape,wv,asf,dsf,iff",
            "-i", f"/dev/fd/{descriptor}", "-filter_complex", graph, *outputs,
        ], capture_output=True, timeout=120, pass_fds=(descriptor,))
        if result.returncode or (render_image and not result.stdout.startswith(b"\x89PNG\r\n\x1a\n")):
            raise ValueError("The audio could not be fully decoded for spectral analysis.")
        after = os.fstat(descriptor)
        if signature(after) != fingerprint or after.st_ctime_ns != changed_ns:
            raise ValueError("The audio changed during analysis.")
        summary = {"name": Path(path).name, "audio": audio, "measurements": measurements(result.stderr)}
        if render_image:
            summary.update(method=METHOD, status="visual review",
                           conclusion="Spectral content alone cannot establish source authenticity.",
                           image_base64=base64.b64encode(result.stdout).decode("ascii"))
        return summary
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Spectral analysis timed out. No library files were changed.") from exc
    except (subprocess.SubprocessError, IndexError, KeyError) as exc:
        raise ValueError("The audio could not be decoded for spectral analysis.") from exc
    finally:
        os.close(descriptor)


def spectrum_filters(render_image):
    metrics = (
        "aresample=44100,aformat=channel_layouts=mono,"
        "asetnsamples=n=4096:p=0,aspectralstats=win_size=4096:overlap=0:measure=rolloff,"
        "astats=metadata=1:reset=1:measure_perchannel=none:measure_overall=RMS_level,"
        "ametadata=mode=print:file='pipe\\:2'[m]"
    )
    if not render_image:
        return "[0:a:0]" + metrics, ["-map", "[m]", "-f", "null", "-"]
    bands = len(BAND_EDGES) + 1
    band_outputs = "".join(f"[band{i}]" for i in range(bands))
    crossovers = " ".join(map(str, BAND_EDGES))
    meters = "".join(f";[band{i}]astats@band{i}=measure_perchannel=none:measure_overall=RMS_level[b{i}]"
                     for i in range(bands))
    band_maps = [part for i in range(bands) for part in ("-map", f"[b{i}]")]
    graph = (
        "[0:a:0]asplit=3[image][metrics][bands];"
        "[image]aresample=44100,showspectrumpic=s=1000x480:legend=1:mode=combined:"
        "color=intensity:scale=log:fscale=lin:win_func=hann:drange=120:limit=0[v];"
        f"[metrics]atrim=duration=300,{metrics};"
        f"[bands]aresample=44100,atrim=duration=120,acrossover=split={crossovers}:order=12th"
        f"{band_outputs}{meters}"
    )
    return graph, ["-map", "[v]", "-frames:v", "1", "-threads", "1", "-c:v", "png", "-f", "image2pipe",
                   "pipe:1", "-map", "[m]", *band_maps, "-f", "null", "-"]


def measurements(output):
    bands = [float(value) for value in re.findall(rb"lavfi.aspectralstats.1.rolloff=([0-9.]+)", output)]
    levels = [max(-90.0, float(value)) for value in re.findall(
        rb"lavfi.astats.Overall.RMS_level=(-inf|-?[0-9.]+)", output)]
    max_frames = round(300 * 44100 / 4096)
    levels = levels[:max_frames]
    sorted_bands = sorted(bands[:max_frames])
    rolloff = round(sorted_bands[int(.95 * (len(sorted_bands) - 1))]) if sorted_bands else None
    band_levels = {int(index): max(-120.0, float(value)) for index, value in re.findall(
        rb"\[astats@band(\d+) .*?RMS level dB: (-inf|-?[0-9.]+)", output)}
    drops = [frequency for index, frequency in enumerate(BAND_EDGES, 1)
             if index > 1 and index in band_levels and index - 1 in band_levels
             and band_levels[index - 1] > -64 and band_levels[index - 1] - band_levels[index] >= 24
             and rolloff is not None and .8 * frequency <= rolloff <= frequency]
    return {"rolloff_p95_hz": rolloff, "levels": levels,
            "band_rms_dbfs": [round(band_levels[index], 1) if index in band_levels else None
                              for index in range(len(BAND_EDGES) + 1)],
            "band_edges_hz": BAND_EDGES, "possible_low_pass_hz": drops}


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


def indexed_spectrum(db, track_id):
    if type(track_id) is not int or not 0 < track_id < 2**63:
        raise ValueError("Choose an indexed track.")
    with closing(connect(db)) as connection:
        row = connection.execute("SELECT * FROM tracks WHERE id=? AND present=1 AND error IS NULL",
                                 (track_id,)).fetchone()
    if row is None:
        raise ValueError("Choose an available, indexed track.")
    return analyze_file(Path(row["root"]) / row["path"],
                        tuple(row[key] for key in ("size", "mtime", "device", "inode")))


def analyze_track(db, track_id=None):
    result = indexed_spectrum(db, track_id)
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


def album_track_summary(db, index, item):
    result = {"index": index, "name": item["name"], "original_track_id": (item.get("original") or {}).get("id"),
              "recording_assessment": item.get("assessment")}
    errors = {}
    reports = {}
    if item.get("source"):
        try:
            reports["download"] = analyze_file(item["source"], item["fingerprint"])
        except (OSError, ValueError) as exc:
            errors["download"] = str(exc)
    else:
        errors["download"] = item.get("assessment") or "Track has not finished downloading."
    if result["original_track_id"] is not None:
        try:
            reports["original"] = indexed_spectrum(db, result["original_track_id"])
        except (OSError, ValueError) as exc:
            errors["original"] = str(exc)
    for label, report in reports.items():
        metrics = report["measurements"]
        result[label] = {key: metrics[key] for key in ("rolloff_p95_hz", "possible_low_pass_hz", "band_rms_dbfs")}
    if {"original", "download"} <= reports.keys():
        result["comparison"] = compare_measurements(reports["original"], reports["download"])
    if errors:
        result["errors"] = errors
    return result


def analyze_album(db, candidate_id=None, original_track_id=None, *, start=0, limit=60):
    """Batch spectra and keep the images out of the album-level response."""
    from .staged_downloads import review_download
    if type(start) is not int or start < 0 or type(limit) is not int or not 1 <= limit <= 60:
        raise ValueError("Choose a nonnegative start index and a limit between 1 and 60.")
    review = review_download(db, candidate_id, original_track_id, include_sources=True)
    page = list(enumerate(review["items"][start:start + limit], start))
    with ThreadPoolExecutor(max_workers=SPECTRAL_WORKERS) as executor:
        rows = list(executor.map(lambda entry: album_track_summary(db, *entry), page))
    return {"candidate_id": candidate_id, "track_count": len(review["items"]), "tracks": rows,
            "analyzed_downloads": sum("download" in row for row in rows),
            "compared_originals": sum("original" in row and "download" in row for row in rows),
            "possible_low_pass_downloads": sum(bool(row.get("download", {}).get("possible_low_pass_hz"))
                                               for row in rows),
            "failed_or_incomplete": sum("errors" in row for row in rows),
            "next_index": start + limit if start + limit < len(review["items"]) else None,
            "band_edges_hz": BAND_EDGES, "advanced_tool": "analyze_download_spectrum",
            "interpretation": "Abrupt band-energy drops can reflect filtering, mastering, or previous encoding. "
                              "Spectral similarity or limited bandwidth does not establish a lossy transcode."}


class AlbumJobs:
    """Queue bounded, read-only spectral work without holding an MCP response open."""

    def __init__(self, db):
        self.db = db
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="spliff-album-spectrum")
        self.jobs = {}

    def start(self, candidate_id, original_track_id=None, *, start=0, limit=60):
        from .nicotine_bridge.downloads import selection_parts
        selection_parts(candidate_id)
        key = (candidate_id, original_track_id, start, limit)
        if (type(start) is not int or start < 0 or type(limit) is not int or not 1 <= limit <= 60
                or original_track_id is not None and (type(original_track_id) is not int or not 0 < original_track_id < 2**63)):
            raise ValueError("Choose a valid original track, start index, and limit.")
        active = [(job, scope) for job, (future, scope) in self.jobs.items() if not future.done()]
        for job, scope in active:
            if scope == key:
                return self.status(job)
        if len(active) >= 4:
            raise ValueError("Four album analyses are pending. Check their status before starting another.")
        if len(self.jobs) >= 16:
            oldest_completed = next(job for job, (future, _) in self.jobs.items() if future.done())
            del self.jobs[oldest_completed]
        job = uuid4().hex
        self.jobs[job] = (self.executor.submit(analyze_album, self.db, candidate_id, original_track_id,
                                               start=start, limit=limit), key)
        return self.status(job)

    def status(self, job_id):
        if not isinstance(job_id, str) or job_id not in self.jobs:
            raise ValueError("This spectral job is unavailable. Start a fresh album analysis.")
        future, scope = self.jobs[job_id]
        state = {"job_id": job_id, "candidate_id": scope[0]}
        if not future.done():
            return {**state, "status": "running" if future.running() else "queued"}
        try:
            result = future.result()
        except (OSError, ValueError, sqlite3.Error) as exc:
            return {**state, "status": "failed", "reason": str(exc)}
        return {**state, **result, "status": "complete"}
