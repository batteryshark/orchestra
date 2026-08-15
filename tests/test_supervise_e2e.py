"""End-to-end supervision against a stub backend binary on PATH.

The stub impersonates `opencode`: it emits the same JSONL shapes the real
backend does (session id, step_finish boundaries, text output), so the
whole dispatch -> supervise -> finalize path runs unmodified.
"""
import os
import tempfile
import threading
import time
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

import subprocess

from dromond import cli, db, merge, project, supervise, traces


def _git(root, *args) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True,
                   capture_output=True)

PROJECT_ID = "53efe3c3-6def-4797-8560-3dce073d7d63"

STUB = """\
#!/usr/bin/env python3
import json, os, sys, time
# W-0176: report what the worker's own environment carried, to a side file
# and never to the log — a token in a trace is the leak tokens exist to end.
if os.environ.get("DROMOND_TEST_ENV_SINK"):
    open(os.environ["DROMOND_TEST_ENV_SINK"], "w").write(
        os.environ.get("DROMOND_RUN_TOKEN", ""))
args = sys.argv[1:]
# W-0191: the backend refusing to resume. "resume" fails only the --session
# invocations, "always" fails every one of them.
gone = os.environ.get("STUB_SESSION_GONE", "")
if gone == "always" or (gone and "--session" in args):
    print('error: no session matches "dead-session"', flush=True)
    sys.exit(1)
if "--session" in args:
    print(json.dumps({"type": "text", "text": "resumed: " + args[-1][:400]}), flush=True)
    sys.exit(0)
print(json.dumps({"sessionID": "stub-session-1"}), flush=True)
time.sleep(float(os.environ.get("STUB_SLEEP", "0")))
for _ in range(int(os.environ.get("STUB_STEPS", "0"))):
    print(json.dumps({"type": "step_finish", "part": {
        "type": "step-finish", "cost": 0.01,
        "tokens": {"total": 1100, "input": 1000, "output": 100,
                   "reasoning": 0, "cache": {"read": 0, "write": 0}}}}), flush=True)
    time.sleep(0.3)
print(json.dumps({"type": "text", "text": "stub work complete"}), flush=True)
sys.exit(int(os.environ.get("STUB_EXIT", "0")))
"""

CONFIG = """\
[settings]
timeout = 60

[profiles.stub]
backend = "opencode"
"""


