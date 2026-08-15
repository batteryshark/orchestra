"""Nod client: file escalations, collect decisions (DESIGN §8, W-0168).

Nod (github.com/batteryshark/nod) is the push/input device for Dromond's
human loop. Dromond builds no push plumbing of its own; it POSTs a request
card and reads the decision back.

Issuer API used here, all joined onto ``base_url`` (which may itself sit
under a proxy path prefix, e.g. ``https://host/boop``):
- ``POST /api/v1/requests``                 file a card
- ``GET  /api/v1/requests/{id}/decision``   read the current decision view
- ``GET  /api/v1/requests/{id}/wait``       long-poll (1-60s, ``timed_out``)
- ``POST /api/v1/requests/{id}/cancel``     withdraw a pending card

ONE TOKEN PER CHANNEL. A Nod issuer token is scoped to exactly one channel,
so Dromond holds two: the decisions channel needs an answer, the alerts
channel is dismiss-only, so alerts can be muted without muting decisions.
A ``NodClient`` therefore *is* a channel — it carries that channel's id and
that channel's token, and cannot be pointed at another channel. Routing a
decision-kind escalation at the alerts client (or the reverse) raises
``NodChannelError`` at the call site rather than presenting the wrong
credential and getting a puzzling 401.

Reads that address an existing request by id take the client from the
channel recorded next to that request id in ``nod_requests`` — never a
guess, never a try-both loop.

Token handling: tokens come from ``DROMOND_NOD_DECISIONS_TOKEN`` /
``DROMOND_NOD_ALERTS_TOKEN``, else the 0600 secrets file. They go in the
Authorization header and nowhere else — never a log line, never an
exception message, never the database.

NOT WIRED YET: nothing calls these helpers. The supervisor/sweeper hookup
is a later item. ``dromond nod test|show|cancel`` is the manual surface.
"""
import json
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dromond import paths

DECISIONS = "decisions"  # channel roles. The channel *ids* are configured;
ALERTS = "alerts"        # a token only ever works for one of them.
ROLES = (DECISIONS, ALERTS)

DEFAULT_SECRETS_FILE = "~/.config/dromond/nod-secrets.env"
SECRET_KEYS = ("base_url", "decisions_channel", "decisions_token",
               "alerts_channel", "alerts_token")
ENV_PREFIX = "DROMOND_NOD_"  # DROMOND_NOD_ALERTS_TOKEN, DROMOND_NOD_BASE_URL, ...
DEFAULT_TIMEOUT = 15
WAIT_MIN, WAIT_MAX = 1, 60  # the server clamps to this range; match it here


class NodError(Exception):
    """Nod rejected a request, or is unreachable.

    Carries the status and the server's message only. No token is ever
    interpolated into this — an escalation failure often ends up in a log
    or a Work comment.
    """

    def __init__(self, status: int, message: str):
        super().__init__(f"nod api {status}: {message}")
        self.status = status


class NodChannelError(Exception):
    """A card was routed at the wrong channel, or at an unconfigured one.

    This is a Dromond-side mistake, not a server response: raised before any
    request goes out, so the wrong token is never presented. Messages name
    channel roles and channel ids, never tokens.
    """


def join_url(base_url: str, path: str) -> str:
    """Join an API path onto base_url, keeping any proxy path prefix.

    ``urljoin`` would throw ``/boop`` away for a root-absolute path, which
    is exactly the deployment this has to support.
    """
    return base_url.rstrip("/") + "/" + path.lstrip("/")


