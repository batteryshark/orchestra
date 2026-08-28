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
- **The control-turn log** (I-0081): ``GET /api/turns`` is the SERIES of
  control turns, newest first, optionally one project's and one layer's. The
  snapshot pins only the latest per project; reading the observer's reasoning
  over time is a screen you open, not a thing worth carrying on a 4s poll.
- **The outbound feed** (CONTRACT §7 Enforcement 2): ``GET
  /api/runs?since=<revision>`` is the cursored read of run outcomes. Every
  runs row carries ``revision``, the monotonic marker the schema's triggers
  stamp on it; a consumer keeps its own cursor and asks for what is past it.
  Orchestra holds no subscriber list, no endpoint and no delivery state, so
  it cannot learn who is listening — the consumer's cursor IS the delivery
  guarantee, which is why nothing here retries. Not SSE and not the board:
  a plain paged GET for programs.
- **One per-project read** (W-0186, reshaped by W-0187): ``GET
  /api/project?id=<projectId>`` carries that project's ENABLED SET — which
  of the global profiles it may staff — and its own statistics. ``POST
  /api/project`` writes the enabled set back. It is a route rather than a
  snapshot field because it is read when a project is picked, not every 4
  seconds, and because the picker itself is derived from ``runs``.
- **The registry** (this wave): ``GET /api/projects`` lists every registered
  project with its archived flag, and ``POST /api/projects`` parks or unparks
  ANY project through the same ``project.set_archived`` the CLI calls.
  Separate from the picker on purpose — the picker is derived from runs, so
  the project worth parking is the one missing from it.
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
  carries titles, prompts and paths. One header, ``X-Orchestra-Key``, carries
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
  one run's normalized trace, ``GET /api/runs/<id>/log/stream`` is that run's
  RAW harness output — a read-only tail of the log file, which is the surface
  that answers "what is the CLI actually doing" when a run looks stuck and
  the parser's reading of it does not — ``GET /api/log/stream`` is the
  daemon's own log, and ``GET /api/board/stream`` is the board's INVALIDATION — a bare
  revision number, never the snapshot, so the dashboard refetches instead of
  polling every 4 seconds. All three resume from ``Last-Event-ID`` — an
  integer event id for a trace, a ``file@offset`` cursor for the log, the
  revision for the board — and all of them are behind the same gate, with the
  run trace and the run's raw log scoped to the run a token names
  (``auth.ROUTES``).
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

from orchestra import (auth, config, db, dispatch, messaging, paths, proc,
                         instrumentation, profile_edit, profiles, project, runway, supervise,
                         traces)

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
# v11 (W-0291): removed the always-empty findings/proposals arrays and the
# inert delegation field on profiles.
# v12 (W-0292): every run reports its effective isolation state explicitly.
# v13 (W-0301): statistics include the latest run instrumentation report.
# v14 (W-0304): each worker run carries board_n, a dense rank among
# layer-IS-NULL rows. Turns keep id and carry board_n null.
# v18: every worker run carries `no`, its project's own count. A single
# global sequence was read as a per-project run count and could not be —
# control turns took 146 of the first 300 numbers and five projects shared
# the rest. `id` stays on the wire as the internal key routes are addressed
# by; nothing shows it to a human.
# v16: pinned_turns is gone. A control turn pinned above the runs list put
# the machine's opinion of ONE run where it read as a headline over all of
# them; each run now carries its own `machine_note`, and the full feed of
# turns already had a card on health.
# v15 (W-0304, second pick): board_n is gone. Dense numbering gave one run
# two numbers, and the board's disagreed with the log, the branch, and the
# detail header. A worker run now carries turns_before: the machine turns
# between it and the previous worker run, which explains the gap in the ids
# instead of renumbering around it.
SNAPSHOT_VERSION = 18

