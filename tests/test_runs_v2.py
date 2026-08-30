import os
import tempfile
import unittest
from unittest.mock import patch

from orchestra import db, fleet_config, groups, runs
from orchestra.contracts import RunRequest


class RunAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"ORCHESTRA_HOME": self.temp.name})
        self.env.start()
        self.con = db.connect(":memory:")
        self.runtime = fleet_config.create_runtime(
            self.con, "Codex", "codex", slug="codex")
        self.profile = fleet_config.create_profile(
            self.con, "Generalist", "codex", slug="generalist", tier=2)
        self.heavy = fleet_config.create_profile(
            self.con, "Heavy", "codex", slug="heavy", tier=3)
        groups.set_cwd(self.con, "general", self.temp.name)

    def tearDown(self):
        self.con.close()
        self.env.stop()
        self.temp.cleanup()

    def request(self, request_id="one", **changes):
        body = {"request_id": request_id,
                "profile": "generalist", "context": "Research the topic"}
        body.update(changes)
        return RunRequest.from_mapping(body)

    def test_root_admission_is_idempotent_and_numbered_in_general(self):
        first, created = runs.submit(self.con, self.request())
        again, repeated = runs.submit(self.con, self.request())
        self.assertTrue(created)
        self.assertFalse(repeated)
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(db.run_no(first), "General #1")
        self.assertTrue(os.path.isfile(first["brief_path"]))

    def test_children_inherit_and_may_not_delegate_upward(self):
        group = groups.create(self.con, "Research batch", slug="batch")
        parent, _ = runs.submit(self.con, self.request(group="batch"))
        with self.assertRaisesRegex(runs.AdmissionError, "upward"):
            runs.submit(self.con, self.request(
                "child-heavy", group="batch", profile="heavy",
                parent_run_id=parent["id"]))
        child, _ = runs.submit(self.con, self.request(
            "child", group="batch", parent_run_id=parent["id"]))
        self.assertEqual(child["group_id"], group["group_id"])
        self.assertEqual(child["cwd"], parent["cwd"])
        self.assertEqual(db.run_no(child), "Research batch #2")

    def test_retry_is_a_new_group_number_with_frozen_snapshots(self):
        original, _ = runs.submit(self.con, self.request())
        self.con.execute(
            "UPDATE runs SET status='failed',finished_at=? WHERE id=?",
            (db.now(), original["id"]))
        self.con.commit()
        retry, created = runs.clone(
            self.con, original["id"], request_id="retry-one", kind="retry",
            requested_by="scheduler")
        self.assertTrue(created)
        self.assertEqual(retry["group_seq"], 2)
        self.assertEqual(retry["retry_of_run_id"], original["id"])
        self.assertEqual(retry["profile_snapshot"], original["profile_snapshot"])

    def test_retry_can_change_profile_and_executable_context(self):
        original, _ = runs.submit(self.con, self.request(context="old"))
        self.con.execute(
            "UPDATE runs SET status='failed',finished_at=? WHERE id=?",
            (db.now(), original["id"]))
        self.con.commit()
        retry, _ = runs.clone(
            self.con, original["id"], request_id="retry-heavy", kind="retry",
            requested_by="operator", profile="heavy", request="new")
        self.assertEqual(retry["profile_id"], self.heavy["profile_id"])
        self.assertEqual(retry["mission"], "new")
        self.assertEqual(retry["workdir"], os.path.realpath(self.temp.name))

    def test_retry_of_child_keeps_delegation_depth(self):
        root, _ = runs.submit(self.con, self.request("depth-root"))
        child, _ = runs.submit(self.con, self.request(
            "depth-child", parent_run_id=root["id"]))
        self.con.execute(
            "UPDATE runs SET status='failed',finished_at=? WHERE id=?",
            (db.now(), child["id"]))
        self.con.commit()
        retried, _ = runs.clone(
            self.con, child["id"], request_id="depth-retry", kind="retry",
            requested_by="operator")
        grandchild, _ = runs.submit(self.con, self.request(
            "depth-grandchild", parent_run_id=retried["id"]))
        with self.assertRaisesRegex(runs.AdmissionError, "depth"):
            runs.submit(self.con, self.request(
                "depth-too-far", parent_run_id=grandchild["id"]))

    def test_parent_tier_is_the_frozen_admission_tier(self):
        parent, _ = runs.submit(self.con, self.request(
            "heavy-parent", profile="heavy"))
        fleet_config.update_profile(
            self.con, self.heavy["profile_id"], {"tier": 1})
        child, _ = runs.submit(self.con, self.request(
            "frozen-tier-child", profile="generalist",
            parent_run_id=parent["id"]))
        self.assertEqual(child["profile_id"], self.profile["profile_id"])

    def test_child_lineage_profile_override_uses_delegation_parent_ceiling(self):
        cheap = fleet_config.create_profile(
            self.con, "Cheap", "codex", slug="cheap", tier=1)
        parent, _ = runs.submit(self.con, self.request("lineage-parent"))
        child, _ = runs.submit(self.con, self.request(
            "lineage-child", profile="cheap", parent_run_id=parent["id"]))
        self.con.execute(
            "UPDATE runs SET status='failed',finished_at=? WHERE id=?",
            (db.now(), child["id"]))
        self.con.commit()

        within_ceiling, _ = runs.clone(
            self.con, child["id"], request_id="lineage-medium", kind="retry",
            requested_by="operator", profile="generalist")
        self.assertEqual(within_ceiling["profile_id"], self.profile["profile_id"])
        self.con.execute(
            "UPDATE runs SET status='failed',finished_at=? WHERE id=?",
            (db.now(), within_ceiling["id"]))
        self.con.commit()
        fleet_config.update_profile(
            self.con, self.profile["profile_id"], {"tier": 3})

        with self.assertRaisesRegex(runs.AdmissionError, "cannot escalate"):
            runs.clone(
                self.con, within_ceiling["id"], request_id="lineage-heavy",
                kind="retry", requested_by="operator", profile="heavy")


if __name__ == "__main__":
    unittest.main()
