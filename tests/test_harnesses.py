"""One cross-module guard for the static supported-harness registry."""
import tempfile
import unittest
from pathlib import Path

from orchestra import acp, harnesses, profiles, runners, traces, worktree


class HarnessContractTests(unittest.TestCase):
    def test_each_harness_is_registered_on_its_public_surfaces(self) -> None:
        fields = {"discovery", "launch", "resume", "trace", "usage",
                  "add_directory", "transport"}
        self.assertEqual(set(harnesses.SUPPORTED), set(harnesses.CAPABILITIES))

        for name in harnesses.SUPPORTED:
            with self.subTest(harness=name):
                facts = harnesses.CAPABILITIES[name]
                self.assertEqual(set(facts), fields)
                cmd = runners.build_cmd(
                    {"name": name, "backend": name, "extra_args": []},
                    workdir="/w", title="title", prompt="prompt")
                self.assertEqual(cmd[0], name)
                self.assertEqual(name in acp.ACP_BACKENDS,
                                 "acp" in facts["transport"])
                if "acp" in facts["transport"]:
                    self.assertEqual(acp.build_acp_cmd(
                        {"name": name, "backend": name}), [name, "acp"])

        with tempfile.TemporaryDirectory() as directory:
            def catalog(command):
                if command[0] == "opencode":
                    return "provider/model\n", None
                return '{"models": []}', None
            discovered = profiles.discover(
                runner=catalog, reasonix_config=Path(directory) / "missing")
        surfaces = {"discovery": discovered, "trace": traces.PARSERS,
                    "usage": runners.USAGE_PARSERS}
        for capability, registered in surfaces.items():
            with self.subTest(capability=capability):
                self.assertEqual(set(registered),
                                 set(harnesses.supporting(capability)))
        self.assertEqual(set(runners.ADD_DIR_BACKENDS),
                         set(harnesses.supporting("add_directory")))
        self.assertEqual(set(worktree.BACKEND_DIRS), set(harnesses.SUPPORTED))


if __name__ == "__main__":
    unittest.main()
