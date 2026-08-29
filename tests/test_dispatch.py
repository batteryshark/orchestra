"""Dispatch policy, core half: the pause switch, isolation defaults, and
how a dispatch names its project and checkout (schema v29).

The claim/order/dependency flow against a work source lives with the
work-bridge project and its tests.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from orchestra import cli, config, db, dispatch, http, project, supervise

CONFIG = """\
[settings]
timeout = 60

[profiles.stub]
backend = "opencode"
"""


class ManualIsolationTests(unittest.TestCase):
    def parsed_worktree(self, *extra: str) -> bool:
        seen = []
        with mock.patch.object(sys, "argv", ["orchestra", "dispatch", "--to",
                                             "stub", *extra, "inspect"]), \
                mock.patch.object(cli, "cmd_dispatch",
                                  side_effect=lambda args: seen.append(args.worktree)):
            cli.main()
        return seen[0]

    def test_manual_dispatch_defaults_isolated_with_an_explicit_shared_mode(self) -> None:
        self.assertTrue(self.parsed_worktree())
        self.assertTrue(self.parsed_worktree("--worktree"))
        self.assertFalse(self.parsed_worktree("--shared"))


class PauseSwitchTests(unittest.TestCase):
    """One switch, one format. There used to be two implementations writing
    the same meta key -- dispatch.py a JSON object, http.py a bare "1"/"0" --
    and the moment anyone pressed Resume in the dashboard or the phone, the
    "0" it left parsed as the int 0 and dispatch.pause_state called .get on
    it. That raised on EVERY daemon tick, so the daemon quietly stopped
    dispatching and observing until someone read stderr."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {"ORCHESTRA_HOME": self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.con = db.connect()
        self.addCleanup(self.con.close)

    def test_the_http_route_and_the_module_agree(self) -> None:
        self.assertFalse(dispatch.paused(self.con))

        http.set_dispatch_paused(self.con, True)
        self.assertTrue(dispatch.paused(self.con))
        self.assertTrue(http.dispatch_paused(self.con))
        self.assertIsNotNone(dispatch.pause_state(self.con)["at"])
        self.assertIsNotNone(http.pause_state(self.con)["since"])

        http.set_dispatch_paused(self.con, False)
        self.assertFalse(dispatch.paused(self.con))
        self.assertFalse(http.dispatch_paused(self.con))
        # The bug: this call is what raised, on every tick, forever.
        self.assertIsNone(dispatch.pause_state(self.con))

    def test_a_legacy_flag_left_in_the_key_is_read_not_fatal(self) -> None:
        for raw, expected in (("0", False), ("1", True), ("", False),
                              ("false", False), ("garbage", True)):
            db.meta_set(self.con, dispatch.PAUSE_KEY, raw)
            self.con.commit()
            self.assertEqual(dispatch.paused(self.con), expected, raw)


