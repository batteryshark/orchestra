"""Isolated git worktrees and harness-scoped context propagation.

Only Orchestra-owned linked checkouts are changed or removed. The group's
owner checkout is never staged, committed, reset, cleaned, or merged.
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

from orchestra import db, harnesses, paths

SHARED_DIRS = [".agents"]
SHARED_FILES = ["AGENTS.md", "ORCHESTRA.md"]
BACKEND_DIRS = {name: f".{name}" for name in harnesses.SUPPORTED}
BACKEND_FILES = {"claude": ["CLAUDE.md"]}

# Union of everything sync_skills may ever write: untracked_context_paths
# still has to keep all of it out of automatic checkpoints.
SKILL_DIRS = [*SHARED_DIRS, *sorted(BACKEND_DIRS.values())]
DOC_FILES = sorted({*SHARED_FILES, *(f for fs in BACKEND_FILES.values() for f in fs)})

# Where ~/.orchestra/skills/ lands in a run: the harness's own skills path
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

    A run starts in a fresh worktree with no memory of the repository, so without
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
        ["git", "-C", str(workdir), "status", "--porcelain",
         "--untracked-files=all"],
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
            continue
        untracked = subprocess.run(
            ["git", "-C", str(workdir), "ls-files", "--others",
             "--exclude-standard", "--", name],
            capture_output=True, text=True)
        excluded.extend(
            line for line in untracked.stdout.splitlines() if line.strip())
    return excluded


def global_skills_dir() -> Path:
    """The v2-local overlay every run sees."""
    return paths.state_dir() / "skills"


def submodules(root: Path, workdir: Path) -> bool:
    """Populate a new worktree's submodules, if the repository has any.

    ``git worktree add`` leaves every declared submodule as an EMPTY
    directory. A worker that needs one to build cannot build, and it will
    not simply stop: PREX3 runs 93, 94, and 99 each reasoned their way to
    `ln -s` from the main checkout — "a local workspace fix, not a git
    write" — which made `git status` refuse the path ("expected submodule
    path ... not to be a symbolic link") and killed the checkpoint of three
    runs whose work was finished and good. Retrying reran the same
    reasoning and broke the same way.

    The objects are already in the shared repository, so this is a local
    checkout and needs no network. Failure is not fatal: a worktree with
    empty submodules is what every run got before this, and the run should
    still start.
    """
    if not (root / ".gitmodules").is_file():
        return False
    # Point each submodule at the copy the SOURCE checkout already holds.
    # Without this every worktree clones godot-cpp from GitHub again: minutes
    # and a network per run, for bytes already on the disk. A submodule the
    # source has not checked out keeps its declared url.
    command = ["git", "-C", str(workdir)]
    for name, rel in _declared_submodules(root).items():
        if (root / rel / ".git").exists():
            command += ["-c", f"submodule.{name}.url={root / rel}"]
    command += ["-c", "protocol.file.allow=always", "submodule", "update",
                "--init", "--recursive"]
    res = subprocess.run(
        command,
        capture_output=True, text=True, timeout=600)
    if res.returncode != 0:
        print(f"orchestra: submodules not populated in {workdir}: "
              f"{(res.stderr or res.stdout).strip()[:300]}", file=sys.stderr)
        return False
    return True


def _declared_submodules(root: Path) -> dict:
    """``{submodule name: path}`` from the repository's .gitmodules."""
    res = subprocess.run(
        ["git", "config", "-f", str(root / ".gitmodules"), "--get-regexp",
         r"^submodule\..*\.path$"], capture_output=True, text=True, timeout=30)
    found = {}
    for line in res.stdout.splitlines():
        key, _, rel = line.partition(" ")
        name = key[len("submodule."):-len(".path")]
        if name and rel:
            found[name] = rel.strip()
    return found


