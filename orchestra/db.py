"""Single owner of Orchestra's SQLite schema (clean v1 — a greenfield has no
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
- ``runs.ref`` (schema v21, CONTRACT §7 Enforcement 1) is the OPAQUE string a
  caller hands in at dispatch to say what the run is FOR. The core stores it,
  echoes it back, and never parses it — a Work ``W-####`` task id today, a
  Linear key or anything else tomorrow, and this table cannot tell the
  difference. It replaced ``work_item`` (schema v2) and the three ``work_*``
  timestamps that sat beside it, which were never facts about a run: a
  watermark over one source's board is that source's own bookkeeping.
- ``work_marks`` (schema v21, CONTRACT §7) is where those three timestamps
  went — ``seen_ts`` is the ferry watermark for the ref's thread,
  ``reported_at`` is the receipt that the completion writeback happened, and
  ``progress_at`` is the clock the heartbeat pass rate-limits on. One row per
  run, written on the first mark. Owned by ``sweeper.py``, the Work adapter,
  exactly as ``conductor_turns`` is owned by ``conductor.py``; nothing in the
  core reads it, and a second source's adapter brings its own table rather
  than columns on ``runs``.
- ``runs.claim_status`` (schema v16 as ``work_claim_status``, renamed in v25)
  is the ADMISSION GATE, and it stayed on ``runs`` when the watermarks left
  because the core genuinely reads it: ``daemon._reap_orphans`` must not reap
  a run parked in ``spawning`` while its dispatch is still being confirmed
  somewhere else. ``pending`` / ``claimed`` / ``abandoned`` describe the run's
  own lifecycle; the core never learns what the claim was made against, so the
  name stopped saying Work (CONTRACT §7 Enforcement 1).
- ``runs.project_id`` (schema v4, the daemon) is the registry's stable
  project id — a local UUID, or whatever id a source adapter cached. Rows key
  on it, never on a path: one central database now holds every project's
  runs, and renaming a folder must lose nothing.
  ``runs.workdir`` stays a path because it is where a process actually ran.
- ``projects.slug`` (schema v27) is the project's HUMAN address: lowercase
  kebab-case, minted once from the project's name and never rewritten by a
  refresh. Unique across project ids (alias rows of one project share it) —
  enforced at mint time, not by SQL, exactly because those alias rows share
  it. ``dispatch --project``, the HTTP dispatch route, and the per-project
  directory ``~/.orchestra/projects/<slug>/`` all key on it.
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
- ``runs.layer`` (schema v15, W-0214) marks a CONTROL TURN — a router,
  merge-judge, observer or conductor model call — recorded as a terminal
  runs row so its transcript normalizes into ``events`` and opens in the
  same detail screen as a run. NULL is a worker run. Fleet queries exclude
  turns with ``layer IS NULL``; queries keyed on ref, branch or parent_run
  never match one, because a turn carries none of them.
- ``runs.landing_status``/``handoff_processed_at`` (schema v16, W-0295) are
  the two durable policy receipts on the terminal result row. NULL means that
  policy has not settled yet; landing records ``ok`` or ``failed``, while
  handoff processing records its completion timestamp. ``landing_commit``
  (schema v23) is the merge commit that verdict produced, NULL when the
  landing made none. It is the whole receipt a reporting consumer needs: the
  landing path writes it and posts nothing (CONTRACT §7 Enforcement). ``pid_identity`` is
  the worker process's kernel creation token; orphan recovery matches it before
  signaling the stored PID, which may otherwise have been reused.
  ``supervisor_pid_identity`` applies the same reuse check to the process that
  owns finalization, though it is never signaled. ``worker_status``/
  ``worker_exit_code`` preserve a supervised process outcome before slower
  result enrichment begins. Worker finalization and explicit stop/reaper paths
  write them; synthetic terminal rows do not become replay candidates merely
  because their status changed. Historical rows remain NULL.
- ``nod_requests`` (schema v7, the human loop) maps a Nod request id to the
  run and the ``ref`` it escalated, so a source adapter can carry the answer
  onward. That column was ``work_item`` until schema v25 and is now the same
  OPAQUE string ``runs.ref`` is: the caller supplies it, the core stores and
  echoes it, and only an adapter knows it spells a Work id today (CONTRACT §7
  Enforcement 1). ``channel`` is stored because a Nod issuer token is scoped to
  exactly one channel: a later decision/wait/cancel read has to pick the
  credential for the channel the card was filed to, never guess.
  ``acted_at`` (schema v14) marks that the daemon's answers pass acted on
  the card's decision — stamped exactly once, so an answered card never
  retriggers on the next tick. Owned by ``nod.py``; carries no issuer token.
- ``projects.archived`` (schema v20, DESIGN §1) mirrors the source's own flag,
  and ``projects.archived_override`` (schema v26) is the owner's answer on top
  of it: effective archived is ``COALESCE(archived_override, archived, 0)``,
  spelled once in ``project.ARCHIVED_SQL``. An archived project is PARKED, so
  the unattended lanes skip its items and the listing surfaces hide it. It hides nothing that already happened — every
  query over ``runs`` ignores this column, so history, statistics and run
  lookup are untouched, and a live run is never disturbed by it.
- ``meta['board_revision']`` (schema v19, DESIGN §3) is the dashboard's
  invalidation counter: three triggers on ``runs`` bump it on every insert,
  update and delete. It exists so the board can be TOLD something changed
  instead of asking every four seconds; the number itself is the SSE event id
  on ``/api/board/stream``, so a reconnect resumes on it. Triggers rather
  than a call in each writer, for the same reason ``revoke_run_token`` is a
  trigger: every path that touches a run must bump it. ``runs`` alone —
  everything else the snapshot reads (pause state, runway, health) rides the
  explicit bump ``http.record_health`` makes once per sweeper tick.
- ``runs.revision`` (schema v22, CONTRACT §7 Enforcement 2) is that same
  counter STAMPED ON THE ROW: the monotonic marker of when this run last
  changed. The global number says only THAT something changed; the column
  says WHICH run, so a consumer keeps a cursor and reads the rows past it
  (``http.runs_since``, ``GET /api/runs?since=``). It is the whole outbound
  feed — Orchestra keeps no subscriber list, no endpoint and no delivery
  state, because the consumer's cursor is the delivery guarantee. Indexed,
  since every read of it is a range scan. Stamped by the same triggers, for
  the same reason: every path that touches a run must mark it.
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import PurePath

from orchestra import paths

SCHEMA_VERSION = "28"

# Columns added after v1; applied idempotently so an older database upgrades
# in place (greenfield policy: extensions, not migration files). ``ref``
# (schema v2's ``work_item``) is deliberately NOT in this list: v21 either
# RENAMES the old column or adds the new one, never both — see ``connect``.
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
# Schema v15 (W-0214). ``layer`` marks a CONTROL TURN — router / merge /
# observer / conductor — recorded as a terminal runs row so its transcript
# ingests into the same events table and opens in the same detail screen.
# NULL is a worker run. Fleet queries (the snapshot, the statistics, the
# performance review) exclude them with ``layer IS NULL``; queries keyed on
# ref / branch / parent_run never match one, because a control turn
# carries none of them.
RUNS_V15_COLUMNS = (
    ("layer", "TEXT"),
)
# Schema v16 (W-0295). Landing, handoff, worker recovery, and claim handoff
# all stamp the plain run row; no result object or replay table is needed.
# ``claim_status`` (v16's ``work_claim_status``) is deliberately NOT in this
# list: v25 either RENAMES the old column or adds the new one, never both —
# see ``connect``.
RUNS_V16_COLUMNS = (
    ("landing_status", "TEXT"),
    ("handoff_processed_at", "TEXT"),
    ("pid_identity", "TEXT"),
    ("supervisor_pid_identity", "TEXT"),
    ("worker_status", "TEXT"),
    ("worker_exit_code", "INTEGER"),
)
# Schema v17. A run may ask for HELP: a weaker profile to take a bounded
# piece while it keeps the mission. ``parent_run`` already says who spawned
# it; these say how deep the tree goes, which request produced this child,
# and how its lead was told the batch had settled — once, whether the lead
# was still running (a message) or already finished (a continuation run).
# Schema v18. THE number a human reads: this project's own count, dense and
# starting at 1. ``id`` stays the internal key every foreign key points at,
# and stops being shown. A single global sequence was read as a per-project
# run count and could not be: control turns took 146 of the first 300
# numbers, and five projects shared the rest, so PREX3's 105 runs were
# spread across ids 1 to 299 (2026-08-27). A control turn is not a run and
# gets no number.
RUNS_V18_COLUMNS = (
    ("project_seq", "INTEGER"),
)
# Schema v22 (CONTRACT §7 Enforcement 2). The change marker the cursored read
# scans. Written only by the triggers below; NULL only between the ALTER and
# the one-shot backfill in ``connect``.
RUNS_V22_COLUMNS = (
    ("revision", "INTEGER"),
)
# Schema v23 (CONTRACT §7 Enforcement). The merge commit a landing produced,
# beside the ``ok``/``failed`` verdict that already sits there. The landing
# path used to keep this fact to itself and post it to Work directly; now it
# writes the receipt and a consumer reads it.
RUNS_V23_COLUMNS = (
    ("landing_commit", "TEXT"),
)
RUNS_V17_COLUMNS = (
    ("child_depth", "INTEGER"),
    ("spawn_request_id", "INTEGER"),
    ("child_wakeup_run", "INTEGER"),
    ("child_wakeup_message", "INTEGER"),
)

# Schema v20 (DESIGN §1). A plain boolean: this project is parked. A
# source-backed row gets it from the source's adapter on every refresh, so
# parking a project there parks it here with no local action; a locally
# adopted row is parked with ``orchestra project archive``. Core code reads
# the flag and never asks who set it (CONTRACT §7).
PROJECTS_V20_COLUMNS = (
    ("archived", "INTEGER NOT NULL DEFAULT 0"),
)

# Schema v26 (DESIGN §1). The OWNER'S OWN answer, above the source's: NULL
# follows ``archived``, 0 and 1 override it. A PURE ADD — nothing is renamed,
# dropped or migrated, so a stale pre-v26 writer that re-adds its own columns
# leaves this one alone and every old row keeps its effective state through
# the COALESCE (2026-08-28, why ``_retire_resurrected`` exists).
PROJECTS_V26_COLUMNS = (
    ("archived_override", "INTEGER"),
)

# Schema v27. The human address (see the data-model note above). Backfilled
# once by ``_backfill_project_slugs``; new rows are minted in ``project.py``.
PROJECTS_V27_COLUMNS = (
    ("slug", "TEXT"),
)

# Schema v28. ``repo`` is the CHECKOUT this run branched from and lands into.
# A project is not one checkout: a dispatch may name any path (``--path``),
# so the registry's default cannot answer "where does this run's branch
# live" — and landing into the wrong repository is not a small mistake.
# Stamped at launch preparation; NULL on older rows, where ``root_for``
# falls back to the registry as before.
RUNS_V28_COLUMNS = (
    ("repo", "TEXT"),
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

# Schema v24 (CONTRACT §7 Enforcement). What the card SAID. An escalation
# record that keeps only a pointer to the push device is not durable: a
# consumer that carries the same escalation somewhere else — a source
# adapter filing a decision — has to be able to read it back without asking
# Nod, and without the filing module knowing who reads it.
NOD_REQUESTS_V24_COLUMNS = (
    ("title", "TEXT"),
    ("body", "TEXT"),
)

# Declared apart from SCHEMA because the v21 migration below has to fill it
# before SCHEMA runs.
WORK_MARKS_SQL = """
-- Schema v21 (CONTRACT §7 Enforcement 1). The Work adapter's bookkeeping
-- about its OWN board, keyed by the run it concerns: how far that ref's
-- thread has been ferried, whether the completion writeback landed, and when
-- the heartbeat last posted. These were columns on ``runs`` and were never
-- facts about a run. Owned by ``sweeper.py`` (with the Work-facing tails of
-- ``verify.py`` and ``merge.py``); no core module reads this table, the same
-- arrangement ``conductor_turns`` has with ``conductor.py``.
CREATE TABLE IF NOT EXISTS work_marks (
  run_id INTEGER PRIMARY KEY REFERENCES runs(id),
  seen_ts TEXT,
  reported_at TEXT,
  progress_at TEXT
);
"""

SCHEMA = WORK_MARKS_SQL + """
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
  pid_identity TEXT,
  supervisor_pid INTEGER,
  supervisor_pid_identity TEXT,
  session_ref TEXT,
  status TEXT NOT NULL DEFAULT 'spawning',
  exit_code INTEGER,
  summary TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  ref TEXT,
  project_id TEXT,
  retry_of INTEGER REFERENCES runs(id),
  tokens_in INTEGER,
  tokens_out INTEGER,
  tokens_total INTEGER,
  cost_usd REAL,
  usage_source TEXT,
  run_token_hash TEXT,
  routed_reason TEXT,
  layer TEXT,
  landing_status TEXT,
  handoff_processed_at TEXT,
  worker_status TEXT,
  worker_exit_code INTEGER,
  claim_status TEXT,
  child_depth INTEGER,
  spawn_request_id INTEGER,
  child_wakeup_run INTEGER,
  child_wakeup_message INTEGER,
  project_seq INTEGER,
  revision INTEGER,
  landing_commit TEXT,
  repo TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_token ON runs(run_token_hash);
