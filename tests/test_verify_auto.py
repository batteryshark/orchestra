"""W-0299: a verifier run follows every landing, on a cheap profile.

The sweeper posts the landed fact, the item derives review, and the SAME
pass records the verification run — no human asks, no config beyond one
tier-1 (workhorse) profile. A verifier that declines sign-off leaves
records: findings on the item's log and a halted fact with reasons, never
chat. The sign-off gates themselves (review, checklist accounted,
dependencies settled) are Work's and stay exactly as they are.
"""
import unittest
from unittest import mock

from orchestra import config, db, verify
from tests.test_sweeper import SweeperFixture


class AutoVerifyTests(SweeperFixture, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        # One tier-1 profile and NOTHING else: no [verify] table, no legacy
        # [work] verify keys. The default carries the whole feature.
        text = self.global_config.read_text()
        text = text.replace(
            '[profiles.stub]\nbackend = "opencode"\n',
            '[profiles.stub]\nbackend = "opencode"\n\n'
            '[profiles.judge]\nbackend = "opencode"\ntier = 1\n')
        self.global_config.write_text(text)
        self.cfg = config.load()

    def _land(self, criterion: str) -> dict:
        """Dispatch W-0001, tick its criterion, finish the worker landed."""
        self.work.add_task("W-0001", "swept item", delegated=True,
                           acceptance=(criterion,))
        self.sweep()
        worker = self.db_run()
        self.client.check_task_item("W-0001", "acceptance", 0, checked=True)
        self.finish_run(worker["id"], "done", "Shipped.")
        return worker

    def _verify_run(self):
        con = db.connect()
        row = con.execute(
            "SELECT * FROM runs WHERE requested_by='verify'").fetchone()
        con.close()
        return row

    def test_a_landed_item_gets_a_verify_run_within_one_sweep(self) -> None:
        """The contract: landed fact and verify run in ONE pass, and the
        verifier's profile is the cheap default, never the worker's."""
        (self.root / "seed.txt").write_text("ok\n")
        worker = self._land("the seed is there — read seed.txt")
        actions = self.sweep()
        kinds = [a.get("action") for a in actions if isinstance(a, dict)]
        self.assertIn("report", kinds)
        self.assertIn("verify", kinds)
        run = self._verify_run()
        self.assertEqual(run["profile"], "judge")
        self.assertNotEqual(run["profile"], worker["profile"])
        self.assertEqual(run["parent_run"], worker["id"])
        self.assertEqual(self.work.tasks["W-0001"]["status"], "done")

    def test_a_declined_sign_off_leaves_findings_as_records(self) -> None:
        """Findings land on the item's log — per-criterion notes and a
        halted fact naming the reasons — not in anyone's chat."""
        self._land("the seed is there — read missing.txt")
        self.sweep()
        task = self.work.tasks["W-0001"]
        self.assertEqual(task["status"], "blocked")
        messages = [e["message"] for e in task["log"]]
        self.assertTrue(any("read missing.txt: not a file" in m
                            for m in messages))
        halted = [m for m in messages
                  if m.startswith("[verify/") and "fact: halted" in m]
        self.assertEqual(len(halted), 1)
        self.assertIn("failed verification", halted[0])

    def test_a_prose_method_is_judged_by_nobody_and_vetoes_nothing(self) -> None:
        """2026-08-25: "harness fixture test" and "click through" were exec'd
        as commands, failed with ENOENT, and blocked two green items. Prose
        is not runnable: the pass reports it needs judgment, ticks nothing,
        posts NO fact, and the item stays in review for the human."""
        self._land("the flow feels right — click through")
        actions = self.sweep()
        verify = [a for a in actions if isinstance(a, dict)
                  and a.get("action") == "verify"]
        self.assertEqual([a.get("to") for a in verify], [None])
        run = self._verify_run()
        self.assertEqual(run["status"], "done")
        self.assertIn("Inconclusive", run["summary"])
        task = self.work.tasks["W-0001"]
        self.assertNotEqual(task["status"], "blocked")
        messages = [e["message"] for e in task["log"]]
        self.assertTrue(any("needs judgment" in m for m in messages))
        self.assertFalse(any("fact: halted" in m for m in messages))
        self.assertFalse(any("fact: verified" in m for m in messages))

    def test_a_real_failure_still_blocks_even_beside_prose(self) -> None:
        """Prose softens nothing about a mechanical failure: one failed
        method halts the item exactly as before."""
        self.work.add_task("W-0001", "swept item", delegated=True,
                           acceptance=("the seed is there — read missing.txt",
                                       "the flow feels right — click through"))
        self.sweep()
        worker = self.db_run()
        self.client.check_task_item("W-0001", "acceptance", 0, checked=True)
        self.client.check_task_item("W-0001", "acceptance", 1, checked=True)
        self.finish_run(worker["id"], "done", "Shipped.")
        self.sweep()
        self.assertEqual(self.work.tasks["W-0001"]["status"], "blocked")
        halted = [e["message"] for e in self.work.tasks["W-0001"]["log"]
                  if "fact: halted" in e["message"]]
        self.assertEqual(len(halted), 1)
        self.assertIn("read missing.txt", halted[0])
        self.assertNotIn("click through", halted[0])

    def test_an_out_of_repo_read_is_declined_not_failed(self) -> None:
        """W-0310: a read whose path escapes the checkout is declined with
        a reason — no halted fact, the item stays in review."""
        self._land("the spec is there — read ../elsewhere/spec.md")
        self.sweep()
        task = self.work.tasks["W-0001"]
        self.assertNotEqual(task["status"], "blocked")
        criterion = task["acceptanceCriteria"][0]
        self.assertTrue(criterion["declined"])
        self.assertIn("outside this checkout", criterion["reason"])
        messages = [e["message"] for e in task["log"]]
        self.assertFalse(any("fact: halted" in m for m in messages))
        self.assertFalse(any("fact: verified" in m for m in messages))

    def test_an_absent_test_target_runs_no_test_process(self) -> None:
        """W-0310, the run 54 class: the named test file lives in another
        repository — the criterion is declined, and no test process starts."""
        self._land("the guard holds — test tests/test_refine.py")
        with mock.patch.object(verify.subprocess, "run") as proc:
            self.sweep()
        proc.assert_not_called()
        task = self.work.tasks["W-0001"]
        self.assertNotEqual(task["status"], "blocked")
        criterion = task["acceptanceCriteria"][0]
        self.assertTrue(criterion["declined"])
        self.assertIn("not in this checkout", criterion["reason"])
        run = self._verify_run()
        self.assertIn("Inconclusive", run["summary"])

    def test_a_mixed_pass_names_only_the_in_repo_failure(self) -> None:
        """W-0310: a real in-repo failure still blocks, and the halted fact
        blames only it — the out-of-reach criterion sits declined beside it."""
        self.work.add_task("W-0001", "swept item", delegated=True,
                           acceptance=("the seed is there — read missing.txt",
                                       "the guard holds — test tests/test_refine.py"))
        self.sweep()
        worker = self.db_run()
        self.client.check_task_item("W-0001", "acceptance", 0, checked=True)
        self.client.check_task_item("W-0001", "acceptance", 1, checked=True)
        self.finish_run(worker["id"], "done", "Shipped.")
        self.sweep()
        task = self.work.tasks["W-0001"]
        self.assertEqual(task["status"], "blocked")
        halted = [e["message"] for e in task["log"]
                  if "fact: halted" in e["message"]]
        self.assertEqual(len(halted), 1)
        self.assertIn("read missing.txt", halted[0])
        self.assertNotIn("test_refine", halted[0])
        self.assertTrue(task["acceptanceCriteria"][1]["declined"])

    def test_a_bare_test_that_collects_nothing_is_judgment_not_failure(self) -> None:
        """W-0311, the last run 54 shape: bare ``test`` in a checkout with
        no Python tests exits 5 (NO TESTS RAN). An empty discovery proves
        nothing — needs judgment, no veto."""
        self._land("the guard holds — test")
        self.sweep()
        task = self.work.tasks["W-0001"]
        self.assertNotEqual(task["status"], "blocked")
        messages = [e["message"] for e in task["log"]]
        self.assertTrue(any("no tests ran in this checkout" in m
                            for m in messages))
        self.assertFalse(any("fact: halted" in m for m in messages))
        run = self._verify_run()
        self.assertIn("Inconclusive", run["summary"])

    def test_a_worker_decline_is_respected_not_rerun(self) -> None:
        """Run 54 re-ran a criterion the worker had declined and failed it
        in the wrong checkout. A decline on the record stays the answer:
        nothing runs, nothing blocks, no fact certifies what nobody saw."""
        (self.root / "seed.txt").write_text("ok\n")
        self.work.add_task("W-0001", "swept item", delegated=True,
                           acceptance=("the seed is there — read seed.txt",
                                       "the other repo holds — test"))
        self.sweep()
        worker = self.db_run()
        self.client.check_task_item("W-0001", "acceptance", 0, checked=True)
        self.client.check_task_item("W-0001", "acceptance", 1,
                                    reason="not attempted, other repository")
        self.finish_run(worker["id"], "done", "Shipped.")
        with mock.patch.object(verify.subprocess, "run") as proc:
            self.sweep()
        proc.assert_not_called()
        task = self.work.tasks["W-0001"]
        self.assertNotEqual(task["status"], "blocked")
        criterion = task["acceptanceCriteria"][1]
        self.assertTrue(criterion["declined"])
        self.assertEqual(criterion["reason"], "not attempted, other repository")
        messages = [e["message"] for e in task["log"]]
        self.assertFalse(any("fact: verified" in m for m in messages))
        self.assertTrue(any("left as recorded" in m for m in messages))

    def test_verify_enabled_false_turns_the_pass_off(self) -> None:
        self.global_config.write_text(
            self.global_config.read_text() + '\n[verify]\nenabled = false\n')
        self.cfg = config.load()
        (self.root / "seed.txt").write_text("ok\n")
        self._land("the seed is there — read seed.txt")
        self.sweep()
        self.assertIsNone(self._verify_run())
        self.assertEqual(self.work.tasks["W-0001"]["status"], "review")

    def test_legacy_work_verify_false_still_means_off(self) -> None:
        """A pre-W-0299 config keeps meaning what it said."""
        # The file ends inside [work], so the bare key lands there.
        self.global_config.write_text(
            self.global_config.read_text() + 'verify = false\n')
        self.cfg = config.load()
        (self.root / "seed.txt").write_text("ok\n")
        self._land("the seed is there — read seed.txt")
        self.sweep()
        self.assertIsNone(self._verify_run())
        self.assertEqual(self.work.tasks["W-0001"]["status"], "review")

    def test_no_tier1_profile_skips_and_says_so(self) -> None:
        """No cheap model to volunteer: the pass skips, the item stays in
        review for a human — it never crashes and never guesses."""
        self.global_config.write_text(
            self.global_config.read_text().replace("tier = 1\n", ""))
        self.cfg = config.load()
        (self.root / "seed.txt").write_text("ok\n")
        self._land("the seed is there — read seed.txt")
        self.sweep()
        self.assertIsNone(self._verify_run())
        self.assertEqual(self.work.tasks["W-0001"]["status"], "review")

    def test_two_tier1_profiles_are_reported_never_guessed(self) -> None:
        self.global_config.write_text(
            self.global_config.read_text().replace(
                '[profiles.judge]\nbackend = "opencode"\ntier = 1\n',
                '[profiles.judge]\nbackend = "opencode"\ntier = 1\n\n'
                '[profiles.judge2]\nbackend = "opencode"\ntier = 1\n'))
        self.cfg = config.load()
        (self.root / "seed.txt").write_text("ok\n")
        self._land("the seed is there — read seed.txt")
        self.sweep()
        self.assertIsNone(self._verify_run())
        self.assertEqual(self.work.tasks["W-0001"]["status"], "review")


if __name__ == "__main__":
    unittest.main()
