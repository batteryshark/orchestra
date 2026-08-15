"""Isolated worktree workdirs + skills folder propagation (ported).

DESIGN §12: a run gets the skill directory for ITS OWN backend plus the
shared set, never all four. Another harness's directory can carry hooks
that fire inside a run Dromond is already hooking.

Worktrees are also given BACK here (W-0172): ``remove`` retires one run's
checkout, ``prune`` sweeps the orphans. One rule outranks everything else in
this file — a worktree belonging to a live run is never touched.
"""
import re
import shutil
import subprocess
from pathlib import Path

from dromond import db, paths

SHARED_DIRS = [".agents"]
# MAESTRO.md is still listed: these files live in the USER'S repo, and W-0188
# renamed the product, not the projects that already carry one. A missing entry
# is skipped, so listing both costs nothing and silently dropping the old name
# would quietly stop delivering context a project still relies on.
SHARED_FILES = ["AGENTS.md", "DROMOND.md", "MAESTRO.md"]
BACKEND_DIRS = {"claude": ".claude", "codex": ".codex",
                "opencode": ".opencode", "reasonix": ".reasonix"}
BACKEND_FILES = {"claude": ["CLAUDE.md"]}

# Union of everything sync_skills may ever write: untracked_context_paths
# still has to keep all of it out of automatic checkpoints.
SKILL_DIRS = [*SHARED_DIRS, *sorted(BACKEND_DIRS.values())]
DOC_FILES = sorted({*SHARED_FILES, *(f for fs in BACKEND_FILES.values() for f in fs)})

# Where ~/.dromond/skills/ lands in a run: the harness's own skills path
# where one is known (Claude Code reads .claude/skills; Reasonix mirrors
# Claude's layout), else the shared .agents/skills. Codex and OpenCode have
# no confirmed skills convention, so the fallback is honest, not guessed.
BACKEND_SKILLS_DEST = {"claude": ".claude/skills", "reasonix": ".reasonix/skills"}
SHARED_SKILLS_DEST = ".agents/skills"


def global_skills_dest(backend: str | None = None) -> str:
    return BACKEND_SKILLS_DEST.get(backend or "", SHARED_SKILLS_DEST)

_IGNORE = shutil.ignore_patterns("logs", "worktrees", "*.db*", "node_modules")


def head(workdir: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(workdir), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"cannot read worktree HEAD: {result.stderr.strip()}")
    return result.stdout.strip()


RECENT_COMMITS = 12


def recent_commits(workdir: Path, count: int = RECENT_COMMITS,
                   since: str | None = None) -> list[str]:
    """What landed here lately, newest first, as `sha subject` lines.

    A run starts in a fresh worktree with no memory of the project, so without
    this it cannot tell work that is waiting to be done from work that landed
    an hour ago. That is how two runs came to build the same thing at once.

    ``since`` narrows it to commits after that sha, which is what a resumed
    run needs: its own worktree branched before them and cannot see them.
    Returns an empty list for anything that is not a repository with commits.
    """
    rev = [f"{since}..HEAD"] if since else []
    result = subprocess.run(
        ["git", "-C", str(workdir), "log", f"-{count}", "--no-merges",
         "--format=%h %s", *rev],
        capture_output=True, text=True)
    if result.returncode != 0:
        return []
    return [ln for ln in result.stdout.splitlines() if ln.strip()]


def status(workdir: Path) -> str:
    """Dirty check: non-empty means uncommitted changes."""
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


def global_skills_dir() -> Path:
    """The overlay every run sees, central state like everything else (§2)."""
    return paths.home() / "skills"


