import os
import tempfile
import unittest
from unittest.mock import patch

from orchestra import api, auth, db, fleet_config


class APITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"ORCHESTRA_HOME": self.temp.name})
        self.env.start()
        self.con = db.connect(":memory:")
        _, token = auth.bootstrap_device(self.con, "Test")
        self.identity = auth.identify(self.con, token)
        fleet_config.create_runtime(
            self.con, "Exec", "exec", slug="exec", command=["agent"])
        fleet_config.create_profile(
            self.con, "P", "exec", slug="p", tier=2)
        self.api = api.API(self.con)

    def tearDown(self):
        self.con.close()
        self.env.stop()
        self.temp.cleanup()

    def test_submit_replay_returns_the_original_v2_projection(self):
        body = {"request_id": "run-one", "profile": "p",
                "context": "Do a neutral task"}
        first = self.api.handle("POST", "/api/v2/runs", {}, body, self.identity)
        second = self.api.handle("POST", "/api/v2/runs", {}, body, self.identity)
        self.assertEqual(first.status, 201)
        self.assertEqual(second.status, 200)
        self.assertEqual(first.data["data"]["run"]["id"],
                         second.data["data"]["run"]["id"])
        self.assertEqual(first.data["data"]["run"]["display"], "General #1")

    def test_request_id_reuse_for_different_body_conflicts(self):
        body = {"request_id": "run-one", "profile": "p",
                "context": "One"}
        self.api.handle("POST", "/api/v2/runs", {}, body, self.identity)
        with self.assertRaisesRegex(api.Problem, "different mutation") as caught:
            self.api.handle("POST", "/api/v2/runs", {},
                            {**body, "context": "Two"}, self.identity)
        self.assertEqual(caught.exception.status, 409)

    def test_snapshot_is_compact_and_instance_bound(self):
        response = self.api.handle(
            "GET", "/api/v2/snapshot", {}, None, self.identity)
        self.assertEqual(response.data["api_version"], 2)
        self.assertEqual(response.data["instance_id"], db.instance_id(self.con))
        self.assertNotIn("runs", response.data["data"])

    def test_service_token_cannot_administer_managed_resources(self):
        _, raw = auth.create_service_token(self.con, "Reader", ["read"])
        service = auth.identify(self.con, raw)
        self.api.handle("GET", "/api/v2/profiles", {}, None, service)
        with self.assertRaises(api.Problem) as caught:
            self.api.handle("POST", "/api/v2/groups", {},
                            {"request_id": "g1", "name": "G"}, service)
        self.assertEqual(caught.exception.status, 403)

    def test_dispatch_token_may_create_but_not_reshape_groups(self):
        # a dispatching integration files runs under a group it names;
        # patching or archiving a group stays operator work
        _, raw = auth.create_service_token(self.con, "Bridge", ["dispatch"])
        service = auth.identify(self.con, raw)
        response = self.api.handle("POST", "/api/v2/groups", {},
                                   {"request_id": "g2", "name": "Exo"},
                                   service)
        group = response.data["data"]["group"]
        self.assertEqual(group["name"], "Exo")
        with self.assertRaises(api.Problem) as caught:
            self.api.handle("PATCH", f"/api/v2/groups/{group['id']}", {},
                            {"request_id": "g3", "name": "Renamed"}, service)
        self.assertEqual(caught.exception.status, 403)


if __name__ == "__main__":
    unittest.main()
