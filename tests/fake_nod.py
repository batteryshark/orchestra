"""In-process stub of Nod's issuer API (stdlib http.server).

Mirrors the shapes the real server actually serves, taken from
nod-server/src/api/requests.rs, views.rs, and nod-proto/src/request.rs:

- ``POST /api/v1/requests`` requires a Bearer token, rejects unknown body
  fields the way the server's ``deny_unknown_fields`` does, and returns
  ``{request_id, deduped, request}``
- ``/decision`` and ``/wait`` return the decision view
  (``request_id, status, decision, decisions, decision_resolution, ...``);
  ``/wait`` adds ``timed_out: true`` when the clamped timeout elapses
- ``/cancel`` returns ``{request}``
- ``/health`` answers without a token

Crucially it also models the fact that made this client two-token: an
issuer token is scoped to EXACTLY ONE CHANNEL. Presenting the alerts token
for a decisions-channel card is a 403 here, as it is on the real server, so
a test cannot pass by accident with the wrong credential.

``prefix`` mounts the whole API under a proxy path (``/boop``), so the
client's url building is exercised against a non-root deployment.

No live server is ever contacted by the tests, and no card ever reaches a
real device.
"""
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# nod-server CreateRequestRequest is deny_unknown_fields — anything outside
# this set is a 422, which is why `priority` cannot go on the wire.
CREATE_FIELDS = {
    "channel_id", "recipients", "decision_resolution", "title", "summary",
    "body_markdown", "fields", "links", "image_url", "notification",
    "dedupe_key", "expires_at", "options", "callback_url", "template_id",
    "template_version", "variables",
}

DECISIONS_CHANNEL = "chan-decisions"
ALERTS_CHANNEL = "chan-alerts"
DECISIONS_TOKEN = "decisions-token-do-not-log"
ALERTS_TOKEN = "alerts-token-do-not-log"


class FakeNod:
    def __init__(self, channels=None, prefix=""):
        # {channel_id: token}. One token per channel, exactly like Nod.
        self.channels = dict(channels or {DECISIONS_CHANNEL: DECISIONS_TOKEN,
                                          ALERTS_CHANNEL: ALERTS_TOKEN})
        self.prefix = prefix.rstrip("/")
        self.requests: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.auth_seen: list[str | None] = []
        self.resolve_after: float | None = None  # seconds; None = never resolves
        self._n = 0
        self._server = None
        self._thread = None

    def channel_for_token(self, token: str | None) -> str | None:
        for channel_id, expected in self.channels.items():
            if token == expected:
                return channel_id
        return None

    def _new_id(self) -> str:
        self._n += 1
        return f"req_{self._n}"

    def resolve(self, request_id, option_id="answer", kind="approve_with_text",
                text=None):
        """Act as the human's device: record a decision."""
        req = self.requests[request_id]
        req["status"] = "resolved"
        req["decision"] = {
            "request_id": request_id, "option_id": option_id, "option_kind": kind,
            "option_label": option_id.title(), "text": text,
            "actor_user_id": "owner", "actor_device_id": "device-1",
            "signature": None, "resolved_at": "2026-08-13T00:00:00Z",
        }

    def decision_view(self, request_id) -> dict:
        req = self.requests[request_id]
        return {"request_id": request_id, "status": req["status"],
                "decision": req.get("decision"), "decisions": [],
                "decision_resolution": "shared", "recipients": [],
                "pending_recipients": [], "request_digest": "sha256:stub"}

    def start(self) -> str:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self))
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)
        self._thread.start()
        return f"http://127.0.0.1:{self._server.server_address[1]}{self.prefix}"

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=5)


def _make_handler(state: FakeNod):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status, obj):
            payload = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _route(self, path: str) -> str | None:
            """Strip the proxy prefix; None when the path is off this mount."""
            if not state.prefix:
                return path
            if path == state.prefix:
                return "/"
            return path[len(state.prefix):] if path.startswith(state.prefix + "/") \
                else None

        def _token_channel(self) -> str | None:
            """The channel this Bearer token may act on, or None (401 sent)."""
            header = self.headers.get("Authorization")
            state.auth_seen.append(header)
            token = header[7:] if (header or "").startswith("Bearer ") else None
            channel = state.channel_for_token(token)
            if channel is None:
                self._send(401, {"error": "unauthorized"})
            return channel

        def _owned(self, request_id, channel) -> dict | None:
            """The request, if this token's channel is the one it was filed to."""
            req = state.requests.get(request_id)
            if req is None:
                self._send(404, {"error": "not found"})
                return None
            if req.get("channel_id") != channel:
                # The real server scopes a token to one channel; another
                # channel's card is simply not visible to it.
                self._send(403, {"error": "token is not scoped to this channel"})
                return None
            return req

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length)) if length else {}

        def do_POST(self):
            path = self._route(self.path)
            if path is None:
                return self._send(404, {"error": "not found"})
            state.calls.append(("POST", path))
            channel = self._token_channel()
            if channel is None:
                return
            if path == "/api/v1/requests":
                body = self._body()
                unknown = set(body) - CREATE_FIELDS
                if unknown:
                    return self._send(422, {"error": f"unknown field {unknown.pop()}"})
                if not body.get("title"):
                    return self._send(422, {"error": "missing field title"})
                if body.get("channel_id") != channel:
                    return self._send(
                        403, {"error": "token is not scoped to this channel"})
                dedupe = body.get("dedupe_key")
                for rid, existing in state.requests.items():
                    if dedupe and existing["dedupe_key"] == dedupe \
                            and existing["status"] == "pending":
                        return self._send(200, {"request_id": rid, "deduped": True,
                                                "request": existing})
                rid = state._new_id()
                state.requests[rid] = {
                    "id": rid, "request_id": rid, "status": "pending",
                    "dedupe_key": dedupe, "decision": None, **body}
                return self._send(200, {"request_id": rid, "deduped": False,
                                        "request": state.requests[rid]})
            m = re.fullmatch(r"/api/v1/requests/([^/]+)/cancel", path)
            if m:
                req = self._owned(m.group(1), channel)
                if req is None:
                    return
                req["status"] = "cancelled"
                return self._send(200, {"request": req})
            self._send(404, {"error": "not found"})

        def do_GET(self):
            raw, _, query = self.path.partition("?")
            path = self._route(raw)
            if path is None:
                return self._send(404, {"error": "not found"})
            state.calls.append(("GET", path))
            if path == "/health":  # unauthenticated on purpose
                return self._send(200, {"status": "ok"})
            channel = self._token_channel()
            if channel is None:
                return
            m = re.fullmatch(r"/api/v1/requests/([^/]+)/decision", path)
            if m:
                if self._owned(m.group(1), channel) is None:
                    return
                return self._send(200, state.decision_view(m.group(1)))
            m = re.fullmatch(r"/api/v1/requests/([^/]+)/wait", path)
            if m:
                rid = m.group(1)
                if self._owned(rid, channel) is None:
                    return
                seconds = int(re.search(r"timeout_seconds=(\d+)", query).group(1))
                assert 1 <= seconds <= 60, "server clamps timeout_seconds to 1-60"
                if state.resolve_after is not None:
                    time.sleep(state.resolve_after)
                    state.resolve(rid)
                    return self._send(200, state.decision_view(rid))
                view = state.decision_view(rid)
                view["timed_out"] = True
                return self._send(200, view)
            self._send(404, {"error": "not found"})

    return Handler
