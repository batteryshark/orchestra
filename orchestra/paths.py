"""Private filesystem layout for one Orchestra v2 instance."""
from __future__ import annotations

import os
import re
from pathlib import Path


def env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value is None else value


def home() -> Path:
    raw = env("ORCHESTRA_HOME", "~/.orchestra")
    if not raw:
        raise SystemExit("orchestra: ORCHESTRA_HOME must not be empty")
    candidate = Path(raw).expanduser()
    resolved = candidate.resolve()
    if resolved in (Path.cwd().resolve(), Path(resolved.anchor)):
        raise SystemExit(
            "orchestra: ORCHESTRA_HOME must be a dedicated state directory")
    return candidate


def owner_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        path.chmod(0o700)
    return path


def state_dir() -> Path:
    """The incompatible v2 state root; older roots are never opened here."""
    return owner_dir(owner_dir(home()) / "v2")


def db_path() -> Path:
    return state_dir() / "orchestra.db"


def bootstrap_path() -> Path:
    """Non-secret daemon bootstrap (bind address and secret-file location)."""
    return state_dir() / "bootstrap.json"


def secret_path() -> Path:
    return state_dir() / "secrets.json"


def _sub(name: str) -> Path:
    return owner_dir(state_dir() / name)


def logs_dir() -> Path:
    return _sub("logs")


def briefs_dir() -> Path:
    return _sub("briefs")


def artifacts_dir() -> Path:
    return _sub("artifacts")


def worktrees_dir(group_slug: str | None = None) -> Path:
    root = _sub("worktrees")
    return owner_dir(root / slugify(group_slug)) if group_slug else root


def groups_dir() -> Path:
    return _sub("groups")


def group_workspace(group_slug: str) -> Path:
    return owner_dir(groups_dir() / slugify(group_slug) / "workspace")


def run_dir(run_id: int) -> Path:
    return owner_dir(_sub("runs") / str(int(run_id)))


def run_artifacts_dir(run_id: int) -> Path:
    return owner_dir(artifacts_dir() / str(int(run_id)))


def backups_dir() -> Path:
    return _sub("backups")


def archives_dir() -> Path:
    return owner_dir(home() / "archives")


def slugify(raw: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", raw or "").strip("-") or "item"


def kebab(raw: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (raw or "").lower()).strip("-") or "item"


def global_config_path() -> Path:
    """Legacy config location, read only by the explicit v1 archive/import."""
    return Path(env("ORCHESTRA_CONFIG", "~/.config/orchestra/config.toml")).expanduser()


def legacy_state_candidates() -> tuple[Path, ...]:
    base = home()
    return tuple(path for path in (base / "fleet", base) if path != state_dir())


def launch_agents_dir() -> Path:
    return Path(env("ORCHESTRA_LAUNCH_AGENTS", "~/Library/LaunchAgents")).expanduser()


def claude_settings_path() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser() \
        / "settings.json"


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()


def reasonix_settings_path() -> Path:
    return Path(os.environ.get("REASONIX_HOME", "~/.reasonix")).expanduser() \
        / "settings.json"
