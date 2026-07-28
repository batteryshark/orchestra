from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestra_cli import cancel, config


class LinuxEnvironmentBoundaryTests(unittest.TestCase):
    def test_linux_passthrough_never_invokes_launchctl(self) -> None:
        cfg = {"settings": {"env_passthrough": ["EXAMPLE_TOKEN"]}}
        supplied = {"PATH": "/usr/bin"}
        with mock.patch.object(config.sys, "platform", "linux"), \
                mock.patch("subprocess.run",
                           side_effect=AssertionError("launchctl must stay macOS-only")):
            result = config.apply_env_passthrough(cfg, supplied)
        self.assertIs(result, supplied)
        self.assertNotIn("EXAMPLE_TOKEN", result)


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux integration coverage")
class LinuxIntegrationTests(unittest.TestCase):
    def test_cli_init_and_database_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            project = base / "project"
            project.mkdir()
            environment = dict(
                os.environ,
                HOME=str(base / "home"),
                ORCHESTRA_CONFIG=str(base / "home" / ".config" / "orchestra" / "config.toml"),
                ORCHESTRA_PROJECTS_FILE=str(base / "home" / ".config" / "orchestra" / "projects.json"),
            )
            repository_root = Path(__file__).resolve().parent.parent
            existing_pythonpath = environment.get("PYTHONPATH")
            environment["PYTHONPATH"] = os.pathsep.join(
                part for part in (str(repository_root), existing_pythonpath) if part
            )
            init = subprocess.run(
                [sys.executable, "-m", "orchestra_cli", "init"],
                cwd=project, env=environment, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(init.returncode, 0, init.stderr or init.stdout)
            self.assertTrue((project / ".orchestra" / "orchestra.db").is_file())
            self.assertTrue((project / "ORCHESTRA.md").is_file())

            roster = subprocess.run(
                [sys.executable, "-m", "orchestra_cli", "roster"],
                cwd=project, env=environment, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(roster.returncode, 0, roster.stderr or roster.stdout)
            self.assertIn("opencode", roster.stdout)

            runs = subprocess.run(
                [sys.executable, "-m", "orchestra_cli", "runs", "--json"],
                cwd=project, env=environment, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(runs.returncode, 0, runs.stderr or runs.stdout)
            self.assertEqual(json.loads(runs.stdout), [])

    def test_real_posix_worker_group_can_be_terminated(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            self.assertEqual(os.getpgid(proc.pid), proc.pid)
            sent, reason = cancel._signal_process_group(proc.pid)
            self.assertTrue(sent)
            self.assertEqual(reason, "sigterm_sent")
            self.assertEqual(proc.wait(timeout=10), -signal.SIGTERM)
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