class E2ETestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name).resolve()
        self.root = self.tmp_path / "workspace" / "demo"
        self.root.mkdir(parents=True)
        self.global_config = self.tmp_path / "global.toml"
        self.global_config.write_text(CONFIG)
        bin_dir = self.tmp_path / "stub-bin"
        bin_dir.mkdir()
        stub = bin_dir / "opencode"
        stub.write_text(STUB)
        stub.chmod(0o755)
        self.env = mock.patch.dict(os.environ, {
            "DROMOND_CONFIG": str(self.global_config),
            "DROMOND_HOME": str(self.tmp_path / "home"),
            "DROMOND_ROOT": str(self.root),
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
            "STUB_SLEEP": "0",
            "STUB_STEPS": "0",
            "STUB_EXIT": "0",
            "STUB_SESSION_GONE": "",
        })
        self.env.start()
        # Work is not running here: seed the project cache directly, which is
        # exactly the offline path the CLI depends on (DESIGN §2).
        con = db.connect()
        project.remember(con, str(self.tmp_path / "workspace"),
                         [{"projectId": PROJECT_ID, "id": "demo", "name": "Demo",
                           "path": "demo"}])
        con.close()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def _dispatch(self, mission: str = "do the thing", after: list[int] | None = None):
        """Run the real dispatch command with the detached spawn patched out."""
        ns = Namespace(mission=[mission], to="stub", after=after, brief_file=None,
                       context=None, title=None, worktree=False, sync=False)
        with mock.patch.object(supervise, "spawn_supervisor") as spawned:
            cli.cmd_dispatch(ns)
        con = db.connect()
        run_id = int(con.execute("SELECT MAX(id) AS n FROM runs").fetchone()["n"])
        con.close()
        return run_id, spawned

    def _run(self, run_id: int):
        rc = supervise.supervise(self.root, run_id)
        con = db.connect()
        run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        con.close()
        return rc, run

    def test_dispatch_supervise_complete(self) -> None:
        run_id, _ = self._dispatch(mission="write the fix")
        rc, run = self._run(run_id)
        self.assertEqual(rc, 0)
        self.assertEqual(run["status"], "done")
        self.assertEqual(run["exit_code"], 0)
        self.assertEqual(run["session_ref"], "stub-session-1")
        # The stub hands off no findings/proposals block, so the completion
        # seam (DESIGN §9) records a protocol failure on top of the summary.
        self.assertTrue(run["summary"].startswith("stub work complete"))
        self.assertIn("handoff protocol failure", run["summary"])
        self.assertIsNotNone(run["finished_at"])
        brief_text = Path(run["brief_path"]).read_text()
        self.assertIn("write the fix", brief_text)
        self.assertIn("## Protocol", brief_text)
        con = db.connect()
        note = con.execute(
            "SELECT body FROM messages WHERE run_id=? AND kind='completion'",
            (run_id,)).fetchone()
        con.close()
        self.assertIn("finished: done", note["body"])

    def test_the_worker_carries_a_run_token_that_dies_with_the_run(self) -> None:
        """W-0176: minted at launch into the worker's environment, revoked
        when the run turns terminal, and in no file anybody reads."""
        from dromond import auth
        sink = self.tmp_path / "token-seen.txt"
        os.environ["DROMOND_TEST_ENV_SINK"] = str(sink)
        run_id, _ = self._dispatch()
        con = db.connect()
        self.assertIsNone(con.execute(
            "SELECT run_token_hash FROM runs WHERE id=?",
            (run_id,)).fetchone()["run_token_hash"], "not minted before launch")
        con.close()

        _, run = self._run(run_id)
        self.assertEqual(run["status"], "done")
        token = sink.read_text()
        self.assertTrue(token, "the worker's environment carried no token")
        con = db.connect()
        try:
            self.assertIsNone(auth.identify(con, token, None))
            self.assertEqual(con.execute(
                "SELECT COUNT(*) AS n FROM runs WHERE run_token_hash=?",
                (auth.hashed(token),)).fetchone()["n"], 0)
        finally:
            con.close()
        self.assertNotIn(token, Path(run["log_path"]).read_text())
        self.assertNotIn(token, Path(run["brief_path"]).read_text())
        self.assertNotIn(token, run["summary"] or "")

    def test_supervision_normalizes_the_trace(self) -> None:
        """DESIGN §7: the supervisor's own tail is what fills the events
        table — no second tailer, no separate pass."""
        run_id, _ = self._dispatch()
        self._run(run_id)
        con = db.connect()
        events = traces.events_for_run(con, run_id)
        cursor = traces.cursor(con, run_id)
        con.close()
        self.assertIn("stub work complete",
                      [e["payload"] for e in events if e["kind"] == "assistant_text"])
        self.assertIn("lifecycle", {e["kind"] for e in events})
        self.assertEqual(cursor["byte_offset"],
                         Path(self.tmp_path / "home" / "logs" /
                              f"run-{run_id}.jsonl").stat().st_size)

    def test_completion_stamps_tokens_and_cost_on_the_run_row(self) -> None:
        """DESIGN §11: the numbers land at completion, so the dashboard is a
        query. Two stub steps, so the sum is the run total."""
        os.environ["STUB_STEPS"] = "2"
        run_id, _ = self._dispatch()
        _, run = self._run(run_id)
        self.assertEqual(run["tokens_in"], 2000)
        self.assertEqual(run["tokens_out"], 200)
        self.assertEqual(run["tokens_total"], 2200)
        self.assertEqual(run["cost_usd"], 0.02)
        self.assertEqual(run["usage_source"], "opencode")

    def test_a_run_without_usage_records_null_not_zero(self) -> None:
        """The stub emits no step tokens here, which is exactly what a
        backend whose usage we cannot read looks like."""
        run_id, _ = self._dispatch()
        _, run = self._run(run_id)
        self.assertIsNone(run["tokens_total"])
        self.assertIsNone(run["cost_usd"])
        self.assertIsNone(run["usage_source"])
        self.assertEqual(run["status"], "done")   # capture never fails a run

    def test_a_merge_failure_never_breaks_finalization(self) -> None:
        """W-0174: the landing seam runs at finalization, after the worktree
        is released. Whatever it hits, the run still completes and the reason
        lands on the summary."""
        run_id, _ = self._dispatch()
        con = db.connect()
        con.execute("UPDATE runs SET branch='dromond/run-x' WHERE id=?", (run_id,))
        con.commit()
        con.close()
        with mock.patch.object(merge, "merge_run",
                               side_effect=OSError("the disk went away")):
            rc, run = self._run(run_id)
        self.assertEqual(rc, 0)
        self.assertEqual(run["status"], "done")
        self.assertIsNotNone(run["finished_at"])
        self.assertIn("Merge failed: the disk went away", run["summary"])

    def test_nonzero_exit_is_failed(self) -> None:
        os.environ["STUB_EXIT"] = "3"
        run_id, _ = self._dispatch()
        rc, run = self._run(run_id)
        self.assertEqual(rc, 1)
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["exit_code"], 3)

    def test_stalled_worker_is_timed_out(self) -> None:
        self.global_config.write_text(CONFIG + "stall_timeout = 1\n")
        os.environ["STUB_SLEEP"] = "30"
        run_id, _ = self._dispatch()
        started = time.time()
        rc, run = self._run(run_id)
        self.assertEqual(rc, 1)
        self.assertEqual(run["status"], "timeout")
        self.assertTrue(run["summary"].startswith("Stalled:"))
        self.assertLess(time.time() - started, 20)

    def test_open_ask_is_not_a_stall(self) -> None:
        """W-0098: a session held open by its Stop hook waiting for a human
        produces no output. Killing it would throw the answer away."""
        self.global_config.write_text(CONFIG + "stall_timeout = 1\n")
        os.environ["STUB_SLEEP"] = "4"
        run_id, _ = self._dispatch()
        con = db.connect()
        con.execute(
            "INSERT INTO nod_requests(request_id, kind, channel, run_id, status, "
            "created_at) VALUES('req_1','blocked','chan-decisions',?,'pending',?)",
            (run_id, db.now()))
        con.commit()
        con.close()
        rc, run = self._run(run_id)
        self.assertEqual(rc, 0)
        self.assertEqual(run["status"], "done")

    def test_after_dependency_release(self) -> None:
        first_id, _ = self._dispatch(mission="first")
        self._run(first_id)
        second_id, spawned = self._dispatch(mission="second", after=[first_id])
        # The prerequisite already succeeded, so dispatch releases it at once.
        spawned.assert_called_once_with(self.root, second_id)
        rc, run = self._run(second_id)
        self.assertEqual(run["status"], "done")
        self.assertIn("second", Path(run["brief_path"]).read_text())

    def test_failed_prerequisite_declines_dependent(self) -> None:
        os.environ["STUB_EXIT"] = "1"
        first_id, _ = self._dispatch(mission="first")
        self._run(first_id)
        second_id, spawned = self._dispatch(mission="second", after=[first_id])
        spawned.assert_not_called()
        con = db.connect()
        run = con.execute("SELECT * FROM runs WHERE id=?", (second_id,)).fetchone()
        deferred = con.execute(
            "SELECT status FROM deferred_dispatches WHERE run_id=?",
            (second_id,)).fetchone()
        con.close()
        self.assertEqual(run["status"], "failed")
        self.assertIn("Declined", run["summary"])
        self.assertEqual(deferred["status"], "declined")

    def test_interrupt_delivered_at_safe_boundary_then_resumed(self) -> None:
        os.environ["STUB_STEPS"] = "40"  # ~12s of step_finish boundaries
        run_id, _ = self._dispatch()
        thread = threading.Thread(target=supervise.supervise, args=(self.root, run_id))
        thread.start()
        try:
            con = db.connect()
            deadline = time.time() + 10
            session_ref = None
            while time.time() < deadline and not session_ref:
                session_ref = con.execute(
                    "SELECT session_ref FROM runs WHERE id=?",
                    (run_id,)).fetchone()["session_ref"]
                time.sleep(0.1)
            con.close()
            self.assertTrue(session_ref, "session ref never captured")
            cli.cmd_interrupt(Namespace(run_id=run_id, message=["use", "tabs"],
                                        message_file=None, now=False))
        finally:
            thread.join(timeout=30)
        self.assertFalse(thread.is_alive())
        con = db.connect()
        run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        message = con.execute(
            "SELECT delivered_at FROM messages WHERE run_id=? AND kind='interrupt'",
            (run_id,)).fetchone()
        con.close()
        self.assertEqual(run["status"], "done")
        self.assertIsNotNone(message["delivered_at"])
        # The resumed session received the message embedded in its prompt.
        self.assertTrue(run["summary"].startswith("resumed:"), run["summary"])
        self.assertIn("use tabs", run["summary"])

    def test_kill_terminates_run(self) -> None:
        os.environ["STUB_SLEEP"] = "30"
        run_id, _ = self._dispatch()
        thread = threading.Thread(target=supervise.supervise, args=(self.root, run_id))
        thread.start()
        try:
            con = db.connect()
            deadline = time.time() + 10
            status = None
            while time.time() < deadline and status != "running":
                status = con.execute("SELECT status FROM runs WHERE id=?",
                                     (run_id,)).fetchone()["status"]
                time.sleep(0.1)
            con.close()
            self.assertEqual(status, "running")
            cli.cmd_kill(Namespace(run_id=run_id))
        finally:
            thread.join(timeout=20)
        self.assertFalse(thread.is_alive())
        con = db.connect()
        run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        con.close()
        self.assertEqual(run["status"], "killed")
        self.assertIsNotNone(run["finished_at"])

    # --- a resume that cannot resume (W-0191) -------------------------------

    def _continuation(self, session_ref: str = "dead-session") -> int:
        """A run that will be launched with --resume against ``session_ref``."""
        parent_id, _ = self._dispatch(mission="the first attempt")
        child_id, _ = self._dispatch(mission="carry on")
        con = db.connect()
        con.execute("UPDATE runs SET parent_run=?, session_ref=? WHERE id=?",
                    (parent_id, session_ref, child_id))
        con.commit()
        con.close()
        return child_id

    def test_a_missing_session_restarts_the_work_instead_of_failing_it(self) -> None:
        """Live failure (run 27): the backend answered `no session matches
        "..."` and the run died in under a second with an empty trace. The
        session is gone, so nothing can resume it — but a FRESH run would have
        worked, and that is what the item deserves."""
        os.environ["STUB_SESSION_GONE"] = "resume"
        run_id = self._continuation()
        rc, run = self._run(run_id)
        self.assertEqual(rc, 0)
        self.assertEqual(run["status"], "done")
        self.assertIn("stub work complete", run["summary"])
        self.assertIn("dead-session", run["summary"])
        self.assertIn("was gone", run["summary"])
        self.assertIn("restarted fresh", run["summary"])
        self.assertEqual(run["session_ref"], "stub-session-1",
                         "the fresh session replaces the dead ref")
        log = Path(run["log_path"]).read_text()
        self.assertEqual(log.count("no session matches"), 1)

    def test_a_backend_that_always_says_the_session_is_gone_fails_normally(self) -> None:
        """One fresh attempt, never a loop."""
        os.environ["STUB_SESSION_GONE"] = "always"
        run_id = self._continuation()
        rc, run = self._run(run_id)
        self.assertEqual(rc, 1)
        self.assertEqual(run["status"], "failed")
        self.assertIn("restarted fresh", run["summary"])
        log = Path(run["log_path"]).read_text()
        self.assertEqual(log.count("no session matches"), 2,
                         "the resume, then exactly one fresh attempt")


