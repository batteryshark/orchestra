from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from orchestra_cli import cli, db, runners, supervise
from orchestra_cli.usage.models import ProviderResult, QuotaWindow


class CommandPreviewTests(unittest.TestCase):
    def test_redacts_claude_prompt_from_log_preview(self) -> None:
        preview = supervise._command_preview([
            "claude", "-p", "secret\nmultiline brief",
            "--output-format", "stream-json", "--verbose",
        ])

        self.assertEqual(
            preview,
            "claude -p <prompt> --output-format stream-json --verbose ...",
        )
        self.assertNotIn("secret", preview)


class CheckpointCommitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "tracked.txt").write_text("base\n")
        subprocess.run(["git", "-C", str(self.root), "add", "tracked.txt"], check=True)
        subprocess.run([
            "git", "-C", str(self.root),
            "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "-q", "-m", "base",
        ], check=True)
        self.base = subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_isolated_worktree_changes_are_checkpointed(self) -> None:
        isolated = self.root / "isolated"
        subprocess.run([
            "git", "-C", str(self.root), "worktree", "add", "-q", "-b", "run-7",
            str(isolated),
        ], check=True)
        (isolated / "tracked.txt").write_text("changed\n")
        (isolated / "new.txt").write_text("new\n")
        (isolated / ".agents").mkdir()
        (isolated / ".agents" / "local.md").write_text("copied context\n")

        checkpoint = supervise._checkpoint_commit({
            "id": 7,
            "workdir": str(isolated),
            "branch": "run-7",
            "writes_tree": 1,
            "base_commit": self.base,
        }, "done")

        names = subprocess.run(
            ["git", "-C", str(isolated), "show", "--pretty=", "--name-only", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.splitlines()
        self.assertNotEqual(checkpoint, self.base)
        self.assertEqual(set(names), {"new.txt", "tracked.txt"})
        self.assertTrue((isolated / ".agents" / "local.md").exists())

    def test_shared_tree_dirty_output_is_not_auto_committed(self) -> None:
        (self.root / "tracked.txt").write_text("uncommitted\n")
        with self.assertRaisesRegex(RuntimeError, "shared-tree writer left uncommitted"):
            supervise._checkpoint_commit({
                "id": 8,
                "workdir": str(self.root),
                "branch": None,
                "writes_tree": 1,
                "base_commit": self.base,
            }, "done")
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(self.root), "rev-parse", "HEAD"],
                check=True, capture_output=True, text=True,
            ).stdout.strip(),
            self.base,
        )


def _project(*, checkin_interval: int = 0, timeout: int = 30) -> tuple[tempfile.TemporaryDirectory, Path]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / ".orchestra").mkdir(parents=True, exist_ok=True)
    (root / ".orchestra" / "config.toml").write_text(
        "[settings]\n"
        f"timeout = {timeout}\n"
        f"supervisor_checkin_interval = {checkin_interval}\n"
        "\n[agents.glm]\n"
        'backend = "opencode"\n'
        'model = "zhipuai-coding-plan/glm-5.2"\n'
    )
    db.connect(root).close()
    return tmp, root


def _insert_run(root: Path, *, agent: str = "glm", model: str = "zhipuai-coding-plan/glm-5.2",
                started_at: str | None = None) -> int:
    brief_path = root / "brief.md"
    log_path = root / "run.jsonl"
    brief_path.write_text("prompt")
    log_path.touch()
    con = db.connect(root)
    try:
        cur = con.execute(
            "INSERT INTO runs(agent, backend, model, title, requested_by, brief_path, "
            "log_path, workdir, status, started_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (agent, "opencode", model, "quota test", "codex", str(brief_path),
             str(log_path), str(root), "spawning", started_at or db.now()),
        )
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def _sleeping_worker(*, line: dict | None = None, seconds: int = 60) -> list[str]:
    code = "import json,time;"
    if line is not None:
        code += f"print(json.dumps({line!r}), flush=True);"
    code += f"time.sleep({seconds})"
    return [sys.executable, "-c", code]