DEFAULT_PORT = 3011
KEY_ENV = "ORCHESTRA_KEY"
COOKIE = "orchestra_key"
HEADER = "X-Orchestra-Key"
# W-0176 deleted ``X-Orchestra-Run``. Authority is no longer a header the
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
# The same run, one level down: the RAW harness log rather than the parser's
# reading of it (DESIGN §7). auth.ROUTES scopes it exactly like the trace.
_RUN_LOG_STREAM = re.compile(r"^/api/runs/(\d+)/log/stream$")
LOG_STREAM = "/api/log/stream"
BOARD_STREAM = "/api/board/stream"
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
# ``GET /api/projects`` is the REGISTRY — every registered project, archived
# rows included — and ``POST`` the same path parks or unparks ONE local one.
# A sibling of PROJECT_ROUTE rather than another field on it: that route is
# keyed on a projectId and carries one project's settings, while this is the
# whole list and is keyed on PATH, the registry's own key. Unlisted in
# auth.ROUTES, so the human's alone: DESIGN §1 parks a project, and a run
# must never park the project it is running in.
PROJECTS_ROUTE = "/api/projects"
# ``GET /api/turns?project=<projectId>&layer=<layer>&limit=<n>`` (I-0081) —
# the control turns themselves, newest first. The snapshot pins one turn per
# project; this is the series behind it, so the observer's decisions read as a
# log. A route rather than a snapshot field for the same reason briefs are
# (W-0183): it is read when the log is opened, not every 4 seconds. Unlisted
# in auth.ROUTES, so the human's alone.
TURNS_ROUTE = "/api/turns"
RECENT_TURNS = 200
# ``GET /api/runs?since=<revision>&limit=<n>`` (CONTRACT §7 Enforcement 2) —
# the cursored feed of run outcomes, oldest change first. Unlisted in
# auth.ROUTES, so the human's alone: it carries every project's refs and
# summaries, which is wider than the snapshot's window. ``FEED_PAGE`` is both
# the default and the ceiling, and it is echoed in the payload so a consumer
# can tell a full page from a final one.
RUNS_ROUTE = "/api/runs"
FEED_PAGE = 200
# ``GET /api/config`` is the file as written; ``POST`` replaces it (W-0190).
# Unlisted in auth.ROUTES, so the human's alone — the file holds the shared
# secret and every profile.
CONFIG_ROUTE = "/api/config"
# ``GET /api/seats`` — which profile each judgment layer runs on; ``POST``
# sets one. The 2026-08-25 auth outage showed the seats were invisible:
# every layer rode the same expired-auth profile and the only way to move
# any of them was hand-editing config.toml. Unlisted in auth.ROUTES, so the
# human's alone, like the raw config it edits.
SEATS_ROUTE = "/api/seats"
# seat -> (table header, key). The write goes through profile_edit.render,
# the same comment-preserving surgery every managed config edit uses. An
# empty router seat turns routing OFF; the other seats then derive from
# profile tiers.
SEATS = {
    "observer": ("[settings]", "observer_profile"),
    "planner": ("[settings]", "planner_profile"),
    "router": ("[work]", "router"),
    "verify": ("[verify]", "profile"),
    "second_opinion": ("[verify]", "second_opinion"),
    "resolver": ("[merge]", "resolver_profile"),
}


def seats_payload() -> dict:
    cfg = config.load()
    tables = {"observer": cfg.get("settings") or {},
              "planner": cfg.get("settings") or {},
              "router": cfg.get("work") or {},
              "verify": cfg.get("verify") or {},
              "second_opinion": cfg.get("verify") or {},
              "resolver": cfg.get("merge") or {}}
    return {
        "seats": {seat: str(tables[seat].get(key) or "") or None
                  for seat, (_, key) in SEATS.items()},
        "profiles": sorted(cfg.get("profiles", {})),
    }


def set_seat(seat: str, profile_name: str | None) -> dict:
    header, key = SEATS[seat]
    cfg_path = config.ensure_global_config()
    text = cfg_path.read_text(encoding="utf-8")
    new = profile_edit.render(text, "", {key: profile_name}, header=header)
    config.check(new)
    profile_edit.write_atomic(cfg_path, new)
    return seats_payload()


# --- config: the shared secret, the bind address, the accepted hosts --------

def http_cfg(cfg: dict | None = None) -> dict:
    cfg = config.load() if cfg is None else cfg
    section = cfg.get("http")
    return section if isinstance(section, dict) else {}


def load_key(cfg: dict | None = None) -> str:
    """``ORCHESTRA_KEY`` overrides the 0600 config, for a shell or a test."""
    return (paths.env(KEY_ENV).strip()
            or str(http_cfg(cfg).get("key") or "").strip())


