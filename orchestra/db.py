"""Orchestra v2's SQLite schema and small shared database helpers.

This is a clean-break schema. A database without the exact v2 marker is
refused; archived databases are never upgraded in place.
"""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from orchestra import paths

SCHEMA_VERSION = "v2"
GENERAL_GROUP_ID = "general"
GENERAL_GROUP_SLUG = "general"

RUN_ACTIVE = ("queued", "starting", "running", "waiting")
RUN_TERMINAL = ("completed", "failed", "timed_out", "stopped", "skipped")
TERMINAL_SQL = "(" + ",".join(f"'{status}'" for status in RUN_TERMINAL) + ")"


class _Connection(sqlite3.Connection):
    """SQLite connection with one private API transaction mode.

    Domain helpers historically own their small transactions.  An HTTP
    mutation must additionally commit its replay receipt with those writes.
    While ``api_mutation`` is active, helper commits and successful ``with
    con`` exits become logical boundaries; the outer API transaction performs
    the only real commit.  Rollback is intentionally never deferred.
    """

    _api_mutation_active = False
    _api_mutation_broken = False
    _api_context_depth = 0

    @contextmanager
    def api_mutation(self):
        if self._api_mutation_active or \
                sqlite3.Connection.in_transaction.__get__(self):
            raise RuntimeError("API mutation requires a clean transaction")
        sqlite3.Connection.execute(self, "BEGIN IMMEDIATE")
        self._api_mutation_active = True
        self._api_mutation_broken = False
        try:
            yield self
        except BaseException:
            sqlite3.Connection.rollback(self)
            raise
        else:
            if self._api_mutation_broken:
                sqlite3.Connection.rollback(self)
                raise RuntimeError(
                    "API mutation was rolled back before completion")
            sqlite3.Connection.commit(self)
        finally:
            self._api_mutation_active = False
            self._api_mutation_broken = False
            self._api_context_depth = 0

    def commit(self) -> None:
        if not self._api_mutation_active:
            sqlite3.Connection.commit(self)

    def rollback(self) -> None:
        if self._api_mutation_active:
            self._api_mutation_broken = True
        sqlite3.Connection.rollback(self)

    def __enter__(self):
        if not self._api_mutation_active:
            return sqlite3.Connection.__enter__(self)
        self._api_context_depth += 1
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if not self._api_mutation_active:
            return sqlite3.Connection.__exit__(
                self, exc_type, exc_value, traceback)
        try:
            if exc_type is not None:
                self.rollback()
            return False
        finally:
            self._api_context_depth = max(0, self._api_context_depth - 1)


def api_mutation(con: sqlite3.Connection):
    """Return the private transaction context used only by the HTTP service."""
    if not isinstance(con, _Connection):
        raise TypeError("API mutations require a connection from db.connect()")
    return con.api_mutation()


def in_api_mutation(con: sqlite3.Connection) -> bool:
    return isinstance(con, _Connection) and con._api_mutation_active

