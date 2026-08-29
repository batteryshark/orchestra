"""The identity registry (schema v29) and the paths module.

A project is a slug and settings, never a folder: checkouts belong to each
dispatch and the run history answers "where does this project run". A
source's label-to-folder map lives in the work-bridge project, with its
tests."""
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestra import db, paths, project

DEMO_ID = "53efe3c3-6def-4797-8560-3dce073d7d63"
INNER_ID = "b993cc1f-857d-450c-96ec-c8864f754bef"


class PathsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.env = mock.patch.dict(os.environ, {"ORCHESTRA_HOME": str(self.home)})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def test_everything_lives_under_one_home(self) -> None:
        self.assertEqual(paths.home(), self.home)
        self.assertEqual(paths.db_path(), self.home / "orchestra.db")
        self.assertEqual(paths.briefs_dir(), self.home / "briefs")
        self.assertEqual(paths.logs_dir(), self.home / "logs")
        self.assertEqual(paths.project_dir("demo"),
                         self.home / "projects" / "demo")
        self.assertEqual(paths.worktrees_dir("demo"),
                         self.home / "projects" / "demo" / "worktrees")
        self.assertEqual(paths.workspace_dir("demo"),
                         self.home / "projects" / "demo" / "workspace")
        self.assertEqual(paths.run_dir("demo", 7),
                         self.home / "projects" / "demo" / "runs" / "run-7")

    @unittest.skipUnless(os.name == "posix", "POSIX mode bits")
    def test_state_directories_are_owner_only_and_existing_modes_are_repaired(self) -> None:
        logs = self.home / "logs"
        logs.mkdir(parents=True, mode=0o755)
        self.home.chmod(0o755)
        logs.chmod(0o755)

        logs = paths.logs_dir()
        self.assertEqual(stat.S_IMODE(self.home.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(logs.stat().st_mode), 0o700)
        touched = [paths.db_path().parent, paths.briefs_dir(), logs,
                   paths.hooks_dir(), paths.worktrees_dir("demo").parent,
                   paths.worktrees_dir("demo")]

        for path in touched:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700, path)

    def test_an_empty_home_is_rejected_without_touching_the_current_directory(self) -> None:
        current = Path.cwd()
        scratch = Path(self.tmp.name) / "cwd"
        scratch.mkdir(mode=0o755)
        before = stat.S_IMODE(scratch.stat().st_mode)
        os.chdir(scratch)
        try:
            with mock.patch.dict(os.environ, {"ORCHESTRA_HOME": ""}):
                with self.assertRaisesRegex(SystemExit, "must not be empty"):
                    paths.db_path()
        finally:
            os.chdir(current)
        self.assertEqual(stat.S_IMODE(scratch.stat().st_mode), before)

    def test_a_broad_home_is_rejected_before_permissions_change(self) -> None:
        current = Path.cwd()
        scratch = Path(self.tmp.name) / "cwd"
        scratch.mkdir(mode=0o755)
        before = stat.S_IMODE(scratch.stat().st_mode)
        os.chdir(scratch)
        try:
            for configured in (".", "nested/..", scratch.anchor):
                with self.subTest(configured=configured), \
                        mock.patch.dict(os.environ, {"ORCHESTRA_HOME": configured}):
                    with self.assertRaisesRegex(SystemExit, "dedicated state directory"):
                        paths.db_path()
        finally:
            os.chdir(current)
        self.assertEqual(stat.S_IMODE(scratch.stat().st_mode), before)

    def test_home_default_is_dot_orchestra_in_the_user_home(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            del os.environ["ORCHESTRA_HOME"]
            self.assertEqual(paths.home(), Path("~/.orchestra").expanduser())


class RegistryFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name).resolve()
        self.workspace = self.tmp_path / "workspace"
        self.workspace.mkdir()
        self.env = mock.patch.dict(os.environ, {
            "ORCHESTRA_HOME": str(self.tmp_path / "home"),
            "ORCHESTRA_CONFIG": str(self.tmp_path / "global.toml")})
        self.env.start()
        self.con = db.connect()

    def tearDown(self) -> None:
        self.con.close()
        self.env.stop()
        self.tmp.cleanup()

    def seed(self, entries=None):
        return project.remember_identity(self.con, [
            (e.get("projectId"), e.get("name"), e.get("archived") is True)
            for e in (entries or [{"projectId": DEMO_ID, "name": "Demo"}])])

    def run_in(self, project_id: str, repo: Path) -> int:
        cur = self.con.execute(
            "INSERT INTO runs(profile, backend, requested_by, workdir, "
            "project_id, repo, started_at) VALUES('p','codex','human',?,?,?,?)",
            (str(repo), project_id, str(repo), db.now()))
        self.con.commit()
        return int(cur.lastrowid)


