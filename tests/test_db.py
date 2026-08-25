import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestra import db, paths

INSERT_MIN = ("INSERT INTO runs(profile, backend, requested_by, workdir, "
              "started_at) VALUES('a','codex','human','/p',?)")


def schema_without(columns) -> str:
    """Today's schema minus a migration's (name, sql_type) column list."""
    schema = db.SCHEMA
    for name, sql_type in columns:
        schema = schema.replace(f",\n  {name} {sql_type}", "")
    return schema


class DbTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"ORCHESTRA_HOME": str(self.root / "home")})
        self.env.start()
        self.con = db.connect()

    def tearDown(self) -> None:
        self.con.close()
        self.env.stop()
        self.tmp.cleanup()

    def legacy_db(self, name: str, schema: str, *seed) -> Path:
        """An old-schema database on disk, ready for db.connect to migrate."""
        legacy = self.root / name
        old = sqlite3.connect(legacy)
        old.executescript(schema)
        for sql, params in seed:
            old.execute(sql, params)
        old.commit()
        old.close()
        return legacy

    def test_database_is_central_not_per_project(self) -> None:
        """DESIGN §2: one database under ORCHESTRA_HOME; the project directory
        gets no state of its own."""
        self.assertEqual(paths.db_path(), self.root / "home" / "orchestra.db")
        self.assertTrue(paths.db_path().is_file())
        self.assertFalse((self.root / ".orchestra").exists())

    def test_v3_database_upgrades_in_place(self) -> None:
        """A pre-W-0163 runs table gains project_id without losing rows."""
        # v3 == today's schema minus the project column and its index.
        # Only the RUNS column goes: an unscoped replace also strips
        # project_id from finding_fingerprints, whose index then fails to
        # create and makes this test about the wrong thing.
        v3 = (db.SCHEMA
              .replace(",\n  project_id TEXT,", ",", 1)
              .replace("CREATE INDEX IF NOT EXISTS idx_runs_project "
                       "ON runs(project_id);", ""))
        con = db.connect(self.legacy_db("legacy.db", v3, (INSERT_MIN, ("t",))))
        cols = {r["name"] for r in con.execute("PRAGMA table_info(runs)")}
        self.assertIn("project_id", cols)
        self.assertEqual(con.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"], 1)
        con.close()

    def test_v8_database_gains_the_usage_columns_in_place(self) -> None:
        """Schema v9 (DESIGN §11) is additive: an existing runs table gains
        the token/cost columns, keeps its rows, and reads them as null."""
        legacy = self.legacy_db("v8.db", schema_without(db.RUNS_V9_COLUMNS),
                                (INSERT_MIN, ("t",)))
        con = db.connect(legacy)
        con.row_factory = sqlite3.Row
        cols = {r["name"] for r in con.execute("PRAGMA table_info(runs)")}
        self.assertTrue({name for name, _ in db.RUNS_V9_COLUMNS} <= cols)
        row = con.execute("SELECT * FROM runs").fetchone()
        self.assertIsNone(row["tokens_total"])
        self.assertIsNone(row["usage_source"])
        db.connect(legacy).close()  # reconnect adds nothing twice
        con.close()

    def test_v15_database_only_settles_completed_terminal_rows(self) -> None:
        """An ambiguous old terminal row must not gain fictional receipts."""
        insert = (
            "INSERT INTO runs(profile, backend, requested_by, workdir, status, "
            "started_at, finished_at) VALUES('a','codex','human','/p',?,?,?)")
        legacy = self.legacy_db(
            "v15.db", schema_without(db.RUNS_V16_COLUMNS),
            (insert, ("done", "started", "done-at")),
            (insert, ("failed", "started", "failed-at")),
            (insert, ("running", "started", None)),
            ("INSERT INTO messages(run_id, sender, body, kind, created_at) "
             "VALUES(1, 'orchestra', 'finished', 'completion', 'done-at')", ()))

        con = db.connect(legacy)
        rows = list(con.execute(
            "SELECT status, landing_status, handoff_processed_at, work_reported_at, "
            "worker_status, worker_exit_code FROM runs ORDER BY id"))
        self.assertEqual(
            [(row["status"], row["landing_status"], row["handoff_processed_at"],
              row["work_reported_at"])
            for row in rows],
            [("done", "ok", "done-at", "done-at"),
             ("failed", None, None, None),
             ("running", None, None, None)])
        self.assertEqual(
            [(row["worker_status"], row["worker_exit_code"]) for row in rows],
            [(None, None), (None, None), (None, None)],
            "migration must not manufacture replay receipts for history")
        db.connect(legacy).close()
        again = con.execute(
            "SELECT landing_status, handoff_processed_at FROM runs WHERE status='done'"
        ).fetchone()
        self.assertEqual((again["landing_status"], again["handoff_processed_at"]),
                         ("ok", "done-at"))
        con.close()

    def test_run_round_trip_carries_project_id_and_slug_is_unique(self) -> None:
        insert = ("INSERT INTO runs(slug, profile, backend, model, title, "
                  "requested_by, workdir, project_id, status, started_at) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?)")
        row = ("calm_otter", "codex", "codex", "gpt-x", "test run", "human",
               "/tmp/p", "53efe3c3-6def-4797-8560-3dce073d7d63", "spawning",
               db.now())
        cur = self.con.execute(insert, row)
        run = self.con.execute("SELECT * FROM runs WHERE id=?",
                               (cur.lastrowid,)).fetchone()
        self.assertEqual(
            (run["slug"], run["profile"], run["status"], run["project_id"]),
            ("calm_otter", "codex", "spawning",
             "53efe3c3-6def-4797-8560-3dce073d7d63"))
        for column in ("finished_at", "landing_status", "handoff_processed_at",
                       "pid_identity", "supervisor_pid_identity", "worker_status",
                       "worker_exit_code", "work_claim_status"):
            self.assertIsNone(run[column], column)
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(insert, row)  # slug is UNIQUE

    def test_schema_version_recorded_and_reconnect_is_idempotent(self) -> None:
        self.con.execute(INSERT_MIN, (db.now(),))
        self.con.commit()
        again = db.connect()
        self.assertEqual(again.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()["value"], db.SCHEMA_VERSION)
        self.assertEqual(
            again.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"], 1)
        again.close()

    def test_messages_address_run_ids_not_profiles(self) -> None:
        """DESIGN D4: messages address run ids; there is no recipient-name
        column anywhere for a profile to act as a worker identity."""
        cols = {r["name"] for r in self.con.execute("PRAGMA table_info(messages)")}
        self.assertIn("run_id", cols)
        self.assertFalse(cols & {"recipient", "profile", "agent"})
        # run_id is mandatory: a message cannot float without a run address.
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "INSERT INTO messages(run_id, sender, body, created_at) "
                "VALUES(NULL, 'human', 'x', ?)", (db.now(),))

    def test_same_profile_runs_concurrently(self) -> None:
        """DESIGN D4: a profile is a launch template, never a singleton —
        the schema must accept many simultaneous active runs of one profile."""
        insert = ("INSERT INTO runs(profile, backend, requested_by, workdir, status, "
                  "started_at) VALUES('codex','codex','human','/p','running',?)")
        self.con.execute(insert, (db.now(),))
        self.con.execute(insert, (db.now(),))
        n = self.con.execute(
            "SELECT COUNT(*) AS n FROM runs WHERE profile='codex' "
            "AND status='running'").fetchone()["n"]
        self.assertEqual(n, 2)


if __name__ == "__main__":
    unittest.main()
