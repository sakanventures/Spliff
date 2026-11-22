"""Local dashboard and explicitly requested catalog actions."""
import json
import shutil
import subprocess
from tempfile import TemporaryDirectory
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .catalog import stats, tracks
from .review import protect, reviews, stage
from .scanner import scan

MAX_CANDIDATE_BYTES = 512 * 1024 * 1024
STATIC = Path(__file__).with_name("static")
ASSETS = {"/": ("index.html", "text/html"), "/app.css": ("app.css", "text/css"),
          "/app.js": ("app.js", "text/javascript")}

def serve(db, port):
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
                elif url.path == "/api/reviews":
                    self.reply_json({"items": reviews(db)})
                else:
                    self.send_error(404)
            except ValueError:
                self.reply_json({"error": "Invalid search parameters"}, 400)
            except sqlite3.Error:
                self.reply_json({"error": "Catalog unavailable"}, 503)

        def do_POST(self):
            host = f"127.0.0.1:{server.server_port}"
            if (self.headers.get("Host") != host or self.headers.get("Origin") != f"http://{host}"
                    or self.headers.get("X-Spliff-Action") != "dashboard"):
                self.reply_json({"error": "Open the local Spliff dashboard to make changes."}, 403)
                return
            try:
                parts = urlsplit(self.path).path.strip("/").split("/")
                if parts == ["api", "scan"]:
                    if self.body_length(0):
                        raise ValueError("Unexpected scan data.")
                    self.pick_and_scan()
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

        def pick_and_scan(self):
            picker = shutil.which("osascript")
            if not picker:
                raise ValueError("The folder picker requires macOS.")
            result = subprocess.run(
                [picker, "-e", 'POSIX path of (choose folder with prompt "Choose a music folder for Spliff")'],
                capture_output=True, text=True, timeout=300,
            )
            if result.returncode:
                if "(-128)" in result.stderr:
                    self.reply_json({"cancelled": True})
                    return
                raise ValueError("Spliff could not open the macOS folder picker.")
            self.reply_json(scan(db, Path(result.stdout.removesuffix("\n"))))

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
        server.serve_forever()
