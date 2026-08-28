"""The daemon loop and its launchd LaunchAgent (DESIGN §2).

Nothing here starts a daemon or talks to the real launchd: `launchctl` is
patched out and the plist is written under ORCHESTRA_LAUNCH_AGENTS.
"""
import io
import os
import plistlib
import signal
import sys
import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from orchestra import daemon, db, dispatch, proc, service, supervise, runway

PROJECT_ID = "53efe3c3-6def-4797-8560-3dce073d7d63"


class DaemonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name).resolve()
        (self.tmp_path / "global.toml").write_text("")
        self.env = mock.patch.dict(os.environ, {
            "ORCHESTRA_HOME": str(self.tmp_path / "home"),
            "ORCHESTRA_CONFIG": str(self.tmp_path / "global.toml")})
        self.env.start()
        self.con = db.connect()

        # daemon.tick() polls every provider for real, and two adapters now
        # shell out: Codex to its app server, Claude to a pseudo-terminal
        # running /usage. A daemon test must not reach the developer's own
        # tools, wait twenty seconds for one, or read their live quota.
        self.no_poll = mock.patch.object(runway, "poll_all", return_value=[])
        self.no_poll.start()
        self.addCleanup(self.no_poll.stop)

    def tearDown(self) -> None:
        self.con.close()
        self.env.stop()
        self.tmp.cleanup()

    def _run(self, status="running", supervisor_pid=None,
             supervisor_pid_identity=None) -> int:
        return int(self.con.execute(
            "INSERT INTO runs(profile, backend, requested_by, workdir, status, "
            "supervisor_pid, supervisor_pid_identity, project_id, started_at) "
            "VALUES('p','codex','human','/p',?,?,?,?,?)",
            (status, supervisor_pid, supervisor_pid_identity,
             PROJECT_ID, db.now())).lastrowid)

    def _set_worker(self, run_id: int, pid: int, identity=None) -> None:
        self.con.execute("UPDATE runs SET pid=?, pid_identity=? WHERE id=?",
                         (pid, identity, run_id))

    def _completions(self, run_id: int) -> int:
        return self.con.execute(
            "SELECT COUNT(*) FROM messages WHERE run_id=? AND kind='completion'",
            (run_id,)).fetchone()[0]

    def test_tick_reaps_a_run_whose_supervisor_vanished(self) -> None:
        # PID 1 exists (launchd) and is not ours -> alive; a free high pid is not.
        lifecycle = []
        recovered_pid = _free_pid()
        recovered = self._run(supervisor_pid=recovered_pid)
        self.con.execute(
            "UPDATE runs SET status='failed', finished_at=?, summary=? WHERE id=?",
            (db.now(), f"Supervisor process {recovered_pid} vanished.", recovered))
        daemon.observer.defer_retry(self.con, recovered)
        lifecycle.append(("defer", recovered, self.con.in_transaction, "failed"))
        self.con.commit()  # simulate a crash before result enrichment
        self.assertEqual(self._completions(recovered), 0)

        dead = self._run(supervisor_pid=_free_pid())
        stopped_worker_pid = 98001
        stopped_worker = self._run(supervisor_pid=_free_pid())
        stuck_worker_pid = 98002
        stuck_worker = self._run(supervisor_pid=_free_pid())
        reused_worker_pid = 98003
        reused_worker = self._run(supervisor_pid=_free_pid())
        legacy_worker_pid = 98004
        legacy_worker = self._run(supervisor_pid=_free_pid())
        self._set_worker(stopped_worker, stopped_worker_pid, "stopped-owner")
        self._set_worker(stuck_worker, stuck_worker_pid, "stuck-owner")
        self._set_worker(reused_worker, reused_worker_pid, "original-owner")
        self._set_worker(legacy_worker, legacy_worker_pid)
        alive_identity = proc.process_identity(os.getpid())
        alive = self._run(supervisor_pid=os.getpid(),
                          supervisor_pid_identity=alive_identity)
        stale = self._run(status="spawning")
        pending_claim = self._run(status="spawning")
        fresh = self._run(status="spawning")
        malformed = self._run(status="spawning")
        self.con.execute("UPDATE runs SET started_at='2000-01-01T00:00:00+00:00' "
                         "WHERE id=?", (stale,))
        self.con.execute(
            "UPDATE runs SET started_at='2000-01-01T00:00:00+00:00', "
            "work_claim_status='pending' WHERE id=?", (pending_claim,))
        self.con.execute("UPDATE runs SET started_at='not-a-time' WHERE id=?",
                         (malformed,))
        self.con.commit()
        real_defer = daemon.observer.defer_retry
        real_finalize = daemon.supervise.finalize_run
        real_signal_group = daemon.proc.signal_group
        worker_alive = {stopped_worker_pid: True, stuck_worker_pid: True,
                        reused_worker_pid: True, legacy_worker_pid: True}
        worker_identities = {stopped_worker_pid: "stopped-owner",
                             stuck_worker_pid: "stuck-owner",
                             reused_worker_pid: "new-unrelated-owner",
                             legacy_worker_pid: "legacy-process",
                             os.getpid(): alive_identity}

        def defer_retry(con, run_id):
            status = con.execute("SELECT status FROM runs WHERE id=?",
                                 (run_id,)).fetchone()["status"]
            lifecycle.append(("defer", run_id, con.in_transaction, status))
            return real_defer(con, run_id)

        def finalize_run(con, run, status, exit_code, **kwargs):
            lifecycle.append(("finalize", int(run["id"]), con.in_transaction,
                              status))
            return real_finalize(con, run, status, exit_code, **kwargs)

        def signal_group(pid, sig):
            if pid not in worker_alive:
                return real_signal_group(pid, sig)
            if not worker_alive[pid]:
                raise ProcessLookupError(pid)

        def terminate_group(pid, *, force=False):
            self.assertTrue(force)
            if pid == stopped_worker_pid:
                worker_alive[pid] = False

        diagnostics = io.StringIO()
        with mock.patch.object(daemon.observer, "defer_retry",
                               side_effect=defer_retry), \
                mock.patch.object(daemon.supervise, "finalize_run",
                                  side_effect=finalize_run), \
                mock.patch.object(daemon.supervise, "fail_launch",
                                  wraps=daemon.supervise.fail_launch) as failed_launches, \
                mock.patch.object(daemon.supervise,
                                  "spawn_supervisor") as spawned, \
                mock.patch.object(daemon.proc, "signal_group",
                                  side_effect=signal_group), \
                mock.patch.object(daemon.proc, "process_identity",
                                  side_effect=worker_identities.get), \
                mock.patch.object(daemon.proc, "terminate_group",
                                  side_effect=terminate_group) as terminated, \
                mock.patch.object(daemon.time, "sleep"), \
                mock.patch.object(daemon.sys, "stderr", diagnostics):
            report = daemon.tick()
        self.assertEqual(report["reaped"],
                         [recovered, dead, stopped_worker, stale, malformed])
        self.assertEqual(
            [(call.args[0], call.kwargs["force"])
             for call in terminated.call_args_list],
            [(stopped_worker_pid, True), (stuck_worker_pid, True)])
        self.assertIn(f"run {stuck_worker}", diagnostics.getvalue())
        self.assertIn(f"worker group {stuck_worker_pid}", diagnostics.getvalue())
        self.assertIn(f"run {reused_worker}", diagnostics.getvalue())
        self.assertIn("identity changed", diagnostics.getvalue())
        self.assertIn(f"run {legacy_worker}", diagnostics.getvalue())
        self.assertIn("no recorded process identity", diagnostics.getvalue())
        self.assertEqual(
            [event[1] for event in lifecycle if event[0] == "finalize"],
            report["reaped"])
        for run_id in report["reaped"]:
            deferred_at = next(i for i, event in enumerate(lifecycle)
                               if event[:2] == ("defer", run_id))
            finalized_at = next(i for i, event in enumerate(lifecycle)
                                if event[:2] == ("finalize", run_id))
            self.assertLess(deferred_at, finalized_at)
            self.assertEqual(lifecycle[deferred_at][2:], (True, "failed"))
            self.assertFalse(lifecycle[finalized_at][2],
                             "finalization must start after the reap commits")
            actions = [row["action"] for row in self.con.execute(
                "SELECT action FROM observations WHERE run_id=? AND layer='retry' "
                "ORDER BY id", (run_id,))]
            self.assertEqual(actions, ["deferred", "retry"])
            self.assertEqual(self._completions(run_id), 1)
        self.assertEqual([call.args[2] for call in failed_launches.call_args_list],
                         [stale, malformed])
        retries = list(self.con.execute(
            "SELECT id, retry_of FROM runs WHERE retry_of IS NOT NULL ORDER BY id"))
        self.assertEqual([row["retry_of"] for row in retries], report["reaped"])
        self.assertEqual([call.args[1] for call in spawned.call_args_list],
                         [row["id"] for row in retries])
        rows = {r["id"]: r for r in self.con.execute("SELECT * FROM runs")}
        self.assertEqual(rows[dead]["status"], "failed")
        self.assertIn("vanished", rows[dead]["summary"])
        self.assertIsNotNone(rows[dead]["finished_at"])
        self.assertEqual(rows[stopped_worker]["status"], "failed")
        self.assertEqual(rows[stuck_worker]["status"], "running")
        self.assertEqual(rows[reused_worker]["status"], "running")
        self.assertEqual(rows[legacy_worker]["status"], "running")
        for untouched in (stuck_worker, reused_worker, legacy_worker):
            self.assertEqual(self.con.execute(
                "SELECT COUNT(*) FROM runs WHERE retry_of=?",
                (untouched,)).fetchone()[0], 0)
        self.assertEqual(rows[alive]["status"], "running")
        self.assertEqual(rows[stale]["status"], "failed")
        self.assertTrue(supervise.never_started(rows[stale]))
        self.assertIn("no supervisor claimed", rows[stale]["summary"])
        self.assertEqual(supervise.supervise(Path("/p"), stale), 1,
                         "a delayed child must lose the terminal CAS")
        self.assertEqual(rows[fresh]["status"], "spawning",
                         "the handoff grace protects a child that may be starting")
        self.assertEqual(rows[pending_claim]["status"], "spawning",
                         "remote claim recovery owns pending handoffs")
        self.assertTrue(supervise.never_started(rows[malformed]))

        # Preparation can finish after the stale scan but before the reaper's
        # write lock. Its refreshed launch clock must win that race.
        self.con.execute("UPDATE runs SET status='killed' WHERE id IN (?,?,?,?,?)",
                         (fresh, stuck_worker, reused_worker, legacy_worker, alive))
        raced = self._run(status="spawning")
        self.con.execute("UPDATE runs SET started_at='1999-01-01T00:00:00+00:00' "
                         "WHERE id=?", (raced,))
        self.con.commit()
        preparation = db.connect()
        real_datetime = daemon.datetime
        refreshed = []

        class RefreshDuringScan:
            @staticmethod
            def now(tz=None):
                return real_datetime.now(tz)

            @staticmethod
            def fromisoformat(value):
                parsed = real_datetime.fromisoformat(value)
                if not refreshed:
                    preparation.execute("UPDATE runs SET started_at=? WHERE id=?",
                                        (db.now(), raced))
                    preparation.commit()
                    refreshed.append(True)
                return parsed

        try:
            with mock.patch.object(daemon, "datetime", RefreshDuringScan):
                self.assertEqual(daemon._reap_orphans(self.con), [])
        finally:
            preparation.close()
        raced_row = self.con.execute(
            "SELECT * FROM runs WHERE id=?", (raced,)).fetchone()
        self.assertEqual(raced_row["status"], "spawning")
        self.assertNotEqual(raced_row["started_at"],
                            "1999-01-01T00:00:00+00:00")

    def test_supervisor_ownership_recovers_only_a_proven_pid_reuse(self) -> None:
        matched = self._run(supervisor_pid=41001,
                            supervisor_pid_identity="owner-41001")
        legacy = self._run(supervisor_pid=41002)
        reused = self._run(supervisor_pid=41003,
                           supervisor_pid_identity="old-owner")
        unreadable = self._run(supervisor_pid=41004,
                               supervisor_pid_identity="owner-41004")
        self.con.execute(
            "UPDATE runs SET status='done', worker_status='done' "
            "WHERE id IN (?,?,?,?)", (matched, legacy, reused, unreadable))
        self.con.commit()
        identities = {41001: "owner-41001", 41003: "new-owner"}

        with mock.patch.object(daemon, "_alive", return_value=True), \
                mock.patch.object(daemon.proc, "process_identity",
                                  side_effect=identities.get), \
                mock.patch.object(daemon.proc, "signal_group") as signalled, \
                mock.patch.object(daemon.proc, "terminate_group") as terminated, \
                mock.patch.object(daemon.supervise, "finalize_run") as finalized:
            recovered = daemon._resume_terminal_results(self.con)

        self.assertEqual(recovered, [reused])
        self.assertEqual([call.args[1]["id"] for call in finalized.call_args_list],
                         [reused])
        signalled.assert_not_called()
        terminated.assert_not_called()

    def test_tick_recovers_terminal_results_and_preserves_worker_receipt(self) -> None:
        receipt = self._run(supervisor_pid=_free_pid())
        self.con.execute(
            "UPDATE runs SET worker_status='done', worker_exit_code=0 WHERE id=?",
            (receipt,))
        done = self._run(supervisor_pid=_free_pid())
        live_identity = proc.process_identity(os.getpid())
        live_owner = self._run(supervisor_pid=os.getpid(),
                               supervisor_pid_identity=live_identity)
        reused_pid = 98005
        reused = self._run(supervisor_pid=_free_pid())
        self.con.execute(
            "UPDATE runs SET status='done', worker_status='done' WHERE id=?",
            (done,))
        self.con.execute(
            "UPDATE runs SET status='killed', worker_status='killed' "
            "WHERE id IN (?,?)", (live_owner, reused))
        self._set_worker(reused, reused_pid, "original-owner")
        self.con.commit()
        real_signal_group = daemon.proc.signal_group

        def signal_group(pid, sig):
            if pid == reused_pid:
                return None
            return real_signal_group(pid, sig)

        diagnostics = io.StringIO()
        identities = {reused_pid: "new-unrelated-owner",
                      os.getpid(): live_identity}
        with mock.patch.object(daemon.proc, "signal_group",
                               side_effect=signal_group), \
                mock.patch.object(daemon.proc, "process_identity",
                                  side_effect=identities.get), \
                mock.patch.object(daemon.sys, "stderr", diagnostics):
            report = daemon.tick()

        self.assertIn(receipt, report["reaped"])
        self.assertIn(done, report["recovered_results"])
        rows = {row["id"]: row for row in self.con.execute("SELECT * FROM runs")}
        self.assertEqual(rows[receipt]["status"], "done")
        self.assertEqual(rows[receipt]["exit_code"], 0)
        self.assertNotIn("vanished", rows[receipt]["summary"] or "")
        for finalized in (receipt, done):
            self.assertEqual(self._completions(finalized), 1)
        for untouched in (live_owner, reused):
            self.assertEqual(self._completions(untouched), 0)
        self.assertIn("identity changed", diagnostics.getvalue())

    def test_tick_report_contract(self) -> None:
        """Without Work configured nothing is swept; the nod answers land in
        the report, and a raising nod pass never ends the tick."""
        self.assertEqual(daemon.tick()["swept"], [])
        acted = [{"request_id": "req_1", "action": "retry", "outcome": "landed"}]
        cases = {
            "the answers are carried": ({"return_value": acted}, acted),
            "a raising pass is survived": (
                {"side_effect": RuntimeError("nod exploded")}, []),
        }
        for label, (patched, expected) in cases.items():
            with self.subTest(label), \
                    mock.patch.object(daemon.nod, "act_on_answers",
                                      **patched) as pass_:
                report = daemon.tick()  # must not raise
            self.assertEqual(report["nod_answers"], expected)
            pass_.assert_called_once()

    def test_paused_tick_runs_every_non_admission_pass(self) -> None:
        dead = self._run(supervisor_pid=_free_pid())
        self.con.commit()
        dispatch.pause(self.con, "maintenance")
        client = object()
        with mock.patch.object(daemon.supervise, "process_ready") as ready, \
                mock.patch.object(daemon, "_poll_runway", return_value=2) as runway_, \
                mock.patch.object(daemon, "_act_on_nod_answers",
                                  return_value=[{"answer": 1}]) as nod_, \
                mock.patch.object(daemon.sweeper, "refresh_projects",
                                  return_value=True) as refresh, \
                mock.patch.object(daemon.work_client, "from_cfg",
                                  return_value=client), \
                mock.patch.object(daemon.sweeper, "sweep",
                                  return_value=[{"action": "report"}]) as swept, \
                mock.patch.object(daemon.conductor, "pass_once",
                                  return_value=[{"action": "wait"}]) as conducted:
            report = daemon.tick()
        self.assertTrue(report["paused"])
        self.assertEqual(report["reaped"], [dead])
        self.assertEqual(report["runway"], 2)
        self.assertEqual(report["nod_answers"], [{"answer": 1}])
        self.assertEqual(report["swept"], [{"action": "report"}])
        self.assertEqual(report["conducted"], [{"action": "wait"}])
        ready.assert_called_once()
        runway_.assert_called_once()
        nod_.assert_called_once()
        refresh.assert_called_once()
        swept.assert_called_once()
        conducted.assert_called_once()

    def test_once_runs_a_single_tick_even_a_failing_one_and_returns_zero(self) -> None:
        cases = {"a clean tick": {"return_value": {}},
                 "a failing tick": {"side_effect": RuntimeError("boom")}}
        for label, patched in cases.items():
            with self.subTest(label), \
                    mock.patch.object(daemon, "tick", **patched) as ticked:
                self.assertEqual(daemon.run(interval=1, once=True), 0)
            self.assertEqual(ticked.call_count, 1)

    @unittest.skipIf(sys.platform == "win32",
                     "os.kill(SIGTERM) terminates immediately on Windows")
    def test_sigterm_stops_the_loop_between_ticks(self) -> None:
        """launchd stops the job with SIGTERM; it must never land mid-pass."""
        previous = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
        ticks = []

        def tick_then_signal():
            ticks.append(1)
            os.kill(os.getpid(), signal.SIGTERM)
            return {}

        try:
            with mock.patch.object(daemon, "tick", side_effect=tick_then_signal):
                self.assertEqual(daemon.run(interval=600), 0)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        self.assertEqual(len(ticks), 1)  # stopped after the pass, not during it

    def test_http_seam_is_named_and_wired_before_the_loop(self) -> None:
        """DESIGN §3 attaches here; this item leaves the seam only."""
        with mock.patch.object(daemon, "serve_http") as seam, \
                mock.patch.object(daemon, "tick", return_value={}):
            daemon.run(interval=1, once=True)
        seam.assert_called_once()


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name).resolve()
        self.agents = self.tmp_path / "LaunchAgents"
        self.env = mock.patch.dict(os.environ, {
            "ORCHESTRA_HOME": str(self.tmp_path / "home"),
            "ORCHESTRA_LAUNCH_AGENTS": str(self.agents)})
        self.env.start()
        self.launchctl = mock.patch.object(
            service, "_launchctl",
            return_value=mock.Mock(returncode=1, stdout="", stderr="")).start()
        mock.patch.object(service, "_windows", return_value=False).start()

    def tearDown(self) -> None:
        mock.patch.stopall()
        self.env.stop()
        self.tmp.cleanup()

    def test_the_plist_lifecycle_never_loads_and_removes_only_ours(self) -> None:
        """status creates nothing; install writes the plist idempotently and
        never loads it; uninstall removes only the plist Orchestra wrote."""
        self.assertEqual(service.status(), 0)
        self.assertFalse(self.agents.exists())
        self.launchctl.reset_mock()  # status may ask launchd, install must not

        self.assertEqual(service.install(), 0)
        p = self.agents / "local.orchestra.daemon.plist"
        with open(p, "rb") as f:
            plist = plistlib.load(f)
        home = str(self.tmp_path / "home")
        self.assertEqual(
            (plist["Label"], plist["ProgramArguments"][-1],
             plist["EnvironmentVariables"]["ORCHESTRA_HOME"]),
            ("local.orchestra.daemon", "daemon", home))
        self.assertTrue(plist["StandardOutPath"].startswith(home))
        self.launchctl.assert_not_called()

        first = p.read_bytes()
        service.install()
        self.assertEqual(p.read_bytes(), first, "install is idempotent")

        keep = self.agents / "someone.elses.plist"
        keep.write_text("not ours")
        self.assertEqual(service.uninstall(), 0)
        self.assertFalse(p.exists())
        self.assertTrue(keep.exists())

    def test_install_start_bootstraps_the_job(self) -> None:
        self.launchctl.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        self.assertEqual(service.install(start=True), 0)
        args = self.launchctl.call_args[0]
        self.assertEqual(args[0], "bootstrap")
        self.assertTrue(args[2].endswith("local.orchestra.daemon.plist"))


