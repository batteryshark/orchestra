import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from orchestra import harnesses, runners


class BuildCmdTests(unittest.TestCase):
    def build(self, backend: str, **profile) -> list[str]:
        return runners.build_cmd(
            {"name": backend, "backend": backend, "extra_args": [], **profile},
            workdir="/w", title="title", prompt="prompt")

    def test_opencode_contract(self) -> None:
        profile = {"model": "provider/model", "variant": "max"}
        fresh = self.build("opencode", **profile)
        resumed = runners.build_cmd(
            {"name": "opencode", "backend": "opencode", "extra_args": [], **profile},
            workdir="/w", title="title", prompt="prompt", resume_ref="session-1")

        self.assertEqual(fresh[:4], ["opencode", "run", "--dir", "/w"])
        self.assertEqual(fresh[-1], "prompt")
        self.assertIn("--title", fresh)
        self.assertIn("--variant", fresh)
        self.assertIn("--session", resumed)
        self.assertNotIn("--title", resumed)

    def test_codex_contract(self) -> None:
        fresh = self.build(
            "codex", model="gpt-x", effort="high",
            sandbox="danger-full-access")
        resumed = runners.build_cmd(
            {"name": "codex", "backend": "codex", "extra_args": []},
            workdir="/w", title="title", prompt="prompt", resume_ref="thread-1")

        self.assertEqual(fresh[:2], ["codex", "exec"])
        self.assertEqual(fresh[fresh.index("--sandbox") + 1], "danger-full-access")
        self.assertIn('model_reasoning_effort="high"', fresh)
        self.assertIn('model_reasoning_summary="detailed"', fresh)
        self.assertLess(resumed.index("--sandbox"), resumed.index("resume"))
        self.assertEqual(resumed[-2:], ["thread-1", "prompt"])

        override = self.build(
            "codex", extra_args=["-c", 'model_reasoning_summary="concise"'])
        self.assertIn('model_reasoning_summary="concise"', override)
        self.assertNotIn('model_reasoning_summary="detailed"', override)

    def test_claude_contract(self) -> None:
        profile = {"name": "claude", "backend": "claude", "model": "claude-x",
                   "effort": "medium", "extra_args": []}
        cmd = runners.build_cmd(
            profile, workdir="/w", title="title", prompt="prompt",
            resume_ref="session-1")

        self.assertEqual(cmd[:3], ["claude", "-p", "prompt"])
        for flag in ("--resume", "--model", "--effort", "--permission-mode"):
            self.assertIn(flag, cmd)
        explicit = self.build("claude", extra_args=["--allowedTools", "Read"])
        self.assertNotIn("--permission-mode", explicit)

    def test_reasonix_contract(self) -> None:
        fresh = self.build("reasonix", model="deepseek-v3.2", effort="high",
                           max_steps=24)
        resumed = runners.build_cmd(
            {"name": "reasonix", "backend": "reasonix",
             "extra_args": ["--yolo"]},
            workdir="/w", title="title", prompt="prompt",
            resume_ref="session-1")

        self.assertEqual(fresh[:4], ["reasonix", "run", "--dir", "/w"])
        self.assertEqual(fresh[-1], "prompt")
        self.assertEqual(fresh[fresh.index("--permission-mode") + 1], "auto")
        self.assertEqual(fresh[fresh.index("--max-steps") + 1], "24")
        self.assertIn("--resume", resumed)
        self.assertNotIn("--permission-mode", resumed)

    def test_unknown_harness_is_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            self.build("gemini")


class AddDirsTests(unittest.TestCase):
    def test_add_dirs_follow_harness_capabilities(self) -> None:
        for backend in harnesses.SUPPORTED:
            with self.subTest(backend=backend), \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                cmd = runners.build_cmd(
                    {"name": backend, "backend": backend, "extra_args": [],
                     "add_dirs": ["/ref/repo"]},
                    workdir="/w", title="title", prompt="prompt")
                if backend in runners.ADD_DIR_BACKENDS:
                    self.assertEqual(cmd[cmd.index("--add-dir") + 1], "/ref/repo")
                else:
                    self.assertNotIn("--add-dir", cmd)
                    self.assertIn("no --add-dir", err.getvalue())


