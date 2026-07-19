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
from unittest import mock

from orchestra_cli import db, ui


class TranscriptNormalizationTests(unittest.TestCase):
    def test_unreadable_saved_prompt_is_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(ui._read_prompt(Path(tmp)))

    def test_suppresses_kimi_placeholder_reasoning_without_losing_real_thinking(self) -> None:
        events = [
            {"part": {"type": "reasoning", "id": "empty", "text": ""}},
            {"part": {"type": "reasoning", "id": "spaces", "text": "  \n"}},
            {"part": {"type": "reasoning", "id": "real", "text": "Inspect the loader."}},
            # A streaming placeholder must not reserve the key and prevent a
            # later populated update from appearing.
            {"part": {"type": "reasoning", "id": "streamed", "text": ""}},
            {"part": {"type": "reasoning", "id": "streamed", "text": "Now patch it."}},
        ]
        transcript = ui.parse_transcript("\n".join(json.dumps(event) for event in events))

        thinking = [item["body"] for item in transcript if item["kind"] == "thinking"]
        self.assertEqual(thinking, ["Inspect the loader.", "Now patch it."])

    def test_teammate_transcript_suppresses_empty_reasoning_parts(self) -> None:
        payload = json.dumps(
            [
                {
                    "info": {"role": "assistant"},
                    "parts": [
                        {"type": "reasoning", "text": ""},
                        {"type": "reasoning", "text": "Useful thought"},
                    ],
                }
            ]
        ).encode()
        response = mock.Mock()
        response.read.return_value = payload
        with mock.patch.object(ui.host, "url", return_value="http://host"), mock.patch.object(
            ui.urllib.request, "urlopen", return_value=response
        ):
            items, _ = ui.teammate_transcript("session")

        self.assertEqual(items, [{"kind": "thinking", "body": "Useful thought"}])


class DetailSerializationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        (cls.root / ".orchestra").mkdir(parents=True, exist_ok=True)
        brief_path = cls.root / "run-1-brief.md"
        brief_path.write_text("Exact runner prompt\nwith mission")
        log_path = cls.root / "run-1.jsonl"
        log_path.write_text('{"part":{"type":"text","id":"a","text":"model reply"}}\n')
        con = db.connect(cls.root)
        con.execute(
            "INSERT INTO runs(agent, backend, model, title, work_item, "
            "team, requested_by, workdir, brief_path, log_path, slug, status, started_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("minimax", "opencode", "minimax-coding-plan/MiniMax-M3",
             "naming work", "W-0007", None, "codex",
             str(cls.root), str(brief_path), str(log_path), "silly_panda", "running",
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
        self.assertEqual(payload["items"], [])

    def test_saved_runner_prompt_precedes_model_output(self) -> None:
        payload = self.get("/api/transcript/1")
        self.assertEqual(
            payload["items"],
            [
                {"kind": "prompt", "body": "Exact runner prompt\nwith mission"},
                {"kind": "text", "body": "model reply"},
            ],
        )

    def test_state_payload_includes_slug_for_dashboard_render(self) -> None:
        # The sidebar shows slugs; check both rows are exposed.
        state = self.get("/api/state")
        slugs = sorted(r["slug"] for r in state["runs"] if r.get("slug"))
        self.assertEqual(slugs, ["feral_otter", "silly_panda"])


class LiveRefreshCacheHeaderTests(unittest.TestCase):
    """Regression test for the dashboard "selected-run detail doesn't update
    until manual browser refresh" bug (W-0014).

    Root cause: ``Handler._json`` did not emit ``Cache-Control: no-store``,
    so the dashboard's repeated ``GET /api/transcript/{id}?etag=X`` polls
    let the browser heuristically cache the ``{unchanged:true}`` 200 keyed
    by URL. Once cached, the browser never re-asked the server, never
    learned the server-side etag had changed, and the detail pane stayed
    stale until a manual page refresh re-keyed the cache.

    The fix is a one-line addition of the ``no-store`` header to
    ``_json``. These tests pin the contract on both the full payload and
    the unchanged short-circuit (the response the browser was actually
    caching) and assert the same header on the other live JSON endpoints
    so the regression cannot return via a different route.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        (cls.root / ".orchestra").mkdir(parents=True, exist_ok=True)
        con = db.connect(cls.root)
        # Write a real log file so the transcript route computes a real
        # (status, size, mtime) etag we can echo back.
        log_path = cls.root / "run-1.jsonl"
        log_path.write_text('{"part":{"type":"text","id":"a","text":"hello"}}\n')
        con.execute(
            "INSERT INTO runs(agent, backend, model, title, work_item, "
            "team, requested_by, workdir, log_path, slug, status, started_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("minimax", "opencode", "minimax-coding-plan/MiniMax-M3",
             "live refresh", "W-0014", None, "codex",
             str(cls.root), str(log_path), "lively_otter", "running",
             "2026-07-18T22:00:00Z"),
        )
        con.commit()
        con.close()
        cls.log_path = log_path

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

    def _get_raw(self, path: str) -> tuple[int, dict, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=4)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            body = resp.read()
            headers = {k.lower(): v for k, v in resp.getheaders()}
            return resp.status, headers, body
        finally:
            conn.close()

    def _matching_etag(self) -> str:
        # Mirror the server's etag formula so we can hit the unchanged
        # short-circuit without hard-coding the mtime.
        st = self.log_path.stat()
        con = db.connect(self.root)
        try:
            status = con.execute("SELECT status FROM runs WHERE id=1").fetchone()["status"]
        finally:
            con.close()
        return f"{status}-{st.st_size}-{int(st.st_mtime)}"

    def test_transcript_full_response_sets_no_store(self) -> None:
        status, headers, body = self._get_raw("/api/transcript/1")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("cache-control"), "no-store")
        payload = json.loads(body.decode("utf-8"))
        self.assertIn("items", payload)
        self.assertNotIn("unchanged", payload)

    def test_transcript_unchanged_response_sets_no_store(self) -> None:
        """The exact response the browser was caching in the W-0014 bug.

        Without no-store, the ``{unchanged:true}`` 200 keyed by
        ``?etag=X`` would be heuristically cached by the browser; once
        cached, the polling loop never reaches the server again, so the
        detail pane stays stale until manual refresh. The header MUST be
        present on this response too, or the regression returns."""
        etag = self._matching_etag()
        status, headers, body = self._get_raw(f"/api/transcript/1?etag={etag}")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("cache-control"), "no-store",
                         "transcript unchanged response must disable browser caching "
                         "or the polling loop will stop reaching the server "
                         "after the first cached {unchanged:true} 200")
        payload = json.loads(body.decode("utf-8"))
        self.assertTrue(payload.get("unchanged"))
        self.assertEqual(payload.get("etag"), etag)

    def test_state_response_sets_no_store(self) -> None:
        # /api/state is also polled every 2.5s by the dashboard; if the
        # browser caches it, the run sidebar stops updating too.
        status, headers, _ = self._get_raw("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("cache-control"), "no-store")

    def test_projects_response_sets_no_store(self) -> None:
        # Project picker polls every 8s; same cache-poisoning risk.
        status, headers, _ = self._get_raw("/api/projects")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("cache-control"), "no-store")


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
