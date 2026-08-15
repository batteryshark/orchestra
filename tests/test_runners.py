import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from dromond import runners


class BuildCmdTests(unittest.TestCase):
    def test_opencode_fresh_and_resume(self) -> None:
        profile = {"name": "oc", "backend": "opencode", "model": "prov/m",
                   "variant": "max", "extra_args": []}
        cmd = runners.build_cmd(profile, workdir="/w", title="t", prompt="do it")
        self.assertEqual(cmd[:4], ["opencode", "run", "--dir", "/w"])
        self.assertIn("--title", cmd)
        self.assertIn("prov/m", cmd)
        self.assertIn("--variant", cmd)
        self.assertEqual(cmd[-1], "do it")
        resumed = runners.build_cmd(profile, workdir="/w", title="t", prompt="p",
                                    resume_ref="sess-1")
        self.assertIn("--session", resumed)
        self.assertNotIn("--title", resumed)

    def test_codex_flags_and_resume_ordering(self) -> None:
        profile = {"name": "cx", "backend": "codex", "model": "gpt-x",
                   "effort": "high", "sandbox": "danger-full-access",
                   "extra_args": ["--extra"]}
        cmd = runners.build_cmd(profile, workdir="/w", title="t", prompt="p")
        self.assertEqual(cmd[:2], ["codex", "exec"])
        self.assertEqual(cmd[cmd.index("--sandbox") + 1], "danger-full-access")
        self.assertIn('model_reasoning_effort="high"', cmd)
        self.assertIn("--extra", cmd)
        self.assertEqual(cmd[-1], "p")
        resumed = runners.build_cmd(profile, workdir="/w", title="t", prompt="p",
                                    resume_ref="ref-9")
        # shared flags stay before the resume subcommand
        self.assertLess(resumed.index("--sandbox"), resumed.index("resume"))
        self.assertEqual(resumed[-2:], ["ref-9", "p"])

    def test_codex_asks_for_reasoning_summaries(self) -> None:
        """Without this, codex emits ZERO reasoning items — measured against
        codex-cli 0.147.0, same prompt and effort, 0 events vs 3-5."""
        cmd = runners.build_cmd({"name": "cx", "backend": "codex", "extra_args": []},
                                workdir="/w", title="t", prompt="p")
        self.assertIn('model_reasoning_summary="detailed"', cmd)
        self.assertEqual(cmd[cmd.index('model_reasoning_summary="detailed"') - 1], "-c")
        # a profile that names its own value keeps it, and gets it only once
        override = runners.build_cmd(
            {"name": "cx", "backend": "codex",
             "extra_args": ["-c", 'model_reasoning_summary="concise"']},
            workdir="/w", title="t", prompt="p")
        self.assertIn('model_reasoning_summary="concise"', override)
        self.assertNotIn('model_reasoning_summary="detailed"', override)

    def test_codex_default_sandbox(self) -> None:
        profile = {"name": "cx", "backend": "codex", "extra_args": []}
        cmd = runners.build_cmd(profile, workdir="/w", title="t", prompt="p")
        self.assertEqual(cmd[cmd.index("--sandbox") + 1], "workspace-write")

    def test_claude_prompt_and_default_permissions(self) -> None:
        profile = {"name": "cl", "backend": "claude", "model": "claude-x",
                   "effort": "medium", "extra_args": []}
        cmd = runners.build_cmd(profile, workdir="/w", title="t", prompt="the prompt",
                                resume_ref="sess-2")
        self.assertEqual(cmd[:3], ["claude", "-p", "the prompt"])
        self.assertIn("--resume", cmd)
        self.assertIn("--model", cmd)
        self.assertIn("--effort", cmd)
        self.assertIn("--permission-mode", cmd)  # default extra_args applied
        explicit = runners.build_cmd(
            {"name": "cl", "backend": "claude", "extra_args": ["--allowedTools", "Read"]},
            workdir="/w", title="t", prompt="p")
        self.assertNotIn("--permission-mode", explicit)

    def test_reasonix_fresh_and_resume(self) -> None:
        profile = {"name": "rx", "backend": "reasonix", "model": "deepseek-v3.2",
                   "effort": "high", "max_steps": 24,
                   "extra_args": ["--permission-mode", "acceptEdits"]}
        fresh = runners.build_cmd(
            profile, workdir="/w", title="t", prompt="do it")
        self.assertEqual(fresh, [
            "reasonix", "run", "--dir", "/w",
            "--output-format", "stream-json",
            "--model", "deepseek-v3.2", "--effort", "high",
            "--max-steps", "24", "--permission-mode", "acceptEdits", "do it",
        ])

        resumed = runners.build_cmd(
            profile, workdir="/w", title="t", prompt="continue",
            resume_ref="sess-3")
        self.assertEqual(
            resumed[resumed.index("--resume"):resumed.index("--resume") + 2],
            ["--resume", "sess-3"])
        self.assertEqual(resumed[-1], "continue")

    def test_unknown_backend_exits(self) -> None:
        with self.assertRaises(SystemExit):
            runners.build_cmd({"name": "x", "backend": "gemini", "extra_args": []},
                              workdir="/w", title="t", prompt="p")


