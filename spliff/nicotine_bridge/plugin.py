"""Adapter to the pinned Nicotine+ search implementation."""
import fcntl
import logging
import os
import socketserver
from pathlib import PureWindowsPath
from concurrent.futures import Future, TimeoutError
from time import time
from uuid import uuid4
from threading import Event, Thread

from pynicotine import __version__
from pynicotine.events import events
from pynicotine.pluginsystem import BasePlugin
from pynicotine.slskmessages import FileAttribute, TransferResponse, UserStatus

from .downloads import Downloads, enabled
from .wire import (
    FORMATS, NICOTINE_VERSION, REQUEST_BYTES, RESPONSE_BYTES, VERSION, WINDOW_SECONDS,
    encode, endpoint, read, search_term, staging_root,
)

LOG = logging.getLogger(__name__)
MAX_FILES = 240


class Search:
    def __init__(self, query):
        self.query = query
        self.id = uuid4().hex
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
        self.searches = {}
        self.downloads = Downloads(self.core, self.config)
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
        self.install_transfer_guard()
        events.connect("file-search-response", self.on_result)
        events.connect("server-disconnect", self.on_disconnect)
        Thread(target=self.bridge.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()

    def disable(self):
        if self.bridge is None:
            return
        self.stopped.set()
        try:
            if self.downloads.allowed:
                self.downloads.stop()
        except (OSError, ValueError):
            LOG.exception("Could not confirm Spliff transfer shutdown")
        finally:
            self.restore_transfer_guard()
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
            return {"nicotine_version": __version__, "staging": enabled(staging_root())}
        if operation == "queue":
            return self.on_main(self.queue, request)
        if operation == "stop_downloads":
            return self.on_main(self.downloads.stop)
        if operation == "download_status":
            return self.on_main(self.downloads.status, request.get("selection"))
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
        return {"files": list(search.files.values()), "truncated": search.truncated,
                "search_id": search.id}

    @staticmethod
    def on_main(operation, *arguments):
        future = Future()

        def invoke():
            if not future.set_running_or_notify_cancel():
                return
            try:
                future.set_result(operation(*arguments))
            except (ValueError, OSError) as exc:
                future.set_exception(exc)
            except Exception:
                LOG.exception("Spliff bridge action failed")
                future.set_exception(ValueError("Nicotine+ could not complete the action."))

        events.invoke_main_thread(invoke)
        try:
            return future.result(timeout=5)
        except TimeoutError as exc:
            future.cancel()
            raise ValueError("Nicotine+ did not confirm the action. Check the staged download before retrying.") from exc

    def queue(self, request):
        if self.stopped.is_set() or self.core.users.login_status == UserStatus.OFFLINE:
            raise ValueError("Connect Nicotine+ to Soulseek before staging a candidate.")
        return self.downloads.queue(request.get("selection"), self.searches, request.get("roots"),
                                    request.get("fingerprint"), request.get("source"), request.get("original_id"))

    def install_transfer_guard(self):
        # Nicotine+ 3.3.11 stores Downloads in a slotted class and can change the size
        # advertised by the peer just before accepting the transfer.
        engine = self.core.downloads
        engine_type = type(engine)
        enqueue = engine_type._enqueue_transfer
        accept = engine_type._transfer_request_downloads

        def guarded_enqueue(instance, transfer, bypass_filter=False):
            if instance is engine and not self.downloads.permit_transfer(transfer, transfer.size):
                engine.abort_downloads([transfer])
                return False
            return enqueue(instance, transfer, bypass_filter=bypass_filter)

        def guarded_accept(instance, message):
            if instance is engine:
                transfer = engine.transfers.get(message.username + message.file)
                if transfer and not self.downloads.permit_transfer(transfer, message.filesize):
                    engine.abort_downloads([transfer])
                    return TransferResponse(allowed=False, reason="Cancelled", token=message.token)
            return accept(instance, message)

        engine_type._enqueue_transfer = guarded_enqueue
        engine_type._transfer_request_downloads = guarded_accept
        self.transfer_guard = (
            ("_enqueue_transfer", guarded_enqueue, enqueue),
            ("_transfer_request_downloads", guarded_accept, accept),
        )

    def restore_transfer_guard(self):
        engine_type = type(self.core.downloads)
        for name, guard, original in self.transfer_guard:
            if getattr(engine_type, name) is guard:
                setattr(engine_type, name, original)

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
            if error is None:
                if len(self.searches) >= 8:
                    del self.searches[next(iter(self.searches))]
                self.searches[search.id] = (time(), list(search.files.values()), search.query)
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
            if (len(path.encode("utf-8")) > 480 or not path.isprintable()
                    or len(message.username) > 64 or not message.username.isprintable()
                    or not 0 < size <= 8 * 1024**3):
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
                "peer": message.username, "path": path, "format": extension, "bytes": size,
                "free_slot": bool(message.freeulslots),
                "queue": 0 if message.freeulslots else message.inqueue or None,
                "seconds": attributes.get(FileAttribute.DURATION),
                "sample_rate": attributes.get(FileAttribute.SAMPLE_RATE),
                "bit_depth": attributes.get(FileAttribute.BIT_DEPTH),
            }