class VerificationOutcomeTests(unittest.TestCase):
    def test_supervisor_rechecks_capabilities_before_continuation_launch(self) -> None:
        tmp, root = _project(checkin_interval=0)
        try:
            run_id = _insert_run(root)
            con = db.connect(root)
            try:
                con.execute(
                    "UPDATE runs SET required_capabilities_json='[\"window-server\"]' "
                    "WHERE id=?",
                    (run_id,),
                )
                con.commit()
            finally:
                con.close()
            with mock.patch.object(
                supervise.runners, "build_cmd",
                side_effect=AssertionError("worker must not launch without fresh evidence"),
            ):
                self.assertEqual(supervise.supervise(root, run_id), 1)
            con = db.connect(root)
            try:
                run = con.execute(
                    "SELECT status, summary FROM runs WHERE id=?", (run_id,)
                ).fetchone()
            finally:
                con.close()
            self.assertEqual(run["status"], "failed")
            self.assertIn("missing=window-server", run["summary"])
        finally:
            tmp.cleanup()

    def test_required_run_without_handoff_outcome_finishes_unverified(self) -> None:
        tmp, root = _project(checkin_interval=0)
        try:
            run_id = _insert_run(root)
            con = db.connect(root)
            try:
                con.execute(
                    "UPDATE runs SET verification_required=1, verification_status='pending' "
                    "WHERE id=?",
                    (run_id,),
                )
                con.commit()
            finally:
                con.close()
            with mock.patch.object(
                supervise.runners, "build_cmd", return_value=[sys.executable, "-c", "pass"]
            ):
                self.assertEqual(supervise.supervise(root, run_id), 0)
            con = db.connect(root)
            try:
                run = con.execute(
                    "SELECT status, verification_status FROM runs WHERE id=?", (run_id,)
                ).fetchone()
                message = con.execute(
                    "SELECT body FROM messages WHERE recipient='codex' AND run_id=? "
                    "ORDER BY id DESC LIMIT 1",
                    (run_id,),
                ).fetchone()
            finally:
                con.close()
            self.assertEqual(run["status"], "done")
            self.assertEqual(run["verification_status"], "unverified")
            self.assertIn("marked unverified", message["body"])
        finally:
            tmp.cleanup()

    def test_late_verified_handoff_is_not_overwritten_at_finalization(self) -> None:
        tmp, root = _project(checkin_interval=0)
        try:
            run_id = _insert_run(root)
            con = db.connect(root)
            try:
                con.execute(
                    "UPDATE runs SET verification_required=1, verification_status='pending' "
                    "WHERE id=?",
                    (run_id,),
                )
                con.commit()
            finally:
                con.close()

            def parse_log(_path: str) -> tuple[None, str]:
                # This models a handoff recorded after supervise refreshed its
                # run row, but before it writes the terminal outcome.
                handoff_con = db.connect(root)
                try:
                    handoff_con.execute(
                        "UPDATE runs SET verification_status='verified' WHERE id=?",
                        (run_id,),
                    )
                    handoff_con.commit()
                finally:
                    handoff_con.close()
                return None, "verified handoff"

            with mock.patch.object(
                supervise.runners, "build_cmd", return_value=[sys.executable, "-c", "pass"]
            ), mock.patch.object(supervise.runners, "parse_log", side_effect=parse_log):
                self.assertEqual(supervise.supervise(root, run_id), 0)
            con = db.connect(root)
            try:
                status = con.execute(
                    "SELECT verification_status FROM runs WHERE id=?", (run_id,)
                ).fetchone()["verification_status"]
            finally:
                con.close()
            self.assertEqual(status, "verified")
        finally:
            tmp.cleanup()


class SupervisorUsageLimitTests(unittest.TestCase):
    def tearDown(self) -> None:
        if hasattr(self, "tmp"):
            self.tmp.cleanup()

    def test_usage_limit_log_terminates_without_waiting_for_run_timeout(self) -> None:
        self.tmp, root = _project(checkin_interval=0, timeout=30)
        run_id = _insert_run(root)
        line = {"type": "error", "message": "Usage limit exceeded for Z.AI coding plan"}

        started = time.monotonic()
        with mock.patch.object(supervise, "PROC_POLL_INTERVAL", 0.05), \
                mock.patch.object(supervise.runners, "build_cmd",
                                  return_value=_sleeping_worker(line=line)):
            rc = supervise.supervise(root, run_id)
        elapsed = time.monotonic() - started

        self.assertEqual(rc, 1)
        self.assertLess(elapsed, 5)
        con = db.connect(root)
        try:
            row = con.execute("SELECT status, summary, finished_at FROM runs WHERE id=?",
                              (run_id,)).fetchone()
            message = con.execute(
                "SELECT body FROM messages WHERE recipient='codex' AND run_id=? "
                "ORDER BY id DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        finally:
            con.close()
        self.assertEqual(row["status"], "failed")
        self.assertIsNotNone(row["finished_at"])
        self.assertIn("Provider usage limit exhausted", row["summary"])
        self.assertIn("reroute the work to another agent", message["body"])

    def test_structured_claude_five_hour_limit_is_authoritative(self) -> None:
        with tempfile.NamedTemporaryFile("w+", suffix=".jsonl") as log:
            events = [
                {
                    "type": "rate_limit_event",
                    "rate_limit_info": {
                        "status": "rejected",
                        "resetsAt": 1_784_955_600,
                        "rateLimitType": "five_hour",
                    },
                },
                {
                    "type": "assistant",
                    "error": "rate_limit",
                    "message": {
                        "content": [{
                            "type": "text",
                            "text": "You've hit your monthly spend limit",
                        }],
                    },
                },
            ]
            log.write("\n".join(json.dumps(event) for event in events))
            log.flush()

            text = supervise._usage_limit_text(log.name)

        self.assertIn("Claude 5-hour usage limit reached", text)
        self.assertNotIn("monthly spend", text)

    def test_usage_limit_mentions_in_non_error_events_do_not_trigger(self) -> None:
        self.tmp, root = _project(checkin_interval=0, timeout=30)
        log_path = root / "run.jsonl"
        rows = [
            {"type": "message", "text": "provider quota exhausted appears in the prompt"},
            {"type": "tool_result", "output": "Usage limit exceeded was found by rg"},
            {"type": "assistant", "text": "I will handle provider usage exhaustion"},
        ]
        log_path.write_text("\n".join(json.dumps(row) for row in rows))

        self.assertIsNone(supervise._usage_limit_text(str(log_path)))

    def test_silent_zero_headroom_session_returns_as_usage_limit(self) -> None:
        self.tmp, root = _project(checkin_interval=1, timeout=30)
        run_id = _insert_run(root)
        minimax_collector = mock.Mock(side_effect=AssertionError("must not collect non-target"))
        zai_collector = mock.Mock(return_value=ProviderResult(
            id="zai",
            name="Z.AI",
            status="ok",
            windows=[
                QuotaWindow.from_remaining(
                    id="daily",
                    label="Daily",
                    scope="Coding",
                    remaining_percent=0,
                )
            ],
        ))

        started = time.monotonic()
        with mock.patch.object(supervise, "PROC_POLL_INTERVAL", 0.05), \
                mock.patch.object(supervise, "DEFAULT_COLLECTORS", (
                    ("minimax", "MiniMax", minimax_collector),
                    ("zai", "Z.AI", zai_collector),
                )), \
                mock.patch.object(supervise.runners, "build_cmd",
                                  return_value=_sleeping_worker(
                                      line={"sessionID": "ses-zero-headroom"})):
            rc = supervise.supervise(root, run_id)
        elapsed = time.monotonic() - started

        self.assertEqual(rc, 1)
        self.assertLess(elapsed, 5)
        con = db.connect(root)
        try:
            row = con.execute("SELECT status, session_ref, summary FROM runs WHERE id=?",
                              (run_id,)).fetchone()
            checkins = con.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE recipient='glm' AND kind='checkin'"
            ).fetchone()["n"]
        finally:
            con.close()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["session_ref"], "ses-zero-headroom")
        self.assertIn("Z.AI coding headroom is 0%", row["summary"])
        self.assertEqual(checkins, 0)
        zai_collector.assert_called_once_with()