class NodClient:
    """One channel: its id, its token, its base url.

    The channel id is not a per-call argument on purpose. A token that only
    works for one channel should not be reachable from code that thinks it
    can pick a channel.
    """

    def __init__(self, base_url: str, channel_id: str, token: str,
                 role: str = DECISIONS, timeout: float = DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.channel_id = channel_id
        self.role = role
        self._token = token
        self.timeout = timeout

    def __repr__(self) -> str:  # keeps the token out of tracebacks and reprs
        return (f"NodClient(base_url={self.base_url!r}, role={self.role!r}, "
                f"channel_id={self.channel_id!r})")

    def _call(self, method: str, path: str, body: dict | None = None,
              params: dict | None = None, timeout: float | None = None) -> dict:
        url = join_url(self.base_url, path)
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self._token}",
            **({"Content-Type": "application/json"} if data is not None else {}),
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode()[:400]
            except OSError:
                detail = exc.reason
            raise NodError(exc.code, detail or str(exc.reason)) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # `from None`: an exception chain can carry the Request object,
            # and the Request carries the Authorization header.
            raise NodError(0, f"unreachable ({method} {path}): "
                              f"{getattr(exc, 'reason', exc)}") from None
        return json.loads(raw) if raw.strip() else {}

    # --- issuer API ---------------------------------------------------------

    def create(self, *, title: str, summary: str = "", body_markdown: str = "",
               fields=(), links=(), priority: int | None = None,
               dedupe_key: str | None = None, expires_at=None,
               callback_url: str | None = None, options=(),
               recipients: list[str] | None = None) -> dict:
        """File a request card on THIS client's channel.

        Returns ``{request_id, deduped, request}``.

        ``priority`` is documented in Nod's README but is NOT a field on the
        server's create body, which is ``deny_unknown_fields`` — sending it
        422s the whole request. It is rendered as a card field instead.
        """
        card_fields = list(fields)
        if priority is not None:
            card_fields.append({"label": "Priority", "value": str(priority)})
        body = {"channel_id": self.channel_id, "title": title, "summary": summary,
                "body_markdown": body_markdown, "fields": card_fields,
                "links": list(links), "options": list(options)}
        if dedupe_key:
            body["dedupe_key"] = dedupe_key
        if expires_at is not None:
            body["expires_at"] = _rfc3339(expires_at)
        if callback_url:
            body["callback_url"] = callback_url
        if recipients:
            body["recipients"] = recipients
        return self._call("POST", "/api/v1/requests", body)

    def decision(self, request_id: str) -> dict:
        """The authoritative decision view: status, decision, decisions."""
        return self._call("GET", f"/api/v1/requests/{request_id}/decision")

    def wait(self, request_id: str, timeout_seconds: int = 55) -> dict:
        """Long-poll for a decision. A timeout is a normal return, not a raise.

        The result always carries a ``timed_out`` bool; when it is True the
        request is still pending and the caller should poll again.
        """
        seconds = max(WAIT_MIN, min(WAIT_MAX, int(timeout_seconds)))
        try:
            got = self._call("GET", f"/api/v1/requests/{request_id}/wait",
                             params={"timeout_seconds": seconds},
                             timeout=seconds + self.timeout)
        except NodError as exc:
            if exc.status:  # a real rejection (404, 403, ...) still raises
                raise
            return {"request_id": request_id, "status": "pending", "timed_out": True}
        got["timed_out"] = bool(got.get("timed_out"))
        return got

    def cancel(self, request_id: str) -> dict:
        return self._call("POST", f"/api/v1/requests/{request_id}/cancel", {})


