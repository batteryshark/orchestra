"""W-0269: verification earns done, or blocks with reasons."""
import unittest

from orchestra import db
from tests.test_sweeper import SweeperFixture


class VerifyTests(SweeperFixture, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        text = self.global_config.read_text()
        text = text.replace(
            '[profiles.stub]\nbackend = "opencode"\n',
            '[profiles.stub]\nbackend = "opencode"\n\n'
            '[profiles.judge]\nbackend = "opencode"\n')
        self.global_config.write_text(
            text + 'verify = true\nverify_profile = "judge"\n')
        from orchestra import config
        self.cfg = config.load()

    def test_verified_criteria_move_review_to_done_with_evidence(self) -> None:
        (self.root / "seed.txt").write_text("ok\n")
        self.work.add_task("W-0001", "lands clean", delegated=True,
                           acceptance=("the seed is there — read seed.txt",))
        self.sweep()
        worker = self.db_run()
        self.client.check_task_item("W-0001", "acceptance", 0, checked=True)
        self.finish_run(worker["id"], "done", "Shipped.")
        actions = self.sweep()
        task = self.work.tasks["W-0001"]
        self.assertEqual(task["status"], "done")
        verify_actions = [a for a in actions if a.get("action") == "verify"]
        self.assertEqual(len(verify_actions), 1)
        self.assertEqual(verify_actions[0]["to"], "done")
        log = "\n".join(e["message"] for e in task["log"])
        self.assertIn("Verified. W-0001 is done.", log)
        self.assertIn("read seed.txt: present", log)
        con = db.connect()
        v = con.execute("SELECT * FROM runs WHERE requested_by='verify'").fetchone()
        con.close()
        self.assertEqual(v["profile"], "judge")
        self.assertNotEqual(v["profile"], worker["profile"])
        self.assertIsNone(v["session_ref"])
        self.assertEqual(v["parent_run"], worker["id"])
        self.assertIn(f"verify/{v['slug']}", log)

    def test_a_failing_criterion_blocks_naming_it(self) -> None:
        self.work.add_task("W-0001", "worker lied", delegated=True,
                           acceptance=("the seed is there — read missing.txt",))
        self.sweep()
        worker = self.db_run()
        self.client.check_task_item("W-0001", "acceptance", 0, checked=True)
        self.finish_run(worker["id"], "done", "Shipped.")
        self.sweep()
        task = self.work.tasks["W-0001"]
        self.assertEqual(task["status"], "blocked")
        log = "\n".join(e["message"] for e in task["log"])
        self.assertIn("the seed is there — read missing.txt", log)
        self.assertIn("failed verification", log)

    def test_a_bare_verified_fact_earns_nothing(self) -> None:
        """Sign-off means done only on top of a landing, and only inside a
        live claim. Without either, `verified` is noise the human outranks."""
        self.work.add_task("W-0001", "not landed", delegated=True)
        self.client.log_task("W-0001", "[verify/quiet_owl] fact: verified")
        self.assertEqual(self.work.tasks["W-0001"]["status"], "ready")
        self.work.agent_claim("W-0001", run=1)
        self.client.log_task("W-0001", "[verify/quiet_owl] fact: verified")
        self.assertEqual(self.work.tasks["W-0001"]["status"], "in_progress")
        self.client.log_task("W-0001", "[orchestra/x] fact: landed")
        self.client.log_task("W-0001", "[verify/quiet_owl] fact: verified")
        self.assertEqual(self.work.tasks["W-0001"]["status"], "done")
        self.assertEqual(self.work.tasks["W-0001"]["storedStatus"], "ready")

    def test_a_depends_on_chain_completes_once_verification_signs_off(self) -> None:
        (self.root / "seed.txt").write_text("ok\n")
        self.work.add_task("W-0001", "first", delegated=True,
                           acceptance=("seed — read seed.txt",))
        self.work.add_task("W-0002", "second", delegated=True,
                           depends_on=["W-0001"])
        self.sweep()
        first = self.db_run()
        self.assertEqual(first["ref"], "W-0001")
        self.assertEqual(self.work.tasks["W-0002"]["status"], "ready")
        self.client.check_task_item("W-0001", "acceptance", 0, checked=True)
        self.finish_run(first["id"], "done", "done")
        self.sweep()
        self.assertEqual(self.work.tasks["W-0001"]["status"], "done")
        self.assertEqual(self.work.tasks["W-0002"]["status"], "in_progress")
        self.assertEqual(len(self.launched), 2)


if __name__ == "__main__":
    unittest.main()
