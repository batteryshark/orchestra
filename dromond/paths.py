"""Where Dromond keeps its state: one central ``~/.dromond``, never per project.

DESIGN §2 — the database, briefs, logs, and worktrees all live under
``~/.dromond`` (``DROMOND_HOME`` overrides it, for tests and for a second
daemon serving a separate workspace). Projects get no state directory of
their own, so there is nothing to gitignore per repo and cross-project
questions are one query.

W-0188 renamed the product from Maestro to Dromond, so this module also owns
the state migration: the names and the directory they point at are one
decision, and splitting them would let the code say ``dromond`` while the
data stayed at ``~/.maestro``. See ``adopt_legacy_home`` and ``env`` below.
"""
import os
import re
import sys
from pathlib import Path

# Pre-W-0163 per-project state, which `dromond migrate` reads. Written when the
# product was still called Maestro, so these two names are HISTORY: they name a
# directory already on disk, not anything Dromond creates. They do not follow
# the rename (W-0188), and the pair must agree, so it is stated once here.
LEGACY_STATE_DIR = ".maestro"
LEGACY_STATE_DB = "maestro.db"


# --- the W-0188 rename ------------------------------------------------------
# Maestro became Dromond. Two kinds of compatibility live here, and they are
# separate mechanisms with separate lifetimes:
#
#   ``env`` reads a DROMOND_* variable, honouring its MAESTRO_* predecessor for
#   ONE release with a deprecation line. It is a shim, and it is meant to go.
#
#   ``adopt_legacy_home`` moves ``~/.maestro`` to ``~/.dromond`` exactly once,
#   the first time a Dromond that has no state runs on a machine that has
#   Maestro's. It is a MOVE, never a copy and never a merge: one rename, so
#   there is never a moment with two databases and no answer to which is live.

_DEPRECATED_SEEN: set[str] = set()


def _say(message: str) -> None:
    """One line, on stderr, at most once per process per message.

    stderr because stdout is parsed: several commands emit JSON, and a
    migration notice on stdout would corrupt it.
    """
    if message not in _DEPRECATED_SEEN:
        _DEPRECATED_SEEN.add(message)
        print(message, file=sys.stderr, flush=True)


def env(name: str, default: str = "") -> str:
    """A ``DROMOND_*`` variable, falling back to its ``MAESTRO_*`` predecessor.

    DROMOND_* always wins, including when it is set to the empty string: an
    explicit empty value is a choice, and falling through to the old name
    there would make the new one impossible to switch off.

    The old spelling keeps working for one release and says so once, so a
    shell profile, a launchd plist or a running worker written before the
    rename does not silently read as unset.
    """
    value = os.environ.get(name)
    if value is not None:
        return value
    legacy = name.replace("DROMOND", "MAESTRO", 1)
    value = os.environ.get(legacy)
    if value is None:
        return default
    _say(f"dromond: {legacy} is deprecated and will stop working next "
         f"release — use {name} instead")
    return value


def _adopt(old: Path, new: Path, what: str) -> bool:
    """Move ``old`` to ``new`` if and only if ``new`` does not exist yet.

    ``os.rename`` is the whole implementation on purpose. It is atomic within
    a filesystem, so the state directory is never half-moved, and it cannot
    merge two directories — if both exist, the old one is reported and left
    exactly as it is, because choosing which of two live databases wins is a
    decision for the human, not for a migration.

    "Once" needs no bookkeeping: after a successful move ``old`` is gone, and
    if it ever comes back the destination now exists, so the second call takes
    the both-exist branch and touches nothing. There is deliberately no
    "already migrated" flag — a flag can disagree with the filesystem, and
    the filesystem is the thing being migrated.
    """
    if not old.exists() or old == new:
        return False
    if new.exists():
        _say(f"dromond: using {new}; the old {what} at {old} was left alone")
        return False
    try:
        new.parent.mkdir(parents=True, exist_ok=True)
        os.rename(old, new)
    except OSError as exc:
        # Never fatal. A cross-device rename or a permission problem must not
        # stop the command; Dromond simply starts with fresh state and says so.
        _say(f"dromond: could not move {old} to {new} ({exc}) — "
             f"starting with a new {what}")
        return False
    _say(f"dromond: moved your {what} from {old} to {new} (Maestro is Dromond now)")
    return True


