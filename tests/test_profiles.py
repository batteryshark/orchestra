"""Profile discovery parsing (fixture output, no live CLIs) and note age."""
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dromond import profiles

OPENCODE_FIXTURE = """\
opencode/big-pickle
deepseek/deepseek-chat
deepseek/deepseek-v4-pro
lmstudio/openai/gpt-oss-20b
"""

CODEX_FIXTURE = """\
{"models":[{"slug":"gpt-5.6-sol","display_name":"GPT-5.6-Sol",
"default_reasoning_level":"low",
"supported_reasoning_levels":[{"effort":"low"},{"effort":"high"},{"effort":"max"}]},
{"slug":"gpt-5.6-lite","supported_reasoning_levels":[]}]}
"""

REASONIX_FIXTURE = """\
default_model = "ds4/deepseek-v4-flash"

[[providers]]
name = "ds4"
models = ["deepseek-v4-flash", "deepseek-v4-pro"]
supported_efforts = ["high", "max"]
default_effort = "high"

[[providers]]
name = "kimi-coding-plan"
models = ["kimi-for-coding"]
"""


class ParserTests(unittest.TestCase):
    def test_opencode_lines_group_by_provider(self) -> None:
        got = profiles.parse_opencode_models(OPENCODE_FIXTURE)
        self.assertEqual(got["deepseek"], ["deepseek-chat", "deepseek-v4-pro"])
        self.assertEqual(got["opencode"], ["big-pickle"])
        # nested slashes: the provider is the first segment only
        self.assertEqual(got["lmstudio"], ["openai/gpt-oss-20b"])

    def test_codex_json_yields_models_and_efforts(self) -> None:
        got = profiles.parse_codex_models(CODEX_FIXTURE)
        self.assertEqual(got[0], {"model": "gpt-5.6-sol",
                                  "efforts": ["low", "high", "max"],
                                  "default_effort": "low"})
        self.assertEqual(got[1]["efforts"], [])

    def test_reasonix_config_yields_providers(self) -> None:
        got = profiles.parse_reasonix_config(REASONIX_FIXTURE)
        self.assertEqual(got[0], {"provider": "ds4",
                                  "models": ["deepseek-v4-flash", "deepseek-v4-pro"],
                                  "efforts": ["high", "max"],
                                  "default_effort": "high"})
        self.assertEqual(got[1]["efforts"], [])


class DiscoverTests(unittest.TestCase):
    def test_fails_soft_per_backend(self) -> None:
        """One broken backend never hides the others (no live CLI calls)."""
        def runner(cmd):
            if cmd[0] == "opencode":
                return OPENCODE_FIXTURE, None
            return None, f"{cmd[0]} is not installed"
        got = profiles.discover(runner=runner,
                                reasonix_config=Path("/nonexistent/config.toml"))
        self.assertIsNone(got["opencode"]["error"])
        self.assertIn("deepseek", got["opencode"]["data"])
        self.assertIn("not installed", got["codex"]["error"])
        self.assertIn("not found", got["reasonix"]["error"])
        self.assertIn("no model listing", got["claude"]["error"])

    def test_unparseable_output_is_an_error_not_a_crash(self) -> None:
        got = profiles.discover(
            runner=lambda cmd: ("this is not json", None),
            reasonix_config=Path("/nonexistent/config.toml"))
        self.assertIsNone(got["codex"]["data"])
        self.assertIn("could not parse", got["codex"]["error"])


class NoteAgeTests(unittest.TestCase):
    def test_ages(self) -> None:
        now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(profiles.note_age("2026-08-12T11:59:30Z", now), "just now")
        self.assertEqual(profiles.note_age("2026-08-12T11:45:00Z", now), "15m ago")
        self.assertEqual(profiles.note_age("2026-08-12T10:00:00Z", now), "2h ago")
        self.assertEqual(profiles.note_age("2026-08-09T12:00:00Z", now), "3d ago")
        self.assertIsNone(profiles.note_age(None, now))
        self.assertIsNone(profiles.note_age("garbage", now))


if __name__ == "__main__":
    unittest.main()