class IdentityTests(RegistryFixture):
    def test_create_mints_a_kebab_slug_and_is_idempotent(self) -> None:
        made = project.create(self.con, "My Project!")
        self.assertEqual(made.slug, "my-project")
        self.assertTrue(made.local)
        self.assertEqual(project.create(self.con, "My Project!").project_id,
                         made.project_id)

    def test_two_names_that_kebab_alike_stay_two_projects(self) -> None:
        # Identity is the slug: the SAME name is the same project for the
        # owner, but a cached source project never collides — its slug gets
        # the suffix instead.
        project.create(self.con, "demo")
        self.seed()  # the source's "Demo" kebabs to "demo" too
        rows = project.all_projects(self.con)
        self.assertEqual({r.slug for r in rows}, {"demo", "demo-2"})

    def test_create_refuses_an_empty_name(self) -> None:
        with self.assertRaisesRegex(SystemExit, "needs a name"):
            project.create(self.con, "  ")

    def test_find_resolves_slug_then_id(self) -> None:
        made = project.create(self.con, "demo")
        for selector in ("demo", made.project_id):
            self.assertEqual(project.find(self.con, selector).project_id,
                             made.project_id, selector)
        self.assertIsNone(project.find(self.con, "no-such"))

    def test_forget_drops_local_and_refuses_source_cached(self) -> None:
        project.create(self.con, "mine")
        self.seed()
        self.assertTrue(project.forget(self.con, "mine"))
        self.assertIsNone(project.by_slug(self.con, "mine"))
        with self.assertRaisesRegex(SystemExit, "cached from a source"):
            project.forget(self.con, "demo")

    def test_archive_hides_from_the_default_list(self) -> None:
        made = project.create(self.con, "parked")
        hit = project.set_archived(self.con, "parked", True)
        self.assertTrue(hit.archived)
        self.assertEqual([r.slug for r in project.all_projects(self.con)], [])
        listed = project.all_projects(self.con, include_archived=True)
        self.assertEqual([r.slug for r in listed], ["parked"])
        project.set_archived(self.con, made.project_id, False)
        self.assertEqual([r.slug for r in project.all_projects(self.con)],
                         ["parked"])

    def test_the_owners_override_survives_a_refresh_that_says_otherwise(self) -> None:
        """DESIGN §1: NULL follows the source; 0 or 1 was decided here and
        no refresh may overwrite it."""
        self.seed()
        project.set_archived(self.con, "demo", True)
        self.seed([{"projectId": DEMO_ID, "id": "demo", "name": "Demo",
                    "path": "demo", "archived": False}])
        self.assertTrue(project.by_slug(self.con, "demo").archived)
        # And the source's own park works with no local action at all.
        self.seed([{"projectId": INNER_ID, "id": "inner", "name": "Inner",
                    "path": "inner", "archived": True}])
        hit = project.by_slug(self.con, "inner")
        self.assertTrue(hit.archived)
        self.assertIsNone(hit.archived_override)

    def test_a_refresh_or_rename_never_rewrites_a_slug(self) -> None:
        self.seed()
        self.seed([{"projectId": DEMO_ID, "id": "demo", "name": "Renamed",
                    "path": "demo"}])
        hit = project.by_slug(self.con, "demo")
        self.assertEqual(hit.project_id, DEMO_ID)
        self.assertEqual(hit.name, "Renamed")
        self.assertIsNone(project.by_slug(self.con, "renamed"))

    def test_a_vanished_source_project_is_pruned_unless_held(self) -> None:
        """A cached identity the source no longer names goes — unless a run,
        or the owner's own override, holds onto it."""
        self.seed([{"projectId": DEMO_ID, "id": "demo", "name": "Demo",
                    "path": "demo"},
                   {"projectId": INNER_ID, "id": "inner", "name": "Inner",
                    "path": "inner"}])
        self.run_in(INNER_ID, self.workspace / "inner")
        keeper = project.create(self.con, "mine")
        self.seed([{"projectId": "p-new", "id": "new", "name": "New",
                    "path": "new"}])
        slugs = {r.slug for r in project.all_projects(self.con, True)}
        self.assertNotIn("demo", slugs)      # vanished, nothing held it
        self.assertIn("inner", slugs)        # its runs hold it
        self.assertIn("mine", slugs)         # owner-minted rows never prune
        self.assertIn("new", slugs)
        self.assertTrue(project.by_id(self.con, keeper.project_id).local)


