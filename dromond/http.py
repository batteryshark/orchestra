"""The HTTP surface: one port, dashboard at ``/``, API under ``/api/``.

DESIGN §3. Attached to the daemon at ``daemon.serve_http`` — one process
serves the control plane and runs the loop, so the dashboard reads the same
database the sweeper writes without a second service.

Shape:
- **One snapshot** (``GET /api/snapshot``) carries the whole control plane
  and an integer ``version``. Any payload change bumps ``SNAPSHOT_VERSION``
  and the captured fixture in ``tests/fixtures/snapshot-v*.json``.
- **Per-run reads**: ``GET /api/runs/<id>/brief`` and ``.../diff`` read the
  selected run's brief and committed changes when their tabs open. They are
  routes rather than snapshot fields because the snapshot carries every run
  every 4 seconds.
- **One per-project read** (W-0186, reshaped by W-0187): ``GET
  /api/project?id=<projectId>`` carries that project's ENABLED SET — which
  of the global profiles it may staff — and its own statistics. ``POST
  /api/project`` writes the enabled set back. It is a route rather than a
  snapshot field because it is read when a project is picked, not every 4
  seconds, and because the picker itself is derived from ``runs``.
- **Action routes** POST only: stop / tell / check a run, force a sweep,
  pause or resume dispatch. Pause lives in ``meta`` so a restart does not
  silently resume it.
- **Profile management** (W-0173): ``GET /api/profiles/options`` is what the
  harnesses report, for the model/effort pickers; ``POST /api/profiles/NAME``
  adds, edits or removes one profile in the config FILE. Write authority is
  split by cost in ``profile_edit`` and keyed on the *identity the credential
  proves*, not on anything the caller declares (W-0176).
- **Raw config** (W-0190): ``GET /api/config`` is the file as text;
  ``POST /api/config`` replaces it after a TOML parse. Unlisted in
  ``auth.ROUTES``, so the human's alone. ``restart: true`` then trips the
  same event as ``POST /api/restart``.
- **Auth** is a credential on *every* route including reads — the snapshot
  carries titles, prompts and paths. One header, ``X-Dromond-Key``, carries
  either of two credentials: the human's shared secret (dashboard, iOS, CLI)
  or one run's own token, minted at dispatch into that worker's environment.
  ``auth.ROUTES`` is the whole authority table — reads for both, stop/tell/
  check for the human and for a run only against ITSELF, and sweep, pause,
  resume and everything unlisted for the human alone. A browser cannot set
  headers on a navigation, so ``GET`` also accepts ``?key=`` once and hands
  the value to a cookie the dashboard's own JavaScript reads back into the
  header; state-changing POSTs demand the header, which is also the CSRF
  guard. A run token is accepted from the header only — never a cookie or a
  query string, which is where credentials end up in logs.
- **Host check** under a tailnet bind, so a rebinding attack from a browser
  on the same machine cannot reach the port through a hostile name.
- **gzip** on any response over ``GZIP_MIN_BYTES`` whose caller asked for it.
  W-0187 widened the run window from 30 to ``RECENT_RUNS``; the snapshot is
  repetitive JSON on a 4s poll, so level-1 compression is what keeps a board
  of hundreds of runs cheaper on the wire than the old board of thirty.

- **SSE** (W-0165/W-0178) at ``sse_stream``: ``GET /api/runs/<id>/stream`` is
  one run's normalized trace, ``GET /api/log/stream`` is the daemon's own
  log. Both resume from ``Last-Event-ID`` — an integer event id for a trace,
  a ``file@offset`` cursor for the log — and both are behind the same gate,
  with the run trace scoped to the run a token names (``auth.ROUTES``).
"""
import functools
import gzip
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dromond import (auth, config, db, dispatch, paths, proc, profile_edit,
                         profiles, project, runway, traces)

# Bumped by ANY change to the snapshot payload (DESIGN §3 acceptance).
# v3 (W-0178): the run rows carry what the dashboard's detail pane shows —
# base commit, brief path, session, retry lineage, per-run usage, and the
# run's own message thread with its delivery badges.
# v8: a runway window whose reset has already passed reports unknown rather
# than the reading from before it, and each provider carries as_of/age_hours.
# v4 (W-0182): one runway entry per PROVIDER carrying its windows, no
# ``limit``, no reading age, and a plain-words ``pace`` per window.
# v5 (W-0186): ``projects`` — the projects the dashboard's picker offers,
# derived from the runs in this same payload and nothing else.
# v6 (W-0187): the run window went from 30 to RECENT_RUNS below, so a client
# that sized anything on 30 rows is wrong now; ``profiles`` is the GLOBAL set
# in every payload, since per-project profile overrides are gone and the
# companion ``GET /api/project`` answers with an enabled set instead of a
# merged profile list.
# v7 (W-0189): ``daemon.observer`` — whether the spin observer can run at
# all, its profile and its first-look cadence, or the exact fix when it
# cannot. Health that looks fine while nothing is watching is the bug.
SNAPSHOT_VERSION = 8

DEFAULT_PORT = 3011
KEY_ENV = "DROMOND_KEY"
COOKIE = "dromond_key"
HEADER = "X-Dromond-Key"
# W-0176 deleted ``X-Dromond-Run``. Authority is no longer a header the
# caller picks: the credential in HEADER is either the human's shared secret
# or one run's own token, and ``auth`` says which. See auth.ROUTES.

HEALTH_KEY = "daemon_health"
PAUSE_KEY = "dispatch_paused"
PAUSE_AT_KEY = "dispatch_paused_at"

DASHBOARD = Path(__file__).with_name("dashboard.html")
# The board's history window (W-0187), up from 30. PROJECTS are the thing
# worth limiting; runs are not — Orchestra runs a couple of hundred at a time
# with their child runs, and the scroller has to hold them. LIVE runs are not
# in this window at all: ``_runs`` selects them separately and UNBOUNDED, so
# this caps only how far back finished ones go, and no amount of history can
# push a running run off the board.
#
# No pagination, deliberately. A paged board would have to special-case live
# runs onto every page to keep that guarantee, and the project picker is
# DERIVED from the runs the snapshot carries — a wider window makes the picker
# strictly more correct, a paged one would make it depend on which page you
# were looking at.
#
# What it costs, measured on this schema with worst-case incompressible text
# (random prose; real summaries repeat phrasing and gzip better):
#
#   runs on the board   JSON     gzip     build     wire @ 4s poll
#   540 (500 + 40 live)  1.96 MB  330 KB   14+5 ms   4.8 MB/min
#   1040                 3.77 MB  635 KB   24+10 ms  9.3 MB/min
#   540, no messages     0.79 MB  132 KB   11+2 ms   1.9 MB/min
#
# ``_send`` gzips anything over GZIP_MIN_BYTES, which is what makes this
# affordable: ~6x for ~5 ms, so a board of 540 runs costs less on the wire
# than the old 30-run board did uncompressed.
#
# ponytail: per-run ``messages`` is ~60% of the payload and only ever rendered
# for the SELECTED run. The ceiling is around 1,000 runs, where the poll
# passes 9 MB/min. The upgrade path is the one this file already took for
# briefs (W-0183): move the thread to ``GET /api/runs/<id>/messages`` and
# fetch it when a run is selected, which roughly halves the payload and drops
# the per-run query in ``_messages`` with it.
RECENT_RUNS = 1000
RECENT_FINDINGS = 20
SUMMARY_CHARS = 600
# A merge report is the longest thing in a run's thread and the detail pane
# shows it whole; the trace pane's own payloads come over SSE, not from here.
MESSAGE_CHARS = 2000
# Below this, the gzip header costs more than the compression saves.
GZIP_MIN_BYTES = 1024
DIFF_BYTES = 250_000
DIFF_TIMEOUT = 15