SCHEMA = """
CREATE TABLE meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE run_groups (
  group_id TEXT PRIMARY KEY,
  slug TEXT NOT NULL COLLATE NOCASE UNIQUE CHECK(length(trim(slug)) > 0),
  name TEXT NOT NULL CHECK(length(trim(name)) > 0),
  default_cwd TEXT,
  archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0, 1)),
  last_run_seq INTEGER NOT NULL DEFAULT 0 CHECK(last_run_seq >= 0),
  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TRIGGER protect_general_group_delete BEFORE DELETE ON run_groups
WHEN OLD.group_id='general'
BEGIN
  SELECT RAISE(ABORT, 'the General group is permanent');
END;

CREATE TRIGGER protect_general_group_identity
BEFORE UPDATE OF slug, name, archived ON run_groups
WHEN OLD.group_id='general' AND
     (NEW.slug IS NOT OLD.slug OR NEW.name IS NOT OLD.name OR NEW.archived <> 0)
BEGIN
  SELECT RAISE(ABORT, 'the General group is permanent');
END;

CREATE TABLE runtimes (
  runtime_id TEXT PRIMARY KEY,
  slug TEXT NOT NULL COLLATE NOCASE UNIQUE CHECK(length(trim(slug)) > 0),
  name TEXT NOT NULL CHECK(length(trim(name)) > 0),
  adapter TEXT NOT NULL CHECK(length(trim(adapter)) > 0),
  command_json TEXT NOT NULL DEFAULT '[]',
  capabilities_json TEXT NOT NULL DEFAULT '{}',
  config_json TEXT NOT NULL DEFAULT '{}',
  enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
  archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0, 1)),
  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE runway_sources (
  source_id TEXT PRIMARY KEY,
  slug TEXT NOT NULL COLLATE NOCASE UNIQUE CHECK(length(trim(slug)) > 0),
  name TEXT NOT NULL CHECK(length(trim(name)) > 0),
  provider TEXT NOT NULL CHECK(length(trim(provider)) > 0),
  account TEXT NOT NULL DEFAULT '',
  lane TEXT NOT NULL DEFAULT '',
  adapter TEXT NOT NULL CHECK(length(trim(adapter)) > 0),
  command_json TEXT NOT NULL DEFAULT '[]',
  config_json TEXT NOT NULL DEFAULT '{}',
  enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
  archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0, 1)),
  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(provider, account, lane)
);

CREATE TABLE profiles (
  profile_id TEXT PRIMARY KEY,
  slug TEXT NOT NULL COLLATE NOCASE UNIQUE CHECK(length(trim(slug)) > 0),
  name TEXT NOT NULL CHECK(length(trim(name)) > 0),
  runtime_id TEXT NOT NULL REFERENCES runtimes(runtime_id),
  model TEXT,
  effort TEXT,
  tier INTEGER NOT NULL CHECK(tier BETWEEN 1 AND 3),
  priority INTEGER NOT NULL DEFAULT 0,
  sandbox TEXT,
  timeout_seconds INTEGER CHECK(timeout_seconds IS NULL OR timeout_seconds > 0),
  max_concurrency INTEGER CHECK(max_concurrency IS NULL OR max_concurrency > 0),
  runway_source_id TEXT REFERENCES runway_sources(source_id),
  env_json TEXT NOT NULL DEFAULT '{}',
  config_json TEXT NOT NULL DEFAULT '{}',
  note TEXT,
  enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
  archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0, 1)),
  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX idx_profiles_runtime ON profiles(runtime_id);
CREATE INDEX idx_profiles_runway_source ON profiles(runway_source_id);

CREATE TABLE fleet_settings (
  key TEXT PRIMARY KEY CHECK(length(trim(key)) > 0),
  value_json TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
  updated_by TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE observer_settings (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0, 1)),
  profile_id TEXT REFERENCES profiles(profile_id),
  max_concurrency INTEGER NOT NULL DEFAULT 1
    CHECK(max_concurrency BETWEEN 1 AND 8),
  first_look_seconds INTEGER NOT NULL DEFAULT 300 CHECK(first_look_seconds > 0),
  minimum_events INTEGER NOT NULL DEFAULT 5 CHECK(minimum_events > 0),
  interval_seconds INTEGER NOT NULL DEFAULT 1800 CHECK(interval_seconds > 0),
  authority TEXT NOT NULL DEFAULT 'correct_then_stop'
    CHECK(authority IN ('advisory', 'tell_only', 'correct_then_stop')),
  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
  updated_by TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  CHECK(enabled=0 OR profile_id IS NOT NULL)
);

CREATE TABLE devices (
  device_id TEXT PRIMARY KEY,
  name TEXT NOT NULL CHECK(length(trim(name)) > 0),
  token_hash TEXT NOT NULL UNIQUE CHECK(length(token_hash) >= 32),
  created_at TEXT NOT NULL,
  last_seen_at TEXT,
  revoked_at TEXT
);

CREATE TABLE service_tokens (
  token_id TEXT PRIMARY KEY,
  name TEXT NOT NULL CHECK(length(trim(name)) > 0),
  token_hash TEXT NOT NULL UNIQUE CHECK(length(token_hash) >= 32),
  authorities_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  last_seen_at TEXT,
  revoked_at TEXT
);

CREATE TABLE pairing_codes (
  pairing_id TEXT PRIMARY KEY,
  code_hash TEXT NOT NULL UNIQUE CHECK(length(code_hash) >= 32),
  created_by_device_id TEXT REFERENCES devices(device_id),
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used_at TEXT
);
CREATE INDEX idx_pairing_codes_expiry ON pairing_codes(expires_at, used_at);

CREATE TABLE runs (
  id INTEGER PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE DEFAULT (lower(hex(randomblob(8)))),
  request_id TEXT NOT NULL UNIQUE,
  group_id TEXT NOT NULL DEFAULT 'general' REFERENCES run_groups(group_id),
  group_seq INTEGER,
  profile_id TEXT NOT NULL REFERENCES profiles(profile_id),
  runtime_id TEXT NOT NULL REFERENCES runtimes(runtime_id),
  runway_source_id TEXT REFERENCES runway_sources(source_id),
  root_run_id INTEGER REFERENCES runs(id),
  parent_run_id INTEGER REFERENCES runs(id),
  retry_of_run_id INTEGER REFERENCES runs(id),
  continuation_of_run_id INTEGER REFERENCES runs(id),
  attempt INTEGER NOT NULL DEFAULT 1 CHECK(attempt > 0),
  title TEXT,
  mission TEXT NOT NULL CHECK(length(trim(mission)) > 0),
  context TEXT,
  requested_by TEXT NOT NULL,
  ref TEXT,
  status TEXT NOT NULL DEFAULT 'queued'
    CHECK(status IN ('queued','starting','running','waiting','completed',
                     'failed','timed_out','stopped','skipped')),
  hold_reason TEXT,
  not_before TEXT,
  waiting_kind TEXT CHECK(waiting_kind IS NULL OR waiting_kind IN ('input','children')),
  summary TEXT,
  exit_code INTEGER,
  queued_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  cwd TEXT NOT NULL,
  cwd_source TEXT NOT NULL
    CHECK(cwd_source IN ('run','group','managed','inherited')),
  workdir TEXT NOT NULL,
  isolation TEXT NOT NULL DEFAULT 'auto'
    CHECK(isolation IN ('auto','worktree','shared')),
  repo TEXT,
  branch TEXT,
  base_commit TEXT,
  head_commit TEXT,
  checkpoint_commit TEXT,
  diff_path TEXT,
  brief_path TEXT,
  log_path TEXT,
  pid INTEGER,
  pid_identity TEXT,
  supervisor_pid INTEGER,
  supervisor_pid_identity TEXT,
  session_ref TEXT,
  worker_status TEXT,
  worker_exit_code INTEGER,
  tokens_in INTEGER CHECK(tokens_in IS NULL OR tokens_in >= 0),
  tokens_out INTEGER CHECK(tokens_out IS NULL OR tokens_out >= 0),
  tokens_total INTEGER CHECK(tokens_total IS NULL OR tokens_total >= 0),
  tokens_cache_read INTEGER CHECK(tokens_cache_read IS NULL OR tokens_cache_read >= 0),
  tokens_cache_write INTEGER CHECK(tokens_cache_write IS NULL OR tokens_cache_write >= 0),
  cost_usd REAL CHECK(cost_usd IS NULL OR cost_usd >= 0),
  usage_source TEXT,
  run_token_hash TEXT UNIQUE,
  profile_snapshot TEXT NOT NULL,
  runtime_snapshot TEXT NOT NULL,
  request_snapshot TEXT NOT NULL,
  revision INTEGER
);
CREATE UNIQUE INDEX idx_runs_group_sequence ON runs(group_id, group_seq);
CREATE INDEX idx_runs_status ON runs(status, id);
CREATE INDEX idx_runs_scheduled ON runs(status, not_before, id);
CREATE INDEX idx_runs_revision ON runs(revision, id);
CREATE INDEX idx_runs_profile ON runs(profile_id, id);
CREATE INDEX idx_runs_parent ON runs(parent_run_id, id);
CREATE INDEX idx_runs_root ON runs(root_run_id, id);
CREATE INDEX idx_runs_ref ON runs(ref, id);

CREATE TRIGGER reject_explicit_group_sequence BEFORE INSERT ON runs
WHEN NEW.group_seq IS NOT NULL
BEGIN
  SELECT RAISE(ABORT, 'group sequence is allocated by Orchestra');
END;

CREATE TRIGGER reject_archived_group_root BEFORE INSERT ON runs
WHEN NEW.parent_run_id IS NULL AND NEW.retry_of_run_id IS NULL
     AND NEW.continuation_of_run_id IS NULL
     AND (SELECT archived FROM run_groups WHERE group_id=NEW.group_id)=1
BEGIN
  SELECT RAISE(ABORT, 'archived group does not accept root runs');
END;

CREATE TRIGGER initialize_run AFTER INSERT ON runs
BEGIN
  UPDATE run_groups SET last_run_seq=last_run_seq+1 WHERE group_id=NEW.group_id;
  INSERT INTO meta(key, value) VALUES('board_revision', 1)
  ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1;
  UPDATE runs SET
    group_seq=(SELECT last_run_seq FROM run_groups WHERE group_id=NEW.group_id),
    root_run_id=COALESCE(
      NEW.root_run_id,
      (SELECT root_run_id FROM runs WHERE id=COALESCE(
        NEW.parent_run_id, NEW.retry_of_run_id, NEW.continuation_of_run_id)),
      NEW.id
    ),
    revision=(SELECT CAST(value AS INTEGER) FROM meta WHERE key='board_revision')
  WHERE id=NEW.id;
END;

CREATE TRIGGER bump_board_revision_update AFTER UPDATE ON runs
WHEN NEW.revision IS OLD.revision
BEGIN
  INSERT INTO meta(key, value) VALUES('board_revision', 1)
  ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1;
  UPDATE runs SET revision=(SELECT CAST(value AS INTEGER) FROM meta
                            WHERE key='board_revision') WHERE id=NEW.id;
END;

CREATE TRIGGER bump_board_revision_delete AFTER DELETE ON runs
BEGIN
  INSERT INTO meta(key, value) VALUES('board_revision', 1)
  ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1;
END;

CREATE TRIGGER revoke_run_token AFTER UPDATE OF status ON runs
WHEN NEW.status IN ('completed','failed','timed_out','stopped','skipped')
     AND NEW.run_token_hash IS NOT NULL
BEGIN
  UPDATE runs SET run_token_hash=NULL WHERE id=NEW.id;
END;

CREATE TABLE run_dependencies (
  run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  depends_on_run_id INTEGER NOT NULL REFERENCES runs(id),
  condition TEXT NOT NULL DEFAULT 'success'
    CHECK(condition IN ('success', 'terminal')),
  PRIMARY KEY(run_id, depends_on_run_id),
  CHECK(run_id <> depends_on_run_id)
);
CREATE INDEX idx_run_dependencies_prerequisite
  ON run_dependencies(depends_on_run_id, run_id);

CREATE TABLE child_requests (
  id INTEGER PRIMARY KEY,
  request_id TEXT NOT NULL UNIQUE,
  parent_run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  requested_by TEXT NOT NULL,
  targets_json TEXT NOT NULL,
  mission TEXT NOT NULL,
  title TEXT,
  context TEXT,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK(status IN ('pending','processing','settled','failed','cancelled')),
  child_run_ids_json TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  processed_at TEXT
);
CREATE INDEX idx_child_requests_parent ON child_requests(parent_run_id, status, id);

CREATE TABLE messages (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  direction TEXT NOT NULL CHECK(direction IN ('inbound', 'outbound', 'system')),
  sender TEXT NOT NULL,
  body TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'message',
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK(status IN ('pending','delivered','undeliverable')),
  correlation_id TEXT,
  reply_to INTEGER REFERENCES messages(id),
  created_at TEXT NOT NULL,
  delivery_offset INTEGER,
  delivered_at TEXT,
  undeliverable_at TEXT,
  undeliverable_reason TEXT
);
CREATE INDEX idx_messages_run ON messages(run_id, id);
CREATE INDEX idx_messages_delivery ON messages(status, id);
CREATE UNIQUE INDEX idx_messages_correlation
  ON messages(run_id, kind, correlation_id) WHERE correlation_id IS NOT NULL;

CREATE TABLE attention_requests (
  id INTEGER PRIMARY KEY,
  run_id INTEGER REFERENCES runs(id) ON DELETE CASCADE,
  kind TEXT NOT NULL
    CHECK(kind IN ('question','decision','alert','profile_proposal')),
  status TEXT NOT NULL DEFAULT 'open'
    CHECK(status IN ('open','resolved','cancelled')),
  blocking INTEGER NOT NULL DEFAULT 0 CHECK(blocking IN (0, 1)),
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  choices_json TEXT,
  fallback_json TEXT,
  proposal_json TEXT,
  correlation_id TEXT NOT NULL UNIQUE,
  deadline TEXT,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  resolved_at TEXT,
  resolution_json TEXT,
  resolved_by TEXT,
  revision INTEGER
);
CREATE INDEX idx_attention_open ON attention_requests(status, created_at, id);
CREATE INDEX idx_attention_run ON attention_requests(run_id, id);
CREATE INDEX idx_attention_revision ON attention_requests(revision, id);

CREATE TRIGGER initialize_attention AFTER INSERT ON attention_requests
BEGIN
  INSERT INTO meta(key, value) VALUES('board_revision', 1)
  ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1;
  UPDATE attention_requests SET revision=(
    SELECT CAST(value AS INTEGER) FROM meta WHERE key='board_revision'
  ) WHERE id=NEW.id;
END;

CREATE TRIGGER bump_attention_revision AFTER UPDATE ON attention_requests
WHEN NEW.revision IS OLD.revision
BEGIN
  INSERT INTO meta(key, value) VALUES('board_revision', 1)
  ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1;
  UPDATE attention_requests SET revision=(
    SELECT CAST(value AS INTEGER) FROM meta WHERE key='board_revision'
  ) WHERE id=NEW.id;
END;

CREATE TABLE attention_responses (
  id INTEGER PRIMARY KEY,
  attention_id INTEGER NOT NULL REFERENCES attention_requests(id) ON DELETE CASCADE,
  actor TEXT NOT NULL,
  response_json TEXT NOT NULL,
  accepted INTEGER NOT NULL DEFAULT 0 CHECK(accepted IN (0, 1)),
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_attention_one_accepted
  ON attention_responses(attention_id) WHERE accepted=1;
CREATE INDEX idx_attention_responses_request ON attention_responses(attention_id, id);

CREATE TABLE events (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  kind TEXT NOT NULL,
  name TEXT,
  payload TEXT NOT NULL DEFAULT '',
  payload_len INTEGER NOT NULL DEFAULT 0,
  truncated INTEGER NOT NULL DEFAULT 0 CHECK(truncated IN (0, 1)),
  byte_offset INTEGER NOT NULL DEFAULT -1,
  byte_length INTEGER NOT NULL DEFAULT 0,
  ts TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(run_id, seq)
);
CREATE INDEX idx_events_run ON events(run_id, seq);

CREATE TABLE trace_cursors (
  run_id INTEGER PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
  byte_offset INTEGER NOT NULL DEFAULT 0,
  seq INTEGER NOT NULL DEFAULT 0,
  skipped INTEGER NOT NULL DEFAULT 0,
  raw_pruned_at TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE supervision_events (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  detector TEXT NOT NULL,
  action TEXT NOT NULL,
  reason TEXT NOT NULL,
  detail_json TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_supervision_events_run ON supervision_events(run_id, id);

CREATE TABLE observer_checks (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  profile_id TEXT REFERENCES profiles(profile_id),
  profile_snapshot TEXT NOT NULL,
  runtime_snapshot TEXT NOT NULL,
  input_json TEXT NOT NULL,
  trigger TEXT NOT NULL,
  authority TEXT NOT NULL DEFAULT 'correct_then_stop'
    CHECK(authority IN ('advisory', 'tell_only', 'correct_then_stop')),
  verdict TEXT,
  action TEXT,
  reason TEXT,
  detail_json TEXT,
  delivery_status TEXT NOT NULL DEFAULT 'not_required'
    CHECK(delivery_status IN ('not_required','pending','delivered','skipped')),
  delivery_error TEXT,
  control_audit_id INTEGER REFERENCES control_events(id),
  event_seq_start INTEGER,
  event_seq_end INTEGER,
  event_count INTEGER NOT NULL DEFAULT 0 CHECK(event_count >= 0),
  tokens_in INTEGER CHECK(tokens_in IS NULL OR tokens_in >= 0),
  tokens_out INTEGER CHECK(tokens_out IS NULL OR tokens_out >= 0),
  tokens_total INTEGER CHECK(tokens_total IS NULL OR tokens_total >= 0),
  cost_usd REAL CHECK(cost_usd IS NULL OR cost_usd >= 0),
  supervisor_pid INTEGER CHECK(supervisor_pid IS NULL OR supervisor_pid > 0),
  supervisor_pid_identity TEXT,
  worker_pid INTEGER CHECK(worker_pid IS NULL OR worker_pid > 0),
  worker_pid_identity TEXT,
  log_path TEXT,
  log_pruned_at TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  error TEXT
);
CREATE INDEX idx_observer_checks_run ON observer_checks(run_id, id);
CREATE INDEX idx_observer_checks_profile ON observer_checks(profile_id, id);
CREATE UNIQUE INDEX idx_observer_active_run
  ON observer_checks(run_id) WHERE finished_at IS NULL;

CREATE TABLE runway_readings (
  id INTEGER PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES runway_sources(source_id),
  remaining REAL,
  limit_value REAL,
  unit TEXT,
  resets_at TEXT,
  as_of TEXT,
  fresh_until TEXT,
  definitive INTEGER NOT NULL DEFAULT 0 CHECK(definitive IN (0, 1)),
  reason TEXT,
  windows_json TEXT,
  raw_json TEXT,
  polled_at TEXT NOT NULL
);
CREATE INDEX idx_runway_readings_source ON runway_readings(source_id, polled_at, id);

CREATE TABLE artifacts (
  artifact_id TEXT PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  relative_path TEXT NOT NULL,
  stored_path TEXT NOT NULL,
  source_path TEXT,
  mime_type TEXT,
  size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
  sha256 TEXT NOT NULL CHECK(length(sha256)=64),
  created_at TEXT NOT NULL,
  pruned_at TEXT,
  UNIQUE(run_id, relative_path)
);
CREATE INDEX idx_artifacts_run ON artifacts(run_id, created_at, artifact_id);

CREATE TABLE evidence_pins (
  run_id INTEGER PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
  reason TEXT,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE prune_plans (
  plan_id TEXT PRIMARY KEY,
  criteria_json TEXT NOT NULL,
  items_json TEXT NOT NULL,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  applied_by TEXT,
  applied_at TEXT,
  result_json TEXT
);
CREATE INDEX idx_prune_plans_created ON prune_plans(created_at, plan_id);

CREATE TABLE control_events (
  id INTEGER PRIMARY KEY,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  target_type TEXT,
  target_id TEXT,
  request_id TEXT,
  detail TEXT,
  outcome TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_control_events_created ON control_events(created_at, id);
CREATE INDEX idx_control_events_target ON control_events(target_type, target_id, id);

CREATE TABLE request_replays (
  request_id TEXT PRIMARY KEY,
  method TEXT NOT NULL,
  path TEXT NOT NULL,
  body_hash TEXT NOT NULL,
  response_json TEXT,
  created_at TEXT NOT NULL,
  finished_at TEXT
);
"""


