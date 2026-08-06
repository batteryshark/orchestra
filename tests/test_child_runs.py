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
         session_ref: str | None = None, pid: int | None = None,
         spawn_request_id: int | None = None) -> int:
    con = db.connect(root)
    try:
        cur = con.execute(
            "INSERT INTO runs(agent,backend,model,title,requested_by,workdir,status,"
            "lead_run,spawn_request_id,child_depth,session_ref,pid,started_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (agent, "codex", "test", "test", "orchestrator", str(root), status,
             lead_run, spawn_request_id, depth, session_ref, pid, db.now()),
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

    def test_tiered_parent_cannot_spawn_a_stronger_tier(self) -> None:
        self.cfg["agents"]["codex"]["tier"] = 2
        self.cfg["agents"]["minimax"]["tier"] = 3
        con = db.connect(self.root)
        try:
            parent = child_runs.validate_parent(con, self.cfg, self.parent_id, "codex")
            with self.assertRaisesRegex(SystemExit, "orchestra consult"):
                child_runs.create(
                    con, self.root, self.cfg, parent, ["minimax"], "inspect"
                )
        finally:
            con.close()

    def test_operator_worker_cannot_spawn_undeclared_child(self) -> None:
        con = db.connect(self.root)
        try:
            con.execute(
                "UPDATE runs SET containment_mode='operator-write' WHERE id=?",
                (self.parent_id,),
            )
            con.commit()
            with self.assertRaisesRegex(SystemExit, "controller owns all fan-out"):
                child_runs.validate_parent(
                    con, self.cfg, self.parent_id, "codex"
                )
        finally:
            con.close()

    def test_creation_records_child_edge_and_defaults_to_worktree(self) -> None:
        con = db.connect(self.root)
        fake_wt = self.root / "child-wt"
        fake_wt.mkdir()
        try:
            parent = child_runs.validate_parent(con, self.cfg, self.parent_id, "codex")
            with mock.patch.object(child_runs.worktree, "create",
                                   return_value=(fake_wt, "orchestra/run-2")) as create, \
                    mock.patch.object(child_runs.worktree, "head", return_value="abc123"):
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
            con.execute("UPDATE runs SET writes_tree=0 WHERE id=?", (child,))
            con.commit()
            parent = dict(con.execute("SELECT * FROM runs WHERE id=?", (child,)).fetchone())
            followup = supervise.create_followup(con, self.root, parent, "codex", "continue")
            row = con.execute("SELECT * FROM runs WHERE id=?", (followup,)).fetchone()
        finally:
            con.close()
        self.assertEqual(row["parent_run"], child)
        self.assertEqual(row["lead_run"], lead)
        self.assertEqual(row["child_depth"], 1)


class SpawnBrokerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp, self.root = _project()
        self.cfg = config.load(self.root)
        self.lead = _run(self.root, session_ref="lead-session")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_outer_supervisor_claims_request_and_launches_children(self) -> None:
        con = db.connect(self.root)
        launched: list[tuple[Path, int]] = []
        try:
            parent = con.execute("SELECT * FROM runs WHERE id=?", (self.lead,)).fetchone()
            request_id = child_runs.enqueue(
                con, parent, ["minimax"], "inspect one bounded area",
                shared_workdir=True,
            )

            results = child_runs.process_pending(
                con, self.root, self.cfg, self.lead,
                lambda root, run_id: launched.append((root, run_id)),
            )

            request = con.execute(
                "SELECT * FROM spawn_requests WHERE id=?", (request_id,)
            ).fetchone()
            child = con.execute(
                "SELECT * FROM runs WHERE spawn_request_id=?", (request_id,)
            ).fetchone()
        finally:
            con.close()
        self.assertEqual(results[0]["status"], "accepted")
        self.assertEqual(request["status"], "accepted")
        self.assertEqual(child["lead_run"], self.lead)
        self.assertEqual(child["spawn_request_id"], request_id)
        self.assertEqual(launched, [(self.root, child["id"])])

    def test_broker_failure_does_not_fail_the_lead(self) -> None:
        self.cfg["settings"]["child_max_per_run"] = 0
        con = db.connect(self.root)
        try:
            parent = con.execute("SELECT * FROM runs WHERE id=?", (self.lead,)).fetchone()
            request_id = child_runs.enqueue(con, parent, ["minimax"], "too many")

            results = child_runs.process_pending(
                con, self.root, self.cfg, self.lead,
                lambda _root, _run_id: None,
            )

            request = con.execute(
                "SELECT * FROM spawn_requests WHERE id=?", (request_id,)
            ).fetchone()
            lead = con.execute("SELECT status FROM runs WHERE id=?", (self.lead,)).fetchone()
        finally:
            con.close()
        self.assertEqual(results[0]["status"], "failed")
        self.assertIn("child count limit", request["error"])
        self.assertEqual(lead["status"], "running")

    def test_non_git_project_falls_back_to_shared_workdir_with_warning(self) -> None:
        con = db.connect(self.root)
        launched: list[int] = []
        try:
            parent = con.execute("SELECT * FROM runs WHERE id=?", (self.lead,)).fetchone()
            request_id = child_runs.enqueue(con, parent, ["minimax"], "read only")
            with mock.patch.object(
                child_runs.worktree,
                "create",
                side_effect=AssertionError("non-git fallback must not create a worktree"),
            ):
                child_runs.process_pending(
                    con,
                    self.root,
                    self.cfg,
                    self.lead,
                    lambda _root, run_id: launched.append(run_id),
                )
            request = con.execute(
                "SELECT * FROM spawn_requests WHERE id=?", (request_id,)
            ).fetchone()
            child = con.execute(
                "SELECT * FROM runs WHERE spawn_request_id=?", (request_id,)
            ).fetchone()
        finally:
            con.close()
        self.assertEqual(request["status"], "accepted")
        self.assertIn("not a git repository", request["error"])
        self.assertEqual(child["workdir"], str(self.root))
        self.assertIsNone(child["branch"])
        self.assertEqual(launched, [child["id"]])

    def test_settled_batch_interrupts_active_lead_exactly_once(self) -> None:
        con = db.connect(self.root)
        try:
            parent = con.execute("SELECT * FROM runs WHERE id=?", (self.lead,)).fetchone()
            request_id = child_runs.enqueue(con, parent, ["minimax"], "inspect")
            con.execute(
                "UPDATE spawn_requests SET status='accepted', child_run_ids_json='[2]', "
                "processed_at=? WHERE id=?",
                (db.now(), request_id),
            )
            con.commit()
            child = _run(
                self.root, agent="minimax", status="done", lead_run=self.lead,
                depth=1, spawn_request_id=request_id,
            )

            self.assertIsNone(child_runs.maybe_wake_lead(con, self.root, child))
            self.assertIsNone(child_runs.maybe_wake_lead(con, self.root, child))
            request = con.execute(
                "SELECT * FROM spawn_requests WHERE id=?", (request_id,)
            ).fetchone()
            messages = list(con.execute(
                "SELECT * FROM messages WHERE run_id=? AND kind='interrupt'",
                (self.lead,),
            ))
        finally:
            con.close()
        self.assertIsNotNone(request["notified_at"])
        self.assertEqual(request["wakeup_message"], messages[0]["id"])
        self.assertEqual(len(messages), 1)
        self.assertIn("All child runs", messages[0]["body"])


class SupervisorChildEnvironmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp, self.root = _project()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_supervisor_exports_current_run_identity(self) -> None:
        run_id = _run(self.root, agent="minimax")
        (self.root / ".orchestra" / "config.toml").write_text(
            '[worker_env]\nPIU_IDA_DB = "{root}/../shared/piu.i64"\n'
        )
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
            "os.environ['ORCHESTRA_SELF']+'|'+os.environ['ORCHESTRA_RUN_ID']+'|'"
            "+os.environ['PIU_IDA_DB'])"
        )
        with mock.patch.object(supervise.runners, "build_cmd",
                               return_value=[sys.executable, "-c", code]):
            self.assertEqual(supervise.supervise(self.root, run_id), 0)
        self.assertEqual(
            observed.read_text(),
            f"minimax|{run_id}|{self.root}/../shared/piu.i64",
        )


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
