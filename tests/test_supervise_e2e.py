"""End-to-end supervision against a stub backend binary on PATH.

The stub impersonates `opencode`: it emits the same JSONL shapes the real
backend does (session id, step_finish boundaries, text output), so the
whole dispatch -> supervise -> finalize path runs unmodified.
"""
import json
import os
import signal as signal_module
import tempfile
import threading
import time
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

import subprocess

from orchestra import cli, db, merge, messaging, project, supervise, traces, worktree


def _git(root, *args) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True,
                   capture_output=True)


def _seed_repo(tmp: str) -> Path:
    """A committed git repo for the follow-up tests to stand in."""
    root = Path(tmp) / "project"
    root.mkdir()
    _git(root, "init", "-q", ".")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "a.txt").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


def _finished_parent(con, root: Path, requested_by: str):
    """A completed parent run whose worktree is already gone."""
    cur = con.execute(
        "INSERT INTO runs(profile, backend, requested_by, workdir, "
        "branch, status, started_at, session_ref) "
        "VALUES('p','codex',?,?,?, 'done', ?, 'sess-1')",
        (requested_by, str(root / "gone"), "orchestra/run-1", db.now()))
    parent = con.execute("SELECT * FROM runs WHERE id=?",
                         (cur.lastrowid,)).fetchone()
    con.commit()
    return parent

PROJECT_ID = "53efe3c3-6def-4797-8560-3dce073d7d63"

