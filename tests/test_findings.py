"""Completion handoff filing (DESIGN §9) against the fake Work API.

Covers the two required fields, the protocol failure an absent one is, the
fingerprint dedup, verb 5 with its goal-parent gate, and the tripwires that
force human review whatever a planner would have said.
"""
import json
import unittest
from unittest import mock

from orchestra import db, findings
from orchestra.work_client import WorkClient, WorkError
from tests.fake_work import FakeWork
from tests.test_sweeper import PROJECT_ID, SweeperFixture

HANDOFF = """\
Did the thing.

```json
{"findings": [%s], "proposals": [%s]}
```
"""

FINDING = ('{"claim": "retry loop drops the last error", "where": "runners.py:88", '
           '"confidence": "observed", "why_not_fixed": "outside this mission"}')
PROPOSAL = '{"title": "Add a retry test", "why": "the loop is untested"}'


def message(text: str) -> str:
    return HANDOFF % ("", "") if not text else text


class ParseTests(unittest.TestCase):
    def test_both_fields_present_and_empty_is_valid(self) -> None:
        handoff, problems = findings.parse_handoff(HANDOFF % ("", ""))
        self.assertEqual(problems, [])
        self.assertEqual(handoff, {"findings": [], "proposals": []})

    def test_absent_field_is_a_protocol_problem(self) -> None:
        _, problems = findings.parse_handoff('```json\n{"findings": []}\n```')
        self.assertEqual(len(problems), 1)
        self.assertIn("`proposals`", problems[0])

    def test_no_block_at_all_is_a_protocol_problem(self) -> None:
        handoff, problems = findings.parse_handoff("I finished. Nothing to report.")
        self.assertEqual(handoff, {"findings": [], "proposals": []})
        self.assertIn("no handoff block", problems[0])

    def test_last_block_wins_and_bare_fence_is_accepted(self) -> None:
        text = ('```json\n{"findings": [], "proposals": []}\n```\n'
                'then more work\n```\n{"findings": [%s], "proposals": []}\n```' % FINDING)
        handoff, problems = findings.parse_handoff(text)
        self.assertEqual(problems, [])
        self.assertEqual(len(handoff["findings"]), 1)

    def test_halt_reason_reads_the_handoff_marker(self) -> None:
        text = '```json\n{"findings": [], "proposals": [], "halt": "api gone"}\n```'
        self.assertEqual(findings.halt_reason(text), "api gone")
        self.assertEqual(findings.halt_reason(
            '```json\n{"halt": "doomed without a handoff"}\n```'),
            "doomed without a handoff")
        self.assertIsNone(findings.halt_reason(HANDOFF % ("", "")))
        self.assertIsNone(findings.halt_reason('```json\n{"halt": "  "}\n```'))
        self.assertIsNone(findings.halt_reason("no block here"))

    def test_non_list_field_does_not_lose_the_sibling(self) -> None:
        handoff, problems = findings.parse_handoff(
            '```json\n{"findings": "none", "proposals": [%s]}\n```' % PROPOSAL)
        self.assertEqual(handoff["findings"], [])
        self.assertEqual(len(handoff["proposals"]), 1)
        self.assertIn("not a list", problems[0])

    def test_bad_confidence_is_recorded_as_suspected_not_dropped(self) -> None:
        problems: list[str] = []
        cleaned = findings.clean_findings(
            [{"claim": "c", "where": "w", "confidence": "pretty sure"}], problems)
        self.assertEqual(cleaned[0]["confidence"], "suspected")
        self.assertEqual(cleaned[0]["why_not_fixed"], "not stated")
        self.assertEqual(len(problems), 2)

    def test_claimless_finding_is_dropped(self) -> None:
        problems: list[str] = []
        self.assertEqual(findings.clean_findings([{"where": "w"}], problems), [])
        self.assertIn("no `claim`", problems[0])

    def test_fingerprint_normalizes_claim_and_location(self) -> None:
        a = findings.fingerprint("p", "runners.py:88", "Retry loop drops the last error!")
        b = findings.fingerprint("p", "Runners.py:88", "retry  loop drops the last error")
        self.assertEqual(a, b)
        self.assertNotEqual(a, findings.fingerprint("other", "runners.py:88",
                                                    "retry loop drops the last error"))


