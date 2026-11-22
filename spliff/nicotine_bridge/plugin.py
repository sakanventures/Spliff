"""Adapter to the pinned Nicotine+ search implementation."""
import fcntl
import logging
import os
import socketserver
from pathlib import PureWindowsPath
from threading import Event, Thread

from pynicotine import __version__
from pynicotine.events import events
from pynicotine.pluginsystem import BasePlugin
from pynicotine.slskmessages import FileAttribute, UserStatus

from .wire import (
    FORMATS, NICOTINE_VERSION, REQUEST_BYTES, RESPONSE_BYTES, VERSION, WINDOW_SECONDS,
    encode, endpoint, read, search_term,
)

LOG = logging.getLogger(__name__)
MAX_FILES = 240


class Search:
    def __init__(self, query):
        self.query = query
        self.token = None
        self.files = {}
        self.done = Event()
        self.error = None
        self.truncated = False


class BridgeHandler(socketserver.StreamRequestHandler):
    timeout = 3

    def handle(self):
        try:
            request = read(self.rfile, REQUEST_BYTES)
            if request.get("version") != VERSION:
                raise ValueError("The Spliff plugin and app use different bridge versions.")
            response = self.server.plugin.request(request)
        except (ValueError, OSError) as exc:
            response = {"error": str(exc)}
        try:
            self.wfile.write(encode({"version": VERSION, **response}, RESPONSE_BYTES))
        except (OSError, ValueError):
            pass


class Bridge(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    block_on_close = False


class Plugin(BasePlugin):
    def init(self):
        self.active = None
        self.bridge = None
        self.stopped = Event()
        if __version__ != NICOTINE_VERSION:
            self.log("Spliff Search supports Nicotine+ %s. Installed: %s", (NICOTINE_VERSION, __version__))
            return
        path = endpoint()
        lock = path.with_suffix(".lock").open("a+b")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            self.log("Spliff Search is already running in another Nicotine+ instance.")
            return
        try:
            path.unlink(missing_ok=True)
            self.bridge = Bridge(str(path), BridgeHandler)
        except Exception:
            lock.close()
            raise
        self.lock = lock
        self.bridge.plugin = self
        self.path = path
        events.connect("file-search-response", self.on_result)
        events.connect("server-disconnect", self.on_disconnect)
        Thread(target=self.bridge.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()

    def disable(self):
        if self.bridge is None:
            return
        self.stopped.set()
        if self.active:
            self.finish(self.active, "Spliff Search was disabled.")
        events.disconnect("file-search-response", self.on_result)
        events.disconnect("server-disconnect", self.on_disconnect)
        self.bridge.shutdown()
        self.bridge.server_close()
        self.path.unlink(missing_ok=True)
        self.lock.close()
        self.bridge = None

    def request(self, request):
        operation = request.get("operation")
        if operation == "status":
            return {"nicotine_version": __version__}
        if operation != "search":
            raise ValueError("Unknown Spliff bridge operation.")
        search = Search(search_term(request.get("query")))
        events.invoke_main_thread(self.start, search)
        if not search.done.wait(WINDOW_SECONDS + 4):
            search.done.set()
            events.invoke_main_thread(self.finish, search, "Nicotine+ did not finish the search.")
            return {"error": "Nicotine+ did not finish the search."}
        if search.error:
            return {"error": search.error}
        return {"files": list(search.files.values()), "truncated": search.truncated}

    def start(self, search):
        if search.done.is_set():
            return
        if self.stopped.is_set():
            self.finish(search, "Spliff Search was disabled.")
        elif self.active:
            self.finish(search, "Another Spliff search is running. Try again shortly.")
        elif self.core.users.login_status == UserStatus.OFFLINE:
            self.finish(search, "Connect Nicotine+ to Soulseek and try again.")
        else:
            self.active = search
            try:
                self.core.search.do_search(search.query, "global", switch_page=False)
                search.token = self.core.search.token
                events.schedule(WINDOW_SECONDS, self.finish, callback_args=(search,))
            except Exception:
                LOG.exception("Spliff could not start a Nicotine+ search")
                self.finish(search, "Nicotine+ could not start the search.")

    def finish(self, search, error=None):
        try:
            if self.active is search:
                self.active = None
                if search.token is not None:
                    self.core.search.remove_search(search.token)
        finally:
            search.error = error
            search.done.set()

    def on_disconnect(self, *_args):
        if self.active:
            self.finish(self.active, "Nicotine+ disconnected during the search.")

    def on_result(self, message):
        search = self.active
        if not search or search.token is None or message.token != search.token or not message.list:
            return
        active = self.core.search.searches.get(search.token)
        if active is None:
            return
        for _code, path, size, _extension, attributes, *_unused in message.list:
            if len(path.encode("utf-8")) > 480 or not path.isprintable() or not message.username.isprintable():
                continue
            lower = path.lower()
            if (not all(word in lower for word in active.included_words)
                    or any(word in lower for word in active.excluded_words)):
                continue
            extension = PureWindowsPath(path).suffix.lower().lstrip(".")
            if extension not in FORMATS:
                continue
            key = (message.username, path)
            if key in search.files:
                continue
            if len(search.files) >= MAX_FILES:
                search.truncated = True
                break
            search.files[key] = {
                "peer": message.username[:64], "path": path, "format": extension, "bytes": size,
                "free_slot": bool(message.freeulslots),
                "queue": 0 if message.freeulslots else message.inqueue or None,
                "seconds": attributes.get(FileAttribute.DURATION),
                "sample_rate": attributes.get(FileAttribute.SAMPLE_RATE),
                "bit_depth": attributes.get(FileAttribute.BIT_DEPTH),
            }
