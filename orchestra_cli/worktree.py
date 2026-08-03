"""Isolated worktree workdirs + skills folder propagation."""
import shutil
import subprocess
from pathlib import Path

from orchestra_cli import paths

SKILL_DIRS = [".agents", ".claude", ".codex", ".opencode"]
DOC_FILES = ["AGENTS.md", "CLAUDE.md", "ORCHESTRA.md"]


def head(workdir: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(workdir), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"cannot read worktree HEAD: {result.stderr.strip()}")
    return result.stdout.strip()


def status(workdir: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(workdir), "status", "--porcelain"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"cannot read worktree status: {result.stderr.strip()}")
    return result.stdout


def untracked_context_paths(workdir: Path) -> list[str]:
    """Context copied for agents must not leak into automatic checkpoints."""
    excluded = []
    for name in [*SKILL_DIRS, *DOC_FILES]:
        if not (workdir / name).exists():
            continue
        tracked = subprocess.run(
            ["git", "-C", str(workdir), "ls-files", "--error-unmatch", "--", name],
            capture_output=True,
            text=True,
        )
        if tracked.returncode != 0:
            excluded.append(name)
    return excluded


def sync_skills(root: Path, workdir: Path) -> list[str]:
    """Mirror the project's skills folders + agent docs into an isolated workdir.

    Git worktrees only contain tracked files, so untracked .agents/.claude/etc.
    would otherwise be missing for the delegated tool.
    """
    synced = []
    for d in SKILL_DIRS:
        src = root / d
        if src.is_dir() and not (workdir / d).exists():
            shutil.copytree(src, workdir / d, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("logs", "worktrees", "*.db*", "node_modules"))
            synced.append(d)
    for f in DOC_FILES:
        src = root / f
        if src.is_file() and not (workdir / f).exists():
            shutil.copy2(src, workdir / f)
            synced.append(f)
    return synced


def create(root: Path, run_id: int, start_point: str | None = None) -> tuple[Path, str]:
    """Create a git worktree for an isolated run; returns (workdir, branch)."""
    if not (root / ".git").exists():
        raise SystemExit("orchestra: --worktree needs the project to be a git repository")
    branch = f"orchestra/run-{run_id}"
    wt = paths.worktrees_dir(root) / f"run-{run_id}"
    cmd = ["git", "-C", str(root), "worktree", "add", "-b", branch, str(wt)]
    if start_point:
        cmd.append(start_point)
    res = subprocess.run(cmd,
                         capture_output=True, text=True)
    if res.returncode != 0:
        raise SystemExit(f"orchestra: git worktree failed: {res.stderr.strip()}")
    sync_skills(root, wt)
    return wt, branch
