"""Provider inference and quota warning tests.

The dispatch-time advisory uses these utilities, and they are intentionally
small — the goal is to map known agent configs to a provider ID without ever
flagging unknown configs as "alarming".
"""
from __future__ import annotations

import unittest

from orchestra_cli.usage.inference import infer_from_agent, infer_provider
from orchestra_cli.usage.models import ProviderResult, QuotaWindow
from orchestra_cli.usage.warning import (
    QuotaWarning,
    assess_targets,
    render_warning_lines,
)


def provider(provider_id: str, remaining: float, status: str = "ok") -> dict:
    p = ProviderResult(
        id=provider_id,
        name=provider_id.title(),
        status=status,
        windows=[
            QuotaWindow.from_remaining(
                id="weekly",
                label="Weekly",
                scope="Coding models",
                remaining_percent=remaining,
                resets_at="2026-07-25T00:00:00+00:00",
            )
        ],
        source="fixture",
    )
    return p.to_dict()


def snapshot(rows: list[dict]) -> dict:
    return {"providers": rows, "recommendation": None}


class InferProviderTests(unittest.TestCase):
    def test_maps_known_minimax_models_to_minimax(self) -> None:
        self.assertEqual(infer_provider("opencode", "minimax-coding-plan/MiniMax-M3"), "minimax")
        self.assertEqual(infer_provider("opencode", "minimax-cn-coding-plan/Foo"), "minimax")

    def test_maps_zhipuai_to_zai(self) -> None:
        self.assertEqual(infer_provider("opencode", "zhipuai-coding-plan/glm-5.2"), "zai")
        self.assertEqual(infer_provider("opencode", "zai-coding-plan/glm-5.2"), "zai")

    def test_maps_kimi_for_coding_to_kimi(self) -> None:
        self.assertEqual(infer_provider("opencode", "kimi-for-coding/k3"), "kimi")
        self.assertEqual(
            infer_provider("opencode", "kimi-for-coding/kimi-for-coding-highspeed"),
            "kimi",
        )

    def test_claude_backend_maps_to_claude(self) -> None:
        # Coding-plan quota only follows the dedicated Claude backend; bare
        # Anthropic claude-* models over opencode are NOT coding-plan quota.
        self.assertEqual(infer_provider("claude", None), "claude")
        self.assertEqual(infer_provider("claude", "claude-sonnet-4-20250514"), "claude")

    def test_anthropic_opencode_models_are_not_coding_plans(self) -> None:
        # opencode + anthropic/claude-* is normal Anthropic API, not Claude Code
        # plan quota — must return None (fail-open, no warning).
        self.assertIsNone(infer_provider("opencode", "anthropic/claude-3.5-sonnet"))

    def test_maps_codex_backend(self) -> None:
        self.assertEqual(infer_provider("codex", None), "codex")
        self.assertEqual(infer_provider("codex", "gpt-5.5"), "codex")

    def test_unknown_returns_none(self) -> None:
        self.assertIsNone(infer_provider("opencode", "lmstudio/llama3"))
        self.assertIsNone(infer_provider("opencode", "anthropic/claude-3.5-sonnet"))
        self.assertIsNone(infer_provider("silly-backend", None))
        self.assertIsNone(infer_provider(None, None))

    def test_infer_from_agent_reads_dict(self) -> None:
        self.assertEqual(
            infer_from_agent({"backend": "opencode", "model": "minimax-coding-plan/x"}),
            "minimax",
        )
        self.assertEqual(infer_from_agent({"backend": "codex"}), "codex")


class AssessTargetsTests(unittest.TestCase):
    def _warning(self, snap, targets, floor=20.0):
        return assess_targets(snap, targets, warn_at_or_below_percent=floor)

    def test_warns_only_when_headroom_at_or_below_floor(self) -> None:
        snap = snapshot([
            provider("minimax", 12.0),
            provider("claude", 80.0),
            provider("zai", 20.0),
            provider("glime", 21.0),
        ])
        warnings = self._warning(
            snap,
            [("minimax-agent", "minimax"),
             ("claude-agent", "claude"),
             ("zai-agent", "zai"),
             ("glime-agent", "glime")],
        )
        # floor is 20.0 inclusive: 12 and 20 warn, 80 and 21 do not.
        self.assertEqual([w.agent for w in warnings], ["minimax-agent", "zai-agent"])
        # 20.0 itself must trigger (the floor is inclusive)
        self.assertEqual(warnings[1].headroom_percent, 20.0)

    def test_unknown_provider_is_fail_open(self) -> None:
        snap = snapshot([provider("minimax", 1.0)])
        # target agent we have no quota data for — must produce no warning
        warnings = self._warning(snap, [("any-agent", None)])
        self.assertEqual(warnings, [])

        # target mapped to a provider the snapshot doesn't list at all → skip
        warnings = self._warning(snap, [("any-agent", "claude")])
        self.assertEqual(warnings, [])

    def test_stale_or_unavailable_providers_do_not_warn(self) -> None:
        snap = snapshot([
            provider("minimax", 0.0, status="stale"),
            provider("zai", 0.0, status="unavailable"),
        ])
        warnings = self._warning(
            snap,
            [("m", "minimax"), ("z", "zai")],
        )
        self.assertEqual(warnings, [])

    def test_render_warning_lines_for_orchestrator_output(self) -> None:
        snap = snapshot([provider("minimax", 5.0)])
        warnings = self._warning(snap, [("minimax", "minimax")])
        lines = render_warning_lines(warnings)
        self.assertEqual(len(lines), 1)
        self.assertIn("minimax", lines[0].lower())
        self.assertIn("5%", lines[0].lower())

    def test_ensemble_assessment_includes_model_pool(self) -> None:
        """The CLI helper `_resolve_quota_targets` should surface every
        provider an ensemble lead could spin up via its model_pool."""
        from orchestra_cli.cli import _resolve_quota_targets
        cfg = {"agents": {
            "ensemble": {
                "backend": "opencode",
                "model": "minimax-coding-plan/MiniMax-M3",
                "ensemble": True,
                "model_pool": [
                    "minimax-coding-plan/MiniMax-M3",
                    "zhipuai-coding-plan/glm-5.2",
                ],
            },
            "minimax": {"backend": "opencode", "model": "minimax-coding-plan/MiniMax-M3"},
            "glm": {"backend": "opencode", "model": "zhipuai-coding-plan/glm-5.2"},
        }}
        targets = _resolve_quota_targets(cfg, ["ensemble"])
        providers = {p for _, p in targets}
        self.assertIn("minimax", providers)
        self.assertIn("zai", providers)


if __name__ == "__main__":
    unittest.main()