DEFAULT_FLEET_SETTINGS = {
    "instance_name": "Orchestra",
    "max_active_runs": 8,
    "paused": False,
    "delegation_max_depth": 2,
    "delegation_max_children": 3,
    "delegation_max_active_children": 3,
}


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_no(run) -> str:
    """Return the group-local human run number, with the row id as fallback."""
    try:
        number = run["group_seq"]
    except (IndexError, KeyError, TypeError):
        number = None
    if number:
        try:
            name = run["group_name"]
        except (IndexError, KeyError, TypeError):
            name = None
        return f"{name} #{number}" if name else f"run {number}"
    return f"run {run['id']}"


def _has_tables(con: sqlite3.Connection) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' LIMIT 1"
    ).fetchone() is not None


def _schema_version(con: sqlite3.Connection) -> str | None:
    if not con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone():
        return None
    row = con.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()
    return str(row[0]) if row else None


def _execute_schema(con: sqlite3.Connection) -> None:
    statement = ""
    for line in SCHEMA.splitlines():
        statement += line + "\n"
        if sqlite3.complete_statement(statement):
            con.execute(statement)
            statement = ""
    if statement.strip():
        raise RuntimeError("orchestra: incomplete v2 schema statement")


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in con.execute(f"PRAGMA table_info({table})")}


