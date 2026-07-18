import sqlite3
from datetime import datetime, timezone
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
  read_at TEXT
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


def connect(root: Path) -> sqlite3.Connection:
    con = sqlite3.connect(paths.db_path(root), timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=10000")
    con.executescript(SCHEMA)
    _apply_migrations(con)
    return con