def _free_pid() -> int:
    """A pid that is not running. ponytail: probes upward from a high number;
    a same-second pid reuse would flake, which no test here can trigger."""
    for pid in range(99000, 99999):
        if not proc.alive(pid):
            return pid
    raise unittest.SkipTest("no free pid found")


class ServiceRestartTests(unittest.TestCase):
    """`orchestra service restart` is the deploy step for an editable install:
    the code is read from the working tree, so restarting is all that ships."""

    def test_restart_contract(self) -> None:
        # Nothing supervises a foreground daemon, so there is nothing to
        # restart; saying so beats a success message that changed nothing.
        cases = {
            "kickstarts a loaded agent": (True, 0, "", 0),
            "reports a bare daemon rather than pretending": (False, 0, "4242\n", 1),
            "says so when nothing runs": (False, 1, "", 1),
        }
        for label, (loaded, pgrep_rc, pgrep_out, expected) in cases.items():
            calls = []

            def fake(*args):
                calls.append(args)
                return subprocess.CompletedProcess(args, 0, "", "")

            with self.subTest(label), \
                    mock.patch.object(service, "_windows", return_value=False), \
                    mock.patch.object(service, "is_loaded", return_value=loaded), \
                    mock.patch.object(service, "_launchctl", side_effect=fake), \
                    mock.patch("subprocess.run",
                               return_value=subprocess.CompletedProcess(
                                   [], pgrep_rc, pgrep_out, "")):
                self.assertEqual(expected, service.restart())
            if loaded:
                kick = [c for c in calls if c[0] == "kickstart"]
                self.assertTrue(kick, "a loaded agent is kickstarted")
                self.assertIn("-k", kick[0])
            else:
                self.assertEqual(calls, [])

    def test_a_kickstart_that_changes_nothing_is_not_a_restart(self) -> None:
        """2026-08-26: `kickstart -k` returned 0 and left the process alone.
        The daemon ran fifteen hours across two reported restarts, serving
        stale code. Success is a NEW pid, so the pid is checked, SIGTERM
        finishes what kickstart would not, and a job that survives both is
        reported as the failure it is."""
        # Each sequence is what launchd reports for the job, poll by poll:
        # the pid before the restart, then one reading per wait.
        cases = {
            # kickstart no-ops for the whole wait, SIGTERM lands: restarted.
            "sigterm finishes the job": ([7] + [7] * 40 + [9] * 40, 0, True),
            # Nothing moves it: never claim a restart that did not happen.
            "an immovable job fails loudly": ([7] * 120, 1, True),
            # The ordinary case still costs exactly one launchctl verb.
            "a working kickstart needs no sigterm": ([7] + [9] * 40, 0, False),
        }
        for label, (pids, expected, sigterm) in cases.items():
            calls = []

            def fake(*args):
                calls.append(args)
                return subprocess.CompletedProcess(args, 0, "", "")

            with self.subTest(label), \
                    mock.patch.object(service, "_windows", return_value=False), \
                    mock.patch.object(service, "is_loaded", return_value=True), \
                    mock.patch.object(service, "_launchctl", side_effect=fake), \
                    mock.patch.object(service, "service_pid",
                                      side_effect=list(pids) + [pids[-1]] * 200), \
                    mock.patch.object(service.time, "sleep"):
                self.assertEqual(expected, service.restart())
            verbs = [c[0] for c in calls]
            self.assertEqual("kickstart", verbs[0])
            self.assertEqual(sigterm, "kill" in verbs,
                             "SIGTERM only when kickstart changed nothing")