def _ensure_run_identity_triggers(con: sqlite3.Connection) -> None:
    """Install run invariants after both clean creation and live-v2 migration."""
    con.execute("""
        CREATE TRIGGER IF NOT EXISTS validate_run_lineage BEFORE INSERT ON runs
        WHEN
          ((NEW.parent_run_id IS NOT NULL) + (NEW.retry_of_run_id IS NOT NULL) +
           (NEW.continuation_of_run_id IS NOT NULL)) > 1
          OR (
            NEW.parent_run_id IS NULL AND NEW.retry_of_run_id IS NULL AND
            NEW.continuation_of_run_id IS NULL AND NEW.root_run_id IS NOT NULL
          )
          OR (
            COALESCE(NEW.parent_run_id, NEW.retry_of_run_id,
                     NEW.continuation_of_run_id) IS NOT NULL AND (
              NEW.group_id IS NOT (SELECT group_id FROM runs WHERE id=COALESCE(
                NEW.parent_run_id, NEW.retry_of_run_id,
                NEW.continuation_of_run_id)) OR
              NEW.cwd IS NOT (SELECT cwd FROM runs WHERE id=COALESCE(
                NEW.parent_run_id, NEW.retry_of_run_id,
                NEW.continuation_of_run_id)) OR
              (NEW.root_run_id IS NOT NULL AND NEW.root_run_id IS NOT
                (SELECT root_run_id FROM runs WHERE id=COALESCE(
                  NEW.parent_run_id, NEW.retry_of_run_id,
                  NEW.continuation_of_run_id)))
            )
          )
        BEGIN
          SELECT RAISE(ABORT, 'invalid run lineage');
        END
    """)
    con.execute("""
        CREATE TRIGGER IF NOT EXISTS immutable_run_identity
        BEFORE UPDATE OF request_id, group_id, group_seq, profile_id,
          runtime_id, runway_source_id, root_run_id, parent_run_id,
          retry_of_run_id, continuation_of_run_id, attempt, mission, context,
          requested_by, cwd, cwd_source, isolation, profile_snapshot,
          runtime_snapshot, request_snapshot ON runs
        WHEN OLD.group_seq IS NOT NULL AND (
          NEW.request_id IS NOT OLD.request_id OR
          NEW.group_id IS NOT OLD.group_id OR
          NEW.group_seq IS NOT OLD.group_seq OR
          NEW.profile_id IS NOT OLD.profile_id OR
          NEW.runtime_id IS NOT OLD.runtime_id OR
          NEW.runway_source_id IS NOT OLD.runway_source_id OR
          NEW.root_run_id IS NOT OLD.root_run_id OR
          NEW.parent_run_id IS NOT OLD.parent_run_id OR
          NEW.retry_of_run_id IS NOT OLD.retry_of_run_id OR
          NEW.continuation_of_run_id IS NOT OLD.continuation_of_run_id OR
          NEW.attempt IS NOT OLD.attempt OR NEW.mission IS NOT OLD.mission OR
          NEW.context IS NOT OLD.context OR
          NEW.requested_by IS NOT OLD.requested_by OR
          NEW.cwd IS NOT OLD.cwd OR NEW.cwd_source IS NOT OLD.cwd_source OR
          NEW.isolation IS NOT OLD.isolation OR
          NEW.profile_snapshot IS NOT OLD.profile_snapshot OR
          NEW.runtime_snapshot IS NOT OLD.runtime_snapshot OR
          NEW.request_snapshot IS NOT OLD.request_snapshot
        )
        BEGIN
          SELECT RAISE(ABORT, 'run identity and snapshots are immutable');
        END
    """)


