import json
import tempfile
import tomllib
import unittest

from orchestra_cli import config
from orchestra_cli.runners import (
    apply_backend_env,
    build_cmd,
    claude_rate_limit_text,
    claude_terminal_failure,
    claude_terminal_failure_text,
    parse_log,
)


class OpenCodeEnvironmentTests(unittest.TestCase):
    def test_ordinary_worker_disables_all_delegation_tools_per_process(self) -> None:
        source = {
            "KEEP": "yes",
            "OPENCODE_CONFIG_CONTENT": json.dumps({
                "provider": {"local": {"name": "Local"}},
                "permission": {"read": "allow", "task": "allow"},
            }),
        }

        updated = apply_backend_env({"backend": "opencode"}, source)

        content = json.loads(updated["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(updated["KEEP"], "yes")
        self.assertEqual(content["provider"], {"local": {"name": "Local"}})
        self.assertEqual(content["permission"]["read"], "allow")
        self.assertEqual(content["permission"]["task"], "deny")
        self.assertEqual(content["permission"]["team_spawn"], "deny")
        self.assertIsNot(updated, source)

    def test_explicit_ensemble_and_native_subagent_profiles_keep_delegation(self) -> None:
        env = {"OPENCODE_CONFIG_CONTENT": '{"permission":{"task":"allow"}}'}

        self.assertIs(apply_backend_env(
            {"backend": "opencode", "ensemble": True}, env
        ), env)
        self.assertIs(apply_backend_env(
            {"backend": "opencode", "opencode_native_subagents": True}, env
        ), env)

    def test_invalid_config_content_fails_before_launch(self) -> None:
        with self.assertRaisesRegex(SystemExit, "must be a JSON object"):
            apply_backend_env(
                {"backend": "opencode"}, {"OPENCODE_CONFIG_CONTENT": "[1,2]"}
            )


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

    def test_default_codex_terra_profile_uses_code_mode_capable_model(self):
        cfg = tomllib.loads(config.DEFAULT_CONFIG)
        self.assertNotIn("codex-55", cfg["agents"])
        agent = config.agent_cfg(cfg, "codex-terra")

        cmd = build_cmd(
            agent,
            workdir="/workspace/project",
            title="run-1",
            prompt="do the work",
        )

        enable_index = cmd.index("--enable")
        self.assertEqual(cmd[enable_index + 1], "code_mode")
        self.assertEqual(cmd[cmd.index("-m") + 1], "gpt-5.6-terra")
        self.assertIn("suppress_unstable_features_warning=true", cmd)
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

    def test_requests_visible_streaming_thinking_and_subagent_text(self):
        cmd = build_cmd(
            self.agent,
            workdir="/workspace/project",
            title="run-1",
            prompt="do the work",
        )

        self.assertIn("--include-partial-messages", cmd)
        self.assertIn("--forward-subagent-text", cmd)
        display = cmd.index("--thinking-display")
        self.assertEqual(cmd[display + 1], "summarized")

    def test_explicit_thinking_display_override_is_preserved(self):
        agent = {
            **self.agent,
            "extra_args": [
                "--permission-mode", "acceptEdits",
                "--thinking-display=omitted",
            ],
        }

        cmd = build_cmd(
            agent,
            workdir="/workspace/project",
            title="run-1",
            prompt="do the work",
        )

        self.assertIn("--thinking-display=omitted", cmd)
        self.assertNotIn("--thinking-display", cmd)
        self.assertNotIn("summarized", cmd)

    def test_profile_effort_is_forwarded(self):
        cmd = build_cmd(
            {**self.agent, "effort": "medium"},
            workdir="/workspace/project",
            title="run-1",
            prompt="do the work",
        )

        effort = cmd.index("--effort")
        self.assertEqual(cmd[effort + 1], "medium")

    def test_explicit_effort_override_is_preserved(self):
        cmd = build_cmd(
            {
                **self.agent,
                "effort": "medium",
                "extra_args": ["--effort=max"],
            },
            workdir="/workspace/project",
            title="run-1",
            prompt="do the work",
        )

        self.assertIn("--effort=max", cmd)
        self.assertNotIn("--effort", cmd)
        self.assertNotIn("medium", cmd)

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


class ClaudeTerminalFailureTests(unittest.TestCase):
    def test_extracts_rejected_tool_from_aborted_tools_result(self) -> None:
        events = [
            {
                "type": "assistant",
                "message": {"content": [{
                    "type": "tool_use",
                    "id": "tool-1",
                    "name": "Bash",
                    "input": {
                        "description": "Stop test process",
                        "command": "pkill -f test-process",
                    },
                }]},
            },
            {
                "type": "user",
                "message": {"content": [{
                    "type": "tool_result",
                    "tool_use_id": "tool-1",
                    "is_error": True,
                    "content": "The tool use was rejected. STOP and wait.",
                }]},
            },
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "terminal_reason": "aborted_tools",
                "result": None,
            },
        ]
        with tempfile.NamedTemporaryFile("w+", suffix=".jsonl") as log:
            log.write("\n".join(json.dumps(event) for event in events))
            log.flush()

            failure = claude_terminal_failure(log.name)

        self.assertIsNotNone(failure)
        self.assertEqual(failure.reason, "aborted_tools")
        self.assertTrue(failure.tool_rejected)
        self.assertEqual(failure.tool_name, "Bash")
        self.assertEqual(failure.tool_description, "Stop test process")
        self.assertEqual(failure.tool_command, "pkill -f test-process")
        self.assertIn("Operator guidance is required", claude_terminal_failure_text(failure))

    def test_describes_aborted_streaming_as_external_interruption(self) -> None:
        with tempfile.NamedTemporaryFile("w+", suffix=".jsonl") as log:
            log.write(json.dumps({
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "terminal_reason": "aborted_streaming",
                "result": None,
            }))
            log.flush()

            failure = claude_terminal_failure(log.name)

        self.assertIsNotNone(failure)
        self.assertIn("without an Orchestra stop", claude_terminal_failure_text(failure))


if __name__ == "__main__":
    unittest.main()
