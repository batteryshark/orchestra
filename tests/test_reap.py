from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from orchestra_cli import db, reap


def _project() -> tuple[tempfile.TemporaryDirectory, Path]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / ".orchestra").mkdir(parents=True, exist_ok=True)
    (root / ".orchestra" / "config.toml").write_text("[settings]\n")
    db.connect(root).close()
    return tmp, root


def _dead_pid() -> int:
    """A pid that is certainly not running: run a trivial child to completion."""
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    return proc.pid


def _write_log(root: Path, run_id: int, records: list[dict]) -> str:
    logs = root / ".orchestra" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / f"run-{run_id}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return str(path)


CLEAN_CLAUDE_TAIL = [
    {"type": "system", "subtype": "init"},
    {"type": "result", "subtype": "success", "is_error": False,
     "result": "W-0001 is done and committed."},
]


def _insert(con, *, status="running", pid=None, supervisor_pid=None,
            log_path=None, work_item=None):
    cur = con.execute(
        "INSERT INTO runs(agent, backend, requested_by, workdir, status, pid, "
        "supervisor_pid, log_path, work_item, started_at) "
        "VALUES('opus','claude','orchestrator','/tmp',?,?,?,?,?,?)",
        (status, pid, supervisor_pid, log_path, work_item, db.now()))
    con.commit()
    return cur.lastrowid


class SupervisorPidReapTests(unittest.TestCase):
    """The precise rule: a recorded supervisor pid that is gone means orphaned."""

    def setUp(self):
        self.tmp, self.root = _project()
        self.addCleanup(self.tmp.cleanup)
        self.con = db.connect(self.root)
        self.addCleanup(self.con.close)

    def test_live_supervisor_is_never_reaped(self):
        run_id = _insert(self.con, supervisor_pid=os.getpid(),
                         log_path=_write_log(self.root, 1, CLEAN_CLAUDE_TAIL))

        self.assertEqual(reap.reap_orphans(self.con), [])
        row = self.con.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        self.assertEqual(row["status"], "running")

    def test_dead_supervisor_with_clean_log_settles_as_done(self):
        log = _write_log(self.root, 1, CLEAN_CLAUDE_TAIL)
        run_id = _insert(self.con, supervisor_pid=_dead_pid(), log_path=log)

        reaped = reap.reap_orphans(self.con)

        self.assertEqual([r["id"] for r in reaped], [run_id])
        row = self.con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["exit_code"], 0)
        # The whole point: the worker's report is recovered from the log.
        self.assertEqual(row["summary"], "W-0001 is done and committed.")
        self.assertIsNotNone(row["finished_at"])

    def test_dead_supervisor_without_terminal_record_is_failed_not_done(self):
        log = _write_log(self.root, 1, [{"type": "system", "subtype": "init"}])
        run_id = _insert(self.con, supervisor_pid=_dead_pid(), log_path=log)

        reap.reap_orphans(self.con)

        row = self.con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIsNone(row["exit_code"])

    def test_error_result_settles_as_failed(self):
        log = _write_log(self.root, 1, [
            {"type": "result", "subtype": "error_during_execution", "is_error": True},
        ])
        run_id = _insert(self.con, supervisor_pid=_dead_pid(), log_path=log)

        reap.reap_orphans(self.con)

        row = self.con.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        self.assertEqual(row["status"], "failed")

    def test_opencode_stop_reason_settles_as_done(self):
        log = _write_log(self.root, 1, [
            {"type": "step_finish", "part": {"reason": "stop"}},
        ])
        run_id = _insert(self.con, supervisor_pid=_dead_pid(), log_path=log)

        reap.reap_orphans(self.con)

        row = self.con.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        self.assertEqual(row["status"], "done")

    def test_already_terminal_runs_are_left_alone(self):
        log = _write_log(self.root, 1, CLEAN_CLAUDE_TAIL)
        run_id = _insert(self.con, status="killed", supervisor_pid=_dead_pid(),
                         log_path=log)

        self.assertEqual(reap.reap_orphans(self.con), [])
        row = self.con.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        self.assertEqual(row["status"], "killed")

    def test_reaping_announces_to_the_requester_and_the_feed(self):
        log = _write_log(self.root, 1, CLEAN_CLAUDE_TAIL)
        run_id = _insert(self.con, supervisor_pid=_dead_pid(), log_path=log)

        reap.reap_orphans(self.con)

        msg = self.con.execute(
            "SELECT * FROM messages WHERE run_id=?", (run_id,)).fetchone()
        self.assertEqual(msg["recipient"], "orchestrator")
        self.assertIn("supervisor died", msg["body"])
        feed = self.con.execute(
            "SELECT * FROM feed WHERE run_id=?", (run_id,)).fetchone()
        self.assertIsNotNone(feed)

    def test_reaping_is_idempotent(self):
        log = _write_log(self.root, 1, CLEAN_CLAUDE_TAIL)
        _insert(self.con, supervisor_pid=_dead_pid(), log_path=log)

        self.assertEqual(len(reap.reap_orphans(self.con)), 1)
        self.assertEqual(reap.reap_orphans(self.con), [])


