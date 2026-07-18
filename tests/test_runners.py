import unittest

from orchestra_cli.runners import build_cmd


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


if __name__ == "__main__":
    unittest.main()
