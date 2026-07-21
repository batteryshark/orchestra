"""Route tests for the orchestra UI.

These prove the dashboard-integrated provider runway:
  * the main dashboard owns the compact rail and expandable drawer;
  * the former /runway page redirects to the open dashboard drawer;
  * /api/usage continues to provide the server-side snapshot.
"""
from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

from orchestra_cli import db, ui
from orchestra_cli.usage.models import ProviderResult, QuotaWindow
from orchestra_cli.usage.service import UsageService


class _Stubbed:
    def __init__(self) -> None:
        self._service = UsageService(
            collectors=(("minimax", "MiniMax", self._minimax_provider),)
        )

    @staticmethod
    def _minimax_provider():
        return ProviderResult(
            id="minimax",
            name="MiniMax",
            status="ok",
            plan="Token Plan",
            windows=[
                QuotaWindow.from_remaining(
                    id="weekly",
                    label="Weekly",
                    scope="Coding models",
                    remaining_percent=72.0,
                    resets_at="2026-07-25T00:00:00+00:00",
                )
            ],
            source="fixture",
        )


class RouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        (cls.root / ".orchestra").mkdir(parents=True, exist_ok=True)
        db.connect(cls.root).close()

        cls._stub = _Stubbed()
        cls._real_default = ui.default_service
        ui.default_service = lambda: cls._stub._service  # type: ignore[assignment]

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
        ui.default_service = cls._real_default  # type: ignore[assignment]
        cls.tmp.cleanup()

    def get(self, path: str) -> tuple[int, dict[str, str], str]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=4)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace")
        headers = {k.lower(): v for k, v in resp.getheaders()}
        conn.close()
        return resp.status, headers, body

    def test_runway_bookmark_redirects_to_dashboard_drawer(self) -> None:
        status, headers, body = self.get("/runway")
        self.assertEqual(status, 302)
        self.assertEqual(headers.get("location"), "/?runway=open")
        self.assertEqual(headers.get("cache-control"), "no-store")
        self.assertEqual(body, "")

    def test_old_runway_assets_are_not_served(self) -> None:
        status, _headers, body = self.get("/runway-assets/styles.css")
        self.assertEqual(status, 404)
        self.assertIn("error", json.loads(body))

    def test_main_dashboard_has_runway_rail_and_drawer(self) -> None:
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("content-type"), "text/html; charset=utf-8")
        self.assertIn('id="runwayRail"', body)
        self.assertIn('id="runwayDrawer"', body)
        self.assertIn('id="runwayProviders"', body)
        self.assertIn("/api/usage", body)
        self.assertIn("refreshRunway", body)
        self.assertIn("runwayOpenProvider", body)
        self.assertIn("runwayCreditsHtml(provider.rate_limit_resets)", body)
        self.assertIn("toggleAttribute('inert', !open)", body)
        self.assertIn("@media (max-width:1199px)", body)
        self.assertIn("brand-mark", body)
        self.assertNotIn('href="/runway"', body)

    def test_main_dashboard_has_runtime_stats_drawer(self) -> None:
        status, _headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn('id="runtimeToggle"', body)
        self.assertIn('id="runtimeView"', body)
        self.assertIn('id="runtimeStats"', body)
        self.assertIn("api('api/stats'", body)
        self.assertIn('By roster agent', body)
        self.assertIn('By model', body)
        self.assertIn('Concurrent workers count separately.', body)

    def test_main_dashboard_has_json_stop_control_wiring(self) -> None:
        status, _headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("async function stopRun", body)
        self.assertIn("canStopRun(r)?", body)
        self.assertIn("api/runs/${id}/stop", body)
        self.assertIn("'Content-Type':'application/json'", body)
        self.assertIn("status === 'killed' ? 'stopped by user' : status", body)

    def test_main_dashboard_labels_recallable_queue_ids(self) -> None:
        status, _headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("queued message recalled", body)
        self.assertIn("message #${esc(it.message_id)}", body)

    def test_main_dashboard_groups_session_continuations(self) -> None:
        status, _headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("function continuationLineage(r)", body)
        self.assertIn("r.parent_run && byId.has(r.parent_run)", body)
        self.assertIn("conversation #${esc(lineage.root)}", body)
        self.assertIn("continues #${esc(r.parent_run)}", body)

    def test_api_usage_still_served(self) -> None:
        status, headers, body = self.get("/api/usage")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("cache-control"), "no-store")
        payload = json.loads(body)
        self.assertEqual(payload["providers"][0]["id"], "minimax")

    def test_api_runtime_stats_still_served_for_empty_project(self) -> None:
        status, headers, body = self.get("/api/stats")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("cache-control"), "no-store")
        payload = json.loads(body)
        self.assertEqual(payload["total_seconds"], 0)
        self.assertEqual(payload["by_agent"], [])
        self.assertEqual(payload["by_model"], [])

    def test_runway_keeps_compact_provider_values_while_closed(self) -> None:
        status, _, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("runway-rail-provider", body)
        self.assertIn("runwayRail.appendChild(button)", body)
        self.assertIn("runwaySetOpen(runwayInitiallyOpen", body)

    def test_api_usage_honors_refresh_query_param(self) -> None:
        """The runway drawer Refresh button sends ?refresh=1 - the endpoint
        must force a fresh snapshot, not serve the cached one."""
        from orchestra_cli.usage.service import UsageService as _US
        from orchestra_cli.usage.models import ProviderResult as _PR
        calls = []

        def collector():
            return _PR(id="minimax", name="MiniMax", status="ok",
                       windows=[], source="force-test")

        class _Tracking:
            def snapshot(self, *, force=False):
                calls.append(force)
                return _US(
                    collectors=(("minimax", "MiniMax", collector),)
                ).snapshot(force=force)

        original = ui.default_service
        ui.default_service = lambda: _Tracking()
        try:
            status, _, _ = self.get("/api/usage?refresh=1")
            self.assertEqual(status, 200)
            self.assertEqual(calls, [True])
            status, _, _ = self.get("/api/usage")
            self.assertEqual(status, 200)
            self.assertEqual(calls, [True, False])
        finally:
            ui.default_service = original

    def test_main_dashboard_brand_mark_opacities_match_prototype(self) -> None:
        _status, _headers, body = self.get("/")
        # The exact prototype bar opacities are .45 / .7 (NOT .55 / .75).
        self.assertIn("opacity:.45", body)
        self.assertIn("opacity:.7", body)
        # The brand must render at 13px / 620 (not the previous 16px override).
        self.assertIn("font-size:13px", body)
        self.assertIn("font-weight:620", body)
        # The h1 inside the brand must not carry its own font-size override.
        # (Either no `font-size:16px` on h1, or all of: `font-size:inherit`,
        # `font-weight:inherit` on the h1 selector.)
        self.assertNotIn("header h1 { font-size:16px", body)


