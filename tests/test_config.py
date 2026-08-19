import os
import tempfile
import pathlib
import unittest
from pathlib import Path
from unittest import mock

from dromond import config, profile_edit


PROJECT_ID = "53efe3c3-6def-4797-8560-3dce073d7d63"


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.global_path = self.root / "global.toml"
        self.env = mock.patch.dict(os.environ, {
            "DROMOND_CONFIG": str(self.global_path),
            "DROMOND_HOME": str(self.root / "home")})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def test_defaults_without_files(self) -> None:
        """Settings have defaults; profiles deliberately do not. A default
        profile would name no model and launch whatever the backend happens
        to default to, which is the guessing DESIGN §5 exists to end."""
        cfg = config.load()
        self.assertEqual(cfg["settings"]["timeout"], config.DEFAULT_RUN_TIMEOUT_SECONDS)
        self.assertEqual(cfg.get("profiles", {}), {})

    def test_per_project_settings_overlay_but_profiles_stay_global(self) -> None:
        """DESIGN §2 keeps per-project SETTINGS in a [project."<projectId>"]
        table. W-0187 takes profiles out of it: a profile is a global preset,
        and the project's only say is which of them it enables."""
        self.global_path.write_text(
            "[settings]\ntimeout = 100\n\n"
            "[profiles.mine]\nbackend = \"codex\"\nmodel = \"m-global\"\neffort = \"low\"\n\n"
            f"[project.\"{PROJECT_ID}\".settings]\ntimeout = 200\n")
        cfg = config.load(PROJECT_ID)
        self.assertEqual(cfg["settings"]["timeout"], 200)
        self.assertEqual(cfg["profiles"]["mine"]["model"], "m-global")
        self.assertEqual(cfg["profiles"]["mine"]["effort"], "low")

    def test_a_legacy_per_project_profile_table_fails_loudly(self) -> None:
        """The same treatment [agents.NAME] gets. Silently ignoring the table
        would read as "this project has no overrides", which is the one
        answer a stale copy of the config must never give."""
        self.global_path.write_text(
            "[profiles.mine]\nbackend = \"codex\"\nmodel = \"m-global\"\n\n"
            f"[project.\"{PROJECT_ID}\".profiles.mine]\nmodel = \"m-project\"\n")
        with self.assertRaises(SystemExit) as caught:
            config.load(PROJECT_ID)
        message = str(caught.exception)
        self.assertIn(f'[project."{PROJECT_ID}".profiles.mine]', message)
        self.assertIn("enabled_profiles", message)

    def test_a_legacy_table_fails_for_every_project_not_just_its_own(self) -> None:
        """Loading some OTHER project — or none at all — must not read a
        config that still carries overrides as a clean one."""
        self.global_path.write_text(
            "[profiles.mine]\nbackend = \"codex\"\n\n"
            f"[project.\"{PROJECT_ID}\".profiles.mine]\nmodel = \"m-project\"\n")
        for asked in (None, "some-other-uuid"):
            with self.assertRaises(SystemExit):
                config.load(asked)

    def test_check_accepts_toml_and_refuses_garbage(self) -> None:
        """The settings page writes the file only after this returns."""
        self.assertEqual(config.check("[settings]\ntimeout = 9\n")["settings"]["timeout"], 9)
        with self.assertRaises(ValueError) as bad:
            config.check("timeout =")
        self.assertIn("TOML", str(bad.exception))
        with self.assertRaises(ValueError) as legacy:
            config.check(f'[project."{PROJECT_ID}".profiles.mine]\nmodel = "x"\n')
        self.assertIn("enabled_profiles", str(legacy.exception))

    def test_no_enabled_profiles_enables_every_profile(self) -> None:
        """Ten profiles already configured and no project table anywhere: the
        move to an enabled set must change nothing for them."""
        self.global_path.write_text(
            "".join(f"[profiles.p{n}]\nbackend = \"codex\"\nmodel = \"m\"\n\n"
                    for n in range(10))
            + f"[project.\"{PROJECT_ID}\".settings]\ntimeout = 5\n")
        cfg = config.load(PROJECT_ID)
        self.assertIsNone(cfg["enabled_profiles"])
        self.assertEqual(len(config.enabled_profiles(cfg)), 10)
        for n in range(10):
            self.assertTrue(config.is_enabled(cfg, f"p{n}"))
            self.assertEqual(config.staff_profile(cfg, f"p{n}")["name"], f"p{n}")

    def test_an_explicit_list_enables_only_those(self) -> None:
        self.global_path.write_text(
            "[profiles.a]\nbackend = \"codex\"\nmodel = \"m\"\n\n"
            "[profiles.b]\nbackend = \"codex\"\nmodel = \"m\"\n\n"
            f"[project.\"{PROJECT_ID}\"]\nenabled_profiles = [\"a\"]\n")
        cfg = config.load(PROJECT_ID)
        self.assertEqual(cfg["enabled_profiles"], ["a"])
        self.assertEqual(sorted(config.enabled_profiles(cfg)), ["a"])
        self.assertEqual(config.staff_profile(cfg, "a")["name"], "a")
        # and another project still sees both
        self.assertIsNone(config.load("other-uuid")["enabled_profiles"])

    def test_staffing_a_profile_the_project_disabled_is_refused_by_name(self) -> None:
        """Not a silent fallback to whatever else is enabled: the refusal
        names the project and the enabled set, because the fix is one of two
        edits and the reader has to know which."""
        self.global_path.write_text(
            "[profiles.a]\nbackend = \"codex\"\nmodel = \"m\"\n\n"
            "[profiles.b]\nbackend = \"codex\"\nmodel = \"m\"\n\n"
            f"[project.\"{PROJECT_ID}\"]\nenabled_profiles = [\"a\"]\n")
        cfg = config.load(PROJECT_ID)
        with self.assertRaises(SystemExit) as caught:
            config.staff_profile(cfg, "b")
        message = str(caught.exception)
        self.assertIn(PROJECT_ID, message)
        self.assertIn("'b'", message)
        self.assertIn("Enabled there: a", message)
        # the profile itself is still perfectly resolvable — a run already in
        # flight on it is never revalidated (W-0187).
        self.assertEqual(config.profile_cfg(cfg, "b")["name"], "b")

    def test_an_enabled_profiles_that_is_not_a_list_is_refused(self) -> None:
        self.global_path.write_text(
            "[profiles.a]\nbackend = \"codex\"\n\n"
            f"[project.\"{PROJECT_ID}\"]\nenabled_profiles = \"a\"\n")
        with self.assertRaises(SystemExit) as caught:
            config.load(PROJECT_ID)
        self.assertIn("must be a list", str(caught.exception))

    def test_another_project_and_no_project_see_only_the_global_values(self) -> None:
        self.global_path.write_text(
            "[settings]\ntimeout = 100\n\n"
            f"[project.\"{PROJECT_ID}\".settings]\ntimeout = 200\n")
        self.assertEqual(config.load()["settings"]["timeout"], 100)
        self.assertEqual(config.load("other-uuid")["settings"]["timeout"], 100)

    def test_profile_cfg_defaults_and_unknown(self) -> None:
        self.global_path.write_text("[profiles.mine]\nbackend = \"claude\"\n")
        cfg = config.load()
        profile = config.profile_cfg(cfg, "mine")
        self.assertEqual(profile["name"], "mine")
        self.assertEqual(profile["backend"], "claude")
        self.assertEqual(profile["extra_args"], [])
        with self.assertRaises(SystemExit):
            config.profile_cfg(cfg, "nope")

    def test_add_dirs_are_declared_per_project(self) -> None:
        """DESIGN §12: extra directories come from the central config, keyed
        on projectId. Another project sees none of them."""
        self.global_path.write_text(
            "[profiles.mine]\nbackend = \"claude\"\n\n"
            f"[project.\"{PROJECT_ID}\".settings]\n"
            "add_dirs = [\"~/ref\", \"/abs/ref\"]\n")
        profile = config.profile_cfg(config.load(PROJECT_ID), "mine")
        self.assertEqual(profile["add_dirs"],
                         [str(Path("~/ref").expanduser()),
                          str(Path("/abs/ref").expanduser())])
        self.assertEqual(config.profile_cfg(config.load("other"), "mine")["add_dirs"], [])

    def test_malformed_add_dirs_is_a_clear_error(self) -> None:
        self.global_path.write_text(
            "[profiles.mine]\nbackend = \"claude\"\nadd_dirs = \"/ref\"\n")
        with self.assertRaises(SystemExit):
            config.profile_cfg(config.load(), "mine")

    def test_profile_is_template_not_identity(self) -> None:
        """DESIGN D4: resolving a profile returns an independent launch
        template; mutating one resolution never leaks into the configured
        profiles or into another resolution (no shared singleton state)."""
        self.global_path.write_text('[profiles.codex]\nbackend = "codex"\n')
        cfg = config.load()
        a = config.profile_cfg(cfg, "codex")
        b = config.profile_cfg(cfg, "codex")
        a["model"] = "mutated"
        a["extra_args"].append("--x")
        self.assertNotIn("model", cfg["profiles"]["codex"])
        self.assertNotEqual(b.get("model"), "mutated")
        self.assertEqual(config.profile_cfg(cfg, "codex")["extra_args"], [])

    def test_no_profiles_configured_says_so_and_points_at_discovery(self) -> None:
        """An empty roster is a setup state, not an 'unknown profile' typo."""
        with self.assertRaises(SystemExit) as ctx:
            config.profile_cfg(config.load(), "anything")
        message = str(ctx.exception)
        self.assertIn("no profiles configured", message)
        self.assertIn("dromond profiles discover", message)

    def test_legacy_agents_table_is_a_clear_error(self) -> None:
        """D10 is greenfield: no alias — a legacy [agents.NAME] table must
        fail loudly with rename instructions naming the config paths."""
        self.global_path.write_text("[agents.codex]\nbackend = \"codex\"\n")
        with self.assertRaises(SystemExit) as ctx:
            config.load()
        message = str(ctx.exception)
        self.assertIn("[agents.NAME]", message)
        self.assertIn("[profiles.NAME]", message)
        self.assertIn(str(self.global_path), message)
        self.assertIn('[project."<projectId>"]', message)

    def test_profile_note_set_and_merged_into_load(self) -> None:
        """W-0173 moved the note into the profile's own table; the JSON
        sidecar is still read so a note written before the move survives."""
        self.global_path.write_text('[profiles.codex]\nbackend = "codex"\n')
        result = profile_edit.save("codex", {"note": "10% weekly left, resets Sunday"})
        self.assertTrue(result["applied"], result)
        cfg = config.load()
        self.assertEqual(cfg["profiles"]["codex"]["note"],
                         "10% weekly left, resets Sunday")
        self.assertTrue(cfg["profiles"]["codex"]["note_at"].endswith("Z"))
        # A note for an unconfigured profile never invents a profile.
        self.assertIn("needs a harness", profile_edit.save("ghost", {"note": "n/a"})["error"])
        self.assertNotIn("ghost", config.load()["profiles"])

    def test_a_legacy_sidecar_note_is_still_read(self) -> None:
        self.global_path.write_text('[profiles.codex]\nbackend = "codex"\n')
        config.profile_notes_path().parent.mkdir(parents=True, exist_ok=True)
        config.profile_notes_path().write_text(
            '{"codex": {"note": "from the sidecar", "note_at": "2026-01-01T00:00:00Z"}}')
        self.assertEqual(config.load()["profiles"]["codex"]["note"], "from the sidecar")
        config.forget_profile_note("codex")
        self.assertIsNone(config.load()["profiles"]["codex"].get("note"))

    def test_worker_env_expansion_and_validation(self) -> None:
        cfg = config.load()
        cfg["worker_env"] = {"MY_PATH": "{root}/data"}
        env = config.apply_worker_env(cfg, {"KEEP": "1"}, self.root)
        self.assertEqual(env["MY_PATH"], f"{self.root}/data")
        self.assertEqual(env["KEEP"], "1")
        cfg["worker_env"] = {"BAD=NAME": "x"}
        with self.assertRaises(SystemExit):
            config.apply_worker_env(cfg, {}, self.root)


def _runway_table_survives_load():
    pass


class RunwayTableTests(unittest.TestCase):
    """Every table has to be named in config.load to survive it. The same
    omission once meant a configured [merge] check never ran."""

    def test_the_runway_table_reaches_the_caller(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "config.toml"
            path.write_text('[runway]\nskip = ["minimax"]\n')
            with mock.patch.dict(os.environ, {"DROMOND_CONFIG": str(path)}):
                cfg = config.load()
        self.assertEqual(["minimax"], cfg["runway"]["skip"])


if __name__ == "__main__":
    unittest.main()
