"""Standard-library HTTP/SSE host for the authoritative v2 API."""
from __future__ import annotations

import http.cookies
import json
import re
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from orchestra import api, artifacts, auth, config, db, paths
from orchestra.contracts import ContractError


MAX_BODY = 1024 * 1024
CHUNK = 256 * 1024


def bind_address() -> str:
    return config.http_config()["bind"]


def _token(handler) -> tuple[str | None, bool]:
    authorization = handler.headers.get("Authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip(), False
    raw_cookie = handler.headers.get("Cookie", "")
    if raw_cookie:
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(raw_cookie)
        except http.cookies.CookieError:
            return None, False
        morsel = jar.get("orchestra_device")
        if morsel:
            return morsel.value, True
    return None, False


def _same_origin(handler) -> bool:
    origin = handler.headers.get("Origin")
    if not origin:
        return True
    parsed = urlsplit(origin)
    return parsed.netloc.lower() == handler.headers.get("Host", "").lower()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler= None, *, stop=None):
        self.stop_event = stop or threading.Event()
        super().__init__(address, handler or Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "Orchestra/2"
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        print("orchestra http: " + (format % args), file=sys.stderr)

    def do_HEAD(self):
        self._dispatch(head=True)

    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def do_PATCH(self):
        self._dispatch()

    def do_DELETE(self):
        self._dispatch()

    def _security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")

    def _json(self, status: int, value, headers=None, *, head=False):
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self._security_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        for key, item in (headers or {}).items():
            self.send_header(key, item)
        self.end_headers()
        if not head:
            self.wfile.write(raw)

    def _problem(self, problem: api.Problem, *, head=False):
        self._json(problem.status, problem.payload(), head=head)

    def _body(self):
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise api.Problem(400, "invalid_content_length",
                              "Content-Length is invalid.") from exc
        if length < 0 or length > MAX_BODY:
            raise api.Problem(413, "body_too_large",
                              "Request body exceeds 1 MiB.")
        if length == 0:
            return {}
        if "application/json" not in self.headers.get("Content-Type", ""):
            raise api.Problem(415, "json_required",
                              "Content-Type must be application/json.")
        try:
            return json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise api.Problem(400, "invalid_json", "Request body is not valid JSON.") \
                from exc

    def _dispatch(self, *, head=False):
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        query = {key: values[-1] for key, values in parse_qs(
            parsed.query, keep_blank_values=True).items()}
        try:
            if path == "/health":
                return self._json(200, {"status": "ok"}, head=head)
            if path == "/" and self.command in ("GET", "HEAD"):
                return self._dashboard(head=head)
            if ((path.startswith("/api/v2/") and path.endswith("/stream")) or
                    path == "/api/v2/stream"):
                if self.command != "GET":
                    raise api.Problem(405, "method_not_allowed", "Stream requires GET.")
                return self._stream(path, query, head=head)
            if not path.startswith("/api/v2/"):
                raise api.Problem(404, "not_found", "No such v2 resource.")
            body = self._body() if self.command in ("POST", "PATCH", "DELETE") else None
            raw, cookie_auth = _token(self)
            con = db.connect()
            try:
                identity = auth.identify(con, raw)
                if cookie_auth and self.command not in ("GET", "HEAD") and \
                        not _same_origin(self):
                    raise api.Problem(403, "origin_mismatch",
                                      "Cookie-authenticated mutation has a foreign Origin.")
                result = api.API(con).handle(
                    "GET" if self.command == "HEAD" else self.command,
                    path, query, body, identity)
                if isinstance(result, api.FileResponse):
                    return self._file(result, head=head)
                self._json(result.status, result.data, result.headers, head=head)
            finally:
                con.close()
        except api.Problem as exc:
            self._problem(exc, head=head)
        except ContractError as exc:
            self._problem(api.Problem(422, "invalid_run_request", str(exc)), head=head)
        except auth.AuthError as exc:
            self._problem(api.Problem(403, "forbidden", str(exc)), head=head)
        except (KeyError, LookupError) as exc:
            self._problem(api.Problem(404, "not_found", str(exc)), head=head)
        except sqlite3.IntegrityError as exc:
            self._problem(api.Problem(409, "state_conflict", str(exc)), head=head)
        except (ValueError, TypeError) as exc:
            self._problem(api.Problem(422, "invalid_value", str(exc)), head=head)
        except RuntimeError as exc:
            self._problem(api.Problem(409, "state_conflict", str(exc)), head=head)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            print(f"orchestra http: unexpected request failure: {exc}", file=sys.stderr)
            self._problem(api.Problem(500, "internal_error",
                                      "The daemon could not complete the request."),
                          head=head)

    def _dashboard(self, *, head=False):
        location = Path(__file__).with_name("dashboard.html")
        raw = location.read_bytes()
        self.send_response(200)
        self._security_headers()
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
                         "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if not head:
            self.wfile.write(raw)

    def _file(self, result: api.FileResponse, *, head=False):
        try:
            size = result.path.stat().st_size
        except OSError as exc:
            raise api.Problem(404, "file_not_found", "Retained file is unavailable.") \
                from exc
        try:
            requested = artifacts.byte_range(size, self.headers.get("Range"))
        except artifacts.ArtifactError as exc:
            self.send_response(416)
            self._security_headers()
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        start, end = requested or (0, max(0, size - 1))
        length = 0 if size == 0 else end - start + 1
        self.send_response(206 if requested else 200)
        self._security_headers()
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", result.media_type)
        self.send_header("Content-Length", str(length))
        if requested:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        disposition = "attachment" if result.download else "inline"
        safe_name = re.sub(r"[^A-Za-z0-9._ -]", "_", result.name)
        self.send_header("Content-Disposition", f'{disposition}; filename="{safe_name}"')
        self.end_headers()
        if head or not length:
            return
        with result.path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining:
                chunk = handle.read(min(CHUNK, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _stream(self, path: str, query: dict, *, head=False):
        raw, _ = _token(self)
        con = db.connect()
        identity = auth.identify(con, raw)
        run_match = re.fullmatch(r"/api/v2/runs/(\d+)/stream", path)
        run_id = int(run_match.group(1)) if run_match else None
        authority = "read"
        try:
            auth.authorize(identity, authority, target_run_id=run_id)
        except auth.AuthError as exc:
            con.close()
            raise api.Problem(401 if identity is None else 403, "forbidden", str(exc)) \
                from exc
        try:
            after = int(self.headers.get("Last-Event-ID") or query.get("after") or 0)
        except ValueError as exc:
            con.close()
            raise api.Problem(400, "invalid_cursor", "Stream cursor is invalid.") from exc
        self.send_response(200)
        self._security_headers()
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        if head:
            con.close()
            return
        try:
            self.wfile.write(b"retry: 3000\n\n")
            self.wfile.flush()
            quiet = 0
            while not self.server.stop_event.is_set():
                emitted = False
                if run_id is not None:
                    rows = con.execute(
                        "SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id LIMIT 200",
                        (run_id, after)).fetchall()
                    for row in rows:
                        after = int(row["id"])
                        self._event(after, "run.event", dict(row))
                        emitted = True
                elif path == "/api/v2/inbox/stream":
                    rows = con.execute(
                        "SELECT id,revision,status,kind,run_id FROM attention_requests "
                        "WHERE revision>? ORDER BY revision,id LIMIT 200", (after,)).fetchall()
                    for row in rows:
                        after = int(row["revision"])
                        self._event(after, "inbox.changed", dict(row))
                        emitted = True
                else:
                    revision = db.board_revision(con)
                    if revision > after:
                        after = revision
                        self._event(after, "fleet.changed", {"revision": revision})
                        emitted = True
                quiet = 0 if emitted else quiet + 1
                if quiet >= 15:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    quiet = 0
                self.server.stop_event.wait(1)
        finally:
            con.close()

    def _event(self, event_id: int, name: str, value) -> None:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        frame = f"id: {event_id}\nevent: {name}\ndata: {raw}\n\n".encode()
        self.wfile.write(frame)
        self.wfile.flush()


def make_server(*, addr: str | None = None, port: int | None = None,
                stop: threading.Event | None = None) -> Server:
    settings = config.http_config()
    return Server((addr or settings["bind"], settings["port"] if port is None else port),
                  stop=stop)


def serve(stop: threading.Event | None = None, wake=None, restart=None,
          addr: str | None = None, port: int | None = None):
    """Serve until stopped. ``wake``/``restart`` stay daemon wiring, not API state."""
    stop = stop or threading.Event()
    try:
        server = make_server(addr=addr, port=port, stop=stop)
    except OSError as exc:
        print(f"orchestra http: cannot bind {addr or bind_address()}:{port}: {exc}",
              file=sys.stderr)
        return None

    def shutdown_when_stopped():
        stop.wait()
        server.shutdown()

    watcher = threading.Thread(target=shutdown_when_stopped, daemon=True)
    watcher.start()
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
    return server