if __name__ == "__main__":
    unittest.main()


class FileLimitTests(unittest.TestCase):
    """piu-arcade-lift run 40 died in one second: "possibly due to low max
    file descriptors (Current limit: 256)". launchd hands its jobs 256, and
    every run Orchestra starts inherits it — the daemon, each supervisor,
    each worker. Run 38 went the other way and was left with no supervisor
    at all, while nine runs were live at once.
    """

    def test_a_launchd_limit_is_raised_for_everything_spawned(self) -> None:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        self.addCleanup(resource.setrlimit, resource.RLIMIT_NOFILE, (soft, hard))
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))

        raised = proc.raise_file_limit()
        self.assertGreater(raised, 256)
        self.assertEqual(raised, resource.getrlimit(resource.RLIMIT_NOFILE)[0])
        # A child inherits it, which is the whole point: the harness is the
        # process that runs out, not us.
        seen = subprocess.run(
            [sys.executable, "-c", "import resource;"
             "print(resource.getrlimit(resource.RLIMIT_NOFILE)[0])"],
            capture_output=True, text=True, check=True)
        self.assertEqual(raised, int(seen.stdout.strip()))

    def test_a_limit_already_high_enough_is_left_alone(self) -> None:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        self.addCleanup(resource.setrlimit, resource.RLIMIT_NOFILE, (soft, hard))
        resource.setrlimit(resource.RLIMIT_NOFILE, (proc.FILE_LIMIT, hard))
        self.assertEqual(proc.FILE_LIMIT, proc.raise_file_limit())

    def test_a_kernel_that_refuses_the_ask_gets_a_smaller_one(self) -> None:
        """macOS caps a process below its own hard limit
        (kern.maxfilesperproc). A refusal is not a reason to leave the run
        with 256 descriptors, so the next size down is tried."""
        import resource
        asked = []

        def picky(which, limits):
            asked.append(limits[0])
            if limits[0] > 10240:
                raise ValueError("current limit exceeds maximum limit")

        with mock.patch.object(resource, "getrlimit",
                               return_value=(256, resource.RLIM_INFINITY)), \
                mock.patch.object(resource, "setrlimit", side_effect=picky):
            self.assertEqual(10240, proc.raise_file_limit())
        self.assertEqual([proc.FILE_LIMIT, 10240], asked,
                         "the big ask first, then the one that fits")
