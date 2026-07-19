from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from orchestra_cli import cli, docs


class PlaybookTemplateTests(unittest.TestCase):
    def test_packaged_template_contains_generic_coordination_contract(self) -> None:
        template = docs.playbook_template()

        self.assertEqual(template.count(docs.PLAYBOOK_MANAGED_START), 1)
        self.assertEqual(template.count(docs.PLAYBOOK_MANAGED_END), 1)
        self.assertIn("## Durable authority and ownership", template)
        self.assertIn("## Correct task size", template)
        self.assertIn("## Required worker brief", template)
        self.assertIn("## Required handoff", template)
        self.assertIn("## Verification and completion gates", template)
        self.assertIn("## Project-specific doctrine", template)
        self.assertNotIn("DIFF_PASS", template)
        self.assertNotIn("HLE", template)

    def test_refresh_replaces_only_managed_section(self) -> None:
        existing = (
            "project preface\n"
            f"{docs.PLAYBOOK_MANAGED_START}\nold generic doctrine\n"
            f"{docs.PLAYBOOK_MANAGED_END}\n"
            "project doctrine\nexact custom text\n"
        )

        refreshed = docs.refresh_playbook(existing)

        self.assertTrue(refreshed.startswith("project preface\n"))
        self.assertTrue(refreshed.endswith("project doctrine\nexact custom text\n"))
        self.assertNotIn("old generic doctrine", refreshed)
        self.assertIn("## Required worker brief", refreshed)

    def test_refresh_refuses_unmarked_legacy_playbook(self) -> None:
        with self.assertRaisesRegex(docs.PlaybookRefreshError, "migrate it manually"):
            docs.refresh_playbook("# customized legacy playbook\n")


class InitPlaybookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.original_cwd = Path.cwd()
        self.original_config = os.environ.get("ORCHESTRA_CONFIG")
        self.original_projects = os.environ.get("ORCHESTRA_PROJECTS_FILE")
        os.environ["ORCHESTRA_CONFIG"] = str(self.root / "global" / "config.toml")
        os.environ["ORCHESTRA_PROJECTS_FILE"] = str(self.root / "global" / "projects.json")
        os.chdir(self.root)

    def tearDown(self) -> None:
        os.chdir(self.original_cwd)
        if self.original_config is None:
            os.environ.pop("ORCHESTRA_CONFIG", None)
        else:
            os.environ["ORCHESTRA_CONFIG"] = self.original_config
        if self.original_projects is None:
            os.environ.pop("ORCHESTRA_PROJECTS_FILE", None)
        else:
            os.environ["ORCHESTRA_PROJECTS_FILE"] = self.original_projects
        self.tmp.cleanup()

    def _init(self, *, refresh: bool = False) -> str:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli.cmd_init(Namespace(work=False, refresh_playbook=refresh))
        return output.getvalue()

    def test_init_creates_packaged_playbook_and_instruction_pointers(self) -> None:
        output = self._init()

        self.assertEqual(
            (self.root / "ORCHESTRA.md").read_text(encoding="utf-8"),
            docs.playbook_template(),
        )
        self.assertIn("created", output)
        for name in ("AGENTS.md", "CLAUDE.md"):
            pointer = (self.root / name).read_text(encoding="utf-8")
            self.assertEqual(pointer.count("<!-- orchestra -->"), 1)
            self.assertIn("read `ORCHESTRA.md`", pointer)

    def test_plain_reinit_preserves_existing_playbook(self) -> None:
        legacy = "# Project-owned playbook without managed markers\n"
        (self.root / "ORCHESTRA.md").write_text(legacy, encoding="utf-8")

        output = self._init()

        self.assertEqual((self.root / "ORCHESTRA.md").read_text(encoding="utf-8"), legacy)
        self.assertIn("preserved existing file", output)

    def test_refresh_preserves_project_owned_suffix(self) -> None:
        self._init()
        playbook = self.root / "ORCHESTRA.md"
        template = playbook.read_text(encoding="utf-8")
        start = template.index(docs.PLAYBOOK_MANAGED_START)
        end = template.index(docs.PLAYBOOK_MANAGED_END) + len(docs.PLAYBOOK_MANAGED_END)
        custom_suffix = "\n\n## Project doctrine\nNever alter the golden fixture.\n"
        playbook.write_text(
            template[:start]
            + docs.PLAYBOOK_MANAGED_START
            + "\nstale generic text\n"
            + docs.PLAYBOOK_MANAGED_END
            + custom_suffix,
            encoding="utf-8",
        )

        output = self._init(refresh=True)
        refreshed = playbook.read_text(encoding="utf-8")

        self.assertTrue(refreshed.endswith(custom_suffix))
        self.assertNotIn("stale generic text", refreshed)
        self.assertIn("## Required worker brief", refreshed)
        self.assertIn("refreshed managed section", output)

    def test_refresh_refuses_legacy_file_without_modifying_it(self) -> None:
        legacy = "# Hand-tuned legacy doctrine\nDo not lose this.\n"
        playbook = self.root / "ORCHESTRA.md"
        playbook.write_text(legacy, encoding="utf-8")

        with self.assertRaisesRegex(SystemExit, "migrate it manually"):
            self._init(refresh=True)

        self.assertEqual(playbook.read_text(encoding="utf-8"), legacy)
        self.assertFalse((self.root / ".orchestra").exists())


if __name__ == "__main__":
    unittest.main()
