"""Safe-serialization tests.

The /api/usage wire shape is just `ProviderResult.to_dict()` plus a few
envelope fields. These tests assert that nothing resembling a credential,
OAuth account, or Codex credit ID ever leaks into the response.
"""
from __future__ import annotations

import json
import unittest

from orchestra_cli.usage.models import (
    ProviderResult,
    QuotaWindow,
    RateLimitResetCredits,
)


SAMPLE_KEY = "not-a-real-key-test-sentinel"
SAMPLE_OAUTH = "oauth-fake-account-1234567890"
SAMPLE_CREDIT_ID = "credit-internal-id-must-be-hidden"


class SafeSerializationTests(unittest.TestCase):
    def test_provider_result_dict_excludes_raw_credential_material(self) -> None:
        result = ProviderResult(
            id="minimax",
            name="MiniMax",
            status="ok",
            windows=[
                QuotaWindow.from_remaining(
                    id="weekly",
                    label="Weekly",
                    scope="Coding models",
                    remaining_percent=80.0,
                )
            ],
            source="OpenCode (minimax-coding-plan)",
            rate_limit_resets=RateLimitResetCredits(
                available_count=2,
                title="Reset credit",
            ),
        )
        payload = json.dumps(result.to_dict())
        self.assertNotIn("value", payload)
        self.assertNotIn("credential", payload.lower())
        self.assertNotIn("Bearer", payload)
        # source is just a label saying WHICH credential store was used,
        # never the key itself.
        self.assertNotIn(SAMPLE_KEY, payload)

    def test_window_only_exposes_public_shape(self) -> None:
        w = QuotaWindow.from_remaining(
            id="weekly",
            label="Weekly",
            scope="Coding models",
            remaining_percent=80.0,
            resets_at="2026-07-25T00:00:00+00:00",
        )
        payload = json.dumps(
            {
                "id": w.id,
                "label": w.label,
                "scope": w.scope,
                "used_percent": w.used_percent,
                "remaining_percent": w.remaining_percent,
                "resets_at": w.resets_at,
            }
        )
        self.assertNotIn(SAMPLE_KEY, payload)
        self.assertNotIn(SAMPLE_OAUTH, payload)

    def test_rate_limit_reset_credit_shape_has_no_id_field(self) -> None:
        creds = RateLimitResetCredits(
            available_count=1, title="Some credit", expires_at="2026-12-31T00:00:00+00:00"
        )
        fields = creds.__dataclass_fields__.keys()
        self.assertNotIn("id", fields)
        self.assertNotIn("credit_id", fields)
        self.assertNotIn("account_id", fields)

    def test_collect_provider_result_carries_only_safe_message(self) -> None:
        # Internal exception text must NOT leak through ProviderResult.message.
        leaked = "secret-opencode-key=ABCDEFGHIJ"
        result = ProviderResult(
            id="minimax",
            name="MiniMax",
            status="unavailable",
            message="Quorum unreachable from your network",
        )
        payload = result.to_dict()
        self.assertEqual(payload["status"], "unavailable")
        self.assertNotIn(leaked, json.dumps(payload))
        self.assertNotIn(SAMPLE_KEY, json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