class TripwireTests(unittest.TestCase):
    def test_each_tripwire_fires(self) -> None:
        self.assertIn("touches another project (other)", findings.tripwires(
            {"project": "other"}, project_path="demo", child_count=0, ceiling=5))
        self.assertIn("changes the goal's acceptance criteria", findings.tripwires(
            {"acceptance_criteria": ["x"]}, project_path="demo", child_count=0,
            ceiling=5))
        self.assertTrue(any("ceiling" in r for r in findings.tripwires(
            {}, project_path="demo", child_count=5, ceiling=5)))

    def test_quiet_proposal_fires_nothing(self) -> None:
        self.assertEqual(findings.tripwires({"project": "demo"}, project_path="demo",
                                            child_count=1, ceiling=5), [])

    def test_no_planner_means_unevaluated_never_aligned(self) -> None:
        self.assertIsNone(findings.evaluate_alignment({}, {}, {}))

    def test_planner_hedge_and_failure_are_unevaluated(self) -> None:
        with mock.patch.object(findings, "PLANNER", lambda **kw: {"verdict": "maybe"}):
            self.assertIsNone(findings.evaluate_alignment({}, {}, {}))
        with mock.patch.object(findings, "PLANNER", mock.Mock(side_effect=RuntimeError)):
            self.assertIsNone(findings.evaluate_alignment({}, {}, {}))


