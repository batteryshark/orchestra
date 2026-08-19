"""Process helpers: Unix process groups vs Windows taskkill / CreateProcess."""
import os
import signal
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
        # Reads the flags from proc, not subprocess: CPython defines them only
        # on Windows, so asserting against subprocess made this test impossible
        # to run on the machine it was written on.
        with mock.patch.object(proc, "IS_WIN", True):
            flags = proc.session_kwargs()["creationflags"]
            self.assertTrue(flags & proc.CREATE_NEW_PROCESS_GROUP)
            detached = proc.session_kwargs(detached=True)["creationflags"]
            self.assertTrue(detached & proc.CREATE_NEW_PROCESS_GROUP)
            self.assertTrue(detached & proc.DETACHED_PROCESS)
            self.assertTrue(detached & proc.CREATE_NO_WINDOW)

    def test_the_named_flags_match_win32(self) -> None:
        # If CPython is on Windows its constants are authoritative; the fixed
        # values above must agree with them.
        for name in ("CREATE_NEW_PROCESS_GROUP", "DETACHED_PROCESS",
                     "CREATE_NO_WINDOW"):
            real = getattr(subprocess, name, None)
            if real is not None:
                self.assertEqual(real, getattr(proc, name), name)


class TerminateGroupTests(unittest.TestCase):
    """The force path is the whole safety net for a worker that ignores
    SIGTERM: without it the timeout escalation asked politely a second time
    and the run never died."""

    def test_polite_by_default(self) -> None:
        sent = []
        with mock.patch.object(proc, "IS_WIN", False), \
             mock.patch.object(proc.os, "killpg", lambda p, s: sent.append(s)):
            proc.terminate_group(4242)
        self.assertEqual([signal.SIGTERM], sent)

    def test_force_skips_straight_to_sigkill(self) -> None:
        sent = []
        with mock.patch.object(proc, "IS_WIN", False), \
             mock.patch.object(proc.os, "killpg", lambda p, s: sent.append(s)):
            proc.terminate_group(4242, force=True)
        self.assertEqual([signal.SIGKILL], sent, "a forced stop must not ask nicely")

    def test_a_gone_process_is_not_an_error(self) -> None:
        def gone(pid, sig):
            raise ProcessLookupError
        with mock.patch.object(proc, "IS_WIN", False), \
             mock.patch.object(proc.os, "killpg", gone):
            proc.terminate_group(4242)
            proc.terminate_group(4242, force=True)

    def test_windows_taskkill_is_already_forceful(self) -> None:
        calls = []
        with mock.patch.object(proc, "IS_WIN", True), \
             mock.patch.object(proc.subprocess, "run",
                               lambda *a, **k: calls.append(a[0])):
            proc.terminate_group(4242, force=True)
        self.assertIn("/F", calls[0])
        self.assertIn("/T", calls[0])


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
