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

# NOTHING the launching shell exported may reach a test. Every MAESTRO_* and
# every DROMOND_* goes, and the sandbox below puts back the only ones a test
# is allowed to see. Three separate incidents, one cause:
#
#   MAESTRO_*  trips the deprecation shim, and decides the W-0188 adoption
#              guard for modules that never asked about it.
#   DROMOND_RUN_ID / _RUN_TOKEN / _ROOT  are a RUN's identity. The suite is
#              often executed BY a supervised run, which exports these into
#              its own shell, and cli._authority() reads DROMOND_RUN_ID to
#              decide whether a caller is an agent -- so `dromond profiles
#              set` inside a test suddenly needed a Work decision, and four
#              tests failed for nobody's fault but the shell they were
#              launched from (I-0008, I-0009).
#   DROMOND_NOD_* / DROMOND_KEY  are LIVE CREDENTIALS. nod.load_secrets lets
#              env win over the secrets file -- even over an explicit
#              `secrets_file =` in a test's own config -- so a shell holding
#              the human's real DROMOND_NOD_BASE_URL pointed every
#              nod-enabled test at the live host instead of its FakeNod:
#              cards filed for real, and each call blocking on the network
#              (15s per request, 60s per await_answer long-poll chunk) until
#              the suite looked hung. That is I-0074, and it reproduces
#              exactly: test_conductor 23.7s/OK becomes 38.7s/FAILED on
#              test_a_card_is_filed_and_mirrored the moment the vars are set.
#
# A prefix sweep rather than a list: the list is what let DROMOND_NOD_* in.
for _leaked in [k for k in os.environ
                if k.startswith(("MAESTRO_", "DROMOND_"))]:
    del os.environ[_leaked]

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
