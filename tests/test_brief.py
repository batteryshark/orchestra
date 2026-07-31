from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from orchestra_cli import brief


class BriefTests(unittest.TestCase):
    def _compose(self, **overrides) -> str:
        args = {
            "root": Path("/project"),
            "run_id": 323,
            "slug": "chilly_ferret",
            "agent": {
                "name": "glm",
                "role": "strong generalist — standard tier for normal feature work",
            },
            "mission": "Implement the verifier.",
            "work_item": None,
            "team": None,
            "requester": "codex",
            "workdir": "/project",
        }
        args.update(overrides)
        return brief.compose(**args)

    def test_brief_uses_run_identity_without_profile_description_or_as_flags(self) -> None:
        text = self._compose()

        self.assertIn("# Run 323 · chilly_ferret", text)
        self.assertIn("Profile: **glm**", text)
        self.assertNotIn("strong generalist", text)
        self.assertNotIn("--as glm", text)
        self.assertNotIn("inbox glm", text)
        self.assertIn("commands infer them automatically", text)
        self.assertIn("Batch independent read-only searches", text)
        self.assertIn("overlapping writes sequential", text)
        self.assertIn('orchestra consult "<question>"', text)
        self.assertIn("without pausing this run", text)

    def test_question_protocol_only_appears_for_opted_in_run(self) -> None:
        default = self._compose()
        opted_in = self._compose(allow_question=True, question_wait_seconds=60)

        self.assertNotIn("orchestra ask", default)
        self.assertIn("orchestra ask", opted_in)
        self.assertIn("waits up to 60 seconds", opted_in)

    def test_json_work_snapshot_is_compact_markdown_and_omits_empty_fields(self) -> None:
        payload = {
            "id": "W-0141",
            "title": "Hash directory corpora",
            "status": "in_progress",
            "projectPath": "piu-recomp/prex3_remaster",
            "type": "feature",
            "priority": "high",
            "assignee": None,
            "agents": [],
            "tags": ["phase-0"],
            "dependsOn": ["W-0140"],
            "blockedBy": [],
            "blockedReason": None,
            "parentId": "W-0131",
            "sections": {"goal": "Pin trees deterministically.", "completionSummary": ""},
            "requirements": [{"checked": False, "text": "Reject symlinks"}],
            "acceptanceCriteria": [{"checked": True, "text": "Seven tests pass"}],
            "log": [{"at": "now", "message": "Created"}],
        }
        completed = mock.Mock(stdout=json.dumps(payload))
        with mock.patch.object(brief.shutil, "which", return_value="/bin/work"), \
                mock.patch.object(brief.subprocess, "run", return_value=completed):
            text = brief.work_snapshot(Path("/project"), "W-0141")

        self.assertIn("**W-0141** — Hash directory corpora", text)
        self.assertIn("in_progress · high · feature", text)
        self.assertIn("- Depends on: W-0140", text)
        self.assertIn("- [ ] Reject symlinks", text)
        self.assertIn("- [x] Seven tests pass", text)
        self.assertNotIn('"assignee"', text)
        self.assertNotIn("blockedReason", text)
        self.assertNotIn("completionSummary", text)
        self.assertNotIn("Created", text)

    def test_non_json_work_output_is_preserved(self) -> None:
        self.assertEqual(brief._render_work_snapshot("already readable"), "already readable")

    def test_continuation_relies_on_inherited_context_and_keeps_required_handoff(self) -> None:
        text = brief.compose_continuation(
            run_id=324,
            parent_run=323,
            requester="codex",
            instructions="Add the two edge cases.",
            work_item="W-0141",
            allow_question=True,
            question_wait_seconds=60,
        )

        self.assertIn("# Run 324 — continuation of run 323", text)
        self.assertIn("original mission, project instructions", text)
        self.assertIn("Add the two edge cases.", text)
        self.assertIn("orchestra ask", text)
        self.assertIn("orchestra consult", text)
        self.assertIn("wait is 60 seconds", text)
        self.assertNotIn("work log W-0141", text)
        self.assertNotIn("work move W-0141", text)
        self.assertIn('orchestra handoff', text)
        self.assertLess(len(text), 700)


if __name__ == "__main__":
    unittest.main()