class BackendEnvTests(unittest.TestCase):
    def test_opencode_denies_native_delegation_unless_opted_in(self) -> None:
        env = {"KEEP": "1"}
        out = runners.apply_backend_env({"backend": "opencode"}, env)
        permissions = json.loads(out["OPENCODE_CONFIG_CONTENT"])["permission"]
        self.assertEqual(permissions["task"], "deny")
        self.assertEqual(permissions["team_spawn"], "deny")
        self.assertEqual(out["KEEP"], "1")
        opted = runners.apply_backend_env(
            {"backend": "opencode", "opencode_native_subagents": True}, env)
        # Native delegation restored — but the snapshot policy still rides
        # the config, so the env is rebuilt rather than returned as-is.
        self.assertNotIn("permission",
                         json.loads(opted["OPENCODE_CONFIG_CONTENT"]))

    def test_quota_lane_strips_credentials_without_mutating_input(self) -> None:
        env = {"ANTHROPIC_API_KEY": "key", "ANTHROPIC_AUTH_TOKEN": "token",
               "KEEP": "1"}
        out = runners.apply_backend_env(
            {"backend": "claude", "lane": "quota"}, env)
        self.assertEqual(out, {"KEEP": "1"})
        self.assertEqual(env["ANTHROPIC_API_KEY"], "key")
        for profile in ({"backend": "claude", "lane": "api"},
                        {"backend": "codex"}):
            with self.subTest(profile=profile):
                self.assertIs(runners.apply_backend_env(profile, env), env)

    def test_profile_env_points_a_base_url_and_leaves_other_vars(self) -> None:
        env = {"KEEP": "1", "ANTHROPIC_BASE_URL": "https://api.anthropic.com"}
        out = runners.apply_backend_env(
            {"backend": "claude",
             "env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8080"}},
            env)
        self.assertEqual(out["ANTHROPIC_BASE_URL"], "http://127.0.0.1:8080")
        self.assertEqual(out["KEEP"], "1")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://api.anthropic.com")
        stripped = runners.apply_backend_env(
            {"backend": "claude", "lane": "quota",
             "env": {"ANTHROPIC_API_KEY": "from-profile",
                     "ANTHROPIC_BASE_URL": "http://127.0.0.1:8080"}},
            {"ANTHROPIC_API_KEY": "from-process", "KEEP": "1"})
        self.assertNotIn("ANTHROPIC_API_KEY", stripped)
        self.assertEqual(stripped["ANTHROPIC_BASE_URL"], "http://127.0.0.1:8080")