class LegacyRowReapTests(unittest.TestCase):
    """Rows predating supervisor_pid: agent pid dead AND the log gone quiet."""

    def setUp(self):
        self.tmp, self.root = _project()
        self.addCleanup(self.tmp.cleanup)
        self.con = db.connect(self.root)
        self.addCleanup(self.con.close)

    def test_fresh_log_is_not_reaped_even_with_a_dead_agent(self):
        # The supervisor relaunches the agent between loop iterations; during
        # that gap the agent pid is legitimately dead. Reaping here would
        # detach a live supervisor from its own run.
        log = _write_log(self.root, 1, CLEAN_CLAUDE_TAIL)
        _insert(self.con, pid=_dead_pid(), log_path=log)

        self.assertEqual(reap.reap_orphans(self.con, grace_seconds=600), [])

    def test_quiet_log_with_a_dead_agent_is_reaped(self):
        log = _write_log(self.root, 1, CLEAN_CLAUDE_TAIL)
        old = time.time() - 3600
        os.utime(log, (old, old))
        run_id = _insert(self.con, pid=_dead_pid(), log_path=log)

        reap.reap_orphans(self.con, grace_seconds=600)

        row = self.con.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        self.assertEqual(row["status"], "done")

    def test_live_agent_is_never_reaped_however_quiet_the_log(self):
        log = _write_log(self.root, 1, CLEAN_CLAUDE_TAIL)
        old = time.time() - 3600
        os.utime(log, (old, old))
        _insert(self.con, pid=os.getpid(), log_path=log)

        self.assertEqual(reap.reap_orphans(self.con, grace_seconds=600), [])

    def test_stale_spawning_row_without_log_is_reaped(self):
        run_id = _insert(self.con, status="spawning", log_path=None)
        self.con.execute(
            "UPDATE runs SET started_at='2000-01-01T00:00:00Z' WHERE id=?",
            (run_id,),
        )
        self.con.commit()

        reaped = reap.reap_orphans(self.con, grace_seconds=600)

        self.assertEqual([item["id"] for item in reaped], [run_id])
        status = self.con.execute(
            "SELECT status FROM runs WHERE id=?", (run_id,)
        ).fetchone()["status"]
        self.assertEqual(status, "failed")


class MigrationTests(unittest.TestCase):
    def test_supervisor_pid_is_added_to_a_preexisting_database(self):
        tmp, root = _project()
        self.addCleanup(tmp.cleanup)
        con = db.connect(root)
        con.execute("ALTER TABLE runs DROP COLUMN supervisor_pid")
        con.commit()
        con.close()

        con = db.connect(root)  # reconnect runs the migrations
        self.addCleanup(con.close)
        cols = {r[1] for r in con.execute("PRAGMA table_info(runs)")}
        self.assertIn("supervisor_pid", cols)


if __name__ == "__main__":
    unittest.main()