if __name__ == "__main__":
    unittest.main()

class FollowupAfterCleanupTests(unittest.TestCase):
    """A follow-up to finished work must not inherit a released worktree.

    Live failure (run 9): its parent had completed, so cleanup gave the
    worktree back and the merge deleted the branch. The follow-up copied both
    paths, the supervisor died the moment it tried to stand in a directory
    that was gone, and the run sat in `spawning` until the reaper caught it.
    """

    def test_a_gone_worktree_is_replaced_not_inherited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            _git(root, "init", "-q", ".")
            _git(root, "config", "user.email", "t@example.com")
            _git(root, "config", "user.name", "t")
            (root / "a.txt").write_text("x\n")
            _git(root, "add", "-A")
            _git(root, "commit", "-qm", "init")

            con = db.connect()
            try:
                cur = con.execute(
                    "INSERT INTO runs(profile, backend, requested_by, workdir, "
                    "branch, status, started_at, session_ref) "
                    "VALUES('p','codex','human',?,?, 'done', ?, 'sess-1')",
                    (str(root / "gone"), "dromond/run-1", db.now()))
                parent = con.execute("SELECT * FROM runs WHERE id=?",
                                     (cur.lastrowid,)).fetchone()
                self.assertFalse(Path(parent["workdir"]).exists())

                run_id = supervise.create_followup(
                    con, root, parent, "human", "carry on")
                child = con.execute("SELECT * FROM runs WHERE id=?",
                                    (run_id,)).fetchone()
                self.assertNotEqual(child["workdir"], parent["workdir"])
                self.assertTrue(Path(child["workdir"]).exists(),
                                "a follow-up must have somewhere to stand")
                self.assertEqual(child["session_ref"], parent["session_ref"],
                                 "the conversation still resumes")
            finally:
                con.close()
