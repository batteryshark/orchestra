from __future__ import annotations

import json
import sys
import tempfile
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
                return _sleeping_worker(line={"sessionID": "ses-checkin"})
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
                "SELECT body FROM messages WHERE recipient='glm' AND kind='checkin'"
            ))
        finally:
            con.close()
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["session_ref"], "ses-checkin")
        self.assertEqual(len(checkins), 1)
        self.assertIn("PROGRESS CHECK-IN", checkins[0]["body"])
        delivery_events = [
            json.loads(line)
            for line in (root / "run.jsonl").read_text().splitlines()
            if line.startswith('{"type":"orchestra.delivery"')
        ]
        self.assertEqual(len(delivery_events), 1)
        self.assertEqual(delivery_events[0]["delivery"], "checkin")
        self.assertIsInstance(delivery_events[0]["message_id"], int)
        self.assertEqual(delivery_events[0]["sender"], "orchestra")
        self.assertEqual(delivery_events[0]["recipient"], "glm")


class InterruptMessageTests(unittest.TestCase):
    def test_cli_records_interrupt_as_typed_inbox_delivery(self) -> None:
        tmp, root = _project(checkin_interval=0)
        self.addCleanup(tmp.cleanup)
        run_id = _insert_run(root)
        con = db.connect(root)
        try:
            con.execute("UPDATE runs SET status='running', session_ref='ses-123' WHERE id=?",
                        (run_id,))
            con.commit()
        finally:
            con.close()

        cfg = {
            "settings": {"default_requester": "orchestrator"},
            "agents": {"glm": {"backend": "opencode"}},
        }
        args = Namespace(run_id=run_id, message=["Check", "your", "inbox"], as_="claude")
        with mock.patch.object(cli.paths, "find_root", return_value=root), \
                mock.patch.object(cli.config, "load", return_value=cfg), \
                mock.patch("builtins.print"):
            cli.cmd_interrupt(args)

        con = db.connect(root)
        try:
            message = con.execute(
                "SELECT sender, recipient, body, kind FROM messages WHERE run_id=?",
                (run_id,),
            ).fetchone()
            status = con.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()[0]
        finally:
            con.close()
        self.assertEqual(dict(message), {
            "sender": "claude",
            "recipient": "glm",
            "body": "[INTERRUPT] Check your inbox",
            "kind": "interrupt",
        })
        self.assertEqual(status, "interrupt")


if __name__ == "__main__":
    unittest.main()
