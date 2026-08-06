from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestra_cli import db, harness_hooks


class HarnessHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".orchestra").mkdir()
        (self.root / ".orchestra" / "config.toml").write_text(
            '[settings]\ndefault_requester = "codex"\n', encoding="utf-8"
        )
        db.connect(self.root).close()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _message(self, *, kind: str | None, body: str) -> int:
        con = db.connect(self.root)
        try:
            cur = con.execute(
                "INSERT INTO messages(sender,recipient,body,kind,created_at) "
                "VALUES('worker','codex',?,?,?)",
                (body, kind, db.now()),
            )
            con.commit()
            return int(cur.lastrowid)
        finally:
            con.close()

    def test_claims_consultation_without_stealing_runner_interrupt(self) -> None:
        interrupt_id = self._message(kind="interrupt", body="[INTERRUPT] use v2")
        consultation_id = self._message(kind="consult", body="CONSULT run 4: v1 or v2?")

        prompt = harness_hooks.wait_for_attention(self.root, poll_interval=0.01)

        self.assertIn("CONSULT run 4", prompt)
        con = db.connect(self.root)
        try:
            rows = {
                row["id"]: row["read_at"]
                for row in con.execute(
                    "SELECT id,read_at FROM messages WHERE id IN (?,?)",
                    (interrupt_id, consultation_id),
                )
            }
        finally:
            con.close()
        self.assertIsNone(rows[interrupt_id])
        self.assertIsNotNone(rows[consultation_id])

    def test_current_supervised_run_does_not_wait_on_itself(self) -> None:
        con = db.connect(self.root)
        try:
            cur = con.execute(
                "INSERT INTO runs(agent,backend,requested_by,workdir,status,started_at) "
                "VALUES('codex','codex','codex',?,'running',?)",
                (str(self.root), db.now()),
            )
            con.commit()
            run_id = int(cur.lastrowid)
        finally:
            con.close()

        with mock.patch.dict(
            os.environ,
            {"ORCHESTRA_SELF": "codex", "ORCHESTRA_RUN_ID": str(run_id)},
            clear=False,
        ):
            self.assertIsNone(
                harness_hooks.wait_for_attention(self.root, poll_interval=0.01)
            )

    def test_backend_results_continue_only_when_attention_is_needed(self) -> None:
        prompt = "worker finished"
        self.assertEqual(harness_hooks.render_hook_result("opencode", prompt), prompt)
        self.assertEqual(harness_hooks.render_hook_result("opencode", None), "")
        codex = json.loads(harness_hooks.render_hook_result("codex", prompt))
        self.assertEqual(codex, {"decision": "block", "reason": prompt})
        self.assertEqual(json.loads(harness_hooks.render_hook_result("claude", None)), {})

    def test_install_merges_existing_hooks_and_preserves_other_settings(self) -> None:
        codex = self.root / ".codex" / "hooks.json"
        codex.parent.mkdir()
        codex.write_text(
            json.dumps({"hooks": {"Stop": [{"hooks": [{"command": "other"}]}]}}),
            encoding="utf-8",
        )
        claude = self.root / ".claude" / "settings.json"
        claude.parent.mkdir()
        claude.write_text(json.dumps({"permissions": {"allow": ["Read"]}}), encoding="utf-8")

        first = harness_hooks.install(self.root)
        second = harness_hooks.install(self.root)

        codex_data = json.loads(codex.read_text(encoding="utf-8"))
        claude_data = json.loads(claude.read_text(encoding="utf-8"))
        commands = [
            hook["command"]
            for group in codex_data["hooks"]["Stop"]
            for hook in group["hooks"]
        ]
        self.assertEqual(commands, ["other", "orchestra hook --backend codex"])
        self.assertEqual(claude_data["permissions"], {"allow": ["Read"]})
        self.assertIn("installed", " ".join(first))
        self.assertTrue(all("already present" in status for status in second))
        self.assertIn(
            'event.type !== "session.idle"',
            (self.root / ".opencode" / "plugins" / "orchestra.js").read_text(),
        )


if __name__ == "__main__":
    unittest.main()