-- A per-run token dies with its run. This is a trigger and not a call in each
-- finalizer because every terminal writer must revoke the credential.
CREATE TRIGGER IF NOT EXISTS revoke_run_token AFTER UPDATE OF status ON runs
WHEN NEW.status IN ('done','failed','timeout','killed','halted')
     AND NEW.run_token_hash IS NOT NULL
BEGIN
  UPDATE runs SET run_token_hash=NULL WHERE id=NEW.id;
END;
-- The board's invalidation counter (DESIGN §3) and, since v22, the run's own
-- change marker (CONTRACT §7 Enforcement 2): every write to runs bumps the
-- counter AND stamps the new value on the row that changed. The SSE seam
-- tails the counter; a cursored consumer range-scans the column. Triggers,
-- not a call per writer: runs is written from a dozen modules.
--
-- The INSERT trigger only TOUCHES the new row; the UPDATE trigger below does
-- the bump and the stamp for both. SQLite stops a trigger re-entering ITSELF
-- (recursive_triggers is off by default and ``connect`` never turns it on),
-- not from firing a SIBLING — so an insert that bumped here as well would
-- advance the counter twice, once here and once through this touch.
CREATE TRIGGER IF NOT EXISTS bump_board_revision_insert AFTER INSERT ON runs
BEGIN
  UPDATE runs SET revision=revision WHERE id=NEW.id;
