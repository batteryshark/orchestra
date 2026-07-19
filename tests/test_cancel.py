from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from orchestra_cli import cancel, db, paths, supervise


def _make_project() -> tuple[tempfile.TemporaryDirectory, Path]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / ".orchestra").mkdir(parents=True, exist_ok=True)
    db.connect(root).close()
    return tmp, root


def _insert_run(root: Path, *, status: str = "running", pid: int | None = None) -> int:
    con = db.connect(root)
    try:
        cur = con.execute(
            "INSERT INTO runs(agent, backend, model, title, work_item, "
            "team, requested_by, workdir, slug, status, pid, started_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("minimax", "opencode", "minimax-coding-plan/MiniMax-M3",
             "stop test", None, None, "codex", str(root), None, status,
             pid, "2026-07-18T22:00:00Z"),
        )
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


class StopRunSemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp, self.root = _make_project()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_active_run_is_marked_killed_before_signal(self) -> None:
        run_id = _insert_run(self.root, pid=4321)
        con = db.connect(self.root)
        try:
            with mock.patch.object(cancel.os, "getpgid", return_value=4321), \
                    mock.patch.object(cancel.os, "killpg") as killpg:
                result = cancel.stop_run(con, run_id)
        finally:
            con.close()

        self.assertIsNotNone(result)
        self.assertTrue(result.stopped)
        self.assertEqual(result.status, "killed")
        self.assertEqual(result.reason, "sigterm_sent")
        killpg.assert_called_once_with(4321, cancel.signal.SIGTERM)

        verify = db.connect(self.root)
        try:
            row = verify.execute("SELECT status, finished_at FROM runs WHERE id=?",
                                 (run_id,)).fetchone()
        finally:
            verify.close()
        self.assertEqual(row["status"], "killed")
        self.assertIsNotNone(row["finished_at"])

    def test_repeated_stop_on_terminal_run_is_truthful_and_idempotent(self) -> None:
        run_id = _insert_run(self.root, status="killed", pid=4321)
        con = db.connect(self.root)
        try:
            with mock.patch.object(cancel.os, "killpg",
                                   side_effect=AssertionError("must not signal")):
                result = cancel.stop_run(con, run_id)
        finally:
            con.close()

        self.assertIsNotNone(result)
        self.assertFalse(result.stopped)
        self.assertEqual(result.status, "killed")
        self.assertEqual(result.reason, "already_terminal")

    def test_stale_pid_that_is_not_group_leader_is_not_signalled(self) -> None:
        run_id = _insert_run(self.root, pid=4321)
        con = db.connect(self.root)
        try:
            with mock.patch.object(cancel.os, "getpgid", return_value=99), \
                    mock.patch.object(cancel.os, "killpg",
                                      side_effect=AssertionError("must not signal")):
                result = cancel.stop_run(con, run_id)
        finally:
            con.close()

        self.assertIsNotNone(result)
        self.assertTrue(result.stopped)
        self.assertFalse(result.signal_sent)
        self.assertEqual(result.reason, "pid_not_process_group_leader")

    def test_missing_run_returns_none(self) -> None:
        con = db.connect(self.root)
        try:
            self.assertIsNone(cancel.stop_run(con, 999))
        finally:
            con.close()


class SupervisorStopRaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp, self.root = _make_project()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_supervisor_does_not_spawn_after_preexisting_user_stop(self) -> None:
        run_id = _insert_run(self.root, status="killed")
        log_path = self.root / "run.log"
        sentinel = self.root / "launched"
        con = db.connect(self.root)
        try:
            run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            outcome, exit_code = supervise._run_proc(
                con,
                run,
                [sys.executable, "-c", f"from pathlib import Path; Path({str(sentinel)!r}).write_text('x')"],
                str(self.root),
                os.environ.copy(),
                log_path,
                run_id,
                time.time() + 30,
            )
        finally:
            con.close()

        self.assertEqual(outcome, "exit")
        self.assertIsNone(exit_code)
        self.assertFalse(sentinel.exists())

    def test_supervisor_finalization_preserves_user_stop(self) -> None:
        run_id = _insert_run(self.root)
        brief_path = self.root / "brief.md"
        log_path = self.root / "run.jsonl"
        brief_path.write_text("prompt")
        log_path.touch()
        con = db.connect(self.root)
        try:
            con.execute(
                "UPDATE runs SET brief_path=?, log_path=? WHERE id=?",
                (str(brief_path), str(log_path), run_id),
            )
            con.commit()
        finally:
            con.close()

        db_path = paths.db_path(self.root)
        code = (
            "import sqlite3;"
            f"con=sqlite3.connect({str(db_path)!r});"
            "con.execute(\"UPDATE runs SET status='killed' WHERE id=?\","
            f"({run_id},));"
            "con.commit();"
            "con.close()"
        )
        with mock.patch.object(supervise.runners, "build_cmd",
                               return_value=[sys.executable, "-c", code]):
            rc = supervise.supervise(self.root, run_id)

        self.assertEqual(rc, 1)
        verify = db.connect(self.root)
        try:
            row = verify.execute("SELECT status, exit_code FROM runs WHERE id=?",
                                 (run_id,)).fetchone()
        finally:
            verify.close()
        self.assertEqual(row["status"], "killed")
        self.assertIsNone(row["exit_code"])


if __name__ == "__main__":
    unittest.main()