def _adopt_database(state: Path) -> None:
    """Rename ``maestro.db`` to ``dromond.db`` inside a just-moved home.

    Moving the DIRECTORY is not enough: ``db_path`` asks for ``dromond.db``,
    so a home that still holds ``maestro.db`` reads as a first install and
    Dromond starts with an empty database while every run record sits beside
    it, untouched and invisible. That is the whole migration failing quietly,
    which is worse than failing loudly.

    The WAL and SHM sidecars are renamed with it, and this is the reason the
    loop exists rather than a single rename: SQLite finds them BY NAME, so a
    ``dromond.db`` next to a ``maestro.db-wal`` silently drops every
    transaction committed since the last checkpoint.
    """
    if (state / "dromond.db").exists():
        return
    for source in sorted(state.glob(LEGACY_STATE_DB + "*")):
        target = state / ("dromond.db" + source.name[len(LEGACY_STATE_DB):])
        try:
            os.rename(source, target)
        except OSError as exc:
            _say(f"dromond: could not rename {source} to {target} ({exc})")
            return
    if (state / "dromond.db").exists():
        _say(f"dromond: your run history came with it — {state / 'dromond.db'}")


def adopt_legacy_home() -> None:
    """Take over Maestro's directories on first start.

    Skipped entirely when the location is overridden. An explicit
    ``DROMOND_HOME`` (or a still-honoured ``MAESTRO_HOME``) names an exact
    directory, and the shim already returns the old one unchanged when only
    the old variable is set — so there is nothing to move, and moving anyway
    would let a process pointed at a scratch directory swallow the real
    ``~/.maestro``. That guard is what keeps the test suite from touching the
    developer's live state.
    """
    if not (os.environ.get("DROMOND_HOME") or os.environ.get("MAESTRO_HOME")):
        state = Path("~/.dromond").expanduser()
        if _adopt(Path("~/.maestro").expanduser(), state, "Dromond state directory"):
            _adopt_database(state)
    if not (os.environ.get("DROMOND_CONFIG") or os.environ.get("MAESTRO_CONFIG")):
        # The directory, not the file: nod-secrets.env and profile-notes.json
        # are its neighbours and have to travel with config.toml.
        _adopt(Path("~/.config/maestro").expanduser(),
               Path("~/.config/dromond").expanduser(), "Dromond config directory")


def home() -> Path:
    # ponytail: adoption hangs off the two path getters rather than a startup
    # hook, because every entry point Dromond has — the CLI, the daemon, the
    # HTTP server, a hook running inside a worker — reaches state through
    # these two and nothing else. One guarded stat beats hunting for "first
    # start" in five places and missing one.
    adopt_legacy_home()
    return Path(env("DROMOND_HOME", "~/.dromond")).expanduser()


def _sub(name: str) -> Path:
    d = home() / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path() -> Path:
    home().mkdir(parents=True, exist_ok=True)
    return home() / "dromond.db"


def logs_dir() -> Path:
    return _sub("logs")


def briefs_dir() -> Path:
    return _sub("briefs")


def slugify(raw: str) -> str:
    """Sanitize an id for use as a directory name."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", raw or "").strip("-") or "project"


def worktrees_dir(project_id: str) -> Path:
    """Keyed by Work's immutable projectId, never by the Work id: the id is
    mutable, so a renamed project would strand its worktree directory."""
    d = home() / "worktrees" / slugify(project_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def hooks_dir() -> Path:
    """Where Dromond keeps the artifacts it installs into harnesses (§6)."""
    return _sub("hooks")


def opencode_plugin_path() -> Path:
    """OpenCode has no shell hooks, so it gets a JS plugin delivered per run
    through ``OPENCODE_CONFIG_CONTENT``. It lives here, not in the user's
    ``~/.config/opencode``: only Dromond-spawned runs should load it."""
    return hooks_dir() / "dromond-opencode.js"


# Harness config homes. Each honours the harness's OWN environment override,
# so a test (or a second workspace) can point them at a throwaway directory
# and never touch the developer's real ~/.claude, ~/.codex or ~/.reasonix.

def claude_settings_path() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser() \
        / "settings.json"


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()


def reasonix_settings_path() -> Path:
    """Verified against reasonix v1.22.0: the global hook scope is
    ``$REASONIX_HOME/settings.json`` (`reasonix hook status --json` reports
    scope "global" as present once it exists)."""
    return Path(os.environ.get("REASONIX_HOME", "~/.reasonix")).expanduser() \
        / "settings.json"


def global_config_path() -> Path:
    adopt_legacy_home()
    return Path(env("DROMOND_CONFIG", "~/.config/dromond/config.toml")).expanduser()


def launch_agents_dir() -> Path:
    return Path(env("DROMOND_LAUNCH_AGENTS", "~/Library/LaunchAgents")).expanduser()