class AddDirsTests(unittest.TestCase):
    """DESIGN §12: extra directories are declared, never discovered."""

    def test_declared_directory_reaches_the_backends_that_accept_it(self) -> None:
        for backend in runners.ADD_DIR_BACKENDS:
            with self.subTest(backend=backend):
                cmd = runners.build_cmd(
                    {"name": "p", "backend": backend, "extra_args": [],
                     "add_dirs": ["/ref/repo"]},
                    workdir="/w", title="t", prompt="p")
                self.assertEqual(cmd[cmd.index("--add-dir") + 1], "/ref/repo")
                self.assertNotIn("/other/repo", cmd)

    def test_opencode_never_gets_add_dir(self) -> None:
        """`opencode run` has no such flag: passing it kills the run."""
        with contextlib.redirect_stderr(io.StringIO()) as err:
            cmd = runners.build_cmd(
                {"name": "oc", "backend": "opencode", "extra_args": [],
                 "add_dirs": ["/ref/repo"]},
                workdir="/w", title="t", prompt="p")
        self.assertNotIn("--add-dir", cmd)
        self.assertNotIn("/ref/repo", cmd)
        self.assertIn("no --add-dir", err.getvalue())  # a dropped grant is visible

    def test_nothing_is_passed_when_nothing_is_declared(self) -> None:
        cmd = runners.build_cmd({"name": "p", "backend": "codex", "extra_args": []},
                                workdir="/w", title="t", prompt="p")
        self.assertNotIn("--add-dir", cmd)

    def test_the_prompt_stays_last(self) -> None:
        for backend in ("codex", "reasonix"):
            cmd = runners.build_cmd(
                {"name": "p", "backend": backend, "extra_args": [],
                 "add_dirs": ["/ref/repo"]},
                workdir="/w", title="t", prompt="the prompt")
            self.assertEqual(cmd[-1], "the prompt", backend)


class ApplyBackendEnvTests(unittest.TestCase):
    def test_opencode_delegation_denied(self) -> None:
        env = runners.apply_backend_env({"backend": "opencode"}, {})
        content = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(content["permission"]["task"], "deny")
        self.assertEqual(content["permission"]["team_spawn"], "deny")

    def test_other_backends_untouched(self) -> None:
        env = {"A": "1"}
        self.assertIs(runners.apply_backend_env({"backend": "codex"}, env), env)

    def test_native_subagents_opt_out(self) -> None:
        env = {"A": "1"}
        profile = {"backend": "opencode", "opencode_native_subagents": True}
        self.assertIs(runners.apply_backend_env(profile, env), env)


class ParseLogTests(unittest.TestCase):
    def test_session_ref_and_last_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "run.jsonl"
            log.write_text(
                "not json\n"
                + json.dumps({"sessionID": "sess-42"}) + "\n"
                + json.dumps({"part": {"text": "working on it"}}) + "\n"
                + json.dumps({"type": "result", "result": "all done"}) + "\n")
            session, last = runners.parse_log(str(log))
            self.assertEqual(session, "sess-42")
            self.assertEqual(last, "all done")

    def test_missing_file_is_tolerated(self) -> None:
        self.assertEqual(runners.parse_log("/nope/none.jsonl"), (None, None))


