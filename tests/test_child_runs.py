from __future__ import annotations

import tempfile
import unittest
import sys
from pathlib import Path
from unittest import mock

from orchestra_cli import cancel, child_runs, config, db, supervise


def _project() -> tuple[tempfile.TemporaryDirectory, Path]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / ".orchestra").mkdir()
    db.connect(root).close()
    return tmp, root


def _run(root: Path, *, agent: str = "codex", status: str = "running",
         lead_run: int | None = None, depth: int = 0,
         session_ref: str | None = None, pid: int | None = None) -> int:
    con = db.connect(root)
    try:
        cur = con.execute(
            "INSERT INTO runs(agent,backend,model,title,requested_by,workdir,status,"
            "lead_run,child_depth,session_ref,pid,started_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (agent, "codex", "test", "test", "orchestrator", str(root), status,
             lead_run, depth, session_ref, pid, db.now()),
        )
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


class ChildPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp, self.root = _project()
        self.cfg = config.load(self.root)
        self.parent_id = _run(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_parent_requires_matching_supervised_identity(self) -> None:
        con = db.connect(self.root)
        try:
            with self.assertRaisesRegex(SystemExit, "identity"):
                child_runs.validate_parent(con, self.cfg, self.parent_id, "minimax")
            parent = child_runs.validate_parent(con, self.cfg, self.parent_id, "codex")
            self.assertEqual(parent["id"], self.parent_id)
        finally:
            con.close()

    def test_default_depth_fails_closed_for_recursive_child(self) -> None:
        child_id = _run(self.root, agent="minimax", lead_run=self.parent_id, depth=1)
        con = db.connect(self.root)
        try:
            with self.assertRaisesRegex(SystemExit, "depth limit"):
                child_runs.validate_parent(con, self.cfg, child_id, "minimax")
        finally:
            con.close()

    def test_invalid_limit_is_rejected_instead_of_coerced(self) -> None:
        self.cfg["settings"]["child_max_active"] = "many"
        with self.assertRaisesRegex(SystemExit, "non-negative integer"):
            child_runs.limits(self.cfg)

    def test_creation_records_child_edge_and_defaults_to_worktree(self) -> None:
        con = db.connect(self.root)
        fake_wt = self.root / "child-wt"
        fake_wt.mkdir()
        try:
            parent = child_runs.validate_parent(con, self.cfg, self.parent_id, "codex")
            with mock.patch.object(child_runs.worktree, "create",
                                   return_value=(fake_wt, "orchestra/run-2")) as create:
                ids = child_runs.create(con, self.root, self.cfg, parent, ["minimax"], "inspect")
            row = con.execute("SELECT * FROM runs WHERE id=?", (ids[0],)).fetchone()
        finally:
            con.close()
        create.assert_called_once_with(self.root, ids[0], start_point=None)
        self.assertEqual(row["lead_run"], self.parent_id)
        self.assertEqual(row["child_depth"], 1)
        self.assertEqual(row["branch"], "orchestra/run-2")
        self.assertIn("Child-run contract", Path(row["brief_path"]).read_text())

    def test_total_and_active_limits_are_enforced_before_creation(self) -> None:
        self.cfg["settings"]["child_max_per_run"] = 1
        self.cfg["settings"]["child_max_active"] = 1
        _run(self.root, agent="minimax", lead_run=self.parent_id, depth=1)
        con = db.connect(self.root)
        try:
            parent = con.execute("SELECT * FROM runs WHERE id=?", (self.parent_id,)).fetchone()
            with self.assertRaisesRegex(SystemExit, "child count limit"):
                child_runs.create(con, self.root, self.cfg, parent, ["glm"], "extra")
        finally:
            con.close()

    def test_active_limit_is_independent_from_lifetime_count(self) -> None:
        self.cfg["settings"]["child_max_per_run"] = 3
        self.cfg["settings"]["child_max_active"] = 1
        _run(self.root, agent="minimax", lead_run=self.parent_id, depth=1)
        con = db.connect(self.root)
        try:
            parent = con.execute("SELECT * FROM runs WHERE id=?", (self.parent_id,)).fetchone()
            with self.assertRaisesRegex(SystemExit, "active child limit"):
                child_runs.create(con, self.root, self.cfg, parent, ["glm"], "extra")
        finally:
            con.close()

    def test_setup_failure_marks_reserved_batch_terminal(self) -> None:
        con = db.connect(self.root)
        try:
            parent = child_runs.validate_parent(con, self.cfg, self.parent_id, "codex")
            with mock.patch.object(child_runs.worktree, "create",
                                   side_effect=SystemExit("git failed")):
                with self.assertRaisesRegex(SystemExit, "git failed"):
                    child_runs.create(con, self.root, self.cfg, parent,
                                      ["minimax", "glm"], "inspect")
            rows = list(con.execute("SELECT status,summary FROM runs WHERE lead_run=?",
                                    (self.parent_id,)))
        finally:
            con.close()
        self.assertEqual([r["status"] for r in rows], ["failed", "failed"])
        self.assertTrue(all("setup failed" in r["summary"] for r in rows))


class ChildWakeupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp, self.root = _project()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_settled_batch_wakes_terminal_lead_exactly_once(self) -> None:
        lead = _run(self.root, status="done", session_ref="session-1")
        a = _run(self.root, agent="minimax", status="done", lead_run=lead, depth=1)
        b = _run(self.root, agent="glm", status="failed", lead_run=lead, depth=1)
        con = db.connect(self.root)
        try:
            first = child_runs.maybe_wake_lead(con, self.root, a)
            second = child_runs.maybe_wake_lead(con, self.root, b)
            lead_row = con.execute("SELECT child_wakeup_run FROM runs WHERE id=?", (lead,)).fetchone()
            wake = con.execute("SELECT * FROM runs WHERE id=?", (first,)).fetchone()
        finally:
            con.close()
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(lead_row["child_wakeup_run"], first)
        self.assertEqual(wake["parent_run"], lead)
        self.assertIsNone(wake["lead_run"])
        self.assertIn("run 2", Path(wake["brief_path"]).read_text())

    def test_active_child_or_running_lead_does_not_wake(self) -> None:
        lead = _run(self.root, status="running", session_ref="session-1")
        child = _run(self.root, status="done", lead_run=lead, depth=1)
        con = db.connect(self.root)
        try:
            self.assertIsNone(child_runs.maybe_wake_lead(con, self.root, child))
            con.execute("UPDATE runs SET status='done' WHERE id=?", (lead,))
            con.execute("UPDATE runs SET status='running' WHERE id=?", (child,))
            con.commit()
            self.assertIsNone(child_runs.maybe_wake_lead(con, self.root, lead))
        finally:
            con.close()

    def test_child_session_followup_preserves_ownership(self) -> None:
        lead = _run(self.root, status="running")
        child = _run(self.root, agent="minimax", status="done", lead_run=lead,
                     depth=1, session_ref="child-session")
        con = db.connect(self.root)
        try:
            parent = dict(con.execute("SELECT * FROM runs WHERE id=?", (child,)).fetchone())
            followup = supervise.create_followup(con, self.root, parent, "codex", "continue")
            row = con.execute("SELECT * FROM runs WHERE id=?", (followup,)).fetchone()
        finally:
            con.close()
        self.assertEqual(row["parent_run"], child)
        self.assertEqual(row["lead_run"], lead)
        self.assertEqual(row["child_depth"], 1)


class SupervisorChildEnvironmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp, self.root = _project()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_supervisor_exports_current_run_identity(self) -> None:
        run_id = _run(self.root, agent="minimax")
        brief_path = self.root / "brief.md"
        log_path = self.root / "run.jsonl"
        observed = self.root / "observed.txt"
        brief_path.write_text("prompt")
        log_path.touch()
        con = db.connect(self.root)
        try:
            con.execute("UPDATE runs SET brief_path=?,log_path=? WHERE id=?",
                        (str(brief_path), str(log_path), run_id))
            con.commit()
        finally:
            con.close()
        code = (
            "import os,pathlib;"
            f"pathlib.Path({str(observed)!r}).write_text("
            "os.environ['ORCHESTRA_SELF']+'|'+os.environ['ORCHESTRA_RUN_ID'])"
        )
        with mock.patch.object(supervise.runners, "build_cmd",
                               return_value=[sys.executable, "-c", code]):
            self.assertEqual(supervise.supervise(self.root, run_id), 0)
        self.assertEqual(observed.read_text(), f"minimax|{run_id}")


class ChildCancellationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp, self.root = _project()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_stopping_lead_cascades_to_active_descendants_only(self) -> None:
        lead = _run(self.root, pid=101)
        child = _run(self.root, lead_run=lead, depth=1, pid=102)
        grandchild = _run(self.root, lead_run=child, depth=2, pid=103)
        finished = _run(self.root, status="done", lead_run=lead, depth=1, pid=104)
        con = db.connect(self.root)
        try:
            with mock.patch.object(cancel, "_signal_process_group", return_value=(True, "sigterm_sent")) as signal:
                result = cancel.stop_run(con, lead)
            states = {r["id"]: r["status"] for r in con.execute("SELECT id,status FROM runs")}
        finally:
            con.close()
        self.assertEqual(result.descendant_ids, (child, grandchild))
        self.assertEqual(states[lead], "killed")
        self.assertEqual(states[child], "killed")
        self.assertEqual(states[grandchild], "killed")
        self.assertEqual(states[finished], "done")
        self.assertEqual(signal.call_count, 3)


if __name__ == "__main__":
    unittest.main()