def sync_skills(root: Path, workdir: Path, backend: str | None = None) -> list[str]:
    """Mirror the shared context + this backend's own skills into a workdir.

    Git worktrees only contain tracked files, so untracked .agents/.claude/etc.
    would otherwise be missing for the delegated tool. An unknown/absent
    backend gets the shared set only -- never another harness's directory.
    Finally the v2 fleet skill directory is overlaid, per entry, group-local
    repository skills winning.
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
                continue  # the repository defines this skill: it wins
            dest_root.mkdir(parents=True, exist_ok=True)
            if entry.is_dir():
                shutil.copytree(entry, dest, ignore=_IGNORE)
            else:
                shutil.copy2(entry, dest)
            synced.append(f"{rel}/{entry.name}")
    return synced


def create(root: Path, run_id: int, group_slug: str,
           start_point: str | None = None,
           backend: str | None = None) -> tuple[Path, str]:
    """Create a git worktree for an isolated run; returns (workdir, branch).

    Worktrees live centrally at ``v2/worktrees/<group>/run-N``, never inside
    the owner checkout.
    """
    if not (root / ".git").exists():
        raise RuntimeError("worktree isolation needs a git repository CWD")
    branch = f"orchestra/run-{run_id}"
    wt = paths.worktrees_dir(group_slug) / f"run-{run_id}"
    # A fresh database restarts run ids, but the user's repository keeps the
    # branches earlier generations made. Step past any taken name.
    suffix = 1
    while (_git(root, ["show-ref", "--verify", "--quiet",
                       f"refs/heads/{branch}"]).returncode == 0 or wt.exists()):
        suffix += 1
        branch = f"orchestra/run-{run_id}-{suffix}"
        wt = paths.worktrees_dir(group_slug) / f"run-{run_id}-{suffix}"
    cmd = ["git", "-C", str(root), "worktree", "add", "-b", branch, str(wt)]
    if start_point:
        cmd.append(start_point)
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise SystemExit(f"orchestra: git worktree failed: {res.stderr.strip()}")
    try:
        submodules(root, wt)
        sync_skills(root, wt, backend)
    except BaseException:
        # The linked checkout and branch already exist at this point. A
        # context-copy failure must not leave an ownerless run-N behind.
        try:
            discard_created(wt, root, branch)
        except Exception:
            pass  # cleanup failure must not hide the context-copy failure
        raise
    return wt, branch


def restore(root: Path, run_id: int, group_slug: str, branch: str,
            backend: str | None = None) -> Path:
    """Recreate the stable checkout for a retained run branch.

    Waiting releases process capacity and its linked checkout after a
    checkpoint. A later answer or child result loads the same session against
    the same path by checking the retained branch out again.
    """
    wt = paths.worktrees_dir(group_slug) / f"run-{int(run_id)}"
    if wt.exists():
        root_of = main_root(wt)
        if root_of == root.resolve():
            return wt
        raise RuntimeError(f"worktree path already exists and is not owned: {wt}")
    exists = _git(root, ["show-ref", "--verify", "--quiet",
                         f"refs/heads/{branch}"])
    if exists.returncode != 0:
        raise RuntimeError(f"retained run branch does not exist: {branch}")
    result = _git(root, ["worktree", "add", str(wt), branch])
    if result.returncode != 0:
        raise RuntimeError(
            f"cannot restore worktree for {branch}: {result.stderr.strip()}")
    try:
        submodules(root, wt)
        sync_skills(root, wt, backend)
    except BaseException:
        remove(wt, root, branch=branch, force=True)
        raise
    return wt


# --- giving a worktree back (DESIGN §14) ------------------------------------

RUN_DIR_RE = re.compile(r"^run-(\d+)(?:-\d+)?$")


def _git(root: Path, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True)


def main_root(workdir: Path) -> Path | None:
    """The owner checkout a linked worktree belongs to, or None when the
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

    The context Orchestra copied in (harness directories, skills, AGENTS.md) is
    not the run's work and never blocks removal — ``_checkpoint_commit``
    excludes exactly the same paths when it commits.
    """
    try:
        lines = [ln for ln in status(workdir).splitlines() if ln.strip()]
    except RuntimeError as exc:
        return [f"status unavailable: {exc}"]
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


def removal_risks(workdir: Path) -> list[str]:
    """Why removing this checkout could lose work.

    Uncommitted changes die with the directory, so they always count.
    Commits do not count: ``git worktree remove`` retains the run branch, which
    is the durable checkpoint and evidence handle.
    """
    risks = []
    dirty = dirty_paths(workdir)
    if dirty:
        risks.append(f"{len(dirty)} uncommitted change(s): {', '.join(dirty[:3])}")
    return risks


def remove(workdir, root: Path | None = None, branch: str | None = None,
           force: bool = False) -> dict:
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
        risks = removal_risks(workdir)
        if root is None and any(workdir.iterdir()):
            risks.append("not a git worktree")
        if risks and not force:
            report["kept"] = "; ".join(risks)
            return report
        report["discarded"] = risks
    if root is None:
        # ponytail: an unrecognizable directory under ~/.orchestra/worktrees is
        # still Orchestra's own; delete it rather than grow a quarantine area.
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


def discard_created(workdir: Path, root: Path, branch: str) -> dict:
    """Undo a worktree that failed before its run could start.

    ``create`` just made this branch and no worker has run in it, so removing
    both checkout and branch cannot discard user work. The report lets the
    caller retain the association on the run row if cleanup itself fails.
    """
    report = remove(workdir, root, branch=branch, force=True)
    report["branch_deleted"] = False
    if not report["removed"]:
        return report
    deleted = _git(root, ["branch", "-D", branch])
    report["branch_deleted"] = deleted.returncode == 0
    if deleted.returncode != 0:
        report["error"] = deleted.stderr.strip() or f"could not delete {branch}"
    return report


def branch_exists(root: Path, branch: str) -> bool:
    return _git(root, ["show-ref", "--verify", "--quiet",
                       f"refs/heads/{branch}"]).returncode == 0


def branch_merged(root: Path, branch: str) -> bool:
    """True when the branch's work is already contained in the current HEAD."""
    return _git(root, ["merge-base", "--is-ancestor", branch,
                       "HEAD"]).returncode == 0


