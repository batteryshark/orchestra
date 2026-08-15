"""Test-suite safety net: no test may touch the developer's real state.

State is central now (DESIGN §2), so a test that forgets to override
``DROMOND_HOME`` would write into the user's live ``~/.dromond``, load a
real LaunchAgent, or rewrite ``~/.config/dromond/config.toml``. Point all
three overrides at a throwaway directory before any test module imports
``dromond``. Individual tests still set their own per-test home.

Setting ``DROMOND_HOME`` here also switches off the W-0188 legacy adoption
for the whole suite: ``paths.adopt_legacy_home`` moves ``~/.maestro`` only
when the location is NOT overridden, so no test can relocate the developer's
live Maestro state by accident. ``tests/test_paths_migration.py`` is the one
module that turns the overrides off, and it moves ``HOME`` to a temporary
directory first so ``~`` itself is a sandbox.
"""
import atexit
import os
import shutil
import tempfile

# Any MAESTRO_* left in the developer's shell must not reach a test: it would
# trip the deprecation shim, and it would decide the adoption guard for
# modules that never asked about it.
for _stale in [k for k in os.environ if k.startswith("MAESTRO_")]:
    del os.environ[_stale]

# Nor may a RUN's identity. The suite is often executed BY a supervised run,
# which exports these into its own shell, and cli._authority() reads
# DROMOND_RUN_ID to decide whether a caller is an agent -- so `dromond
# profiles set` inside a test suddenly needed a Work decision, and four tests
# failed for nobody's fault but the shell they were launched from (I-0008,
# I-0009). A test's authority is the test's own business.
for _inherited in ("DROMOND_RUN_ID", "DROMOND_RUN_TOKEN", "DROMOND_ROOT"):
    os.environ.pop(_inherited, None)

_SANDBOX = tempfile.mkdtemp(prefix="dromond-tests-")
os.environ["DROMOND_HOME"] = os.path.join(_SANDBOX, "home")
os.environ["DROMOND_CONFIG"] = os.path.join(_SANDBOX, "config.toml")
os.environ["DROMOND_LAUNCH_AGENTS"] = os.path.join(_SANDBOX, "LaunchAgents")
# Same rule for the harness homes `dromond init` installs hooks into (§6):
# each harness's own override, pointed at the sandbox, so no test can write
# into the developer's real ~/.claude, ~/.codex or ~/.reasonix.
os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(_SANDBOX, "claude")
os.environ["CODEX_HOME"] = os.path.join(_SANDBOX, "codex")
os.environ["REASONIX_HOME"] = os.path.join(_SANDBOX, "reasonix")
# Never the real Nod credentials: without this a test that enables [nod]
# reads the human's tokens and files against the live host.
os.environ["DROMOND_NOD_SECRETS_FILE"] = os.path.join(_SANDBOX, "nod-secrets.env")
atexit.register(shutil.rmtree, _SANDBOX, ignore_errors=True)
