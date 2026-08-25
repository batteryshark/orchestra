"""Profile discovery parsing from fixture output; no live harness calls."""
import unittest
from datetime import datetime, timezone
from pathlib import Path

from orchestra import profiles


OPENCODE_FIXTURE = """\
deepseek/deepseek-chat
deepseek/deepseek-v4-pro
lmstudio/openai/gpt-oss-20b
"""

CODEX_FIXTURE = """\
{"models":[{"slug":"gpt-5.6-sol","default_reasoning_level":"low",
"supported_reasoning_levels":[{"effort":"low"},{"effort":"max"}]}]}
"""

REASONIX_FIXTURE = """\
[[providers]]
name = "ds4"
models = ["deepseek-v4-flash", "deepseek-v4-pro"]
supported_efforts = ["high", "max"]
default_effort = "high"
"""


class ParserTests(unittest.TestCase):
    def test_parser_contract_per_listable_harness(self) -> None:
        cases = (
            ("opencode", profiles.parse_opencode_models, OPENCODE_FIXTURE, {
                "deepseek": ["deepseek-chat", "deepseek-v4-pro"],
                "lmstudio": ["openai/gpt-oss-20b"],
            }),
            ("codex", profiles.parse_codex_models, CODEX_FIXTURE, [
                {"model": "gpt-5.6-sol", "efforts": ["low", "max"],
                 "default_effort": "low"},
            ]),
            ("reasonix", profiles.parse_reasonix_config, REASONIX_FIXTURE, [
                {"provider": "ds4",
                 "models": ["deepseek-v4-flash", "deepseek-v4-pro"],
                 "efforts": ["high", "max"], "default_effort": "high"},
            ]),
        )
        for backend, parser, fixture, expected in cases:
            with self.subTest(backend=backend):
                self.assertEqual(parser(fixture), expected)

    def test_discovery_fails_soft_per_harness(self) -> None:
        def runner(cmd):
            if cmd[0] == "opencode":
                return OPENCODE_FIXTURE, None
            return None, f"{cmd[0]} is not installed"

        found = profiles.discover(
            runner=runner, reasonix_config=Path("/nonexistent/reasonix.toml"))
        self.assertIn("deepseek", found["opencode"]["data"])
        for backend in ("codex", "reasonix", "claude"):
            with self.subTest(backend=backend):
                self.assertIsNone(found[backend]["data"])
                self.assertTrue(found[backend]["error"])

        malformed = profiles.discover(
            runner=lambda cmd: ("not json", None),
            reasonix_config=Path("/nonexistent/reasonix.toml"))
        self.assertIn("could not parse", malformed["codex"]["error"])


class NoteAgeTests(unittest.TestCase):
    def test_age_buckets_and_bad_values(self) -> None:
        now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
        cases = (
            ("2026-08-12T11:59:30Z", "just now"),
            ("2026-08-12T11:45:00Z", "15m ago"),
            ("2026-08-12T10:00:00Z", "2h ago"),
            ("2026-08-09T12:00:00Z", "3d ago"),
            (None, None),
            ("garbage", None),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(profiles.note_age(value, now), expected)


if __name__ == "__main__":
    unittest.main()
