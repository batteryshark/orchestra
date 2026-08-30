import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from orchestra import auth, db, http


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"ORCHESTRA_HOME": self.temp.name})
        self.env.start()
        con = db.connect()
        _, self.token = auth.bootstrap_device(con, "Test")
        con.close()
        self.stop = threading.Event()
        self.server = http.make_server(addr="127.0.0.1", port=0, stop=self.stop)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.url = f"http://{host}:{port}"

    def tearDown(self):
        self.stop.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.env.stop()
        self.temp.cleanup()

    def get(self, path, token=None):
        request = urllib.request.Request(self.url + path)
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read())
            finally:
                exc.close()

    def post(self, path, body, token=None):
        request = urllib.request.Request(
            self.url + path, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read()), response.headers

    def test_health_is_minimal_and_v2_requires_auth(self):
        status, health = self.get("/health")
        self.assertEqual((status, health), (200, {"status": "ok"}))
        status, denied = self.get("/api/v2/snapshot")
        self.assertEqual(status, 401)
        self.assertEqual(denied["error"]["code"], "unauthorized")
        status, snapshot = self.get("/api/v2/snapshot", self.token)
        self.assertEqual(status, 200)
        self.assertEqual(snapshot["api_version"], 2)

    def test_v1_routes_do_not_exist(self):
        status, payload = self.get("/api/snapshot", self.token)
        self.assertEqual(status, 404)
        self.assertIn("v2", payload["error"]["message"])

    def test_openapi_is_raw_and_browser_pairing_sets_secure_state(self):
        status, document = self.get("/api/v2/openapi.json", self.token)
        self.assertEqual(status, 200)
        self.assertEqual(document["openapi"], "3.1.0")
        self.assertNotIn("api_version", document)

        _, pairing, _ = self.post(
            "/api/v2/devices/pairing",
            {"request_id": "pair-browser", "label": "Browser"}, self.token)
        status, redemption, headers = self.post(
            "/api/v2/pairing/redeem", {
                "request_id": "redeem-browser", "code": pairing["data"]["code"],
                "label": "Browser", "browser": True,
            })
        self.assertEqual(status, 201)
        self.assertEqual(redemption["data"]["device"]["label"], "Browser")
        self.assertNotIn("token", redemption["data"])
        cookie = headers.get("Set-Cookie", "")
        self.assertIn("orchestra_device=", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

    def test_global_stream_uses_the_documented_named_event(self):
        con = db.connect()
        db.bump_board_revision(con)
        con.commit()
        con.close()
        request = urllib.request.Request(self.url + "/api/v2/stream")
        request.add_header("Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(request, timeout=3) as response:
            lines = [response.readline().decode().strip() for _ in range(6)]
        self.assertIn("event: fleet.changed", lines)


if __name__ == "__main__":
    unittest.main()
