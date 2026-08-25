import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestra import config


PROJECT_ID = "53efe3c3-6def-4797-8560-3dce073d7d63"


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "config.toml"
        self.env = mock.patch.dict(os.environ, {
            "ORCHESTRA_CONFIG": str(self.path),
            "ORCHESTRA_HOME": str(self.root / "home"),
        })
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def test_defaults_global_profiles_and_project_overlay(self) -> None:
        empty = config.load()
        self.assertEqual(empty["settings"]["timeout"],
                         config.DEFAULT_RUN_TIMEOUT_SECONDS)
        self.assertEqual(empty.get("profiles", {}), {})

        self.path.write_text(
            "[settings]\ntimeout = 100\n"
            "[runway]\nskip = [\"minimax\"]\n"
            "[profiles.mine]\nbackend = \"codex\"\nmodel = \"global\"\n"
            f"[project.\"{PROJECT_ID}\".settings]\ntimeout = 200\n")
        scoped = config.load(PROJECT_ID)
        self.assertEqual(scoped["settings"]["timeout"], 200)
        self.assertEqual(scoped["profiles"]["mine"]["model"], "global")
        self.assertEqual(scoped["runway"]["skip"], ["minimax"])
        self.assertEqual(config.load("other")["settings"]["timeout"], 100)

    def test_parser_and_legacy_shapes_are_rejected(self) -> None:
        self.assertEqual(
            config.check("[settings]\ntimeout = 9\n")["settings"]["timeout"], 9)
        cases = (
            ("timeout =", "TOML"),
            (f'[project."{PROJECT_ID}".profiles.mine]\nmodel = "x"\n',
             "enabled_profiles"),
            ('[agents.old]\nbackend = "codex"\n', "[profiles.NAME]"),
        )
        for text, marker in cases:
            with self.subTest(marker=marker), self.assertRaises(ValueError) as caught:
                config.check(text)
            self.assertIn(marker, str(caught.exception))

        self.path.write_text(
            f'[project."{PROJECT_ID}".profiles.mine]\nmodel = "x"\n')
        for asked in (None, PROJECT_ID, "other"):
            with self.subTest(project=asked), self.assertRaises(SystemExit):
                config.load(asked)

    def test_enabled_profiles_gate_staffing_not_resolution(self) -> None:
        self.path.write_text(
            "[profiles.a]\nbackend = \"codex\"\n"
            "[profiles.b]\nbackend = \"codex\"\n"
            f"[project.\"{PROJECT_ID}\"]\nenabled_profiles = [\"a\"]\n")
        cfg = config.load(PROJECT_ID)
        self.assertEqual(list(config.enabled_profiles(cfg)), ["a"])
        self.assertEqual(config.staff_profile(cfg, "a")["name"], "a")
        self.assertEqual(config.profile_cfg(cfg, "b")["name"], "b")
        with self.assertRaises(SystemExit) as caught:
            config.staff_profile(cfg, "b")
        self.assertIn(PROJECT_ID, str(caught.exception))
        self.assertIn("Enabled there: a", str(caught.exception))

        unscoped = config.load("other")
        self.assertIsNone(unscoped["enabled_profiles"])
        self.assertEqual(set(config.enabled_profiles(unscoped)), {"a", "b"})

    def test_enabled_profiles_must_be_a_string_list(self) -> None:
        for value in ('"a"', '["a", 2]'):
            with self.subTest(value=value):
                self.path.write_text(
                    "[profiles.a]\nbackend = \"codex\"\n"
                    f"[project.\"{PROJECT_ID}\"]\nenabled_profiles = {value}\n")
                with self.assertRaises(SystemExit) as caught:
                    config.load(PROJECT_ID)
                self.assertIn("must be a list", str(caught.exception))

    def test_profile_resolution_is_an_independent_launch_template(self) -> None:
        self.path.write_text(
            "[profiles.mine]\nbackend = \"claude\"\n"
            f"[project.\"{PROJECT_ID}\".settings]\n"
            "add_dirs = [\"~/ref\", \"/abs/ref\"]\n")
        cfg = config.load(PROJECT_ID)
        profile = config.profile_cfg(cfg, "mine")
        self.assertEqual(profile["name"], "mine")
        self.assertEqual(profile["add_dirs"],
                         [str(Path("~/ref").expanduser()), "/abs/ref"])
        profile["extra_args"].append("--changed")
        self.assertEqual(config.profile_cfg(cfg, "mine")["extra_args"], [])
        with self.assertRaises(SystemExit):
            config.profile_cfg(cfg, "missing")

    def test_profile_resolution_refuses_invalid_directories_and_empty_roster(self) -> None:
        self.path.write_text(
            "[profiles.mine]\nbackend = \"claude\"\nadd_dirs = \"/ref\"\n")
        with self.assertRaises(SystemExit):
            config.profile_cfg(config.load(), "mine")

        self.path.unlink()
        with self.assertRaises(SystemExit) as caught:
            config.profile_cfg(config.load(), "anything")
        self.assertIn("orchestra profiles discover", str(caught.exception))

    def test_legacy_profile_note_is_loaded_and_can_be_forgotten(self) -> None:
        self.path.write_text('[profiles.codex]\nbackend = "codex"\n')
        note_path = config.profile_notes_path()
        note_path.write_text(
            '{"codex": {"note": "legacy", "note_at": "2026-01-01T00:00:00Z"}}')
        self.assertEqual(config.load()["profiles"]["codex"]["note"], "legacy")
        config.forget_profile_note("codex")
        self.assertIsNone(config.load()["profiles"]["codex"].get("note"))

    def test_worker_environment_expands_root_and_validates_names(self) -> None:
        cfg = config.load()
        cfg["worker_env"] = {"MY_PATH": "{root}/data"}
        self.assertEqual(
            config.apply_worker_env(cfg, {"KEEP": "1"}, self.root),
            {"KEEP": "1", "MY_PATH": f"{self.root}/data"})
        cfg["worker_env"] = {"BAD=NAME": "x"}
        with self.assertRaises(SystemExit):
            config.apply_worker_env(cfg, {}, self.root)


if __name__ == "__main__":
    unittest.main()