STUB = """\
#!/usr/bin/env python3
import json, os, sys, time
# W-0176: report what the worker's own environment carried, to a side file
# and never to the log — a token in a trace is the leak tokens exist to end.
if os.environ.get("ORCHESTRA_TEST_ENV_SINK"):
    open(os.environ["ORCHESTRA_TEST_ENV_SINK"], "w").write(
        os.environ.get("ORCHESTRA_RUN_TOKEN", ""))
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
    time.sleep(float(os.environ.get("STUB_STEP_SLEEP", "0")))
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
            "ORCHESTRA_CONFIG": str(self.global_config),
            "ORCHESTRA_HOME": str(self.tmp_path / "home"),
            "ORCHESTRA_ROOT": str(self.root),
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
            "STUB_SLEEP": "0",
            "STUB_STEP_SLEEP": "0",
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

    def _run_field(self, run_id: int, field: str):
        con = db.connect()
        try:
            return con.execute(f"SELECT {field} FROM runs WHERE id=?",
                               (run_id,)).fetchone()[field]
        finally:
            con.close()

    def _wait_until(self, probe, done=bool, timeout: float = 10.0):
        """Bounded wait: poll ``probe`` until ``done(value)`` or the deadline.

        Returns the last observed value either way, so the caller's assert
        fails naming the observed state instead of a timing guess."""
        deadline = time.monotonic() + timeout
        value = probe()
        while not done(value) and time.monotonic() < deadline:
            time.sleep(0.01)
            value = probe()
        return value

    def _supervise_bg(self, run_id: int) -> threading.Thread:
        thread = threading.Thread(target=supervise.supervise,
                                  args=(self.root, run_id))
        thread.start()
        return thread

    def test_merge_check_failure_is_injected_into_the_same_run(self) -> None:
        root = _seed_repo(str(self.tmp_path))
        run_dir, branch = worktree.create(root, 99, PROJECT_ID)
        log = self.tmp_path / "check-ferry.jsonl"
        log.write_text("")
        con = db.connect()
        try:
            cur = con.execute(
                "INSERT INTO runs(profile, backend, requested_by, workdir, "
                "project_id, branch, log_path, status, started_at, session_ref) "
                "VALUES('stub','opencode','work',?,?,?,?, 'running', ?, 'sess-1')",
                (str(run_dir), PROJECT_ID, branch, str(log), db.now()))
            con.commit()
            run_id = int(cur.lastrowid)
            run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()

            ferried = supervise._ferry_check_failure(
                con, {"merge": {"checks": {"test": "exit 7"}}}, run, "done")
            delivered = messaging.claim_pending(con, run_id)

            self.assertTrue(ferried)
            self.assertEqual(len(delivered), 1)
            self.assertEqual(delivered[0]["sender"], "orchestra:merge-check")
            self.assertIn("exit: 7", delivered[0]["body"])
            injections = [event for event in traces.events_for_run(con, run_id)
                          if event["kind"] == "human_injection"]
            self.assertEqual(len(injections), 1)
            self.assertIn("declared merge check failed", injections[0]["payload"])
        finally:
            con.close()

    def test_dispatch_supervise_complete(self) -> None:
        """One default run, asserted in phases; the subTest names what broke.

        W-0176 phase: minted at launch into the worker's environment, revoked
        when the run turns terminal, and in no file anybody reads.
        DESIGN §7 phase: the supervisor's own tail is what fills the events
        table — no second tailer, no separate pass.
        Null-usage phase: the stub emits no step tokens here, which is exactly
        what a backend whose usage we cannot read looks like."""
        from orchestra import auth
        sink = self.tmp_path / "token-seen.txt"
        os.environ["ORCHESTRA_TEST_ENV_SINK"] = str(sink)
        run_id, _ = self._dispatch(mission="write the fix")
        self.assertIsNone(self._run_field(run_id, "run_token_hash"),
                          "not minted before launch")
        rc, run = self._run(run_id)

        with self.subTest("the run completes"):
            self.assertEqual(rc, 0)
            self.assertEqual(run["status"], "done")
            self.assertEqual(run["exit_code"], 0)
            self.assertEqual(run["session_ref"], "stub-session-1")
            self.assertIsNotNone(run["pid_identity"])
            # The stub hands off no findings/proposals block, so the completion
            # seam (DESIGN §9) records a protocol failure on top of the summary.
            self.assertTrue(run["summary"].startswith("stub work complete"))
            self.assertIn("handoff protocol failure", run["summary"])
            self.assertIsNotNone(run["finished_at"])
            brief_text = Path(run["brief_path"]).read_text()
            self.assertIn("write the fix", brief_text)
            self.assertIn("## Protocol", brief_text)

        with self.subTest("a run without usage records null not zero"):
            self.assertIsNone(run["tokens_total"])
            self.assertIsNone(run["cost_usd"])
            self.assertIsNone(run["usage_source"])

        with self.subTest("supervision normalizes the trace"):
            con = db.connect()
            events = traces.events_for_run(con, run_id)
            cursor = traces.cursor(con, run_id)
            con.close()
            self.assertIn(
                "stub work complete",
                [e["payload"] for e in events if e["kind"] == "assistant_text"])
            self.assertIn("lifecycle", {e["kind"] for e in events})
            self.assertEqual(cursor["byte_offset"],
                             Path(self.tmp_path / "home" / "logs" /
                                  f"run-{run_id}.jsonl").stat().st_size)

        with self.subTest("the run token dies with the run"):
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

        with self.subTest("finalize replay keeps the durable decision"):
            con = db.connect()
            note = con.execute(
                "SELECT body FROM messages WHERE run_id=? AND kind='completion'",
                (run_id,)).fetchone()
            # Recovery may replay the result seam. The durable decision wins and
            # the completion notice remains one fact, not one row per replay.
            con.execute("UPDATE runs SET tokens_in=11, tokens_out=7, tokens_total=18, "
                        "cost_usd=0.25, usage_source='preserved' WHERE id=?", (run_id,))
            con.commit()
            result = supervise.finalize_run(
                con, {**dict(run), "log_path": None}, "failed", 99)
            completions = con.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE run_id=? "
                "AND kind='completion'", (run_id,)).fetchone()["n"]
            con.close()
            self.assertIn("finished: done", note["body"])
            self.assertEqual((result["status"], result["exit_code"]), ("done", 0))
            self.assertEqual(completions, 1)
            self.assertEqual((result["tokens_in"], result["tokens_out"],
                              result["tokens_total"], result["cost_usd"],
                              result["usage_source"]),
                             (11, 7, 18, 0.25, "preserved"))

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

    def test_a_merge_failure_never_breaks_finalization(self) -> None:
        """W-0174: the landing seam runs at finalization, after the worktree
        is released. Whatever it hits, the run still completes and the reason
        lands on the summary."""
        run_id, _ = self._dispatch()
        con = db.connect()
        con.execute("UPDATE runs SET branch='orchestra/run-x' WHERE id=?", (run_id,))
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

        # A reaper has no worker log to parse, but it still gets a completion
        # record and a retry hold in the same terminal transaction.
        con = db.connect()
        orphan_id = int(con.execute(
            "INSERT INTO runs(profile, backend, requested_by, workdir, status, "
            "summary, started_at, finished_at) VALUES("
            "'stub','opencode','human',?,'failed','supervisor vanished',?,?)",
            (str(self.root), db.now(), db.now())).lastrowid)
        con.commit()
        orphan = con.execute("SELECT * FROM runs WHERE id=?", (orphan_id,)).fetchone()
        result = supervise.finalize_run(con, orphan, "failed", None)
        self.assertEqual(result["summary"], "supervisor vanished")
        self.assertIsNone(result["log_path"])
        self.assertEqual(con.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE run_id=? AND kind='completion'",
            (orphan_id,)).fetchone()["n"], 1)
        self.assertEqual(con.execute(
            "SELECT action FROM observations WHERE run_id=? AND layer='retry'",
            (orphan_id,)).fetchone()["action"], "deferred")
        con.close()

    def test_a_failed_run_says_why_it_died_not_its_last_words(self) -> None:
        """PREX3 run 64 died on "the model is currently at capacity" and the
        board showed a .gitignore fragment. The structured error was read
        only when the worker had emitted NO text at all — and a worker that
        dies mid-task has almost always emitted some. The reason outranks
        the last thing the model happened to say before it stopped.
        """
        log = self.root / "run-64.jsonl"
        log.write_text(
            json.dumps({"type": "message",
                        "text": "# Godot generated files\n.godot/"}) + "\n"
            + json.dumps({"type": "error", "error": {
                "message": "The model is currently at capacity due to high "
                           "demand. Please try again in a few minutes."}}) + "\n")
        con = db.connect()
        run_id = int(con.execute(
            "INSERT INTO runs(profile, backend, requested_by, workdir, status, "
            "log_path, started_at) VALUES('stub','opencode','human',?,'running',?,?)",
            (str(self.root), str(log), db.now())).lastrowid)
        con.commit()
        run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        result = supervise.finalize_run(con, run, "failed", 1)
        self.assertIn("at capacity", result["summary"])
        self.assertNotIn("Godot generated files", result["summary"])
        con.close()

    def test_a_run_that_finished_keeps_its_closing_words(self) -> None:
        """The precedence is for the DEAD only: a run that ended on its own
        terms still reports what it said, even when the log holds an error it
        recovered from."""
        log = self.root / "run-ok.jsonl"
        log.write_text(
            json.dumps({"type": "error", "error": {"message": "transient"}}) + "\n"
            + json.dumps({"type": "message", "text": "Pack builds."}) + "\n")
        con = db.connect()
        run_id = int(con.execute(
            "INSERT INTO runs(profile, backend, requested_by, workdir, status, "
            "log_path, started_at) VALUES('stub','opencode','human',?,'running',?,?)",
            (str(self.root), str(log), db.now())).lastrowid)
        con.commit()
        run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        result = supervise.finalize_run(con, run, "done", 0)
        self.assertEqual("Pack builds.", result["summary"])
        con.close()

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
        os.environ["STUB_STEPS"] = "40"
        os.environ["STUB_STEP_SLEEP"] = "0.3"  # ~12s of step_finish boundaries
        run_id, _ = self._dispatch()
        thread = self._supervise_bg(run_id)
        try:
            session_ref = self._wait_until(
                lambda: self._run_field(run_id, "session_ref"))
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
        thread = self._supervise_bg(run_id)
        try:
            status = self._wait_until(lambda: self._run_field(run_id, "status"),
                                      done=lambda s: s == "running")
            self.assertEqual(status, "running")
            cli.cmd_kill(Namespace(run_id=run_id))
        finally:
            thread.join(timeout=20)
        self.assertFalse(thread.is_alive())
        con = db.connect()
        run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        completion = con.execute(
            "SELECT COUNT(*) FROM messages WHERE run_id=? AND kind='completion'",
            (run_id,)).fetchone()[0]
        con.close()
        self.assertEqual(run["status"], "killed")
        self.assertIsNotNone(run["finished_at"])
        self.assertEqual(completion, 1)

        con = db.connect()
        reused = int(con.execute(
            "INSERT INTO runs(profile, backend, requested_by, workdir, status, "
            "pid, pid_identity, started_at) VALUES"
            "('stub','opencode','human',?,'running',4242,'old-owner',?)",
            (str(self.root), db.now())).lastrowid)
        con.commit()
        con.close()
        with mock.patch.object(cli.proc, "signal_owned_group",
                               return_value=("refused", "identity changed")) as signal:
            cli.cmd_kill(Namespace(run_id=reused))
        signal.assert_called_once_with(4242, "old-owner", signal_module.SIGTERM)
        con = db.connect()
        reused_row = con.execute(
            "SELECT status, worker_status FROM runs WHERE id=?", (reused,)).fetchone()
        completion = con.execute(
            "SELECT COUNT(*) FROM messages WHERE run_id=? AND kind='completion'",
            (reused,)).fetchone()[0]
        con.close()
        self.assertEqual((reused_row["status"], reused_row["worker_status"]),
                         ("killed", "killed"))
        self.assertEqual(completion, 0)

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


class FollowupAfterCleanupTests(unittest.TestCase):
    """A follow-up to finished work must not inherit a released worktree.

    Live failure (run 9): its parent had completed, so cleanup gave the
    worktree back and the merge deleted the branch. The follow-up copied both
    paths, the supervisor died the moment it tried to stand in a directory
    that was gone, and the run sat in `spawning` until the reaper caught it.
    """

    def setUp(self) -> None:
        self.state = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(
            os.environ, {"ORCHESTRA_HOME": str(Path(self.state.name) / "home")})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.state.cleanup()

    def test_a_gone_worktree_is_replaced_not_inherited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _seed_repo(tmp)
            (root / ".codex").mkdir()
            (root / ".codex" / "context.txt").write_text("codex context\n")

            con = db.connect()
            try:
                parent = _finished_parent(con, root, "human")
                self.assertFalse(Path(parent["workdir"]).exists())

                run_id = supervise.create_followup(
                    con, root, parent, "human", "carry on")
                child = con.execute("SELECT * FROM runs WHERE id=?",
                                    (run_id,)).fetchone()
                self.assertNotEqual(child["workdir"], parent["workdir"])
                self.assertEqual(child["branch"], f"orchestra/run-{run_id}")
                self.assertTrue(Path(child["workdir"]).exists(),
                                "a follow-up must have somewhere to stand")
                self.assertEqual(child["session_ref"], parent["session_ref"],
                                 "the conversation still resumes")
                self.assertEqual(
                    (Path(child["workdir"]) / ".codex" / "context.txt").read_text(),
                    "codex context\n")
            finally:
                con.close()

    def test_launch_setup_failure_discards_its_new_worktree_and_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _seed_repo(tmp)
            con = db.connect()
            try:
                cur = con.execute(
                    "INSERT INTO runs(profile, backend, requested_by, workdir, "
                    "status, started_at) VALUES('p','codex','human',?,"
                    "'spawning',?)", (str(root), db.now()))
                run = con.execute("SELECT * FROM runs WHERE id=?",
                                  (cur.lastrowid,)).fetchone()
                cfg = {"profiles": {"p": {"backend": "codex"}}}
                with mock.patch.object(supervise.brief, "compose",
                                       side_effect=RuntimeError("brief failed")), \
                        self.assertRaisesRegex(RuntimeError, "brief failed"):
                    supervise.prepare_launch(
                        con, root, cfg, run, mission="x", use_worktree=True)
                row = con.execute("SELECT * FROM runs WHERE id=?",
                                  (run["id"],)).fetchone()
                self.assertEqual(row["workdir"], str(root))
                self.assertIsNone(row["branch"])
                branches = subprocess.run(
                    ["git", "-C", str(root), "branch", "--list", "orchestra/run-*"],
                    check=True, capture_output=True, text=True).stdout
                self.assertEqual(branches.strip(), "")
                worktrees = subprocess.run(
                    ["git", "-C", str(root), "worktree", "list", "--porcelain"],
                    check=True, capture_output=True, text=True).stdout
                self.assertNotIn("/run-", worktrees)
                con.execute("UPDATE runs SET status='killed', summary='owner stop', "
                            "finished_at=? WHERE id=?", (db.now(), run["id"]))
                con.commit()
                supervise.fail_launch(con, root, run["id"], "late setup error")
                stopped = con.execute("SELECT * FROM runs WHERE id=?",
                                      (run["id"],)).fetchone()
                self.assertEqual((stopped["status"], stopped["summary"]),
                                 ("killed", "owner stop"))
            finally:
                con.close()

    def test_a_gone_isolated_worktree_never_falls_back_to_shared(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _seed_repo(tmp)
            con = db.connect()
            try:
                parent = _finished_parent(con, root, "work")
                before = con.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"]
                with mock.patch.object(worktree, "create",
                                       side_effect=SystemExit("cannot isolate")), \
                        self.assertRaisesRegex(SystemExit, "cannot isolate"):
                    supervise.create_followup(con, root, parent, "work", "carry on")
                rows = list(con.execute("SELECT * FROM runs ORDER BY id"))
                self.assertEqual(len(rows), before + 1)
                failed = rows[-1]
                self.assertEqual(failed["status"], "failed")
                self.assertIn("cannot isolate", failed["summary"])
                self.assertIsNone(failed["branch"])
                self.assertEqual(failed["workdir"], str(root))

                next_id = supervise.create_followup(
                    con, root, failed, "work", "try once more")
                resumed = con.execute("SELECT * FROM runs WHERE id=?",
                                      (next_id,)).fetchone()
                self.assertNotEqual(resumed["workdir"], str(root))
                self.assertEqual(resumed["branch"], f"orchestra/run-{next_id}")
                self.assertTrue(Path(resumed["workdir"]).exists())
            finally:
                con.close()


if __name__ == "__main__":
    unittest.main()