# Enough stored polls to measure a window's pace over hours, per provider.
RUNWAY_POLLS = 400
# Below this the difference between two readings is mostly rounding.
PACE_MIN_HOURS = 0.5

_RUN_ROUTE = re.compile(r"^/api/runs/(\d+)/(stop|tell|check)$")
_BRIEF_ROUTE = re.compile(r"^/api/runs/(\d+)/brief$")
_DIFF_ROUTE = re.compile(r"^/api/runs/(\d+)/diff$")
_TRACE_STREAM = re.compile(r"^/api/runs/(\d+)/stream$")
LOG_STREAM = "/api/log/stream"
_PROFILE_ROUTE = re.compile(r"^/api/profiles/([A-Za-z0-9][A-Za-z0-9._-]{0,63})$")
OPTIONS_ROUTE = "/api/profiles/options"  # reserved: never a profile name
# ``?refresh=1`` POLLS THE PROVIDERS before answering; without it the route
# replays what is stored. Unlisted in auth.ROUTES, so it is the human's alone
# — a forced poll spends outbound requests against the owner's own plans.
RUNWAY_ROUTE = "/api/runway"
# ``GET /api/project?id=<projectId>`` — ONE project's view of the two things
# a project actually changes (W-0186/W-0187): its ENABLED SET and its own
# statistics. ``POST`` the same path writes the enabled set. Unlisted in
# auth.ROUTES, so the human's alone, like runway.
PROJECT_ROUTE = "/api/project"
# ``GET /api/config`` is the file as written; ``POST`` replaces it (W-0190).
# Unlisted in auth.ROUTES, so the human's alone — the file holds the shared
# secret and every profile.
CONFIG_ROUTE = "/api/config"


# --- config: the shared secret, the bind address, the accepted hosts --------

def http_cfg(cfg: dict | None = None) -> dict:
    cfg = config.load() if cfg is None else cfg
    section = cfg.get("http")
    return section if isinstance(section, dict) else {}


def load_key(cfg: dict | None = None) -> str:
    """``DROMOND_KEY`` overrides the 0600 config, for a shell or a test."""
    return (paths.env(KEY_ENV).strip()
            or str(http_cfg(cfg).get("key") or "").strip())


KEY_BLOCK = """
# --- HTTP surface + dashboard (DESIGN §3) ---------------------------------
# One shared secret for every route including reads: the snapshot carries
# titles, paths and prompts. Sent as the {header} header; DROMOND_KEY
# overrides it. This file is 0600 — keep it that way.
[http]
key = "{key}"
port = {port}
# bind: "auto" is this machine's Tailscale address, loopback if it has none.
bind = "auto"
# Extra Host header values to accept, e.g. a MagicDNS name.
hosts = []
"""


def ensure_key() -> tuple[str, bool]:
    """`dromond init`: mint the secret into the 0600 config, once.

    Returns (key, minted). An existing ``[http]`` table without a key is the
    user's to fix — rewriting a TOML table needs a writer the stdlib has not
    got, and guessing at line surgery on someone's config is worse.
    """
    existing = load_key()
    if existing:
        return existing, False
    path = config.ensure_global_config()
    if http_cfg():
        raise SystemExit(
            f"dromond: {path} has an [http] table but no key. Add\n"
            f'  key = "{secrets.token_urlsafe(32)}"\n'
            "to it (and keep the file mode 0600).")
    key = secrets.token_urlsafe(32)
    text = path.read_text(encoding="utf-8")
    if text and not text.endswith("\n"):
        text += "\n"
    path.write_text(text + KEY_BLOCK.format(key=key, port=DEFAULT_PORT,
                                            header=HEADER), encoding="utf-8")
    proc.chmod(path, 0o600)
    return key, True


def _tailscale_exe() -> str:
    return (proc.which("tailscale")
            or "/Applications/Tailscale.app/Contents/MacOS/Tailscale")


def tailscale_address() -> str | None:
    """Bind to the tailnet like Work does, so the phone reaches it and the
    coffee-shop wifi does not.

    ponytail: shells out to `tailscale ip -4`; scanning interfaces for the
    100.64.0.0/10 range would guess at a CGNAT lease that may not be ours.
    """
    return _tailscale_ip()


def tailscale_dns_name() -> str | None:
    """This machine's MagicDNS name, so a Host allowlist can accept it."""
    return _tailscale_dns()


