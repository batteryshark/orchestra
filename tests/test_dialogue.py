"""W-0307: a needs-judgment criterion gets a capped two-seat dialogue.

The prose-vs-mechanical split left an honest gap: a criterion no command
settles got nobody. With `[verify] second_opinion` set, the pass runs a
bounded exchange between the verify seat and that second voice — message
cap, output budget, per-turn timeout enforced in code (the yeschef rooms
lesson, W-0306) — recorded as `dialogue` control turns. Advisory only:
the verdict may tick a criterion, never fail one, and an unset second
seat means the pass behaves exactly as before.
"""
import json
import unittest
from unittest import mock

from orchestra import db, observer
from tests.test_sweeper import SweeperFixture

VERDICT = json.dumps({"criteria": [{"index": 0, "verdict": "met"}]})


class DialogueTests(SweeperFixture, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        text = self.global_config.read_text()
        text = text.replace(
            '[profiles.stub]\nbackend = "opencode"\n',
            '[profiles.stub]\nbackend = "opencode"\n\n'
            '[profiles.judge]\nbackend = "opencode"\ntier = 1\n\n'
            '[profiles.judge2]\nbackend = "opencode"\n\n'
            '[verify]\nsecond_opinion = "judge2"\n')
        self.global_config.write_text(text)

    def _land_prose(self) -> None:
        self.work.add_task("W-0001", "swept item", delegated=True,
                           acceptance=("the flow feels right — click through",))
        self.sweep()
        worker = self.db_run()
        self.client.check_task_item("W-0001", "acceptance", 0, checked=True)
        self.finish_run(worker["id"], "done", "Shipped.")

    def _sweep_with_replies(self, replies: list[str]) -> None:
        """Run the verify sweep with the dialogue's model calls scripted.

        Patches under model_turn, not over it, so the real recording path
        files each message as a `dialogue` control turn."""
        proc = mock.Mock(returncode=0, stdout="{}", stderr="")
        with mock.patch.object(observer.subprocess, "run",
                               return_value=proc), \
             mock.patch.object(observer.runners, "parse_log",
                               side_effect=[(None, r) for r in replies]):
            self.sweep()

    def _dialogue_rows(self):
        con = db.connect()
        rows = con.execute(
            "SELECT * FROM runs WHERE layer='dialogue' ORDER BY id").fetchall()
        con.close()
        return rows

    def test_a_prose_criterion_gets_a_recorded_dialogue_and_a_verdict(self):
        """The strongest borrow, end to end: two messages, a met verdict,
        the criterion ticked, and the pass completes as verified — prose
        no longer strands the item in review."""
        self._land_prose()
        self._sweep_with_replies(["I doubt the flow; show me.", VERDICT])
        task = self.work.tasks["W-0001"]
        self.assertEqual(task["status"], "done")
        messages = [e["message"] for e in task["log"]]
        self.assertTrue(any("judged met by the second-opinion dialogue" in m
                            for m in messages))
        self.assertTrue(any("fact: verified" in m for m in messages))
        rows = self._dialogue_rows()
        self.assertEqual(len(rows), 2, "one control turn per message")
        self.assertEqual({r["profile"] for r in rows}, {"judge2", "judge"},
                         "the second voice opens, the verify seat answers")

    def test_the_message_cap_ends_the_dialogue_with_its_reason(self):
        """No verdict in four messages: the cap ends it, the reason is on
        the record, nothing is ticked, and the item stays the human's."""
        self._land_prose()
        self._sweep_with_replies(["hm"] * 4)
        task = self.work.tasks["W-0001"]
        self.assertNotEqual(task["status"], "blocked")
        self.assertNotEqual(task["status"], "done")
        messages = [e["message"] for e in task["log"]]
        self.assertTrue(any("message cap (4) reached" in m for m in messages))
        self.assertFalse(any("fact: verified" in m for m in messages))
        self.assertEqual(len(self._dialogue_rows()), 4)

    def test_no_second_seat_means_no_dialogue(self):
        """Requirement 6: unset seat, the feature is off — the pass is the
        pre-W-0307 inconclusive, and no dialogue turn exists."""
        text = self.global_config.read_text()
        self.global_config.write_text(
            text.replace('[verify]\nsecond_opinion = "judge2"\n', ""))
        self._land_prose()
        self.sweep()
        task = self.work.tasks["W-0001"]
        self.assertNotEqual(task["status"], "done")
        self.assertEqual(self._dialogue_rows(), [])


if __name__ == "__main__":
    unittest.main()