class SupervisorStallTimeoutTests(unittest.TestCase):
    def tearDown(self) -> None:
        if hasattr(self, "tmp"):
            self.tmp.cleanup()

    def test_productive_worker_can_outlive_stall_window(self) -> None:
        self.tmp, root = _project(checkin_interval=0, timeout=10)
        run_id = _insert_run(root)
        con = db.connect(root)
        try:
            run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            code = (
                "import time;"
                "[(print(i,flush=True),time.sleep(.08)) for i in range(6)]"
            )
            outcome, exit_code = supervise._run_proc(
                con,
                run,
                [sys.executable, "-c", code],
                str(root),
                {},
                run["log_path"],
                run_id,
                time.time() + 3,
                stall_timeout=0.15,
                poll_interval=0.02,
            )
        finally:
            con.close()

        self.assertEqual((outcome, exit_code), ("exit", 0))

    def test_silent_worker_times_out_with_durable_reason(self) -> None:
        self.tmp, root = _project(checkin_interval=0, timeout=10)
        config_path = root / ".orchestra" / "config.toml"
        config_path.write_text(config_path.read_text() + "stall_timeout = 1\n")
        run_id = _insert_run(root)

        started = time.monotonic()
        with mock.patch.object(supervise, "PROC_POLL_INTERVAL", 0.05), \
                mock.patch.object(supervise.runners, "build_cmd",
                                  return_value=_sleeping_worker(seconds=10)):
            rc = supervise.supervise(root, run_id)
        elapsed = time.monotonic() - started

        con = db.connect(root)
        try:
            row = con.execute(
                "SELECT status, summary FROM runs WHERE id=?", (run_id,)
            ).fetchone()
        finally:
            con.close()
        self.assertEqual(rc, 1)
        self.assertEqual(row["status"], "timeout")
        self.assertIn("Stalled: no worker output", row["summary"])
        self.assertLess(elapsed, 3)

    def test_supervisor_checkin_does_not_reset_worker_stall_clock(self) -> None:
        self.tmp, root = _project(checkin_interval=0, timeout=10)
        run_id = _insert_run(root)
        con = db.connect(root)
        started = time.monotonic()
        try:
            run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            outcome, exit_code = supervise._run_proc(
                con,
                run,
                _sleeping_worker(line={"sessionID": "ses-silent"}, seconds=5),
                str(root),
                {},
                run["log_path"],
                run_id,
                time.time() + 3,
                checkin_interval=0.15,
                checkin_state={"last_sent_at": time.time()},
                stall_timeout=0.4,
                poll_interval=0.01,
            )
            checkins = con.execute(
                "SELECT COUNT(*) FROM messages WHERE run_id=? AND kind='checkin'",
                (run_id,),
            ).fetchone()[0]
        finally:
            con.close()
        elapsed = time.monotonic() - started

        self.assertEqual((outcome, exit_code), ("timeout", None))
        self.assertEqual(checkins, 1)
        # Before the fix, the supervisor-authored check-in counted as worker
        # output and this took roughly 0.55s (check-in + full stall window).
        self.assertLess(elapsed, 0.52)

    def test_stall_timeout_validation(self) -> None:
        self.assertIsNone(supervise._stall_timeout_seconds(0))
        self.assertEqual(supervise._stall_timeout_seconds("30"), 30)
        with self.assertRaisesRegex(SystemExit, "zero or a positive"):
            supervise._stall_timeout_seconds(-1)


