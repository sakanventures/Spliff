"""Local dashboard and explicitly requested catalog actions."""
import json
import secrets
import shutil
import subprocess
from contextlib import closing
from tempfile import TemporaryDirectory
from threading import Event, Lock, Thread
from time import monotonic
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .catalog import albums, connect, file_metadata_preference, replacement_preference, stats, tracks
from . import keep_both, library_import, sync
from .nicotine import install_plugin, search, transfer_control, transfer_readiness
from .review import protect, reviews, stage
from .spectral import analyze_track, analyze_download
from .artist import library as artist_library, upgrades as artist_upgrades
from .staged_downloads import list_downloads, queue, review_download

MAX_CANDIDATE_BYTES = 512 * 1024 * 1024
STATIC = Path(__file__).with_name("static")
ASSETS = {"/": ("index.html", "text/html"), "/app.css": ("app.css", "text/css"),
          "/app.js": ("app.js", "text/javascript")}
ASSETS.update({
    f"/fonts/poppins-{subset}-{weight}.woff2": (f"fonts/poppins-{subset}-{weight}.woff2", "font/woff2")
    for subset in ("latin", "latin-ext")
    for weight in (400, 500, 600, 700)
})

def serve(db, port):
    scan_lock = Lock()
    scan_state = None
    approval_lock = Lock()
    approvals = {}
    import_editions = {}
    picked_roots = {}

    def issue_approval(selection, payload=None):
        now = monotonic()
        with approval_lock:
            for key, (expiry, _, _) in list(approvals.items()):
                if expiry <= now:
                    del approvals[key]
            if len(approvals) >= 256:
                del approvals[next(iter(approvals))]
            approval = secrets.token_urlsafe(32)
            approvals[approval] = (now + 15 * 60, selection, payload)
            return approval

    def consume_approval(approval, selection):
        with approval_lock:
            issued = approvals.get(approval)
            if not issued or issued[0] <= monotonic() or issued[1] != selection:
                if selection[0] == "import":
                    raise ValueError("Review the local import plan again before choosing Add to library.")
                raise ValueError("Open the local upgrade review again and choose one of its displayed plans.")
            del approvals[approval]
            return issued[2]

    def start_scan(folders):
        nonlocal scan_state
        roots = sync.selected_folders(folders)
        with scan_lock:
            if scan_state and scan_state["current"] is not None:
                raise ValueError("A folder scan is already running.")
            sync.remember_folders(db, roots)
            scan_id = scan_state["id"] + 1 if scan_state else 1
            scan_state = {"id": scan_id, "completed": 0, "total": len(roots),
                          "current": str(roots[0]), "failures": []}

        def progress(completed, current, failures):
            nonlocal scan_state
            with scan_lock:
                scan_state = {"id": scan_id, "completed": completed, "total": len(roots),
                              "current": current, "failures": list(failures)}

        Thread(target=sync.start_many, args=(db, roots, progress), daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get("Host") != f"127.0.0.1:{server.server_port}":
                self.send_error(403)
                return
            try:
                url = urlsplit(self.path)
                params = {key: values[-1] for key, values in
                          parse_qs(url.query, max_num_fields=8).items()}
                if url.path in ASSETS:
                    filename, mime = ASSETS[url.path]
                    self.reply((STATIC / filename).read_bytes(), mime)
                elif url.path == "/api/tracks":
                    self.reply_json(tracks(db, params.get("q", ""), params.get("scope", "all"),
                                           after=int(params.get("after", 0))))
                elif url.path == "/api/summary":
                    self.reply_json(stats(db))
                elif url.path == "/api/transfer-readiness":
                    self.reply_json(transfer_readiness())
                elif url.path == "/api/replacement-preference":
                    self.reply_json(replacement_preference(db))
                elif url.path == "/api/file-metadata-preference":
                    self.reply_json(file_metadata_preference(db))
                elif url.path == "/api/albums":
                    self.reply_json(albums(db, params.get("q", "")))
                elif url.path == "/api/artist-library":
                    self.reply_json(artist_library(db, params.get("artist"), int(params.get("after", 0))))
                elif url.path == "/api/downloads":
                    self.reply_json({"items": list_downloads(db)})
                elif url.path == "/api/scan-progress":
                    with scan_lock:
                        progress = scan_state
                    self.reply_json({"scan": progress})
                elif url.path == "/api/sync":
                    self.reply_json(sync.status(db))
                elif url.path == "/api/download-review":
                    original = int(params["original"]) if params.get("original") else None
                    try:
                        self.reply_json(review_download(db, params.get("candidate"), original))
                    except ValueError as exc:
                        self.reply_json({"error": str(exc)}, 400)
                elif url.path == "/api/upgrade-options":
                    self.reply_json(keep_both.review_plans(db, params.get("candidate"), int(params.get("original", 0)),
                                                         refresh=params.get("refresh") == "1"))
                elif url.path == "/api/keep-both":
                    try:
                        self.reply_json(keep_both.preview(db, params.get("candidate"), int(params.get("original", 0)),
                                                          params.get("mode", "keep")))
                    except (FileNotFoundError, ValueError) as exc:
                        self.reply_json({"error": str(exc)}, 400)
                elif url.path == "/api/reviews":
                    self.reply_json({"items": reviews(db)})
                elif url.path == "/api/spectrum":
                    try:
                        if params.get("candidate"):
                            self.reply_json(analyze_download(db, params["candidate"],
                                int(params.get("index", 0)),
                                int(params["original"]) if params.get("original") else None))
                        else:
                            self.reply_json({"original": analyze_track(db, int(params.get("track", 0)))})
                    except (ValueError, OSError, subprocess.SubprocessError) as exc:
                        self.reply_json({"error": str(exc) if isinstance(exc, ValueError)
                                         else "Spectral analysis unavailable. No files were changed."}, 422)
                else:
                    self.send_error(404)
            except ValueError:
                self.reply_json({"error": "Invalid search parameters"}, 400)
            except (OSError, sqlite3.Error):
                self.reply_json({"error": "Catalog or staging unavailable"}, 503)

        def do_POST(self):
            host = f"127.0.0.1:{server.server_port}"
            if (self.headers.get("Host") != host or self.headers.get("Origin") != f"http://{host}"
                    or self.headers.get("X-Spliff-Action") != "dashboard"):
                self.reply_json({"error": "Open the local Spliff dashboard to make changes."}, 403)
                return
            try:
                parts = urlsplit(self.path).path.strip("/").split("/")
                if parts == ["api", "transfer-permission"]:
                    self.body_length(0)
                    params = parse_qs(urlsplit(self.path).query, max_num_fields=1)
                    action = params.get("action", [None])[0]
                    try:
                        result = transfer_control(action)
                    except OSError:
                        raise ValueError("Spliff could not save download permission. Try again or contact Spliff support.")
                    self.reply_json(result)
                    return
                if parts == ["api", "install-nicotine-bridge"]:
                    self.body_length(0)
                    try:
                        install_plugin(Path.home() / ".local/share/nicotine/plugins", upgrade=True)
                    except OSError:
                        raise ValueError("Spliff could not install Spliff Search. Try again or contact Spliff support.")
                    self.reply_json({"message": "Quit and reopen Nicotine+, then enable Spliff Search in Settings → Plugins."})
                    return
                if parts == ["api", "upgrade-options"]:
                    self.body_length(0)
                    params = parse_qs(urlsplit(self.path).query, max_num_fields=3)
                    candidate = params.get("candidate", [None])[0]
                    original = int(params.get("original", [0])[0])
                    plans = keep_both.review_plans(db, candidate, original,
                                                  refresh=params.get("refresh") == ["1"])
                    for mode, plan in plans["options"].items():
                        plan["approval"] = issue_approval((candidate, original, plan["token"], mode))
                    self.reply_json(plans)
                    return
                if parts == ["api", "import-options"]:
                    self.body_length(0)
                    params = parse_qs(urlsplit(self.path).query, max_num_fields=1)
                    candidate = params.get("candidate", [None])[0]
                    options = library_import.options(db, candidate)
                    with approval_lock:
                        if candidate not in import_editions and len(import_editions) >= 32:
                            del import_editions[next(iter(import_editions))]
                        import_editions[candidate] = {item["id"]: item for item in options["editions"]}
                    self.reply_json(options)
                    return
                if parts == ["api", "import-root"]:
                    self.body_length(0)
                    folders = self.pick_folders(single=True)
                    if not folders:
                        self.reply_json({"cancelled": True})
                        return
                    root, identity = library_import.root_identity(db, folders[0])
                    with approval_lock:
                        picked_roots[root] = identity
                    self.reply_json({"root": root})
                    return
                if parts == ["api", "import-plan"]:
                    data = json.loads(self.rfile.read(self.body_length(16 * 1024)))
                    if (not isinstance(data, dict) or not isinstance(data.get("root"), str)
                            or not isinstance(data.get("candidate"), str)):
                        raise ValueError("Choose a local music library folder and release details.")
                    candidate, root = data.get("candidate"), data["root"]
                    with closing(connect(db)) as connection:
                        identity = library_import.catalog_roots(connection).get(root)
                    edition_id = data.get("edition")
                    with approval_lock:
                        identity = picked_roots.get(root) or identity
                        edition = import_editions.get(candidate, {}).get(edition_id) if type(edition_id) is int else None
                    if not identity:
                        raise ValueError("Choose the music library folder on your Mac.")
                    if edition_id is not None and not edition:
                        raise ValueError("Reopen the import to refresh your Discogs edition choices.")
                    details = {key: data.get(key) for key in ("artist", "title", "year", "category", "posthumous")}
                    plan = library_import.prepare(db, candidate, root, identity, details, edition)
                    preview = library_import.preview(plan)
                    preview["approval"] = issue_approval(("import", candidate, preview["token"]), plan)
                    self.reply_json(preview)
                    return
                if parts == ["api", "import-apply"]:
                    self.body_length(0)
                    params = parse_qs(urlsplit(self.path).query, max_num_fields=2)
                    selection = ("import", params.get("candidate", [None])[0], params.get("token", [None])[0])
                    plan = consume_approval(self.headers.get("X-Spliff-Approval"), selection)
                    self.reply_json(library_import.apply(db, plan))
                    return
                if parts == ["api", "replacement-preference"]:
                    self.body_length(0)
                    params = parse_qs(urlsplit(self.path).query, max_num_fields=1)
                    self.reply_json(replacement_preference(db, params.get("destination", [""])[0]))
                    return
                if parts == ["api", "file-metadata-preference"]:
                    self.body_length(0)
                    params = parse_qs(urlsplit(self.path).query, max_num_fields=1)
                    self.reply_json(file_metadata_preference(db, params.get("mode", [""])[0]))
                    return
                if parts == ["api", "keep-both"]:
                    self.body_length(0)
                    params = parse_qs(urlsplit(self.path).query, max_num_fields=4)
                    selection = (params.get("candidate", [None])[0],
                        int(params.get("original", [0])[0]), params.get("token", [None])[0],
                        params.get("mode", ["keep"])[0])
                    consume_approval(self.headers.get("X-Spliff-Approval"), selection)
                    self.reply_json(keep_both.apply(db, *selection))
                    return
                if parts == ["api", "downloads"]:
                    self.body_length(0)
                    params = parse_qs(urlsplit(self.path).query, max_num_fields=2)
                    selection = params.get("candidate", [None])[0]
                    original = params.get("original", [None])[0]
                    self.reply_json(queue(db, selection, original_id=int(original) if original else None, source="dashboard"), 201)
                    return
                if parts == ["api", "album-search"]:
                    self.body_length(0)
                    query = parse_qs(urlsplit(self.path).query, max_num_fields=1).get("q", [""])[0]
                    self.reply_json(search(db, query, kind="album", limit=None))
                    return
                if parts == ["api", "artist-upgrades"]:
                    self.body_length(0)
                    params = parse_qs(urlsplit(self.path).query, max_num_fields=2)
                    self.reply_json(artist_upgrades(db, params.get("artist", [None])[0],
                                                   int(params.get("after", [0])[0])))
                    return
                if parts == ["api", "scan"]:
                    length = self.body_length(1024 * 1024)
                    folders = json.loads(self.rfile.read(length)) if length else self.pick_folders()
                    if folders is None and not length:
                        self.reply_json({"cancelled": True})
                    else:
                        start_scan(folders)
                        self.reply_json({"started": True}, 202)
                    return
                if len(parts) != 4 or parts[:2] != ["api", "tracks"]:
                    self.reply_json({"error": "Unknown action."}, 404)
                    return
                track_id = int(parts[2])
                if not 0 < track_id < 2**63:
                    raise ValueError("Choose an indexed track.")
                action = parts[3]
                if action in ("protect", "unprotect"):
                    self.body_length(0)
                    protect(db, track_id, action == "protect")
                    self.reply_json({"protected": action == "protect"})
                elif action == "stage":
                    self.upload_candidate(track_id)
                else:
                    self.reply_json({"error": "Unknown action."}, 404)
            except ValueError as exc:
                self.reply_json({"error": str(exc)}, 400)
            except sqlite3.Error:
                self.reply_json({"error": "The catalog is busy or unavailable. Try again."}, 503)
            except (FileNotFoundError, OSError, subprocess.SubprocessError, IndexError, KeyError):
                self.reply_json({"error": "Spliff could not read the selected file or folder. Check access and try again."}, 422)

        def body_length(self, maximum):
            value = self.headers.get("Content-Length")
            if self.headers.get("Transfer-Encoding") or value is None or not value.isascii() or not value.isdigit():
                raise ValueError("The request has an invalid file size.")
            length = int(value)
            if length > maximum:
                raise ValueError("The request exceeds the allowed size.")
            return length

        def upload_candidate(self, track_id):
            name = unquote(self.headers.get("X-Spliff-Filename", ""), errors="strict")
            if (not name or name != Path(name).name or len(name.encode()) > 240
                    or any(ord(char) < 32 for char in name)
                    or Path(name).suffix.lower() not in (".flac", ".wav", ".aif", ".aiff", ".m4a", ".alac")):
                raise ValueError("Choose a FLAC, WAV, AIFF, or ALAC file.")
            length = self.body_length(MAX_CANDIDATE_BYTES)
            if not length:
                raise ValueError("The candidate file is empty.")
            self.connection.settimeout(30)
            with TemporaryDirectory(prefix="spliff-review-") as directory:
                candidate = Path(directory) / name
                with candidate.open("xb") as output:
                    remaining = length
                    while remaining:
                        chunk = self.rfile.read(min(remaining, 1024 * 1024))
                        if not chunk:
                            raise ValueError("The file transfer ended early.")
                        output.write(chunk)
                        remaining -= len(chunk)
                review_id = stage(db, track_id, candidate)
            self.reply_json({"id": review_id}, 201)

        def pick_folders(self, single=False):
            picker = shutil.which("osascript")
            if not picker:
                raise ValueError("The folder picker requires macOS.")
            choice = (
                'JSON.stringify([app.chooseFolder({withPrompt: "Choose a music library for Spliff"}).toString()])'
                if single else
                'JSON.stringify(app.chooseFolder({withPrompt: "Choose music folders for Spliff", '
                'multipleSelectionsAllowed: true}).map(path => path.toString()))'
            )
            result = subprocess.run(
                [picker, "-l", "JavaScript", "-e",
                 'const app = Application.currentApplication(); app.includeStandardAdditions = true; ' + choice],
                capture_output=True, text=True, timeout=300,
            )
            if result.returncode:
                if "(-128)" in result.stderr:
                    return None
                raise ValueError("Spliff could not open the macOS folder picker.")
            return json.loads(result.stdout)

        def log_message(self, *_args):
            pass

        def reply_json(self, content, status=200):
            self.reply(json.dumps(content).encode(), "application/json", status)

        def reply(self, content, mime, status=200):
            self.send_response(status)
            self.send_header("Content-Type", mime + "; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; frame-ancestors 'none'; object-src 'none'")
            self.end_headers()
            self.wfile.write(content)

    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        print(f"http://127.0.0.1:{server.server_port}", flush=True)
        stop = Event()
        worker = Thread(target=sync.run_worker, args=(db, stop), daemon=True, name="spliff-sync")
        worker.start()
        try:
            server.serve_forever()
        finally:
            stop.set()
