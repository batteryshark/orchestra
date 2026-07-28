from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orchestra_cli import db
from orchestra_cli.usage.spend import tracked_opencode_spend, with_project_spend


class ProjectSpendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".orchestra").mkdir()
        self.together_log = self.root / "together.jsonl"
        self.other_log = self.root / "other.jsonl"
        self.together_log.write_text(
            "\n".join(
                [
                    json.dumps(
                        {"part": {"type": "step-finish", "cost": 0.125}}
                    ),
                    json.dumps(
                        {"part": {"type": "step-finish", "cost": 0.375}}
                    ),
                    json.dumps({"part": {"type": "text", "cost": 100}}),
                    "not json",
                ]
            )
            + "\n"
        )
        self.other_log.write_text(
            json.dumps({"part": {"type": "step-finish", "cost": 9.99}}) + "\n"
        )
        con = db.connect(self.root)
        try:
            con.execute(
                "INSERT INTO runs(agent, backend, model, requested_by, workdir, "
                "status, started_at, log_path) VALUES(?,?,?,?,?,?,?,?)",
                (
                    "glm-together",
                    "opencode",
                    "togetherai/zai-org/GLM-5.2",
                    "codex",
                    str(self.root),
                    "done",
                    db.now(),
                    str(self.together_log),
                ),
            )
            con.execute(
                "INSERT INTO runs(agent, backend, model, requested_by, workdir, "
                "status, started_at, log_path) VALUES(?,?,?,?,?,?,?,?)",
                (
                    "glm",
                    "opencode",
                    "zhipuai-coding-plan/glm-5.2",
                    "codex",
                    str(self.root),
                    "done",
                    db.now(),
                    str(self.other_log),
                ),
            )
            con.commit()
        finally:
            con.close()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_sums_only_matching_provider_step_costs(self) -> None:
        self.assertEqual(tracked_opencode_spend(self.root, "togetherai/"), 0.5)

    def test_enrichment_is_project_scoped_and_does_not_mutate_cache(self) -> None:
        snapshot = {
            "providers": [
                {
                    "id": "together",
                    "account_balance": {"currency": "USD", "remaining": 21.78},
                }
            ]
        }
        enriched = with_project_spend(snapshot, self.root)

        self.assertNotIn("spent", snapshot["providers"][0]["account_balance"])
        balance = enriched["providers"][0]["account_balance"]
        self.assertEqual(balance["spent"], 0.5)
        self.assertEqual(balance["spent_scope"], f"{self.root.name} runs")


if __name__ == "__main__":
    unittest.main()
