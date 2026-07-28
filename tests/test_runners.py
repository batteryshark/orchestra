import json
import tempfile
import tomllib
import unittest

from orchestra_cli import config
from orchestra_cli.runners import build_cmd, claude_rate_limit_text, parse_log


class CodexCommandTests(unittest.TestCase):
    def setUp(self):
        self.agent = {
            "name": "codex",
            "backend": "codex",
            "model": "gpt-test",
            "effort": "high",
        }

    def test_new_session_keeps_exec_flags_before_prompt(self):
        cmd = build_cmd(
            self.agent,
            workdir="/workspace/project",
            title="run-1",
            prompt="do the work",
            add_dirs=["/workspace/root"],
        )

        self.assertEqual(cmd[:2], ["codex", "exec"])
        self.assertEqual(cmd[-1], "do the work")
        self.assertNotIn("resume", cmd)
        self.assertLess(cmd.index("--cd"), len(cmd) - 1)

    def test_resume_places_exec_only_flags_before_subcommand(self):
        cmd = build_cmd(
            self.agent,
            workdir="/workspace/project",
            title="follow-up",
            prompt="continue",
            resume_ref="session-123",
            add_dirs=["/workspace/root"],
        )

        resume_index = cmd.index("resume")
        self.assertEqual(cmd[:2], ["codex", "exec"])
        self.assertEqual(cmd[resume_index + 1 :], ["session-123", "continue"])
        for flag in ("--cd", "--sandbox", "--add-dir", "--skip-git-repo-check"):
            self.assertLess(cmd.index(flag), resume_index)

    def test_default_codex_55_profile_enables_bounded_code_mode_trial(self):
        cfg = tomllib.loads(config.DEFAULT_CONFIG)
        agent = config.agent_cfg(cfg, "codex-55")

        cmd = build_cmd(
            agent,
            workdir="/workspace/project",
            title="run-1",
            prompt="do the work",
        )

        enable_index = cmd.index("--enable")
        self.assertEqual(cmd[enable_index + 1], "code_mode")
        self.assertLess(enable_index, len(cmd) - 1)
        self.assertNotIn("code_mode_only", cmd)


class ClaudeCommandTests(unittest.TestCase):
    def setUp(self):
        self.agent = {
            "name": "claude",
            "backend": "claude",
            "model": "opus",
        }

    def test_prompt_immediately_follows_print_flag(self):
        cmd = build_cmd(
            self.agent,
            workdir="/workspace/project",
            title="run-1",
            prompt="do the work",
        )

        self.assertEqual(cmd[:3], ["claude", "-p", "do the work"])
        self.assertEqual(cmd.count("do the work"), 1)
        self.assertIn("stream-json", cmd)

    def test_resume_keeps_prompt_as_print_value(self):
        cmd = build_cmd(
            self.agent,
            workdir="/workspace/project",
            title="follow-up",
            prompt="continue",
            resume_ref="session-123",
        )

        self.assertEqual(cmd[:3], ["claude", "-p", "continue"])
        self.assertEqual(cmd[cmd.index("--resume") + 1], "session-123")
        self.assertEqual(cmd.count("continue"), 1)


class ClaudeRateLimitTests(unittest.TestCase):
    def test_formats_structured_five_hour_limit_instead_of_spend_copy(self) -> None:
        event = {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": "rejected",
                "resetsAt": 1_784_955_600,
                "rateLimitType": "five_hour",
            },
        }

        text = claude_rate_limit_text(event)

        self.assertEqual(
            text,
            "Claude 5-hour usage limit reached; resets at 2026-07-25T05:00:00+00:00",
        )
        self.assertNotIn("monthly", text)
        self.assertNotIn("spend", text)

    def test_parse_log_keeps_structured_limit_over_synthetic_monthly_message(self) -> None:
        events = [
            {
                "type": "rate_limit_event",
                "session_id": "session-1",
                "rate_limit_info": {
                    "status": "rejected",
                    "resetsAt": 1_784_955_600,
                    "rateLimitType": "five_hour",
                },
            },
            {
                "type": "assistant",
                "session_id": "session-1",
                "error": "rate_limit",
                "message": {
                    "content": [{
                        "type": "text",
                        "text": "You've hit your monthly spend limit",
                    }],
                },
            },
            {
                "type": "result",
                "session_id": "session-1",
                "is_error": True,
                "terminal_reason": "api_error",
                "api_error_status": 429,
                "result": "You've hit your monthly spend limit",
            },
        ]
        with tempfile.NamedTemporaryFile("w+", suffix=".jsonl") as log:
            log.write("\n".join(json.dumps(event) for event in events))
            log.flush()

            session, last_text = parse_log(log.name)

        self.assertEqual(session, "session-1")
        self.assertIn("5-hour usage limit", last_text)
        self.assertNotIn("monthly spend", last_text)


if __name__ == "__main__":
    unittest.main()