END;
CREATE TRIGGER IF NOT EXISTS bump_board_revision_update AFTER UPDATE ON runs
BEGIN
  INSERT INTO meta(key, value) VALUES('board_revision', 1)
  ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1;
  UPDATE runs SET revision=(SELECT CAST(value AS INTEGER) FROM meta
                            WHERE key='board_revision') WHERE id=NEW.id;
END;
CREATE TRIGGER IF NOT EXISTS bump_board_revision_delete AFTER DELETE ON runs
BEGIN
  INSERT INTO meta(key, value) VALUES('board_revision', 1)
  ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1;
END;
CREATE INDEX IF NOT EXISTS idx_runs_parent_run ON runs(parent_run);
-- The cursored read is one range scan on this and nothing else.
CREATE INDEX IF NOT EXISTS idx_runs_revision ON runs(revision);
CREATE INDEX IF NOT EXISTS idx_runs_ref ON runs(ref);
CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project_id);
-- Orchestra's project registry, so an offline CLI still resolves a directory
-- to a project. One row per local path (a source's alias path gets its own
-- row); ``project_id`` is what everything else keys on.
-- ``source_ref`` (schema v24, CONTRACT §7) is the SOURCE'S OWN identifier for
-- the project, opaque here exactly like ``runs.ref``: NULL means the row was
-- adopted locally and no source stands behind it. Only a source's adapter
-- mints one or reads meaning into it; the core compares it and nothing more.
CREATE TABLE IF NOT EXISTS projects (
  path TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  source_ref TEXT,
  name TEXT,
  refreshed_at TEXT NOT NULL,
  archived INTEGER NOT NULL DEFAULT 0,
  archived_override INTEGER,
  slug TEXT
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
-- Schema v17. A worker asks for help by WRITING here; it never launches a
-- process from inside its own sandbox. The parent's supervisor claims each
-- request, enforces the bounds, creates the batch, and starts it. The
-- broker is the enforcement point, never the model's judgment.
CREATE TABLE IF NOT EXISTS spawn_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lead_run INTEGER NOT NULL REFERENCES runs(id),
  requested_by TEXT NOT NULL,
  targets_json TEXT NOT NULL,
  mission TEXT NOT NULL,
  title TEXT,
  context TEXT,
  shared_workdir INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending',
  error TEXT,
  child_run_ids_json TEXT,
  wakeup_run INTEGER,
  wakeup_message INTEGER,
  notified_at TEXT,
  created_at TEXT NOT NULL,
  processed_at TEXT
);
CREATE INDEX IF NOT EXISTS spawn_requests_lead
  ON spawn_requests(lead_run, status);
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
-- with no raw backing (a human injection Orchestra recorded directly).
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
  ref TEXT,
  dedupe_key TEXT,
  title TEXT,
  body TEXT,
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
CREATE INDEX IF NOT EXISTS idx_nod_requests_ref ON nod_requests(ref);
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

