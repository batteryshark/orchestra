"""Single owner of Dromond's SQLite schema (clean v1 — a greenfield has no
legacy, so there are no migrations; future subsystems join THIS schema and
bump meta.schema_version when they extend it).

Data-model invariants (DESIGN D4):
- ``runs.id`` is the only address for a run. ``runs.slug`` is a memorable
  display alias (UNIQUE so concurrent dispatchers cannot double-mint one).
- ``runs.profile`` records which launch template started the run — pure
  provenance/display. Nothing keys on it.
- ``messages`` address run ids. There is deliberately no recipient-name
  column: a profile is a launch template, never a worker identity.
  ``undeliverable_at``/``undeliverable_reason`` (schema v9, DESIGN §6) mark a
  message whose run ended before it was delivered. Marked and surfaced, never
  dropped — and never moved to a later run, which would hand a correction to
  a run that never saw the context it referred to.
- ``runs.work_item`` (schema v2, the sweeper) is the durable mapping to the
  Work item (``W-####`` task or ``issue_*``) that a run serves;
  ``work_seen_ts`` is the ferry watermark for that item's thread, and
  ``work_reported_at`` marks that the completion writeback happened.
- ``runs.project_id`` (schema v4, the daemon) is Work's immutable
  ``projectId``. Rows key on it, never on a path: one central database now
  holds every project's runs, and renaming a folder must lose nothing.
  ``runs.workdir`` stays a path because it is where a process actually ran.
- ``dispatch_queue`` (schema v8, DESIGN §4) holds Work items that cannot
  start yet, with the reason. It is queue state only: nothing dispatches
  from a row here, and the run row appears at actual dispatch.
- ``runway_polls`` (schema v5, DESIGN §11) is a self-contained append-only
  log of provider runway polls — one row per adapter per poll, unknowns
  included. It references nothing and nothing references it. ``windows``
  (v12, W-0179) is every window that poll reported; the scalar columns are
  the tightest live one, which is what dispatch and the trend read.
- ``events`` (schema v6, DESIGN §7) is the ONE normalized trace table: every
  backend's JSONL is mapped into the same seven kinds at ingest. The raw
  file stays the source of truth — each row carries a truncated payload
  plus the byte offset/length of the line it came from, so a viewer expands
  in place. ``trace_cursors`` is the per-run tail watermark (and the record
  that a terminal run's raw log was pruned).
- ``finding_fingerprints`` (schema v9, DESIGN §9) is the dedup ledger for
  filed findings: one row per ``(project, where, normalized claim)``, so a
  repeat increments ``occurrences`` and comments on ``issue_id`` instead of
  filing a duplicate Work issue. Owned by ``findings.py``.
- ``observations`` (schema v9, DESIGN §7) is the spin observer's record: one
  row per judgement, from any of its three layers, with the reasoning that
  produced it. It exists because the observer may never silently kill a run
  — a stop has to leave its reasoning somewhere durable — and because the
  hourly cadence anchors on the last row for a run. ``runs.retry_of`` (also
  v9) is the retry lineage: which run this one is the single automatic
  retry of, so a second consecutive infrastructure failure is countable.
- ``runs.tokens_*``/``cost_usd``/``usage_source`` (schema v9, DESIGN §11) are
  the backend's own usage totals, stamped on the row at completion so
  statistics are a query and never a re-parse. All NULL when the backend
  reported nothing recognizable — ``usage_source`` names the parser that
  produced the numbers, and NULL there means "not captured", never "zero".
- ``conductor_turns`` (schema v10, DESIGN §10) is the conductor's log AND its
  only state: one row per planner turn, carrying the trigger and the key that
  makes that trigger fire exactly once. Owned by ``conductor.py``; references
  Work item ids rather than run ids, because a planner turn is not a run.
- ``runs.run_token_hash`` (schema v11, DESIGN §3/§5, W-0176) is the SHA-256 of
  the per-run token minted at dispatch into the worker's environment. Only the
  hash is ever stored — the raw token lives in the worker's environment and
  nowhere else, the same discipline the config applies to provider keys.
  Revocation is the ``revoke_run_token`` trigger below rather than a call
  every finalizer has to remember: reaching a terminal status nulls the hash,
  whichever code path got the run there. Owned by ``auth.py``.
- ``nod_requests`` (schema v7, the human loop) maps a Nod request id to the
  run and Work item it escalated, so a decision can be mirrored into the
  Work thread. ``channel`` is stored because a Nod issuer token is scoped to
  exactly one channel: a later decision/wait/cancel read has to pick the
  credential for the channel the card was filed to, never guess.
  ``acted_at`` (schema v14) marks that the daemon's answers pass acted on
  the card's decision — stamped exactly once, so an answered card never
  retriggers on the next tick. Owned by ``nod.py``; carries no issuer token.
"""
import sqlite3
from datetime import datetime, timezone

