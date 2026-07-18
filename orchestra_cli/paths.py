import os
from pathlib import Path

STATE_DIR = ".orchestra"


def find_root(explicit: str | None = None) -> Path:
    """Locate the project root containing .orchestra, like git's walk-up."""
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if (p / STATE_DIR).is_dir():
            return p
        raise SystemExit(f"orchestra: no {STATE_DIR}/ in {p} (run `orchestra init` there first)")
    env = os.environ.get("ORCHESTRA_ROOT")
    if env and (Path(env) / STATE_DIR).is_dir():
        return Path(env).resolve()
    cur = Path.cwd()
    for candidate in [cur, *cur.parents]:
        if (candidate / STATE_DIR).is_dir():
            return candidate
    raise SystemExit(
        "orchestra: no .orchestra/ found in this directory or any parent.\n"
        "Run `orchestra init` at your project root first."
    )


def state_dir(root: Path) -> Path:
    return root / STATE_DIR


def db_path(root: Path) -> Path:
    return state_dir(root) / "orchestra.db"


def logs_dir(root: Path) -> Path:
    d = state_dir(root) / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def briefs_dir(root: Path) -> Path:
    d = state_dir(root) / "briefs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def worktrees_dir(root: Path) -> Path:
    d = state_dir(root) / "worktrees"
    d.mkdir(parents=True, exist_ok=True)
    return d


def checkpoints_dir(root: Path, *, create: bool = False) -> Path:
    """Durable handoff artifacts written by ``orchestra checkpoint``.

    Read-only callers (e.g. ``takeover`` without a checkpoint) MUST pass
    ``create=False`` so a missing checkpoint surfaces as "no checkpoints
    found" instead of silently instantiating an empty directory.
    """
    d = state_dir(root) / "checkpoints"
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def global_config_path() -> Path:
    return Path(os.environ.get("ORCHESTRA_CONFIG", "~/.config/orchestra/config.toml")).expanduser()


def project_config_path(root: Path) -> Path:
    return state_dir(root) / "config.toml"