class SupervisorCheckinTests(unittest.TestCase):
    def tearDown(self) -> None:
        if hasattr(self, "tmp"):
            self.tmp.cleanup()

    def test_periodic_checkin_interrupts_once_and_resumes_same_session(self) -> None:
        self.tmp, root = _project(checkin_interval=1, timeout=30)
        run_id = _insert_run(root)
        con = db.connect(root)
        try:
            con.execute(
                "INSERT INTO messages(sender, recipient, body, run_id, created_at) "
                "VALUES('reviewer', 'glm', 'ordinary inbox note', ?, ?)",
                (run_id, db.now()),
            )
            con.commit()
        finally:
            con.close()
        zai_collector = mock.Mock(return_value=ProviderResult(
            id="zai",
            name="Z.AI",
            status="ok",
            windows=[
                QuotaWindow.from_remaining(
                    id="daily",
                    label="Daily",
                    scope="Coding",
                    remaining_percent=80,
                )
            ],
        ))
        calls: list[tuple[str | None, str]] = []

        def build_cmd(agent, *, workdir, title, prompt, resume_ref=None, add_dirs=None, attach=None):
            calls.append((resume_ref, prompt))
            if resume_ref is None:
                code = (
                    "import json,time;"
                    "print(json.dumps('tools'),flush=True);"
                    "print(json.dumps({'sessionID':'ses-checkin'}),flush=True);"
                    "time.sleep(1.2);"
                    "print(json.dumps({'type':'step_finish','part':"
                    "{'type':'step-finish'}}),flush=True);"
                    "time.sleep(60)"
                )
                return [sys.executable, "-c", code]
            code = (
                "import json;"
                f"print(json.dumps({{'sessionID':'ses-checkin','text':'HANDOFF run {run_id}: done'}}))"
            )
            return [sys.executable, "-c", code]

        with mock.patch.object(supervise, "PROC_POLL_INTERVAL", 0.05), \
                mock.patch.object(supervise, "DEFAULT_COLLECTORS", (
                    ("zai", "Z.AI", zai_collector),
                )), \
                mock.patch.object(supervise.runners, "build_cmd", side_effect=build_cmd):
            rc = supervise.supervise(root, run_id)

        self.assertEqual(rc, 0)
        self.assertEqual([c[0] for c in calls], [None, "ses-checkin"])
        self.assertIn("PROGRESS CHECK-IN", calls[1][1])
        for checkpoint in (
            "current hypothesis",
            "new evidence",
            "cheapest untried falsification",
            "acceptance evidence status",
            "environment blocker",
            "next bounded step",
        ):
            self.assertIn(checkpoint, calls[1][1])
        self.assertIn("No inbox lookup is needed", calls[1][1])
        self.assertNotIn("orchestra inbox", calls[1][1])
        con = db.connect(root)
        try:
            row = con.execute("SELECT status, session_ref, summary FROM runs WHERE id=?",
                              (run_id,)).fetchone()
            checkins = list(con.execute(
                "SELECT body, delivery_offset, delivered_at, read_at FROM messages "
                "WHERE recipient='glm' AND kind='checkin'"
            ))
            ordinary = con.execute(
                "SELECT read_at FROM messages WHERE body='ordinary inbox note'"
            ).fetchone()
        finally:
            con.close()
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["session_ref"], "ses-checkin")
        self.assertEqual(len(checkins), 1)
        self.assertIn("PROGRESS CHECK-IN", checkins[0]["body"])
        self.assertIsNotNone(checkins[0]["delivery_offset"])
        self.assertIsNotNone(checkins[0]["delivered_at"])
        self.assertIsNotNone(checkins[0]["read_at"])
        self.assertIsNone(ordinary["read_at"])
        delivery_events = [
            json.loads(line)
            for line in (root / "run.jsonl").read_text().splitlines()
            if line.startswith('{"type":"orchestra.delivery"')
        ]
        self.assertEqual([event["phase"] for event in delivery_events],
                         ["pending", "delivered"])
        self.assertEqual(len({event["message_id"] for event in delivery_events}), 1)
        self.assertIsInstance(delivery_events[0]["message_id"], int)
        self.assertEqual(delivery_events[0]["delivery"], "checkin")
        self.assertEqual(delivery_events[0]["sender"], "orchestra")
        self.assertEqual(delivery_events[0]["recipient"], "glm")


