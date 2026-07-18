"""Smoke test for the /api/usage endpoint integration in `orchestra_cli.ui`.

Spins up the real handler against a temp project and verifies Cache-Control,
status code, and the wire shape (no credential material leaks).
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
from orchestra_cli.usage.models import ProviderResult, QuotaWindow
from orchestra_cli.usage.service import UsageService


class ApiUsageEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".orchestra").mkdir(parents=True, exist_ok=True)
        db.connect(self.root).close()

        # Patch the `default_service` name already imported into the
        # `orchestra_cli.ui` module so the test does not actually call out
        # to MiniMax / Claude / Z.AI / Codex.
        self._patched_service = UsageService(
            collectors=(("minimax", "MiniMax", self._minimax_provider),)
        )
        self._real_ui_default = ui.default_service
        ui.default_service = lambda: self._patched_service  # type: ignore[assignment]

        self.handler = ui.make_handler(self.root)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        ui.default_service = self._real_ui_default  # type: ignore[assignment]
        self.tmp.cleanup()

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

    def test_api_usage_returns_no_store_json(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=4)
        conn.request("GET", "/api/usage")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")
        body = resp.read().decode()
        payload = json.loads(body)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(len(payload["providers"]), 1)
        provider = payload["providers"][0]
        self.assertEqual(provider["id"], "minimax")
        self.assertEqual(provider["headroom_percent"], 72.0)
        self.assertEqual(provider["windows"][0]["remaining_percent"], 72.0)
        # No credential material may leak.
        self.assertNotIn("Bearer", body)
        self.assertNotIn("secret", body)
        self.assertNotIn("value", body)
        conn.close()

    def test_api_usage_404_on_unknown(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=4)
        conn.request("GET", "/api/no-such-endpoint")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 404)
        body = resp.read().decode()
        payload = json.loads(body)
        self.assertEqual(payload.get("error"), "not found")
        conn.close()

    def test_codex_provider_with_zero_credits_serializes_through_to_dict(self) -> None:
        """The render code is JS — but the wire shape must include
        rate_limit_resets even when the count is zero so the dashboard can
        render an explicit 'no credits available' line."""
        from orchestra_cli.usage.models import RateLimitResetCredits
        result = ProviderResult(
            id="codex",
            name="Codex",
            status="ok",
            plan="Pro",
            windows=[
                QuotaWindow.from_remaining(
                    id="weekly",
                    label="Weekly",
                    scope="Codex models",
                    remaining_percent=80.0,
                    resets_at="2026-07-25T00:00:00+00:00",
                )
            ],
            source="fixture",
            rate_limit_resets=RateLimitResetCredits(
                available_count=0, title=None, expires_at=None
            ),
        )
        serialized = result.to_dict()
        self.assertEqual(serialized["rate_limit_resets"]["available_count"], 0)
        self.assertIsNone(serialized["rate_limit_resets"]["title"])


if __name__ == "__main__":
    unittest.main()
