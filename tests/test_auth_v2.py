import os
import tempfile
import unittest
from unittest.mock import patch

from orchestra import auth, db, fleet_config, runs
from orchestra.contracts import RunRequest


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"ORCHESTRA_HOME": self.temp.name})
        self.env.start()
        self.con = db.connect(":memory:")

    def tearDown(self):
        self.con.close()
        self.env.stop()
        self.temp.cleanup()

    def test_pairing_mints_a_revocable_device_token_once(self):
        first, token = auth.bootstrap_device(self.con, "Mac")
        identity = auth.identify(self.con, token)
        self.assertEqual(identity.kind, "device")
        pair = auth.create_pairing(
            self.con, created_by_device_id=first["device_id"])
        self.assertRegex(pair["code"], r"^[0-9A-HJKMNP-TV-Z]{4}(?:-[0-9A-HJKMNP-TV-Z]{4}){2}$")
        phone, phone_token = auth.redeem_pairing(
            self.con, pair["pairing_id"],
            pair["code"].replace("-", " ").lower(), "Phone")
        with self.assertRaisesRegex(auth.AuthError, "already used"):
            auth.redeem_pairing(
                self.con, pair["pairing_id"], pair["code"], "Again")
        self.assertTrue(auth.revoke_device(self.con, phone["device_id"]))
        self.assertIsNone(auth.identify(self.con, phone_token))

    def test_service_authority_is_fixed_and_narrow(self):
        _, token = auth.create_service_token(self.con, "Bridge", ["read", "dispatch"])
        identity = auth.identify(self.con, token)
        auth.authorize(identity, "dispatch")
        with self.assertRaisesRegex(auth.AuthError, "no control"):
            auth.authorize(identity, "control")
        with self.assertRaisesRegex(auth.AuthError, "subset"):
            auth.create_service_token(self.con, "Admin", ["device_admin"])

    def test_run_token_is_self_scoped_and_terminally_revoked(self):
        fleet_config.create_runtime(self.con, "E", "exec", slug="e", command=["x"])
        fleet_config.create_profile(self.con, "P", "e", slug="p", tier=1)
        run, _ = runs.submit(self.con, RunRequest.from_mapping({
            "request_id": "one", "profile": "p", "context": "m",
            "cwd": self.temp.name}))
        token = auth.mint_run(self.con, run["id"])
        identity = auth.identify(self.con, token)
        auth.authorize(identity, "artifact", target_run_id=run["id"])
        with self.assertRaisesRegex(auth.AuthError, "only on itself"):
            auth.authorize(identity, "artifact", target_run_id=run["id"] + 1)
        self.con.execute("UPDATE runs SET status='completed' WHERE id=?", (run["id"],))
        self.con.commit()
        self.assertIsNone(auth.identify(self.con, token))


if __name__ == "__main__":
    unittest.main()