KEY_BLOCK = """
# --- HTTP surface + dashboard (DESIGN §3) ---------------------------------
# One shared secret for every route including reads: the snapshot carries
# titles, paths and prompts. Sent as the {header} header; ORCHESTRA_KEY
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
    """`orchestra init`: mint the secret into the 0600 config, once.

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
            f"orchestra: {path} has an [http] table but no key. Add\n"
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
    # DESIGN §3: pause is a meta write, so no runs trigger fires for it. Every
    # OTHER open client would wait for the next health tick to learn the
    # switch moved -- the one bump here is what the board push is for.
    db.bump_board_revision(con)
    con.commit()
    return pause_state(con)


def health(con) -> dict:
    """The daemon's last tick, plus whether the spin observer can run.

    The observer's state is config-derived, not tick-derived, so it is read
    fresh here rather than stored: a health view that says everything is fine
    while NOTHING is watching any run is the failure W-0189 is about.
    """
    from orchestra import observer  # SEAM (W-0166): observer imports this
    try:
        entry = json.loads(db.meta_get(con, HEALTH_KEY) or "{}")
    except ValueError:
        entry = {}
    entry["observer"] = observer.status()
    # Auth outages (2026-08-25): a harness whose credential died answers
    # every call with its own auth error. The flag is set where that reply
    # is recognized (observer.note_auth_outage) and cleared by the next
    # clean turn; here it rides the snapshot so the banner can say it.
    entry["outages"] = _auth_outages(con)
    return entry


def _auth_outages(con) -> list[dict]:
    out = []
    for row in con.execute(
            "SELECT key, value FROM meta WHERE key LIKE 'auth_outage:%' "
            "AND value != ''"):
        try:
            entry = json.loads(row["value"])
        except ValueError:
            continue
        entry["backend"] = row["key"].split(":", 1)[1]
        out.append(entry)
    return out


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
        # DESIGN §3: the board's other fields — pause state, runway, health
        # itself — move without a runs write. One bump per tick caps the
        # board's staleness at the sweep interval instead of adding triggers
        # to meta, which db.connect writes on every single connection.
        db.bump_board_revision(con)
        con.commit()
        return entry
    finally:
        if own:
            con.close()


# --- the snapshot -----------------------------------------------------------

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
    "(SELECT source_ref FROM projects p WHERE p.project_id=r.project_id LIMIT 1) "
    "AS project_source_ref, "
    "(SELECT name FROM projects p WHERE p.project_id=r.project_id LIMIT 1) "
    "AS project_name, "
    # W-0304, second pick: how many machine turns sit between this worker
    # run and the previous one. Dense numbering was the first attempt and
    # gave one run two numbers — the board said #37 where the log, the
    # branch, and the detail header all said 64. The item allowed for this:
    # "fall back to the divider if dual numbering proves confusing." The id
    # is the id everywhere; this count explains the gap instead of hiding it.
    "(SELECT COUNT(*) FROM runs t WHERE t.layer IS NOT NULL AND t.id<r.id "
    " AND t.id>COALESCE((SELECT MAX(w.id) FROM runs w "
    "                    WHERE w.layer IS NULL AND w.id<r.id), 0)) "
    "AS turns_before, "
    # The machine's latest word ABOUT THIS RUN. A control turn's own row
    # carries no link back to what it judged; the observation does, so the
    # note belongs on the run it is about rather than pinned above a list
    # where it read as a headline (2026-08-27).
    "(SELECT o.action || ': ' || o.reason FROM observations o "
    " WHERE o.run_id=r.id AND o.layer IN ('observer','mechanical') "
    " ORDER BY o.id DESC LIMIT 1) AS machine_note FROM runs r ")


