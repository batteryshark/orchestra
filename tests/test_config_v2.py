import json
import os
import tempfile
import unittest
from pathlib import Path

from orchestra import config


class BootstrapTests(unittest.TestCase):
    def test_bootstrap_is_small_json_and_rejects_unknown_policy(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "bootstrap.json"
            config.write({"bind": "127.0.0.1", "port": 9000,
                          "callback_command": ["notify"],
                          "secret_file": str(Path(raw) / "secrets.json")}, path)
            self.assertEqual(config.read(path)["port"], 9000)
            with self.assertRaisesRegex(config.ConfigError, "routing"):
                config.write({"routing": "smart"}, path)

    @unittest.skipUnless(os.name == "posix", "POSIX mode contract")
    def test_secret_file_requires_owner_only_mode(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "secrets.json"
            path.write_text(json.dumps({"AGENT_TOKEN": "secret"}))
            path.chmod(0o644)
            with self.assertRaisesRegex(config.ConfigError, "0600"):
                config.secret_environment(path)
            path.chmod(0o600)
            self.assertEqual(config.secret_environment(path)["AGENT_TOKEN"], "secret")


if __name__ == "__main__":
    unittest.main()