def _migrate_scope_model(con: sqlite3.Connection) -> None:
    """Collapse live v2 scopes into group defaults without retaining an alias.

    Historical runs already froze their owner directory in ``repo/workdir``.
    A group inherits a default only when all its historical scope bindings
    agree; ambiguous groups stay unbound and require an explicit future CWD.
    """
    group_columns = _columns(con, "run_groups")
    if "default_cwd" not in group_columns:
        con.execute("ALTER TABLE run_groups ADD COLUMN default_cwd TEXT")

    run_columns = _columns(con, "runs")
    legacy_scope = "scope_id" in run_columns
    needs_backfill = legacy_scope or "cwd" not in run_columns or \
        "cwd_source" not in run_columns
    if "cwd" not in run_columns:
        con.execute("ALTER TABLE runs ADD COLUMN cwd TEXT")
    if "cwd_source" not in run_columns:
        con.execute(
            "ALTER TABLE runs ADD COLUMN cwd_source TEXT NOT NULL "
            "DEFAULT 'managed' CHECK(cwd_source IN "
            "('run','group','managed','inherited'))")

    has_scopes = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scopes'"
    ).fetchone() is not None
    if legacy_scope and has_scopes:
        con.execute("""
            WITH agreed AS (
              SELECT r.group_id,MIN(s.root) AS root
              FROM runs r JOIN scopes s ON s.scope_id=r.scope_id
              GROUP BY r.group_id HAVING COUNT(DISTINCT s.root)=1
            )
            UPDATE run_groups SET default_cwd=(
              SELECT agreed.root FROM agreed
              WHERE agreed.group_id=run_groups.group_id
            )
            WHERE default_cwd IS NULL AND group_id IN (
              SELECT group_id FROM agreed
            )
        """)

    if needs_backfill:
        con.execute(
            "UPDATE runs SET cwd=COALESCE(NULLIF(repo,''),NULLIF(workdir,'')) "
            "WHERE cwd IS NULL OR trim(cwd)=''"
        )
        missing = con.execute(
            "SELECT id FROM runs WHERE cwd IS NULL OR trim(cwd)='' LIMIT 1"
        ).fetchone()
        if missing is not None:
            raise RuntimeError(
                f"orchestra: cannot migrate run {missing['id']} without a frozen CWD")
        con.execute("""
            UPDATE runs SET cwd_source=CASE
              WHEN cwd=(SELECT default_cwd FROM run_groups
                        WHERE group_id=runs.group_id) THEN 'group'
              ELSE 'run' END
        """)

    if legacy_scope:
        con.execute("DROP TRIGGER IF EXISTS validate_run_lineage")
        con.execute("DROP TRIGGER IF EXISTS immutable_run_identity")
        con.execute("DROP INDEX IF EXISTS idx_runs_scope")
        con.execute("ALTER TABLE runs DROP COLUMN scope_id")
    con.execute("DROP TABLE IF EXISTS scope_profiles")
    con.execute("DROP TABLE IF EXISTS scopes")
    _ensure_run_identity_triggers(con)


