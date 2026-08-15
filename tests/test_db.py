import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dromond import db, paths


class DbTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"DROMOND_HOME": str(self.root / "home")})
        self.env.start()
        self.con = db.connect()

    def tearDown(self) -> None:
        self.con.close()
        self.env.stop()
        self.tmp.cleanup()

    def test_database_is_central_not_per_project(self) -> None:
        """DESIGN §2: one database under DROMOND_HOME; the project directory
        gets no state of its own."""
        self.assertEqual(paths.db_path(), self.root / "home" / "dromond.db")
        self.assertTrue(paths.db_path().is_file())
        self.assertFalse((self.root / ".dromond").exists())

    def test_project_id_is_carried_and_indexed(self) -> None:
        cur = self.con.execute(
            "INSERT INTO runs(profile, backend, requested_by, workdir, project_id, "
            "started_at) VALUES('a','codex','human','/p',?,?)",
            ("53efe3c3-6def-4797-8560-3dce073d7d63", db.now()))
        run = self.con.execute("SELECT * FROM runs WHERE id=?",
                               (cur.lastrowid,)).fetchone()
        self.assertEqual(run["project_id"], "53efe3c3-6def-4797-8560-3dce073d7d63")

    def test_v3_database_upgrades_in_place(self) -> None:
        """A pre-W-0163 runs table gains project_id without losing rows."""
        legacy = self.root / "legacy.db"
        old = sqlite3.connect(legacy)
        # v3 == today's schema minus the project column and its index.
        # Only the RUNS column goes: an unscoped replace also strips
        # project_id from finding_fingerprints, whose index then fails to
        # create and makes this test about the wrong thing.
        old.executescript(db.SCHEMA
                          .replace(",\n  project_id TEXT,", ",", 1)
                          .replace("CREATE INDEX IF NOT EXISTS idx_runs_project "
                                   "ON runs(project_id);", ""))
        old.execute("INSERT INTO runs(profile, backend, requested_by, workdir, "
                    "started_at) VALUES('a','codex','human','/p','t')")
        old.commit()
        old.close()
        con = db.connect(legacy)
        cols = {r["name"] for r in con.execute("PRAGMA table_info(runs)")}
        self.assertIn("project_id", cols)
        self.assertEqual(con.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"], 1)
        con.close()

    def test_v8_database_gains_the_usage_columns_in_place(self) -> None:
        """Schema v9 (DESIGN §11) is additive: an existing runs table gains
        the token/cost columns, keeps its rows, and reads them as null."""
        legacy = self.root / "v8.db"
        v8 = db.SCHEMA
        for name, sql_type in db.RUNS_V9_COLUMNS:
            v8 = v8.replace(f",\n  {name} {sql_type}", "")
        old = sqlite3.connect(legacy)
        old.executescript(v8)
        old.execute("INSERT INTO runs(profile, backend, requested_by, workdir, "
                    "started_at) VALUES('a','codex','human','/p','t')")
        old.commit()
        old.close()
        con = db.connect(legacy)
        con.row_factory = sqlite3.Row
        cols = {r["name"] for r in con.execute("PRAGMA table_info(runs)")}
        self.assertTrue({name for name, _ in db.RUNS_V9_COLUMNS} <= cols)
        row = con.execute("SELECT * FROM runs").fetchone()
        self.assertIsNone(row["tokens_total"])
        self.assertIsNone(row["usage_source"])
        db.connect(legacy).close()  # reconnect adds nothing twice
        con.close()

    def test_run_round_trip(self) -> None:
        cur = self.con.execute(
            "INSERT INTO runs(slug, profile, backend, model, title, requested_by, "
            "workdir, status, started_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("calm_otter", "codex", "codex", "gpt-x", "test run", "human",
             "/tmp/p", "spawning", db.now()))
        run = self.con.execute("SELECT * FROM runs WHERE id=?", (cur.lastrowid,)).fetchone()
        self.assertEqual(run["slug"], "calm_otter")
        self.assertEqual(run["profile"], "codex")
        self.assertEqual(run["status"], "spawning")
        self.assertIsNone(run["finished_at"])

    def test_slug_unique(self) -> None:
        insert = ("INSERT INTO runs(slug, profile, backend, requested_by, workdir, "
                  "started_at) VALUES(?,?,?,?,?,?)")
        self.con.execute(insert, ("calm_otter", "a", "codex", "human", "/p", db.now()))
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(insert, ("calm_otter", "b", "codex", "human", "/p", db.now()))

    def test_schema_version_recorded(self) -> None:
        row = self.con.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()
        self.assertEqual(row["value"], db.SCHEMA_VERSION)

    def test_reconnect_is_idempotent(self) -> None:
        self.con.execute(
            "INSERT INTO runs(profile, backend, requested_by, workdir, started_at) "
            "VALUES('a','codex','human','/p',?)", (db.now(),))
        self.con.commit()
        again = db.connect()
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
