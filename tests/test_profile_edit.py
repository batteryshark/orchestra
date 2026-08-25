"""Managed profile edits against an isolated config file."""
import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestra import config, profile_edit


FOUND = {
    "opencode": {"data": {"deepseek": ["deepseek-v4-pro"]}, "error": None},
    "codex": {"data": [
        {"model": "gpt-5.6-sol", "efforts": ["low", "high", "max", "ultra"]},
        {"model": "gpt-5.6-luna", "efforts": ["low", "high", "max"]},
    ], "error": None},
    "reasonix": {"data": [
        {"provider": "ds4", "models": ["deepseek-v4-flash"],
         "efforts": ["high", "max"]},
    ], "error": None},
    "claude": {"data": None, "error": "no model listing"},
}

CONFIG = """\
# Orchestra profiles + settings. Hand-editable.

[settings]
timeout = 36000        # hard cap for a runaway worker

# --- profiles -------------------------------------------------------------
[profiles.thinker]
backend = "codex"      # the expensive one
model = "gpt-5.6-sol"
effort = "high"

[profiles.cheap]
backend = "opencode"
model = "deepseek/deepseek-v4-pro"
"""

PROJECT_ID = "53efe3c3-6def-4797-8560-3dce073d7d63"


class EditCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.toml"
        self.path.write_text(CONFIG)
        os.chmod(self.path, 0o600)
        self.env = mock.patch.dict(os.environ, {
            "ORCHESTRA_CONFIG": str(self.path),
            "ORCHESTRA_HOME": str(Path(self.tmp.name) / "home"),
        })
        self.env.start()
        self.options = profile_edit.picker_options(FOUND)

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def save(self, name: str, changes: dict, **kwargs) -> dict:
        kwargs.setdefault("options", self.options)
        return profile_edit.save(name, changes, **kwargs)

    def table(self, name: str) -> dict:
        import tomllib
        return tomllib.loads(self.path.read_text())["profiles"][name]


class ProfileWriteTests(EditCase):
    def test_targeted_edit_preserves_comments_and_atomic_permissions(self) -> None:
        before = self.path.read_text()
        result = self.save("thinker", {"effort": "max"})
        self.assertTrue(result["applied"], result)
        self.assertEqual(
            self.path.read_text(), before.replace('effort = "high"', 'effort = "max"'))

        self.save("thinker", {"backend": "claude", "note": "resets Sunday"})
        text = self.path.read_text()
        self.assertIn('backend = "claude"      # the expensive one', text)
        self.assertTrue(self.table("thinker")["note_at"].endswith("Z"))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["config.toml"])

    def test_create_remove_and_first_profile_contracts(self) -> None:
        before = self.path.read_text()
        result = self.save("scout", {
            "backend": "codex", "model": "gpt-5.6-luna", "effort": "low"})
        self.assertTrue(result["applied"], result)
        self.assertTrue(self.path.read_text().startswith(before))
        self.assertEqual(self.table("scout"), {
            "backend": "codex", "model": "gpt-5.6-luna", "effort": "low"})
        self.assertTrue(self.save("cheap", {}, delete=True)["applied"])
        self.assertNotIn("[profiles.cheap]", self.path.read_text())

        self.path.unlink()
        self.assertTrue(self.save("first", {
            "backend": "codex", "model": "gpt-5.6-sol"})["applied"])
        self.assertIn("[profiles.first]", self.path.read_text())
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_new_note_replaces_a_legacy_sidecar_note(self) -> None:
        note_path = config.profile_notes_path()
        note_path.write_text(json.dumps({
            "thinker": {"note": "stale", "note_at": "2026-01-01T00:00:00Z"}}))
        self.assertEqual(config.load()["profiles"]["thinker"]["note"], "stale")
        self.assertTrue(self.save("thinker", {"note": "fresh"})["applied"])
        self.assertEqual(config.load()["profiles"]["thinker"]["note"], "fresh")


class ValidationTests(EditCase):
    def test_picker_and_request_validation_matrix(self) -> None:
        self.assertFalse(self.options["opencode"]["supports_effort"])
        self.assertTrue(self.options["claude"]["free_model"])
        cases = (
            ("cheap", {"effort": "high"}, "takes no reasoning effort"),
            ("thinker", {"effort": "hyper"}, "not 'hyper'"),
            ("thinker", {"model": "gpt-5.6-luna", "effort": "ultra"},
             "not 'ultra'"),
            ("thinker", {"model": "gpt-9-imaginary"}, "discovery lists"),
            ("thinker", {"max_steps": 40}, "not an editable profile key"),
            ("../etc/passwd", {"backend": "codex"}, "not a usable profile name"),
            ("ghost", {"note": "n/a"}, "needs a harness"),
            ("cheap", {"tier": 4}, "tier must be 1"),
            ("cheap", {"priority": -1}, "must not be negative"),
            ("cheap", {"priority": 100}, "priority must be 0-99"),
        )
        for name, changes, marker in cases:
            with self.subTest(name=name, changes=changes):
                self.path.write_text(CONFIG)
                result = self.save(name, changes)
                self.assertFalse(result["applied"])
                self.assertIn(marker, result["error"])

    def test_routing_metadata_normalizes_named_tiers(self) -> None:
        result = self.save("cheap", {"tier": "workhorse", "priority": 10})
        self.assertTrue(result["applied"], result)
        self.assertEqual(self.table("cheap")["tier"], 1)
        self.assertEqual(self.table("cheap")["priority"], 10)

        self.path.write_text(CONFIG + '\ntier = "cheap"\n')
        self.assertTrue(self.save("cheap", {"note": "plenty"})["applied"])


