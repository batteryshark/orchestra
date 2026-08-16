"""The daemon loop and its launchd LaunchAgent (DESIGN §2).

Nothing here starts a daemon or talks to the real launchd: `launchctl` is
patched out and the plist is written under DROMOND_LAUNCH_AGENTS.
"""
import os
import plistlib
import signal
import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from dromond import daemon, db, service, runway

PROJECT_ID = "53efe3c3-6def-4797-8560-3dce073d7d63"


class DaemonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name).resolve()
        (self.tmp_path / "global.toml").write_text("")
        self.env = mock.patch.dict(os.environ, {
            "DROMOND_HOME": str(self.tmp_path / "home"),
            "DROMOND_CONFIG": str(self.tmp_path / "global.toml")})
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

    def _run(self, status="running", supervisor_pid=None) -> int:
        return int(self.con.execute(
            "INSERT INTO runs(profile, backend, requested_by, workdir, status, "
            "supervisor_pid, project_id, started_at) VALUES('p','codex','human','/p',"
            "?,?,?,?)", (status, supervisor_pid, PROJECT_ID, db.now())).lastrowid)

    def test_tick_reaps_a_run_whose_supervisor_vanished(self) -> None:
        # PID 1 exists (launchd) and is not ours -> alive; a free high pid is not.
        dead = self._run(supervisor_pid=_free_pid())
        alive = self._run(supervisor_pid=1)
        self.con.commit()
        report = daemon.tick()
        self.assertEqual(report["reaped"], [dead])
        rows = {r["id"]: r for r in self.con.execute("SELECT * FROM runs")}
        self.assertEqual(rows[dead]["status"], "failed")
        self.assertIn("vanished", rows[dead]["summary"])
        self.assertIsNotNone(rows[dead]["finished_at"])
        self.assertEqual(rows[alive]["status"], "running")

    def test_tick_leaves_a_finished_run_alone(self) -> None:
        done = self._run(status="done", supervisor_pid=_free_pid())
        self.con.commit()
        self.assertEqual(daemon.tick()["reaped"], [])
        self.assertEqual(
            self.con.execute("SELECT status FROM runs WHERE id=?",
                             (done,)).fetchone()["status"], "done")

    def test_tick_without_work_configured_does_not_sweep(self) -> None:
        report = daemon.tick()
        self.assertEqual(report["swept"], [])

    def test_once_runs_a_single_tick_and_returns_zero(self) -> None:
        with mock.patch.object(daemon, "tick", return_value={}) as ticked:
            self.assertEqual(daemon.run(interval=1, once=True), 0)
        self.assertEqual(ticked.call_count, 1)

    def test_a_failing_tick_does_not_end_the_daemon(self) -> None:
        with mock.patch.object(daemon, "tick", side_effect=RuntimeError("boom")):
            self.assertEqual(daemon.run(interval=1, once=True), 0)

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
            "DROMOND_HOME": str(self.tmp_path / "home"),
            "DROMOND_LAUNCH_AGENTS": str(self.agents)})
        self.env.start()
        self.launchctl = mock.patch.object(
            service, "_launchctl",
            return_value=mock.Mock(returncode=1, stdout="", stderr="")).start()

    def tearDown(self) -> None:
        mock.patch.stopall()
        self.env.stop()
        self.tmp.cleanup()

    def test_install_writes_the_plist_and_never_loads_it(self) -> None:
        self.assertEqual(service.install(), 0)
        p = self.agents / "local.dromond.daemon.plist"
        self.assertTrue(p.is_file())
        with open(p, "rb") as f:
            plist = plistlib.load(f)
        self.assertEqual(plist["Label"], "local.dromond.daemon")
        self.assertEqual(plist["ProgramArguments"][-1], "daemon")
        self.assertTrue(plist["StandardOutPath"].startswith(str(self.tmp_path / "home")))
        self.assertEqual(plist["EnvironmentVariables"]["DROMOND_HOME"],
                         str(self.tmp_path / "home"))
        self.launchctl.assert_not_called()

    def test_install_start_bootstraps_the_job(self) -> None:
        self.launchctl.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        self.assertEqual(service.install(start=True), 0)
        args = self.launchctl.call_args[0]
        self.assertEqual(args[0], "bootstrap")
        self.assertTrue(args[2].endswith("local.dromond.daemon.plist"))

    def test_install_is_idempotent(self) -> None:
        service.install()
        first = (self.agents / "local.dromond.daemon.plist").read_bytes()
        service.install()
        self.assertEqual((self.agents / "local.dromond.daemon.plist").read_bytes(),
                         first)

    def test_uninstall_removes_only_the_plist_dromond_wrote(self) -> None:
        service.install()
        keep = self.agents / "someone.elses.plist"
        keep.write_text("not ours")
        self.assertEqual(service.uninstall(), 0)
        self.assertFalse((self.agents / "local.dromond.daemon.plist").exists())
        self.assertTrue(keep.exists())

    def test_status_reports_absent_before_install(self) -> None:
        self.assertEqual(service.status(), 0)
        self.assertFalse(self.agents.exists())


def _free_pid() -> int:
    """A pid that is not running. ponytail: probes upward from a high number;
    a same-second pid reuse would flake, which no test here can trigger."""
    for pid in range(99000, 99999):
        try:
            os.kill(pid, 0)
        except OSError:
            return pid
    raise unittest.SkipTest("no free pid found")


class ServiceRestartTests(unittest.TestCase):
    """`dromond service restart` is the deploy step for an editable install:
    the code is read from the working tree, so restarting is all that ships."""

    def test_restart_kickstarts_a_loaded_agent(self) -> None:
        calls = []

        def fake(*args):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch.object(service, "is_loaded", return_value=True), \
             mock.patch.object(service, "_launchctl", side_effect=fake):
            self.assertEqual(0, service.restart())
        self.assertEqual("kickstart", calls[0][0])
        self.assertIn("-k", calls[0])

    def test_restart_reports_a_bare_daemon_rather_than_pretending(self) -> None:
        # Nothing supervises a foreground daemon, so there is nothing to
        # restart; saying so beats a success message that changed nothing.
        with mock.patch.object(service, "is_loaded", return_value=False), \
             mock.patch("subprocess.run",
                        return_value=subprocess.CompletedProcess([], 0, "4242\n", "")):
            self.assertEqual(1, service.restart())

    def test_restart_says_so_when_nothing_runs(self) -> None:
        with mock.patch.object(service, "is_loaded", return_value=False), \
             mock.patch("subprocess.run",
                        return_value=subprocess.CompletedProcess([], 1, "", "")):
            self.assertEqual(1, service.restart())


if __name__ == "__main__":
    unittest.main()