class ReplyRecoveryTests(unittest.TestCase):
    def test_reply_recovers_orphaned_interrupt_as_session_followup(self) -> None:
        tmp, root = _project(checkin_interval=0)
        self.addCleanup(tmp.cleanup)
        run_id = _insert_run(root)
        con = db.connect(root)
        try:
            con.execute(
                "UPDATE runs SET status='interrupt', session_ref='ses-orphan', pid=4321 "
                "WHERE id=?",
                (run_id,),
            )
            con.commit()
        finally:
            con.close()

        cfg = {"settings": {"default_requester": "orchestrator"}}
        args = Namespace(
            run_id=run_id,
            message=["Continue", "after", "the", "check-in"],
            as_="claude",
            sync=False,
        )
        with mock.patch.object(cli.paths, "find_root", return_value=root), \
                mock.patch.object(cli.config, "load", return_value=cfg), \
                mock.patch.object(cli, "_spawn_supervisor") as spawn, \
                mock.patch("builtins.print"):
            cli.cmd_reply(args)

        con = db.connect(root)
        try:
            original = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            followup = con.execute(
                "SELECT * FROM runs WHERE parent_run=? ORDER BY id DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        finally:
            con.close()
        self.assertEqual(original["status"], "killed")
        self.assertIsNotNone(original["finished_at"])
        self.assertEqual(followup["session_ref"], "ses-orphan")
        self.assertEqual(followup["status"], "spawning")
        spawn.assert_called_once_with(root, followup["id"])


class InterruptMessageTests(unittest.TestCase):
    def test_safe_interrupt_rejects_legacy_detached_supervisor(self) -> None:
        tmp, root = _project(checkin_interval=0)
        self.addCleanup(tmp.cleanup)
        run_id = _insert_run(root)
        con = db.connect(root)
        try:
            con.execute("UPDATE runs SET status='running', session_ref='ses-old' WHERE id=?",
                        (run_id,))
            con.commit()
        finally:
            con.close()
        cfg = {
            "settings": {"default_requester": "orchestrator"},
            "agents": {"glm": {"backend": "opencode"}},
        }
        args = Namespace(run_id=run_id, message=["Change", "direction"], as_="claude",
                         now=False)
        with mock.patch.object(cli.paths, "find_root", return_value=root), \
                mock.patch.object(cli.config, "load", return_value=cfg):
            with self.assertRaisesRegex(SystemExit, "predates safe interrupts"):
                cli.cmd_interrupt(args)

        con = db.connect(root)
        try:
            count = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(count, 0)

    def test_cli_records_interrupt_as_typed_inbox_delivery(self) -> None:
        tmp, root = _project(checkin_interval=0)
        self.addCleanup(tmp.cleanup)
        run_id = _insert_run(root)
        con = db.connect(root)
        try:
            con.execute("UPDATE runs SET status='running', session_ref='ses-123', "
                        "supervisor_protocol=1 WHERE id=?",
                        (run_id,))
            con.commit()
        finally:
            con.close()

        cfg = {
            "settings": {"default_requester": "orchestrator"},
            "agents": {"glm": {"backend": "opencode"}},
        }
        args = Namespace(run_id=run_id, message=["Check", "your", "inbox"], as_="claude",
                         now=False)
        with mock.patch.object(cli.paths, "find_root", return_value=root), \
                mock.patch.object(cli.config, "load", return_value=cfg), \
                mock.patch("builtins.print"):
            cli.cmd_interrupt(args)

        con = db.connect(root)
        try:
            message = con.execute(
                "SELECT id, sender, recipient, body, kind, created_at, delivery_offset, "
                "delivered_at "
                "FROM messages WHERE run_id=?",
                (run_id,),
            ).fetchone()
            status = con.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()[0]
        finally:
            con.close()
        self.assertEqual({key: message[key] for key in ("sender", "recipient", "body", "kind")}, {
            "sender": "claude",
            "recipient": "glm",
            "body": "[INTERRUPT] Check your inbox",
            "kind": "interrupt",
        })
        self.assertEqual(status, "running")
        self.assertIsNotNone(message["delivery_offset"])
        self.assertIsNone(message["delivered_at"])
        delivery_events = [
            json.loads(line)
            for line in (root / "run.jsonl").read_text().splitlines()
            if line.startswith('{"type":"orchestra.delivery"')
        ]
        self.assertEqual(len(delivery_events), 1)
        self.assertEqual(delivery_events[0]["message_id"], message["id"])
        self.assertEqual(delivery_events[0]["delivery"], "interrupt")
        self.assertEqual(delivery_events[0]["created_at"], message["created_at"])
        self.assertEqual(delivery_events[0]["phase"], "pending")

    def test_now_preserves_immediate_stop_behavior(self) -> None:
        tmp, root = _project(checkin_interval=0)
        self.addCleanup(tmp.cleanup)
        run_id = _insert_run(root)
        con = db.connect(root)
        try:
            con.execute("UPDATE runs SET status='running', session_ref='ses-123', pid=4321 "
                        "WHERE id=?", (run_id,))
            con.commit()
        finally:
            con.close()
        cfg = {
            "settings": {"default_requester": "orchestrator"},
            "agents": {"glm": {"backend": "opencode"}},
        }
        args = Namespace(run_id=run_id, message=["Stop", "now"], as_="claude", now=True)
        with mock.patch.object(cli.paths, "find_root", return_value=root), \
                mock.patch.object(cli.config, "load", return_value=cfg), \
                mock.patch.object(cli.os, "killpg") as killpg, \
                mock.patch("builtins.print"):
            cli.cmd_interrupt(args)

        con = db.connect(root)
        try:
            run = con.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
            message = con.execute(
                "SELECT delivered_at FROM messages WHERE run_id=?", (run_id,)
            ).fetchone()
        finally:
            con.close()
        self.assertEqual(run["status"], "interrupt")
        self.assertIsNotNone(message["delivered_at"])
        killpg.assert_called_once_with(4321, cli.signal.SIGTERM)


class SafeBoundaryTests(unittest.TestCase):
    def test_recognizes_backend_action_completion_events(self) -> None:
        self.assertTrue(supervise._is_safe_boundary("opencode", {
            "type": "step_finish", "part": {"type": "step-finish"},
        }))
        self.assertTrue(supervise._is_safe_boundary("codex", {
            "type": "item.completed",
            "item": {"type": "file_change", "status": "completed"},
        }))
        self.assertTrue(supervise._is_safe_boundary("claude", {
            "type": "user",
            "message": {"content": [{"type": "tool_result"}]},
        }))

    def test_does_not_treat_started_tool_or_reasoning_as_safe(self) -> None:
        self.assertFalse(supervise._is_safe_boundary("codex", {
            "type": "item.started", "item": {"type": "command_execution"},
        }))
        self.assertFalse(supervise._is_safe_boundary("opencode", {
            "part": {"type": "reasoning", "text": "still working"},
        }))

    def test_oversized_log_event_cannot_stall_boundary_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "worker.jsonl"
            boundary = json.dumps({
                "type": "step_finish", "part": {"type": "step-finish"},
            })
            log_path.write_text("x" * 140 + "\n" + boundary + "\n")
            offset = 0
            observed = []
            for _ in range(10):
                events, next_offset = supervise._read_log_events(
                    str(log_path), offset, max_bytes=128
                )
                observed.extend(events)
                self.assertGreaterEqual(next_offset, offset)
                offset = next_offset
                if any(supervise._is_safe_boundary("opencode", event)
                       for event in observed):
                    break
            self.assertTrue(any(supervise._is_safe_boundary("opencode", event)
                                for event in observed))

    def test_natural_exit_delivers_pending_interrupt_as_immediate_resume(self) -> None:
        tmp, root = _project(checkin_interval=0, timeout=10)
        self.addCleanup(tmp.cleanup)
        run_id = _insert_run(root)
        calls: list[tuple[str | None, str]] = []

        def build_cmd(agent, *, workdir, title, prompt, resume_ref=None,
                      add_dirs=None, attach=None):
            calls.append((resume_ref, prompt))
            if resume_ref is None:
                code = (
                    "import json,time;"
                    "print(json.dumps({'sessionID':'ses-natural'}),flush=True);"
                    "time.sleep(1)"
                )
            else:
                code = (
                    "import json;"
                    f"print(json.dumps({{'sessionID':'ses-natural',"
                    f"'text':'HANDOFF run {run_id}: done'}}))"
                )
            return [sys.executable, "-c", code]

        cfg = {
            "settings": {
                "default_requester": "orchestrator",
                "timeout": 10,
                "supervisor_checkin_interval": 0,
            },
            "agents": {"glm": {"backend": "opencode", "timeout": 10}},
        }
        result: list[int] = []
        with mock.patch.object(supervise, "PROC_POLL_INTERVAL", 0.05), \
                mock.patch.object(supervise.config, "load", return_value=cfg), \
                mock.patch.object(supervise.runners, "build_cmd", side_effect=build_cmd), \
                mock.patch.object(cli.paths, "find_root", return_value=root), \
                mock.patch.object(cli.config, "load", return_value=cfg), \
                mock.patch("builtins.print"):
            thread = threading.Thread(
                target=lambda: result.append(supervise.supervise(root, run_id)), daemon=True
            )
            thread.start()
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                con = db.connect(root)
                try:
                    row = con.execute(
                        "SELECT session_ref, supervisor_protocol FROM runs WHERE id=?",
                        (run_id,),
                    ).fetchone()
                finally:
                    con.close()
                if row["session_ref"] and row["supervisor_protocol"] == 1:
                    break
                time.sleep(0.02)
            else:
                self.fail("supervisor did not expose a resumable session")

            cli.cmd_interrupt(Namespace(
                run_id=run_id, message=["Apply", "this"], as_="claude", now=False,
            ))
            thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [0])
        self.assertEqual([call[0] for call in calls], [None, "ses-natural"])
        self.assertIn("Apply this", calls[1][1])
        self.assertNotIn("orchestra inbox", calls[1][1])
        con = db.connect(root)
        try:
            message = con.execute(
                "SELECT delivered_at, read_at FROM messages WHERE kind='interrupt'"
            ).fetchone()
        finally:
            con.close()
        self.assertIsNotNone(message["delivered_at"])
        self.assertIsNotNone(message["read_at"])


class BlockingQuestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp, self.root = _project(checkin_interval=0)
        self.run_id = _insert_run(self.root)
        con = db.connect(self.root)
        try:
            con.execute(
                "UPDATE runs SET status='running', session_ref='ses-question', "
                "allow_question=1, question_wait_seconds=60 WHERE id=?",
                (self.run_id,),
            )
            con.commit()
        finally:
            con.close()
        self.cfg = {
            "settings": {"default_requester": "orchestrator"},
            "agents": {"glm": {"backend": "opencode"}},
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_ask_pauses_and_answer_resolves_the_one_question(self) -> None:
        ask = Namespace(
            run_id=self.run_id,
            question=["Preserve", "malformed", "frames?"],
            default="Preserve them with a warning",
            as_="glm",
        )
        answer = Namespace(
            run_id=self.run_id,
            answer=["Reject", "malformed", "frames"],
            as_="codex",
        )
        with mock.patch.object(cli.paths, "find_root", return_value=self.root), \
                mock.patch.object(cli.config, "load", return_value=self.cfg), \
                mock.patch("builtins.print"):
            cli.cmd_ask(ask)
            cli.cmd_answer(answer)

        con = db.connect(self.root)
        try:
            run = con.execute("SELECT status FROM runs WHERE id=?", (self.run_id,)).fetchone()
            question = con.execute(
                "SELECT * FROM questions WHERE run_id=?", (self.run_id,)
            ).fetchone()
            message = con.execute(
                "SELECT kind, read_at FROM messages WHERE run_id=? AND kind='question'",
                (self.run_id,),
            ).fetchone()
        finally:
            con.close()
        self.assertEqual(run["status"], "waiting_input")
        self.assertEqual(question["status"], "answered")
        self.assertEqual(question["answer"], "Reject malformed frames")
        self.assertEqual(question["answered_by"], "codex")
        self.assertEqual(message["kind"], "question")
        self.assertIsNotNone(message["read_at"])

    def test_default_run_cannot_block(self) -> None:
        con = db.connect(self.root)
        try:
            con.execute("UPDATE runs SET allow_question=0 WHERE id=?", (self.run_id,))
            con.commit()
        finally:
            con.close()
        ask = Namespace(
            run_id=self.run_id,
            question=["Can", "I", "wait?"],
            default="Continue",
            as_="glm",
        )
        with mock.patch.object(cli.paths, "find_root", return_value=self.root), \
                mock.patch.object(cli.config, "load", return_value=self.cfg), \
                self.assertRaisesRegex(SystemExit, "not dispatched with --allow-question"):
            cli.cmd_ask(ask)

    def test_rejected_claude_tool_creates_bounded_operator_question(self) -> None:
        failure = runners.ClaudeTerminalFailure(
            reason="aborted_tools",
            tool_rejected=True,
            tool_name="Bash",
            tool_description="Stop test process",
            tool_command="pkill -f test-process",
        )
        con = db.connect(self.root)
        try:
            run = con.execute("SELECT * FROM runs WHERE id=?", (self.run_id,)).fetchone()

            paused = supervise._pause_for_rejected_tool(con, run, failure)

            updated = con.execute(
                "SELECT status FROM runs WHERE id=?", (self.run_id,)
            ).fetchone()
            question = con.execute(
                "SELECT * FROM questions WHERE run_id=?", (self.run_id,)
            ).fetchone()
            message = con.execute(
                "SELECT * FROM messages WHERE run_id=? AND kind='question'", (self.run_id,)
            ).fetchone()
        finally:
            con.close()

        self.assertTrue(paused)
        self.assertEqual(updated["status"], "waiting_input")
        self.assertIn("Bash (Stop test process)", question["question"])
        self.assertIn("pkill -f test-process", question["question"])
        self.assertIn("safer non-destructive alternative", question["recommended_default"])
        self.assertEqual(question["recipient"], "codex")
        self.assertIn("orchestra answer", message["body"])

    def test_aborted_tool_without_rejection_does_not_claim_operator_denied_it(self) -> None:
        failure = runners.ClaudeTerminalFailure(reason="aborted_tools")
        con = db.connect(self.root)
        try:
            run = con.execute("SELECT * FROM runs WHERE id=?", (self.run_id,)).fetchone()

            paused = supervise._pause_for_rejected_tool(con, run, failure)

            question_count = con.execute(
                "SELECT COUNT(*) FROM questions WHERE run_id=?", (self.run_id,)
            ).fetchone()[0]
        finally:
            con.close()

        self.assertFalse(paused)
        self.assertEqual(question_count, 0)
        self.assertIn("interrupted while running a tool",
                      runners.claude_terminal_failure_text(failure))

    def test_supervisor_resumes_claude_after_rejected_tool_answer(self) -> None:
        (self.root / ".orchestra" / "config.toml").write_text(
            "[settings]\n"
            "timeout = 30\n"
            "supervisor_checkin_interval = 0\n"
            "\n[agents.glm]\n"
            'backend = "claude"\n'
            'model = "opus"\n'
        )
        con = db.connect(self.root)
        try:
            con.execute(
                "UPDATE runs SET status='spawning', session_ref=NULL, backend='claude' "
                "WHERE id=?",
                (self.run_id,),
            )
            con.commit()
        finally:
            con.close()

        calls: list[tuple[str | None, str]] = []
        attempt = 0

        def build_cmd(agent, *, workdir, title, prompt, resume_ref=None,
                      add_dirs=None, attach=None):
            calls.append((resume_ref, prompt))
            return [sys.executable, "-c", "pass"]

        def run_proc(con, run, cmd, workdir, env, log_path, run_id, deadline, **kwargs):
            nonlocal attempt
            attempt += 1
            if attempt == 1:
                events = [
                    {
                        "type": "assistant",
                        "session_id": "ses-denied",
                        "message": {"content": [{
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "Bash",
                            "input": {"description": "Stop test process",
                                      "command": "pkill -f test-process"},
                        }]},
                    },
                    {
                        "type": "user",
                        "session_id": "ses-denied",
                        "message": {"content": [{
                            "type": "tool_result",
                            "tool_use_id": "tool-1",
                            "is_error": True,
                            "content": "The tool use was rejected. STOP and wait.",
                        }]},
                    },
                    {
                        "type": "result",
                        "session_id": "ses-denied",
                        "is_error": True,
                        "terminal_reason": "aborted_tools",
                        "result": None,
                    },
                ]
                with open(log_path, "a") as log:
                    for event in events:
                        log.write(json.dumps(event) + "\n")
                con.execute(
                    "UPDATE runs SET status='running', session_ref='ses-denied' WHERE id=?",
                    (run_id,),
                )
                con.commit()
                return "exit", 143
            with open(log_path, "a") as log:
                log.write(json.dumps({
                    "type": "result",
                    "session_id": "ses-denied",
                    "subtype": "success",
                    "is_error": False,
                    "result": "Finished safely",
                }) + "\n")
            return "exit", 0

        def answer_question(con, run):
            answered_at = db.now()
            con.execute(
                "UPDATE questions SET status='answered', answer=?, answered_by=?, "
                "answered_at=? WHERE run_id=?",
                ("Do not kill it; inspect the PID only", "codex", answered_at, run["id"]),
            )
            con.commit()
            return con.execute(
                "SELECT * FROM questions WHERE run_id=?", (run["id"],)
            ).fetchone(), 0

        with mock.patch.object(supervise.runners, "build_cmd", side_effect=build_cmd), \
                mock.patch.object(supervise, "_run_proc", side_effect=run_proc), \
                mock.patch.object(supervise, "_wait_for_question", side_effect=answer_question):
            rc = supervise.supervise(self.root, self.run_id)

        con = db.connect(self.root)
        try:
            run = con.execute(
                "SELECT status, exit_code, summary FROM runs WHERE id=?", (self.run_id,)
            ).fetchone()
            question = con.execute(
                "SELECT status, answer FROM questions WHERE run_id=?", (self.run_id,)
            ).fetchone()
        finally:
            con.close()

        self.assertEqual(rc, 0)
        self.assertEqual(run["status"], "done")
        self.assertEqual(run["exit_code"], 0)
        self.assertEqual(run["summary"], "Finished safely")
        self.assertEqual(question["status"], "answered")
        self.assertEqual(calls[1][0], "ses-denied")
        self.assertIn("Answer to apply: Do not kill it; inspect the PID only", calls[1][1])

    def test_unanswered_question_uses_declared_fallback(self) -> None:
        con = db.connect(self.root)
        try:
            con.execute(
                "INSERT INTO questions(run_id,sender,recipient,question,recommended_default,"
                "asked_at,deadline_at) VALUES(?,?,?,?,?,?,?)",
                (self.run_id, "glm", "codex", "Which mode?", "Use safe mode",
                 db.now(), db.now()),
            )
            con.execute("UPDATE runs SET status='waiting_input' WHERE id=?", (self.run_id,))
            con.commit()
            run = con.execute("SELECT * FROM runs WHERE id=?", (self.run_id,)).fetchone()
            question, _waited = supervise._wait_for_question(con, run, poll_interval=0.01)
        finally:
            con.close()
        self.assertEqual(question["status"], "defaulted")
        self.assertEqual(question["answer"], "Use safe mode")
        self.assertEqual(question["answered_by"], "orchestra")

    def test_supervisor_resumes_answered_question_in_same_session(self) -> None:
        calls: list[tuple[str | None, str]] = []
        outcomes = iter(["waiting_input", "exit"])

        def build_cmd(agent, *, workdir, title, prompt, resume_ref=None, add_dirs=None, attach=None):
            calls.append((resume_ref, prompt))
            return [sys.executable, "-c", "pass"]

        def run_proc(con, run, cmd, workdir, env, log_path, run_id, deadline, **kwargs):
            outcome = next(outcomes)
            if outcome == "waiting_input":
                con.execute(
                    "INSERT INTO questions(run_id,sender,recipient,question,recommended_default,"
                    "status,asked_at,deadline_at,answered_at,answered_by,answer) "
                    "VALUES(?,?,?,?,?,'answered',?,?,?,?,?)",
                    (run_id, "glm", "codex", "Which mode?", "Use safe mode",
                     db.now(), db.after(60), db.now(), "codex", "Use strict mode"),
                )
                con.execute(
                    "UPDATE runs SET status='waiting_input', session_ref='ses-question' WHERE id=?",
                    (run_id,),
                )
                con.commit()
                return outcome, None
            return outcome, 0

        con = db.connect(self.root)
        try:
            con.execute("DELETE FROM questions WHERE run_id=?", (self.run_id,))
            con.execute("UPDATE runs SET status='spawning', session_ref=NULL WHERE id=?",
                        (self.run_id,))
            con.commit()
        finally:
            con.close()

        with mock.patch.object(supervise.runners, "build_cmd", side_effect=build_cmd), \
                mock.patch.object(supervise, "_run_proc", side_effect=run_proc), \
                mock.patch.object(supervise.runners, "parse_log",
                                  return_value=("ses-question", "finished")):
            rc = supervise.supervise(self.root, self.run_id)

        self.assertEqual(rc, 0)
        self.assertEqual(calls[0][0], None)
        self.assertEqual(calls[1][0], "ses-question")
        self.assertIn("Answer to apply: Use strict mode", calls[1][1])


if __name__ == "__main__":
    unittest.main()
