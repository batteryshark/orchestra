"""Central paths and the directory -> Work projectId mapping (DESIGN §2)."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dromond import config, db, paths, project
from tests.fake_work import FakeWork

DEMO_ID = "53efe3c3-6def-4797-8560-3dce073d7d63"
INNER_ID = "b993cc1f-857d-450c-96ec-c8864f754bef"


class PathsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.env = mock.patch.dict(os.environ, {"DROMOND_HOME": str(self.home)})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def test_everything_lives_under_one_home(self) -> None:
        self.assertEqual(paths.home(), self.home)
        self.assertEqual(paths.db_path(), self.home / "dromond.db")
        self.assertEqual(paths.briefs_dir(), self.home / "briefs")
        self.assertEqual(paths.logs_dir(), self.home / "logs")
        self.assertEqual(paths.worktrees_dir("demo"),
                         self.home / "worktrees" / "demo")

    def test_worktree_dir_is_keyed_by_the_immutable_project_id(self) -> None:
        """The Work id is mutable, so renaming a project would strand its
        worktree directory; the projectId UUID never changes."""
        uuid = "53efe3c3-6def-4797-8560-3dce073d7d63"
        self.assertEqual(paths.worktrees_dir(uuid),
                         self.home / "worktrees" / uuid)
        self.assertTrue(paths.worktrees_dir(uuid).is_dir())

    def test_home_default_is_dot_dromond_in_the_user_home(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            del os.environ["DROMOND_HOME"]
            self.assertEqual(paths.home(), Path("~/.dromond").expanduser())


class ProjectResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name).resolve()
        self.workspace = self.tmp_path / "workspace"
        (self.workspace / "demo" / "src").mkdir(parents=True)
        (self.workspace / "demo" / "inner").mkdir(parents=True)
        self.global_config = self.tmp_path / "global.toml"
        self.global_config.write_text("")
        self.env = mock.patch.dict(os.environ, {
            "DROMOND_HOME": str(self.tmp_path / "home"),
            "DROMOND_CONFIG": str(self.global_config)})
        self.env.start()
        self.con = db.connect()

    def tearDown(self) -> None:
        self.con.close()
        self.env.stop()
        self.tmp.cleanup()

    def seed(self, entries=None):
        return project.remember(self.con, str(self.workspace), entries or [
            {"projectId": DEMO_ID, "id": "demo", "name": "Demo", "path": "demo"}])

    def resolve(self, where):
        return project.resolve(self.con, {}, str(where))

    def test_subdirectory_resolves_to_its_project(self) -> None:
        self.seed()
        hit = self.resolve(self.workspace / "demo" / "src")
        self.assertEqual(hit.project_id, DEMO_ID)
        self.assertEqual(hit.path, self.workspace / "demo")

    def test_deepest_prefix_wins_over_an_enclosing_project(self) -> None:
        self.seed([
            {"projectId": DEMO_ID, "id": "demo", "name": "Demo", "path": "demo"},
            {"projectId": INNER_ID, "id": "demo/inner", "name": "Inner",
             "path": "demo/inner"}])
        self.assertEqual(self.resolve(self.workspace / "demo" / "inner").project_id,
                         INNER_ID)
        self.assertEqual(self.resolve(self.workspace / "demo" / "src").project_id,
                         DEMO_ID)

    def test_alias_paths_resolve_to_the_same_project_id(self) -> None:
        alias = self.tmp_path / "elsewhere"
        alias.mkdir()
        self.seed([{"projectId": DEMO_ID, "id": "demo", "name": "Demo",
                    "path": "demo", "aliasPaths": [str(alias)]}])
        self.assertEqual(self.resolve(alias).project_id, DEMO_ID)

    def test_a_directory_outside_every_project_is_a_clear_error(self) -> None:
        self.seed()
        with self.assertRaises(SystemExit) as ctx:
            self.resolve(self.tmp_path)
        self.assertIn("not inside a known Work project", str(ctx.exception))

    def test_offline_resolution_uses_the_cache(self) -> None:
        """Work unreachable: the CLI still resolves from the cached mapping."""
        self.seed()
        cfg = {"work": {"enabled": True, "api_url": "http://127.0.0.1:9"}}
        hit = project.resolve(self.con, cfg, str(self.workspace / "demo"))
        self.assertEqual(hit.project_id, DEMO_ID)

    def test_a_miss_refreshes_from_work_once_and_then_resolves(self) -> None:
        work = FakeWork(workspace_root=self.workspace)
        work.add_project("demo", DEMO_ID, path="demo", name="Demo")
        url = work.start()
        try:
            cfg = {"work": {"enabled": True, "api_url": url}}
            hit = project.resolve(self.con, cfg, str(self.workspace / "demo" / "src"))
            self.assertEqual(hit.project_id, DEMO_ID)
            self.assertIn(("GET", "/api/projects"), work.requests)
        finally:
            work.stop()

    def test_renaming_the_folder_keeps_the_projects_settings(self) -> None:
        """Acceptance (W-0163): settings key on projectId, so a rename that
        Work reports back loses nothing."""
        self.seed()
        self.global_config.write_text(
            f"[project.\"{DEMO_ID}\".settings]\ntimeout = 4242\n")
        run = self.con.execute(
            "INSERT INTO runs(profile, backend, requested_by, workdir, project_id, "
            "started_at) VALUES('p','codex','human',?,?,?)",
            (str(self.workspace / "demo"), DEMO_ID, db.now())).lastrowid

        (self.workspace / "renamed").mkdir()
        self.con.execute("DELETE FROM projects")  # Work re-reports the new path
        self.seed([{"projectId": DEMO_ID, "id": "demo", "name": "Demo",
                    "path": "renamed"}])

        row = self.con.execute("SELECT * FROM runs WHERE id=?", (run,)).fetchone()
        self.assertEqual(project.root_for(self.con, row), self.workspace / "renamed")
        self.assertEqual(self.resolve(self.workspace / "renamed").project_id, DEMO_ID)
        self.assertEqual(config.load(row["project_id"])["settings"]["timeout"], 4242)

    def test_work_item_project_path_maps_to_the_project_id(self) -> None:
        self.seed()
        self.assertEqual(project.by_work_path(self.con, "demo").project_id, DEMO_ID)
        self.assertEqual(
            project.by_work_path(self.con, str(self.workspace / "demo")).project_id,
            DEMO_ID)
        self.assertIsNone(project.by_work_path(self.con, "no-such-project"))
        self.assertIsNone(project.by_work_path(self.con, None))

    def test_a_project_work_has_not_stamped_is_not_cached(self) -> None:
        self.assertEqual(self.seed([{"id": "demo", "path": "demo"}]), 0)


class CliSurfaceTests(unittest.TestCase):
    """Acceptance (W-0163): init leaves nothing in the project directory, and
    the read commands work from anywhere now that state is central."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name).resolve()
        self.workspace = self.tmp_path / "workspace"
        self.project_dir = self.workspace / "demo"
        self.project_dir.mkdir(parents=True)
        self.env = mock.patch.dict(os.environ, {
            "DROMOND_HOME": str(self.tmp_path / "home"),
            "DROMOND_CONFIG": str(self.tmp_path / "global.toml"),
            "DROMOND_LAUNCH_AGENTS": str(self.tmp_path / "LaunchAgents"),
            "DROMOND_ROOT": str(self.project_dir)})
        self.env.start()
        con = db.connect()
        project.remember(con, str(self.workspace),
                         [{"projectId": DEMO_ID, "id": "demo", "name": "Demo",
                           "path": "demo"}])
        con.close()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def _run(self, fn, **kwargs):
        import contextlib
        import io
        from argparse import Namespace
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fn(Namespace(**kwargs))
        return out.getvalue()

    def test_init_creates_no_directory_inside_the_project(self) -> None:
        from dromond import cli
        with mock.patch("dromond.cli.Path.cwd", return_value=self.project_dir):
            text = self._run(cli.cmd_init)
        self.assertEqual(list(self.project_dir.iterdir()), [self.project_dir / ".git"])
        self.assertFalse((self.project_dir / ".dromond").exists())
        self.assertIn(str(paths.home()), text)
        self.assertIn(DEMO_ID, text)

    def test_runs_lists_every_project_and_here_narrows_it(self) -> None:
        from dromond import cli
        con = db.connect()
        for pid, title in ((DEMO_ID, "mine"), (INNER_ID, "theirs")):
            con.execute("INSERT INTO runs(profile, backend, title, requested_by, "
                        "workdir, project_id, started_at) "
                        "VALUES('p','codex',?,'human','/p',?,?)", (title, pid, db.now()))
        con.commit()
        con.close()
        everything = self._run(cli.cmd_runs, active=False, here=False, json=False)
        self.assertIn("mine", everything)
        self.assertIn("theirs", everything)
        here = self._run(cli.cmd_runs, active=False, here=True, json=False)
        self.assertIn("mine", here)
        self.assertNotIn("theirs", here)

    def test_doctor_reports_the_central_home(self) -> None:
        from dromond import cli
        text = self._run(cli.cmd_doctor)
        self.assertIn(str(paths.home()), text)
        self.assertIn(DEMO_ID, text)


