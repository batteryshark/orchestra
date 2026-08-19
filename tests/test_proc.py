"""Process helpers: Unix process groups vs Windows taskkill / CreateProcess."""
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from dromond import proc


class WhichTests(unittest.TestCase):
    def test_which_finds_python(self) -> None:
        found = proc.which("python") or proc.which("python3")
        self.assertIsNotNone(found)
        self.assertTrue(Path(found).is_file())

    @unittest.skipUnless(sys.platform == "win32", "Windows launchable suffix")
    def test_windows_prefers_a_launchable_suffix(self) -> None:
        found = proc.which("opencode") or proc.which("claude")
        if found is None:
            self.skipTest("neither opencode nor claude on PATH")
        self.assertIn(Path(found).suffix.lower(), proc._WIN_LAUNCHABLE)


class AliveTests(unittest.TestCase):
    def test_this_process_is_alive(self) -> None:
        self.assertTrue(proc.alive(os.getpid()))

    def test_a_free_pid_is_not_alive(self) -> None:
        for pid in range(99000, 99999):
            if not proc.alive(pid):
                return
        self.skipTest("no free pid found")


class SessionKwargsTests(unittest.TestCase):
    def test_unix_uses_start_new_session(self) -> None:
        with mock.patch.object(proc, "IS_WIN", False):
            self.assertEqual(proc.session_kwargs(), {"start_new_session": True})

    def test_windows_uses_creationflags(self) -> None:
        with mock.patch.object(proc, "IS_WIN", True):
            flags = proc.session_kwargs()["creationflags"]
            self.assertTrue(flags & subprocess.CREATE_NEW_PROCESS_GROUP)
            detached = proc.session_kwargs(detached=True)["creationflags"]
            self.assertTrue(detached & subprocess.CREATE_NEW_PROCESS_GROUP)
            self.assertTrue(detached & subprocess.DETACHED_PROCESS)


class EnrichPathTests(unittest.TestCase):
    def test_existing_path_is_kept_and_login_dirs_prepended(self) -> None:
        env = proc.enrich_path({"PATH": "already-there"})
        self.assertTrue(env["PATH"].endswith("already-there")
                        or "already-there" in env["PATH"].split(os.pathsep))


class HarvestTests(unittest.TestCase):
    def test_windows_harvest_is_zero(self) -> None:
        with mock.patch.object(proc, "IS_WIN", True):
            self.assertEqual(proc.harvest_children(), 0)


if __name__ == "__main__":
    unittest.main()
