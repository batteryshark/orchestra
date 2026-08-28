"""Central paths and local or Work-backed project resolution (DESIGN §2)."""
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestra import config, db, paths, project, sweeper
from tests.fake_work import FakeWork

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
        self.assertEqual(paths.worktrees_dir("demo"),
                         self.home / "worktrees" / "demo")

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

    def test_worktree_dir_is_keyed_by_the_immutable_project_id(self) -> None:
        """The Work id is mutable, so renaming a project would strand its
        worktree directory; the projectId UUID never changes."""
        uuid = "53efe3c3-6def-4797-8560-3dce073d7d63"
        self.assertEqual(paths.worktrees_dir(uuid),
                         self.home / "worktrees" / uuid)
        self.assertTrue(paths.worktrees_dir(uuid).is_dir())

    def test_home_default_is_dot_orchestra_in_the_user_home(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            del os.environ["ORCHESTRA_HOME"]
            self.assertEqual(paths.home(), Path("~/.orchestra").expanduser())


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
            "ORCHESTRA_HOME": str(self.tmp_path / "home"),
            "ORCHESTRA_CONFIG": str(self.global_config)})
        self.env.start()
        self.con = db.connect()

    def tearDown(self) -> None:
        self.con.close()
        self.env.stop()
        self.tmp.cleanup()

    def seed(self, entries=None):
        return sweeper.remember_projects(self.con, str(self.workspace), entries or [
            {"projectId": DEMO_ID, "id": "demo", "name": "Demo", "path": "demo"}])

    def resolve(self, where):
        return project.resolve(self.con, {}, str(where))

    def resolve_live(self, cfg, where):
        """As the CLI resolves: the adapter's refresher warms a cold cache."""
        return project.resolve(self.con, cfg, str(where),
                               refresh=sweeper.refresh_projects)

    def test_a_moved_project_leaves_no_stale_work_row(self) -> None:
        """A worktree copy that briefly won discovery cached a wrong path, and
        the ghost outlived the Work-side fix by days (I-0013's afterlife):
        refresh only ever upserted, never pruned. A Work-sourced path Work no
        longer names must go; a locally adopted row must survive every prune."""
        self.seed([{"projectId": DEMO_ID, "id": "demo", "name": "Demo",
                    "path": "demo/inner"}])          # the wrong (stale) path
        project.adopt(self.con, self.workspace / "demo" / "src", "Mine")
        self.seed([{"projectId": DEMO_ID, "id": "demo", "name": "Demo",
                    "path": "demo"}])                # Work now names the truth
        paths_cached = [r["path"] for r in self.con.execute(
            "SELECT path FROM projects ORDER BY path")]
        self.assertIn(str(self.workspace / "demo"), paths_cached)
        self.assertNotIn(str(self.workspace / "demo" / "inner"), paths_cached,
                         "the stale Work-sourced path must be pruned")
        self.assertIn(str(self.workspace / "demo" / "src"), paths_cached,
                      "a locally adopted row is never Work's to delete")

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
        self.assertIn("not inside a registered project", str(ctx.exception))
        self.assertIn("orchestra project add .", str(ctx.exception))

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
            hit = self.resolve_live(cfg, self.workspace / "demo" / "src")
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

    def test_work_ref_project_path_maps_to_the_project_id(self) -> None:
        self.seed()
        self.assertEqual(project.by_source_ref(self.con, "demo").project_id, DEMO_ID)
        self.assertEqual(
            project.by_source_ref(self.con, str(self.workspace / "demo")).project_id,
            DEMO_ID)
        self.assertIsNone(project.by_source_ref(self.con, "no-such-project"))
        self.assertIsNone(project.by_source_ref(self.con, None))

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
            "ORCHESTRA_HOME": str(self.tmp_path / "home"),
            "ORCHESTRA_CONFIG": str(self.tmp_path / "global.toml"),
            "ORCHESTRA_LAUNCH_AGENTS": str(self.tmp_path / "LaunchAgents"),
            "ORCHESTRA_ROOT": str(self.project_dir)})
        self.env.start()
        con = db.connect()
        sweeper.remember_projects(con, str(self.workspace),
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
        from orchestra import cli
        with mock.patch("orchestra.cli.Path.cwd", return_value=self.project_dir):
            text = self._run(cli.cmd_init)
        self.assertEqual(list(self.project_dir.iterdir()), [self.project_dir / ".git"])
        self.assertFalse((self.project_dir / ".orchestra").exists())
        self.assertIn(str(paths.home()), text)
        self.assertIn(DEMO_ID, text)

    def test_runs_lists_every_project_and_here_narrows_it(self) -> None:
        from orchestra import cli
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
        from orchestra import cli
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
        self.env = mock.patch.dict(os.environ, {"ORCHESTRA_HOME": str(self.root / "home")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.con = db.connect()
        self.addCleanup(self.con.close)
        self.repo = self.root / "repo"
        self.repo.mkdir()

    def test_an_adopted_directory_resolves(self) -> None:
        adopted = project.adopt(self.con, self.repo)
        self.assertEqual(self.repo, Path(adopted.path))
        self.assertIsNone(adopted.source_ref)
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
        sweeper.remember_projects(self.con, str(self.root),
                         [{"projectId": "w-1", "id": 7, "name": "theirs",
                           "path": "from-work"}])
        paths = {str(p.path) for p in project.all_projects(self.con)}
        self.assertIn(str(self.repo), paths)
        self.assertEqual(adopted.project_id,
                         project.resolve(self.con, {}, str(self.repo)).project_id)

    def test_work_wins_when_it_names_the_same_directory(self) -> None:
        project.adopt(self.con, self.repo)
        sweeper.remember_projects(self.con, str(self.root),
                         [{"projectId": "w-1", "id": 7, "name": "theirs",
                           "path": "repo"}])
        found = project.resolve(self.con, {}, str(self.repo))
        self.assertEqual("w-1", found.project_id)
        # source_ref is a TEXT column, so an integer id round-trips as a string.
        self.assertEqual("7", str(found.source_ref))

    def test_the_source_owns_the_archived_flag_and_a_refresh_flips_it(self) -> None:
        """DESIGN §1: parking a project at the source parks it here, with no
        local action — and unparking it there brings it back the same way.
        The core reads a plain boolean and never asks who set it (CONTRACT §7);
        only the adapter's ``remember_projects`` writes it."""
        def refresh(**flag):
            sweeper.remember_projects(self.con, str(self.root),
                             [{"projectId": "w-1", "id": 7, "name": "theirs",
                               "path": "repo", **flag}])
            return project.resolve(self.con, {}, str(self.repo)).archived

        self.assertFalse(refresh(archived=False))
        self.assertTrue(refresh(archived=True))
        # Parked at the source HIDES it, exactly as a local archive does; the
        # 2026-08-27 bill CONTRACT 0.10 names was this flag never arriving.
        self.assertEqual([], project.all_projects(self.con))
        self.assertEqual([self.repo], [p.path for p in project.all_projects(
            self.con, include_archived=True)])
        self.assertFalse(refresh(archived=False), "back on the source's word alone")
        self.assertEqual([self.repo], [p.path for p in project.all_projects(self.con)])
        self.assertTrue(refresh(archived=True))
        # An older source that serves no flag at all is not an archived project.
        self.assertFalse(refresh())

    def test_an_archived_project_is_off_the_list_until_all_asks(self) -> None:
        project.adopt(self.con, self.repo)
        project.set_archived(self.con, self.repo, True)
        self.assertEqual([], project.all_projects(self.con))
        listed = project.all_projects(self.con, include_archived=True)
        self.assertEqual([self.repo], [p.path for p in listed])
        self.assertTrue(listed[0].archived)
        # It still RESOLVES: a directory is addressable whether parked or not,
        # which is what keeps manual dispatch and every history read working.
        self.assertTrue(project.resolve(self.con, {}, str(self.repo)).archived)
        project.set_archived(self.con, self.repo, False)
        self.assertEqual([self.repo], [p.path for p in project.all_projects(self.con)])

    def test_a_source_backed_project_archives_and_unarchives_here(self) -> None:
        """DESIGN §1: archiving means "hide this from Orchestra", which is
        Orchestra's own decision about its own surface. Every project takes
        it, source-backed or not, and it is never refused."""
        sweeper.remember_projects(self.con, str(self.root),
                         [{"projectId": "w-1", "id": 7, "name": "theirs",
                           "path": "repo"}])
        self.assertTrue(project.set_archived(self.con, self.repo, True))
        self.assertTrue(project.resolve(self.con, {}, str(self.repo)).archived)
        self.assertEqual([], project.all_projects(self.con))
        self.assertTrue(project.set_archived(self.con, self.repo, False))
        self.assertEqual([self.repo],
                         [p.path for p in project.all_projects(self.con)])
        self.assertFalse(project.set_archived(self.con, self.root / "nowhere", True))

    def test_the_owners_override_survives_a_refresh_that_says_otherwise(self) -> None:
        """The human decided HERE, so the human wins: the adapter keeps
        mirroring the source into ``archived`` and the override sits above it,
        in both directions."""
        def refresh(**flag):
            sweeper.remember_projects(self.con, str(self.root),
                             [{"projectId": "w-1", "id": 7, "name": "theirs",
                               "path": "repo", **flag}])
            return project.resolve(self.con, {}, str(self.repo))

        refresh(archived=False)
        project.set_archived(self.con, self.repo, True)
        self.assertTrue(refresh(archived=False).archived,
                        "a refresh undid the owner's parking")
        # The source's own mirror still tracks the source underneath it.
        self.assertEqual(0, self.con.execute(
            "SELECT archived FROM projects WHERE path=?",
            (str(self.repo),)).fetchone()["archived"])
        # And the other way: the owner un-parks what the source parked.
        project.set_archived(self.con, self.repo, False)
        hit = refresh(archived=True)
        self.assertFalse(hit.archived, "a refresh re-parked an unparked project")
        self.assertIs(False, hit.archived_override)

    def test_a_pre_existing_archived_row_still_parks_with_no_override(self) -> None:
        """The column is a PURE ADD and no migration runs: a row archived
        before v26 carries a NULL override, so the COALESCE keeps parking it
        until the owner toggles it."""
        project.adopt(self.con, self.repo)
        self.con.execute("UPDATE projects SET archived=1 WHERE path=?",
                         (str(self.repo),))
        self.con.commit()
        hit = project.resolve(self.con, {}, str(self.repo))
        self.assertTrue(hit.archived)
        self.assertIsNone(hit.archived_override)
        self.assertEqual([], project.all_projects(self.con))
        project.set_archived(self.con, self.repo, False)
        self.assertEqual([self.repo],
                         [p.path for p in project.all_projects(self.con)])

    def test_forget_drops_a_local_project_but_refuses_one_from_work(self) -> None:
        project.adopt(self.con, self.repo)
        self.assertTrue(project.forget(self.con, self.repo))
        self.assertEqual([], project.all_projects(self.con))
        self.assertFalse(project.forget(self.con, self.repo))

        sweeper.remember_projects(self.con, str(self.root),
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


class StoreOnlyProjectTests(unittest.TestCase):
    """W-0312: Work's project path is ORGANIZATIONAL, not a directory.

    Since the store-only reset a Work project is a record — `projects.mjs`
    hardcodes empty aliasPaths because a store project has no filesystem
    markers. Orchestra still derived a checkout from that path, so grouping
    the two tools under "Agentic Engineering" pointed every dispatch at a
    directory that never existed: I-0302's run 59 died on the worktree
    guard, and its retry spawned a supervisor with a missing cwd that
    vanished before writing a byte.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.env = mock.patch.dict(os.environ,
                                   {"ORCHESTRA_HOME": str(self.root / "home")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.con = db.connect()
        self.addCleanup(self.con.close)
        # Exactly what Work serves: a grouped path, no aliases, no directory.
        sweeper.remember_projects(self.con, str(self.root),
                         [{"projectId": "p-orch", "id": "Group/orchestra",
                           "name": "Orchestra", "path": "Group/orchestra"}])

    def test_a_project_with_no_directory_gets_an_ephemeral_workspace(self) -> None:
        """No checkout anywhere: the run still gets somewhere to work, keyed
        by the project id so a second pass sees the first one's files."""
        proj = project.by_source_ref(self.con, "Group/orchestra")
        self.assertTrue(proj.path.is_dir(), "the run has somewhere to work")
        self.assertEqual(0o700, proj.path.stat().st_mode & 0o777,
                         "owner-only, like every directory paths hands out")
        self.assertEqual(proj.path,
                         project.by_source_ref(self.con, "Group/orchestra").path,
                         "the same project comes back to the same workspace")
        self.assertEqual("p-orch", proj.project_id,
                         "Work's id survives, so settings and writeback line up")

    def test_the_folder_is_found_without_anyone_naming_it(self) -> None:
        """Grouping is Work's business; the folder keeps its own name. The
        moment the two tools moved under "Agentic Engineering", the checkout
        stayed at ~/Projects/orchestra — Orchestra can see that, so no human
        should have to tell it."""
        repo = self.root / "orchestra"
        repo.mkdir()
        found = project.by_source_ref(self.con, "Group/orchestra")
        self.assertEqual(repo, found.path)
        self.assertEqual("p-orch", found.project_id)

    def test_a_folder_another_project_claims_is_not_borrowed(self) -> None:
        """Two projects named the same under different groups must not
        collapse onto one checkout: an ambiguous hit is declined, and the
        workspace answers instead."""
        shared = self.root / "docs"
        shared.mkdir()
        sweeper.remember_projects(self.con, str(self.root),
                         [{"projectId": "p-orch", "id": "Group/docs",
                           "name": "Ours", "path": "Group/docs"},
                          {"projectId": "p-other", "id": "docs",
                           "name": "Theirs", "path": "docs"}])
        found = project.by_source_ref(self.con, "Group/docs")
        self.assertEqual(paths.workspace_dir("p-orch"), found.path)
        self.assertEqual(shared, project.by_source_ref(self.con, "docs").path,
                         "the project that owns the folder still gets it")

    def test_an_ungrouped_project_with_no_folder_gets_a_workspace(self) -> None:
        """Nothing to strip: a flat path that names no directory is a
        store-only project, not a misplaced checkout."""
        sweeper.remember_projects(self.con, str(self.root),
                         [{"projectId": "p-flat", "id": "errands",
                           "name": "Errands", "path": "errands"}])
        self.assertEqual(paths.workspace_dir("p-flat"),
                         project.by_source_ref(self.con, "errands").path)

    def test_a_linked_checkout_answers_for_the_work_path(self) -> None:
        """The repository Work cannot name: link it once, and every dispatch
        for that project lands in it — with Work's own project id intact, so
        per-project settings and the item's writeback still line up."""
        (self.root / "orchestra").mkdir()   # what discovery would find
        elsewhere = self.root / "checkouts" / "orch"
        elsewhere.mkdir(parents=True)
        linked = project.link(self.con, "Group/orchestra", elsewhere)
        self.assertEqual("p-orch", linked.project_id)
        found = project.by_source_ref(self.con, "Group/orchestra")
        self.assertEqual(elsewhere, found.path,
                         "an explicit binding outranks a lucky name match")

    def test_a_refresh_keeps_the_link(self) -> None:
        """Work re-serves its list every sweep. The bound checkout is
        Orchestra's own row, so neither the replace nor the stale-row prune
        may touch it — otherwise the fix would last one pass."""
        repo = self.root / "orchestra"
        repo.mkdir()
        project.link(self.con, "Group/orchestra", repo)
        sweeper.remember_projects(self.con, str(self.root),
                         [{"projectId": "p-orch", "id": "Group/orchestra",
                           "name": "Orchestra", "path": "Group/orchestra"}])
        self.assertEqual(repo, project.by_source_ref(self.con,
                                                    "Group/orchestra").path)

    def test_a_real_directory_still_wins_untouched(self) -> None:
        """The common case must not move: a Work path that IS a directory is
        used exactly as before, with no workspace and no link involved."""
        real = self.root / "plain"
        real.mkdir()
        sweeper.remember_projects(self.con, str(self.root),
                         [{"projectId": "p-plain", "id": "plain",
                           "name": "Plain", "path": "plain"}])
        self.assertEqual(real, project.by_source_ref(self.con, "plain").path)

    def test_a_link_whose_directory_is_gone_fails_loudly(self) -> None:
        """An unmounted volume must not become an empty workspace: linking is
        how a checkout is claimed, and a claim that stops resolving is an
        error a human fixes, not a blank directory an agent fills."""
        repo = self.root / "on-a-volume"
        repo.mkdir()
        project.link(self.con, "Group/orchestra", repo)
        repo.rmdir()  # the volume goes away
        found = project.by_source_ref(self.con, "Group/orchestra")
        self.assertEqual(repo, found.path)
        self.assertFalse(found.path.exists(),
                         "no workspace is substituted for a claimed checkout")

    def test_link_refuses_a_project_it_cannot_find(self) -> None:
        repo = self.root / "orchestra"
        repo.mkdir()
        with self.assertRaises(SystemExit):
            project.link(self.con, "no/such/project", repo)