class HistoryResolutionTests(RegistryFixture):
    """cwd -> project comes from the RUN HISTORY: the runner's own records
    are the address book, and no stored path exists to go stale."""

    def test_the_deepest_run_repo_wins(self) -> None:
        outer = self.workspace / "demo"
        inner = outer / "vendored"
        inner.mkdir(parents=True)
        self.run_in(DEMO_ID, outer)
        self.run_in(INNER_ID, inner)
        self.seed([{"projectId": DEMO_ID, "id": "demo", "name": "Demo",
                    "path": "demo"},
                   {"projectId": INNER_ID, "id": "inner", "name": "Inner",
                    "path": "demo/vendored"}])
        self.assertEqual(project.for_dir(self.con, outer / "src").project_id,
                         DEMO_ID)
        self.assertEqual(project.for_dir(self.con, inner).project_id,
                         INNER_ID)
        self.assertIsNone(project.for_dir(self.con, self.tmp_path / "else"))

    def test_last_root_is_the_newest_existing_checkout(self) -> None:
        self.seed()
        gone = self.workspace / "gone"
        real = self.workspace / "real"
        real.mkdir()
        self.run_in(DEMO_ID, real)
        self.run_in(DEMO_ID, gone)  # newer, but the folder does not exist
        self.assertEqual(project.last_root(self.con, DEMO_ID), real)
        self.assertIsNone(project.last_root(self.con, INNER_ID))

    def test_root_for_prefers_the_runs_own_repo(self) -> None:
        self.seed()
        repo = self.workspace / "demo"
        repo.mkdir()
        run_id = self.run_in(DEMO_ID, repo)
        run = self.con.execute("SELECT * FROM runs WHERE id=?",
                               (run_id,)).fetchone()
        self.assertEqual(project.root_for(self.con, run), repo)


class ArtifactPathTests(RegistryFixture):
    def test_run_artifacts_file_under_the_project_by_board_number(self) -> None:
        made = project.create(self.con, "demo")
        cur = self.con.execute(
            "INSERT INTO runs(profile, backend, requested_by, workdir, "
            f"project_id, started_at, project_seq) VALUES('p','codex','human',"
            f"'/x',?,?,{db.NEXT_PROJECT_SEQ})",
            (made.project_id, db.now(), made.project_id))
        self.con.commit()
        run = self.con.execute("SELECT * FROM runs WHERE id=?",
                               (cur.lastrowid,)).fetchone()
        bp, lp = project.run_artifacts(self.con, run)
        base = paths.project_dir("demo") / "runs" / "run-1"
        self.assertEqual(bp, base / "brief.md")
        self.assertEqual(lp, base / "log.jsonl")
        self.assertEqual(project.dir_key_for(self.con, run), "demo")

    def test_a_control_turn_stays_flat_by_row_id(self) -> None:
        cur = self.con.execute(
            "INSERT INTO runs(profile, backend, requested_by, workdir, "
            "layer, started_at) VALUES('p','codex','observer','/x','observer',?)",
            (db.now(),))
        self.con.commit()
        run = self.con.execute("SELECT * FROM runs WHERE id=?",
                               (cur.lastrowid,)).fetchone()
        bp, lp = project.run_artifacts(self.con, run)
        self.assertEqual(bp, paths.briefs_dir() / f"run-{run['id']}.md")
        self.assertEqual(lp, paths.logs_dir() / f"run-{run['id']}.jsonl")