from dromond import paths

SCHEMA_VERSION = "14"

# Columns added after v1; applied idempotently so an older database upgrades
# in place (greenfield policy: extensions, not migration files).
RUNS_V2_COLUMNS = (
    ("work_item", "TEXT"),
    ("work_seen_ts", "TEXT"),
    ("work_reported_at", "TEXT"),
    ("work_progress_at", "TEXT"),
)
RUNS_V4_COLUMNS = (
    ("project_id", "TEXT"),
)
RUNS_V9_COLUMNS = (
    ("retry_of", "INTEGER"),
    ("tokens_in", "INTEGER"),
    ("tokens_out", "INTEGER"),
    ("tokens_total", "INTEGER"),
    ("cost_usd", "REAL"),
    ("usage_source", "TEXT"),
)
RUNS_V11_COLUMNS = (
    ("run_token_hash", "TEXT"),
)
# Schema v13 (W-0183). The staffing turn's one line: which profile it chose
# and why, or why it fell back to the [work] profile. NULL means routing was
# off for that dispatch — there was no decision to explain.
RUNS_V13_COLUMNS = (
    ("routed_reason", "TEXT"),
)

# Schema v12 (DESIGN §11, W-0179). ``runway_polls.windows`` is the JSON list
# of EVERY window the provider reported in that poll — Claude's 5-hour and
# weekly limits are two facts. The scalar columns stay the tightest live one.
RUNWAY_V12_COLUMNS = (
    ("windows", "TEXT"),
)

# Schema v9 (DESIGN §6, messaging). A message that never reached its run is
# MARKED, never dropped and never re-aimed at a later run.
MESSAGES_V9_COLUMNS = (
    ("undeliverable_at", "TEXT"),
    ("undeliverable_reason", "TEXT"),
)

