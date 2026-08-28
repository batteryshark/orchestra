"""Dispatch policy (DESIGN §4, W-0164): no caps, order, honest queue state,
the pause switch.

The headline is a negative and it opens its own test: fifteen delegated
items in one project dispatch in ONE pass, with no cap and no stagger. The
rest cover what only matters for items that must wait.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestra import cli, db, dispatch, supervise
from tests.test_sweeper import PROJECT_ID, SweeperFixture


class NoCapsTests(SweeperFixture, unittest.TestCase):
    def test_fifteen_items_dispatch_in_one_pass_and_live_runs_never_gate(self) -> None:
        """Fifteen delegated items in ONE pass, no cap, no stagger; then a
        second pass adds more runs beside the live ones -- no global ceiling,
        live runs never gate the next dispatch."""
        for n in range(15):
            self.work.add_task(f"W-{n:04d}", f"job {n}", delegated=True)
        actions = self.sweep()
        self.assertEqual([a["action"] for a in actions], ["dispatch"] * 15)
        # Every one of them actually launched, in one pass, same project.
        self.assertEqual(len(self.launched), 15)
        self.assertEqual({root for root, _ in self.launched}, {self.root})
        con = db.connect()
        live = dispatch.live_runs(con)
        waiting = dispatch.waiting(con)
        con.close()
        self.assertEqual(live, 15)
        self.assertEqual(waiting, [])  # nothing was made to wait for a slot
        self.assertEqual(
            {t["status"] for t in self.work.tasks.values()}, {"in_progress"})

        for n in range(15, 20):
            self.work.add_task(f"W-{n:04d}", f"job {n}", delegated=True)
        actions = self.sweep()
        self.assertEqual([a["action"] for a in actions], ["dispatch"] * 5)
        con = db.connect()
        self.assertEqual(dispatch.live_runs(con), 20)
        con.close()


class DependencyOrderTests(SweeperFixture, unittest.TestCase):
    def waiting(self) -> list[dict]:
        con = db.connect()
        rows = dispatch.waiting(con)
        con.close()
        return rows

    def test_dependency_holds_are_honest_logged_once_and_settle(self) -> None:
        """depends_on and blocked_by both hold with the blocker as the
        detail; holding is logged once, not once per pass; a settled
        dependency releases the item and its queue row clears at dispatch."""
        self.work.add_task("W-0001", "the prerequisite", status="in_progress")
        self.work.add_task("W-0002", "the dependent", delegated=True,
                           depends_on=["W-0001"])
        self.work.add_task("W-0003", "blocker", status="review")
        self.work.add_task("W-0004", "blocked", delegated=True,
                           blocked_by=["W-0003"])
        actions = self.sweep()
        self.assertEqual(
            [(a["action"], a["reason"], a["detail"]) for a in actions],
            [("hold", "dependency", "W-0001"),
             ("hold", "dependency", "W-0003")])
        # Honest queue state: they wait, so NOT in_progress and no runs.
        self.assertEqual(self.work.tasks["W-0002"]["status"], "ready")
        self.assertIsNone(self.db_run())
        self.assertEqual(self.launched, [])
        self.assertEqual(
            [(r["item_id"], r["reason"], r["detail"]) for r in self.waiting()],
            [("W-0002", "dependency", "W-0001"),
             ("W-0004", "dependency", "W-0003")])
        self.assertEqual(self.sweep(), [])  # logged once, not once per pass
        self.assertEqual(len(self.waiting()), 2)
        self.work.human_move("W-0001", "done")
        actions = self.sweep()
        self.assertEqual([a["action"] for a in actions], ["dispatch"])
        self.assertEqual(self.db_run()["ref"], "W-0002")
        # W-0002's queue row cleared at dispatch; the other still waits.
        self.assertEqual([r["item_id"] for r in self.waiting()], ["W-0004"])

    def test_an_open_issue_dependency_holds_until_the_issue_closes(self) -> None:
        # Work's semantics: an issue in dependsOn is settled only when its
        # state is closed (resolved folded into closed, 2026-08-14).
        self.work.add_issue("I-0001", "the prerequisite issue", state="queued")
        self.work.add_task("W-0001", "the dependent", delegated=True,
                           depends_on=["I-0001"])
        actions = self.sweep()
        self.assertEqual([a["action"] for a in actions], ["hold"])
        self.assertEqual(actions[0]["reason"], "dependency")
        self.assertEqual(actions[0]["detail"], "I-0001")
        # Honest queue state: it waits, so it is NOT in_progress and has no run.
        self.assertEqual(self.work.tasks["W-0001"]["status"], "ready")
        self.assertIsNone(self.db_run())
        self.work.human_close_issue("I-0001", summary="settled")
        actions = self.sweep()
        self.assertEqual([a["action"] for a in actions], ["dispatch"])
        self.assertEqual(self.db_run()["ref"], "W-0001")
        self.assertEqual(self.waiting(), [])  # queue row cleared at dispatch

    def test_board_order_decides_and_dependencies_outrank_it(self) -> None:
        # Work has no priority field: the lane's order is the whole signal.
        con = db.connect()
        dispatch.pause(con, "hold everything")
        con.close()
        for name in ("W-0001", "W-0002", "W-0003"):
            self.work.add_task(name, f"job {name}", delegated=True)
        self.sweep()
        self.assertEqual([r["item_id"] for r in self.waiting()],
                         ["W-0001", "W-0002", "W-0003"])
        # The human reorders the lane from the phone, then lets dispatch go.
        self.work.reorder_lane("W-0003", "W-0001", "W-0002")
        con = db.connect()
        dispatch.resume(con)
        con.close()
        actions = self.sweep()
        self.assertEqual([a["item"] for a in actions],
                         ["W-0003", "W-0001", "W-0002"])

        self.work.add_task("W-0004", "prerequisite", status="in_progress")
        self.work.add_task("W-0005", "blocked, but first on the board",
                           delegated=True, depends_on=["W-0004"])
        self.work.add_task("W-0006", "ready, second on the board", delegated=True)
        actions = self.sweep()
        # The ready one goes; the blocked one waits despite being higher.
        self.assertEqual([(a["action"], a["item"]) for a in actions],
                         [("dispatch", "W-0006"), ("hold", "W-0005")])


class AdmissionPauseTests(SweeperFixture, unittest.TestCase):
    def insert_run(self, con, run_id: int, status: str, *, depends_on=None,
                   mission=None, use_worktree=0, **cols) -> None:
        """Seed a run row directly, plus its dependency edge and deferred
        dispatch when given."""
        cols = {"profile": "stub", "backend": "opencode",
                "requested_by": "human", "workdir": str(self.root),
                "status": status, "started_at": db.now(), **cols}
        con.execute(
            f"INSERT INTO runs(id, {', '.join(cols)}) "
            f"VALUES(?, {', '.join('?' * len(cols))})",
            (run_id, *cols.values()))
        if depends_on is not None:
            con.execute("INSERT INTO dispatch_dependencies(run_id, "
                        "depends_on_run) VALUES(?, ?)", (run_id, depends_on))
        if mission is not None:
            con.execute("INSERT INTO deferred_dispatches(run_id, mission, "
                        "use_worktree, created_at) VALUES(?, ?, ?, ?)",
                        (run_id, mission, use_worktree, db.now()))

    def restart(self) -> bool:
        """A daemon restart holds nothing in memory, so the switch has to
        come back out of the database on a fresh connection."""
        con = db.connect()
        try:
            return dispatch.paused(con)
        finally:
            con.close()

    def test_pause_survives_restart_blocks_dispatch_and_resume_releases(self) -> None:
        con = db.connect()
        dispatch.pause(con, "provider is flaky")
        con.close()
        self.assertTrue(self.restart())
        self.work.add_task("W-0001", "would have run", delegated=True)
        actions = self.sweep()
        self.assertEqual(
            [(a["action"], a["reason"], a["detail"]) for a in actions],
            [("hold", "paused", "provider is flaky")])
        # Not started, and not claiming to be started.
        self.assertIsNone(self.db_run())
        self.assertEqual(self.work.tasks["W-0001"]["status"], "ready")
        self.assertEqual(self.launched, [])

        con = db.connect()
        self.assertIsNotNone(dispatch.resume(con))
        # A second resume on an unpaused switch is a no-op.
        self.assertIsNone(dispatch.resume(con))
        self.assertFalse(dispatch.paused(con))
        con.close()
        actions = self.sweep()
        self.assertEqual([a["action"] for a in actions], ["dispatch"])
        self.assertEqual(self.work.tasks["W-0001"]["status"], "in_progress")

    def test_pause_leaves_in_flight_runs_completely_alone(self) -> None:
        self.work.add_task("W-0001", "already running", delegated=True)
        self.sweep()
        run = self.db_run()
        con = db.connect()
        dispatch.pause(con)
        self.assertEqual(dispatch.live_runs(con), 1)  # counted, not touched
        con.close()
        self.work.add_task("W-0002", "arrives during the pause", delegated=True)
        self.sweep()
        after = self.db_run(run["id"])
        self.assertEqual(after["status"], run["status"])
        self.assertIsNone(after["finished_at"])
        # The live run keeps being serviced: a human comment still ferries.
        self.finish_run(run["id"], status="running", summary=None,
                        session_ref="sess-1")
        self.work.human_log("W-0001", "one more thing")
        self.assertEqual([a["action"] for a in self.sweep()], ["ferry"])

    def test_manual_dispatch_refuses_while_paused(self) -> None:
        con = db.connect()
        dispatch.pause(con, "quota")
        with self.assertRaises(SystemExit) as caught:
            cli._gate_dispatch(con, self.cfg, "human")
        dispatch.resume(con)
        con.close()
        self.assertIn("paused", str(caught.exception))
        self.assertIn("orchestra resume", str(caught.exception))

        # The pause wins while this admission is waiting on SQLite's write
        # lock. Checking before BEGIN would read the old unpaused value and
        # incorrectly insert after the pause commits.
        def admit(worker):
            return supervise.create_run(
                worker, profile="stub", backend="opencode",
                requested_by="human", workdir=str(self.root),
                project_id=PROJECT_ID)

        outcome, = self.race_admissions(
            [admit], locked_write=lambda blocker: db.meta_set(
                blocker, dispatch.PAUSE_KEY, "1"))
        self.assertEqual(outcome, (None, "paused"))
        self.assertIsNone(self.db_run())

        # Admission never takes ownership of a caller's transaction. The old
        # duplicated inserts committed implicitly; the common boundary must
        # refuse instead of making unrelated writes durable.
        con = db.connect()
        db.meta_set(con, "unrelated_admission_write", "still provisional")
        with self.assertRaisesRegex(RuntimeError, "clean database transaction"):
            admit(con)
        con.rollback()
        self.assertIsNone(db.meta_get(con, "unrelated_admission_write"))
        con.close()

    def test_manual_reply_obeys_pause_and_records_async_spawn_failure(self) -> None:
        self.work.add_task("W-0001", "finished conversation", delegated=True)
        self.sweep()
        run = self.db_run()
        self.finish_run(run["id"], session_ref="session-1")
        con = db.connect()
        dispatch.pause(con, "hold continuations")
        before = con.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"]
        con.close()
        with self.assertRaises(SystemExit) as caught, \
                mock.patch.object(supervise, "spawn_supervisor"):
            cli.cmd_reply(mock.Mock(run_id=run["id"], message=["continue"],
                                    sync=False))
        con = db.connect()
        after = con.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"]
        con.close()
        self.assertEqual(before, after)
        self.assertIn("paused", str(caught.exception))

        con = db.connect()
        dispatch.resume(con)
        con.close()
        with mock.patch.object(supervise, "spawn_supervisor",
                               side_effect=RuntimeError("supervisor absent")), \
                self.assertRaisesRegex(RuntimeError, "supervisor absent"):
            cli.cmd_reply(mock.Mock(run_id=run["id"], message=["continue"],
                                    sync=False))
        failed = self.db_run()
        self.assertNotEqual(failed["id"], run["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertIn("supervisor absent", failed["summary"])

    def test_deferred_release_waits_for_resume_and_cleans_failed_launch(self) -> None:
        for args in (("init", "-q"),
                     ("config", "user.email", "t@example.com"),
                     ("config", "user.name", "t")):
            subprocess.run(["git", *args], cwd=self.root, check=True)
        (self.root / "seed.txt").write_text("seed\n")
        subprocess.run(["git", "add", "seed.txt"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "seed"], cwd=self.root,
                       check=True)
        con = db.connect()
        self.insert_run(con, 1, "done", title="done one", project_id=PROJECT_ID)
        self.insert_run(con, 2, "pending", title="waiting",
                        project_id=PROJECT_ID, depends_on=1, mission="go",
                        use_worktree=1)
        con.commit()
        con.close()
        launched: list[int] = []

        def fail_spawn(_root, run_id):
            launched.append(run_id)
            raise RuntimeError("supervisor absent")

        outcome, = self.race_admissions(
            [lambda worker: supervise.admit_pending(worker, 2)],
            locked_write=lambda blocker: db.meta_set(
                blocker, dispatch.PAUSE_KEY, "1"))
        self.assertEqual(outcome, (None, "paused"))
        self.assertEqual(launched, [])
        con = db.connect()
        self.assertEqual(
            con.execute("SELECT status FROM runs WHERE id=2").fetchone()["status"],
            "pending")  # still pending, still honest
        dispatch.resume(con)
        released = supervise.process_ready(con, fail_spawn)
        run = con.execute("SELECT * FROM runs WHERE id=2").fetchone()
        deferred = con.execute(
            "SELECT * FROM deferred_dispatches WHERE run_id=2").fetchone()
        con.close()
        self.assertEqual([r["status"] for r in released], ["failed"])
        self.assertEqual(launched, [2])
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["workdir"], str(self.root))
        self.assertIsNone(run["branch"])
        self.assertIn("supervisor absent", run["summary"])
        self.assertEqual(deferred["status"], "failed")
        branches = subprocess.run(
            ["git", "branch", "--list", "orchestra/run-2"], cwd=self.root,
            check=True, capture_output=True, text=True).stdout
        self.assertEqual(branches.strip(), "")
        worktrees = subprocess.run(
            ["git", "worktree", "list", "--porcelain"], cwd=self.root,
            check=True, capture_output=True, text=True).stdout
        self.assertNotIn("/run-2", worktrees)

    def test_pause_still_settles_a_broken_dependency_chain(self) -> None:
        con = db.connect()
        self.insert_run(con, 1, "failed")
        self.insert_run(con, 2, "pending", depends_on=1, mission="go")
        con.commit()
        dispatch.pause(con, "no new work")
        launched = []
        settled = supervise.process_ready(con, lambda root, rid: launched.append(rid))
        self.assertEqual(settled, [{"run_id": 2, "status": "declined"}])
        self.assertEqual(launched, [])
        self.assertEqual(
            con.execute("SELECT status FROM runs WHERE id=2").fetchone()["status"],
            "failed")

        self.insert_run(con, 3, "done", branch="orchestra/run-3")
        self.insert_run(con, 4, "pending", depends_on=3,
                        mission="wait for landing")
        con.commit()
        self.assertEqual(supervise.process_ready(
            con, lambda root, rid: launched.append(rid)), [],
            "a done execution is not success until its branch lands")
        con.execute("UPDATE runs SET landing_status='failed' WHERE id=3")
        con.commit()
        self.assertEqual(supervise.process_ready(
            con, lambda root, rid: launched.append(rid)),
            [{"run_id": 4, "status": "declined"}])
        con.close()


class StateReportTests(SweeperFixture, unittest.TestCase):
    def test_state_reports_pause_live_count_and_reasons(self) -> None:
        self.work.add_task("W-0001", "running one", delegated=True)
        self.sweep()
        self.work.add_task("W-0002", "prerequisite", status="ready")
        self.work.add_task("W-0003", "dependent", delegated=True,
                           depends_on=["W-0002"])
        self.sweep()
        con = db.connect()
        state = dispatch.state(con)
        con.close()
        self.assertEqual(state["live_runs"], 1)
        self.assertFalse(state["paused"])
        self.assertEqual([(w["item_id"], w["reason"]) for w in state["waiting"]],
                         [("W-0003", "dependency")])


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
    sweeping, dispatching and observing until someone read stderr."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {"ORCHESTRA_HOME": self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.con = db.connect()
        self.addCleanup(self.con.close)

    def test_the_http_route_and_the_module_agree(self) -> None:
        from orchestra import http
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

if __name__ == "__main__":
    unittest.main()


class NamedProjectDispatchTests(SweeperFixture, unittest.TestCase):
    """`--project <slug>` and POST /api/dispatch: a caller NAMES the target
    project instead of standing in its directory."""

    def _args(self, **over):
        from argparse import Namespace
        base = dict(mission=["do the thing"], to="stub", after=None,
                    brief_file=None, context=None, title=None, worktree=False,
                    sync=False, project="demo", path=None)
        base.update(over)
        return Namespace(**base)

    def test_cli_dispatch_by_slug_needs_no_cwd(self) -> None:
        # The test process's cwd is nowhere near the fixture project.
        with mock.patch.object(supervise, "spawn_supervisor") as spawn:
            cli.cmd_dispatch(self._args())
        con = db.connect()
        run = con.execute("SELECT * FROM runs ORDER BY id DESC").fetchone()
        con.close()
        self.assertEqual(run["project_id"], PROJECT_ID)
        self.assertEqual(run["workdir"], str(self.root))
        spawn.assert_called_once()
        # Artifacts file under the project by ITS run number — the number a
        # human quotes from the board — not the global row id.
        home = Path(os.environ["ORCHESTRA_HOME"])
        base = home / "projects" / "demo" / "runs" / f"run-{run['project_seq']}"
        self.assertEqual(Path(run["brief_path"]), base / "brief.md")
        self.assertEqual(Path(run["log_path"]), base / "log.jsonl")
        self.assertTrue(base.joinpath("brief.md").is_file())

    def test_cli_dispatch_names_the_miss(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            cli.cmd_dispatch(self._args(project="no-such"))
        self.assertIn("no project matches 'no-such'", str(caught.exception))
        self.assertIn("orchestra project list", str(caught.exception))

    def _warm_registry(self) -> None:
        # The route answers from the registry cache, as the daemon's own
        # refresh keeps it; only the CLI carries the refresh-on-miss seam.
        from orchestra import sweeper
        con = db.connect()
        sweeper.refresh_projects(con, self.cfg)
        con.close()

    def test_http_dispatch_run_uses_the_same_path(self) -> None:
        from orchestra import http
        self._warm_registry()
        launched = []
        result = http.dispatch_run(
            {"project": "demo", "profile": "stub", "mission": "do the thing",
             "worktree": False},
            launcher=lambda root, run_id: launched.append((root, run_id)))
        self.assertNotIn("error", result)
        self.assertEqual(result["project"], "demo")
        con = db.connect()
        run = con.execute("SELECT * FROM runs WHERE id=?",
                          (result["run"],)).fetchone()
        con.close()
        self.assertEqual(run["project_id"], PROJECT_ID)
        self.assertEqual(launched, [(self.root, result["run"])])

    def test_http_dispatch_refusals_are_payloads(self) -> None:
        from orchestra import http
        self._warm_registry()
        noop = lambda root, run_id: None
        self.assertIn("needs project, profile, mission",
                      http.dispatch_run({}, launcher=noop)["error"])
        self.assertIn("no project matches",
                      http.dispatch_run({"project": "nope", "profile": "stub",
                                         "mission": "m"},
                                        launcher=noop)["error"])
        con = db.connect()
        dispatch.pause(con, "hold")
        con.close()
        try:
            result = http.dispatch_run({"project": "demo", "profile": "stub",
                                        "mission": "m"}, launcher=noop)
            self.assertIn("paused", result["error"])
        finally:
            con = db.connect()
            dispatch.resume(con)
            con.close()

    def test_http_add_project_registers_and_repeats(self) -> None:
        from orchestra import http
        fresh = self.workspace / "brand new"
        fresh.mkdir()
        made = http.add_project(str(fresh))
        self.assertEqual(made["slug"], "brand-new")
        again = http.add_project(str(fresh))
        self.assertEqual(again["project_id"], made["project_id"])
        self.assertIn("error", http.add_project(""))
        self.assertIn("error",
                      http.add_project(str(self.workspace / "missing")))


class RunPathTests(SweeperFixture, unittest.TestCase):
    """A project is not one checkout: --path names the repository ONE run
    branches from and lands into, while its artifacts still file under the
    project's slug."""

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

    def _args(self, **over):
        from argparse import Namespace
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

    def test_a_run_branches_from_the_named_checkout(self) -> None:
        other = self._repo("second-checkout")
        with mock.patch.object(supervise, "spawn_supervisor"):
            cli.cmd_dispatch(self._args(path=str(other), worktree=True))
        run = self._last_run()
        self.assertEqual(run["repo"], str(other))
        self.assertEqual(run["project_id"], PROJECT_ID)
        # The branch lives in the NAMED repo; the worktree files under the
        # project's slug like any other.
        branches = subprocess.run(
            ["git", "-C", str(other), "branch", "--list", run["branch"]],
            capture_output=True, text=True, check=True).stdout
        self.assertIn(run["branch"], branches)
        home = Path(os.environ["ORCHESTRA_HOME"])
        self.assertTrue(Path(run["workdir"]).is_relative_to(
            home / "projects" / "demo" / "worktrees"))
        # Landing must return to the same checkout.
        from orchestra import project
        con = db.connect()
        self.assertEqual(project.root_for(con, run), other)
        con.close()

    def test_a_bare_path_names_the_project_too(self) -> None:
        with mock.patch.object(supervise, "spawn_supervisor"):
            cli.cmd_dispatch(self._args(project=None, path=str(self.root)))
        run = self._last_run()
        self.assertEqual(run["project_id"], PROJECT_ID)
        self.assertEqual(run["workdir"], str(self.root))
        self.assertEqual(run["repo"], str(self.root))

    def test_a_missing_path_is_refused_by_name(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            cli.cmd_dispatch(self._args(path=str(self.tmp_path / "nope")))
        self.assertIn("is not a directory", str(caught.exception))
