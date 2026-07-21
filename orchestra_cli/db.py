import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orchestra_cli import paths

# Slug is a memorable, Docker-style name (e.g. ``silly_panda``). It is purely
# a display + UNIQUE-key aid; the numeric ``runs.id`` stays authoritative.
# The column is declared UNIQUE here so concurrent inserts can't double-mint
# the same slug at the schema layer (defence in depth alongside the
# in-Python collision retry in orchestra_cli.names).
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS teams (
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  about TEXT DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS members (
  team_id INTEGER NOT NULL REFERENCES teams(id),
  agent TEXT NOT NULL,
  UNIQUE(team_id, agent)
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,
  sender TEXT NOT NULL,
  recipient TEXT NOT NULL,
  body TEXT NOT NULL,
  work_item TEXT,
  run_id INTEGER,
  kind TEXT DEFAULT '',
  created_at TEXT NOT NULL,
  read_at TEXT,
  delivery_offset INTEGER,
  delivered_at TEXT,
  recalled_at TEXT,
  recalled_by TEXT
);
CREATE TABLE IF NOT EXISTS questions (
  id INTEGER PRIMARY KEY,
  run_id INTEGER UNIQUE NOT NULL REFERENCES runs(id),
  sender TEXT NOT NULL,
  recipient TEXT NOT NULL,
  question TEXT NOT NULL,
  recommended_default TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'waiting',
  asked_at TEXT NOT NULL,
  deadline_at TEXT NOT NULL,
  answered_at TEXT,
  answered_by TEXT,
  answer TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  agent TEXT NOT NULL,
  backend TEXT NOT NULL,
  model TEXT,
  title TEXT,
  work_item TEXT,
  team TEXT,
  requested_by TEXT NOT NULL,
  brief_path TEXT,
  log_path TEXT,
  workdir TEXT NOT NULL,
  branch TEXT,
  parent_run INTEGER,
  lead_run INTEGER,
  child_depth INTEGER NOT NULL DEFAULT 0,
  child_wakeup_run INTEGER,
  allow_question INTEGER NOT NULL DEFAULT 0,
  question_wait_seconds INTEGER NOT NULL DEFAULT 1800,
  supervisor_protocol INTEGER NOT NULL DEFAULT 0,
  pid INTEGER,
  session_ref TEXT,
  status TEXT NOT NULL DEFAULT 'spawning',
  exit_code INTEGER,
  summary TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS feed (
  id INTEGER PRIMARY KEY,
  author TEXT NOT NULL,
  body TEXT NOT NULL,
  tags TEXT DEFAULT '',
  work_item TEXT,
  run_id INTEGER,
  created_at TEXT NOT NULL
);
"""

RUN_TERMINAL = ("done", "failed", "timeout", "killed")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def after(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _has_column(con: sqlite3.Connection, table: str, column: str) -> bool:
    rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


def _apply_migrations(con: sqlite3.Connection) -> None:
    """Idempotent, forwards-only schema migrations.

    Every branch MUST be guarded by ``_has_column`` (or its specific check)
    so reopening an already-migrated DB is a no-op. Old on-disk DBs created
    before W-0007 remain readable: the ``slug`` column is nullable and
    back-filling it is the user's choice, not an automatic operation (it
    would need to rewrite every row's display surface).
    """
    # Pre-W-0007: messages.kind was added later.
    try:
        con.execute("ALTER TABLE messages ADD COLUMN kind TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    if not _has_column(con, "messages", "delivery_offset"):
        con.execute("ALTER TABLE messages ADD COLUMN delivery_offset INTEGER")
    if not _has_column(con, "messages", "delivered_at"):
        con.execute("ALTER TABLE messages ADD COLUMN delivered_at TEXT")
    if not _has_column(con, "messages", "recalled_at"):
        con.execute("ALTER TABLE messages ADD COLUMN recalled_at TEXT")
    if not _has_column(con, "messages", "recalled_by"):
        con.execute("ALTER TABLE messages ADD COLUMN recalled_by TEXT")

    # W-0007: memorable run identities. The column is added without a UNIQUE
    # constraint so existing pre-W-0007 rows (all slug = NULL) are valid;
    # uniqueness is then enforced by a PARTIAL UNIQUE INDEX that ignores
    # NULL rows. The DB is the single source of truth here — even when two
    # processes run concurrently and race the in-Python collision check,
    # the constraint still rejects the loser with ``UNIQUE constraint
    # failed: runs.slug``.
    if not _has_column(con, "runs", "slug"):
        con.execute("ALTER TABLE runs ADD COLUMN slug TEXT")
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_slug_unique "
        "ON runs(slug) WHERE slug IS NOT NULL"
    )

    # W-0015: native child runs. ``parent_run`` already means a backend
    # session continuation, so child ownership must remain a separate edge.
    if not _has_column(con, "runs", "lead_run"):
        con.execute("ALTER TABLE runs ADD COLUMN lead_run INTEGER")
    if not _has_column(con, "runs", "child_depth"):
        con.execute("ALTER TABLE runs ADD COLUMN child_depth INTEGER NOT NULL DEFAULT 0")
    if not _has_column(con, "runs", "child_wakeup_run"):
        con.execute("ALTER TABLE runs ADD COLUMN child_wakeup_run INTEGER")
    if not _has_column(con, "runs", "allow_question"):
        con.execute("ALTER TABLE runs ADD COLUMN allow_question INTEGER NOT NULL DEFAULT 0")
    if not _has_column(con, "runs", "question_wait_seconds"):
        con.execute(
            "ALTER TABLE runs ADD COLUMN question_wait_seconds INTEGER NOT NULL DEFAULT 1800"
        )
    if not _has_column(con, "runs", "supervisor_protocol"):
        con.execute("ALTER TABLE runs ADD COLUMN supervisor_protocol INTEGER NOT NULL DEFAULT 0")
    con.execute(
        "CREATE TABLE IF NOT EXISTS questions ("
        "id INTEGER PRIMARY KEY, run_id INTEGER UNIQUE NOT NULL REFERENCES runs(id), "
        "sender TEXT NOT NULL, recipient TEXT NOT NULL, question TEXT NOT NULL, "
        "recommended_default TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'waiting', "
        "asked_at TEXT NOT NULL, deadline_at TEXT NOT NULL, answered_at TEXT, "
        "answered_by TEXT, answer TEXT)"
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_runs_lead_run ON runs(lead_run)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_runs_parent_run ON runs(parent_run)")


def connect(root: Path) -> sqlite3.Connection:
    con = sqlite3.connect(paths.db_path(root), timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=10000")
    con.executescript(SCHEMA)
    _apply_migrations(con)
    return con


def connect_readonly(root: Path) -> sqlite3.Connection:
    """Open the project DB in strict read-only mode.

    Used by ``orchestra takeover`` and any other operation that must
    never mutate source rows or schema: no schema executes, no migrations
    run, no journal-mode switch is performed, and any write attempt raises
    ``sqlite3.OperationalError: attempt to write a readonly database``.

    SQLite may still materialize WAL/SHM bookkeeping sidecars when the
    database is already in WAL mode. That is SQLite's read protocol; the
    database file and its logical contents remain unchanged.

    SQLite's URI ``mode=ro`` is honored at the protocol layer; we set
    ``row_factory`` + ``busy_timeout`` for parity with :func:`connect`,
    and otherwise leave the connection untouched.
    """
    db_file = paths.db_path(root).resolve()
    uri = db_file.as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=10000")
    # Sanity probe — a write would fail loudly here if the URI was
    # silently downgraded. We don't roll back; we just observe.
    con.execute("SELECT 1").fetchone()
    return con
