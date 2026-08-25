"""Sweeper behaviors against the fake Work API (no live Work instance).

State is central (DESIGN §2): the sweeper owns the whole workspace, resolves
each item's project from Work, and writes to one database under
``ORCHESTRA_HOME``. The fixture therefore builds a workspace with a project
directory in it, not a project with a state directory.
"""
import json
import os
import queue
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from orchestra import brief, config, db, project, supervise, sweeper
from orchestra.work_client import WorkClient, WorkError
from tests.fake_work import FakeWork

PROJECT_ID = "53efe3c3-6def-4797-8560-3dce073d7d63"

CONFIG = """\
[settings]
timeout = 60

[profiles.stub]
backend = "opencode"

[work]
enabled = true
agent_identity = "orchestra"
profile = "stub"
poll_interval = 7
# Most sweeper tests exercise Work behavior in a directory with no repository.
# Shared mode is explicit here; isolation and its failure mode have focused tests.
worktree = false
"""


# A per-project enabled set that names another profile (tests of the
# refusal path append this to the global config verbatim).
OTHER_PROFILE_ONLY = (
    "\n[profiles.other]\nbackend = \"codex\"\n"
    f"\n[project.\"{PROJECT_ID}\"]\nenabled_profiles = [\"other\"]\n")


def tool_line(tool: str, **args) -> str:
    """One finished tool in the fixture profile's backend (opencode) — the
    shape the progress heartbeat counts."""
    return json.dumps({"type": "message.part.updated", "part": {
        "type": "tool", "tool": tool,
        "state": {"status": "completed", "input": args, "output": "ok"}}}) + "\n"