def _run_payload(con, r, blocked: dict) -> dict:
    summary = r["summary"] or ""
    return {
        "id": r["id"],
        # THE number a human reads: this project's own count. `id` is the
        # internal key and is no longer shown (schema v18).
        "no": None if r["layer"] else r["project_seq"],
        "turns_before": None if r["layer"] else r["turns_before"],
        "machine_note": r["machine_note"],
        "slug": r["slug"],
        "status": r["status"],
        "profile": r["profile"],
        "backend": r["backend"],
        "model": r["model"],
        "title": r["title"],
        "ref": r["ref"],
        "project_id": r["project_id"],
        "project": r["project_source_ref"] or r["project_name"],
        "workdir": r["workdir"],
        "branch": r["branch"],
        "isolation": run_isolation(r),
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
        "layer": r["layer"],
        "routed_reason": r["routed_reason"],
        "tokens_in": r["tokens_in"],
        "tokens_out": r["tokens_out"],
        "tokens_total": r["tokens_total"],
        "cost_usd": r["cost_usd"],
        # plan-backed runs have no price at all (W-0179): a subscription
        # reporting 0 is not free work.
        "billing": runway.kind_of(runway.provider_of(r["backend"], r["model"])),
        "usage_source": r["usage_source"],
        "messages": _messages(con, r["id"]),
    }


def run_isolation(run) -> str:
    """The execution mode a run actually reached, not merely requested."""
    if run["layer"]:
        return "control"
    if run["status"] in ("pending", "spawning") or supervise.never_started(run):
        return "not_started"
    return "isolated" if run["branch"] else "shared"


def _runs(con) -> list[dict]:
    """Every live run, plus the tail of finished ones. The board must show
    what is running in full; history is a window.

    Control turns (W-0214, ``layer`` set) are never in this list: they are
    not the fleet, so the live count and the tab badge do not move for them.
    Each run carries the machine's latest word about it as
    ``machine_note``; the turns themselves are served by /api/turns.
    """
    rows = list(con.execute(
        _RUN_SELECT + f"WHERE r.status NOT IN {db.TERMINAL_SQL} "
        "AND r.layer IS NULL ORDER BY r.id DESC"))
    rows += list(con.execute(
        _RUN_SELECT + f"WHERE r.status IN {db.TERMINAL_SQL} "
        "AND r.layer IS NULL ORDER BY r.id DESC LIMIT ?", (RECENT_RUNS,)))
    blocked = {}
    for row in con.execute(
            "SELECT run_id, depends_on_run FROM dispatch_dependencies "
            "ORDER BY depends_on_run"):
        blocked.setdefault(row["run_id"], []).append(row["depends_on_run"])
    return [_run_payload(con, r, blocked) for r in rows]


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


def _projects(con, runs: list[dict]) -> list[dict]:
    """The projects the dashboard's picker offers (W-0186).

    DERIVED FROM THE RUNS IN THIS SNAPSHOT, never from Work's project list:
    the picker is for switching between the projects you have actually
    kicked something off from, so a project with no run in the window is not
    in it — offering one would filter the board to nothing.

    ``name`` is whatever the run row already resolved (the project's Work id,
    else its name); a run with no ``project_id`` belongs to no project and
    contributes nothing.

    An ARCHIVED project is off the picker (DESIGN §1) — its runs stay on the
    board under "all projects", and the client drops a scope whose project
    left this list, so archiving one mid-session falls back rather than
    stranding the view.
    """
    parked = {r["project_id"] for r in con.execute(
        f"SELECT project_id FROM projects WHERE {project.ARCHIVED_SQL}=1")}
    seen: dict[str, dict] = {}
    for r in runs:
        pid = r.get("project_id")
        if not pid or pid in parked:
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
    # A provider that used to be staffed leaves rows behind. The board shows
    # what is being spent now, so yesterday's plan drops off with it.
    on_the_board = runway.shown(config.load())
    out = []
    for provider in sorted(p for p in history if p in on_the_board):
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
            # Orchestra READS rather than polls (Claude's cache file) can be days
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


def _statistics(con, project_id: str | None = None,
                instrumentation_limit: int = 30) -> dict:
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
            "tokens_total, cost_usd FROM runs WHERE layer IS NULL"
            + (" AND project_id=?" if project_id else ""),
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
        "instrumentation": instrumentation.report(
            con, instrumentation_limit, project_id),
    }