def _migrate_usage_breakdown(con: sqlite3.Connection) -> None:
    """Add the cache-token split to databases created before it existed."""
    columns = _columns(con, "runs")
    for name in ("tokens_cache_read", "tokens_cache_write"):
        if name not in columns:
            con.execute(
                f"ALTER TABLE runs ADD COLUMN {name} INTEGER "
                f"CHECK({name} IS NULL OR {name} >= 0)")


def _ensure_message_revision_triggers(con: sqlite3.Connection) -> None:
    """Install additive v2 triggers on both new and already-created v2 stores."""
    con.execute("""
        CREATE TRIGGER IF NOT EXISTS bump_board_revision_message_insert
        AFTER INSERT ON messages
        BEGIN
          INSERT INTO meta(key, value) VALUES('board_revision', 1)
          ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1;
        END
    """)
    con.execute("""
        CREATE TRIGGER IF NOT EXISTS bump_board_revision_message_delivery
        AFTER UPDATE OF status, delivered_at, undeliverable_at,
          undeliverable_reason ON messages
        WHEN NEW.status IS NOT OLD.status OR
             NEW.delivered_at IS NOT OLD.delivered_at OR
             NEW.undeliverable_at IS NOT OLD.undeliverable_at OR
             NEW.undeliverable_reason IS NOT OLD.undeliverable_reason
        BEGIN
          INSERT INTO meta(key, value) VALUES('board_revision', 1)
          ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1;
        END
    """)