RUN_TERMINAL = ("done", "failed", "timeout", "killed", "halted")
TERMINAL_SQL = "(" + ",".join(f"'{s}'" for s in RUN_TERMINAL) + ")"


# The next number for a project, as a scalar subquery: the caller passes the
# project_id one more time where this lands. Written inline so the number is
# taken inside the same write lock that reserves the row — two dispatches
# racing must not both read the same maximum.
NEXT_PROJECT_SEQ = ("(SELECT COALESCE(MAX(project_seq), 0) + 1 FROM runs "
                    " WHERE layer IS NULL AND project_id IS ?)")


def run_no(run) -> str:
    """How a run is named to a human: its project's own count (schema v18).

    Falls back to the row id for a row that has no number — a control turn,
    or a run recorded before the column existed. Never invents one.
    """
    try:
        number = run["project_seq"]
    except (IndexError, KeyError, TypeError):
        number = None
    return f"run {number}" if number else f"run {run['id']}"


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _retire_resurrected(con: sqlite3.Connection) -> None:
    """Fold away an old column name a stale process put back (2026-08-28).

    v21 and v25 rename in ONE SHOT and no reader tolerates the old spelling
    (CONTRACT §6). That holds for readers; it did not survive a WRITER left
    running across the upgrade. A supervisor started before the upgrade keeps
    pre-v21 code in memory, and its own ``connect`` re-adds every column it
    expects. All five came back on the owner's database, and one of them,
    ``work_reported_at``, came back carrying 276 rows, because the missing
    columns also re-triggered the v16 backfill.

    So "both spellings exist" is a REAL state, and crashing on it took the
    daemon down. This stays a migration rather than the read-path shim §6
    forbids: it FOLDS the twin into the live column and DROPS it, so the old
    shape is gone by the time this returns. Folding instead of dropping means
    it never matters which side happened to hold the value.
    """
    names = {r["name"] for r in con.execute("PRAGMA table_info(runs)")}
    if "work_item" in names and "ref" in names:
        con.execute("UPDATE runs SET ref = COALESCE(ref, work_item) "
                    "WHERE work_item IS NOT NULL")
        # The stale writer's SCHEMA rebuilt the old index too, and SQLite
        # refuses to drop a column an index still names.
        con.execute("DROP INDEX IF EXISTS idx_runs_work_item")
        con.execute("ALTER TABLE runs DROP COLUMN work_item")
    if "work_claim_status" in names and "claim_status" in names:
        con.execute("UPDATE runs SET claim_status = "
                    "COALESCE(claim_status, work_claim_status) "
                    "WHERE work_claim_status IS NOT NULL")
        con.execute("ALTER TABLE runs DROP COLUMN work_claim_status")
    for column, mark in (("work_seen_ts", "seen_ts"),
                         ("work_reported_at", "reported_at"),
                         ("work_progress_at", "progress_at")):
        if column not in names:
            continue
        # The adapter's own table is the home. A mark already there WINS: it
        # was written by code that understood the new shape, where the twin
        # may hold a value an old backfill re-derived.
        con.execute(f"INSERT INTO work_marks(run_id, {mark}) "
                    f"SELECT id, {column} FROM runs WHERE {column} IS NOT NULL "
                    f"ON CONFLICT(run_id) DO UPDATE SET "
                    f"{mark} = COALESCE(work_marks.{mark}, excluded.{mark})")
        con.execute(f"ALTER TABLE runs DROP COLUMN {column}")