def runway_now(force: bool = False, con=None) -> dict:
    """``GET /api/runway`` (W-0182). ``force`` polls every provider first.

    Nothing else in Orchestra polls runway on a timer, so the dashboard's own
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


def projects_registry(con=None) -> dict:
    """``GET /api/projects`` — the whole project registry (DESIGN §2).

    NOT the picker. ``_projects`` derives the picker from the runs in the
    snapshot on purpose (W-0186), so a project with nothing recent is not in
    it — and a project with nothing recent is exactly the one a human comes
    here to park. The panel that archives therefore reads the registry.

    ``source_ref`` stays opaque (CONTRACT §7): it is compared against nothing
    and parsed for nothing. ``archived`` is already the DERIVED value
    (DESIGN §1); ``archived_override`` is NULL while the row still follows its
    source, which is the only way a surface can say "parked by the source"
    rather than "parked here".
    """
    own = con is None
    con = db.connect() if own else con
    try:
        rows = project.all_projects(con, include_archived=True)
        return {
            "projects": [{
                "project_id": p.project_id,
                "path": str(p.path),
                "name": p.name,
                "source_ref": p.source_ref,
                "archived": p.archived,
                "archived_override": p.archived_override,
            } for p in rows],
            "generated_at": db.now(),
        }
    finally:
        if own:
            con.close()


def control_turns(project_id: str = "", layer: str = "",
                  limit: int = RECENT_TURNS, con=None) -> dict:
    """``GET /api/turns`` (I-0081) — the control turns, newest first.

    The snapshot pins the LATEST turn per project. This is the series behind
    that one line: the observer's thinking, the router's staffing, the merge
    judge's verdicts, read as a log going back.

    The rows are the ordinary run payload, because a control turn IS a runs
    row — so the client opens one in the same detail screen and reads its
    transcript through the same trace renderer as any run.

    Empty ``project_id`` means every project, matching the client's own "all
    projects" scope. ``layer`` narrows to one of router/merge/observer/
    conductor and is filtered HERE, not on the client, so ``limit`` counts the
    turns that were asked for rather than whatever happened to be newest.
    """
    own = con is None
    con = db.connect() if own else con
    try:
        where = ["r.layer IS NOT NULL"]
        args: list = []
        if project_id:
            where.append("r.project_id=?")
            args.append(project_id)
        if layer:
            where.append("r.layer=?")
            args.append(layer)
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = RECENT_TURNS
        limit = max(1, min(limit, RECENT_TURNS))
        rows = con.execute(
            _RUN_SELECT + "WHERE " + " AND ".join(where) +
            " ORDER BY r.id DESC LIMIT ?", (*args, limit)).fetchall()
        return {
            "turns": [_run_payload(con, r, {}) for r in rows],
            "project_id": project_id or None,
            "layer": layer or None,
            "limit": limit,
            "generated_at": db.now(),
        }
    finally:
        if own:
            con.close()


def _feed_payload(r) -> dict:
    """One run as a CONSUMER sees it: enough to act on the outcome without a
    second call.

    The fields are the ones ``sweeper``'s reporting path actually reads off a
    run row — ``ref`` (which item), ``status``/``summary``/``landing_status``
    (what happened, and whether the landing settled), ``landing_commit``
    (the merge commit that landing produced, which is the only place a
    ``landed`` fact can get its sha and its revert line), ``branch`` and
    ``handoff_processed_at`` (whether the result is settled at all),
    ``requested_by`` (which lane reports itself), ``slug`` and ``no`` (the tag
    and the human number in every comment it posts), ``layer`` (a control
    turn is not a run) — plus the usage the row carries, so a consumer can
    price an outcome without reopening the run.

    Deliberately NOT the dashboard's ``_run_payload``: that one runs four
    subqueries and a message read per row, which is the wrong shape for a
    walk over every run ever recorded.
    """
    return {
        "id": r["id"],
        "revision": r["revision"],
        "ref": r["ref"],
        "slug": r["slug"],
        "no": None if r["layer"] else r["project_seq"],
        "layer": r["layer"],
        "project_id": r["project_id"],
        "status": r["status"],
        # Full, not truncated: a consumer's report carries the summary
        # onward, and the board comment is where the length limit belongs.
        "summary": r["summary"],
        "title": r["title"],
        "branch": r["branch"],
        "landing_status": r["landing_status"],
        "landing_commit": r["landing_commit"],
        "handoff_processed_at": r["handoff_processed_at"],
        "requested_by": r["requested_by"],
        "exit_code": r["exit_code"],
        "started_at": r["started_at"],
        "finished_at": r["finished_at"],
        "tokens_in": r["tokens_in"],
        "tokens_out": r["tokens_out"],
        "tokens_total": r["tokens_total"],
        "cost_usd": r["cost_usd"],
        "usage_source": r["usage_source"],
    }


def runs_since(since=0, limit=FEED_PAGE, con=None) -> dict:
    """``GET /api/runs?since=<revision>`` — what changed after that cursor.

    A range scan on ``runs.revision``, oldest change first. ``next_cursor``
    is the last row's own marker, so resuming from it repeats nothing and
    skips nothing; an empty page hands the cursor straight back. A row that
    changes again reappears with a higher marker — the feed carries CURRENT
    state per run, not a history of transitions, which is why it needs no
    event table behind it. A deleted run simply stops appearing.

    ``limit`` is echoed because a consumer cannot otherwise tell a full page
    from the end of the feed: ``len(runs) == limit`` means ask again.
    """
    own = con is None
    con = db.connect() if own else con
    try:
        try:
            since = max(0, int(since))
        except (TypeError, ValueError):
            since = 0
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = FEED_PAGE
        limit = max(1, min(limit, FEED_PAGE))
        rows = list(con.execute(
            "SELECT * FROM runs WHERE revision > ? ORDER BY revision LIMIT ?",
            (since, limit)))
        return {
            "runs": [_feed_payload(r) for r in rows],
            "cursor": since,
            "next_cursor": rows[-1]["revision"] if rows else since,
            "limit": limit,
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
            # v10 (W-0214 follow-up): the most recent control turn per
            # project, pinned at the top of the Runs tab. Never in ``runs``,
            # never in ``live_runs``. The client shows the one for the project
            # it is scoped to.
            # v5 (W-0186): what the project picker offers, derived from the
            # runs above — never a roster of every project on the machine.
            "projects": _projects(con, runs),
            "dispatch": pause_state(con),
            "profiles": _profiles(cfg),
            "runway": _runway(con),
            "statistics": _statistics(con),
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
        mode = run_isolation(r)
        return {"run": run_id, "text": None,
                "message": ("no branch — execution never started"
                            if mode == "not_started" else
                            "no branch — this run worked in the shared tree")}
    # A run that committed its own work leaves nothing for the checkpoint to
    # record, so the branch name was the only pointer — and merging deletes it.
    # merge_run anchors the head at refs/orchestra/run-N before that, so the
    # candidates run newest-first: checkpoint, kept ref, branch.
    base = r["base_commit"]
    short = str(r["branch"]).rsplit("/", 1)[-1]
    candidates = [c for c in (r["checkpoint_commit"], f"refs/orchestra/{short}",
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
    changed = con.execute(
        f"UPDATE runs SET status='killed', worker_status=COALESCE(worker_status, "
        f"'killed'), finished_at=? WHERE id=? "
        f"AND status NOT IN {db.TERMINAL_SQL}", (db.now(), run_id))
    con.commit()
    if changed.rowcount != 1:
        latest = _run_row(con, run_id)
        return {"run": run_id, "status": latest["status"], "changed": False}
    signalled = False
    has_worker = r["pid"] is not None
    signal_outcome = "gone" if not has_worker else "refused"
    if has_worker:
        signal_outcome, detail = proc.signal_owned_group(
            r["pid"], r["pid_identity"], signal.SIGTERM)
        signalled = signal_outcome == "signalled"
        if signal_outcome == "refused":
            print(f"orchestra http: run {run_id} not signalled: {detail}",
                  file=sys.stderr)
    try:
        supervise.finalize_if_unowned(
            con, run_id, worker_gone=signal_outcome == "gone")
    except Exception as exc:
        print(f"orchestra http: run {run_id} cleanup deferred: {exc}",
              file=sys.stderr)
    # Dependents of a killed prerequisite are declined by the daemon's next
    # tick (supervise.process_ready) — spawning supervisors from an HTTP
    # thread would race the loop for no gain.
    return {"run": run_id, "status": "killed", "changed": True,
            "signalled": signalled}


def tell_run(con, run_id: int, text: str, now: bool = False) -> dict:
    """`tell` is `orchestra interrupt`: a message delivered at the worker's
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
        messaging.queue_tell(con, run_id, "dashboard", text, r["log_path"])
    except messaging.RunClosed:
        latest = _run_row(con, run_id)
        return {"error": f"run {run_id} already {latest['status']} — dispatch a "
                         "fresh run instead"}
    if now:
        con.execute(f"UPDATE runs SET status='interrupt' WHERE id=? "
                    f"AND status NOT IN {db.TERMINAL_SQL}", (run_id,))
    con.commit()
    if now and r["pid"]:
        outcome, detail = proc.signal_owned_group(
            r["pid"], r["pid_identity"], signal.SIGTERM)
        if outcome == "refused":
            print(f"orchestra http: run {run_id} not signalled: {detail}",
                  file=sys.stderr)
    return {"run": run_id, "queued": True, "immediate": bool(now)}