def _ensure_v2_defaults(con: sqlite3.Connection) -> None:
    con.execute(
        "INSERT OR IGNORE INTO fleet_settings("
        "key,value_json,updated_by,updated_at) VALUES('instance_name',?,'system',?)",
        (json.dumps("Orchestra"), now()),
    )


def _clear_subscription_costs(con: sqlite3.Connection) -> None:
    """A provider-reported token-plan estimate is not metered USD spend."""
    def plan(profile_raw, runtime_raw) -> bool:
        try:
            profile = json.loads(profile_raw or "{}")
            runtime = json.loads(runtime_raw or "{}")
        except (TypeError, ValueError):
            return False
        adapter = str(runtime.get("adapter") or runtime.get("kind") or "").lower()
        if adapter in {"claude", "codex"}:
            return True
        provider = str(profile.get("model") or "").split("/", 1)[0].lower()
        return provider.startswith(("xai", "grok", "kimi", "moonshot", "minimax"))

    for table in ("runs", "observer_checks"):
        if not _columns(con, table) or "cost_usd" not in _columns(con, table):
            continue
        rows = con.execute(
            f"SELECT id,profile_snapshot,runtime_snapshot FROM {table} "
            "WHERE cost_usd IS NOT NULL"
        ).fetchall()
        ids = [(int(row["id"]),) for row in rows
               if plan(row["profile_snapshot"], row["runtime_snapshot"])]
        if ids:
            con.executemany(f"UPDATE {table} SET cost_usd=NULL WHERE id=?", ids)


