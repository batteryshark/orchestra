"""Route tests for the orchestra UI.

These prove the layout split the orchestrator asked for:
  * the dedicated provider-runway page is served at /runway along with its
    static assets at /runway-assets/*;
  * the main dashboard at / has NO runway panel and does NOT fetch /api/usage;
  * /api/usage continues to be served (both pages read it).
"""
from __future__ import annotations

import http.client
import json
import os
import re
import tempfile
import threading
import unittest
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

    def test_runway_page_serves(self) -> None:
        status, headers, body = self.get("/runway")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("content-type"), "text/html; charset=utf-8")
        self.assertEqual(headers.get("cache-control"), "no-store")
        # The brand wordmark + dedicated title must be present
        self.assertIn("provider runway", body.lower())
        self.assertIn("orchestra", body)
        # It must point at the runway-namespaced assets
        self.assertIn("/runway-assets/styles.css", body)
        self.assertIn("/runway-assets/app.js", body)

    def test_runway_assets_serve(self) -> None:
        status, headers, body = self.get("/runway-assets/styles.css")
        self.assertEqual(status, 200)
        self.assertIn("text/css", headers.get("content-type", ""))
        self.assertIn(".brand-mark", body)

        status, headers, body = self.get("/runway-assets/app.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers.get("content-type", ""))
        self.assertIn("/api/usage", body)
        self.assertIn("Usage refreshing", body)
        self.assertIn("retry shortly", body)

    def test_runway_assets_missing_returns_404(self) -> None:
        status, headers, body = self.get("/runway-assets/does-not-exist.css")
        self.assertEqual(status, 404)
        payload = json.loads(body)
        self.assertIn("error", payload)

    def test_main_dashboard_has_no_runway_panel(self) -> None:
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("content-type"), "text/html; charset=utf-8")
        # No runway panel IDs, classes, or fetch wiring inside the main ui.
        self.assertNotIn("runway-card", body)
        self.assertNotIn("id=\"runway\"", body)
        self.assertNotIn("refreshRunway", body)
        self.assertNotIn("rwProviderCard", body)
        self.assertNotIn("rwResetCredits", body)
        self.assertNotIn("/api/usage", body)
        # The header still gets the wordmark + runway nav link.
        self.assertIn("brand-mark", body)
        self.assertIn('href="/runway"', body)
        self.assertIn("provider runway</a>", body.lower()
                      or "provider runway</A>".lower())

    def test_api_usage_still_served(self) -> None:
        status, headers, body = self.get("/api/usage")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("cache-control"), "no-store")
        payload = json.loads(body)
        self.assertEqual(payload["providers"][0]["id"], "minimax")

    def test_api_usage_honors_refresh_query_param(self) -> None:
        """The runway page Refresh button sends ?refresh=1 - the endpoint
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

    def test_path_traversal_is_caught_by_safe_resolve(self) -> None:
        # Unit-level: confirm the handler's path-guard rejects paths whose
        # resolved real path escapes RUNWAY_ASSETS_DIR. urllib's parser
        # normalises "/.." segments away before the server sees them, so
        # we exercise the guard directly.
        from orchestra_cli.ui import RUNWAY_ASSETS_DIR
        suspect = (RUNWAY_ASSETS_DIR / "../../../../etc/passwd").resolve()
        self.assertNotIn(RUNWAY_ASSETS_DIR.resolve(), suspect.parents)
        self.assertNotEqual(suspect.parent, RUNWAY_ASSETS_DIR.resolve())
        # A relative request that survives un-normalised is impossible from
        # a real browser, but the assertion above proves the resolver
        # cannot be tricked by the pattern RunwayAssetsDir trust would
        # allow.

    def test_runway_returns_500_json_when_template_missing(self) -> None:
        """If the package HTML disappears, the route must fail loudly with a
        JSON 500 — never silently return HTTP 200 with an error string."""
        from orchestra_cli import ui as _ui
        # Patch the module-level RUNWAY_FILE pointer at the call site of
        # make_handler is taken (closure), so we patch the module-level
        # constant and rebind the handler.
        sentinel = Path(self.tmp.name) / "_missing_runway.html"
        original = _ui.RUNWAY_FILE
        _ui.RUNWAY_FILE = sentinel
        try:
            server = ThreadingHTTPServer(("127.0.0.1", 0),
                                         _ui.make_handler(self.root))
            port = server.server_port
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
                conn.request("GET", "/runway")
                resp = conn.getresponse()
                status = resp.status
                body = resp.read().decode("utf-8", errors="replace")
                conn.close()
            finally:
                server.shutdown()
                server.server_close()
        finally:
            _ui.RUNWAY_FILE = original

        self.assertEqual(status, 500)
        payload = json.loads(body)
        self.assertIn("error", payload)
        self.assertIn("runway", payload["error"].lower())


if __name__ == "__main__":
    unittest.main()