class RuntimeSummaryTests(unittest.TestCase):
    def test_combines_concurrent_runtime_and_groups_by_agent_and_model(self) -> None:
        rows = [
            {"agent": "kimi-max", "backend": "opencode", "model": "kimi/k3",
             "status": "done", "started_at": "2026-07-19T00:00:00Z",
             "finished_at": "2026-07-19T01:00:00Z"},
            {"agent": "kimi-max", "backend": "opencode", "model": "kimi/k3-fast",
             "status": "done", "started_at": "2026-07-19T00:00:00Z",
             "finished_at": "2026-07-19T00:30:00Z"},
            {"agent": "opus", "backend": "claude", "model": "opus",
             "status": "running", "started_at": "2026-07-19T00:30:00Z",
             "finished_at": None},
            {"agent": "legacy", "backend": "opencode", "model": None,
             "status": "done", "started_at": "not-a-time", "finished_at": None},
        ]
        result = ui.summarize_runtime(
            rows,
            now=datetime(2026, 7, 19, 1, 0, tzinfo=timezone.utc),
            roles={"kimi-max": "heavy reasoning"},
        )

        self.assertEqual(result["total_seconds"], 7200)
        self.assertEqual(result["timed_runs"], 3)
        self.assertEqual(result["active_runs"], 1)
        self.assertEqual(result["ignored_runs"], 1)
        self.assertEqual(result["by_agent"][0]["agent"], "kimi-max")
        self.assertEqual(result["by_agent"][0]["seconds"], 5400)
        self.assertEqual(result["by_agent"][0]["models"], ["kimi/k3", "kimi/k3-fast"])
        self.assertEqual(result["by_agent"][0]["role"], "heavy reasoning")
        self.assertEqual(result["by_agent"][1]["active_runs"], 1)
        self.assertEqual(
            {(item["backend"], item["model"]) for item in result["by_model"]},
            {("opencode", "kimi/k3"), ("opencode", "kimi/k3-fast"),
             ("claude", "opus")},
        )

if __name__ == "__main__":
    unittest.main()