def _initialize(con: sqlite3.Connection) -> None:
    timestamp = now()
    _execute_schema(con)
    con.executemany(
        "INSERT INTO meta(key, value) VALUES(?,?)",
        (("schema_version", SCHEMA_VERSION),
         ("instance_id", str(uuid.uuid4())),
         ("board_revision", "0")),
    )
    con.execute(
        "INSERT INTO run_groups(group_id, slug, name, created_at, updated_at) "
        "VALUES(?,?,?,?,?)",
        (GENERAL_GROUP_ID, GENERAL_GROUP_SLUG, "General", timestamp, timestamp),
    )
    con.executemany(
        "INSERT INTO fleet_settings(key, value_json, updated_by, updated_at) "
        "VALUES(?,?,?,?)",
        ((key, json.dumps(value), "system", timestamp)
         for key, value in DEFAULT_FLEET_SETTINGS.items()),
    )
    con.execute(
        "INSERT INTO observer_settings(singleton, updated_by, updated_at) "
        "VALUES(1,'system',?)",
        (timestamp,),
    )


def connect(db_file=None) -> sqlite3.Connection:
    """Open the authoritative v2 database, refusing every other schema."""
    target = Path(db_file) if db_file is not None else paths.db_path()
    if str(target) != ":memory:":
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    con = sqlite3.connect(str(target), timeout=15, factory=_Connection)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=10000")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        con.execute("BEGIN EXCLUSIVE")
        if _has_tables(con):
            version = _schema_version(con)
            if version != SCHEMA_VERSION:
                raise RuntimeError(
                    f"orchestra: {target} is not a {SCHEMA_VERSION} database; "
                    "leave it archived and initialize the v2 state directory"
                )
        else:
            _initialize(con)
        _migrate_scope_model(con)
        _migrate_usage_breakdown(con)
        _ensure_message_revision_triggers(con)
        _ensure_v2_defaults(con)
        _clear_subscription_costs(con)
        con.commit()
        return con
    except BaseException:
        con.rollback()
        con.close()
        raise


def meta_get(con: sqlite3.Connection, key: str) -> str | None:
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def meta_set(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO meta(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def instance_id(con: sqlite3.Connection) -> str:
    value = meta_get(con, "instance_id")
    if not value:
        raise RuntimeError("orchestra: v2 database has no instance identity")
    return value


BOARD_REVISION = "board_revision"


def board_revision(con: sqlite3.Connection) -> int:
    value = meta_get(con, BOARD_REVISION)
    try:
        return int(value or 0)
    except ValueError:
        return 0


def bump_board_revision(con: sqlite3.Connection) -> None:
    con.execute(
        "INSERT INTO meta(key, value) VALUES(?, 1) "
        "ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1",
        (BOARD_REVISION,),
    )


def record_control(
    con: sqlite3.Connection,
    *,
    actor: str,
    action: str,
    outcome: str,
    target_type: str | None = None,
    target_id: str | int | None = None,
    request_id: str | None = None,
    detail=None,
) -> int:
    """Append one bounded operator/configuration audit event."""
    encoded = None if detail is None else json.dumps(
        detail, ensure_ascii=False, default=str
    )[:4000]
    cur = con.execute(
        "INSERT INTO control_events(actor, action, target_type, target_id, "
        "request_id, detail, outcome, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (actor, action, target_type,
         None if target_id is None else str(target_id), request_id, encoded,
         outcome, now()),
    )
    return int(cur.lastrowid)
