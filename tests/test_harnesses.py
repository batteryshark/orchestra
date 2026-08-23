"""One guard for the static supported-harness contract."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestra import (acp, harnesses, hooks, profile_edit, profiles, runners,
                         traces, worktree)


class HarnessContractTests(unittest.TestCase):
    def test_capabilities_match_every_full_integration_surface(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            discovered = profiles.discover(
                runner=lambda cmd: (
                    '{"models": []}' if cmd[0] == "codex" else "", None),
                reasonix_config=Path(tmp) / "missing.toml")

        self.assertEqual(
            {name: facts["discovery"]
             for name, facts in harnesses.CAPABILITIES.items()},
            {"claude": "manual", "codex": "cli", "opencode": "cli",
             "reasonix": "config"})
        self.assertEqual(
            {name: facts["correction"]
             for name, facts in harnesses.CAPABILITIES.items()},
            {"claude": "nested-hook", "codex": "nested-hook",
             "opencode": "plugin", "reasonix": "flat-hook"})

        launches = {}
        resumes = {}
        acp_commands = {}
        corrections = {}
        for name, facts in harnesses.CAPABILITIES.items():
            profile = {"name": name, "backend": name, "extra_args": []}
            self.assertFalse(set(facts["transport"]) - {"exec", "acp"}, name)
            if "exec" in facts["transport"]:
                fresh = runners.build_cmd(
                    profile, workdir="/w", title="t", prompt="p")
                resumed = runners.build_cmd(
                    profile, workdir="/w", title="t", prompt="p",
                    resume_ref="session-1")
                self.assertEqual(fresh[0], name)
                launches[name] = fresh
                if resumed != fresh:
                    resumes[name] = resumed
            if "acp" in facts["transport"]:
                acp_commands[name] = acp.build_acp_cmd(profile)
                self.assertEqual(acp_commands[name], [name, "acp"])
            else:
                with self.assertRaises(SystemExit, msg=name):
                    acp.build_acp_cmd(profile)

            mode = facts["correction"]
            if mode == "plugin":
                if f"--backend {name}" in hooks.OPENCODE_PLUGIN:
                    corrections[name] = mode
            elif hooks._handlers(hooks._entry(name)):
                corrections[name] = mode

        fields = {"discovery", "launch", "resume", "trace", "correction",
                  "usage", "add_directory", "transport"}
        for name, facts in harnesses.CAPABILITIES.items():
            self.assertEqual(set(facts), fields, name)
        for capability, registered in {
            "discovery": discovered,
            "launch": launches,
            "resume": resumes,
            "trace": traces.PARSERS,
            "correction": corrections,
            "usage": runners.USAGE_PARSERS,
        }.items():
            self.assertEqual(set(registered),
                             set(harnesses.supporting(capability)), capability)
        self.assertEqual(set(profile_edit.picker_options(discovered)),
                         set(harnesses.supporting("discovery")))
        self.assertEqual(set(runners.ADD_DIR_BACKENDS),
                         set(harnesses.supporting("add_directory")))
        self.assertEqual(set(launches),
                         set(harnesses.supporting("transport", "exec")))
        self.assertEqual(set(acp_commands),
                         set(harnesses.supporting("transport", "acp")))
        self.assertEqual(set(acp.ACP_BACKENDS),
                         set(harnesses.supporting("transport", "acp")))
        self.assertEqual(set(worktree.BACKEND_DIRS), set(harnesses.SUPPORTED))
        with mock.patch.object(hooks, "install_file", return_value="ok") as files, \
                mock.patch.object(hooks, "install_opencode_plugin",
                                  return_value="ok") as plugin, \
                mock.patch.object(hooks, "provision_codex_trust",
                                  return_value="ok"):
            hooks.install_all()
        installed = {call.args[1] for call in files.call_args_list}
        if plugin.called:
            installed.add("opencode")
        self.assertEqual(installed, set(harnesses.supporting("correction")))


if __name__ == "__main__":
    unittest.main()
