from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from orchestra_cli.usage.credentials import opencode_api_key
from orchestra_cli.usage.providers import (
    ProviderRequestError,
    collect_claude,
    parse_claude,
    parse_codex,
    parse_codex_reset_credits,
    parse_minimax,
    parse_zai,
    read_recent_codex_snapshot,
)


class MiniMaxParserTests(unittest.TestCase):
    def test_parses_remaining_percentages_and_returned_durations(self) -> None:
        payload = {
            "base_resp": {"status_code": 0, "status_msg": "success"},
            "model_remains": [
                {
                    "model_name": "general",
                    "start_time": 1_784_404_800_000,
                    "end_time": 1_784_419_200_000,
                    "current_interval_remaining_percent": 99,
                    "weekly_start_time": 1_783_900_800_000,
                    "weekly_end_time": 1_784_505_600_000,
                    "current_weekly_remaining_percent": 98,
                }
            ],
        }
        windows = parse_minimax(payload)
        self.assertEqual([window.label for window in windows], ["4-hour", "Weekly"])
        self.assertEqual([window.remaining_percent for window in windows], [99.0, 98.0])
        self.assertEqual([window.used_percent for window in windows], [1.0, 2.0])

    def test_rejects_unsuccessful_response(self) -> None:
        with self.assertRaises(ProviderRequestError):
            parse_minimax({"base_resp": {"status_code": 1001}})


class ZaiParserTests(unittest.TestCase):
    def test_parses_coding_and_tool_windows(self) -> None:
        payload = {
            "code": 200,
            "data": {
                "limits": [
                    {
                        "type": "TOKENS_LIMIT",
                        "unit": 3,
                        "number": 5,
                        "percentage": 60,
                        "nextResetTime": 1_784_408_972_994,
                    },
                    {
                        "type": "TOKENS_LIMIT",
                        "unit": 6,
                        "number": 1,
                        "percentage": 30,
                        "nextResetTime": 1_784_643_324_997,
                    },
                    {
                        "type": "TIME_LIMIT",
                        "unit": 5,
                        "number": 1,
                        "percentage": 0,
                        "nextResetTime": 1_784_816_124_982,
                    },
                ]
            },
        }
        windows = parse_zai(payload)
        self.assertEqual([window.label for window in windows], ["5-hour", "Weekly", "Monthly"])
        self.assertEqual([window.scope for window in windows], ["Coding tokens", "Coding tokens", "MCP tools"])
        self.assertEqual([window.remaining_percent for window in windows], [40.0, 70.0, 100.0])


class ClaudeParserTests(unittest.TestCase):
    def test_parses_fractional_utilization_and_iso_reset(self) -> None:
        windows = parse_claude(
            {
                "five_hour": {"utilization": 0.12, "resets_at": "2026-07-18T21:00:00Z"},
                "seven_day": {"utilization": 0.43, "resets_at": "2026-07-22T12:00:00Z"},
                "seven_day_sonnet": None,
                "limits": [
                    {
                        "kind": "weekly_scoped",
                        "percent": 51,
                        "resets_at": "2026-07-22T12:00:00Z",
                        "scope": {"model": {"display_name": "Fable"}},
                    }
                ],
            }
        )
        self.assertEqual([window.used_percent for window in windows], [12.0, 43.0, 51.0])
        self.assertEqual(windows[2].scope, "Fable")
        self.assertTrue(windows[0].resets_at and windows[0].resets_at.endswith("+00:00"))

    def test_collects_from_fresh_non_secret_claude_usage_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".claude.json"
            path.write_text(
                json.dumps(
                    {
                        "oauthAccount": {"subscriptionType": "max"},
                        "cachedUsageUtilization": {
                            "fetchedAtMs": time.time() * 1000,
                            "utilization": {
                                "five_hour": {
                                    "utilization": 84,
                                    "resets_at": "2026-07-18T22:59:59Z",
                                },
                                "seven_day": {
                                    "utilization": 20,
                                    "resets_at": "2026-07-21T22:59:59Z",
                                },
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            result = collect_claude(state_path=path)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.plan, "Claude Max")
        self.assertEqual(result.windows[0].remaining_percent, 16.0)
        self.assertEqual(result.source, "Claude Code /usage cache")


class CodexParserTests(unittest.TestCase):
    def test_parses_multiple_limit_buckets(self) -> None:
        windows, plan = parse_codex(
            {
                "rateLimitsByLimitId": {
                    "codex_bengalfox": {
                        "limitId": "codex_bengalfox",
                        "limitName": "GPT-5.3-Codex-Spark",
                        "primary": {
                            "usedPercent": 0,
                            "windowDurationMins": 10080,
                            "resetsAt": 1_784_949_866,
                        },
                        "planType": "prolite",
                    },
                    "codex": {
                        "limitId": "codex",
                        "primary": {
                            "usedPercent": 19,
                            "windowDurationMins": 10080,
                            "resetsAt": 1_784_949_866,
                        },
                        "planType": "prolite",
                    },
                }
            }
        )
        self.assertEqual(plan, "Prolite")
        self.assertEqual(windows[0].scope, "Codex models")
        self.assertEqual(windows[0].label, "Weekly")
        self.assertEqual(windows[0].remaining_percent, 81.0)
        self.assertEqual(windows[1].scope, "GPT-5.3-Codex-Spark")

    def test_parses_available_rate_limit_reset_credit(self) -> None:
        credits = parse_codex_reset_credits(
            {
                "rateLimitResetCredits": {
                    "availableCount": 1,
                    "credits": [
                        {
                            "status": "available",
                            "title": "Full reset",
                            "expiresAt": 1_786_556_460,
                            "id": "must-not-be-serialized",
                        }
                    ],
                }
            }
        )
        self.assertIsNotNone(credits)
        self.assertEqual(credits.available_count, 1)
        self.assertEqual(credits.title, "Full reset")
        self.assertTrue(credits.expires_at and credits.expires_at.endswith("+00:00"))
        self.assertNotIn("id", credits.__dataclass_fields__)

    def test_rejects_invalid_rate_limit_reset_count(self) -> None:
        self.assertIsNone(
            parse_codex_reset_credits(
                {"rateLimitResetCredits": {"availableCount": -1, "credits": []}}
            )
        )

    def test_bounded_rollout_fallback_reads_latest_rate_limit_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rollout_dir = Path(directory) / "2026/07/18"
            rollout_dir.mkdir(parents=True)
            path = rollout_dir / "rollout-test.jsonl"
            path.write_text(
                json.dumps({"payload": {"rate_limits": {"primary": {"used_percent": 14}}}})
                + "\n",
                encoding="utf-8",
            )
            snapshot = read_recent_codex_snapshot(sessions_path=Path(directory))
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["primary"]["used_percent"], 14)


class CredentialTests(unittest.TestCase):
    def test_reads_named_opencode_provider_without_exposing_other_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.json"
            path.write_text(
                json.dumps(
                    {
                        "minimax-coding-plan": {"type": "api", "key": "fixture-minimax"},
                        "other": {"type": "api", "key": "fixture-other"},
                    }
                ),
                encoding="utf-8",
            )
            credential = opencode_api_key(
                ("minimax-coding-plan",), (), auth_path=path
            )
        self.assertEqual(credential.value, "fixture-minimax")
        self.assertEqual(credential.source, "OpenCode (minimax-coding-plan)")


if __name__ == "__main__":
    unittest.main()
