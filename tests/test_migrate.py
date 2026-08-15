"""`dromond migrate`: fold a legacy per-project database into ~/.dromond."""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dromond import db, migrate, paths

PROJECT_ID = "53efe3c3-6def-4797-8560-3dce073d7d63"

LEGACY_SCHEMA = (db.SCHEMA
                 .replace(",\n  project_id TEXT\n", "\n")
                 .replace("CREATE INDEX IF NOT EXISTS idx_runs_project "
                          "ON runs(project_id);", ""))


class MigrateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name).resolve()
        self.project = self.tmp_path / "demo"
        # The legacy per-project directory keeps its pre-rename name: it is
        # already on disk from the Maestro era, so W-0188 does not touch it.
        self.legacy_dir = self.project / paths.LEGACY_STATE_DIR
        self.assertEqual(paths.LEGACY_STATE_DIR, ".maestro")
        (self.legacy_dir / "briefs").mkdir(parents=True)
        (self.legacy_dir / "logs").mkdir(parents=True)
        self.legacy_db = self.legacy_dir / paths.LEGACY_STATE_DB
        self.env = mock.patch.dict(os.environ,
                                   {"DROMOND_HOME": str(self.tmp_path / "home")})
        self.env.start()
        self._build_legacy()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def _build_legacy(self) -> None:
        con = sqlite3.connect(self.legacy_db)
        con.executescript(LEGACY_SCHEMA)
        for run_id, slug in ((1, "calm_otter"), (2, "brave_stoat")):
            brief = self.legacy_dir / "briefs" / f"run-{run_id}.md"
            brief.write_text(f"brief for legacy run {run_id}")
            log = self.legacy_dir / "logs" / f"run-{run_id}.jsonl"
            log.write_text("{}\n")
            con.execute(
                "INSERT INTO runs(id, slug, profile, backend, title, requested_by, "
                "workdir, brief_path, log_path, status, started_at, work_item) "
                "VALUES(?,?, 'codex','codex','t','human',?,?,?, 'done','2026-01-01',?)",
                (run_id, slug, str(self.project), str(brief), str(log),
                 f"W-000{run_id}"))
        # run 2 continues run 1's session, and carries a message + a dependency
        con.execute("UPDATE runs SET parent_run=1 WHERE id=2")
        con.execute("INSERT INTO messages(run_id, sender, body, kind, created_at) "
                    "VALUES(2,'human','carry me','interrupt','2026-01-01')")
        con.execute("INSERT INTO dispatch_dependencies(run_id, depends_on_run) "
                    "VALUES(2,1)")
        con.execute("INSERT INTO deferred_dispatches(run_id, mission, status, "
                    "created_at) VALUES(2,'do it','fired','2026-01-01')")
        con.execute("INSERT INTO meta(key, value) VALUES('work_cursor_tasks','old')")
        con.commit()
        con.close()

    def test_find_legacy_db_walks_up_like_git(self) -> None:
        (self.project / "src").mkdir()
        self.assertEqual(migrate.find_legacy_db(self.project / "src"), self.legacy_db)
        self.assertIsNone(migrate.find_legacy_db(self.tmp_path))

    def test_fold_copies_runs_and_stamps_the_project_id(self) -> None:
        con = db.connect()
        report = migrate.fold(self.legacy_db, PROJECT_ID, con)
        self.assertEqual(report["runs"], 2)
        rows = list(con.execute("SELECT * FROM runs ORDER BY id"))
        self.assertEqual([r["work_item"] for r in rows], ["W-0001", "W-0002"])
        self.assertTrue(all(r["project_id"] == PROJECT_ID for r in rows))
        con.close()

    def test_fold_renumbers_and_remaps_every_reference(self) -> None:
        con = db.connect()
        # A run already in the central database, so legacy ids cannot be reused.
        con.execute("INSERT INTO runs(profile, backend, requested_by, workdir, "
                    "started_at) VALUES('p','codex','human','/p','2026-01-01')")
        con.commit()
        report = migrate.fold(self.legacy_db, PROJECT_ID, con)
        new_first, new_second = report["id_map"][1], report["id_map"][2]
        self.assertNotIn(1, (new_first, new_second))
        second = con.execute("SELECT * FROM runs WHERE id=?", (new_second,)).fetchone()
        self.assertEqual(second["parent_run"], new_first)
        self.assertEqual(con.execute(
            "SELECT run_id FROM messages").fetchone()["run_id"], new_second)
        dep = con.execute("SELECT * FROM dispatch_dependencies").fetchone()
        self.assertEqual((dep["run_id"], dep["depends_on_run"]),
                         (new_second, new_first))
        self.assertEqual(con.execute(
            "SELECT run_id FROM deferred_dispatches").fetchone()["run_id"],
            new_second)
        con.close()

    def test_fold_copies_briefs_and_logs_into_the_central_home(self) -> None:
        con = db.connect()
        report = migrate.fold(self.legacy_db, PROJECT_ID, con)
        new_id = report["id_map"][1]
        row = con.execute("SELECT * FROM runs WHERE id=?", (new_id,)).fetchone()
        self.assertEqual(Path(row["brief_path"]),
                         paths.briefs_dir() / f"run-{new_id}.md")
        self.assertEqual(Path(row["brief_path"]).read_text(),
                         "brief for legacy run 1")
        self.assertEqual(Path(row["log_path"]), paths.logs_dir() / f"run-{new_id}.jsonl")
        con.close()

    def test_fold_deletes_nothing_the_user_owns(self) -> None:
        before = sorted(p.name for p in self.legacy_dir.rglob("*"))
        con = db.connect()
        migrate.fold(self.legacy_db, PROJECT_ID, con)
        con.close()
        self.assertTrue(self.legacy_db.is_file())
        self.assertEqual(sorted(p.name for p in self.legacy_dir.rglob("*")), before)
        legacy = sqlite3.connect(self.legacy_db)
        self.assertEqual(
            legacy.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 2)
        legacy.close()

    def test_a_duplicate_slug_is_dropped_rather_than_colliding(self) -> None:
        con = db.connect()
        con.execute("INSERT INTO runs(slug, profile, backend, requested_by, workdir, "
                    "started_at) VALUES('calm_otter','p','codex','human','/p','x')")
        con.commit()
        report = migrate.fold(self.legacy_db, PROJECT_ID, con)
        self.assertEqual(report["dropped_slugs"], 1)
        slugs = [r["slug"] for r in con.execute("SELECT slug FROM runs ORDER BY id")]
        self.assertEqual(slugs, ["calm_otter", None, "brave_stoat"])
        con.close()

    def test_work_cursors_stay_the_central_databases_own(self) -> None:
        con = db.connect()
        migrate.fold(self.legacy_db, PROJECT_ID, con)
        self.assertIsNone(db.meta_get(con, "work_cursor_tasks"))
        self.assertEqual(db.meta_get(con, "schema_version"), db.SCHEMA_VERSION)
        con.close()

    def test_the_command_reports_what_it_copied_and_what_it_left(self) -> None:
        import contextlib
        import io
        from argparse import Namespace

        from dromond import cli
        os.environ["DROMOND_CONFIG"] = str(self.tmp_path / "global.toml")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.cmd_migrate(Namespace(path=str(self.project), project_id=PROJECT_ID))
        text = out.getvalue()
        self.assertIn("runs: 2", text)
        self.assertIn("nothing was deleted", text)
        self.assertIn(str(paths.db_path()), text)
        with self.assertRaises(SystemExit):
            cli.cmd_migrate(Namespace(path=str(self.tmp_path.parent),
                                      project_id=None))

    def test_fold_without_a_project_id_still_carries_the_runs(self) -> None:
        con = db.connect()
        self.assertEqual(migrate.fold(self.legacy_db, None, con)["runs"], 2)
        self.assertTrue(all(r["project_id"] is None
                            for r in con.execute("SELECT project_id FROM runs")))
        con.close()


if __name__ == "__main__":
    unittest.main()