class AdoptTests(unittest.TestCase):
    """Standalone use. The projects table was written only by Work, so without
    it `dispatch` could not resolve any directory and the tool did not run on
    its own at all."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.env = mock.patch.dict(os.environ, {"DROMOND_HOME": str(self.root / "home")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.con = db.connect()
        self.addCleanup(self.con.close)
        self.repo = self.root / "repo"
        self.repo.mkdir()

    def test_an_adopted_directory_resolves(self) -> None:
        adopted = project.adopt(self.con, self.repo)
        self.assertEqual(self.repo, Path(adopted.path))
        self.assertIsNone(adopted.work_id)
        self.assertTrue(adopted.project_id)
        found = project.resolve(self.con, {}, str(self.repo))
        self.assertEqual(adopted.project_id, found.project_id)

    def test_adopting_twice_is_the_same_project(self) -> None:
        first = project.adopt(self.con, self.repo)
        again = project.adopt(self.con, self.repo, name="renamed")
        self.assertEqual(first.project_id, again.project_id)
        self.assertEqual(1, len(project.all_projects(self.con)))

    def test_a_work_refresh_does_not_delete_an_adopted_project(self) -> None:
        adopted = project.adopt(self.con, self.repo)
        other = self.root / "from-work"
        other.mkdir()
        project.remember(self.con, str(self.root),
                         [{"projectId": "w-1", "id": 7, "name": "theirs",
                           "path": "from-work"}])
        paths = {str(p.path) for p in project.all_projects(self.con)}
        self.assertIn(str(self.repo), paths)
        self.assertEqual(adopted.project_id,
                         project.resolve(self.con, {}, str(self.repo)).project_id)

    def test_work_wins_when_it_names_the_same_directory(self) -> None:
        project.adopt(self.con, self.repo)
        project.remember(self.con, str(self.root),
                         [{"projectId": "w-1", "id": 7, "name": "theirs",
                           "path": "repo"}])
        found = project.resolve(self.con, {}, str(self.repo))
        self.assertEqual("w-1", found.project_id)
        # work_id is a TEXT column, so an integer id round-trips as a string.
        self.assertEqual("7", str(found.work_id))

    def test_forget_drops_a_local_project_but_refuses_one_from_work(self) -> None:
        project.adopt(self.con, self.repo)
        self.assertTrue(project.forget(self.con, self.repo))
        self.assertEqual([], project.all_projects(self.con))
        self.assertFalse(project.forget(self.con, self.repo))

        project.remember(self.con, str(self.root),
                         [{"projectId": "w-1", "id": 7, "name": "theirs",
                           "path": "repo"}])
        with self.assertRaises(SystemExit):
            project.forget(self.con, self.repo)

    def test_a_file_is_not_a_project(self) -> None:
        f = self.root / "a.txt"
        f.write_text("x")
        with self.assertRaises(SystemExit):
            project.adopt(self.con, f)


if __name__ == "__main__":
    unittest.main()
