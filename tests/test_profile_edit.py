"""Managed profile edits against an isolated config file."""
import contextlib
import io
import json
import os
import stat
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from orchestra import config, db, nod, profile_edit


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
            # Never the default path: that one is the developer's real
            # Nod credentials.
            "ORCHESTRA_NOD_SECRETS_FILE": str(Path(self.tmp.name) / "none.env"),
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
    """DESIGN §5's split, and where a refused change goes.

    ``profile_edit`` writes ONE durable record and knows nothing about a
    record system (CONTRACT §7 Enforcement). The record is what survives; a
    source adapter files the decision off it later.
    """

    def cards(self) -> list:
        con = db.connect()
        try:
            return list(con.execute("SELECT * FROM nod_requests ORDER BY rowid"))
        finally:
            con.close()

    def test_agent_authority_boundary(self) -> None:
        allowed = ({"note": "use it"}, {"effort": "low"})
        for changes in allowed:
            with self.subTest(allowed=changes):
                self.path.write_text(CONFIG)
                self.assertTrue(self.save(
                    "thinker", changes, authority="agent")["applied"])
                self.assertEqual(self.cards(), [])

        protected = (
            ("thinker", {"effort": "ultra"}, False),
            ("thinker", {"model": "gpt-5.6-luna"}, False),
            ("thinker", {"tier": 3}, False),
            ("thinker", {"priority": 0}, False),
            ("newone", {"backend": "codex", "model": "gpt-5.6-sol"}, False),
            ("cheap", {}, True),
        )
        for i, (name, changes, delete) in enumerate(protected, start=1):
            with self.subTest(protected=name, changes=changes, delete=delete):
                self.path.write_text(CONFIG)
                before = self.path.read_text()
                result = self.save(name, changes, delete=delete,
                                   authority="agent")
                self.assertFalse(result["applied"])
                self.assertTrue(result["filed"])
                self.assertEqual(self.path.read_text(), before)
                cards = self.cards()
                self.assertEqual(len(cards), i)
                self.assertEqual(cards[-1]["request_id"], result["escalation"])
                self.assertEqual(cards[-1]["kind"], nod.PROFILE_CHANGE)
                self.assertEqual(cards[-1]["status"], nod.RECORDED)
                self.assertIn(name, cards[-1]["title"])
                # The VALUES, not just the refusal. This is what was lost.
                for key, value in changes.items():
                    self.assertIn(repr(value), cards[-1]["body"])

    def test_the_values_survive_when_the_source_cannot_be_reached(self) -> None:
        """The 2026-08-28 loss: filing failed and took the request with it.

        The record is written before anything can fail, so a source that
        refuses every call changes nothing about what a human can read back.
        """
        result = self.save("thinker", {"model": "gpt-5.6-luna", "tier": 3},
                           authority="agent")
        self.assertTrue(result["filed"])

        # And a human reads it back from the CLI with no source at all.
        printed = CliContractTests.run_cli(["profiles"])
        self.assertIn("waiting on you", printed)
        self.assertIn("'gpt-5.6-luna'", printed)
        self.assertIn("tier = 3", printed)


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
    @staticmethod
    def run_cli(argv: list[str], options=None) -> str:
        from orchestra import cli

        output = io.StringIO()
        patches = [mock.patch.object(cli.sys, "argv", ["orchestra", *argv]),
                   contextlib.redirect_stdout(output)]
        if options is not None:
            patches.insert(0, mock.patch.object(
                cli.profile_edit, "discovery_options", return_value=options))
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            cli.main()
        return output.getvalue()

    def cli(self, argv: list[str]) -> str:
        return self.run_cli(argv, self.options)

    def test_picker_and_agent_authority_reach_the_shared_write_path(self) -> None:
        with mock.patch("builtins.input", return_value="2"):
            self.cli(["profiles", "set", "thinker", "--model"])
        self.assertEqual(self.table("thinker")["model"], "gpt-5.6-luna")

        with mock.patch.dict(os.environ, {"ORCHESTRA_RUN_ID": "12"}):
            output = self.cli([
                "profiles", "set", "thinker", "--model", "gpt-5.6-sol"])
        self.assertIn("recorded for the human", output)
        self.assertEqual(self.table("thinker")["model"], "gpt-5.6-luna")
        self.assertIn("'gpt-5.6-sol'", self.run_cli(["profiles"]))


if __name__ == "__main__":
    unittest.main()


class InsertPointTests(unittest.TestCase):
    """W-render: a new key lands in its own table, not under the next
    section's heading. The live config carried the scar — grok-4-6's tier
    and priority sat below `# --- workhorses ---`, parsing right and
    reading wrong."""

    SCARRED = (
        '[profiles.grok-4-6]\n'
        'backend = "opencode"\n'
        'model = "xai/grok-4.6"\n'
        '# OpenCode exposes no effort flag.\n'
        '\n'
        '# --- workhorses ---------------------------------------------\n'
        '\n'
        '[profiles.luna-max]\n'
        'backend = "codex"\n'
    )

    def test_a_new_key_stays_above_the_next_sections_heading(self):
        after = profile_edit.render(self.SCARRED, "grok-4-6", {"tier": 2})
        lines = after.splitlines()
        self.assertLess(lines.index("tier = 2"),
                        next(i for i, l in enumerate(lines)
                             if l.startswith("# --- workhorses")))
        self.assertEqual(tomllib.loads(after)["profiles"]["grok-4-6"]["tier"], 2)

    def test_a_comment_touching_a_key_keeps_the_new_key_below_it(self):
        after = profile_edit.render(self.SCARRED, "grok-4-6", {"tier": 2})
        lines = after.splitlines()
        self.assertLess(lines.index("# OpenCode exposes no effort flag."),
                        lines.index("tier = 2"))

    def test_the_next_table_is_never_entered(self):
        after = profile_edit.render(self.SCARRED, "grok-4-6", {"tier": 2})
        self.assertNotIn("tier", tomllib.loads(after)["profiles"]["luna-max"])

    def test_a_table_at_end_of_file_is_unchanged_in_behaviour(self):
        text = '[profiles.solo]\nbackend = "codex"\n'
        after = profile_edit.render(text, "solo", {"tier": 1})
        self.assertEqual(after, '[profiles.solo]\nbackend = "codex"\ntier = 1\n')