def sync_skills(root: Path, workdir: Path, backend: str | None = None) -> list[str]:
    """Mirror the shared context + this backend's own skills into a workdir.

    Git worktrees only contain tracked files, so untracked .agents/.claude/etc.
    would otherwise be missing for the delegated tool. An unknown/absent
    backend gets the shared set only -- never another harness's directory.
    Finally ~/.dromond/skills/ is overlaid, per entry, project skills winning.
    """
    synced = []
    for d in [*SHARED_DIRS, *([BACKEND_DIRS[backend]] if backend in BACKEND_DIRS else [])]:
        src = root / d
        if src.is_dir() and not (workdir / d).exists():
            shutil.copytree(src, workdir / d, dirs_exist_ok=True, ignore=_IGNORE)
            synced.append(d)
    for f in [*SHARED_FILES, *BACKEND_FILES.get(backend or "", [])]:
        src = root / f
        if src.is_file() and not (workdir / f).exists():
            shutil.copy2(src, workdir / f)
            synced.append(f)
    overlay = global_skills_dir()
    if overlay.is_dir():
        rel = global_skills_dest(backend)
        dest_root = workdir / rel
        for entry in sorted(overlay.iterdir()):
            dest = dest_root / entry.name
            if dest.exists():
                continue  # the project defines this skill: it wins
            dest_root.mkdir(parents=True, exist_ok=True)
            if entry.is_dir():
                shutil.copytree(entry, dest, ignore=_IGNORE)
            else:
                shutil.copy2(entry, dest)
            synced.append(f"{rel}/{entry.name}")
    return synced


def create(root: Path, run_id: int, project_id: str,
           start_point: str | None = None,
           backend: str | None = None) -> tuple[Path, str]:
    """Create a git worktree for an isolated run; returns (workdir, branch).

    Worktrees live centrally at ~/.dromond/worktrees/<project-slug>/run-N
    (DESIGN §2), never inside the project.
    """
    if not (root / ".git").exists():
        raise SystemExit("dromond: --worktree needs the project to be a git repository")
    branch = f"dromond/run-{run_id}"
    wt = paths.worktrees_dir(project_id) / f"run-{run_id}"
    cmd = ["git", "-C", str(root), "worktree", "add", "-b", branch, str(wt)]
    if start_point:
        cmd.append(start_point)
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise SystemExit(f"dromond: git worktree failed: {res.stderr.strip()}")
    sync_skills(root, wt, backend)
    return wt, branch


# --- giving a worktree back (W-0172) ----------------------------------------

RUN_DIR_RE = re.compile(r"^run-(\d+)$")


def _git(root: Path, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True)


def main_root(workdir: Path) -> Path | None:
    """The project checkout a linked worktree belongs to, or None when the
    directory is not a git worktree at all."""
    r = _git(workdir, ["rev-parse", "--git-common-dir"])
    if r.returncode != 0:
        return None
    common = Path(r.stdout.strip())
    if not common.is_absolute():
        common = workdir / common
    return common.resolve().parent


def dirty_paths(workdir: Path) -> list[str]:
    """Uncommitted work in a worktree.

    The context Dromond copied in (harness directories, skills, AGENTS.md) is
    not the run's work and never blocks removal — ``_checkpoint_commit``
    excludes exactly the same paths when it commits.
    """
    try:
        lines = [ln for ln in status(workdir).splitlines() if ln.strip()]
    except RuntimeError:
        return []
    context = untracked_context_paths(workdir)
    out = []
    for line in lines:
        path = line[3:].split(" -> ")[-1].strip().strip('"')
        if any(path == c or path.startswith(f"{c}/") for c in context):
            continue
        out.append(path)
    return out


def live_holders(con, workdir: Path, ignore_run: int | None = None) -> list[int]:
    """Run ids that are NOT terminal and are working in this checkout.

    The rule that must hold under every code path: a live run keeps its
    worktree, however old the directory looks. A follow-up run inherits its
    parent's workdir, so the check is by path, not by directory name.
    """
    target = Path(workdir).resolve()
    return [int(r["id"]) for r in con.execute(
        f"SELECT id, workdir FROM runs WHERE status NOT IN {db.TERMINAL_SQL}")
        if r["workdir"] and Path(r["workdir"]).resolve() == target
        and int(r["id"]) != ignore_run]


def removal_risks(workdir: Path, root: Path | None = None,
                  branch: str | None = None, base: str | None = None) -> list[str]:
    """Why removing this checkout could lose work.

    Uncommitted changes die with the directory, so they always count.
    Commits on the run branch do NOT — ``git worktree remove`` never deletes a
    branch — so they count only when the caller names a ``base`` and wants the
    conservative answer (``dromond prune``). The terminal-state removal passes
    no base on purpose: the merge step is what consumes that branch next, and
    it cannot delete it while this checkout still holds it.
    """
    risks = []
    dirty = dirty_paths(workdir)
    if dirty:
        risks.append(f"{len(dirty)} uncommitted change(s): {', '.join(dirty[:3])}")
    if base and branch and root:
        ahead = [ln for ln in _git(root, ["rev-list", f"{base}..{branch}"]
                                   ).stdout.splitlines() if ln]
        if ahead:
            risks.append(f"{len(ahead)} commit(s) on {branch} not on {base} "
                         f"(the branch keeps them; only the checkout goes)")
    return risks