def _backfill_project_slugs(con: sqlite3.Connection) -> None:
    """Schema v27, one shot: every registered project gets its human address.

    One slug per project id — alias and link rows of one project share it —
    minted from the project's name (else its folder name) and deduplicated
    with a numeric suffix. Never re-run: a slug keys the project's own
    directory under ``~/.orchestra/projects/``, so rewriting one strands it.
    """
    taken = {r["slug"] for r in con.execute(
        "SELECT DISTINCT slug FROM projects WHERE slug IS NOT NULL")}
    rows = con.execute(
        "SELECT project_id, MIN(name) AS name, MIN(path) AS path FROM projects "
        "WHERE slug IS NULL GROUP BY project_id ORDER BY MIN(path)").fetchall()
    for row in rows:
        base = paths.kebab(row["name"] or PurePath(row["path"]).name)
        slug, n = base, 2
        while slug in taken:
            slug, n = f"{base}-{n}", n + 1
        taken.add(slug)
        con.execute("UPDATE projects SET slug=? WHERE project_id=?",
                    (slug, row["project_id"]))


def connect(db_file=None) -> sqlite3.Connection:
    """The central database (DESIGN §2). ``db_file`` opens another file, for
    tests."""
    con = sqlite3.connect(db_file or paths.db_path(), timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=10000")
    existing = {r["name"] for r in con.execute("PRAGMA table_info(runs)")}
    upgrading_v16 = bool(existing) and any(
        name not in existing for name, _ in RUNS_V16_COLUMNS)
    if existing:  # extend a pre-existing table before SCHEMA's indexes run
        con.executescript(WORK_MARKS_SQL)  # both migrations below fill it
        for name, sql_type in (RUNS_V4_COLUMNS
                               + RUNS_V9_COLUMNS + RUNS_V11_COLUMNS
                               + RUNS_V13_COLUMNS + RUNS_V15_COLUMNS
                               + RUNS_V16_COLUMNS + RUNS_V17_COLUMNS
                               + RUNS_V18_COLUMNS + RUNS_V22_COLUMNS
                               + RUNS_V23_COLUMNS + RUNS_V28_COLUMNS):
            if name not in existing:
                con.execute(f"ALTER TABLE runs ADD COLUMN {name} {sql_type}")
        # Schema v21 (CONTRACT §6, §7 Enforcement 1). ONE SHOT, run here and
        # never again: the source-specific column becomes the opaque ``ref``,
        # and the three board timestamps move to the adapter's own table. No
        # reader below tolerates the old shape — that tolerance is the leak
        # §6 forbids — so this branch is the only code that ever names them.
        # It runs BEFORE the v16 backfill: real receipts move first, and the
        # backfill then fills only what is still missing.
        if "work_item" in existing and "ref" not in existing:
            con.execute("ALTER TABLE runs RENAME COLUMN work_item TO ref")
            con.execute("DROP INDEX IF EXISTS idx_runs_work_item")
            con.execute(
                "INSERT OR IGNORE INTO work_marks(run_id, seen_ts, "
                "reported_at, progress_at) SELECT id, work_seen_ts, "
                "work_reported_at, work_progress_at FROM runs "
                "WHERE work_seen_ts IS NOT NULL OR work_reported_at IS NOT NULL "
                "OR work_progress_at IS NOT NULL")
            for column in ("work_seen_ts", "work_reported_at", "work_progress_at"):
                con.execute(f"ALTER TABLE runs DROP COLUMN {column}")
        elif "ref" not in existing:
            con.execute("ALTER TABLE runs ADD COLUMN ref TEXT")
        # Schema v25 (CONTRACT §6, §7 Enforcement 1). ONE SHOT: the admission
        # gate keeps its place on ``runs`` — the reaper reads it — and stops
        # naming the one source that happens to confirm the claim. Same rule
        # as v21: rename OR add, never a reader that accepts both spellings.
        if "work_claim_status" in existing and "claim_status" not in existing:
            con.execute(
                "ALTER TABLE runs RENAME COLUMN work_claim_status TO claim_status")
        elif "claim_status" not in existing:
            con.execute("ALTER TABLE runs ADD COLUMN claim_status TEXT")
        # Both spellings can coexist when a pre-upgrade WRITER re-adds the old
        # one. Fold the twin away before anything below reads either.
        _retire_resurrected(con)
        if upgrading_v16:
            # A historical completion notice proves the old finalizer reached
            # its durable result boundary. Settle only those rows: replaying
            # years of old side effects is unsafe, but an old terminal row with
            # no completion is ambiguous and must not be labelled complete.
            settled = (f"status IN {TERMINAL_SQL} AND EXISTS ("
                       "SELECT 1 FROM messages m WHERE m.run_id=runs.id "
                       "AND m.kind='completion')")
            con.execute(
                "UPDATE runs SET landing_status=COALESCE(landing_status, 'ok'), "
                "handoff_processed_at=COALESCE(handoff_processed_at, "
                f"finished_at, ?) WHERE {settled}", (now(),))
            # The writeback receipt is the Work adapter's since v21.
            con.execute(
                "INSERT INTO work_marks(run_id, reported_at) "
                f"SELECT id, COALESCE(finished_at, ?) FROM runs WHERE {settled} "
                "ON CONFLICT(run_id) DO UPDATE SET reported_at="
                "COALESCE(work_marks.reported_at, excluded.reported_at)",
                (now(),))
        if "project_seq" not in existing:
            # Every run already recorded gets the number it would have had:
            # its project's order, by id. Done once, on the upgrade.
            con.execute(
                "UPDATE runs SET project_seq = (SELECT COUNT(*) FROM runs earlier "
                " WHERE earlier.layer IS NULL AND earlier.id <= runs.id "
                " AND earlier.project_id IS runs.project_id) "
                "WHERE layer IS NULL")
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
    known = {r["name"] for r in con.execute("PRAGMA table_info(projects)")}
    if known:
        for name, sql_type in (PROJECTS_V20_COLUMNS + PROJECTS_V26_COLUMNS
                               + PROJECTS_V27_COLUMNS):
            if name not in known:
                con.execute(f"ALTER TABLE projects ADD COLUMN {name} {sql_type}")
        if "slug" not in known:
            _backfill_project_slugs(con)
        # Schema v24 (CONTRACT §6, §7). ONE SHOT: the column that held a
        # source's own project identifier stops carrying that source's name.
        # No reader below accepts the old spelling — that tolerance is the
        # leak §6 forbids — so this line is the only code that ever names it.
        if "work_id" in known:
            con.execute(
                "ALTER TABLE projects RENAME COLUMN work_id TO source_ref")
    cards = {r["name"] for r in con.execute("PRAGMA table_info(nod_requests)")}
    if cards:
        for name, sql_type in NOD_REQUESTS_V14_COLUMNS + NOD_REQUESTS_V24_COLUMNS:
            if name not in cards:
                con.execute(f"ALTER TABLE nod_requests ADD COLUMN {name} {sql_type}")
        # Schema v25 (CONTRACT §6, §7 Enforcement 1). ONE SHOT: an escalation
        # carries the same OPAQUE ref a run does, so the column stops spelling
        # one source's name. The index is recreated under its new name by
        # SCHEMA below; no reader accepts the old spelling.
        if "work_item" in cards:
            con.execute("DROP INDEX IF EXISTS idx_nod_requests_work")
            con.execute("ALTER TABLE nod_requests RENAME COLUMN work_item TO ref")
        elif "ref" not in cards:
            con.execute("ALTER TABLE nod_requests ADD COLUMN ref TEXT")
    # Recreate so an older database picks up new terminal statuses in WHEN.
    con.execute("DROP TRIGGER IF EXISTS revoke_run_token")
    # Both board-revision bodies changed in v22 (they now stamp the row);
    # CREATE ... IF NOT EXISTS would leave a v19 database on the old pair.
    con.execute("DROP TRIGGER IF EXISTS bump_board_revision_insert")
    con.execute("DROP TRIGGER IF EXISTS bump_board_revision_update")
    con.executescript(SCHEMA)
    if existing and "revision" not in existing:
        # Schema v22, one shot. A run recorded before the column existed
        # carries no marker, so a consumer starting at cursor 0 would never
        # see it. Touch each one and let the trigger stamp it — a table scan
        # is rowid order, so the markers land in the order the runs happened.
        con.execute("UPDATE runs SET revision=revision WHERE revision IS NULL")
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


BOARD_REVISION = "board_revision"


def board_revision(con: sqlite3.Connection) -> int:
    """The dashboard's invalidation counter (DESIGN §3). 0 on a fresh file."""
    row = con.execute("SELECT value FROM meta WHERE key=?",
                      (BOARD_REVISION,)).fetchone()
    try:
        return int(row["value"]) if row else 0
    except (TypeError, ValueError):
        return 0


def bump_board_revision(con: sqlite3.Connection) -> None:
    """Bump it for a board change no ``runs`` trigger sees — pause state,
    runway, daemon health. ``http.record_health`` is the one caller, so the
    board's staleness is capped at one sweeper tick even when nothing runs."""
    con.execute(
        "INSERT INTO meta(key, value) VALUES(?, 1) "
        "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1",
        (BOARD_REVISION,))