def _tailscale_ip() -> str | None:
    try:
        out = subprocess.run([_tailscale_exe(), "ip", "-4"], capture_output=True,
                             text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    found = (out.stdout or "").split()
    return found[0] if out.returncode == 0 and found else None


def _tailscale_dns() -> str | None:
    try:
        out = subprocess.run([_tailscale_exe(), "status", "--json"],
                             capture_output=True, text=True, timeout=5)
        data = json.loads(out.stdout or "{}")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None
    name = str((data.get("Self") or {}).get("DNSName") or "").strip().rstrip(".")
    return name or None


_tailscale_ip = functools.cache(_tailscale_ip)
_tailscale_dns = functools.cache(_tailscale_dns)


def bind_address(cfg: dict | None = None) -> str:
    """The primary listen address: the tailnet when there is one.

    Never every interface — a laptop joins untrusted networks, and the Host
    allowlist is CSRF protection rather than a boundary, since a header can be
    forged. Loopback gets its OWN socket instead (see ``serve``).
    """
    want = str(http_cfg(cfg).get("bind") or "auto").strip()
    if want and want != "auto":
        return want
    return tailscale_address() or "127.0.0.1"


def allowed_hosts(addr: str, cfg: dict | None = None) -> set[str]:
    hosts = {addr, "localhost", "127.0.0.1", "::1", "[::1]"}
    tail = tailscale_address()
    if tail:
        hosts.add(tail)
    dns = tailscale_dns_name()
    if dns:
        hosts.add(dns)
        short = dns.split(".")[0]
        if short:
            hosts.add(short)
    for extra in http_cfg(cfg).get("hosts") or []:
        if str(extra).strip():
            hosts.add(str(extra).strip())
    return {h.lower() for h in hosts}


def host_of(raw: str) -> str:
    """Host header minus the port. ``[::1]:3011`` keeps its brackets."""
    h = (raw or "").strip().lower()
    if h.startswith("["):
        return h[:h.index("]") + 1] if "]" in h else h
    return h.rsplit(":", 1)[0] if ":" in h else h


# --- state the daemon writes and the snapshot reads -------------------------

# The pause switch has ONE implementation, in dispatch.py, and these three
# delegate to it. They used to be a second one: this module wrote a bare
# "1"/"0" flag while dispatch.py wrote a JSON object, both against the same
# meta key. Pressing Resume anywhere therefore left a "0" that dispatch.py
# parsed as the int 0 and called .get on -- which raised on EVERY daemon tick,
# so the daemon stopped sweeping, dispatching and observing entirely, and said
# so only in one repr on stderr.

def dispatch_paused(con) -> bool:
    """The pause switch (DESIGN §4). In ``meta``, so a daemon restart does
    not quietly resume dispatch — a switch that forgets is worse than none."""
    return dispatch.paused(con)


def pause_state(con) -> dict:
    state = dispatch.pause_state(con)
    return {"paused": state is not None, "since": (state or {}).get("at")}


def set_dispatch_paused(con, paused: bool) -> dict:
    if paused:
        dispatch.pause(con)
    else:
        dispatch.resume(con)
    return pause_state(con)


def health(con) -> dict:
    """The daemon's last tick, plus whether the spin observer can run.

    The observer's state is config-derived, not tick-derived, so it is read
    fresh here rather than stored: a health view that says everything is fine
    while NOTHING is watching any run is the failure W-0189 is about.
    """
    from dromond import observer  # SEAM (W-0166): observer imports this
    try:
        entry = json.loads(db.meta_get(con, HEALTH_KEY) or "{}")
    except ValueError:
        entry = {}
    entry["observer"] = observer.status()
    return entry


def record_health(report: dict, error: str | None = None, con=None) -> dict:
    """The daemon's answer to "is the sweeper actually working" (DESIGN §3),
    written once per tick so the dashboard reads it instead of the log."""
    own = con is None
    con = db.connect() if own else con
    try:
        previous = health(con)
        swept = list(report.get("swept") or [])
        entry = {
            "pid": os.getpid(),
            "last_sweep_at": db.now(),
            "outcome": ("error" if error
                        else "paused" if report.get("paused") else "ok"),
            "claimed": [a.get("item") for a in swept
                        if a.get("action") == "dispatch"],
            "actions": len(swept),
            "released": len(report.get("released") or []),
            "reaped": len(report.get("reaped") or []),
            "error": error,
            "last_error": error or previous.get("last_error"),
            "last_error_at": (db.now() if error
                              else previous.get("last_error_at")),
            "started_at": previous.get("started_at") or db.now(),
        }
        db.meta_set(con, HEALTH_KEY, json.dumps(entry))
        con.commit()
        return entry
    finally:
        if own:
            con.close()


# --- the snapshot -----------------------------------------------------------

def _table_exists(con, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def _epoch(stamp: str | None) -> float | None:
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def _seconds(started: str | None, finished: str | None) -> float | None:
    start = _epoch(started)
    if start is None:
        return None
    end = _epoch(finished) if finished else datetime.now(timezone.utc).timestamp()
    return max(0.0, round((end or 0) - start, 1))


_RUN_SELECT = (
    "SELECT r.*, "
    "(SELECT work_id FROM projects p WHERE p.project_id=r.project_id LIMIT 1) "
    "AS project_work_id, "
    "(SELECT name FROM projects p WHERE p.project_id=r.project_id LIMIT 1) "
    "AS project_name FROM runs r ")


def _runs(con) -> list[dict]:
    """Every live run, plus the tail of finished ones. The board must show
    what is running in full; history is a window."""
    rows = list(con.execute(
        _RUN_SELECT + f"WHERE r.status NOT IN {db.TERMINAL_SQL} ORDER BY r.id DESC"))
    rows += list(con.execute(
        _RUN_SELECT + f"WHERE r.status IN {db.TERMINAL_SQL} "
        "ORDER BY r.id DESC LIMIT ?", (RECENT_RUNS,)))
    blocked = {}
    for row in con.execute(
            "SELECT run_id, depends_on_run FROM dispatch_dependencies "
            "ORDER BY depends_on_run"):
        blocked.setdefault(row["run_id"], []).append(row["depends_on_run"])
    out = []
    for r in rows:
        summary = r["summary"] or ""
        out.append({
            "id": r["id"],
            "slug": r["slug"],
            "status": r["status"],
            "profile": r["profile"],
            "backend": r["backend"],
            "model": r["model"],
            "title": r["title"],
            "work_item": r["work_item"],
            "project_id": r["project_id"],
            "project": r["project_work_id"] or r["project_name"],
            "workdir": r["workdir"],
            "branch": r["branch"],
            "parent_run": r["parent_run"],
            "requested_by": r["requested_by"],
            "started_at": r["started_at"],
            "finished_at": r["finished_at"],
            "elapsed_seconds": _seconds(r["started_at"], r["finished_at"]),
            "exit_code": r["exit_code"],
            "live": r["status"] not in db.RUN_TERMINAL,
            "blocked_on": blocked.get(r["id"], []),
            "summary": summary[:SUMMARY_CHARS],
            # v3 (W-0178): what the detail pane shows beside the trace. The
            # token hash is never among them — see tests/test_auth.py.
            "base_commit": r["base_commit"],
            "checkpoint_commit": r["checkpoint_commit"],
            "brief_path": r["brief_path"],
            "session_ref": r["session_ref"],
            "retry_of": r["retry_of"],
            "tokens_in": r["tokens_in"],
            "tokens_out": r["tokens_out"],
            "tokens_total": r["tokens_total"],
            "cost_usd": r["cost_usd"],
            # plan-backed runs have no price at all (W-0179): a subscription
            # reporting 0 is not free work.
            "billing": runway.kind_of(runway.provider_of(r["backend"], r["model"])),
            "usage_source": r["usage_source"],
            "messages": _messages(con, r["id"]),
        })
    return out


def _messages(con, run_id: int) -> list[dict]:
    """One run's thread, badged by ``traces.run_messages`` (DESIGN §6/§7).

    The merge report is a message of kind ``merge``, so the detail pane's
    inbox/outbox and its merge result are the same read.

    ponytail: one query per run in the snapshot (~35 on a busy board). Local
    SQLite behind a tailnet, so it is cheaper than a per-run detail route
    that would break "the snapshot is the single read"; give this a single
    grouped query if a snapshot ever shows up in a profile.
    """
    out = []
    for m in traces.run_messages(con, run_id):
        m["body"] = (m["body"] or "")[:MESSAGE_CHARS]
        out.append(m)
    return out


def _projects(runs: list[dict]) -> list[dict]:
    """The projects the dashboard's picker offers (W-0186).

    DERIVED FROM THE RUNS IN THIS SNAPSHOT, never from Work's project list:
    the picker is for switching between the projects you have actually
    kicked something off from, so a project with no run in the window is not
    in it — offering one would filter the board to nothing.

    ``name`` is whatever the run row already resolved (the project's Work id,
    else its name); a run with no ``project_id`` belongs to no project and
    contributes nothing.
    """
    seen: dict[str, dict] = {}
    for r in runs:
        pid = r.get("project_id")
        if not pid:
            continue
        entry = seen.setdefault(pid, {"project_id": pid, "name": None,
                                      "runs": 0, "live": 0})
        entry["name"] = entry["name"] or r.get("project")
        entry["runs"] += 1
        entry["live"] += 1 if r.get("live") else 0
    return sorted(seen.values(),
                  key=lambda e: (e["name"] or e["project_id"]).lower())


def _profiles(cfg: dict) -> list[dict]:
    """Profiles in ROUTING order (W-0181): priority first, `nice`-style, so
    the list a human reads and the list a planner reads are the same list."""
    out = []
    for name in sorted(cfg.get("profiles", {})):
        p = cfg["profiles"][name]
        out.append({
            "name": name,
            "backend": p.get("backend", "opencode"),   # the harness
            "model": p.get("model"),
            "effort": p.get("effort"),
            "role": p.get("role"),
            # v2 (W-0173): the fields the dashboard's profile editor writes
            # back, so editing needs no second fetch per profile.
            "variant": p.get("variant"),
            # Routing metadata (W-0181), normalized: a legacy named tier is
            # reported as its number, and priority carries its default.
            "tier": config.tier_of(p.get("tier")),
            "tier_name": config.TIERS.get(config.tier_of(p.get("tier"))),
            "priority": config.priority_of(p),
            "spawn_profiles": list(p.get("spawn_profiles") or []),
            "note": p.get("note"),
            "note_at": p.get("note_at"),
            "note_age": profiles.note_age(p.get("note_at")),
        })
    out.sort(key=lambda e: (e["priority"], e["name"]))
    return out


def _json_obj(text) -> dict:
    """A JSON column written by an older build (or by nothing) is {}."""
    try:
        value = json.loads(text or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _json_list(text) -> list[dict]:
    """A JSON column written by an older build (or by nothing) is []."""
    try:
        value = json.loads(text or "[]")
    except ValueError:
        return []
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _epoch(stamp: str | None) -> float | None:
    try:
        return datetime.fromisoformat(
            (stamp or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _pace(rows, window) -> str | None:
    """How fast THIS window is being spent, in words (W-0182).

    Replaces the sparkline nobody could read. The comparison is against the
    oldest stored poll of the SAME window instance — same label, same reset
    stamp — because a window that refilled in between makes the difference
    meaningless, not merely noisy. Too little history, or too little
    movement, and the line says nothing rather than something unexplained.
    """
    label, resets = window.get("label"), window.get("resets_at")
    now = _epoch(rows[0]["polled_at"])
    if now is None or window.get("remaining") is None:
        return None
    for row in reversed(rows[1:]):
        older = next((w for w in _json_list(row["windows"])
                      if w.get("label") == label and w.get("resets_at") == resets
                      and w.get("remaining") is not None), None)
        then = _epoch(row["polled_at"])
        if older is None or then is None:
            continue
        hours = (now - then) / 3600
        if hours < PACE_MIN_HOURS:
            return None
        rate = (older["remaining"] - window["remaining"]) / hours
        return f"using {rate:.0f}% an hour" if rate >= 0.5 else "holding steady"
    return None


def _runway(con) -> list[dict]:
    """ONE ENTRY PER PROVIDER (W-0182), carrying every window that provider
    reports — Claude's 5-hour and weekly limits are two windows of one plan,
    not two providers. The note on a profile stays authoritative over these
    numbers for routing.

    ``kind`` says whether money exists for this provider at all: a plan
    provider shows consumption, and only an ``api`` provider has a balance.
    ``credits`` is whatever else the provider banks — a dollar balance, or
    banked usage resets on a plan (W-0184) — already phrased by its adapter.
    No ``limit`` (a window's limit is always 100% of itself) and no reading
    age — the one age that changes meaning rides on the window as ``stale``.
    """
    history: dict[str, list] = {}
    for r in con.execute("SELECT * FROM runway_polls ORDER BY id DESC LIMIT ?",
                         (RUNWAY_POLLS,)):
        history.setdefault(r["provider"], []).append(r)
    out = []
    for provider in sorted(history):
        rows = history[provider]
        latest = rows[0]
        windows = [runway.as_of_now(w) for w in _json_list(latest["windows"])]
        for w in windows:
            w.pop("limit", None)  # written by a build before W-0182
            w["resets_in"] = runway.until_text(w.get("resets_at"))
            w["pace"] = _pace(rows, w)
        out.append({
            "provider": provider,
            "kind": runway.kind_of(provider),
            "remaining": latest["remaining"],
            "unit": latest["unit"],
            "resets_in": runway.until_text(latest["resets_at"]),
            "windows": windows,
            "credits": runway.credits_text(_json_obj(latest["raw"])),
            "reason": latest["reason"],
            "known": latest["remaining"] is not None,
            # How old the underlying reading is. Shipped because a provider
            # Dromond READS rather than polls (Claude's cache file) can be days
            # behind with nothing broken, and a number with no age on it cannot
            # be told apart from one taken a second ago.
            "as_of": latest["as_of"],
            "age_hours": runway.age_hours(latest["as_of"]),
        })
    return out


def _add(carried, value, digits: int | None = None):
    """Sum that keeps null meaning "nothing captured" (DESIGN §11): a column
    nobody ever recorded stays None instead of collapsing to a false 0."""
    if value is None:
        return carried
    total = (carried or 0) + value
    return round(total, digits) if digits is not None else total


def _statistics(con, project_id: str | None = None) -> dict:
    """Per DESIGN §11, plus the plan/api split (W-0179): a run on a
    subscription has NO price. OpenCode reports ``cost: 0`` on plan data and
    Codex reports none at all — both read as "free" if shown as money, so a
    plan-backed run contributes nothing to cost and its billing says so.

    ``project_id`` narrows the scan to that project's runs (W-0186). It scans
    the WHOLE runs table either way — the snapshot's RECENT_RUNS window is
    the board, not the history these totals are over."""
    by_status: dict[str, int] = {}
    by_profile: dict[str, dict] = {}
    total = active = plan_runs = 0
    worker_seconds = 0.0
    tokens = cost = None
    for r in con.execute(
            "SELECT profile, backend, model, status, started_at, finished_at, "
            "tokens_total, cost_usd FROM runs"
            + (" WHERE project_id=?" if project_id else ""),
            (project_id,) if project_id else ()):
        total += 1
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        live = r["status"] not in db.RUN_TERMINAL
        active += 1 if live else 0
        seconds = _seconds(r["started_at"], r["finished_at"]) or 0.0
        worker_seconds += seconds
        billing = runway.kind_of(runway.provider_of(r["backend"], r["model"]))
        spend = r["cost_usd"] if billing == "api" else None
        plan_runs += 1 if billing == "plan" else 0
        tokens = _add(tokens, r["tokens_total"])
        cost = _add(cost, spend, 6)
        entry = by_profile.setdefault(
            r["profile"], {"profile": r["profile"], "runs": 0, "active": 0,
                           "seconds": 0.0, "tokens": None, "cost": None,
                           "billing": billing})
        entry["runs"] += 1
        entry["active"] += 1 if live else 0
        entry["seconds"] = round(entry["seconds"] + seconds, 1)
        # Null until a run of this profile recorded usage; a backend that
        # reports no cost (codex) keeps cost null while tokens count.
        entry["tokens"] = _add(entry["tokens"], r["tokens_total"])
        entry["cost"] = _add(entry["cost"], spend, 6)
        if entry["billing"] != billing:
            entry["billing"] = "mixed"  # the profile was re-pointed mid-history
    return {
        "runs_total": total,
        "runs_active": active,
        "plan_runs": plan_runs,
        "by_status": by_status,
        "worker_seconds": round(worker_seconds, 1),
        "tokens_total": tokens,
        "cost_usd": cost,
        "by_profile": [by_profile[k] for k in sorted(by_profile)],
    }


def _recent(con, table: str) -> list[dict]:
    """Findings and proposals (DESIGN §9) do not have tables yet. Absent is
    an empty list, not an error — the dashboard renders whichever it gets."""
    if not _table_exists(con, table):
        return []
    rows = con.execute(
        f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?", (RECENT_FINDINGS,))
    return [{k: r[k] for k in r.keys()} for r in rows]


def runway_now(force: bool = False, con=None) -> dict:
    """``GET /api/runway`` (W-0182). ``force`` polls every provider first.

    Nothing else in Dromond polls runway on a timer, so the dashboard's own
    refresh is what keeps these numbers current. The poll is parallel
    (``runway.poll_all``) and every adapter fails soft, so the worst case is
    one adapter's timeout, not the sum of six.
    """
    own = con is None
    con = db.connect() if own else con
    try:
        if force:
            runway.record(con, runway.poll_all(config.load()))
        return {"runway": _runway(con), "generated_at": db.now()}
    finally:
        if own:
            con.close()


def project_scope(project_id: str, con=None) -> dict:
    """``GET /api/project?id=<projectId>`` (W-0186, reshaped by W-0187).

    Two things a project actually changes, and no more. Its ENABLED SET —
    which of the GLOBAL profiles it may staff a run with — and its own
    STATISTICS, the same numbers the snapshot carries over that project's
    runs alone.

    ``enabled_profiles`` is ``null`` when the project has not said, which
    means every profile is enabled. It is deliberately the raw answer rather
    than an expanded list: null and "all ten of them" differ the moment an
    eleventh profile is added.

    The profiles themselves are NOT here. They are global (W-0187), the
    snapshot already carries them, and a second copy per project is exactly
    the per-project profile the enabled set replaced.

    Runs are not here either: the snapshot carries every run with its
    ``project_id``, so filtering the board is the client's own array filter.
    Runway and daemon health belong to the machine, not to one project.
    """
    own = con is None
    con = db.connect() if own else con
    try:
        return {
            "project_id": project_id,
            "enabled_profiles": config.load(project_id).get("enabled_profiles"),
            "statistics": _statistics(con, project_id),
            "generated_at": db.now(),
        }
    finally:
        if own:
            con.close()


def snapshot(con=None) -> dict:
    own = con is None
    con = db.connect() if own else con
    try:
        cfg = config.load()
        runs = _runs(con)
        return {
            "version": SNAPSHOT_VERSION,
            "generated_at": db.now(),
            "home": str(paths.home()),
            "runs": runs,
            "live_runs": sum(1 for r in runs if r["live"]),
            # v5 (W-0186): what the project picker offers, derived from the
            # runs above — never a roster of every project on the machine.
            "projects": _projects(runs),
            "dispatch": pause_state(con),
            "profiles": _profiles(cfg),
            "runway": _runway(con),
            "statistics": _statistics(con),
            "findings": _recent(con, "findings"),
            "proposals": _recent(con, "proposals"),
            "daemon": health(con),
        }
    finally:
        if own:
            con.close()


# --- actions ----------------------------------------------------------------
# ponytail: stop/tell mirror cli.cmd_kill and cli.cmd_interrupt (~25 lines of
# overlap). Fold the three into one module when a third caller appears; two
# is not a pattern, and cli.py belongs to whoever is editing it this week.

def _run_row(con, run_id: int):
    return con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()


def run_brief(con, run_id: int) -> dict:
    """One run's brief FILE, read at request time (W-0183).

    Deliberately NOT in the snapshot: that payload carries every run on a 4s
    poll, and ~35 briefs on every tick is a different thing from one brief
    when a human opens the brief tab. A brief is bounded (DESIGN §6: ~300
    fixed tokens plus a work snapshot capped at 2,000 chars), so the single
    read is cheap.

    A run with no brief file, or whose file has since been swept, is a
    message and a 200 — the run exists, the tab renders, nothing failed.
    """
    r = _run_row(con, run_id)
    if r is None:
        return {"error": f"no run {run_id}"}
    path = r["brief_path"]
    if not path:
        return {"run": run_id, "path": None, "text": None,
                "message": "no brief file: this run was launched from its "
                           "title alone"}
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"run": run_id, "path": path, "text": None,
                "message": f"the brief file is gone: {exc.strerror or exc}"}
    return {"run": run_id, "path": path, "text": text}


def run_diff(con, run_id: int) -> dict:
    """Return one run's committed changes, even after its branch is deleted."""
    r = _run_row(con, run_id)
    if r is None:
        return {"error": f"no run {run_id}"}
    if not r["branch"]:
        return {"run": run_id, "text": None,
                "message": "no branch — this run worked in the shared tree"}
    # A run that committed its own work leaves nothing for the checkpoint to
    # record, so the branch name was the only pointer — and merging deletes it.
    # merge_run anchors the head at refs/dromond/run-N before that, so the
    # candidates run newest-first: checkpoint, kept ref, branch.
    #
    # A run merged BEFORE the W-0188 rename was anchored at refs/maestro/run-N
    # instead, and needs no candidate of its own: its stored branch name is
    # "maestro/run-N", and git's ref search path resolves a bare <name> against
    # refs/<name>, so the branch entry below finds the old anchor even though
    # the branch itself is long deleted. Verified, and covered by
    # test_diff_survives_the_rename_for_a_run_merged_before_it.
    base = r["base_commit"]
    short = str(r["branch"]).rsplit("/", 1)[-1]
    candidates = [c for c in (r["checkpoint_commit"], f"refs/dromond/{short}",
                              r["branch"]) if c]
    if not base or not candidates:
        return {"run": run_id, "text": None,
                "message": "no committed changes recorded yet"}

    root = project.root_for(con, r)

    def _resolve(ref: str) -> str | None:
        checked = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", f"{ref}^{{commit}}"],
            capture_output=True, text=True, timeout=DIFF_TIMEOUT)
        return None if checked.returncode else checked.stdout.strip()

    try:
        resolved_base = _resolve(base)
        resolved_target = next((sha for sha in map(_resolve, candidates) if sha), None)
        if not resolved_base or not resolved_target:
            return {"run": run_id, "text": None,
                    "message": "the recorded commits are no longer available"}
        resolved = [resolved_base, resolved_target]
        # Spool before reading the cap so a pathological repository cannot
        # turn the daemon's memory into an equally pathological diff buffer.
        with tempfile.TemporaryFile() as output:
            diff = subprocess.run(
                ["git", "-C", str(root), "diff", "--no-ext-diff", "--no-color",
                 "--unified=3", *resolved, "--"], stdout=output,
                stderr=subprocess.PIPE, timeout=DIFF_TIMEOUT)
            if diff.returncode:
                detail = diff.stderr.decode(errors="replace").strip()
                return {"error": f"cannot read run {run_id} diff: {detail}"}
            output.seek(0)
            raw = output.read(DIFF_BYTES + 1)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error": f"cannot read run {run_id} diff: {exc}"}

    truncated = len(raw) > DIFF_BYTES
    text = raw[:DIFF_BYTES].decode(errors="replace")
    return {"run": run_id, "base": resolved[0], "head": resolved[1],
            "text": text, "truncated": truncated,
            "message": "no committed changes" if not text else None}


def stop_run(con, run_id: int) -> dict:
    r = _run_row(con, run_id)
    if r is None:
        return {"error": f"no run {run_id}"}
    if r["status"] in db.RUN_TERMINAL:
        return {"run": run_id, "status": r["status"], "changed": False}
    con.execute("UPDATE deferred_dispatches SET status='cancelled', "
                "processed_at=? WHERE run_id=? AND status='pending'",
                (db.now(), run_id))
    con.execute(f"UPDATE runs SET status='killed', finished_at=? WHERE id=? "
                f"AND status NOT IN {db.TERMINAL_SQL}", (db.now(), run_id))
    con.commit()
    signalled = False
    if r["pid"]:
        try:
            proc.signal_group(r["pid"], signal.SIGTERM)
            signalled = True
        except (ProcessLookupError, PermissionError, OSError):
            pass
    # Dependents of a killed prerequisite are declined by the daemon's next
    # tick (supervise.process_ready) — spawning supervisors from an HTTP
    # thread would race the loop for no gain.
    return {"run": run_id, "status": "killed", "changed": True,
            "signalled": signalled}


def tell_run(con, run_id: int, text: str, now: bool = False) -> dict:
    """`tell` is `dromond interrupt`: a message delivered at the worker's
    next safe action boundary, or immediately with ``now``."""
    if not (text or "").strip():
        return {"error": "tell needs a non-empty message"}
    r = _run_row(con, run_id)
    if r is None:
        return {"error": f"no run {run_id}"}
    if r["status"] in db.RUN_TERMINAL:
        return {"error": f"run {run_id} already {r['status']} — dispatch a "
                         "fresh run instead"}
    if not r["session_ref"]:
        return {"error": f"run {run_id}'s session isn't identified yet "
                         "(~10s after spawn) — retry in a moment"}
    try:
        offset = Path(r["log_path"]).stat().st_size
    except (OSError, TypeError):
        offset = 0
    con.execute(
        "INSERT INTO messages(run_id, sender, body, kind, created_at, "
        "delivery_offset) VALUES(?,?,?, 'interrupt', ?, ?)",
        (run_id, "dashboard", text, db.now(), offset))
    if now:
        con.execute(f"UPDATE runs SET status='interrupt' WHERE id=? "
                    f"AND status NOT IN {db.TERMINAL_SQL}", (run_id,))
    con.commit()
    if now and r["pid"]:
        try:
            proc.signal_group(r["pid"], signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    return {"run": run_id, "queued": True, "immediate": bool(now)}


def check_run(con, run_id: int, observe: bool = True) -> dict:
    """`dromond check` on demand (DESIGN §7).

    Layer (a) — process liveness + log silence — is here. Layers (b) and (c)
    attach at the ``observer.check`` seam at the bottom, and their verdict
    replaces ``verdict``. ``observe=False`` skips the model turn only.
    """
    r = _run_row(con, run_id)
    if r is None:
        return {"error": f"no run {run_id}"}
    alive = None
    if r["pid"]:
        try:
            proc.signal_group(r["pid"], 0)
            alive = True
        except PermissionError:
            alive = True
        except OSError:
            alive = False
    silent_for = None
    log_bytes = None
    try:
        stat = Path(r["log_path"]).stat()
        silent_for = round(time.time() - stat.st_mtime, 1)
        log_bytes = stat.st_size
    except (OSError, TypeError):
        pass
    stall = config.load(r["project_id"]).get("settings", {}).get(
        "stall_timeout", config.DEFAULT_STALL_TIMEOUT_SECONDS)
    if r["status"] in db.RUN_TERMINAL:
        verdict = f"finished ({r['status']})"
    elif alive is False:
        verdict = "process is gone but the run is not terminal — the next "\
                  "daemon tick reaps it"
    elif silent_for is None:
        verdict = "no log yet"
    elif stall and silent_for > float(stall):
        verdict = f"silent for {silent_for:.0f}s, past the {stall}s stall cap"
    else:
        verdict = f"working — log written {silent_for:.0f}s ago"
    result = {"run": run_id, "status": r["status"], "alive": alive,
              "silent_for": silent_for, "log_bytes": log_bytes,
              "elapsed_seconds": _seconds(r["started_at"], r["finished_at"]),
              "stall_timeout": stall, "verdict": verdict}
    from dromond import observer  # SEAM (W-0166): observer imports this
    return observer.check(con, r, result, model=observe)


# --- the SSE seam (W-0165) --------------------------------------------------

def sse_stream(handler, path: str) -> bool:
    """SEAM: the trace stream and the daemon's own log over SSE (DESIGN §3
    "Liveness", §7).

    Called for any ``GET /api/**/stream`` after the Host check and auth have
    both passed. Writes the whole response (``text/event-stream``, no
    Content-Length, flush per event) and returns True; an unknown stream path
    returns False and the caller answers 501.

    Two routes, two resume cursors, both carried by ``Last-Event-ID``:
    ``/api/runs/<id>/stream`` resumes on the integer event id it last sent,
    ``/api/log/stream`` on the composite ``file@offset`` cursor
    ``traces.parse_daemon_cursor`` decodes. A browser's ``EventSource``
    replays that header itself, which is why the cursor lives there and not
    in a query string.

    ``EventSource`` cannot set headers, so this route is reached with the
    cookie the dashboard already holds — hence GET, and hence never a POST.
    """
    resume = (handler.headers.get("Last-Event-ID") or "").strip()
    stop = getattr(handler.server, "sse_stop", None)
    match = _TRACE_STREAM.match(path)
    if match:
        after_id = int(resume) if resume.isdigit() else 0
        frames = traces.stream_run_trace(int(match.group(1)), after_id, stop=stop)
    elif path == LOG_STREAM:
        frames = traces.stream_daemon_log(traces.parse_daemon_cursor(resume),
                                          stop=stop)
    else:
        return False
    return _write_stream(handler, frames)


def _write_stream(handler, frames) -> bool:
    """Headers, then one flush per frame until the client or the daemon goes.

    No Content-Length is possible on a stream, so the response is delimited
    by the close and the connection cannot be kept alive.
    """
    handler.close_connection = True
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Connection", "close")
    handler.end_headers()
    if handler.command == "HEAD":
        frames.close()
        return True
    try:
        for frame in frames:
            handler.wfile.write(frame.encode())
            handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass  # the viewer closed the tab; EventSource reconnects with a cursor
    finally:
        frames.close()
    return True


# --- the server -------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "dromond"
    sys_version = ""
    identity = None  # set by _gate, before any route runs

    # -- plumbing
    def log_message(self, fmt, *args):  # noqa: A003 — BaseHTTPRequestHandler API
        print(f"dromond http: {self.client_address[0]} {fmt % args}", flush=True)

    def _send(self, code: int, body: bytes, ctype: str, extra=()):
        encoding = None
        # W-0187 widened the run window from 30 to RECENT_RUNS, and the
        # snapshot is polled every 4 seconds. The payload is repetitive JSON,
        # so level 1 gives an order of magnitude for ~1.5ms — which is what
        # makes a board of hundreds of runs cheaper than the old board of
        # thirty was uncompressed. Level 1 deliberately, not 9: this is a
        # latency path, and the last few percent cost twice the CPU.
        if (len(body) >= GZIP_MIN_BYTES
                and "gzip" in (self.headers.get("Accept-Encoding") or "")):
            body, encoding = gzip.compress(body, 1), "gzip"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if encoding:
            self.send_header("Content-Encoding", encoding)
            self.send_header("Vary", "Accept-Encoding")
        for name, value in extra:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, code: int = 200):
        self._send(code, json.dumps(payload).encode(), "application/json")

    def _deny(self, code: int, reason: str):
        """Honest rejection: the real code, one line of why, the source IP in
        the log. Never the key itself, and never a decoy 404."""
        # A refused POST leaves its body unread, and on a keep-alive
        # connection the next parse would read that body as a request line.
        self.close_connection = True
        self.log_message("%s %s -> %d %s", self.command, self.path, code, reason)
        self._send(code, (reason + "\n").encode(), "text/plain; charset=utf-8")

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    # -- gates
    def _host_ok(self) -> bool:
        host = host_of(self.headers.get("Host", ""))
        return bool(host) and host in self.server.allowed_hosts

    def _loopback_human(self) -> bool:
        """Opt-in: treat a request from this machine as the human.

        Off by default, and the reason is not paranoia about the network —
        it is that WORKERS RUN ON THIS MACHINE. Loopback cannot tell the human
        from a run, so turning this on hands every worker the authority the
        per-run tokens exist to withhold. Convenience for a single-user box;
        never with untrusted runs.
        """
        if not http_cfg(config.load()).get("trust_local"):
            return False
        peer = (self.client_address or ("",))[0]
        return peer in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def _authorized(self, query: dict, header_only: bool):
        """``(identity, None)`` when authorized, else ``(None, reason)``.

        The credential is either the human's shared secret or one live run's
        token (W-0176). A run token is honoured from the header alone: a
        cookie belongs to a browser and a query string ends up in logs.
        """
        supplied = (self.headers.get(HEADER) or "").strip()
        source = "header"
        if not supplied and not header_only:
            cookies = self.headers.get("Cookie") or ""
            for part in cookies.split(";"):
                name, _, value = part.strip().partition("=")
                if name == COOKIE and value:
                    supplied, source = value.strip(), "cookie"
                    break
            if not supplied:
                supplied = (query.get("key") or [""])[0].strip()
                source = "query"
        if not supplied:
            # A run always carries its token, so an unauthenticated loopback
            # request is the human at the keyboard — but only when the owner
            # has said so, because a worker can reach loopback too.
            if self._loopback_human():
                return auth.Identity(auth.HUMAN), None
            return None, (f"missing {HEADER}" if header_only
                          else f"missing {HEADER} (or ?key= on a first visit)")
        if secrets.compare_digest(supplied, self.server.api_key):
            return auth.Identity(auth.HUMAN), None
        if source == "header":
            # ponytail: one db.connect() per non-human credential, schema
            # script and all. Local SQLite behind a tailnet, so it is cheaper
            # than a cache that has to learn about revocation; give the server
            # a shared connection if this ever shows up in a profile.
            con = db.connect()
            try:
                identity = auth.identify(con, supplied, human_key=None)
            finally:
                con.close()
            if identity is not None:
                return identity, None
        return None, f"bad {HEADER} ({source})"

    def _gate(self, path: str, query: dict, header_only: bool) -> bool:
        if not self._host_ok():
            self._deny(403, "host not allowed")
            return False
        identity, reason = self._authorized(query, header_only)
        if reason:
            self._deny(401, reason)
            return False
        key, target = auth.route_key(self.command, path)
        denial = auth.permit(identity, key, target)
        if denial:
            self._deny(403, denial)
            return False
        self.identity = identity
        return True

    # -- routes
    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path.rstrip("/") or "/"
        if not self._gate(path, query, header_only=False):
            return
        if path == "/":
            return self._dashboard(query)
        if path == "/api/snapshot":
            return self._json(snapshot())
        if path == RUNWAY_ROUTE:
            return self._json(runway_now(
                force=(query.get("refresh") or [""])[0] == "1"))
        if path == PROJECT_ROUTE:
            project_id = (query.get("id") or [""])[0].strip()
            if not project_id:
                return self._deny(400, "GET /api/project needs ?id=<projectId>")
            return self._json(project_scope(project_id))
        if path == CONFIG_ROUTE:
            cfg_path = config.ensure_global_config()
            return self._json({"path": str(cfg_path),
                               "text": cfg_path.read_text(encoding="utf-8")})
        if path == OPTIONS_ROUTE:
            # What the harnesses actually offer, for the model/effort
            # pickers (DESIGN §5). Cached: it costs three subprocesses.
            return self._json(profile_edit.discovery_options(
                force=(query.get("refresh") or [""])[0] == "1"))
        if path.startswith("/api/") and path.endswith("/stream"):
            if not sse_stream(self, path):
                self._deny(501, f"no SSE stream at {path}")
            return
        match = _BRIEF_ROUTE.match(path)
        if match:
            con = db.connect()
            try:
                result = run_brief(con, int(match.group(1)))
            finally:
                con.close()
            return self._json(result, 400 if "error" in result else 200)
        match = _DIFF_ROUTE.match(path)
        if match:
            con = db.connect()
            try:
                result = run_diff(con, int(match.group(1)))
            finally:
                con.close()
            return self._json(result, 400 if "error" in result else 200)
        self._deny(404, f"no route {path}")

    do_HEAD = do_GET

    def _dashboard(self, query: dict):
        extra = ()
        if (query.get("key") or [""])[0]:
            # The browser arrived with the secret in the URL. Park it in a
            # readable cookie (the page's JS lifts it back into the header)
            # and get it out of the address bar.
            extra = (("Set-Cookie",
                      f"{COOKIE}={self.server.api_key}; Path=/; Max-Age=31536000; "
                      "SameSite=Strict"),)
        try:
            body = DASHBOARD.read_bytes()
        except OSError as exc:
            return self._deny(500, f"dashboard file missing: {exc}")
        self._send(200, body, "text/html; charset=utf-8", extra)

    def do_POST(self):  # noqa: N802 — BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        # Header only: a cookie would let any page on this browser POST here.
        if not self._gate(path, parse_qs(parsed.query), header_only=True):
            return
        body = self._body()
        match = _RUN_ROUTE.match(path)
        if match:
            run_id, action = int(match.group(1)), match.group(2)
            con = db.connect()
            try:
                if action == "stop":
                    result = stop_run(con, run_id)
                elif action == "tell":
                    result = tell_run(con, run_id, str(body.get("text") or ""),
                                      bool(body.get("now")))
                else:
                    result = check_run(con, run_id)
            finally:
                con.close()
            return self._json(result, 400 if "error" in result else 200)
        if path == "/api/restart":
            # Restarting only stops sweeping, ferrying, the conductor and
            # runway polling. A supervisor is its own session, so IN-FLIGHT
            # RUNS KEEP RUNNING, keep logging and still finalize themselves —
            # which is what makes a restart button safe to offer at all.
            stop = getattr(self.server, "restart", None)
            if stop is None:
                return self._deny(503, "no daemon loop attached to restart")
            stop.set()
            wake = getattr(self.server, "wake", None)
            if wake is not None:
                wake.set()
            return self._json({"restarting": True,
                               "note": "in-flight runs keep running"})
        if path == CONFIG_ROUTE:
            text = body.get("text")
            if not isinstance(text, str):
                return self._deny(400, "POST /api/config needs text")
            try:
                config.check(text)
            except ValueError as exc:
                return self._json({"applied": False, "error": str(exc)}, 400)
            profile_edit.write_atomic(config.ensure_global_config(), text)
            restarting = False
            if body.get("restart"):
                ev = getattr(self.server, "restart", None)
                if ev is not None:
                    ev.set()
                    wake = getattr(self.server, "wake", None)
                    if wake is not None:
                        wake.set()
                    restarting = True
            return self._json({"applied": True, "restarting": restarting})
        if path == "/api/sweep":
            wake = getattr(self.server, "wake", None)
            if wake is None:
                return self._deny(503, "no daemon loop attached to wake")
            wake.set()
            return self._json({"queued": True})
        if path == OPTIONS_ROUTE:
            return self._deny(400, "'options' is the discovery route, not a profile")
        match = _PROFILE_ROUTE.match(path)
        if match:
            # Writes go to the config FILE, not the database: the daemon
            # reads config (DESIGN §5). Options are passed only when
            # discovery is already cached — a write must never block behind
            # a backend CLI that is not installed.
            result = profile_edit.save(
                match.group(1), body.get("profile") or {},
                authority=("agent" if self.identity.kind == auth.RUN
                           else "human"),
                delete=bool(body.get("delete")),
                options=profile_edit.cached_options())
            return self._json(result, 400 if result.get("error") else 200)
        if path == PROJECT_ROUTE:
            # The ENABLED SET (W-0187): which global profiles this project may
            # staff. Unlisted in auth.ROUTES, so the human's alone — a run
            # widening its own project's choices is exactly what DESIGN §5's
            # "nothing grants itself what it asks for" rules out.
            project_id = str(body.get("project_id") or "").strip()
            if not project_id:
                return self._deny(400, "POST /api/project needs project_id")
            names = body.get("enabled_profiles")
            result = profile_edit.set_enabled(project_id, names)
            return self._json(result, 400 if result.get("error") else 200)
        if path in ("/api/dispatch/pause", "/api/dispatch/resume"):
            con = db.connect()
            try:
                state = set_dispatch_paused(con, path.endswith("pause"))
            finally:
                con.close()
            return self._json(state)
        self._deny(404, f"no route {path}")


def serve(stop: threading.Event | None = None, wake=None, addr=None,
          port=None, cfg=None, restart=None):
    """Start the surface on its own threads. Returns the server, or None when
    there is nothing to serve with — no secret means no port, since an
    unauthenticated snapshot is the one thing DESIGN §3 forbids."""
    cfg = config.load() if cfg is None else cfg
    key = load_key(cfg)
    if not key:
        print("dromond http: no shared secret configured — HTTP surface off; "
              "run `dromond init`", flush=True)
        return None
    addr = bind_address(cfg) if addr is None else addr
    port = int(http_cfg(cfg).get("port") or DEFAULT_PORT) if port is None else port
    try:
        srv = ThreadingHTTPServer((addr, port), Handler)
    except OSError as exc:
        print(f"dromond http: cannot bind {addr}:{port} — {exc}",
              file=sys.stderr, flush=True)
        return None
    srv.daemon_threads = True
    srv.api_key = key
    srv.allowed_hosts = allowed_hosts(addr, cfg)
    srv.wake = wake
    srv.restart = restart
    # An SSE handler blocks in its own thread; this is how a shutdown reaches
    # it, since serve_forever's own stop never touches a live handler.
    srv.sse_stop = stop
    threading.Thread(target=srv.serve_forever, name="dromond-http",
                     daemon=True).start()
    if stop is not None:
        threading.Thread(target=_shutdown_when(stop, srv), daemon=True).start()
    # A second socket on loopback, so the dashboard answers at
    # http://localhost:PORT as well as over the tailnet. Two named sockets,
    # never 0.0.0.0: this machine joins networks that are nobody's friend.
    srv.local = None
    if addr not in ("127.0.0.1", "::1", "localhost"):
        try:
            local = ThreadingHTTPServer(("127.0.0.1", srv.server_port), Handler)
        except OSError as exc:
            print(f"dromond http: no loopback listener — {exc}",
                  file=sys.stderr, flush=True)
        else:
            local.daemon_threads = True
            local.api_key, local.wake, local.sse_stop = key, wake, stop
            local.restart = restart
            local.allowed_hosts = srv.allowed_hosts
            threading.Thread(target=local.serve_forever,
                             name="dromond-http-local", daemon=True).start()
            if stop is not None:
                threading.Thread(target=_shutdown_when(stop, local),
                                 daemon=True).start()
            srv.local = local
    print(f"dromond http: http://{addr}:{srv.server_port}/ "
          f"({HEADER} required on every route)", flush=True)
    return srv


def _shutdown_when(stop: threading.Event, srv):
    def wait():
        stop.wait()
        srv.shutdown()
        srv.server_close()
    return wait
