"""Detail-serialization tests for the UI's /api/transcript route.

Coverage targets:
  * The route surfaces configured backend + model WITHOUT leaking any
    credential-like field (the run row never carried credentials; this test
    nails that contract down so a future refactor doesn't accidentally add
    one).
  * Memo / slug appears in the response so the dashboard's brand-new
    names badge has something to render.
  * Numeric run id stays authoritative (the dispatcher's job).
"""
from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from orchestra_cli import db, ui


class DetailSerializationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        (cls.root / ".orchestra").mkdir(parents=True, exist_ok=True)
        con = db.connect(cls.root)
        con.execute(
            "INSERT INTO runs(agent, backend, model, title, work_item, "
            "team, requested_by, workdir, slug, status, started_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("minimax", "opencode", "minimax-coding-plan/MiniMax-M3",
             "naming work", "W-0007", None, "codex",
             str(cls.root), "silly_panda", "running",
             "2026-07-18T22:00:00Z"),
        )
        con.execute(
            "INSERT INTO runs(agent, backend, model, title, work_item, "
            "team, requested_by, workdir, slug, status, started_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("codex", "codex", "gpt-5.6-sol (xhigh)",
             "follow-up", None, None, "codex",
             str(cls.root), "feral_otter", "done",
             "2026-07-18T22:00:00Z"),
        )
        con.commit()
        con.close()

        cls.handler = ui.make_handler(cls.root)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), cls.handler)
        cls.port = cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.tmp.cleanup()

    def get(self, path: str) -> dict:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=4)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            return json.loads(resp.read().decode("utf-8", errors="replace"))
        finally:
            conn.close()

    def test_transcript_payload_includes_backend_and_model(self) -> None:
        payload = self.get("/api/transcript/1")
        self.assertIn("run", payload)
        r = payload["run"]
        self.assertEqual(r["agent"], "minimax")
        self.assertEqual(r["backend"], "opencode")
        self.assertEqual(r["model"], "minimax-coding-plan/MiniMax-M3")
        self.assertEqual(r["slug"], "silly_panda")
        self.assertEqual(r["id"], 1)  # numeric id remains authoritative
        # No credential-shaped fields leak. The exact whitelist is the
        # current schema: id/agent/backend/model/title/work_item/team/
        # requested_by/brief_path/log_path/workdir/branch/parent_run/pid/
        # session_ref/status/exit_code/summary/started_at/finished_at/slug.
        forbidden = [k for k in r.keys()
                     if any(token in k.lower()
                            for token in ("key", "token", "secret", "password", "cred"))]
        self.assertEqual(forbidden, [],
                         f"credential-shaped field leaked: {forbidden}")

    def test_transcript_payload_handles_missing_session_ref(self) -> None:
        # Run 2 has no session_ref; the route must still render cleanly.
        payload = self.get("/api/transcript/2")
        r = payload["run"]
        self.assertEqual(r["backend"], "codex")
        # model carries the ("xhigh")-suffix the dispatcher applies.
        self.assertIn("xhigh", r["model"])
        self.assertEqual(r["slug"], "feral_otter")

    def test_state_payload_includes_slug_for_dashboard_render(self) -> None:
        # The sidebar shows slugs; check both rows are exposed.
        state = self.get("/api/state")
        slugs = sorted(r["slug"] for r in state["runs"] if r.get("slug"))
        self.assertEqual(slugs, ["feral_otter", "silly_panda"])


class StopRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".orchestra").mkdir(parents=True, exist_ok=True)
        con = db.connect(self.root)
        con.execute(
            "INSERT INTO runs(agent, backend, model, title, work_item, "
            "team, requested_by, workdir, slug, status, started_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("minimax", "opencode", "minimax-coding-plan/MiniMax-M3",
             "active work", "W-0012", None, "codex",
             str(self.root), "brisk_stop", "running",
             "2026-07-18T22:00:00Z"),
        )
        con.execute(
            "INSERT INTO runs(agent, backend, model, title, work_item, "
            "team, requested_by, workdir, slug, status, started_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("codex", "codex", "gpt-5.6-sol (xhigh)",
             "finished work", None, None, "codex",
             str(self.root), "done_stop", "done",
             "2026-07-18T22:00:00Z"),
        )
        con.commit()
        con.close()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ui.make_handler(self.root))
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def post(self, path: str, *, body: str = "{}", content_type: str | None = "application/json"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=4)
        headers = {}
        if content_type is not None:
            headers["Content-Type"] = content_type
        try:
            conn.request("POST", path, body=body, headers=headers)
            resp = conn.getresponse()
            payload = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(payload)
        finally:
            conn.close()

    def test_stop_route_marks_active_run_killed(self) -> None:
        status, payload = self.post("/api/runs/1/stop")
        self.assertEqual(status, 200)
        self.assertTrue(payload["stopped"])
        self.assertEqual(payload["status"], "killed")
        self.assertEqual(payload["label"], "stopped by user")
        self.assertEqual(payload["reason"], "no_pid")

        con = db.connect(self.root)
        try:
            row = con.execute("SELECT status, finished_at FROM runs WHERE id=1").fetchone()
        finally:
            con.close()
        self.assertEqual(row["status"], "killed")
        self.assertIsNotNone(row["finished_at"])

    def test_stop_route_is_idempotent_for_terminal_runs(self) -> None:
        status, payload = self.post("/api/runs/2/stop")
        self.assertEqual(status, 200)
        self.assertFalse(payload["stopped"])
        self.assertEqual(payload["status"], "done")
        self.assertEqual(payload["reason"], "already_terminal")

    def test_stop_route_rejects_simple_form_post(self) -> None:
        status, payload = self.post(
            "/api/runs/1/stop",
            body="stop=1",
            content_type="application/x-www-form-urlencoded",
        )
        self.assertEqual(status, 415)
        self.assertIn("Content-Type", payload["error"])

        con = db.connect(self.root)
        try:
            row = con.execute("SELECT status FROM runs WHERE id=1").fetchone()
        finally:
            con.close()
        self.assertEqual(row["status"], "running")

    def test_stop_route_rejects_negative_content_length(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=4)
        try:
            conn.putrequest("POST", "/api/runs/1/stop")
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", "-1")
            conn.endheaders()
            resp = conn.getresponse()
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        finally:
            conn.close()

        self.assertEqual(resp.status, 400)
        self.assertEqual(payload["error"], "invalid Content-Length")

        con = db.connect(self.root)
        try:
            row = con.execute("SELECT status FROM runs WHERE id=1").fetchone()
        finally:
            con.close()
        self.assertEqual(row["status"], "running")


if __name__ == "__main__":
    unittest.main()