# Schema v14 (the human loop, acting half). ``acted_at`` is the answers
# pass's once-and-only-once stamp: NULL means the decision has not been
# acted on yet, anything else means it must never trigger an action again.
NOD_REQUESTS_V14_COLUMNS = (
    ("acted_at", "TEXT"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  slug TEXT UNIQUE,
  profile TEXT NOT NULL,
  backend TEXT NOT NULL,
  model TEXT,
  title TEXT,
  requested_by TEXT NOT NULL,
  brief_path TEXT,
  log_path TEXT,
  workdir TEXT NOT NULL,
  branch TEXT,
  base_commit TEXT,
  checkpoint_commit TEXT,
  parent_run INTEGER REFERENCES runs(id),
  pid INTEGER,
  supervisor_pid INTEGER,
  session_ref TEXT,
  status TEXT NOT NULL DEFAULT 'spawning',
  exit_code INTEGER,
  summary TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  work_item TEXT,
  work_seen_ts TEXT,
  work_reported_at TEXT,
  work_progress_at TEXT,
  project_id TEXT,
  retry_of INTEGER REFERENCES runs(id),
  tokens_in INTEGER,
  tokens_out INTEGER,
  tokens_total INTEGER,
  cost_usd REAL,
  usage_source TEXT,
  run_token_hash TEXT,
  routed_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_token ON runs(run_token_hash);
-- Schema v11 (W-0176). A per-run token dies with its run. The revocation is
-- a trigger and not a call in each finalizer, because there are five paths to
-- a terminal status (supervisor, sweeper reaper, HTTP stop, `dromond kill`,
-- observer) and one of them would eventually forget.
CREATE TRIGGER IF NOT EXISTS revoke_run_token AFTER UPDATE OF status ON runs
WHEN NEW.status IN ('done','failed','timeout','killed')
     AND NEW.run_token_hash IS NOT NULL
BEGIN
  UPDATE runs SET run_token_hash=NULL WHERE id=NEW.id;
END;
CREATE INDEX IF NOT EXISTS idx_runs_parent_run ON runs(parent_run);
CREATE INDEX IF NOT EXISTS idx_runs_work_item ON runs(work_item);
CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project_id);
-- Cached Work project list, so an offline CLI still resolves a directory to
-- a project. One row per local path (an aliasPath gets its own row); the
-- projectId is what everything else keys on.
CREATE TABLE IF NOT EXISTS projects (
  path TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  work_id TEXT,
  name TEXT,
  refreshed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_projects_project_id ON projects(project_id);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES runs(id),
  sender TEXT NOT NULL,
  body TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  delivery_offset INTEGER,
  delivered_at TEXT,
  undeliverable_at TEXT,
  undeliverable_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_run ON messages(run_id);
CREATE TABLE IF NOT EXISTS dispatch_dependencies (
  run_id INTEGER NOT NULL REFERENCES runs(id),
  depends_on_run INTEGER NOT NULL REFERENCES runs(id),
  kind TEXT NOT NULL DEFAULT 'requires_success'
    CHECK(kind IN ('requires_success', 'wait_for')),
  PRIMARY KEY(run_id, depends_on_run)
);
CREATE INDEX IF NOT EXISTS idx_dispatch_dependencies_prerequisite
  ON dispatch_dependencies(depends_on_run);
CREATE TABLE IF NOT EXISTS deferred_dispatches (
  run_id INTEGER PRIMARY KEY REFERENCES runs(id),
  mission TEXT NOT NULL,
  context TEXT,
  use_worktree INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending',
  error TEXT,
  created_at TEXT NOT NULL,
  processed_at TEXT
);
-- Honest queue state (schema v6, DESIGN §4): a Work item that cannot start
-- yet waits HERE, with the reason it waits, instead of being moved to
-- in_progress on entering a queue. One row per item; ``id`` is the FIFO
-- tiebreak and ``lane_index`` the ready-lane board position it last had.
CREATE TABLE IF NOT EXISTS dispatch_queue (
  id INTEGER PRIMARY KEY,
  item_id TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,
  reason TEXT NOT NULL,
  detail TEXT,
  lane_index INTEGER,
  enqueued_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runway_polls (
  id INTEGER PRIMARY KEY,
  provider TEXT NOT NULL,
  remaining REAL,
  limit_value REAL,
  unit TEXT,
  resets_at TEXT,
  as_of TEXT,
  reason TEXT,
  raw TEXT,
  windows TEXT,
  polled_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runway_polls_provider
  ON runway_polls(provider, polled_at);
-- Schema v6 (DESIGN §7). One normalized trace row per interesting JSONL
-- line, for every backend. `payload` is truncated to ~2KB; `payload_len` is
-- the untruncated length; `byte_offset`/`byte_length` locate the raw line so
-- a viewer expands from the file itself. `byte_offset = -1` marks an event
-- with no raw backing (a human injection Dromond recorded directly).
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES runs(id),
  seq INTEGER NOT NULL,
  kind TEXT NOT NULL,
  name TEXT,
  payload TEXT NOT NULL DEFAULT '',
  payload_len INTEGER NOT NULL DEFAULT 0,
  truncated INTEGER NOT NULL DEFAULT 0,
  byte_offset INTEGER NOT NULL DEFAULT -1,
  byte_length INTEGER NOT NULL DEFAULT 0,
  ts TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(run_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);
CREATE TABLE IF NOT EXISTS trace_cursors (
  run_id INTEGER PRIMARY KEY REFERENCES runs(id),
  byte_offset INTEGER NOT NULL DEFAULT 0,
  seq INTEGER NOT NULL DEFAULT 0,
  skipped INTEGER NOT NULL DEFAULT 0,
  raw_pruned_at TEXT,
  updated_at TEXT NOT NULL
);
-- Schema v9 (DESIGN §7, the spin observer). One row per judgement: which
-- layer produced it (stall / mechanical / observer / retry / planner), what
-- it did (ok / tell / stop / retry / escalate / deferred), and WHY. The why
-- is the point: a stop must never be silent, and the hourly observer cadence
-- measures from the last row for the run.
CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES runs(id),
  layer TEXT NOT NULL,
  action TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  detail TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_observations_run ON observations(run_id, id);
CREATE TABLE IF NOT EXISTS nod_requests (
  request_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  channel TEXT NOT NULL,
  run_id INTEGER REFERENCES runs(id),
  work_item TEXT,
  dedupe_key TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  option_id TEXT,
  option_kind TEXT,
  decision_text TEXT,
  decided_at TEXT,
  mirrored_at TEXT,
  acted_at TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nod_requests_run ON nod_requests(run_id);
CREATE INDEX IF NOT EXISTS idx_nod_requests_work ON nod_requests(work_item);
-- Schema v9 (DESIGN §9). ``fingerprint`` is the hash of (project, where,
-- normalized claim); ``issue_id`` is the Work issue the first occurrence
-- filed, which every repeat comments on instead of duplicating.
CREATE TABLE IF NOT EXISTS finding_fingerprints (
  fingerprint TEXT PRIMARY KEY,
  project_id TEXT,
  location TEXT NOT NULL,
  claim TEXT NOT NULL,
  issue_id TEXT,
  occurrences INTEGER NOT NULL DEFAULT 1,
  first_run INTEGER,
  last_run INTEGER,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_finding_fingerprints_project
  ON finding_fingerprints(project_id);
-- Schema v10 (DESIGN §10, the conductor). One row per planner turn, and the
-- conductor's whole memory: the ~2-minute floor, the wait gate, the
-- once-per-event guard and the delta watermark are all queries against it.
-- ``trigger_kind`` + ``trigger_key`` is what makes a trigger fire once and
-- only once — the key is the batch that settled, the run that blocked, the
-- comment's timestamp. ``wait_event`` is the event a `wait` turn named, and
-- until it arrives nothing else wakes that goal. A `wait` turn lives ONLY
-- here: it never reaches the goal's Work thread.
CREATE TABLE IF NOT EXISTS conductor_turns (
  id INTEGER PRIMARY KEY,
  goal_id TEXT NOT NULL,
  trigger_kind TEXT NOT NULL,
  trigger_key TEXT NOT NULL DEFAULT '',
  slug TEXT,
  profile TEXT,
  action TEXT NOT NULL,
  rationale TEXT NOT NULL DEFAULT '',
  wait_event TEXT,
  comment_ts TEXT,
  packet_tokens INTEGER,
  detail TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conductor_turns_goal
  ON conductor_turns(goal_id, id);
"""

RUN_TERMINAL = ("done", "failed", "timeout", "killed")
TERMINAL_SQL = "('done','failed','timeout','killed')"


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_file=None) -> sqlite3.Connection:
    """The central database (DESIGN §2). ``db_file`` is for `dromond migrate`,
    which opens a legacy per-project database read-side."""
    con = sqlite3.connect(db_file or paths.db_path(), timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=10000")
    existing = {r["name"] for r in con.execute("PRAGMA table_info(runs)")}
    if existing:  # extend a pre-existing table before SCHEMA's indexes run
        for name, sql_type in (RUNS_V2_COLUMNS + RUNS_V4_COLUMNS
                               + RUNS_V9_COLUMNS + RUNS_V11_COLUMNS
                               + RUNS_V13_COLUMNS):
            if name not in existing:
                con.execute(f"ALTER TABLE runs ADD COLUMN {name} {sql_type}")
    polls = {r["name"] for r in con.execute("PRAGMA table_info(runway_polls)")}
    if polls:
        for name, sql_type in RUNWAY_V12_COLUMNS:
            if name not in polls:
                con.execute(f"ALTER TABLE runway_polls ADD COLUMN {name} {sql_type}")
    have = {r["name"] for r in con.execute("PRAGMA table_info(messages)")}
    if have:
        for name, sql_type in MESSAGES_V9_COLUMNS:
            if name not in have:
                con.execute(f"ALTER TABLE messages ADD COLUMN {name} {sql_type}")
    cards = {r["name"] for r in con.execute("PRAGMA table_info(nod_requests)")}
    if cards:
        for name, sql_type in NOD_REQUESTS_V14_COLUMNS:
            if name not in cards:
                con.execute(f"ALTER TABLE nod_requests ADD COLUMN {name} {sql_type}")
    con.executescript(SCHEMA)
    con.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
        (SCHEMA_VERSION,),
    )
    con.commit()
    return con


def meta_get(con: sqlite3.Connection, key: str) -> str | None:
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def meta_set(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)", (key, value))