class MigrationV29Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name).resolve()
        self.env = mock.patch.dict(os.environ, {
            "ORCHESTRA_HOME": str(self.tmp_path / "home"),
            "ORCHESTRA_CONFIG": str(self.tmp_path / "global.toml")})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def test_the_path_registry_becomes_identity_plus_checkouts(self) -> None:
        db_file = self.tmp_path / "old.db"
        old = sqlite3.connect(db_file)
        old.executescript("""
            CREATE TABLE projects (
              path TEXT PRIMARY KEY, project_id TEXT NOT NULL,
              source_ref TEXT, name TEXT, refreshed_at TEXT NOT NULL,
              archived INTEGER NOT NULL DEFAULT 0, archived_override INTEGER);
            INSERT INTO projects VALUES
              ('/w/My Tool', 'p-1', 'g/tool', 'My Tool', 't', 0, NULL),
              ('/w/link',    'p-1', NULL,     'My Tool', 't', 0, NULL),
              ('/w/local',   'p-2', NULL,     'Local',   't', 0, 1);
        """)
        old.commit()
        old.close()

        con = db.connect(db_file)
        try:
            cols = {r["name"] for r in con.execute(
                "PRAGMA table_info(projects)")}
            self.assertNotIn("path", cols)
            self.assertIn("slug", cols)
            rows = {r["project_id"]: r for r in con.execute(
                "SELECT * FROM projects")}
            self.assertEqual(rows["p-1"]["slug"], "my-tool")
            self.assertEqual(rows["p-1"]["local"], 0)
            self.assertEqual(rows["p-2"]["slug"], "local")
            self.assertEqual(rows["p-2"]["local"], 1)
            self.assertEqual(rows["p-2"]["archived_override"], 1)
            # The adapter's map got the source row AND its link binding;
            # the purely local path was dropped, not moved.
            checkouts = {r["path"]: r["source_ref"] for r in con.execute(
                "SELECT * FROM checkouts")}
            self.assertEqual(checkouts,
                             {"/w/My Tool": "g/tool", "/w/link": None})
        finally:
            con.close()

    def test_old_runs_keep_a_landing_target(self) -> None:
        """``runs.repo`` is backfilled from the registry BEFORE the paths
        leave the core — but only from a path that IS a directory: a
        source's cached path is often an organizational label that never
        existed on disk, and stamping that would aim root_for at nothing."""
        real = self.tmp_path / "tool"
        real.mkdir()
        db_file = self.tmp_path / "old.db"
        db.connect(db_file).close()  # a real runs table, current shape
        old = sqlite3.connect(db_file)
        old.executescript(f"""
            DROP TABLE projects;
            DROP TABLE IF EXISTS checkouts;
            CREATE TABLE projects (
              path TEXT PRIMARY KEY, project_id TEXT NOT NULL,
              source_ref TEXT, name TEXT, refreshed_at TEXT NOT NULL,
              archived INTEGER NOT NULL DEFAULT 0, archived_override INTEGER);
            INSERT INTO projects VALUES
              ('/w/Group/tool', 'p-1', 'Group/tool', 'Tool', 't', 0, NULL),
              ('{real}',        'p-1', NULL,         'Tool', 't', 0, NULL),
              ('/w/ghost',      'p-2', 'g/ghost',    'Ghost', 't', 0, NULL);
            INSERT INTO runs(id, profile, backend, requested_by, workdir,
                             status, started_at, project_id)
              VALUES (1, 'p', 'codex', 'human', '/w/wt/run-1', 'done', 't',
                      'p-1'),
                     (2, 'p', 'codex', 'human', '/w/wt/run-2', 'done', 't',
                      'p-2');
        """)
        old.commit()
        old.close()

        con = db.connect(db_file)
        try:
            rows = {r["id"]: r["repo"] for r in con.execute(
                "SELECT id, repo FROM runs")}
            self.assertEqual(rows[1], str(real))  # the one that exists
            self.assertIsNone(rows[2])  # a label is not a landing target
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
