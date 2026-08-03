from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from orchestra_cli import cancel, cli, config, db, dependencies, paths, supervise


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


def _defer(root: Path, prerequisite: int, *, kind: str) -> int:
    run_id = _insert_run(root, status="pending")
    con = db.connect(root)
    try:
        dependencies.enqueue(
            con,
            run_id,
            [prerequisite],
            mission="wait for prerequisite",
            context=None,
            use_worktree=False,
            dependency_kind=kind,
            writes_tree=False,
        )
        con.commit()
        return run_id
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

    def test_waiting_for_input_run_remains_stoppable(self) -> None:
        run_id = _insert_run(self.root, status="waiting_input")
        con = db.connect(self.root)
        try:
            result = cancel.stop_run(con, run_id)
        finally:
            con.close()
        self.assertTrue(result.stopped)
        self.assertEqual(result.previous_status, "waiting_input")
        self.assertEqual(result.status, "killed")

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

    def test_cancellation_preview_distinguishes_declines_from_wait_for_release(self) -> None:
        producer = _insert_run(self.root)
        declined = _defer(
            self.root, producer, kind=dependencies.REQUIRES_SUCCESS
        )
        transitive_decline = _defer(
            self.root, declined, kind=dependencies.REQUIRES_SUCCESS
        )
        unblocked = _defer(self.root, producer, kind=dependencies.WAIT_FOR)
        transitive_unblock = _defer(self.root, declined, kind=dependencies.WAIT_FOR)
        con = db.connect(self.root)
        try:
            impact = dependencies.cancellation_impact(con, producer)
            before = {
                int(row["id"]): row["status"]
                for row in con.execute(
                    "SELECT id, status FROM runs WHERE id IN (?,?,?,?)",
                    (declined, transitive_decline, unblocked, transitive_unblock),
                )
            }
            result = cancel.stop_run(con, producer)
            after = {
                int(row["id"]): row["status"]
                for row in con.execute(
                    "SELECT id, status FROM runs WHERE id IN (?,?,?,?)",
                    (declined, transitive_decline, unblocked, transitive_unblock),
                )
            }
        finally:
            con.close()

        self.assertEqual(
            impact,
            {
                "declined_run_ids": [],
                "held_run_ids": [declined],
                "unblocked_run_ids": [unblocked],
            },
        )
        self.assertEqual(set(before.values()), {"pending"})
        self.assertEqual(after[declined], "pending")
        self.assertEqual(after[transitive_decline], "pending")
        self.assertEqual(after[unblocked], "pending")
        self.assertEqual(after[transitive_unblock], "pending")
        self.assertIsNotNone(result)
        self.assertEqual(result.declined_run_ids, ())
        self.assertEqual(result.held_run_ids, (declined,))
        self.assertEqual(result.unblocked_run_ids, (unblocked,))
        self.assertEqual(result.as_dict()["held_run_ids"], [declined])

    def test_terminal_stop_has_no_new_cancellation_impact(self) -> None:
        producer = _insert_run(self.root, status="done")
        _defer(self.root, producer, kind=dependencies.REQUIRES_SUCCESS)
        con = db.connect(self.root)
        try:
            result = cancel.stop_run(con, producer)
        finally:
            con.close()
        self.assertIsNotNone(result)
        self.assertFalse(result.stopped)
        self.assertEqual(result.declined_run_ids, ())
        self.assertEqual(result.unblocked_run_ids, ())

    def test_preview_wait_for_with_another_active_prerequisite_is_not_unblocked(self) -> None:
        cancelled = _insert_run(self.root)
        still_running = _insert_run(self.root)
        consumer = _defer(self.root, cancelled, kind=dependencies.WAIT_FOR)
        con = db.connect(self.root)
        try:
            con.execute(
                "INSERT INTO dispatch_dependencies(run_id, depends_on_run, kind) "
                "VALUES(?,?,?)",
                (consumer, still_running, dependencies.WAIT_FOR),
            )
            con.commit()
            impact = dependencies.cancellation_impact(con, cancelled)
            status = con.execute(
                "SELECT status FROM runs WHERE id=?", (consumer,)
            ).fetchone()["status"]
        finally:
            con.close()

        self.assertEqual(
            impact,
            {"declined_run_ids": [], "held_run_ids": [], "unblocked_run_ids": []},
        )
        self.assertEqual(status, "pending")

    def test_preview_declines_mixed_wait_for_and_failed_success_edge(self) -> None:
        cancelled = _insert_run(self.root)
        already_failed = _insert_run(self.root, status="failed")
        consumer = _defer(self.root, cancelled, kind=dependencies.WAIT_FOR)
        con = db.connect(self.root)
        try:
            con.execute(
                "INSERT INTO dispatch_dependencies(run_id, depends_on_run, kind) "
                "VALUES(?,?,?)",
                (consumer, already_failed, dependencies.REQUIRES_SUCCESS),
            )
            con.commit()
            impact = dependencies.cancellation_impact(con, cancelled)
            before = con.execute(
                "SELECT status FROM runs WHERE id=?", (consumer,)
            ).fetchone()["status"]
            result = cancel.stop_run(con, cancelled)
            after = con.execute(
                "SELECT status FROM runs WHERE id=?", (consumer,)
            ).fetchone()["status"]
        finally:
            con.close()

        self.assertEqual(
            impact,
            {"declined_run_ids": [consumer], "held_run_ids": [], "unblocked_run_ids": []},
        )
        self.assertEqual(before, "pending")
        self.assertIsNotNone(result)
        self.assertEqual(result.declined_run_ids, (consumer,))
        self.assertEqual(result.unblocked_run_ids, ())
        self.assertEqual(after, "failed")

    def test_unknown_cancellation_impact_is_empty_and_read_only(self) -> None:
        prerequisite = _insert_run(self.root)
        consumer = _defer(self.root, prerequisite, kind=dependencies.REQUIRES_SUCCESS)
        con = db.connect(self.root)
        try:
            impact = dependencies.cancellation_impact(con, 999)
            row = con.execute(
                "SELECT status FROM runs WHERE id=?", (consumer,)
            ).fetchone()
            deferred = con.execute(
                "SELECT status FROM deferred_dispatches WHERE run_id=?", (consumer,)
            ).fetchone()
        finally:
            con.close()

        self.assertEqual(
            impact,
            {"declined_run_ids": [], "held_run_ids": [], "unblocked_run_ids": []},
        )
        self.assertEqual(row["status"], "pending")
        self.assertEqual(deferred["status"], "pending")

    def test_reconciliation_can_launch_wait_for_after_stop(self) -> None:
        producer = _insert_run(self.root)
        consumer = _defer(self.root, producer, kind=dependencies.WAIT_FOR)
        launched: list[int] = []
        con = db.connect(self.root)
        try:
            result = cancel.stop_run(con, producer)
            dependencies.process_ready(
                con,
                self.root,
                config.load(self.root),
                lambda _root, run_id: launched.append(run_id),
            )
            status = con.execute(
                "SELECT status FROM runs WHERE id=?", (consumer,)
            ).fetchone()["status"]
        finally:
            con.close()

        self.assertIsNotNone(result)
        self.assertEqual(result.unblocked_run_ids, (consumer,))
        self.assertEqual(launched, [consumer])
        self.assertEqual(status, "spawning")

    def test_cli_dry_run_is_read_only_and_real_cancel_releases_wait_for(self) -> None:
        producer = _insert_run(self.root)
        consumer = _defer(self.root, producer, kind=dependencies.WAIT_FOR)
        output = StringIO()
        with mock.patch.object(cli.paths, "find_root", return_value=self.root), \
                redirect_stdout(output):
            cli.cmd_kill(SimpleNamespace(run_id=producer, dry_run=True))

        con = db.connect(self.root)
        try:
            self.assertEqual(
                con.execute("SELECT status FROM runs WHERE id=?", (producer,)).fetchone()[0],
                "running",
            )
            self.assertEqual(
                con.execute("SELECT status FROM runs WHERE id=?", (consumer,)).fetchone()[0],
                "pending",
            )
        finally:
            con.close()
        self.assertIn(f"would release wait-only run(s): {consumer}", output.getvalue())

        launched: list[int] = []
        with mock.patch.object(cli.paths, "find_root", return_value=self.root), \
                mock.patch.object(
                    cli, "_spawn_supervisor",
                    side_effect=lambda _root, run_id: launched.append(run_id),
                ), redirect_stdout(StringIO()):
            cli.cmd_kill(SimpleNamespace(run_id=producer, dry_run=False))
        self.assertEqual(launched, [consumer])


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