def check_run(con, run_id: int, observe: bool = True) -> dict:
    """`orchestra check` on demand (DESIGN §7).

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
    from orchestra import observer  # SEAM (W-0166): observer imports this
    return observer.check(con, r, result, model=observe)


# --- the SSE seam (W-0165) --------------------------------------------------

def sse_stream(handler, path: str) -> bool:
    """SEAM: the trace stream and the daemon's own log over SSE (DESIGN §3
    "Liveness", §7).

    Called for any ``GET /api/**/stream`` after the Host check and auth have
    both passed. Writes the whole response (``text/event-stream``, no
    Content-Length, flush per event) and returns True; an unknown stream path
    returns False and the caller answers 501.

    Four routes, three kinds of resume cursor, all carried by
    ``Last-Event-ID``: ``/api/runs/<id>/stream`` resumes on the integer event
    id it last sent, ``/api/runs/<id>/log/stream`` and ``/api/log/stream``
    on the composite ``file@offset`` cursor
    ``traces.parse_daemon_cursor`` decodes, and ``/api/board/stream`` on the
    board revision. A browser's ``EventSource`` replays that header itself,
    which is why the cursor lives there and not in a query string.

    The board stream carries NO payload, unlike the other two: it says only
    that ``meta.board_revision`` moved, and the client refetches
    ``/api/snapshot``. That is what retired the dashboard's 4s poll without
    growing a second copy of the snapshot's auth, shape and version.

    ``EventSource`` cannot set headers, so this route is reached with the
    cookie the dashboard already holds — hence GET, and hence never a POST.
    """
    resume = (handler.headers.get("Last-Event-ID") or "").strip()
    stop = getattr(handler.server, "sse_stop", None)
    match = _TRACE_STREAM.match(path)
    raw = _RUN_LOG_STREAM.match(path)
    if match:
        after_id = int(resume) if resume.isdigit() else 0
        frames = traces.stream_run_trace(int(match.group(1)), after_id, stop=stop)
    elif raw:
        # Same ``name@offset`` cursor as the daemon log, because the tail
        # underneath is the same one — only the file differs.
        frames = traces.stream_run_log(int(raw.group(1)),
                                       traces.parse_daemon_cursor(resume),
                                       stop=stop)
    elif path == LOG_STREAM:
        frames = traces.stream_daemon_log(traces.parse_daemon_cursor(resume),
                                          stop=stop)
    elif path == BOARD_STREAM:
        frames = traces.stream_board(int(resume) if resume.isdigit() else 0,
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

# A client that resets the socket while ThreadingHTTPServer is still
# reading the request line is not a bug in the handler; the stdlib dumps
# a full traceback for it anyway.
_DROPPED = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)


class Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        exc = sys.exception()
        if isinstance(exc, _DROPPED):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "orchestra"
    sys_version = ""
    identity = None  # set by _gate, before any route runs

    def handle(self):
        try:
            super().handle()
        except _DROPPED:
            pass

    # -- plumbing
    def log_message(self, fmt, *args):  # noqa: A003 — BaseHTTPRequestHandler API
        print(f"orchestra http: {self.client_address[0]} {fmt % args}", flush=True)

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
        if path == PROJECTS_ROUTE:
            return self._json(projects_registry())
        if path == RUNS_ROUTE:
            return self._json(runs_since(
                (query.get("since") or ["0"])[0].strip(),
                (query.get("limit") or [FEED_PAGE])[0]))
        if path == TURNS_ROUTE:
            return self._json(control_turns(
                (query.get("project") or [""])[0].strip(),
                (query.get("layer") or [""])[0].strip(),
                (query.get("limit") or [RECENT_TURNS])[0]))
        if path == CONFIG_ROUTE:
            cfg_path = config.ensure_global_config()
            return self._json({"path": str(cfg_path),
                               "text": cfg_path.read_text(encoding="utf-8")})
        if path == SEATS_ROUTE:
            return self._json(seats_payload())
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
        if path == SEATS_ROUTE:
            seat = str(body.get("seat") or "")
            if seat not in SEATS:
                return self._deny(400, "unknown seat "
                                  f"(known: {', '.join(sorted(SEATS))})")
            name = body.get("profile")
            if name is not None:
                name = str(name).strip() or None
            if name is not None and name not in config.load().get("profiles", {}):
                return self._deny(400, f"unknown profile '{name}'")
            try:
                return self._json(set_seat(seat, name))
            except ValueError as exc:
                return self._json({"applied": False, "error": str(exc)}, 400)
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
        if path == PROJECTS_ROUTE:
            # Park or unpark ONE project, source-backed or not (DESIGN §1).
            # No rule is restated here: project.set_archived is the same call
            # `orchestra project archive` makes, it writes the owner's
            # override, and the lanes that skip a parked project keep reading
            # the one derived value.
            root = str(body.get("path") or "").strip()
            if not root:
                return self._deny(400, "POST /api/projects needs path")
            want = bool(body.get("archived"))
            con = db.connect()
            try:
                known = project.set_archived(con, Path(root), want)
                # Parking a project changes the picker, and the projects
                # table has no trigger of its own. Without this bump another
                # open board keeps the parked project until a RUN happens to
                # change — the same gap `set_dispatch_paused` closes.
                if known:
                    db.bump_board_revision(con)
                    con.commit()
            finally:
                con.close()
            if not known:
                return self._deny(404, f"{root} is not a registered project")
            return self._json({"path": root, "archived": want})
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
        print("orchestra http: no shared secret configured — HTTP surface off; "
              "run `orchestra init`", flush=True)
        return None
    addr = bind_address(cfg) if addr is None else addr
    port = int(http_cfg(cfg).get("port") or DEFAULT_PORT) if port is None else port
    try:
        srv = Server((addr, port), Handler)
    except OSError as exc:
        print(f"orchestra http: cannot bind {addr}:{port} — {exc}",
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
    threading.Thread(target=srv.serve_forever, name="orchestra-http",
                     daemon=True).start()
    if stop is not None:
        threading.Thread(target=_shutdown_when(stop, srv), daemon=True).start()
    # A second socket on loopback, so the dashboard answers at
    # http://localhost:PORT as well as over the tailnet. Two named sockets,
    # never 0.0.0.0: this machine joins networks that are nobody's friend.
    srv.local = None
    if addr not in ("127.0.0.1", "::1", "localhost"):
        try:
            local = Server(("127.0.0.1", srv.server_port), Handler)
        except OSError as exc:
            print(f"orchestra http: no loopback listener — {exc}",
                  file=sys.stderr, flush=True)
        else:
            local.daemon_threads = True
            local.api_key, local.wake, local.sse_stop = key, wake, stop
            local.restart = restart
            local.allowed_hosts = srv.allowed_hosts
            threading.Thread(target=local.serve_forever,
                             name="orchestra-http-local", daemon=True).start()
            if stop is not None:
                threading.Thread(target=_shutdown_when(stop, local),
                                 daemon=True).start()
            srv.local = local
    print(f"orchestra http: http://{addr}:{srv.server_port}/ "
          f"({HEADER} required on every route)", flush=True)
    return srv


def _shutdown_when(stop: threading.Event, srv):
    def wait():
        stop.wait()
        srv.shutdown()
        srv.server_close()
    return wait
