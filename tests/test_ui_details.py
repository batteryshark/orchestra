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


if __name__ == "__main__":
    unittest.main()