class FilingTestCase(SweeperFixture, unittest.TestCase):
    """A run that finished, its Work goal, and the fake server behind them."""

    def setUp(self) -> None:
        super().setUp()
        self.work.add_task("W-0001", "the goal", delegated=True, tags=["goal"])
        con = db.connect()
        con.execute(
            "INSERT INTO runs(id, slug, profile, backend, requested_by, workdir, "
            "project_id, work_item, status, started_at) "
            "VALUES(7, 'calm_otter', 'stub', 'opencode', 'work', ?, ?, 'W-0001', "
            "'done', ?)", (str(self.root), PROJECT_ID, db.now()))
        con.commit()
        con.close()

    def run_at_completion(self, text, status="done", run_id=7, **cfg_settings):
        con = db.connect()
        log_path = self.tmp_path / f"run-{run_id}.jsonl"
        log_path.write_text(json.dumps({"type": "result", "result": text}) + "\n")
        con.execute("UPDATE runs SET status=?, log_path=? WHERE id=?",
                    (status, str(log_path), run_id))
        con.commit()
        run = dict(con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
        cfg = dict(self.cfg)
        if cfg_settings:
            cfg["settings"] = {**cfg.get("settings", {}), **cfg_settings}
        result = findings.at_completion(con, cfg, run)
        con.close()
        return result

    def add_run(self, run_id: int) -> None:
        con = db.connect()
        con.execute(
            "INSERT INTO runs(id, slug, profile, backend, requested_by, workdir, "
            "project_id, work_item, status, started_at) "
            "VALUES(?, ?, 'stub', 'opencode', 'work', ?, ?, 'W-0001', 'done', ?)",
            (run_id, f"run_{run_id}", str(self.root), PROJECT_ID, db.now()))
        con.commit()
        con.close()

    # --- findings -----------------------------------------------------------

    def test_finding_becomes_a_triage_issue_attributed_to_the_run(self) -> None:
        result = self.run_at_completion(HANDOFF % (FINDING, ""))
        self.assertEqual(result["problems"], [])
        self.assertEqual([f["action"] for f in result["findings"]], ["filed"])
        issue = self.work.issues[result["findings"][0]["issue"]]
        self.assertFalse(issue["delegated"])
        self.assertEqual(issue["state"], "queued")
        self.assertIn("calm_otter", issue["body"])
        self.assertIn("observed", issue["body"])
        self.assertIn("runners.py:88", issue["body"])

    def test_lost_issue_response_is_recovered_by_its_run_marker(self) -> None:
        create = WorkClient.create_issue

        with mock.patch.object(
                WorkClient, "create_issue",
                side_effect=WorkError(503, "unavailable", "try again")):
            unavailable = self.run_at_completion(HANDOFF % (FINDING, ""))
        self.assertEqual(unavailable["findings"][0]["action"], "deferred")
        self.assertIsNone(self.db_run(7)["handoff_processed_at"])

        def committed_then_lost(client, *args, **kwargs):
            create(client, *args, **kwargs)
            return None

        with mock.patch.object(WorkClient, "create_issue", committed_then_lost):
            first = self.run_at_completion(HANDOFF % (FINDING, ""))
        self.assertEqual(first["findings"][0]["action"], "deferred")
        self.assertEqual(len(self.work.issues), 1)

        second = self.run_at_completion(HANDOFF % (FINDING, ""))
        self.assertEqual(second["findings"][0]["action"], "filed")
        self.assertEqual(len(self.work.issues), 1)
        self.assertIsNotNone(self.db_run(7)["handoff_processed_at"])

    def test_repeat_comments_and_counts_instead_of_filing_a_duplicate(self) -> None:
        first = self.run_at_completion(HANDOFF % (FINDING, ""))
        issue_id = first["findings"][0]["issue"]
        # The issue has to be claimed for an agent reply; that is Work's rule,
        # not ours, and it is the only way to observe the comment.
        self.client.claim_issue(issue_id)
        before = len(self.work.issues)
        self.add_run(8)
        again = self.run_at_completion(HANDOFF % (FINDING, ""), run_id=8)
        self.assertEqual(len(self.work.issues), before)
        self.assertEqual(again["findings"][0]["action"], "duplicate")
        self.assertEqual(again["findings"][0]["occurrences"], 2)
        self.assertIn("occurrence 2", self.work.issues[issue_id]["messages"][-1]["body"])

    def test_repeat_on_an_unclaimed_issue_still_counts(self) -> None:
        self.run_at_completion(HANDOFF % (FINDING, ""))
        self.add_run(8)
        again = self.run_at_completion(HANDOFF % (FINDING, ""), run_id=8)
        self.assertEqual(again["findings"][0]["occurrences"], 2)
        self.assertEqual(again["findings"][0]["comment_skipped"], "issue_not_claimed")

    def test_missing_field_is_recorded_on_the_run_not_silently_passed(self) -> None:
        self.run_at_completion("done, no block here")
        con = db.connect()
        run = con.execute("SELECT * FROM runs WHERE id=7").fetchone()
        note = con.execute("SELECT * FROM messages WHERE run_id=7 AND kind='protocol'"
                           ).fetchone()
        con.close()
        self.assertIn("protocol failure", run["summary"])
        self.assertIsNotNone(note)

    def test_unfinished_run_is_not_held_to_the_protocol(self) -> None:
        result = self.run_at_completion("killed mid-sentence", status="killed")
        self.assertFalse(result["parsed"])
        self.assertEqual(result["problems"], [])

    # --- proposals ----------------------------------------------------------

    def test_unevaluated_proposal_goes_to_the_human_never_self_approves(self) -> None:
        result = self.run_at_completion(HANDOFF % ("", PROPOSAL))
        self.assertEqual(result["proposals"][0]["action"], "decision")
        self.assertIsNone(result["proposals"][0]["verdict"])
        decision = self.work.decisions[result["proposals"][0]["decision"]]
        self.assertEqual(decision["refs"], ["W-0001"])
        self.assertIn("unevaluated", decision["detail"])
        self.assertIn("decision",
                      [e["type"] for e in self.client.needs_you()])
        self.assertEqual([t for t in self.work.tasks.values()
                          if t["parentId"] == "W-0001"], [])

    def test_lost_decision_response_is_recovered_by_its_run_marker(self) -> None:
        create = WorkClient.create_decision

        with mock.patch.object(
                WorkClient, "create_decision",
                side_effect=WorkError(503, "unavailable", "try again")):
            unavailable = self.run_at_completion(HANDOFF % ("", PROPOSAL))
        self.assertEqual(unavailable["proposals"][0]["action"], "deferred")
        self.assertIsNone(self.db_run(7)["handoff_processed_at"])

        def committed_then_lost(client, *args, **kwargs):
            create(client, *args, **kwargs)
            return None

        with mock.patch.object(WorkClient, "create_decision", committed_then_lost):
            first = self.run_at_completion(HANDOFF % ("", PROPOSAL))
        self.assertEqual(first["proposals"][0]["action"], "deferred")
        self.assertEqual(len(self.work.decisions), 1)

        second = self.run_at_completion(HANDOFF % ("", PROPOSAL))
        self.assertEqual(second["proposals"][0]["action"], "decision")
        self.assertEqual(len(self.work.decisions), 1)

    def test_aligned_proposal_lands_as_a_child_task_plus_one_comment(self) -> None:
        planner = lambda **kw: {"verdict": "aligned", "rationale": "same goal"}
        with mock.patch.object(findings, "PLANNER", planner):
            result = self.run_at_completion(HANDOFF % ("", PROPOSAL))
        self.assertEqual(result["proposals"][0]["action"], "child")
        child = self.work.tasks[result["proposals"][0]["task"]]
        self.assertEqual(child["parentId"], "W-0001")
        self.assertFalse(child["delegated"])
        self.assertEqual(child["projectPath"], "demo")
        comments = [e for e in self.work.tasks["W-0001"]["log"]
                    if "added child" in e["message"]]
        self.assertEqual(len(comments), 1)

    def test_lost_child_response_is_recovered_by_its_run_marker(self) -> None:
        planner = lambda **kw: {"verdict": "aligned", "rationale": "same goal"}
        create = WorkClient.create_task

        def committed_then_lost(client, *args, **kwargs):
            create(client, *args, **kwargs)
            return None

        with mock.patch.object(findings, "PLANNER", planner), \
                mock.patch.object(WorkClient, "create_task", committed_then_lost):
            first = self.run_at_completion(HANDOFF % ("", PROPOSAL))
        self.assertEqual(first["proposals"][0]["action"], "deferred")

        with mock.patch.object(findings, "PLANNER", planner):
            second = self.run_at_completion(HANDOFF % ("", PROPOSAL))
        children = [task for task in self.work.tasks.values()
                    if task["parentId"] == "W-0001"]
        self.assertEqual(second["proposals"][0]["action"], "child")
        self.assertEqual(len(children), 1)

    def test_lost_child_comment_response_does_not_repeat_the_comment(self) -> None:
        planner = lambda **kw: {"verdict": "aligned", "rationale": "same goal"}
        post = WorkClient.log_task

        def committed_then_lost(client, item_id, body):
            reply = post(client, item_id, body)
            return None if " proposal:1 added child " in body else reply

        with mock.patch.object(findings, "PLANNER", planner), \
                mock.patch.object(WorkClient, "log_task", committed_then_lost):
            first = self.run_at_completion(HANDOFF % ("", PROPOSAL))
        self.assertEqual(first["proposals"][0]["action"], "deferred")

        with mock.patch.object(findings, "PLANNER", planner):
            second = self.run_at_completion(HANDOFF % ("", PROPOSAL))
        self.assertEqual(second["proposals"][0]["action"], "child")
        comments = [entry["message"] for entry in self.work.tasks["W-0001"]["log"]]
        self.assertEqual(sum(" proposal:1 added child " in note for note in comments), 1)

    def test_own_project_is_resolved_so_it_is_not_a_cross_project_proposal(self) -> None:
        """The run's project comes from the projectId cache; a cold cache
        refreshes rather than filing everything as cross-project."""
        planner = lambda **kw: {"verdict": "aligned", "rationale": "sure"}
        own = '{"title": "Add a retry test", "project": "demo"}'
        with mock.patch.object(findings, "PLANNER", planner):
            result = self.run_at_completion(HANDOFF % ("", own))
        self.assertEqual(result["proposals"][0]["action"], "child")

    def test_pivot_becomes_a_decision(self) -> None:
        planner = lambda **kw: {"verdict": "pivot", "rationale": "different goal"}
        with mock.patch.object(findings, "PLANNER", planner):
            result = self.run_at_completion(HANDOFF % ("", PROPOSAL))
        self.assertEqual(result["proposals"][0]["action"], "decision")
        self.assertIn("different goal",
                      self.work.decisions[result["proposals"][0]["decision"]]["detail"])

    def test_a_projectless_proposal_decision_files_under_the_runs_project(self) -> None:
        # Run 38's two orphans: the proposal named no project and the decision
        # landed nowhere. It files where the run worked, like the adopted-task
        # path always has.
        planner = lambda **kw: {"verdict": "pivot", "rationale": "different goal"}
        with mock.patch.object(findings, "PLANNER", planner):
            result = self.run_at_completion(HANDOFF % ("", PROPOSAL))
        decision = self.work.decisions[result["proposals"][0]["decision"]]
        self.assertEqual("demo", decision["projectPath"])

    def test_the_decision_says_what_each_choice_does(self) -> None:
        # "Add as a child of W-0001" named a relationship, not an outcome, and
        # the owner could not tell what clicking it would create.
        planner = lambda **kw: {"verdict": "pivot", "rationale": "different goal"}
        with mock.patch.object(findings, "PLANNER", planner):
            result = self.run_at_completion(HANDOFF % ("", PROPOSAL))
        decision = self.work.decisions[result["proposals"][0]["decision"]]
        self.assertEqual(["Create it as a new backlog task under W-0001",
                          "Decline — create nothing"], decision["options"])
        self.assertIn("files a NEW backlog task", decision["detail"])
        self.assertIn("Declining creates nothing", decision["detail"])

    def test_tripwire_overrides_an_aligned_verdict(self) -> None:
        planner = lambda **kw: {"verdict": "aligned", "rationale": "sure"}
        cross = '{"title": "Touch the other repo", "project": "elsewhere"}'
        with mock.patch.object(findings, "PLANNER", planner):
            result = self.run_at_completion(HANDOFF % ("", cross))
        self.assertEqual(result["proposals"][0]["action"], "decision")
        self.assertEqual(result["proposals"][0]["verdict"], "aligned")
        self.assertIn("touches another project (elsewhere)",
                      result["proposals"][0]["reasons"])

    def test_child_ceiling_tripwire_counts_existing_children(self) -> None:
        for n in range(2):
            self.work.add_task(f"W-010{n}", f"child {n}", parent_id="W-0001")
        planner = lambda **kw: {"verdict": "aligned", "rationale": "sure"}
        with mock.patch.object(findings, "PLANNER", planner):
            result = self.run_at_completion(HANDOFF % ("", PROPOSAL),
                                            proposal_child_ceiling=2)
        self.assertTrue(any("ceiling 2" in r for r in result["proposals"][0]["reasons"]))

    def test_goal_parent_gate_rejection_is_recorded_not_retried(self) -> None:
        """Work refuses an agent task whose parent is not a delegated goal."""
        self.work.tasks["W-0001"]["delegated"] = False
        planner = lambda **kw: {"verdict": "aligned", "rationale": "sure"}
        with mock.patch.object(findings, "PLANNER", planner):
            result = self.run_at_completion(HANDOFF % ("", PROPOSAL))
        self.assertEqual(result["proposals"][0]["action"], "rejected")
        self.assertEqual(result["proposals"][0]["error"],
                         "agent_task_parent_not_delegated")
        self.assertEqual([t for t in self.work.tasks.values()
                          if t["parentId"] == "W-0001"], [])

    def test_run_without_a_goal_task_cannot_propose(self) -> None:
        con = db.connect()
        con.execute("UPDATE runs SET work_item='issue_9' WHERE id=7")
        con.commit()
        con.close()
        result = self.run_at_completion(HANDOFF % ("", PROPOSAL))
        self.assertEqual(result["proposals"][0]["action"], "dropped")
        self.assertEqual(self.work.decisions, {})

    def test_work_disabled_still_enforces_the_protocol_locally(self) -> None:
        cfg = dict(self.cfg, work={**self.cfg["work"], "enabled": False})
        con = db.connect()
        log_path = self.tmp_path / "run-7.jsonl"
        log_path.write_text(json.dumps({"type": "result",
                                        "result": "no block"}) + "\n")
        con.execute("UPDATE runs SET log_path=? WHERE id=7", (str(log_path),))
        con.commit()
        run = dict(con.execute("SELECT * FROM runs WHERE id=7").fetchone())
        with mock.patch.object(FakeWork, "now", side_effect=AssertionError):
            result = findings.at_completion(con, cfg, run)
        con.close()
        self.assertIn("no handoff block", result["problems"][0])
        self.assertEqual(result["findings"], [])


if __name__ == "__main__":
    unittest.main()