def merge_into_owner(root: Path, branch: str) -> dict:
    """Merge one retained run branch into the owner checkout's current branch.

    An operator convenience, never automatic. Refuses a dirty owner checkout
    and any conflicted merge; a refused merge is aborted so the checkout is
    left exactly as found.
    """
    if not branch_exists(root, branch):
        raise RuntimeError(f"run branch does not exist: {branch}")
    if branch_merged(root, branch):
        raise RuntimeError(f"run branch is already merged: {branch}")
    if status(root).strip():
        raise RuntimeError(
            "owner checkout has uncommitted changes; commit or stash them first")
    merged = _git(root, ["merge", "--no-edit", branch])
    if merged.returncode != 0:
        _git(root, ["merge", "--abort"])
        raise RuntimeError(
            f"merge refused: {(merged.stderr or merged.stdout).strip()[:300]}")
    head_commit = _git(root, ["rev-parse", "HEAD"]).stdout.strip()
    return {"merged": True, "branch": branch, "commit": head_commit}


def prune(con, force: bool = False) -> dict:
    """Sweep Orchestra's v2 per-group worktree directories.

    An orphan is a worktree whose run row is terminal or gone. A live run's
    worktree is skipped and reported, never removed — not even with --force.
    Pre-reset state is an archive and is intentionally never mutated here.
    """
    out = {"worktrees": [], "dirs": []}
    root = paths.worktrees_dir()
    containers = sorted(path for path in root.iterdir() if path.is_dir())
    for container in containers:
        for wt in sorted(p for p in container.iterdir() if p.is_dir()):
            out["worktrees"].append(_prune_one(con, wt, force))
        if not any(container.iterdir()):
            container.rmdir()
            out["dirs"].append(str(container))
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
                  force=force)