class CoreDispatchFixture(unittest.TestCase):
    """A registered identity, a real checkout, and NO work source anywhere."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name).resolve()
        self.root = self.tmp_path / "checkout"
        self.root.mkdir()
        self.config_path = self.tmp_path / "config.toml"
        self.config_path.write_text(CONFIG)
        self.env = mock.patch.dict(os.environ, {
            "ORCHESTRA_HOME": str(self.tmp_path / "home"),
            "ORCHESTRA_CONFIG": str(self.config_path)})
        self.env.start()
        self.addCleanup(self.env.stop)
        con = db.connect()
        self.proj = project.create(con, "Demo")
        con.close()

    def _args(self, **over):
        base = dict(mission=["do the thing"], to="stub", after=None,
                    brief_file=None, context=None, title=None, worktree=False,
                    sync=False, project="demo", path=None)
        base.update(over)
        return Namespace(**base)

    def _last_run(self):
        con = db.connect()
        run = con.execute("SELECT * FROM runs ORDER BY id DESC").fetchone()
        con.close()
        return run


class NamedProjectDispatchTests(CoreDispatchFixture):
    def test_the_first_dispatch_names_a_path_and_the_history_remembers(self) -> None:
        # No run history yet: a named project needs --path exactly once.
        with self.assertRaisesRegex(SystemExit, "no known checkout"):
            cli.cmd_dispatch(self._args())
        with mock.patch.object(supervise, "spawn_supervisor"):
            cli.cmd_dispatch(self._args(path=str(self.root)))
            first = self._last_run()
            self.assertEqual(first["repo"], str(self.root))
            # The history now answers, so the flag is no longer needed.
            cli.cmd_dispatch(self._args())
        again = self._last_run()
        self.assertEqual(again["workdir"], str(self.root))
        self.assertEqual(again["project_id"], self.proj.project_id)
        # Artifacts file under the project by ITS run number.
        home = Path(os.environ["ORCHESTRA_HOME"])
        base = home / "projects" / "demo" / "runs" / f"run-{again['project_seq']}"
        self.assertEqual(Path(again["brief_path"]), base / "brief.md")

    def test_a_bare_dispatch_resolves_the_project_from_the_history(self) -> None:
        with mock.patch.object(supervise, "spawn_supervisor"):
            cli.cmd_dispatch(self._args(path=str(self.root)))
        args = self._args(project=None, path=None)
        with mock.patch.object(project, "start_dir", return_value=self.root), \
                mock.patch.object(supervise, "spawn_supervisor"):
            cli.cmd_dispatch(args)
        run = self._last_run()
        self.assertEqual(run["project_id"], self.proj.project_id)
        self.assertEqual(run["workdir"], str(self.root))

    def test_misses_are_named(self) -> None:
        with self.assertRaisesRegex(SystemExit, "no project matches"):
            cli.cmd_dispatch(self._args(project="no-such"))
        with self.assertRaisesRegex(SystemExit, "is not a directory"):
            cli.cmd_dispatch(self._args(path=str(self.tmp_path / "nope")))


class RunPathTests(CoreDispatchFixture):
    """--path names the repository ONE run branches from and lands into,
    while its artifacts still file under the project's slug."""

    def _repo(self, name: str) -> Path:
        repo = self.tmp_path / name
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "a.txt").write_text("a\n")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t",
                        "-c", "user.name=t", "commit", "-qm", "init"],
                       check=True)
        return repo

    def test_a_run_branches_from_the_named_checkout(self) -> None:
        other = self._repo("second-checkout")
        with mock.patch.object(supervise, "spawn_supervisor"):
            cli.cmd_dispatch(self._args(path=str(other), worktree=True))
        run = self._last_run()
        self.assertEqual(run["repo"], str(other))
        branches = subprocess.run(
            ["git", "-C", str(other), "branch", "--list", run["branch"]],
            capture_output=True, text=True, check=True).stdout
        self.assertIn(run["branch"], branches)
        home = Path(os.environ["ORCHESTRA_HOME"])
        self.assertTrue(Path(run["workdir"]).is_relative_to(
            home / "projects" / "demo" / "worktrees"))
        con = db.connect()
        self.assertEqual(project.root_for(con, run), other)
        con.close()

    def test_http_dispatch_takes_the_same_path(self) -> None:
        launched = []
        first = http.dispatch_run(
            {"project": "demo", "profile": "stub", "mission": "do",
             "worktree": False, "path": str(self.root)},
            launcher=lambda root, run_id: launched.append((root, run_id)))
        self.assertNotIn("error", first)
        # The next dispatch needs no path: the run history remembers.
        again = http.dispatch_run(
            {"project": "demo", "profile": "stub", "mission": "more",
             "worktree": False},
            launcher=lambda root, run_id: launched.append((root, run_id)))
        self.assertNotIn("error", again)
        self.assertEqual(launched[-1][0], self.root)

    def test_refusals_are_payloads(self) -> None:
        noop = lambda root, run_id: None
        self.assertIn("needs project, profile, mission",
                      http.dispatch_run({}, launcher=noop)["error"])
        self.assertIn("no project matches",
                      http.dispatch_run({"project": "nope", "profile": "stub",
                                         "mission": "m"},
                                        launcher=noop)["error"])
        self.assertIn("no run history",
                      http.dispatch_run({"project": "demo", "profile": "stub",
                                         "mission": "m"},
                                        launcher=noop)["error"])


class StateDirGuardTests(CoreDispatchFixture):
    """A caller-named path inside ~/.orchestra is refused: a worktree of the
    run database is never what anyone meant. A project's own workspace is
    the one legitimate in-home place a run stands."""

    def test_the_state_directory_is_refused_on_both_surfaces(self) -> None:
        home = Path(os.environ["ORCHESTRA_HOME"])
        (home / "logs").mkdir(parents=True, exist_ok=True)
        with self.assertRaisesRegex(SystemExit, "own state directory"):
            cli.cmd_dispatch(self._args(path=str(home / "logs")))
        said = http.dispatch_run(
            {"project": "demo", "profile": "stub", "mission": "m",
             "path": str(home)}, launcher=lambda root, run_id: None)
        self.assertIn("own state directory", said["error"])
        from orchestra import paths as mpaths
        ws = mpaths.workspace_dir("demo")
        self.assertEqual(project.guard_run_path(ws), ws.resolve())


if __name__ == "__main__":
    unittest.main()
