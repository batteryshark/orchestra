"""Backwards-compatible migration tests for the runs.slug column.

Coverage targets:
  * Old DB files (no slug column) are still readable end-to-end after
    db.connect.
  * Pre-existing rows remain valid; the new column is NULL for them.
  * Dispatcher's INSERT path now populates slug; SELECT * exposes it.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from orchestra_cli import db, names


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        sd = self.root / ".orchestra"
        sd.mkdir(parents=True, exist_ok=True)
        # Hand-craft an OLD database that doesn't have the slug column.
        # db.connect() resolves to <root>/.orchestra/orchestra.db — the test
        # must write there, not at the project root.
        legacy = sqlite3.connect(sd / "orchestra.db")
        legacy.executescript("""
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE teams (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL,
              about TEXT DEFAULT '', created_at TEXT NOT NULL);
            CREATE TABLE members (team_id INTEGER NOT NULL REFERENCES teams(id),
              agent TEXT NOT NULL, UNIQUE(team_id, agent));
            CREATE TABLE messages (id INTEGER PRIMARY KEY, sender TEXT NOT NULL,
              recipient TEXT NOT NULL, body TEXT NOT NULL, work_item TEXT,
              run_id INTEGER, kind TEXT DEFAULT '', created_at TEXT NOT NULL,
              read_at TEXT);
            CREATE TABLE runs (
              id INTEGER PRIMARY KEY, agent TEXT NOT NULL, backend TEXT NOT NULL,
              model TEXT, title TEXT, work_item TEXT, team TEXT,
              requested_by TEXT NOT NULL, brief_path TEXT, log_path TEXT,
              workdir TEXT NOT NULL, branch TEXT, parent_run INTEGER, pid INTEGER,
              session_ref TEXT, status TEXT NOT NULL DEFAULT 'spawning',
              exit_code INTEGER, summary TEXT, started_at TEXT NOT NULL,
              finished_at TEXT
            );
            CREATE TABLE feed (id INTEGER PRIMARY KEY, author TEXT NOT NULL,
              body TEXT NOT NULL, tags TEXT DEFAULT '', work_item TEXT, run_id INTEGER,
              created_at TEXT NOT NULL);
        """)
        legacy.execute(
            "INSERT INTO runs(agent, backend, model, title, work_item, team, "
            "requested_by, workdir, started_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            ("minimax", "opencode", "minimax-coding-plan/MiniMax-M3",
             "pre-existing", "W-0006", None, "codex",
             str(self.root), _now()),
        )
        legacy.commit()
        legacy.close()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_connect_keeps_legacy_rows_readable(self) -> None:
        con = db.connect(self.root)
        try:
            row = con.execute("SELECT * FROM runs LIMIT 1").fetchone()
            self.assertEqual(row["agent"], "minimax")
            self.assertEqual(row["backend"], "opencode")
            # The newly-migrated column must be present and NULL.
            self.assertIn("slug", row.keys())
            self.assertIsNone(row["slug"])
            self.assertEqual(row["allow_question"], 0)
            self.assertEqual(row["question_wait_seconds"], 1800)
            self.assertEqual(row["supervisor_protocol"], 0)
            message_cols = {
                r["name"] for r in con.execute("PRAGMA table_info(messages)").fetchall()
            }
            self.assertIn("delivery_offset", message_cols)
            self.assertIn("delivered_at", message_cols)
            question_cols = {
                r["name"] for r in con.execute("PRAGMA table_info(questions)").fetchall()
            }
            self.assertIn("recommended_default", question_cols)
            self.assertIn("deadline_at", question_cols)
        finally:
            con.close()

    def test_repeat_connect_is_idempotent(self) -> None:
        # Opening twice in a row must NOT blow up (PRAGMA would re-add the
        # column on every call otherwise).
        con1 = db.connect(self.root); con1.close()
        con2 = db.connect(self.root)
        try:
            cols = [r["name"] for r in con2.execute("PRAGMA table_info(runs)").fetchall()]
            self.assertEqual(cols.count("slug"), 1)
        finally:
            con2.close()

    def test_dispatcher_style_insert_writes_slug(self) -> None:
        con = db.connect(self.root)
        try:
            slug = names.assign_slug(con)
            cur = con.execute(
                "INSERT INTO runs(agent, backend, model, title, work_item, "
                "team, requested_by, workdir, slug, status, started_at) "
                "VALUES(?,?,?,?,?,?,?,?,?, 'spawning', ?)",
                ("glim", "opencode", "minimax-coding-plan/MiniMax-M3",
                 "after", "W-0007", None, "codex",
                 str(self.root), slug, _now()),
            )
            con.commit()
            row = con.execute("SELECT * FROM runs WHERE id=?", (cur.lastrowid,)).fetchone()
            self.assertEqual(row["slug"], slug)
            # The legacy row is still readable.
            legacy = con.execute("SELECT id FROM runs WHERE title='pre-existing'").fetchone()
            self.assertIsNotNone(legacy)
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
