import tempfile
import unittest

from orchestra import child_runs, db, fleet_config, groups, runs
from orchestra.contracts import RunRequest


class ChildRunsV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.con = db.connect(":memory:")
        fleet_config.create_runtime(
            self.con, "Exec", "exec", slug="exec", command=["agent"])
        fleet_config.create_profile(
            self.con, "General", "exec", slug="general", tier=2)
        fleet_config.create_profile(
            self.con, "Light", "exec", slug="light", tier=1)
        groups.set_cwd(self.con, "general", self.temp.name)
        self.parent, _ = runs.submit(self.con, RunRequest.from_mapping({
            "request_id": "parent", "profile": "general",
            "context": "Lead the work"}))

    def tearDown(self):
        self.con.close()
        self.temp.cleanup()

    def test_request_is_idempotent_and_daemon_admits_explicit_profiles(self):
        request, created = child_runs.enqueue(
            self.con, self.parent["id"], ["general", "light"],
            "Investigate", request_id="delegate-one")
        again, repeated = child_runs.enqueue(
            self.con, self.parent["id"], ["general", "light"],
            "Investigate", request_id="delegate-one")
        self.assertTrue(created)
        self.assertFalse(repeated)
        self.assertEqual(request["id"], again["id"])
        result = child_runs.process_pending(self.con)
        self.assertEqual(len(result[0]["child_run_ids"]), 2)
        children = self.con.execute(
            "SELECT * FROM runs WHERE parent_run_id=? ORDER BY id",
            (self.parent["id"],)).fetchall()
        self.assertEqual([row["profile_id"] for row in children], [
            fleet_config.find_profile(self.con, "general")["profile_id"],
            fleet_config.find_profile(self.con, "light")["profile_id"],
        ])
        self.assertTrue(all(row["group_id"] == self.parent["group_id"]
                            and row["cwd"] == self.parent["cwd"]
                            for row in children))

    def test_reservations_enforce_parent_total_before_processing(self):
        fleet_config.set_fleet_setting(
            self.con, "delegation_max_children", 2)
        child_runs.enqueue(
            self.con, self.parent["id"], ["light", "light"], "First batch")
        with self.assertRaisesRegex(child_runs.DelegationError, "limit"):
            child_runs.enqueue(
                self.con, self.parent["id"], ["light"], "Too many")

    def test_active_limit_rejects_the_whole_batch_before_any_child_exists(self):
        fleet_config.set_fleet_setting(
            self.con, "delegation_max_children", 5)
        fleet_config.set_fleet_setting(
            self.con, "delegation_max_active_children", 1)
        with self.assertRaisesRegex(child_runs.DelegationError, "active-child"):
            child_runs.enqueue(
                self.con, self.parent["id"], ["light", "light"], "Too wide")
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM child_requests").fetchone()[0], 0)

    def test_processing_recovers_a_child_created_before_batch_bookkeeping(self):
        request, _ = child_runs.enqueue(
            self.con, self.parent["id"], ["light", "light"], "Recover")
        self.con.execute(
            "UPDATE child_requests SET status='processing',processed_at=? WHERE id=?",
            (db.now(), request["id"]))
        self.con.commit()
        first, _ = runs.submit(self.con, RunRequest.from_mapping({
            "request_id": f"child-request:{request['id']}:1",
            "group": "general", "profile": "light",
            "context": "Recover", "parent_run_id": self.parent["id"],
        }))

        result = child_runs.process_pending(self.con)

        self.assertEqual(result[0]["child_run_ids"][0], first["id"])
        self.assertEqual(len(result[0]["child_run_ids"]), 2)
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM runs WHERE parent_run_id=?",
            (self.parent["id"],)).fetchone()[0], 2)

    def test_request_id_cannot_alias_a_different_parent_or_payload(self):
        child_runs.enqueue(
            self.con, self.parent["id"], ["light"], "Original",
            request_id="same")
        with self.assertRaisesRegex(child_runs.DelegationError, "different"):
            child_runs.enqueue(
                self.con, self.parent["id"], ["light"], "Changed",
                request_id="same")

    def test_batch_settles_only_after_every_child_is_terminal(self):
        request, _ = child_runs.enqueue(
            self.con, self.parent["id"], ["light"], "Help")
        child_id = child_runs.process_pending(self.con)[0]["child_run_ids"][0]
        self.assertEqual(child_runs.settle_requests(self.con), [])
        self.con.execute(
            "UPDATE runs SET status='completed',finished_at=? WHERE id=?",
            (db.now(), child_id))
        self.con.commit()
        self.assertEqual(child_runs.settle_requests(self.con), [request["id"]])

    def test_crash_partial_batch_cannot_settle_before_all_targets_exist(self):
        request, _ = child_runs.enqueue(
            self.con, self.parent["id"], ["light", "light"], "Help")
        child, _ = runs.submit(self.con, RunRequest.from_mapping({
            "request_id": f"child-request:{request['id']}:1",
            "group": "general", "profile": "light",
            "context": "Help", "parent_run_id": self.parent["id"],
        }))
        self.con.execute(
            "UPDATE child_requests SET status='processing',processed_at=?,"
            "child_run_ids_json=? WHERE id=?",
            (db.now(), f"[{child['id']}]", request["id"]))
        self.con.execute("UPDATE runs SET status='completed' WHERE id=?",
                         (child["id"],))
        self.con.commit()
        self.assertEqual(child_runs.settle_requests(self.con), [])
        self.assertTrue(child_runs.unsettled_requests(
            self.con, self.parent["id"]))


if __name__ == "__main__":
    unittest.main()