class AuthorityTests(EditCase):
    class FakeWork:
        def __init__(self) -> None:
            self.filed = []

        def create_decision(self, **kwargs) -> dict:
            self.filed.append(kwargs)
            return {"id": "W-9001"}

    def test_agent_authority_boundary(self) -> None:
        allowed = ({"note": "use it"}, {"effort": "low"})
        for changes in allowed:
            with self.subTest(allowed=changes):
                self.path.write_text(CONFIG)
                work = self.FakeWork()
                self.assertTrue(self.save(
                    "thinker", changes, authority="agent", work=work)["applied"])
                self.assertEqual(work.filed, [])

        protected = (
            ("thinker", {"effort": "ultra"}, False),
            ("thinker", {"model": "gpt-5.6-luna"}, False),
            ("thinker", {"tier": 3}, False),
            ("thinker", {"priority": 0}, False),
            ("newone", {"backend": "codex", "model": "gpt-5.6-sol"}, False),
            ("cheap", {}, True),
        )
        for name, changes, delete in protected:
            with self.subTest(protected=name, changes=changes, delete=delete):
                self.path.write_text(CONFIG)
                before = self.path.read_text()
                work = self.FakeWork()
                result = self.save(name, changes, delete=delete,
                                   authority="agent", work=work)
                self.assertFalse(result["applied"])
                self.assertEqual(result["decision"], "W-9001")
                self.assertEqual(self.path.read_text(), before)
                self.assertEqual(len(work.filed), 1)


class EnabledSetTests(EditCase):
    def project_table(self) -> dict:
        import tomllib
        return (tomllib.loads(self.path.read_text()).get("project")
                or {}).get(PROJECT_ID, {})

    def test_add_replace_and_clear_enabled_set(self) -> None:
        before = self.path.read_text()
        self.assertTrue(profile_edit.set_enabled(PROJECT_ID, ["cheap"])["applied"])
        self.assertEqual(self.project_table()["enabled_profiles"], ["cheap"])
        self.assertTrue(all(line in self.path.read_text()
                            for line in before.splitlines() if line.startswith("#")))
        self.assertTrue(profile_edit.set_enabled(
            PROJECT_ID, ["thinker", "cheap"])["applied"])
        self.assertEqual(self.path.read_text().count(f'[project."{PROJECT_ID}"]'), 1)
        self.assertTrue(profile_edit.set_enabled(PROJECT_ID, None)["applied"])
        self.assertIsNone(config.load(PROJECT_ID)["enabled_profiles"])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_enabled_set_rejects_bad_inputs_without_writing(self) -> None:
        for names in (["ghost"], "cheap", ["cheap", 2]):
            with self.subTest(names=names):
                before = self.path.read_text()
                result = profile_edit.set_enabled(PROJECT_ID, names)
                self.assertFalse(result["applied"])
                self.assertTrue(result["error"])
                self.assertEqual(self.path.read_text(), before)


class CliContractTests(EditCase):
    def cli(self, argv: list[str]) -> str:
        from orchestra import cli

        output = io.StringIO()
        with mock.patch.object(cli.profile_edit, "discovery_options",
                               return_value=self.options), \
                mock.patch.object(cli.sys, "argv", ["orchestra", *argv]), \
                contextlib.redirect_stdout(output):
            cli.main()
        return output.getvalue()

    def test_picker_and_agent_authority_reach_the_shared_write_path(self) -> None:
        with mock.patch("builtins.input", return_value="2"):
            self.cli(["profiles", "set", "thinker", "--model"])
        self.assertEqual(self.table("thinker")["model"], "gpt-5.6-luna")

        work = AuthorityTests.FakeWork()
        with mock.patch.dict(os.environ, {"ORCHESTRA_RUN_ID": "12"}), \
                mock.patch.object(profile_edit.work_client, "from_cfg",
                                  return_value=work):
            output = self.cli([
                "profiles", "set", "thinker", "--model", "gpt-5.6-sol"])
        self.assertIn("Work decision", output)
        self.assertEqual(self.table("thinker")["model"], "gpt-5.6-luna")
        self.assertEqual(len(work.filed), 1)


if __name__ == "__main__":
    unittest.main()
