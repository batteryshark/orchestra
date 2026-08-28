"""Where Orchestra keeps its state: one central ``~/.orchestra``, never per project.

DESIGN §2 — the database, briefs, logs, and worktrees all live under
``~/.orchestra`` (``ORCHESTRA_HOME`` overrides it, for tests and for a second
daemon serving a separate workspace). Projects get no state directory of
their own, so there is nothing to gitignore per repo and cross-project
questions are one query.
"""
import os
import re
from pathlib import Path


def env(name: str, default: str = "") -> str:
    """An ``ORCHESTRA_*`` variable, or ``default`` when it is unset. An explicit
    empty value is a choice and is returned as such."""
    value = os.environ.get(name)
    return default if value is None else value


def home() -> Path:
    configured = env("ORCHESTRA_HOME", "~/.orchestra")
    if not configured:
        raise SystemExit("orchestra: ORCHESTRA_HOME must not be empty")
    candidate = Path(configured).expanduser()
    resolved = candidate.resolve()
    if resolved == Path.cwd().resolve() or resolved == Path(resolved.anchor):
        raise SystemExit("orchestra: ORCHESTRA_HOME must name a dedicated state "
                         "directory, not the current directory or filesystem root")
    return candidate


def _owner_dir(path: Path) -> Path:
    """Create a state directory and keep it private to the current user.

    The directory is the security boundary for every artifact beneath it,
    including files created by SQLite and launchd whose own modes follow the
    process umask. Windows relies on the user's profile ACLs; its chmod only
    controls the read-only bit and cannot express this POSIX invariant.
    """
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        path.chmod(0o700)
    return path


def _sub(name: str) -> Path:
    return _owner_dir(_owner_dir(home()) / name)


def db_path() -> Path:
    return _owner_dir(home()) / "orchestra.db"


def logs_dir() -> Path:
    return _sub("logs")


def briefs_dir() -> Path:
    return _sub("briefs")


def slugify(raw: str) -> str:
    """Sanitize an id for use as a directory name."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", raw or "").strip("-") or "project"


def kebab(raw: str) -> str:
    """A human-readable lowercase kebab-case slug: ``My Project!`` ->
    ``my-project``. Empty input gets the honest placeholder."""
    return re.sub(r"[^a-z0-9]+", "-", (raw or "").lower()).strip("-") or "project"


def project_dir(slug: str) -> Path:
    """ONE per-project area: ``~/.orchestra/projects/<slug>/``.

    Worktrees and the ephemeral workspace live inside it, and any future
    per-project artifact lands beside them. Keyed by the registry's stable
    ``projects.slug``; per-RUN artifacts (briefs, logs) stay in the central
    ``briefs/`` and ``logs/`` because they are pruned per run, not per project.
    """
    return _owner_dir(_sub("projects") / slugify(slug))


def run_dir(slug: str, seq: int) -> Path:
    """One run's own artifacts — brief, raw log, future outputs — filed under
    its project by the PROJECT's run number, the number the board shows and
    the one humans quote. Globally unique as a pair: the slug is unique
    across projects, the number inside its project."""
    return _owner_dir(project_dir(slug) / "runs" / f"run-{seq}")


def worktrees_dir(slug: str) -> Path:
    """Keyed by the project's stable slug, never by its path: the path is
    mutable, so a renamed project folder would strand its worktree directory."""
    return _owner_dir(project_dir(slug) / "worktrees")


def workspace_dir(slug: str) -> Path:
    """Where a project with no checkout of its own runs (W-0312).

    A store-only project — a trip to book, a will to finalize — has an
    organizational reference and no directory anywhere. It still needs
    somewhere to put a file, so it gets one here, keyed by the stable slug and
    kept across runs so a second pass sees what the first one wrote.
    """
    return _owner_dir(project_dir(slug) / "workspace")


def hooks_dir() -> Path:
    """Where Orchestra keeps the artifacts it installs into harnesses (§6)."""
    return _sub("hooks")


def opencode_plugin_path() -> Path:
    """OpenCode has no shell hooks, so it gets a JS plugin delivered per run
    through ``OPENCODE_CONFIG_CONTENT``. It lives here, not in the user's
    ``~/.config/opencode``: only Orchestra-spawned runs should load it."""
    return hooks_dir() / "orchestra-opencode.js"


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
    return Path(env("ORCHESTRA_CONFIG", "~/.config/orchestra/config.toml")).expanduser()


def launch_agents_dir() -> Path:
    return Path(env("ORCHESTRA_LAUNCH_AGENTS", "~/Library/LaunchAgents")).expanduser()