def health(base_url: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Reachability probe. Unauthenticated on purpose: no token is spent on
    a liveness check, and this files no card and notifies nobody."""
    url = join_url(base_url, "/health")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise NodError(exc.code, exc.reason or "health check failed") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise NodError(0, f"unreachable (GET {url}): "
                          f"{getattr(exc, 'reason', exc)}") from None
    return json.loads(raw) if raw.strip() else {}


class Nod:
    """The configured channels, by role. Missing a token is not fatal.

    One channel configured and the other not is a working setup: the
    configured channel files cards, the other reports as unconfigured when
    something actually tries to use it.
    """

    def __init__(self, clients: dict[str, NodClient]):
        self.clients = clients

    def __repr__(self) -> str:
        return f"Nod(base_url={self.base_url!r}, configured={self.configured!r})"

    @property
    def base_url(self) -> str:
        return next(iter(self.clients.values())).base_url if self.clients else ""

    @property
    def configured(self) -> list[str]:
        return [role for role in ROLES if role in self.clients]

    def for_role(self, role: str) -> NodClient:
        client = self.clients.get(role)
        if client is None:
            raise NodChannelError(
                f"the Nod {role} channel is not configured — set "
                f"{ENV_PREFIX}{role.upper()}_TOKEN and {role}_channel, or add "
                f"{role}_token/{role}_channel to {DEFAULT_SECRETS_FILE}")
        return client

    def for_request(self, con: sqlite3.Connection, request_id: str) -> NodClient:
        """The client for the channel this request was actually filed to.

        A token only works for its own channel, so reading a decision back
        is not free to guess. The channel was written down when the card was
        filed; if it was not, say so instead of trying tokens until one works.
        """
        row = con.execute("SELECT channel FROM nod_requests WHERE request_id=?",
                          (request_id,)).fetchone()
        if row is None:
            raise NodChannelError(
                f"{request_id} is not in nod_requests, so its channel is "
                f"unknown — name the channel explicitly ({'/'.join(ROLES)})")
        return self.for_channel_id(row["channel"])

    def for_channel_id(self, channel_id: str) -> NodClient:
        for role in ROLES:
            client = self.clients.get(role)
            if client is not None and client.channel_id == channel_id:
                return client
        raise NodChannelError(
            f"no configured Nod token is scoped to channel {channel_id!r} "
            f"(configured: {', '.join(self.configured) or 'none'})")


def decision_after_callback(client: NodClient, request_id: str) -> dict:
    """Handle a Nod callback POST: re-read the decision through the API.

    SECURITY — DO NOT REMOVE, DO NOT "OPTIMIZE" THIS AWAY IN A REFACTOR.
    Nod's ``callback_url`` POST is unsigned and best-effort by design. It is
    a wake-up hint and nothing more. Anyone who can reach the callback route
    can post any body they like, so the body is never evidence of a decision.
    Take the request id from it if you must, then ALWAYS re-read the decision
    through the authenticated issuer API — this function — before acting.
    Never read the option, the text, or the status out of the callback body.
    """
    return client.decision(request_id)


# --- escalation kinds (DESIGN §8: what escalates) ---------------------------
# Never run start or finish: a feed that buzzes trains you to ignore it.

ANSWER = {"id": "answer", "label": "Answer", "kind": "approve_with_text",
          "requires_text": True, "text_placeholder": "Answer for the worker"}
STOP = {"id": "stop", "label": "Stop the run", "kind": "reject", "destructive": True}
RETRY = {"id": "retry", "label": "Retry", "kind": "custom"}
RESOLVER = {"id": "resolver", "label": "Dispatch a resolver", "kind": "custom"}
LEAVE = {"id": "leave", "label": "Leave it", "kind": "dismiss"}
ACCEPT = {"id": "accept", "label": "Accept", "kind": "approve"}
REJECT = {"id": "reject", "label": "Reject with reason", "kind": "reject_with_text",
          "requires_text": True, "destructive": True}
ABANDON = {"id": "abandon", "label": "Abandon", "kind": "reject", "destructive": True}
DISMISS = {"id": "ok", "label": "Dismiss", "kind": "dismiss"}

# Which channel each escalation kind belongs on. This is the routing table
# the wrong-channel guard checks against; it is not a caller's choice.
KIND_ROLE = {"blocked": DECISIONS, "merge_conflict": DECISIONS,
             "pivot": DECISIONS, "failure": DECISIONS, "alert": ALERTS}


def blocked_run(target, question: str, **ctx) -> dict:
    """A run is blocked on a question only the human can answer."""
    return file_escalation(target, kind="blocked", options=[ANSWER, STOP],
                           body_markdown=question, **ctx)


def merge_conflict(target, detail: str, **ctx) -> dict:
    """A merge conflict: retry the merge, dispatch a resolver, or leave it."""
    return file_escalation(target, kind="merge_conflict",
                           options=[RETRY, RESOLVER, LEAVE],
                           body_markdown=detail, **ctx)


def pivot_proposal(target, proposal: str, **ctx) -> dict:
    """A planner wants to change direction: accept, or reject with a reason."""
    return file_escalation(target, kind="pivot", options=[ACCEPT, REJECT],
                           body_markdown=proposal, **ctx)


def failure(target, detail: str, **ctx) -> dict:
    """Two infrastructure failures on one item (D4 retry policy exhausted)."""
    return file_escalation(target, kind="failure", options=[RETRY, ABANDON],
                           body_markdown=detail, **ctx)


def alert(target, detail: str = "", **ctx) -> dict:
    """Informational, dismiss-only. Goes to the mutable alerts channel."""
    return file_escalation(target, kind="alert", options=[DISMISS],
                           body_markdown=detail, **ctx)


def client_for_kind(target: "Nod | NodClient", kind: str) -> NodClient:
    """The channel client this escalation kind must use.

    Given a ``Nod``, picks the right channel. Given a bare ``NodClient``,
    verifies it is the right one and raises if it is not — a decision card
    filed with the alerts token would otherwise 401, or worse, land in the
    channel the human muted.
    """
    role = KIND_ROLE.get(kind)
    if role is None:
        raise NodChannelError(f"unknown escalation kind {kind!r} — no channel "
                              f"routing for it (known: {', '.join(KIND_ROLE)})")
    if isinstance(target, Nod):
        return target.for_role(role)
    if target.role != role:
        raise NodChannelError(
            f"{kind!r} escalations belong on the {role} channel, but this "
            f"client is the {target.role} channel ({target.channel_id!r}) — "
            f"a Nod issuer token is scoped to exactly one channel")
    return target


def file_escalation(target: "Nod | NodClient", *, kind: str, title: str, options,
                    con: sqlite3.Connection | None = None,
                    run_id: int | None = None, work_item: str | None = None,
                    dedupe_key: str | None = None, **card) -> dict:
    """File one card on the channel its kind belongs to, and record it.

    ``dedupe_key`` defaults to kind + run/item, so a retried run that
    re-escalates the same thing does not buzz the phone twice.
    """
    client = client_for_kind(target, kind)
    dedupe_key = dedupe_key or ":".join(
        str(p) for p in ("dromond", kind, run_id, work_item) if p is not None)
    created = client.create(title=title, dedupe_key=dedupe_key, options=options,
                            **card)
    if con is not None:
        record(con, created["request_id"], kind=kind, channel=client.channel_id,
               run_id=run_id, work_item=work_item, dedupe_key=dedupe_key)
    return created


def links_for(work_url: str | None = None, run_url: str | None = None) -> list[dict]:
    """DESIGN §8: cards carry "open the Work item" and "open the run trace"."""
    return [link for link in (
        {"label": "Work item", "url": work_url} if work_url else None,
        {"label": "Run trace", "url": run_url} if run_url else None,
    ) if link]


def expires_in(seconds: int) -> str:
    return _rfc3339(datetime.now(timezone.utc) + timedelta(seconds=seconds))


def _rfc3339(when) -> str:
    if isinstance(when, str):
        return when
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- persistence (db.nod_requests, schema v6) -------------------------------
# Nod is the input device; Work stays the ledger. This table is the bridge:
# it remembers which run and which Work item a Nod request id belongs to, so
# a decision can be mirrored into the Work thread later.
# `channel` is what makes a later read possible at all: the token is scoped
# to one channel, so the channel has to be remembered, not inferred.
# No token is stored in this table, and no column exists for one.

def record(con: sqlite3.Connection, request_id: str, *, kind: str, channel: str,
           run_id: int | None = None, work_item: str | None = None,
           dedupe_key: str | None = None) -> None:
    con.execute(
        "INSERT OR REPLACE INTO nod_requests(request_id, kind, channel, run_id, "
        "work_item, dedupe_key, status, created_at) VALUES(?,?,?,?,?,?,'pending',?)",
        (request_id, kind, channel, run_id, work_item, dedupe_key, _now()))
    con.commit()


def save_decision(con: sqlite3.Connection, request_id: str, view: dict) -> None:
    """Store a decision view read back from the API (never a callback body)."""
    decision = view.get("decision") or {}
    con.execute(
        "UPDATE nod_requests SET status=?, option_id=?, option_kind=?, "
        "decision_text=?, decided_at=? WHERE request_id=?",
        (view.get("status", "pending"), decision.get("option_id"),
         decision.get("option_kind"), decision.get("text"),
         decision.get("resolved_at") or (_now() if decision else None), request_id))
    con.commit()


def mark_mirrored(con: sqlite3.Connection, request_id: str) -> None:
    """The decision reached the Work thread; stop re-mirroring it."""
    con.execute("UPDATE nod_requests SET mirrored_at=? WHERE request_id=?",
                (_now(), request_id))
    con.commit()


def open_requests(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """Cards still awaiting an answer — the startup ``wait`` backstop list."""
    return list(con.execute(
        "SELECT * FROM nod_requests WHERE status='pending' ORDER BY created_at"))


def unmirrored(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """Answered cards whose decision has not reached Work yet."""
    return list(con.execute(
        "SELECT * FROM nod_requests WHERE status!='pending' AND mirrored_at IS NULL "
        "AND work_item IS NOT NULL ORDER BY created_at"))


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- config -----------------------------------------------------------------

def nod_cfg(cfg: dict) -> dict:
    return dict(cfg.get("nod", {}))


def secrets_path(cfg: dict) -> Path:
    """Where the per-channel issuer tokens live.

    An explicit ``secrets_file`` wins. Otherwise DROMOND_NOD_SECRETS_FILE
    replaces the default, so a process that merely enables [nod] cannot fall
    back to the human's real credentials and file against the live Nod host —
    which happened during W-0098's build.
    """
    configured = nod_cfg(cfg).get("secrets_file")
    if configured:
        return Path(configured).expanduser()
    override = paths.env("DROMOND_NOD_SECRETS_FILE")
    return Path(override or DEFAULT_SECRETS_FILE).expanduser()


def load_secrets(cfg: dict) -> dict:
    """Base url, channel ids, and per-channel tokens.

    Env wins over the file, per key: ``DROMOND_NOD_ALERTS_TOKEN``,
    ``DROMOND_NOD_DECISIONS_TOKEN``, ``DROMOND_NOD_BASE_URL``, and so on.

    Deliberately NOT read from config.toml: that file is shared and routinely
    pasted into issues. The secrets file must be 0600 — a token any other
    local user can read is not a token.
    """
    path = secrets_path(cfg)
    values = _read_env_file(path)
    for key in SECRET_KEYS:
        env = paths.env(ENV_PREFIX + key.upper()).strip()
        if env:
            values[key] = env
    return values


def _read_env_file(path: Path) -> dict:
    if not path.is_file():
        return {}
    if path.stat().st_mode & 0o077:
        raise SystemExit(f"dromond: {path} is readable by other users — "
                         f"run `chmod 600 {path}`")
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.removeprefix("export ").partition("=")
        if not sep:
            continue
        key = key.strip().lower()
        if key in SECRET_KEYS:
            values[key] = value.strip().strip("'\"")
    return values


def from_cfg(cfg: dict) -> Nod | None:
    """The configured channels, or None when the human loop is off.

    One channel configured and the other not is fine and returns a ``Nod``;
    the unconfigured one raises ``NodChannelError`` only if used.
    """
    n = nod_cfg(cfg)
    if not n.get("enabled"):
        return None
    secrets = load_secrets(cfg)
    base_url = secrets.get("base_url") or ""
    if not base_url:
        return None
    timeout = float(n.get("timeout", DEFAULT_TIMEOUT))
    clients = {}
    for role in ROLES:
        token = secrets.get(f"{role}_token")
        channel_id = secrets.get(f"{role}_channel")
        if token and channel_id:
            clients[role] = NodClient(base_url, channel_id, token, role=role,
                                      timeout=timeout)
    return Nod(clients) if clients else None