def remove(workdir, root: Path | None = None, branch: str | None = None,
           base: str | None = None, force: bool = False) -> dict:
    """Remove ONE run worktree. The branch is never deleted here.

    Returns {"workdir", "branch", "removed", "kept", "discarded", "error"}:
    ``kept`` is the reason we refused, ``discarded`` is what ``force`` threw
    away. Callers must check liveness (``live_holders``) first.
    """
    workdir = Path(workdir)
    report = {"workdir": str(workdir), "branch": branch, "removed": False,
              "kept": None, "discarded": [], "error": None}
    exists = workdir.exists()
    root = root or (main_root(workdir) if exists else None)
    if exists:
        risks = removal_risks(workdir, root, branch, base)
        if root is None and any(workdir.iterdir()):
            risks.append("not a git worktree")
        if risks and not force:
            report["kept"] = "; ".join(risks)
            return report
        report["discarded"] = risks
    if root is None:
        # ponytail: an unrecognizable directory under ~/.dromond/worktrees is
        # still Dromond's own; delete it rather than grow a quarantine area.
        shutil.rmtree(workdir, ignore_errors=True)
        report["removed"] = not workdir.exists()
        if not report["removed"]:
            report["error"] = "not a git worktree, and the directory would not delete"
        return report
    r = _git(root, ["worktree", "remove", str(workdir)])
    if r.returncode != 0:
        # Two ordinary cases: untracked context files git will not discard on
        # its own, and a directory a human already deleted by hand.
        forced = _git(root, ["worktree", "remove", "--force", str(workdir)])
        shutil.rmtree(workdir, ignore_errors=True)
        _git(root, ["worktree", "prune"])
        if workdir.exists():
            report["error"] = (forced.stderr or r.stderr).strip()
            return report
    report["removed"] = True
    return report


def _base_branch(root: Path) -> str | None:
    """What a run branch is measured against.

    ponytail: the project's checked-out branch, merge.py's own default. A
    project that overrides [merge] base only makes prune more conservative,
    never less; read the config here if that ever stops being true.
    """
    return _git(root, ["symbolic-ref", "--short", "HEAD"]).stdout.strip() or None


def prune(con, force: bool = False) -> dict:
    """Sweep ~/.dromond/worktrees: orphan checkouts, then empty project dirs.

    An orphan is a worktree whose run row is terminal or gone. A live run's
    worktree is skipped and reported, never removed — not even with --force.
    """
    base_dir = paths.home() / "worktrees"
    out = {"worktrees": [], "dirs": []}
    if not base_dir.is_dir():
        return out
    for project_dir in sorted(p for p in base_dir.iterdir() if p.is_dir()):
        for wt in sorted(p for p in project_dir.iterdir() if p.is_dir()):
            out["worktrees"].append(_prune_one(con, wt, force))
        if not any(project_dir.iterdir()):
            project_dir.rmdir()
            out["dirs"].append(str(project_dir))
    return out


def _prune_one(con, wt: Path, force: bool) -> dict:
    match = RUN_DIR_RE.match(wt.name)
    run = con.execute("SELECT * FROM runs WHERE id=?",
                      (int(match.group(1)),)).fetchone() if match else None
    if run is not None and run["status"] not in db.RUN_TERMINAL:
        return {"workdir": str(wt), "branch": run["branch"], "removed": False,
                "kept": f"run {run['id']} is {run['status']}", "discarded": [],
                "error": None, "live": True}
    holders = live_holders(con, wt)
    if holders:
        return {"workdir": str(wt), "branch": None, "removed": False,
                "kept": f"run {holders[0]} is live in this checkout",
                "discarded": [], "error": None, "live": True}
    root = main_root(wt)
    return remove(wt, root, branch=run["branch"] if run is not None else None,
                  base=_base_branch(root) if root else None, force=force)