class SweeperFixture:
    """Workspace + fake Work + a throwaway ORCHESTRA_HOME. Mixed into the case
    below and into tests/test_dispatch.py, which needs the same harness."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name).resolve()
        self.workspace = self.tmp_path / "workspace"
        self.root = self.workspace / "demo"   # the project checkout
        self.root.mkdir(parents=True)
        self.work = FakeWork(workspace_root=self.workspace)
        self.work.add_project("demo", PROJECT_ID, path="demo", name="Demo")
        url = self.work.start()
        self.global_config = self.tmp_path / "global.toml"
        self.global_config.write_text(CONFIG + f'api_url = "{url}"\n')
        self.env = mock.patch.dict(os.environ, {
            "ORCHESTRA_CONFIG": str(self.global_config),
            "ORCHESTRA_HOME": str(self.tmp_path / "home")})
        self.env.start()
        db.connect().close()
        self.cfg = config.load()
        self.client = sweeper.client_from_cfg(self.cfg)
        self.launched: list[tuple[Path, int]] = []
        self.launcher = lambda root, run_id: self.launched.append((root, run_id))

    def tearDown(self) -> None:
        self.env.stop()
        self.work.stop()
        self.tmp.cleanup()

    def sweep(self):
        return sweeper.sweep(self.cfg, self.client, launcher=self.launcher)

    def race_admissions(self, calls, locked_write=None):
        """Run admissions after every contender has reached its first BEGIN.

        The blocker forces a stale check-then-write implementation to read
        before either contender can insert. A correct BEGIN-then-check path
        serializes them and lets the loser observe the winner's reservation.
        Connections open before the blocker because ``db.connect`` itself
        refreshes schema metadata.
        """
        ready = threading.Barrier(len(calls) + 1)
        start = threading.Event()
        began = [threading.Event() for _ in calls]
        results = queue.Queue()

        def worker(index, call):
            con = db.connect()
            traced_begin = False

            def trace(statement):
                nonlocal traced_begin
                if not traced_begin and statement.lstrip().upper().startswith("BEGIN"):
                    traced_begin = True
                    began[index].set()

            con.set_trace_callback(trace)
            try:
                ready.wait(timeout=10)
                if not start.wait(timeout=10):
                    raise TimeoutError("admission race never started")
                results.put((index, call(con), None))
            except BaseException as exc:
                results.put((index, None, exc))
            finally:
                con.close()

        threads = [threading.Thread(target=worker, args=(i, call))
                   for i, call in enumerate(calls)]
        for thread in threads:
            thread.start()
        blocker = None
        try:
            ready.wait(timeout=10)
            blocker = db.connect()
            blocker.execute("BEGIN IMMEDIATE")
            if locked_write is not None:
                locked_write(blocker)
            start.set()
            for event in began:
                self.assertTrue(event.wait(timeout=10),
                                "contender never reached database admission")
            blocker.commit()
        finally:
            start.set()
            if blocker is not None:
                if blocker.in_transaction:
                    blocker.rollback()
                blocker.close()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive(), "admission contender did not finish")
        ordered = sorted((results.get_nowait() for _ in calls), key=lambda item: item[0])
        for _, _, error in ordered:
            self.assertIsNone(error, repr(error))
        return [value for _, value, _ in ordered]

    def db_run(self, run_id=None):
        con = db.connect()
        query = "SELECT * FROM runs" + (" WHERE id=?" if run_id else
                                        " ORDER BY id DESC LIMIT 1")
        run = con.execute(query, (run_id,) if run_id else ()).fetchone()
        con.close()
        return run

    def finish_run(self, run_id, status="done", summary="all good",
                   session_ref=None):
        con = db.connect()
        finished = db.now()
        con.execute(
            "UPDATE runs SET status=?, summary=?, finished_at=?, "
            "session_ref=COALESCE(?, session_ref) WHERE id=?",
            (status, summary, finished, session_ref, run_id))
        if status in db.RUN_TERMINAL:
            con.execute("UPDATE runs SET handoff_processed_at=? WHERE id=?",
                        (finished, run_id))
            con.execute(
                "INSERT INTO messages(run_id, sender, body, kind, created_at) "
                "SELECT ?, 'orchestra', ?, 'completion', ? WHERE NOT EXISTS ("
                "SELECT 1 FROM messages WHERE run_id=? AND kind='completion')",
                (run_id, f"run {run_id} finished: {status}", finished, run_id))
        con.commit()
        con.close()

    def item_log(self, item_id="W-0001") -> str:
        return " ".join(str(e) for e in self.work.tasks[item_id].get("log", []))

    def age_progress(self, run_id) -> None:
        """Let the next sweep past the progress rate limit."""
        self.db_exec("UPDATE runs SET work_progress_at='2000-01-01T00:00:00Z' "
                     "WHERE id=?", run_id)

    def db_exec(self, sql, *args) -> None:
        con = db.connect()
        con.execute(sql, args)
        con.commit()
        con.close()

    def db_one(self, sql, *args):
        con = db.connect()
        row = con.execute(sql, args).fetchone()
        con.close()
        return row

    def dispatch_item(self, item="W-0001", title="job", **kw):
        """Add a delegated item, sweep once, return its run row."""
        add = self.work.add_issue if item.startswith("issue") else self.work.add_task
        add(item, title, delegated=True, **kw)
        self.sweep()
        return self.db_run()

    def mark_running(self, run_id, session_ref) -> None:
        self.db_exec("UPDATE runs SET status='running', session_ref=? "
                     "WHERE id=?", session_ref, run_id)

    def report(self, run_id):
        """One report_result pass over the run's current row."""
        con = db.connect()
        try:
            return sweeper.report_result(con, self.client,
                                         dict(self.db_run(run_id)))
        finally:
            con.close()

    def lose_response(self, method="log_task", marker=None):
        """Patch a client call so the fake commits but the response is lost
        (None) for calls whose body contains ``marker`` — every call when
        ``marker`` is None."""
        real = getattr(self.client, method)

        def committed_then_lost(item_id, *args):
            result = real(item_id, *args)
            return None if marker is None or marker in args[0] else result

        return mock.patch.object(self.client, method, committed_then_lost)

    def add_config(self, extra: str) -> None:
        self.global_config.write_text(self.global_config.read_text() + extra)

    def enable_worktree(self) -> None:
        self.global_config.write_text(self.global_config.read_text().replace(
            "worktree = false", "worktree = true"))

    def init_repo(self, message="seed") -> None:
        for args in (["init", "-q"], ["config", "user.email", "t@example.com"],
                     ["config", "user.name", "t"]):
            subprocess.run(["git", *args], cwd=self.root, check=True)
        (self.root / "seed.txt").write_text("seed\n")
        subprocess.run(["git", "add", "seed.txt"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", message], cwd=self.root,
                       check=True)


class SweeperTestCase(SweeperFixture, unittest.TestCase):

    # --- claim --------------------------------------------------------------

    def test_claims_and_dispatches_assigned_ready_task(self) -> None:
        self.work.add_task("W-0001", "Fix the flaky test", delegated=True,
                           goal="Make CI green again.")
        actions = self.sweep()
        self.assertEqual([a["action"] for a in actions], ["dispatch"])
        run = self.db_run()
        self.assertEqual(run["work_item"], "W-0001")
        self.assertEqual(run["requested_by"], "work")
        self.assertEqual(self.launched, [(self.root, run["id"])])
        self.assertEqual(self.work.tasks["W-0001"]["status"], "in_progress")
        log = "\n".join(e["message"] for e in self.work.tasks["W-0001"]["log"])
        self.assertIn(f"dispatched run {run['id']}", log)
        text = Path(run["brief_path"]).read_text()
        self.assertIn("## Work item snapshot", text)
        self.assertIn("Make CI green again.", text)

    def test_a_parent_with_children_is_ordinary_work(self) -> None:
        """The container refusal is gone from both sides: Work made a task
        with subtasks workable everywhere and moved epics to their own E-####
        kind, and a claim is now an append, which nothing referees. So the
        sweeper has no container case left to skip."""
        self.work.add_task("W-0100", "The parent", delegated=True)
        self.work.add_task("W-0101", "The slice", delegated=True,
                           parent_id="W-0100")
        actions = self.sweep()
        dispatched = [a for a in actions if a.get("action") == "dispatch"]
        self.assertEqual(sorted(a["item"] for a in dispatched),
                         ["W-0100", "W-0101"])

    def test_claims_assigned_queued_issue(self) -> None:
        self.work.add_issue("issue_auth_timeout", "Auth times out",
                            delegated=True, body="Login hangs on retry.")
        self.sweep()
        issue = self.work.issues["issue_auth_timeout"]
        self.assertEqual(issue["state"], "in_progress")
        self.assertEqual(issue["claimedBy"], {"kind": "agent", "name": "orchestra"})
        self.assertIn("dispatched run", issue["messages"][-1]["body"])
        run = self.db_run()
        self.assertEqual(run["work_item"], "issue_auth_timeout")
        self.assertIn("Login hangs on retry.",
                      Path(run["brief_path"]).read_text())

    def test_lost_task_claim_response_reuses_the_reserved_run(self) -> None:
        self.work.add_task("W-0001", "claim once", delegated=True)

        with mock.patch.object(
                self.client, "log_task",
                side_effect=WorkError(503, "unavailable", "try again")):
            self.assertEqual(self.sweep(), [])
        pending = self.db_run()
        self.assertEqual(pending["work_claim_status"], "pending")

        with self.lose_response(marker=" fact: claimed"):
            self.assertEqual(self.sweep(), [])
        still_pending = self.db_run()
        self.assertEqual(still_pending["id"], pending["id"])
        self.assertEqual(still_pending["status"], "pending")
        self.assertEqual(still_pending["work_claim_status"], "pending")
        self.assertIsNone(still_pending["brief_path"])
        self.assertEqual(self.launched, [])

        actions = self.sweep()
        self.assertEqual(actions, [{"action": "dispatch", "item": "W-0001",
                                    "run": pending["id"], "resumed": False}])
        settled = self.db_run()
        self.assertEqual(settled["id"], pending["id"])
        self.assertEqual(settled["work_claim_status"], "claimed")
        self.assertEqual(self.launched, [(self.root, pending["id"])])
        claim = f"fact: claimed run={pending['id']}"
        self.assertEqual(sum(claim in entry["message"]
                             for entry in self.work.tasks["W-0001"]["log"]), 1)
        self.sweep()
        self.assertEqual(self.launched, [(self.root, pending["id"])])

    def test_human_override_settles_a_prepared_pending_claim(self) -> None:
        self.work.add_task("W-0001", "do not launch stale work", delegated=True)

        with self.lose_response(marker=" fact: claimed"):
            self.assertEqual(self.sweep(), [])
        pending = self.db_run()
        con = db.connect()
        self.assertEqual(sweeper._confirm_claim(
            con, self.client, "task", self.work.tasks["W-0001"], pending),
            "claimed")
        supervise.prepare_launch(
            con, self.root, self.cfg, pending, mission="prepared before crash",
            use_worktree=False,
            work_snapshot=sweeper.render_snapshot(
                self.work.tasks["W-0001"], "task"))
        con.commit()
        prepared = dict(con.execute("SELECT * FROM runs WHERE id=?",
                                    (pending["id"],)).fetchone())
        con.close()
        self.assertTrue(Path(prepared["brief_path"]).exists())

        self.work.human_move("W-0001", "backlog")
        self.assertEqual(self.sweep(), [])
        abandoned = self.db_run(pending["id"])
        self.assertEqual(abandoned["status"], "killed")
        self.assertEqual(abandoned["work_claim_status"], "abandoned")
        self.assertFalse(Path(prepared["brief_path"]).exists())
        self.assertEqual(self.db_one(
            "SELECT COUNT(*) FROM observations WHERE run_id=? AND layer='retry'",
            pending["id"])[0], 0)

    def test_lost_issue_claim_response_reconciles_claimed_by(self) -> None:
        self.work.add_issue("issue_once", "claim once", delegated=True)

        with self.lose_response(method="claim_issue"):
            self.assertEqual(self.sweep(), [])
        pending = self.db_run()
        self.assertEqual((pending["status"], pending["work_claim_status"]),
                         ("pending", "pending"))
        self.assertEqual(self.work.issues["issue_once"]["claimedBy"],
                         {"kind": "agent", "name": "orchestra"})
        self.assertEqual(self.launched, [])

        actions = self.sweep()
        self.assertEqual(actions, [{"action": "dispatch", "item": "issue_once",
                                    "run": pending["id"], "resumed": False}])
        self.assertEqual(self.db_run()["id"], pending["id"])
        self.assertEqual(self.db_run()["work_claim_status"], "claimed")
        bodies = [entry["body"]
                  for entry in self.work.issues["issue_once"]["messages"]]
        self.assertEqual(sum("fact: claimed" in body for body in bodies), 1)
        self.sweep()
        self.assertEqual(self.launched, [(self.root, pending["id"])])

    def test_unassigned_wrong_state_or_legacy_items_are_untouched(self) -> None:
        self.work.add_task("W-0001", "not ours", agents=["someone_else"])
        self.work.add_task("W-0002", "ours but backlog", delegated=True,
                           status="backlog")
        # A legacy agents list is history, not delegation: it records which
        # agent did the work in an older system. Reading it as delegation
        # offered 96 finished records to the runner, so Work stopped emitting
        # the key and the contract stopped honouring it. Only an explicit
        # human tick dispatches.
        self.work.add_task("W-0003", "legacy shape", agents=["orchestra"])
        del self.work.tasks["W-0003"]["delegated"]
        self.assertEqual(self.sweep(), [])
        self.assertIsNone(self.db_run())

    def test_no_double_dispatch_while_run_is_live(self) -> None:
        self.work.add_task("W-0001", "one run only", delegated=True)

        def admit(con):
            return supervise.create_run(
                con, profile="stub", backend="opencode", requested_by="work",
                workdir=str(self.root), project_id=PROJECT_ID,
                work_item="W-0001")

        outcomes = self.race_admissions([admit, admit])
        admitted = [row for row, reason in outcomes if row is not None]
        refused = [reason for row, reason in outcomes if row is None]
        self.assertEqual(len(admitted), 1)
        self.assertEqual(refused, [f"work_item:{admitted[0]['id']}"])
        # Even if the item somehow reads ready again, a live run blocks it.
        self.work.human_move("W-0001", "ready")
        actions = self.sweep()
        self.assertNotIn("dispatch", [a["action"] for a in actions])
        self.assertEqual(self.db_one("SELECT COUNT(*) AS n FROM runs")["n"], 1)

    # --- ferry --------------------------------------------------------------

    def test_human_comment_is_ferried_to_the_live_run(self) -> None:
        run = self.dispatch_item(title="with feedback")
        self.mark_running(run["id"], "sess-1")
        self.work.human_log("W-0001", "Prefer tabs over spaces here.")
        actions = self.sweep()
        self.assertIn({"action": "ferry", "item": "W-0001", "run": run["id"],
                       "comments": 1}, actions)
        msg = self.db_one(
            "SELECT * FROM messages WHERE run_id=? AND kind='interrupt'",
            run["id"])
        seen = self.db_one("SELECT work_seen_ts FROM runs WHERE id=?",
                           run["id"])["work_seen_ts"]
        self.assertIn("Prefer tabs over spaces here.", msg["body"])
        self.assertEqual(msg["sender"], "work:W-0001")
        self.assertIsNone(msg["delivered_at"])  # supervisor owns delivery
        self.assertEqual(seen, self.work.tasks["W-0001"]["log"][-1]["at"])
        # Same comment is never ferried twice.
        again = self.sweep()
        self.assertNotIn("ferry", [a["action"] for a in again])

    def test_issue_human_reply_is_ferried(self) -> None:
        run = self.dispatch_item("issue_x", "threaded")
        self.mark_running(run["id"], "sess-9")
        self.work.human_reply("issue_x", "Also cover the SSO path.")
        self.sweep()
        msg = self.db_one(
            "SELECT body FROM messages WHERE run_id=? AND kind='interrupt'",
            run["id"])
        self.assertIn("Also cover the SSO path.", msg["body"])

    def test_ferry_waits_for_a_session_ref(self) -> None:
        self.dispatch_item(title="early comment")
        self.work.human_log("W-0001", "too early")
        actions = self.sweep()  # run has no session_ref yet
        self.assertNotIn("ferry", [a["action"] for a in actions])

    def test_ferry_losing_the_terminal_race_is_a_no_op(self) -> None:
        run = self.dispatch_item(title="late comment")
        self.mark_running(run["id"], "sess-1")
        self.work.human_log("W-0001", "too late")
        with mock.patch.object(sweeper.messaging, "queue_tell",
                               side_effect=sweeper.messaging.RunClosed("done")):
            actions = self.sweep()
        self.assertNotIn("ferry", [action["action"] for action in actions])

    # --- report -------------------------------------------------------------

    def test_report_waits_for_completion_and_handoff_receipts(self) -> None:
        run = self.dispatch_item(title="not finalized yet")
        self.db_exec("UPDATE runs SET status='done', summary='done', "
                     "finished_at=? WHERE id=?", db.now(), run["id"])
        before = self.work.mutation_count()
        self.assertEqual(self.report(run["id"]), (True, None))
        self.db_exec(
            "INSERT INTO messages(run_id, sender, body, kind, created_at) "
            "VALUES(?, 'orchestra', 'finished', 'completion', ?)",
            run["id"], db.now())
        self.assertEqual(self.report(run["id"]), (True, None))
        self.db_exec("UPDATE runs SET handoff_processed_at=? WHERE id=?",
                     db.now(), run["id"])
        settled, action = self.report(run["id"])
        self.assertTrue(settled)
        self.assertEqual(action["action"], "report")
        self.assertGreater(self.work.mutation_count(), before)

    def test_lost_report_comment_response_does_not_duplicate_the_comment(self) -> None:
        run = self.dispatch_item(title="ambiguous comment")
        self.finish_run(run["id"], "done", "Shipped once.")

        with mock.patch.object(
                self.client, "task",
                side_effect=WorkError(503, "unavailable", "try again")):
            self.assertEqual(self.report(run["id"]), (False, None))
        self.assertIsNone(self.db_run(run["id"])["work_reported_at"])

        with self.lose_response(marker=" finished: "):
            self.assertEqual(self.report(run["id"]), (False, None))
        settled, _ = self.report(run["id"])
        self.assertTrue(settled)
        messages = [entry["message"] for entry in self.work.tasks["W-0001"]["log"]]
        self.assertEqual(sum(" finished: done" in message for message in messages), 1)

    def test_lost_report_fact_response_is_reconciled_before_retry(self) -> None:
        run = self.dispatch_item(title="ambiguous fact")
        self.finish_run(run["id"], "done", "Shipped once.")

        with self.lose_response(marker=" fact: landed"):
            self.assertEqual(self.report(run["id"]), (False, None))
        settled, action = self.report(run["id"])
        self.assertTrue(settled)
        self.assertEqual(action["action"], "report")
        messages = [entry["message"] for entry in self.work.tasks["W-0001"]["log"]]
        self.assertEqual(sum(" fact: landed" in message for message in messages), 1)

    def test_lost_issue_fact_response_is_reconciled_after_close(self) -> None:
        run = self.dispatch_item("issue_once", "ambiguous issue fact")
        self.finish_run(run["id"], "done", "Resolved once.")

        with self.lose_response(method="reply_issue", marker=" fact: resolved"):
            self.assertEqual(self.report(run["id"]), (False, None))
        settled, action = self.report(run["id"])
        self.assertTrue(settled)
        self.assertEqual(action["to"], "closed")
        messages = [entry["body"] for entry in self.work.issues["issue_once"]["messages"]]
        self.assertEqual(sum(" fact: resolved" in message for message in messages), 1)

    def test_completed_run_reports_a_landed_fact_and_writes_no_status(self) -> None:
        run = self.dispatch_item(title="finishes well")
        self.finish_run(run["id"], "done", "Shipped the fix; tests pass.")
        self.db_exec("UPDATE runs SET branch='orchestra/run-race' WHERE id=?",
                     run["id"])
        self.db_exec(
            "INSERT INTO messages(run_id, sender, body, kind, created_at) "
            "VALUES(?, 'orchestra', 'old readiness signal', 'merge', ?)",
            run["id"], db.now())
        before = self.work.mutation_count()
        self.assertEqual(self.sweep(), [], "a terminal row is not a landing")
        self.assertEqual(self.work.mutation_count(), before)
        self.assertIsNone(self.db_run(run["id"])["work_reported_at"])
        self.db_exec("UPDATE runs SET landing_status='ok' WHERE id=?", run["id"])

        with mock.patch.object(self.client, "log_task", return_value=None):
            self.assertEqual(self.report(run["id"]), (False, None))
        self.assertIsNone(self.db_run(run["id"])["work_reported_at"])
        settled, action = self.report(run["id"])
        self.assertTrue(settled)
        self.assertEqual(action, {"action": "report", "item": "W-0001",
                                  "run": run["id"], "to": "review"})
        task = self.work.tasks["W-0001"]
        self.assertEqual(task["status"], "review")
        # Derived, never written: the human's own status never moved.
        self.assertEqual(task["storedStatus"], "ready")
        log = "\n".join(e["message"] for e in task["log"])
        self.assertIn(f"fact: claimed run={run['id']}", log)
        self.assertIn("fact: landed", log)
        self.assertIn("finished: done", log)
        self.assertIn("Shipped the fix; tests pass.", log)
        self.assertIsNotNone(self.db_run(run["id"])["work_reported_at"])

    def test_failed_run_reports_a_failed_fact_and_reads_blocked(self) -> None:
        run = self.dispatch_item(title="goes badly")
        self.finish_run(run["id"], "failed", "Cannot find the config file.")
        policy = db.connect()
        policy.execute(
            "INSERT INTO observations(run_id, layer, action, reason, created_at) "
            "VALUES(?, 'retry', 'deferred', 'retry policy pending', ?)",
            (run["id"], db.now()))
        policy.commit()
        before = self.work.mutation_count()
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.work.mutation_count(), before,
                         "a deferred retry must not publish a failure")

        newer, blocked = supervise.create_run(
            policy, profile=run["profile"], backend=run["backend"],
            requested_by="retry", workdir=run["workdir"],
            project_id=run["project_id"], work_item=run["work_item"])
        self.assertIsNone(blocked)
        policy.execute(
            "INSERT INTO observations(run_id, layer, action, reason, created_at) "
            "VALUES(?, 'retry', 'retry', 'newer attempt owns the item', ?)",
            (run["id"], db.now()))
        policy.commit()
        policy.close()
        self.finish_run(newer["id"], "failed",
                        "Retry could not find the config file.")
        self.sweep()
        self.assertEqual(self.work.tasks["W-0001"]["status"], "blocked")
        # The board shows the fact's reason, so it must ride along --
        # "blocked" with no cause tells the human nothing.
        notes = " ".join(str(entry) for entry in self.work.tasks["W-0001"].get("log", []))
        self.assertIn("fact: failed reason=", notes)
        self.assertIn("Retry could not find the config file.", notes)
        self.assertNotIn(f"run {run['id']} finished: failed", notes)
        self.assertIsNone(self.db_run(run["id"])["work_reported_at"])
        self.assertIsNotNone(self.db_run(newer["id"])["work_reported_at"])

    def test_the_brief_teaches_the_checklist_verb_to_task_runs_only(self) -> None:
        # Issues carry no checklist, and a brief never teaches a verb the run
        # cannot use (D11).
        self.work.add_task("W-0001", "gated job", delegated=True,
                           acceptance=("proves it", "and the other thing"))
        self.work.add_issue("issue_abc123", "no checklist here", delegated=True)
        self.sweep()
        con = db.connect()
        briefs = {r["work_item"]: Path(r["brief_path"]).read_text()
                  for r in con.execute("SELECT work_item, brief_path FROM runs")}
        con.close()
        self.assertIn("work check W-0001 requirement|acceptance <index>",
                      briefs["W-0001"])
        self.assertIn("--decline", briefs["W-0001"])
        self.assertNotIn("work check", briefs["issue_abc123"])

    def test_a_run_that_leaves_criteria_open_is_accounted_for_anyway(self) -> None:
        # CONTRACT §3 verb 2: nothing leaves the run's hands unanswered. A run
        # that dies without ticking or declining anything answers for none of
        # them, so the sweeper declines what is left before reporting its fact.
        run = self.dispatch_item(
            title="dies mid-flight",
            acceptance=("proves it", "and the other thing"))
        self.finish_run(run["id"], "failed", "Cannot find the config file.")
        actions = self.sweep()

        task = self.work.tasks["W-0001"]
        self.assertEqual(task["status"], "blocked")
        self.assertIn({"action": "report", "item": "W-0001", "run": run["id"],
                       "to": "blocked"}, actions)
        # Declined, never ticked: the sweeper verified nothing and says so.
        self.assertEqual([(i["checked"], i["declined"], i["reason"])
                          for i in task["acceptanceCriteria"]],
                         [(False, True, f"not accounted for by run {run['id']} (failed)")] * 2)
        self.assertIsNotNone(self.db_run(run["id"])["work_reported_at"])

    def test_what_the_run_ticked_itself_is_left_alone(self) -> None:
        run = self.dispatch_item(
            title="half done",
            acceptance=("proves it", "and the other thing"))
        # The worker ticked the first criterion and declined nothing else.
        self.client.check_task_item("W-0001", "acceptance", 0, checked=True)
        self.finish_run(run["id"], "failed", "ran out of road")
        self.sweep()

        items = self.work.tasks["W-0001"]["acceptanceCriteria"]
        self.assertEqual((items[0]["checked"], items[0]["declined"]), (True, False))
        self.assertTrue(items[1]["declined"])

    def test_a_success_that_skipped_its_criteria_lands_and_is_accounted_for(self) -> None:
        # The referee is gone: the run says it landed, and the sweeper no
        # longer downgrades that claim to blocked on its behalf. It answers
        # for the criteria the run left silent -- declined, naming the run --
        # and the human reads both on the board.
        run = self.dispatch_item(title="claims success",
                                 acceptance=("proves it",))
        self.finish_run(run["id"], "done", "Shipped it.")
        self.sweep()

        task = self.work.tasks["W-0001"]
        self.assertEqual(task["status"], "review")
        criterion = task["acceptanceCriteria"][0]
        self.assertEqual((criterion["checked"], criterion["declined"]),
                         (False, True))
        self.assertIn(f"run {run['id']}", criterion["reason"])

    def test_a_dispatched_brief_names_what_recently_landed(self) -> None:
        # End to end: real repository, real git log, real brief on disk.
        self.init_repo("the observer looks after 5m")
        run = self.dispatch_item(title="build the thing")
        text = Path(run["brief_path"]).read_text()
        self.assertIn("## Recently landed here", text)
        self.assertIn("the observer looks after 5m", text)

    def test_a_brief_outside_a_repository_has_no_empty_commit_block(self) -> None:
        run = self.dispatch_item(title="build the thing")
        self.assertNotIn("Recently landed", Path(run["brief_path"]).read_text())

    def test_swept_run_isolates_and_cleans_up_if_supervisor_never_starts(self) -> None:
        self.enable_worktree()
        self.init_repo()
        run = self.dispatch_item(title="isolated job")
        self.assertNotEqual(run["workdir"], str(self.root),
                            "a swept run must not share the human's checkout")
        self.assertEqual(run["branch"], f"orchestra/run-{run['id']}")

        self.launcher = mock.Mock(side_effect=RuntimeError("supervisor absent"))
        self.work.add_task("W-0002", "failed isolated job", delegated=True)
        actions = self.sweep()
        failed = self.db_run()
        self.assertEqual([a["action"] for a in actions], ["launch_failed"])
        self.assertEqual(failed["status"], "failed")
        self.assertIsNone(failed["work_reported_at"])
        self.assertEqual(self.db_one(
            "SELECT action FROM observations WHERE run_id=? AND layer='retry'",
            failed["id"])["action"], "deferred")
        self.assertEqual(failed["workdir"], str(self.root))
        self.assertIsNone(failed["branch"])
        self.assertIn("supervisor absent", failed["summary"])
        branches = subprocess.run(
            ["git", "branch", "--list", f"orchestra/run-{failed['id']}"],
            cwd=self.root, check=True, capture_output=True, text=True).stdout
        self.assertEqual(branches.strip(), "")
        worktrees = subprocess.run(
            ["git", "worktree", "list", "--porcelain"], cwd=self.root,
            check=True, capture_output=True, text=True).stdout
        self.assertNotIn(f"/run-{failed['id']}", worktrees)

    def test_swept_run_fails_closed_when_worktree_is_impossible(self) -> None:
        self.enable_worktree()
        # The fixture root is not a git repo. It must not silently become a
        # shared-checkout run.
        self.work.add_task("W-0001", "no repo here", delegated=True)
        actions = self.sweep()
        run = self.db_run()
        self.assertEqual(actions[0]["action"], "launch_failed")
        self.assertEqual(len(actions), 1)
        self.assertEqual(run["workdir"], str(self.root))
        self.assertEqual(run["status"], "failed")
        self.assertIn("--worktree needs", run["summary"])
        self.assertEqual(self.work.tasks["W-0001"]["status"], "in_progress")
        self.assertIsNone(run["work_reported_at"])
        self.assertEqual(self.launched, [])
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.db_one("SELECT COUNT(*) AS n FROM runs")["n"], 1)
        self.assertEqual(self.db_one(
            "SELECT action FROM observations WHERE run_id=? AND layer='retry'",
            run["id"])["action"], "deferred")

    def test_progress_heartbeat_posts_then_rate_limits(self) -> None:
        run = self.dispatch_item(title="long job")
        Path(run["log_path"]).write_text(tool_line("bash", command="pytest -q"))

        self.sweep()  # first pass after activity: heartbeat posts
        notes = self.item_log()
        self.assertIn("still working", notes)
        self.assertIn("pytest -q", notes)
        before = notes.count("still working")

        self.sweep()  # immediately again: rate-limited, no second heartbeat
        self.assertEqual(self.item_log().count("still working"), before)

    def test_progress_count_advances_as_the_trace_grows(self) -> None:
        """Current fields carry current facts: a frozen progress line makes a
        healthy run indistinguishable from a hung one (I-0121). Run 234 held
        at "1 tool call" for 40 minutes because only shell commands counted,
        while it went on reading and grepping 80-odd times."""
        run = self.dispatch_item(title="long job")
        log = Path(run["log_path"])
        log.write_text(tool_line("bash", command="pytest -q"))
        self.sweep()
        self.assertIn("1 tool call;", self.item_log())

        with log.open("a") as handle:  # more tools, none of them a shell command
            handle.write(tool_line("read", filePath="/src/db.py"))
            handle.write(tool_line("grep", pattern="def main"))
        self.age_progress(run["id"])
        self.sweep()

        notes = self.item_log()
        self.assertIn("3 tool calls;", notes)
        self.assertIn("last: grep def main", notes)

    def test_progress_heartbeat_skips_terminal_runs(self) -> None:
        run = self.dispatch_item(title="quick job")
        Path(run["log_path"]).write_text(tool_line("bash", command="ls"))
        self.finish_run(run["id"], "done", "finished")
        self.sweep()
        self.assertNotIn("still working", self.item_log())

    def test_issue_outcomes_closed_and_needs_human(self) -> None:
        self.work.add_issue("issue_good", "resolves", delegated=True)
        self.work.add_issue("issue_bad", "stalls", delegated=True)
        self.sweep()
        con = db.connect()
        runs = {r["work_item"]: r["id"] for r in
                con.execute("SELECT id, work_item FROM runs")}
        con.close()
        self.finish_run(runs["issue_good"], "done", "Root cause fixed.")
        self.finish_run(runs["issue_bad"], "failed", "Need credentials.")
        self.sweep()
        # A successful run closes its issue with a summary ("resolved" was
        # collapsed into "closed"); the human can always reopen.
        self.assertEqual(self.work.issues["issue_good"]["state"], "closed")
        self.assertEqual(self.work.issues["issue_good"]["resolutionSummary"],
                         "Root cause fixed.")
        self.assertEqual(self.work.issues["issue_bad"]["state"], "needs_human")
        entries = self.client.needs_you()
        self.assertEqual([e["id"] for e in entries], ["issue_bad"])

    def test_a_legacy_transition_is_bridged_into_its_fact(self) -> None:
        """Orchestra sends facts now, but Work still accepts the old move
        call and records it as the fact it meant — the wire shape stays
        compatible, and neither side needs a lockstep deploy (CONTRACT 0.8).
        Nothing an agent sends writes stored status; done is still refused."""
        self.work.add_task("W-0001", "legacy caller", delegated=True)
        move = "/api/tasks/W-0001/move"
        self.client._call("POST", move,
                          {"status": "in_progress", "note": "run 7 dispatched"})
        self.assertEqual(self.work.tasks["W-0001"]["status"], "in_progress")
        self.assertEqual(self.client._call("POST", move,
                                           {"status": "review"})["status"],
                         "review")
        self.assertEqual(self.work.tasks["W-0001"]["storedStatus"], "ready")
        log = "\n".join(e["message"] for e in self.work.tasks["W-0001"]["log"])
        self.assertIn("fact: claimed run=7", log)
        self.assertIn("fact: landed", log)
        with self.assertRaises(WorkError) as ctx:
            self.client._call("POST", move, {"status": "done"})
        self.assertEqual(ctx.exception.code, "task_status_forbidden")

    # --- ghosts: a report against a human-moved item is history --------------

    def test_a_report_after_a_human_move_changes_nothing_but_history(self) -> None:
        """Runs 234/238/240/242/249: the human closed the item, the run was
        killed, and its late report reopened what the human had settled. A
        human move dismisses every earlier run's narrative, so the report
        lands as history — the board does not move, and nothing waits."""
        run = self.dispatch_item(title="settled by hand")
        self.work.human_move("W-0001", "done")          # the human settles it
        self.finish_run(run["id"], "killed", "stopped mid-flight")
        actions = self.sweep()

        task = self.work.tasks["W-0001"]
        self.assertEqual(task["status"], "done", "the ghost cannot reopen it")
        self.assertEqual(task["storedStatus"], "done")
        log = "\n".join(e["message"] for e in task["log"])
        self.assertIn("fact: failed", log, "the report is recorded, not refused")
        self.assertIn({"action": "report", "item": "W-0001", "run": run["id"],
                       "to": "blocked"}, actions)
        self.assertEqual([e["id"] for e in self.client.needs_you()], [])
        # Once only: the report never runs again, and nothing dispatches.
        self.assertIsNotNone(self.db_run(run["id"])["work_reported_at"])
        self.assertEqual(self.sweep(), [])

    def test_a_report_on_a_human_closed_issue_is_refused_once_not_forever(self) -> None:
        """The issue half of the same ghost. Work refuses a reply to an issue
        the human closed, and a refused append is terminal: stamp it reported
        and never retry (two closed issues once became permanent sweep noise,
        2026-08-20)."""
        run = self.dispatch_item("issue_x", "settled by hand")
        self.work.human_close_issue("issue_x", summary="I did it myself")
        self.finish_run(run["id"], "done", "Root cause fixed.")
        refused = self.report(run["id"])

        issue = self.work.issues["issue_x"]
        self.assertEqual(issue["state"], "closed")
        self.assertEqual(issue["resolutionSummary"], "I did it myself")
        self.assertEqual(refused, (True, None))
        self.assertIsNotNone(self.db_run(run["id"])["work_reported_at"])
        before = self.work.mutation_count()
        self.sweep()
        self.assertEqual(self.work.mutation_count(), before, "no retry loop")

    # --- the §4 step-5 loop: human answers, session resumes ------------------

    def test_human_answer_resumes_the_prior_session(self) -> None:
        first = self.dispatch_item(title="needs an answer")
        self.finish_run(first["id"], "failed", "Which auth provider?",
                        session_ref="sess-42")
        self.sweep()  # reports; task -> blocked
        self.assertEqual(self.work.tasks["W-0001"]["status"], "blocked")
        self.work.human_log("W-0001", "Use the Okta provider.")
        self.work.human_move("W-0001", "ready")
        actions = self.sweep()
        self.assertIn("dispatch", [a["action"] for a in actions])
        followup = self.db_run()
        self.assertEqual(followup["parent_run"], first["id"])
        self.assertEqual(followup["session_ref"], "sess-42")
        self.assertEqual(followup["work_item"], "W-0001")
        self.assertIn("Use the Okta provider.",
                      Path(followup["brief_path"]).read_text())
        self.assertEqual(self.work.tasks["W-0001"]["status"], "in_progress")

        # A backend session is single-writer even without a Work item. Force
        # two continuations to read the same terminal parent before either can
        # reserve its child; only one may be admitted.
        self.db_exec("UPDATE runs SET status='done', finished_at=?, "
                     "work_item=NULL WHERE id=?", db.now(), followup["id"])
        parent = dict(self.db_one("SELECT * FROM runs WHERE id=?",
                                  followup["id"]))

        def reserve(con):
            return supervise.reserve_followup(
                con, self.root, parent, "human", "one continuation")

        outcomes = self.race_admissions([reserve, reserve])
        admitted = [row for row, reason in outcomes if row is not None]
        refused = [reason for row, reason in outcomes if row is None]
        self.assertEqual(len(admitted), 1)
        self.assertEqual(refused, [f"session:{admitted[0]['id']}"])
        self.assertEqual(self.db_one(
            "SELECT COUNT(*) AS n FROM runs WHERE parent_run=?",
            followup["id"])["n"], 1)

    def test_lost_continuation_claim_keeps_its_atomic_reservation(self) -> None:
        first = self.dispatch_item(title="needs an answer")
        self.finish_run(first["id"], "failed", "Which provider?",
                        session_ref="sess-42")
        self.sweep()
        self.work.human_log("W-0001", "Use Okta.")
        self.work.human_move("W-0001", "ready")

        with self.lose_response(marker=" fact: claimed"):
            self.assertEqual(self.sweep(), [])
        pending = self.db_run()
        self.assertEqual(pending["parent_run"], first["id"])
        self.assertEqual(pending["session_ref"], "sess-42")
        self.assertEqual((pending["status"], pending["work_claim_status"]),
                         ("pending", "pending"))

        actions = self.sweep()
        self.assertEqual(actions, [{"action": "dispatch", "item": "W-0001",
                                    "run": pending["id"], "resumed": True}])
        resumed = self.db_run()
        self.assertEqual(resumed["id"], pending["id"])
        self.assertEqual(resumed["work_claim_status"], "claimed")
        self.assertIn("Use Okta.", Path(resumed["brief_path"]).read_text())

    def test_failed_issue_continuation_releases_ownership_to_the_human(self) -> None:
        first = self.dispatch_item("issue_x", "needs an answer")
        self.finish_run(first["id"], "failed", "Which provider?",
                        session_ref="sess-42")
        self.sweep()
        self.work.human_reply("issue_x", "Use Okta.")
        before = first["id"]
        with mock.patch.object(sweeper.supervise.brief, "compose_continuation",
                               side_effect=SystemExit("cannot compose")):
            actions = self.sweep()
        self.assertEqual(actions[0]["action"], "launch_failed")
        self.assertEqual(len(actions), 1)
        self.assertEqual(self.work.issues["issue_x"]["state"], "in_progress")
        failed = self.db_run()
        self.assertGreater(failed["id"], before)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["work_item"], "issue_x")
        self.assertIn("cannot compose", failed["summary"])
        self.assertIsNone(failed["work_reported_at"])
        self.assertEqual(self.db_one(
            "SELECT action FROM observations WHERE run_id=? AND layer='retry'",
            failed["id"])["action"], "deferred")
        self.assertEqual(self.sweep(), [])

    def test_a_halted_run_blocks_the_item_and_later_sweeps_pass_over_it(self) -> None:
        run = self.dispatch_item(title="doomed")
        self.finish_run(run["id"], "halted", "the vendor API is gone")
        actions = self.sweep()
        self.assertIn({"action": "report", "item": "W-0001", "run": run["id"],
                       "to": "blocked"}, actions)
        task = self.work.tasks["W-0001"]
        self.assertEqual(task["status"], "blocked")
        self.assertIn("the vendor API is gone", self.item_log())
        launched = len(self.launched)
        again = self.sweep()
        self.assertNotIn("dispatch", [a["action"] for a in again])
        self.assertEqual(len(self.launched), launched)
        self.assertEqual(self.work.tasks["W-0001"]["status"], "blocked")

    def test_clearing_a_halt_dispatches_fresh(self) -> None:
        first = self.dispatch_item(title="doomed")
        self.finish_run(first["id"], "halted", "the vendor API is gone",
                        session_ref="sess-42")
        self.sweep()
        self.work.human_move("W-0001", "ready")
        actions = self.sweep()
        self.assertIn("dispatch", [a["action"] for a in actions])
        fresh = self.db_run()
        self.assertNotEqual(fresh["id"], first["id"])
        self.assertIsNone(fresh["parent_run"])
        self.assertIsNone(fresh["session_ref"])
        self.assertEqual(fresh["work_item"], "W-0001")

    def test_a_killed_runs_session_is_never_resumed(self) -> None:
        """Live failure (run 27): the sweeper resumed a KILLED run's session,
        the backend answered `no session matches`, and the item failed in
        under a second. A stop is not a conversation to continue."""
        first = self.dispatch_item(title="stop this one")
        self.finish_run(first["id"], "killed", "stopped by a human",
                        session_ref="sess-42")
        self.sweep()
        self.work.human_move("W-0001", "ready")
        self.sweep()
        fresh = self.db_run()
        self.assertNotEqual(fresh["id"], first["id"], "the item was re-dispatched")
        self.assertIsNone(fresh["parent_run"], "a kill is not resumed")
        self.assertIsNone(fresh["session_ref"], "a fresh session, not the dead one")
        self.assertEqual(fresh["work_item"], "W-0001")

    def test_a_kill_also_blocks_resuming_the_run_behind_it(self) -> None:
        """The stop was aimed at the ITEM, not at one process, so an earlier
        session on the same item is not quietly resumed instead."""
        con = db.connect()
        for status, ref in (("done", "sess-old"), ("killed", "sess-42")):
            con.execute(
                "INSERT INTO runs(profile, backend, requested_by, workdir, status, "
                "started_at, work_item, session_ref) VALUES('stub','opencode',"
                "'work',?,?,?, 'W-0009', ?)",
                (str(self.root), status, db.now(), ref))
        con.commit()
        self.assertIsNone(sweeper._last_session_run(con, "W-0009"))
        con.execute("UPDATE runs SET status='timeout' WHERE session_ref='sess-42'")
        con.commit()
        self.assertEqual(sweeper._last_session_run(con, "W-0009")["session_ref"],
                         "sess-42", "a run that ended on its own still resumes")
        con.close()

    # --- cursor -------------------------------------------------------------

    def test_cursor_advances_and_a_second_pass_mutates_nothing(self) -> None:
        run = self.dispatch_item(title="cursor check")
        self.finish_run(run["id"], "done", "done deal")
        self.sweep()
        before = self.work.mutation_count()
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.work.mutation_count(), before)
        con = db.connect()
        cursor = db.meta_get(con, "work_cursor_tasks")
        con.close()
        self.assertEqual(cursor, self.work.tasks["W-0001"]["updatedAt"])

    def test_unreachable_work_degrades_gracefully(self) -> None:
        dead = WorkClient("http://127.0.0.1:9", identity="orchestra", timeout=0.5)
        self.assertEqual(sweeper.sweep(self.cfg, dead, launcher=self.launcher), [])
        con = db.connect()
        self.assertIsNone(db.meta_get(con, "work_cursor_tasks"))
        con.close()

    # --- snapshot -----------------------------------------------------------

    def test_snapshot_is_capped_and_frozen_at_dispatch(self) -> None:
        self.work.add_task("W-0001", "huge goal", delegated=True,
                           goal="x" * 5000)
        self.assertLessEqual(
            len(sweeper.render_snapshot(self.work.tasks["W-0001"], "task")),
            brief.WORK_SNAPSHOT_MAX_CHARS)
        self.sweep()
        run = self.db_run()
        frozen = Path(run["brief_path"]).read_text()
        self.assertLessEqual(frozen.count("x"), brief.WORK_SNAPSHOT_MAX_CHARS)
        self.work.human_log("W-0001", "changed after dispatch")
        self.sweep()
        self.assertEqual(Path(run["brief_path"]).read_text(), frozen)

    # --- config -------------------------------------------------------------

    def test_work_config_merges_and_gates(self) -> None:
        self.assertEqual(self.cfg["work"]["agent_identity"], "orchestra")
        self.assertEqual(self.cfg["work"]["poll_interval"], 7)
        self.global_config.write_text(
            CONFIG.replace("enabled = true", "enabled = false"))
        self.assertIsNone(sweeper.client_from_cfg(config.load()))

    # --- project resolution (DESIGN §2) --------------------------------------

    def test_two_projects_land_in_one_database(self) -> None:
        other_id = "b993cc1f-857d-450c-96ec-c8864f754bef"
        (self.workspace / "other").mkdir()
        self.work.add_project("other", other_id, path="other", name="Other")
        self.work.add_task("W-0001", "first project", delegated=True,
                           project_path="demo")
        self.work.add_task("W-0002", "second project", delegated=True,
                           project_path="other")
        self.sweep()
        con = db.connect()
        rows = {r["work_item"]: r for r in con.execute("SELECT * FROM runs")}
        con.close()
        self.assertEqual(rows["W-0001"]["project_id"], PROJECT_ID)
        self.assertEqual(rows["W-0001"]["workdir"], str(self.root))
        self.assertEqual(rows["W-0002"]["project_id"], other_id)
        self.assertEqual(rows["W-0002"]["workdir"], str(self.workspace / "other"))

    def test_item_in_an_unknown_project_is_skipped_not_dispatched(self) -> None:
        self.work.add_task("W-0001", "orphan", delegated=True,
                           project_path="never-heard-of-it")
        self.assertEqual([a["action"] for a in self.sweep()], [])
        self.assertIsNone(self.db_run())
        self.assertEqual(self.work.tasks["W-0001"]["status"], "ready")

    def test_per_project_overrides_pick_the_launch_profile(self) -> None:
        """DESIGN §2: [project."<projectId>"] in the one config file, not a
        file inside the project."""
        self.add_config(
            "\n[profiles.special]\nbackend = \"codex\"\n"
            + f"\n[project.\"{PROJECT_ID}\".work]\nprofile = \"special\"\n")
        self.assertEqual(self.dispatch_item(title="overridden")["profile"],
                         "special")

    # --- the enabled set (W-0187) --------------------------------------------

    def test_an_absent_enabled_set_staffs_as_before(self) -> None:
        """No [project."<id>"] table anywhere: every profile is enabled, so
        the move to an enabled set changes nothing for an existing install."""
        self.add_config(f"\n[project.\"{PROJECT_ID}\".settings]\ntimeout = 30\n")
        self.work.add_task("W-0001", "unchanged", delegated=True)
        self.assertEqual([a["action"] for a in self.sweep()], ["dispatch"])
        self.assertEqual(self.db_run()["profile"], "stub")

    def test_a_profile_the_project_has_not_enabled_is_refused_by_name(self) -> None:
        """Not a silent fallback to whatever IS enabled: nothing is staffed,
        the item stays claimable, and the refusal names the project."""
        self.add_config(OTHER_PROFILE_ONLY)
        self.work.add_task("W-0001", "not enabled here", delegated=True)
        with mock.patch("builtins.print") as printed:
            self.assertEqual([a["action"] for a in self.sweep()], [])
        said = "\n".join(str(c.args[0]) for c in printed.call_args_list)
        self.assertIn(PROJECT_ID, said)
        self.assertIn("'stub'", said)
        self.assertIn("other", said)          # the enabled set, named
        self.assertIsNone(self.db_run())      # nothing staffed on any profile
        self.assertEqual(self.work.tasks["W-0001"]["status"], "ready")

    def test_an_enabled_profile_still_dispatches(self) -> None:
        self.add_config(
            f"\n[project.\"{PROJECT_ID}\"]\nenabled_profiles = [\"stub\"]\n")
        self.work.add_task("W-0001", "enabled here", delegated=True)
        self.assertEqual([a["action"] for a in self.sweep()], ["dispatch"])
        self.assertEqual(self.db_run()["profile"], "stub")

    def test_orchestra_dispatch_refuses_a_profile_the_project_disabled(self) -> None:
        """`orchestra dispatch --to NAME` staffs a run, so it goes through the
        same gate the sweeper does — and says which project refused."""
        from argparse import Namespace

        from orchestra import cli
        self.add_config(OTHER_PROFILE_ONLY)
        con = db.connect()
        try:
            self.assertTrue(project.refresh(con, config.load()))
        finally:
            con.close()
        args = Namespace(mission=["do the thing"], to="stub", after=None,
                         brief_file=None, context=None, title=None,
                         worktree=False, sync=False)
        with mock.patch.dict(os.environ, {"ORCHESTRA_ROOT": str(self.root)}), \
                self.assertRaises(SystemExit) as caught:
            cli.cmd_dispatch(args)
        message = str(caught.exception)
        self.assertIn(PROJECT_ID, message)
        self.assertIn("'stub'", message)
        self.assertIn("Enabled there: other", message)
        self.assertIsNone(self.db_run())

    def test_a_run_in_flight_is_never_revalidated(self) -> None:
        """The owner's own line: "I'm okay with an existing run trying a stale
        preset, that's on me." So a run dispatched on `stub` keeps running on
        `stub` after the project disables it — the sweeper reports it,
        transitions its item, and nothing anywhere re-checks the preset."""
        run_id = self.dispatch_item(title="in flight")["id"]
        # the project changes its mind mid-run
        self.add_config(OTHER_PROFILE_ONLY)
        self.cfg = config.load()
        pcfg = config.load(PROJECT_ID)
        # the launch path reads through profile_cfg, which is ungated
        self.assertEqual(config.profile_cfg(pcfg, "stub")["name"], "stub")
        self.finish_run(run_id)
        self.assertEqual([a["action"] for a in self.sweep()], ["report"])
        self.assertEqual(self.db_run(run_id)["profile"], "stub")
        self.assertEqual(self.work.tasks["W-0001"]["status"], "review")


if __name__ == "__main__":
    unittest.main()