class LogParsingTests(unittest.TestCase):
    def write_log(self, lines: list) -> tuple[tempfile.TemporaryDirectory, Path]:
        tmp = tempfile.TemporaryDirectory()
        log = Path(tmp.name) / "run.jsonl"
        log.write_text("".join(
            (line if isinstance(line, str) else json.dumps(line)) + "\n"
            for line in lines))
        return tmp, log

    def usage(self, backend: str, lines: list) -> dict:
        tmp, log = self.write_log(lines)
        try:
            return runners.parse_usage(str(log), backend)
        finally:
            tmp.cleanup()

    def test_session_and_last_text_are_best_effort(self) -> None:
        tmp, log = self.write_log([
            "not json",
            {"sessionID": "session-42"},
            {"part": {"text": "working"}},
            {"type": "result", "result": "done"},
        ])
        try:
            self.assertEqual(runners.parse_log(str(log)), ("session-42", "done"))
        finally:
            tmp.cleanup()
        self.assertEqual(runners.parse_log("/no/such/log.jsonl"), (None, None))

    def test_usage_contract_per_harness(self) -> None:
        cases = (
            ("claude", [
                {"type": "assistant", "message": {"usage": {
                    "input_tokens": 999, "output_tokens": 999}}},
                {"type": "result", "total_cost_usd": 0.1234567, "usage": {
                    "input_tokens": 9, "output_tokens": 6,
                    "cache_creation_input_tokens": 3,
                    "cache_read_input_tokens": 2}},
            ], {"tokens_in": 14, "tokens_out": 6, "tokens_total": 20,
                "cost_usd": 0.123457, "usage_source": "claude"}),
            ("codex", [
                {"type": "turn.completed", "usage": {
                    "input_tokens": 10, "output_tokens": 2}},
                {"type": "turn.completed", "usage": {
                    "input_tokens": 4, "output_tokens": 1, "total_tokens": 5}},
            ], {"tokens_in": 14, "tokens_out": 3, "tokens_total": 17,
                "cost_usd": None, "usage_source": "codex"}),
            ("opencode", [
                {"part": {"type": "step-finish", "cost": 0.1,
                          "tokens": {"total": 15, "input": 10, "output": 2,
                                     "cache": {"read": 3}}}},
                {"part": {"type": "step-finish", "cost": 0.2,
                          "tokens": {"total": 8, "input": 4, "output": 1,
                                     "cache": {"read": 3}}}},
            ], {"tokens_in": 20, "tokens_out": 3, "tokens_total": 23,
                "cost_usd": 0.3, "usage_source": "opencode"}),
            ("reasonix", [
                {"kind": "usage", "usage": {"promptTokens": 999}},
                {"type": "result", "total_cost": 0.0040127528,
                 "currency": "USD", "usage": {
                     "input_tokens": 10, "output_tokens": 2,
                     "cache_read_input_tokens": 8}},
            ], {"tokens_in": 10, "tokens_out": 2, "tokens_total": 12,
                "cost_usd": 0.004013, "usage_source": "reasonix"}),
            ("reasonix-eur", [
                {"type": "result", "total_cost": 12.5, "currency": "EUR",
                 "usage": {"input_tokens": 10, "output_tokens": 2}},
            ], {"tokens_in": 10, "tokens_out": 2, "tokens_total": 12,
                "cost_usd": None, "usage_source": "reasonix"}),
            ("codex-zero", [
                {"type": "turn.completed", "usage": {
                    "input_tokens": 0, "output_tokens": 0}},
            ], {"tokens_in": 0, "tokens_out": 0, "tokens_total": 0,
                "cost_usd": None, "usage_source": "codex"}),
        )
        for label, lines, expected in cases:
            backend = label.split("-", 1)[0]
            with self.subTest(case=label):
                self.assertEqual(self.usage(backend, lines), expected)

    def test_unrecognized_usage_degrades_to_null(self) -> None:
        cases = (
            ("claude", ["not json", {"type": "result", "usage": "lots"}]),
            ("codex", [{"type": "turn.completed",
                        "usage": {"input_tokens": None}}]),
            ("opencode", [{"part": {"type": "step-finish"}}]),
            ("reasonix", [{"type": "result"}]),
            ("gemini", [{"type": "result", "usage": {"input_tokens": 5}}]),
        )
        for backend, lines in cases:
            with self.subTest(backend=backend):
                self.assertEqual(self.usage(backend, lines), runners.EMPTY_USAGE)
        self.assertEqual(
            runners.parse_usage("/no/such/log.jsonl", "claude"),
            runners.EMPTY_USAGE)


class QuotaLaneTests(unittest.TestCase):
    def test_spent_quota_falls_back_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "run.jsonl"
            log.write_text("Claude AI usage limit reached\n")
            profile = {"backend": "claude", "lane": "quota", "name": "q"}
            env = {"ANTHROPIC_API_KEY": "key"}
            retry = runners.next_lane(profile, env, str(log), False)
            self.assertEqual(retry["lane"], "api")
            self.assertIsNone(runners.next_lane(retry, env, str(log), True))
            self.assertIsNone(runners.next_lane(profile, {}, str(log), False))

            log.write_text("Usage limit approaching. Checkpoint now.\n")
            self.assertIsNone(runners.next_lane(profile, env, str(log), False))
            runners.write_lane(str(log), "quota")
            self.assertEqual(json.loads(log.read_text().splitlines()[-1])["lane"],
                             "quota")


if __name__ == "__main__":
    unittest.main()


class OpencodeSnapshotTests(unittest.TestCase):
    def test_supervised_runs_disable_the_snapshot_store(self) -> None:
        """OpenCode clones the whole workdir into its undo store per session;
        an Orchestra worktree is transient, so that is a dead full copy
        (495GB of them by 2026-08-28)."""
        env = runners.apply_backend_env({"backend": "opencode"}, {})
        content = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        self.assertIs(content["snapshot"], False)
        # The escape hatch, and the caller's own explicit value, both win.
        kept = runners.apply_backend_env(
            {"backend": "opencode", "opencode_snapshots": True}, {})
        self.assertNotIn("snapshot",
                         json.loads(kept.get("OPENCODE_CONFIG_CONTENT", "{}")))
        explicit = runners.apply_backend_env(
            {"backend": "opencode"},
            {"OPENCODE_CONFIG_CONTENT": '{"snapshot": true}'})
        self.assertIs(json.loads(
            explicit["OPENCODE_CONFIG_CONTENT"])["snapshot"], True)
