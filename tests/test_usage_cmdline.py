"""Focused tests for the cmd_usage Codex reset-credit formatting."""
from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout

from orchestra_cli import cli


class CmdUsageResetCreditFormatTests(unittest.TestCase):
    def _format(self, resets):
        return cli._format_reset_credits(resets)

    def test_zero_credits_renders_explicit_zero(self) -> None:
        # Critical UX: operators must see "0 reset credits available" rather
        # than a missing row when the Codex account has nothing to spend.
        text = self._format({"available_count": 0, "title": None,
                             "expires_at": None})
        self.assertEqual(text, " · 0 reset credits available")

    def test_single_credit_uses_singular(self) -> None:
        text = self._format({"available_count": 1, "title": None,
                             "expires_at": None})
        self.assertEqual(text, " · 1 reset credit available")

    def test_multiple_credits_uses_plural(self) -> None:
        text = self._format({"available_count": 3, "title": None,
                             "expires_at": None})
        self.assertEqual(text, " · 3 reset credits available")

    def test_missing_returns_blank(self) -> None:
        self.assertEqual(self._format(None), "")
        self.assertEqual(self._format({}), "")

    def test_invalid_count_returns_blank(self) -> None:
        self.assertEqual(self._format({"available_count": -1}), "")
        self.assertEqual(self._format({"available_count": "zero"}), "")


class CmdUsageProviderLineRenderingTests(unittest.TestCase):
    """cmd_usage should print the reset-credit note for Codex even when
    available_count is zero, and omit it for providers that don't carry a
    rate_limit_resets payload."""

    def setUp(self) -> None:
        # Stub the cached service
        self._snapshots = [
            {
                "generated_at": "x", "status": "ok",
                "providers": [
                    {
                        "id": "codex", "name": "Codex", "status": "ok",
                        "plan": "Prolite",
                        "headroom_percent": 77.0,
                        "rate_limit_resets": {"available_count": 0,
                                               "title": None,
                                               "expires_at": None},
                    },
                    {
                        "id": "minimax", "name": "MiniMax", "status": "ok",
                        "plan": "Token Plan",
                        "headroom_percent": 93.0,
                        "rate_limit_resets": None,
                    },
                ],
                "recommendation": None, "trend": {},
            }
        ]
        self._call_count = 0

        def _service():
            class _S:
                def snapshot(self_inner, *, force=False):
                    self._call_count += 1
                    return self._snapshots[0]
            return _S()

        self._orig = cli.default_service
        cli.default_service = _service  # type: ignore[assignment]

    def tearDown(self) -> None:
        cli.default_service = self._orig  # type: ignore[assignment]

    def test_codex_zero_credits_line_includes_zero_count(self) -> None:
        from argparse import Namespace
        out = io.StringIO()
        with redirect_stdout(out):
            cli.cmd_usage(Namespace(refresh=False))
        lines = out.getvalue().splitlines()
        codex_line = next(l for l in lines if "Codex" in l and "headroom" in l)
        self.assertIn("0 reset credits available", codex_line)

    def test_minimax_has_no_reset_credit_note(self) -> None:
        from argparse import Namespace
        out = io.StringIO()
        with redirect_stdout(out):
            cli.cmd_usage(Namespace(refresh=False))
        lines = out.getvalue().splitlines()
        minimax_line = next(l for l in lines if "MiniMax" in l and "headroom" in l)
        self.assertNotIn("reset credit", minimax_line)


if __name__ == "__main__":
    unittest.main()
