"""Discogs OAuth authorization on the local Mac."""
import json
import os
import secrets
import stat
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Thread
from time import monotonic, time
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from .discogs import request_discogs

STATIC = Path(__file__).with_name("static")


def credential_path():
    return Path.home() / "Library/Application Support/Spliff/Discogs/oauth.json"


def consumer():
    key = os.environ.get("SPLIFF_DISCOGS_CONSUMER_KEY")
    secret = os.environ.get("SPLIFF_DISCOGS_CONSUMER_SECRET")
    if key and secret:
        return key, secret
    record = read_credentials(credential_path().with_name("application.json"))
    if not record:
        raise ValueError("Spliff's Discogs application credentials are missing on this Mac.")
    return record["key"], record["secret"]


def header(client, token_secret="", **fields):
    consumer_key, consumer_secret = client
    fields.update(oauth_consumer_key=consumer_key, oauth_nonce=secrets.token_hex(16),
                  oauth_timestamp=str(int(time())), oauth_signature_method="PLAINTEXT",
                  oauth_version="1.0",
                  oauth_signature=quote(consumer_secret, safe="") + "&" + quote(token_secret, safe=""))
    return "OAuth " + ", ".join(f'{key}="{quote(value, safe="")}"' for key, value in fields.items())


def read_credentials(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Spliff's Discogs credentials must be private.")
        return json.load(stream)


def saved_authorization():
    record = read_credentials(credential_path())
    if not record:
        return None
    return header(consumer(), record["oauth_token_secret"], oauth_token=record["oauth_token"])


def save_credentials(credentials, path=None):
    path = path or credential_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as store:
        temporary = Path(store.name)
        try:
            json.dump(credentials, store)
            store.flush()
            os.fsync(store.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def token_fields(data):
    fields = {name: values[0] for name, values in parse_qs(data.decode()).items() if len(values) == 1}
    if not fields.get("oauth_token") or not fields.get("oauth_token_secret"):
        raise ValueError("Discogs did not return an authorization credential.")
    return fields


def connect_discogs(_db, *, application=None):
    if saved_authorization():
        return {"status": "connected"}
    client = application or consumer()
    callback_path = "/discogs/" + secrets.token_urlsafe(24)
    finished = False

    class Callback(BaseHTTPRequestHandler):
        timeout = 10

        def do_GET(self):
            nonlocal finished
            url = urlsplit(self.path)
            values = parse_qs(url.query)
            if (self.headers.get("Host") != host or url.path != callback_path
                    or values.get("oauth_token") != [pending["oauth_token"]]
                    or len(values.get("oauth_verifier", [])) != 1):
                self.send_error(400, "Invalid Discogs callback.")
                return
            finished = True
            try:
                authorization = header(client, pending["oauth_token_secret"],
                                       oauth_token=pending["oauth_token"],
                                       oauth_verifier=values["oauth_verifier"][0])
                tokens = token_fields(request_discogs("/oauth/access_token", authorization, method="POST"))
                save_credentials({key: tokens[key] for key in ("oauth_token", "oauth_token_secret")})
            except (OSError, ValueError):
                self.send_error(502, "Discogs sign-in failed. Start again from ChatGPT.")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(b"Discogs is connected. Return to ChatGPT to look up a release.")

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Callback)
    server.timeout = 1
    host = f"127.0.0.1:{server.server_port}"
    try:
        authorization = header(client, oauth_callback=f"http://{host}{callback_path}")
        pending = token_fields(request_discogs("/oauth/request_token", authorization))
        if pending.get("oauth_callback_confirmed") != "true":
            raise ValueError("Discogs did not confirm the callback.")
    except BaseException:
        server.server_close()
        raise

    def wait_for_browser():
        deadline = monotonic() + 300
        with server:
            while not finished and monotonic() < deadline:
                server.handle_request()

    Thread(target=wait_for_browser, daemon=True).start()
    return {"status": "authorization_required", "expires_in_seconds": 300,
            "authorization_url": "https://www.discogs.com/oauth/authorize?" +
            urlencode({"oauth_token": pending["oauth_token"]})}


def setup_discogs():
    """Collect application credentials locally, then send the user to Discogs."""
    if saved_authorization():
        raise ValueError("Discogs is already connected.")
    entry = "/" + secrets.token_urlsafe(24)
    awaiting_approval = False
    form = (STATIC / "discogs-setup.html").read_text(encoding="utf-8").replace("{{entry}}", entry)
    styles = {f"{entry}/app.css": "app.css", f"{entry}/setup.css": "discogs-setup.css"}

    class Setup(BaseHTTPRequestHandler):
        timeout = 10

        def valid_address(self):
            return self.headers.get("Host") == host and self.path == entry

        def reply(self, status, mime, body):
            self.send_response(status)
            self.send_header("Content-Type", mime + "; charset=utf-8")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'self'; form-action 'self' https://www.discogs.com; "
                             "frame-ancestors 'none'; base-uri 'none'")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "same-origin")
            self.end_headers()
            self.wfile.write(body)

        def show_form(self, status=200, error=""):
            message = f'<p role="alert">{escape(error)}</p>' if error else ""
            self.reply(status, "text/html", form.replace("{{error}}", message).encode())

        def do_GET(self):
            if self.headers.get("Host") == host and self.path in styles:
                self.reply(200, "text/css", (STATIC / styles[self.path]).read_bytes())
            elif self.valid_address() and not awaiting_approval:
                self.show_form()
            else:
                self.send_error(404)

        def do_POST(self):
            nonlocal awaiting_approval, deadline
            if (awaiting_approval or not self.valid_address()
                    or self.headers.get("Origin") != f"http://{host}"
                    or self.headers.get_content_type() != "application/x-www-form-urlencoded"):
                if self.valid_address() and not awaiting_approval:
                    self.show_form(403, "Open this setup link directly on your Mac and try again.")
                else:
                    self.send_error(403)
                return
            length = self.headers.get("Content-Length", "")
            if not length.isascii() or not length.isdecimal() or len(length) > 4 or not 0 < int(length) <= 4096:
                self.show_form(400, "Enter both application credentials and try again.")
                return
            try:
                values = parse_qs(self.rfile.read(int(length)).decode(), max_num_fields=2)
                key, secret = (values[name][0].strip() for name in ("key", "secret"))
                if not all(0 < len(value) <= 256 and value.isascii() and value.isprintable()
                           for value in (key, secret)):
                    raise ValueError("Invalid application credentials.")
                result = connect_discogs(None, application=(key, secret))
                save_credentials({"key": key, "secret": secret}, credential_path().with_name("application.json"))
            except (KeyError, ValueError, OSError):
                self.show_form(400, "Spliff could not connect. Check the application key and secret and try again.")
                return
            awaiting_approval = True
            deadline = monotonic() + 310
            self.send_response(303)
            self.send_header("Location", result["authorization_url"])
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()

        def log_message(self, *_args):
            pass

    with HTTPServer(("127.0.0.1", 0), Setup) as server:
        server.timeout = 1
        host = f"127.0.0.1:{server.server_port}"
        print(f"http://{host}{entry}", flush=True)
        deadline = monotonic() + 600
        while monotonic() < deadline:
            server.handle_request()
            if awaiting_approval and saved_authorization():
                print("Discogs connected.", flush=True)
                return
    raise ValueError("Discogs setup expired.")
