"""Test-suite safety net: no test may touch the developer's real state.

State is central now (DESIGN §2), so a test that forgets to override
``ORCHESTRA_HOME`` could write into the user's live ``~/.orchestra`` or touch
service and legacy-import paths. Point every filesystem boundary at a
throwaway directory before any test module imports ``orchestra``. Individual
tests still set their own per-test home.
"""
import atexit
import os
import shutil
import tempfile

# Nothing the launching shell exported may reach a test. Every ORCHESTRA_*
# goes, and the sandbox below puts back the only ones a test is allowed to
# see. In particular, a supervised test run inherits run identity and its
# CLI calls would otherwise be authorized as the worker instead of the test:
#
#   ORCHESTRA_RUN_ID / _RUN_TOKEN / _ROOT are a run's identity. The suite is
#   often executed by a supervised run, so inheriting those values would make
#   test CLI calls authenticate as that worker instead of the test device.
for _leaked in [k for k in os.environ if k.startswith("ORCHESTRA_")]:
    del os.environ[_leaked]

_SANDBOX = tempfile.mkdtemp(prefix="orchestra-tests-")
os.environ["ORCHESTRA_HOME"] = os.path.join(_SANDBOX, "home")
os.environ["ORCHESTRA_CONFIG"] = os.path.join(_SANDBOX, "config.toml")
os.environ["ORCHESTRA_LAUNCH_AGENTS"] = os.path.join(_SANDBOX, "LaunchAgents")
# The same rule covers harness homes read by discovery and the legacy archive:
# no test can touch the developer's real ~/.claude, ~/.codex, or ~/.reasonix.
os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(_SANDBOX, "claude")
os.environ["CODEX_HOME"] = os.path.join(_SANDBOX, "codex")
os.environ["REASONIX_HOME"] = os.path.join(_SANDBOX, "reasonix")
atexit.register(shutil.rmtree, _SANDBOX, ignore_errors=True)
