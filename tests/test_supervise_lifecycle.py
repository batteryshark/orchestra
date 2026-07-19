from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from orchestra_cli import cli, db, supervise
from orchestra_cli.usage.models import ProviderResult, QuotaWindow


def _project(*, checkin_interval: int = 0, timeout: int = 30) -> tuple[tempfile.TemporaryDirectory, Path]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / ".orchestra").mkdir(parents=True, exist_ok=True)
    (root / ".orchestra" / "config.toml").write_text(
        "[settings]\n"
        f"timeout = {timeout}\n"
        f"supervisor_checkin_interval = {checkin_interval}\n"
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


class SupervisorCheckinTests(unittest.TestCase):
    def tearDown(self) -> None:
        if hasattr(self, "tmp"):
            self.tmp.cleanup()

    def test_periodic_checkin_interrupts_once_and_resumes_same_session(self) -> None:
        self.tmp, root = _project(checkin_interval=1, timeout=30)
        run_id = _insert_run(root)
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
        self.assertIn("IMMEDIATELY run `orchestra inbox glm --unread --mark-read`", calls[1][1])
        con = db.connect(root)
        try:
            row = con.execute("SELECT status, session_ref, summary FROM runs WHERE id=?",
                              (run_id,)).fetchone()
            checkins = list(con.execute(
                "SELECT body, delivery_offset, delivered_at FROM messages "
                "WHERE recipient='glm' AND kind='checkin'"
            ))
        finally:
            con.close()
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["session_ref"], "ses-checkin")
        self.assertEqual(len(checkins), 1)
        self.assertIn("PROGRESS CHECK-IN", checkins[0]["body"])
        self.assertIsNotNone(checkins[0]["delivery_offset"])
        self.assertIsNotNone(checkins[0]["delivered_at"])
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
        calls: list[str | None] = []

        def build_cmd(agent, *, workdir, title, prompt, resume_ref=None,
                      add_dirs=None, attach=None):
            calls.append(resume_ref)
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
        self.assertEqual(calls, [None, "ses-natural"])
        con = db.connect(root)
        try:
            message = con.execute(
                "SELECT delivered_at FROM messages WHERE kind='interrupt'"
            ).fetchone()
        finally:
            con.close()
        self.assertIsNotNone(message["delivered_at"])


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