class ParseUsageTests(unittest.TestCase):
    """DESIGN §11 token/cost capture. Every fixture below is the shape of a
    real transcript (reasonix + codex: dromond's own logs; claude: `claude -p
    --output-format stream-json`; opencode: an orchestra worker log)."""

    def _usage(self, backend: str, lines: list) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "run.jsonl"
            log.write_text("".join(
                (line if isinstance(line, str) else json.dumps(line)) + "\n"
                for line in lines))
            return runners.parse_usage(str(log), backend)

    def test_reasonix_result_event_is_not_double_counted(self) -> None:
        """input_tokens ALREADY includes cache read + creation (94976 +
        19719 == 114695), and the per-turn `kind: usage` lines repeat the
        same tokens — only the result event counts."""
        usage = self._usage("reasonix", [
            {"kind": "usage", "usage": {"promptTokens": 15183, "completionTokens": 141,
                                        "totalTokens": 15324, "cacheHitTokens": 0,
                                        "cacheMissTokens": 15183}},
            {"kind": "usage", "usage": {"promptTokens": 22244, "completionTokens": 642,
                                        "totalTokens": 22886, "cacheHitTokens": 22144,
                                        "cacheMissTokens": 100}},
            {"type": "result", "subtype": "success", "total_cost": 0.0040127528,
             "currency": "USD", "total_cost_usd": 0.0040127528,
             "usage": {"input_tokens": 114695, "output_tokens": 3522,
                       "cache_read_input_tokens": 94976,
                       "cache_creation_input_tokens": 19719}},
        ])
        self.assertEqual(usage["tokens_in"], 114695)
        self.assertEqual(usage["tokens_out"], 3522)
        self.assertEqual(usage["tokens_total"], 118217)
        self.assertEqual(usage["cost_usd"], 0.004013)
        self.assertEqual(usage["usage_source"], "reasonix")

    def test_reasonix_non_usd_cost_stays_null(self) -> None:
        usage = self._usage("reasonix", [
            {"type": "result", "total_cost": 12.5, "currency": "EUR",
             "usage": {"input_tokens": 10, "output_tokens": 2}},
        ])
        self.assertEqual(usage["tokens_total"], 12)
        self.assertIsNone(usage["cost_usd"])

    def test_claude_counts_the_result_event_only(self) -> None:
        """Assistant messages repeat their usage, so summing them would
        double count; input_tokens EXCLUDES cache tokens here."""
        usage = self._usage("claude", [
            {"type": "assistant", "message": {"usage": {
                "input_tokens": 9, "output_tokens": 4,
                "cache_creation_input_tokens": 12948,
                "cache_read_input_tokens": 21623}}},
            {"type": "result", "is_error": False, "total_cost_usd": 0.0291973,
             "usage": {"input_tokens": 9, "output_tokens": 226,
                       "cache_creation_input_tokens": 12948,
                       "cache_read_input_tokens": 21623}},
        ])
        self.assertEqual(usage["tokens_in"], 34580)   # 9 + 12948 + 21623
        self.assertEqual(usage["tokens_out"], 226)
        self.assertEqual(usage["tokens_total"], 34806)
        self.assertEqual(usage["cost_usd"], 0.029197)  # stored to 6 decimals

    def test_codex_sums_turns_and_reports_no_cost(self) -> None:
        """A resumed run writes one turn.completed per process invocation;
        codex prices nothing, so cost stays null beside real tokens."""
        usage = self._usage("codex", [
            {"type": "turn.completed", "usage": {
                "input_tokens": 675250, "cached_input_tokens": 605696,
                "cache_write_input_tokens": 0, "output_tokens": 4596,
                "reasoning_output_tokens": 1551}},
            {"type": "turn.completed", "usage": {
                "input_tokens": 1000, "cached_input_tokens": 0,
                "output_tokens": 100, "total_tokens": 1100}},
        ])
        self.assertEqual(usage["tokens_in"], 676250)
        self.assertEqual(usage["tokens_out"], 4696)
        self.assertEqual(usage["tokens_total"], 680946)
        self.assertIsNone(usage["cost_usd"])
        self.assertEqual(usage["usage_source"], "codex")

    def test_opencode_sums_every_step(self) -> None:
        usage = self._usage("opencode", [
            {"type": "step_finish", "part": {
                "type": "step-finish", "cost": 0.0125,
                "tokens": {"total": 11226, "input": 11093, "output": 133,
                           "reasoning": 0, "cache": {"write": 0, "read": 0}}}},
            {"type": "step_finish", "part": {
                "type": "step-finish", "cost": 0.02,
                "tokens": {"total": 175825, "input": 472, "output": 249,
                           "reasoning": 0, "cache": {"write": 0, "read": 175104}}}},
        ])
        self.assertEqual(usage["tokens_in"], 186669)   # 11093 + 472 + 175104
        self.assertEqual(usage["tokens_out"], 382)
        self.assertEqual(usage["tokens_total"], 187051)
        self.assertEqual(usage["cost_usd"], 0.0325)

    def test_unparseable_or_absent_usage_yields_null_not_a_raise(self) -> None:
        """The contract: null, never a crash and never a guess."""
        for backend, lines in (
                ("claude", ["not json", "{bad", {"type": "assistant"}]),
                ("claude", [{"type": "result", "usage": "lots"}]),
                ("codex", [{"type": "turn.completed", "usage": {"input_tokens": None}}]),
                ("opencode", [{"type": "step_finish", "part": {"type": "step-finish"}}]),
                ("reasonix", [{"type": "result"}]),
                ("gemini", [{"type": "result", "usage": {"input_tokens": 5}}]),
        ):
            with self.subTest(backend=backend, lines=lines):
                self.assertEqual(self._usage(backend, lines), runners.EMPTY_USAGE)
        self.assertEqual(runners.parse_usage("/nope/none.jsonl", "claude"),
                         runners.EMPTY_USAGE)

    def test_a_zero_usage_event_is_a_number_not_a_null(self) -> None:
        usage = self._usage("codex", [
            {"type": "turn.completed", "usage": {"input_tokens": 0, "output_tokens": 0}}])
        self.assertEqual(usage["tokens_total"], 0)
        self.assertEqual(usage["usage_source"], "codex")


if __name__ == "__main__":
    unittest.main()


class ReasonixPermissionTestCase(unittest.TestCase):
    def test_supervised_run_sets_a_permission_posture(self) -> None:
        """Without one, every write is declined and the worker stops empty."""
        cmd = runners.build_cmd({"name": "r", "backend": "reasonix"},
                                workdir="/tmp/x", title="t", prompt="do it")
        self.assertIn("--permission-mode", cmd)
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "auto")

    def test_profile_can_override_the_posture(self) -> None:
        cmd = runners.build_cmd(
            {"name": "r", "backend": "reasonix", "extra_args": ["--yolo"]},
            workdir="/tmp/x", title="t", prompt="do it")
        self.assertNotIn("--permission-mode", cmd)
