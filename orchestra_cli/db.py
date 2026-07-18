import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from orchestra_cli import paths

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


def connect(root: Path) -> sqlite3.Connection:
    con = sqlite3.connect(paths.db_path(root), timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=10000")
    con.executescript(SCHEMA)
    try:  # migration for DBs created before messages.kind existed
        con.execute("ALTER TABLE messages ADD COLUMN kind TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    return con
