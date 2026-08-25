"""W-0299: a verifier run follows every landing, on a cheap profile.

The sweeper posts the landed fact, the item derives review, and the SAME
pass records the verification run — no human asks, no config beyond one
tier-1 (workhorse) profile. A verifier that declines sign-off leaves
records: findings on the item's log and a halted fact with reasons, never
chat. The sign-off gates themselves (review, checklist accounted,
dependencies settled) are Work's and stay exactly as they are.
"""
import unittest

from orchestra import config, db
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
