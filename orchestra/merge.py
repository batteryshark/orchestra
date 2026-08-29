"""Land a run branch on the base branch (DESIGN §9, "Landing the work").

A base that moves between the rebase and the ref swap is a race between two
runs landing at once; ``SWAP_ATTEMPTS`` says how many times to rebase onto the
new base and try again before it stops being self-correcting and becomes the
human's problem.

Verification enforces declared checks followed by mechanical tripwires. The
merge itself happens in a THROWAWAY WORKTREE: the base branch ref is updated with
``git update-ref``, so the owner's checkout, which routinely holds
uncommitted work, is never touched.

Self-contained by design: the git helpers live here, not in worktree.py.

Per-project configuration, in a ``[project."<projectId>"]`` table of the
global ``~/.config/orchestra/config.toml`` (projects have no state directory
of their own — DESIGN §2)::

    [merge]
    enabled = true           # false: runs END at their branch + receipts, and
                             # whatever lands them is external (a human, or an
                             # agentic lander). Explicit retries still land.
    base = "main"            # default: the branch the project root has checked out
    max_files = 50           # tripwire: files touched by the diff
    max_lines = 2000         # tripwire: insertions + deletions
    allow_deletions = false  # tripwire: any deleted file
    project_paths = ["src"]  # tripwire: a touched file outside these prefixes
    check_timeout = 1800     # per-check seconds
    require_clean = false    # opt-in: refuse the merge when the owner's
                             # uncommitted edits OVERLAP the merged files.
                             # Off by default — the merge lands either way and
                             # the checkout simply keeps its pre-merge tree.
    judge_tripwires = true   # a model judges tripwired facts against the mission first
    resolver_profile = "big" # resolver staffing; unset = best tier-2 profile
                             # dispatch (default: the highest-priority tier-3 profile)

    [merge.checks]           # declared checks, run in declared order
    test = "uv run python -m unittest discover -s tests"
    lint = "ruff check ."

Result shape (a plain dict, consumed by this module alone)::

    {"ok", "stage", "escalation", "base", "branch", "commit", "files_changed",
     "checks", "checks_skipped", "tripwires", "conflicts",
     "revert_command", "branch_deleted", "refresh", "note", "kept_ref",
     "dropped", "dirty"}

``stage`` is where the run stopped: dirty | rebase | checks | tripwires |
merged. ``commit`` is the merge commit this run created, and is None
when the base already contained the branch — then there is no merge commit and
no ``revert_command``, because a revert line aimed at a commit the run did not
create either errors or undoes somebody else's work (I-0077). ``refresh`` reports what happened to a checkout sitting on the base
branch (refreshed | skipped | refused, always with a reason). ``merge_run``
never posts anywhere and never resolves a conflict.

``at_completion`` is the other half (W-0174): the seam ``supervise.py`` calls
when a run reaches ``done``. It lands the branch, files a Nod card when a
human must choose, writes the report into the run's own thread, and stamps
the landing receipt — ``landing_status`` plus ``landing_commit`` — on the run
row. It reports to no source: rebasing a branch and moving a ref must not
know a record system exists (source boundary). Whoever reports a run
to its source reads the receipt. Nothing in here may break finalization.
"""
import inspect
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from orchestra import db, nod, paths, project

SWAP_ATTEMPTS = 3
MAX_REPLAY_DROPS = 100

DEFAULTS = {
    # Auto-landing. Off means Orchestra's contract ends at the branch and
    # the receipts (landing_status 'skipped'); an external lander consumes
    # them. A human's explicit retry still lands — policy gates the
    # automatic path, never the owner's own hand.
    "enabled": True,
    "base": None,
    "checks": {},
    "max_files": 50,
    "max_lines": 2000,
    "allow_deletions": False,
    "project_paths": [],
    "check_timeout": 1800,
    # On by default, and narrow: only an overlap between the owner's
    # uncommitted edits and the merged files escalates. Off means even that
    # lands, with the refresh left to decline underneath it.
    "require_clean": False,
    # Tripwires yield to a judgment turn before they yield to the phone: a run
    # dispatched to delete dead code must not escalate its own deletions. Off
    # means every tripwire escalates, as before.
    "judge_tripwires": True,
    # Which profile a "Dispatch a resolver" answer staffs (resolver.py). None
    # means the highest-priority enabled tier-3 profile.
    "resolver_profile": None,
}
CHECK_OUTPUT_MAX_CHARS = 4000


# --- git --------------------------------------------------------------------

def _git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {(r.stderr or r.stdout).strip()}")
    return r


def _out(args: list[str], cwd: Path) -> str:
    return _git(args, cwd).stdout.strip()


def _lines(args: list[str], cwd: Path) -> list[str]:
    return [ln for ln in _out(args, cwd).splitlines() if ln]


# --- configuration ----------------------------------------------------------

def merge_cfg(cfg: dict | None = None) -> dict:
    """The [merge] table over the defaults.

    Per-project settings live in the central config under
    [project."<projectId>"] (DESIGN §2); projects have no state directory of
    their own, so there is no per-project file to read.
    """
    table = (cfg or {}).get("merge") or {}
    return {**DEFAULTS, **table}


# --- verification stages ----------------------------------------------------

def run_checks(cfg: dict, workdir: Path) -> tuple[list[dict], bool]:
    """Stage 1: the repo's declared checks, in declared order, first failure wins."""
    results = []
    for name, command in (cfg["checks"] or {}).items():
        try:
            r = subprocess.run(command, shell=True, cwd=str(workdir),
                               capture_output=True, text=True,
                               timeout=cfg["check_timeout"])
            code, output = r.returncode, (r.stdout + r.stderr)
        except subprocess.TimeoutExpired:
            code, output = 124, f"timed out after {cfg['check_timeout']}s"
        results.append({"name": name, "command": command, "ok": code == 0,
                        "exit_code": code, "output": output[-CHECK_OUTPUT_MAX_CHARS:]})
        if code != 0:
            return results, False
    return results, True


def tripwires(cfg: dict, workdir: Path, base_sha: str, head_sha: str) -> list[str]:
    """Stage 2: mechanical limits. Returns the tripped ones, empty when clean."""
    tripped = []
    status = _lines(["diff", "--name-status", base_sha, head_sha], workdir)
    deleted = [ln.split("\t", 1)[1] for ln in status if ln.startswith("D")]
    if deleted and not cfg["allow_deletions"]:
        tripped.append(f"deletes {len(deleted)} file(s): {', '.join(deleted[:5])}")

    files = [ln.split("\t")[-1] for ln in status]
    if cfg["max_files"] and len(files) > cfg["max_files"]:
        tripped.append(f"touches {len(files)} files (max {cfg['max_files']})")

    changed = 0
    for ln in _lines(["diff", "--numstat", base_sha, head_sha], workdir):
        added, removed, _ = ln.split("\t", 2)
        changed += sum(int(n) for n in (added, removed) if n.isdigit())
    if cfg["max_lines"] and changed > cfg["max_lines"]:
        tripped.append(f"changes {changed} lines (max {cfg['max_lines']})")

    prefixes = cfg["project_paths"] or []
    if prefixes:
        outside = [f for f in files
                   if not any(f == p or f.startswith(p.rstrip("/") + "/") for p in prefixes)]
        if outside:
            tripped.append(f"touches {len(outside)} file(s) outside the project: "
                           f"{', '.join(outside[:5])}")
    return tripped


# The judge answers ONE question -- is this the mission's own work -- and the
# shape of the change answers it: which files, added or deleted, how much. The
# full contents rarely add anything, and a 60KB diff made a real opus-medium
# turn blow through its 180s timeout, which lands on escalate and pings the
# owner anyway. Stat first, then a bounded slice of the diff for texture.
JUDGE_STAT_MAX_CHARS = 4_000
JUDGE_DIFF_MAX_CHARS = 8_000

JUDGE_INSTRUCTIONS = """\
You are Orchestra's merge judge. A finished run's branch tripped a mechanical
tripwire, and the only question is whether the flagged facts are what the
mission asked for.

Mission:
{mission}

Tripwires fired:
{fired}

The change, as a diffstat and then a truncated diff:
{diff}

A run sent to delete dead code will delete files; a run sent to refactor one
module has no business deleting six. Judge ONLY whether the flagged facts are
the mission's own work. When the mission does not clearly call for them, say
escalate — a human look costs a tap, a wrong landing costs an evening.

Reply with ONE JSON object and nothing else:
{{"verdict": "mission_work|escalate", "rationale": "<one sentence>"}}
"""


def judge_tripwires(cfg: dict | None, mission: str, fired: list[str],
                    diff: str, turn=None) -> dict:
    """Ask a model whether the tripwired facts are the mission's own work.

    Tripwires are mechanical and know nothing of intent: a run titled
    "dead-code deletion" deleted six files and the deletion tripwire escalated
    the exact work the run was dispatched to do (run 35). Code coordinates and
    agents judge (DESIGN principle 6) — so the tripwire now yields to a
    judgment turn before it yields to the phone.

    Every failure lands on "escalate": no nameable judge, an unparsable reply,
    a dead turn. The tripwire's old behaviour is the floor, never the ceiling.
    """
    from orchestra import observer  # SEAM (W-0166): observer imports this module

    if not (mission or "").strip():
        return {"verdict": "escalate", "rationale": "no mission to judge against"}
    try:
        profile = observer.observer_profile(cfg or {})
    except Exception as exc:
        return {"verdict": "escalate", "rationale": f"no judge profile: {exc}"}
    prompt = JUDGE_INSTRUCTIONS.format(
        mission=mission.strip()[:4000],
        fired="\n".join(f"- {f}" for f in fired),
        diff=diff[:JUDGE_DIFF_MAX_CHARS])
    try:
        meta: dict = {}
        if turn is not None:
            reply = turn(profile, prompt)
        else:
            reply = observer.model_turn(profile, prompt, layer="merge",
                                        meta=meta,
                                        project_id=(cfg or {}).get("project_id"))
    except Exception as exc:
        return {"verdict": "escalate", "rationale": f"judge turn failed: {exc}"}
    found = observer.last_json_object(reply or "", "verdict") or {}
    verdict = str(found.get("verdict", "")).strip().lower()
    if verdict != "mission_work":
        verdict = "escalate"
    rationale = str(found.get("rationale") or "no rationale given")[:2000]
    # The turn's own row reads the verdict it produced; an escalate carries
    # its turn id back into the card ``_escalate`` files.
    observer.note_turn(None, meta.get("turn_id"), f"{verdict}: {rationale}")
    out = {"verdict": verdict, "rationale": rationale}
    if meta.get("turn_id"):
        out["turn_id"] = meta["turn_id"]
    return out


# --- the merge --------------------------------------------------------------

def revert_command(root: Path, sha: str) -> str:
    """The escape hatch for a landed merge (DESIGN §9). One spelling, because
    the report that names it and the receipt that carries the sha are read by
    different modules."""
    return f"git -C {root} revert -m 1 {sha}"


def blank_result(base: str, branch: str, checks_skipped: bool = True) -> dict:
    """The result shape, before anything has happened to it."""
    return {"ok": False, "stage": "rebase", "escalation": None,
            "base": base, "branch": branch, "commit": None, "files_changed": [],
            "checks": [], "checks_skipped": checks_skipped, "tripwires": [],
            "conflicts": [], "revert_command": None,
            "branch_deleted": False, "refresh": None, "note": None,
            "kept_ref": None, "dropped": [], "dirty": [],
            "tripwire_verdict": None}


def _ignored_by_base(root: Path, paths: list[str]) -> list[str]:
    """Of ``paths``, the ones the BASE checkout declares are not source.

    check-ignore runs against ``root`` — the checkout the merge lands into —
    so the rules consulted are the base branch's own .gitignore, not the run
    branch's. Asking git beats carrying a list of paths to keep in sync.
    """
    if not paths:
        return []
    asked = subprocess.run(["git", "check-ignore", "--stdin"], cwd=str(root),
                           input="\n".join(paths), capture_output=True, text=True)
    return sorted({ln for ln in asked.stdout.splitlines() if ln})


def rebase_dropping_ignored(root: Path, scratch: Path, base: str) -> tuple[bool, list[str], list[str]]:
    """Rebase the run branch, dropping anything the base does not track.

    The host checkpoints with `git add -A`, so it sweeps up whatever sits in
    its worktree — including a live service's own record store. That service
    rewrites those files continuously while the run holds its branch, so both sides edit the
    same append-only log and the rebase conflicts EVERY time. Nothing raced:
    two processes own one file, and retrying cannot help. That was the
    recurring "did not land on main" card, and it was never a decision worth
    a human's attention.

    The base branch already says what is not source, so a run may not land it.
    The tip is cleaned first, and each replayed commit is cleaned as it
    conflicts — the run's history still carries the file even after the tip
    stops doing so.

    Dropping is safe in the one direction that matters: the file stays on
    disk and the service that owns it keeps writing. Only the run's stale
    snapshot goes. A conflict in anything the base DOES track is untouched
    and still reaches the human, because that is a judgment nobody can
    automate.

    Returns (ok, dropped paths, conflicted paths).
    """
    dropped = _ignored_by_base(
        root, _lines(["diff", "--name-only", f"{base}...HEAD"], scratch))
    if dropped:
        _git(["rm", "-q", "--cached", "--ignore-unmatch", "--", *dropped], scratch)
        _git(["commit", "-q", "-m", "Drop files the base branch does not track\n\n"
              + "\n".join(f"- {p}" for p in dropped)], scratch)

    rebase = _git(["rebase", "--empty=drop", base], scratch, check=False)
    # One pass per replayed commit; the bound is a backstop, not a policy.
    for _ in range(MAX_REPLAY_DROPS):
        if rebase.returncode == 0:
            return True, dropped, []
        conflicts = _lines(["diff", "--name-only", "--diff-filter=U"], scratch) \
            or _lines(["diff", "--name-only", "--diff-filter=UDA", "--cached"], scratch)
        resolvable = _ignored_by_base(root, conflicts)
        if not conflicts or set(conflicts) - set(resolvable):
            return False, dropped, conflicts
        _git(["rm", "-q", "--force", "--ignore-unmatch", "--", *resolvable], scratch)
        for path in resolvable:
            if path not in dropped:
                dropped.append(path)
        rebase = subprocess.run(["git", "rebase", "--continue"], cwd=str(scratch),
                                capture_output=True, text=True,
                                env={**os.environ, "GIT_EDITOR": "true"})
    return rebase.returncode == 0, dropped, _lines(
        ["diff", "--name-only", "--diff-filter=U"], scratch)


def dirty_paths(root: Path, base: str) -> list[str]:
    """Uncommitted changes in the base checkout, ignoring untracked files.

    Untracked files are excluded deliberately: a build directory or a scratch
    note is not work in flight, and refusing every merge because one exists
    would make the guard useless within a day. Modified and staged tracked
    files are the ones a merge would strand.

    Returns [] when the checkout is not on ``base`` at all -- then it is not
    the tree this merge is about to move.
    """
    if _out(["rev-parse", "--abbrev-ref", "HEAD"], root) != base:
        return []
    # NOT _lines(): it strips the whole output, which eats the leading space of
    # a " M path" status line and takes the first character of the path with it.
    out = _git(["status", "--porcelain", "--untracked-files=no"], root).stdout
    return sorted(line[3:] for line in out.split("\n") if len(line) > 3)


def merge_run(root: Path, branch: str, item_id: str | None = None,
              settings: dict | None = None, mission: str = "",
              judge=None) -> dict:
    """Verify ``branch`` and land it on the base branch. Returns the result dict.

    Never touches the owner's working tree: the rebase, the checks and the
    merge commit all happen in a scratch worktree, and the base branch ref
    moves by compare-and-swap.
    """
    root = Path(root).resolve()
    cfg = merge_cfg(settings)
    base = cfg["base"] or _out(["symbolic-ref", "--short", "HEAD"], root)
    result = blank_result(base, branch, checks_skipped=not cfg["checks"])
    base_sha = _out(["rev-parse", base], root)

    # A dirty base checkout is the NORMAL state of a repo whose owner works in
    # it, so refusing on any dirt at all escalated every run forever -- nine
    # cards, one kind, and the resolver sent to clear one hit the same guard
    # and filed another (runs 60/61/62, owner 2026-08-19). The merge cannot
    # touch those files anyway: it happens in a scratch worktree and the ref
    # moves by update-ref. Only an OVERLAP between the owner's edits and the
    # merged files is a real problem -- that is the one case _refresh_base_-
    # checkout must refuse, stranding them on a stale index. So the check
    # waits until the merged file list is known; see below.
    result["dirty"] = dirty_paths(root, base)

    holder = Path(tempfile.mkdtemp(prefix="orchestra-merge-"))
    scratch = holder / "wt"
    try:
        _git(["worktree", "add", "--detach", str(scratch), branch], root)

        rebased, dropped, conflicts = rebase_dropping_ignored(root, scratch, base)
        result["dropped"] = dropped
        if not rebased:
            result["conflicts"] = conflicts
            _git(["rebase", "--abort"], scratch, check=False)
            return _escalate(result, "rebase",
                             f"rebase onto {base} conflicted; resolve by hand")
        rebased_sha = _out(["rev-parse", "HEAD"], scratch)
        result["files_changed"] = _lines(
            ["diff", "--name-only", base_sha, rebased_sha], scratch)

        # An overlap between the owner's edits and the merged files no
        # longer stops the merge. It used to file a card whose only options
        # were Retry (does nothing until the owner acts) and Leave it (the
        # branch piles up) -- a notification with no resolution in it, which
        # is worse than no notification (owner, 2026-08-19). The merge is
        # safe regardless: it happens in a scratch worktree, the ref moves by
        # update-ref, and _refresh_base_checkout REFUSES to overwrite a local
        # edit, so the owner keeps their work and their pre-merge tree and
        # gets a one-line refresh command. require_clean = true restores the
        # old refusal for anyone who wants the merge to wait.
        overlap = sorted(set(result["dirty"]) & set(result["files_changed"]))
        result["overlap"] = overlap
        if cfg["require_clean"] and overlap:
            shown = ", ".join(overlap[:5]) + (f", plus {len(overlap) - 5} more"
                                              if len(overlap) > 5 else "")
            return _escalate(result, "dirty",
                             f"your uncommitted edits to {shown} overlap this "
                             f"merge; commit or stash those files and merge "
                             f"again")

        result["checks"], checks_ok = run_checks(cfg, scratch)
        if not checks_ok:
            failed = result["checks"][-1]["name"]
            return _escalate(result, "checks", f"declared check '{failed}' failed")

        result["tripwires"] = tripwires(cfg, scratch, base_sha, rebased_sha)
        if result["tripwires"]:
            verdict = {"verdict": "escalate", "rationale": "judging is off"}
            if cfg["judge_tripwires"]:
                shape = (_out(["diff", "--stat", base_sha, rebased_sha],
                              scratch)[:JUDGE_STAT_MAX_CHARS]
                         + "\n\n"
                         + _out(["diff", base_sha, rebased_sha],
                                scratch)[:JUDGE_DIFF_MAX_CHARS])
                verdict = (judge or judge_tripwires)(
                    settings, mission, result["tripwires"], shape)
            result["tripwire_verdict"] = verdict
            if verdict["verdict"] != "mission_work":
                return _escalate(result, "tripwires",
                                 "; ".join(result["tripwires"])
                                 + f" — judge: {verdict['rationale']}"
                                 + (f" (judge turn #{verdict['turn_id']})"
                                    if verdict.get("turn_id") else ""))
            # The facts stay on the result: a landed merge still SAYS it
            # deleted six files, it just no longer asks permission to have
            # done what the mission ordered.

        subject = f"orchestra: merge {branch}" + (f" ({item_id})" if item_id else "")
        # A base that moved between the rebase and the swap is a RACE, not a
        # conflict: two runs finished close together and the other landed
        # first. The compare-and-swap is right to refuse — but the answer is
        # to rebase onto the new base and try again, not to ask a human
        # whether to retry (owner, 2026-08-14; principle 6). Only a real
        # conflict, or losing the race repeatedly, is worth their attention.
        merge_sha = None
        for attempt in range(SWAP_ATTEMPTS):
            # `git merge --no-ff` of a branch the base ALREADY contains creates
            # no commit at all: it says "Already up to date" and leaves HEAD on
            # the base. Reading HEAD afterwards then reported whatever commit
            # happened to be there — on run 65 an unrelated owner commit, with
            # a `revert -m 1` line pointed at it (I-0077). Ask ancestry first,
            # and let merge_sha stay None when the run created nothing.
            if _git(["merge-base", "--is-ancestor", rebased_sha, base_sha],
                    scratch, check=False).returncode == 0:
                break
            _git(["checkout", "--detach", base_sha], scratch)
            _git(["merge", "--no-ff", "-m", subject, rebased_sha], scratch)
            merge_sha = _out(["rev-parse", "HEAD"], scratch)
            swap = _git(["update-ref", f"refs/heads/{base}", merge_sha, base_sha],
                        scratch, check=False)
            if swap.returncode == 0:
                break
            moved = _out(["rev-parse", base], root)
            if moved == base_sha or attempt == SWAP_ATTEMPTS - 1:
                return _escalate(result, "merge", (swap.stderr or "").strip()
                                 or "the base ref could not be updated")
            result.setdefault("races", []).append(
                {"was": base_sha, "now": moved})
            base_sha = moved
            _git(["checkout", "--detach", branch], scratch)
            again = _git(["rebase", base_sha], scratch, check=False)
            if again.returncode != 0:
                result["conflicts"] = _lines(
                    ["diff", "--name-only", "--diff-filter=U"], scratch)
                _git(["rebase", "--abort"], scratch, check=False)
                return _escalate(result, "rebase",
                                 f"the base moved to {moved[:12]} and rebasing "
                                 "onto it conflicts")
            rebased_sha = _out(["rev-parse", "HEAD"], scratch)
    finally:
        _git(["worktree", "remove", "--force", str(scratch)], root, check=False)
        shutil.rmtree(holder, ignore_errors=True)

    result["ok"] = True
    result["stage"] = "merged"
    result["commit"] = merge_sha
    if merge_sha is None:
        # No commit, so no revert command: an escape hatch aimed at a commit
        # this run did not create either errors or destroys a bystander's work.
        result["note"] = f"already on {base}; nothing to merge"
        result["refresh"] = {"status": "skipped", "command": None,
                             "why": f"{base} did not move; nothing to refresh"}
    else:
        result["refresh"] = _refresh_base_checkout(root, base, base_sha, merge_sha)
        if result["refresh"]["command"]:
            overlapped = result.get("overlap") or []
            kept = (f" Your edits to {', '.join(overlapped[:3])} are untouched."
                    if overlapped else "")
            result["note"] = (f"{result['refresh']['why']}; refresh it with "
                              f"`{result['refresh']['command']}`.{kept}")
        result["revert_command"] = revert_command(root, merge_sha)
    # Anchor the run's own commits before the branch name goes away. A run that
    # committed its own work leaves nothing for the checkpoint to record, so
    # the branch WAS the only pointer and `orchestra show --changes` lost the
    # diff the moment it was deleted. A ref costs 41 bytes, keeps the objects
    # off the garbage collector, and survives deletion by design.
    head_sha = _out(["rev-parse", "--verify", f"{branch}^{{commit}}"], root).strip()
    if head_sha:
        ref = f"refs/orchestra/{branch.rsplit('/', 1)[-1]}"
        if _git(["update-ref", ref, head_sha], root, check=False).returncode == 0:
            result["kept_ref"] = ref
    # Branch kept whenever anything escalated; deleted only here. A branch
    # still checked out in the run's own worktree simply stays.
    result["branch_deleted"] = _git(["branch", "-D", branch], root,
                                    check=False).returncode == 0
    return result


IN_PROGRESS_MARKERS = ["rebase-merge", "rebase-apply", "MERGE_HEAD",
                       "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG"]


def _refresh_base_checkout(root: Path, base: str, old_sha: str, new_sha: str) -> dict:
    """Bring the owner's checkout to the merged content, or say why we did not.

    Moving the base ref under a checkout that sits on it leaves that tree
    holding pre-merge content — git would report every merged file as deleted.
    ``read-tree -m -u`` is the only acceptable refresh: it updates tracked
    files and REFUSES when a local edit would be overwritten. Nothing here may
    force it, and no fallback to checkout -f / reset --hard exists: the project
    root routinely holds the owner's uncommitted work.

    Returns {"status": refreshed | skipped | refused, "why", "command"};
    ``command`` is the hand-runnable refresh, set whenever we did not do it.

    ponytail: only the project root is checked, not every linked worktree — a
    second checkout of the base branch is not a shape Orchestra creates.
    """
    cmd = f"git -C {root} read-tree -m -u {old_sha} {new_sha}"
    if _out(["rev-parse", "--abbrev-ref", "HEAD"], root) != base:
        return {"status": "skipped", "command": None,
                "why": f"{root} is not on {base}; nothing to refresh"}
    git_dir = Path(_out(["rev-parse", "--absolute-git-dir"], root))
    busy = [m for m in IN_PROGRESS_MARKERS if (git_dir / m).exists()]
    if busy:
        return {"status": "skipped", "command": cmd,
                "why": f"{root} is mid-operation ({busy[0]}) and still holds "
                       f"the pre-merge tree"}
    r = _git(["read-tree", "-m", "-u", old_sha, new_sha], root, check=False)
    if r.returncode != 0:
        return {"status": "refused", "command": cmd,
                "why": f"{root} keeps the pre-merge tree: local edits would be "
                       f"overwritten ({(r.stderr or r.stdout).strip().splitlines()[0]})"}
    return {"status": "refreshed", "command": None,
            "why": f"{root} now holds the merged content; uncommitted work kept"}


def _escalate(result: dict, stage: str, reason: str) -> dict:
    result["ok"] = False
    result["stage"] = stage
    result["escalation"] = reason
    return result


# --- the completion seam (W-0174: the supervisor merges, not a human) --------

def run_mission(run: dict) -> str:
    """What the run was dispatched to do, for the tripwire judge.

    The brief's Mission section is the authoritative text; the run title is
    the fallback when the brief file has aged out. A missing mission is an
    empty string, and the judge treats that as unjudgeable — escalate.
    """
    brief_path = run.get("brief_path")
    if brief_path:
        try:
            text = Path(brief_path).read_text(encoding="utf-8")
            if "## Mission" in text:
                section = text.split("## Mission", 1)[1]
                return section.split("\n## ", 1)[0].strip()[:4000]
        except OSError:
            pass
    return str(run.get("title") or "").strip()


def _recovered_landing(root: Path, cfg: dict, run: dict) -> dict | None:
    """Rebuild a successful result when landing won but its receipt did not.

    ``merge_run`` updates the base before it anchors and deletes the run branch.
    A missing branch is therefore ambiguous until either the uniquely named
    merge commit is on the base, or the run's checkpoint is already contained
    by it. In every other case the normal landing path must fail loudly.
    """
    try:
        if _git(["rev-parse", "--git-dir"], root, check=False).returncode != 0:
            return None
    except OSError:
        return None
    branch = run["branch"]
    branch_ref = branch if branch.startswith("refs/") else f"refs/heads/{branch}"
    if _git(["rev-parse", "--verify", f"{branch_ref}^{{commit}}"],
            root, check=False).returncode == 0:
        return None

    settings = merge_cfg(cfg)
    base = settings["base"] or _out(["symbolic-ref", "--short", "HEAD"], root)
    subject = f"orchestra: merge {branch}" \
        + (f" ({run['ref']})" if run.get("ref") else "")
    merge_sha = None
    matches = _lines(
        ["log", "--first-parent", "--format=%H%x09%s", "--fixed-strings",
         f"--grep={subject}", base], root)
    for line in matches:
        sha, separator, found_subject = line.partition("\t")
        if separator and found_subject == subject \
                and len(_out(["show", "-s", "--format=%P", sha], root).split()) >= 2:
            merge_sha = sha
            break

    kept_ref = f"refs/orchestra/{branch.rsplit('/', 1)[-1]}"
    kept = _git(["rev-parse", "--verify", f"{kept_ref}^{{commit}}"],
                root, check=False)
    checkpoint = run.get("checkpoint_commit") or (
        kept.stdout.strip() if kept.returncode == 0 else None)
    if merge_sha is None and (not checkpoint or _git(
            ["merge-base", "--is-ancestor", checkpoint, base],
            root, check=False).returncode != 0):
        return None

    result = blank_result(base, branch, checks_skipped=not settings["checks"])
    result.update({
        "ok": True,
        "stage": "merged",
        "commit": merge_sha,
        "files_changed": (_lines(
            ["diff", "--name-only", f"{merge_sha}^1", merge_sha], root)
            if merge_sha else []),
        "checks": [
            {"name": name, "command": command, "ok": True, "exit_code": 0,
             "output": "landing receipt recovered; original output unavailable"}
            for name, command in (settings["checks"] or {}).items()
        ],
        "branch_deleted": True,
        "refresh": {"status": "skipped", "command": None,
                    "why": "landing receipt recovered from the base history"},
        "kept_ref": kept_ref if kept.returncode == 0 else None,
    })
    if merge_sha:
        result["revert_command"] = revert_command(root, merge_sha)
    else:
        result["note"] = f"already on {base}; nothing to merge"
    return result


def _record_landing(con, run: dict, status: str | None,
                    note: str | None = None, commit: str | None = None) -> None:
    """Stamp the landing receipt and keep its human reason on the result row.

    The receipt is the WHOLE outbound interface of this module (source boundary): verdict, merge commit, and the human line, all on the run
    row. Whoever reports a run to its source reads them there — this module
    knows git and nothing about a board.

    ``status=None`` records the note WITHOUT closing the receipt: an
    exception is not a landing verdict, but its reason still belongs on the
    row the reader gets."""
    row = con.execute("SELECT summary FROM runs WHERE id=?", (run["id"],)).fetchone()
    if row is None:
        return
    summary = row["summary"]
    if note and note not in (summary or ""):
        if status != "ok":
            summary = f"{note}\n\n{summary}" if summary else note
        else:
            summary = f"{summary}\n\n{note}" if summary else note
    con.execute(
        "UPDATE runs SET landing_status=COALESCE(?, landing_status), "
        "landing_commit=COALESCE(?, landing_commit), summary=? WHERE id=?",
        (status, commit, (summary or "")[:2000] or None, run["id"]))
    con.commit()


def _consume_landing(con, cfg: dict, result, status: str,
                     forced: bool = False) -> str | None:
    """Run landing policy with an explicit effective execution status."""
    run = dict(result)
    try:
        return _land(con, cfg, run, status, forced=forced)
    except Exception as exc:
        note = f"Merge failed: {exc}"
        print(f"orchestra: run {result['id']} {note}", file=sys.stderr)
        try:
            _thread(con, int(result["id"]), note)
        except Exception:  # the database is the last thing left; say nothing
            pass
        # This was not a deliberate landing verdict. The base may already
        # have moved before an after-step failed, so leave the receipt open;
        # daemon replay can prove success from Git or retry the unsettled step.
        # The reason still lands on the row, where the result reader is.
        try:
            _record_landing(con, run, None, note)
        except Exception:
            pass
        return note


def at_completion(con, cfg: dict, result) -> str | None:
    """SEAM: consume a terminal result and land a verified run's branch.

    ``result`` is the terminal run row. ``supervise.py`` calls this once at
    finalization, AFTER
    ``release_worktree`` — git refuses to delete a branch a worktree still
    has checked out (W-0172, DESIGN §9 "Ordering").

    Returns one line for the run summary, or ``None`` when there was nothing
    to land. A merge is never worth losing a finalization, so every failure
    in here becomes a recorded note instead of an exception.
    """
    run = dict(result)
    persisted = con.execute(
        "SELECT landing_status FROM runs WHERE id=?", (run["id"],)).fetchone()
    if persisted is not None and persisted["landing_status"] is not None:
        return None
    return _consume_landing(con, cfg, run, run["status"])


def retry_landing(con, cfg: dict, result) -> str | None:
    """Retry landing for the persisted terminal row, whatever its outcome.

    A human explicitly chose to retry the kept branch. That changes landing
    policy, not the execution result recorded on the row — and it overrides
    ``[merge] enabled = false``: the switch gates the automatic path, never
    the owner's own hand.
    """
    return _consume_landing(con, cfg, result, "done", forced=True)


def _land(con, cfg: dict, run: dict, status: str,
          forced: bool = False) -> str | None:
    if status != "done" or not run.get("branch"):
        _record_landing(con, run, "ok")
        return None  # only a verified success lands; a shared-tree run has no branch
    branch = run["branch"]
    if not forced and not merge_cfg(cfg).get("enabled", True):
        # The pure-runner contract: the run ENDS at its branch and this
        # receipt. Whatever lands it — a human, an external agentic lander —
        # reads landing_status 'skipped' and the kept branch, and posts its
        # own facts. Nothing is merged, nothing is deleted.
        note = (f"Landing is off ([merge] enabled = false): branch {branch} "
                "is kept for an external landing")
        _record_landing(con, run, "skipped", note)
        return note
    root = project.root_for(con, run)
    result = _recovered_landing(root, cfg, run)
    if result is None:
        try:
            result = merge_run(root, branch, item_id=run.get("ref"),
                               settings=cfg, mission=run_mission(run))
        except RuntimeError as exc:
            # git itself refused — most importantly the compare-and-swap losing to
            # a base that moved under us, which is meant to fail LOUDLY. Shape it
            # as an escalation so it blocks the item and reaches the phone, rather
            # than becoming a note under an item the sweeper moves to review.
            result = _escalate(blank_result(merge_cfg(cfg)["base"] or "the base",
                                            branch), "merge", str(exc))
    if result["ok"] and hasattr(nod, "withdraw_merge_cards"):
        # A landed branch answers its own escalation: the phone card filed
        # for an earlier failure of this run is stale the moment the retry
        # (or a resolver) lands it. hasattr: this branch runs before nod.py
        # grows the withdrawal; nothing here may break finalization either.
        try:
            nod.withdraw_merge_cards(
                con, cfg, int(run["id"]),
                note=(f"landed as {result['commit'][:12]}" if result["commit"]
                      else f"already on {result['base']}; nothing to merge"))
        except Exception as exc:
            print(f"orchestra: run {run['id']} stale merge card not withdrawn: "
                  f"{exc}", file=sys.stderr)
    request_id = None
    if not result["ok"]:
        # ANY landing failure's FIRST escalation is a question with an
        # obvious answer: dispatch a resolver. The rule started as an
        # enumeration — rebase and merge only, from runs 99 and 104 — and the
        # checks stage leaked straight through it and rang the phone twice in
        # one evening (runs 163 and 165, 2026-08-20): the class the doctrine
        # bans, reborn by enumeration. The rule is now the default for every
        # stage but one: `dirty` is the human's own checkout state, and an
        # automatic move there would touch their in-flight work. The card is
        # reserved for the resolver's own failure — a real judgment point.
        resolved_id = None
        if result["stage"] != "dirty" and not _is_resolver(run):
            from orchestra import resolver  # lazy, matching nod.py's import
            try:
                resolved_id = resolver.dispatch_resolver(
                    con, cfg, int(run["id"]),
                    f"auto: {result['stage']} failure on {result['branch']}")
            except Exception as exc:
                print(f"orchestra: run {run['id']} auto-resolver not dispatched: "
                      f"{exc}", file=sys.stderr)
        if resolved_id is not None:
            result["auto_resolver"] = resolved_id
        else:
            request_id = _file_card(con, cfg, run, result)
    _thread(con, int(run["id"]), _report_text(run, result, request_id))
    note = _note(run, result, request_id)
    # The verdict is deliberate and final either way, so the receipt closes
    # here and nothing waits on a remote system being reachable. Delivery is
    # the consumer's cursor, not this path's retry (source boundary).
    _record_landing(con, run, "ok" if result["ok"] else "failed", note,
                    commit=result["commit"])
    return note


def _is_resolver(run: dict) -> bool:
    """A resolver resolving a resolver is a loop, not persistence: the second
    failure is the human's to judge."""
    return str(run.get("requested_by") or "") == "resolver" or         str(run.get("title") or "").startswith("Resolve the landing of")


def _thread(con, run_id: int, body: str) -> None:
    """The run's own thread. This is the whole report for a hand-dispatched
    run, which has no source item to post to."""
    existing = [row["body"] for row in con.execute(
        "SELECT body FROM messages WHERE run_id=? AND kind='merge'", (run_id,))]
    success = body.startswith(f"run {run_id} landed `") or (
        body.startswith(f"run {run_id}: `") and "nothing to merge" in body)
    if body in existing or (success and any(
            old.startswith(f"run {run_id} landed `") or (
                old.startswith(f"run {run_id}: `") and "nothing to merge" in old)
            for old in existing)):
        return
    con.execute("INSERT INTO messages(run_id, sender, body, kind, created_at) "
                "VALUES(?, 'orchestra', ?, 'merge', ?)", (run_id, body, db.now()))
    con.commit()


def _checks_line(result: dict) -> str:
    if result["checks_skipped"]:
        return "none declared"
    if not result["checks"]:
        return "not run"
    return "; ".join(
        c["name"] + (" ok" if c["ok"] else f" FAILED (exit {c['exit_code']})")
        for c in result["checks"])


def _report_text(run: dict, result: dict, request_id: str | None) -> str:
    """The comment that goes in the source's thread and the run thread.

    DESIGN §9 names its contents: merge commit, files changed, check results,
    and the revert command.
    """
    files = result["files_changed"]
    lines = [f"- files changed ({len(files)}): "
             + (", ".join(f"`{f}`" for f in files[:20])
                + (" …" if len(files) > 20 else "") if files else "none"),
             f"- checks: {_checks_line(result)}"]
    if result["ok"]:
        landed = result["commit"] is not None
        head = (f"run {run['id']} landed `{result['branch']}` on "
                f"`{result['base']}`." if landed else
                f"run {run['id']}: `{result['branch']}` is already on "
                f"`{result['base']}`; nothing to merge.")
        lines = [f"- merge commit: `{result['commit']}`" if landed else
                 "- merge commit: none (this run created no commit)",
                 *lines,
                 f"- branch: {'deleted' if result['branch_deleted'] else 'kept'}",
                 f"- checkout: {(result['refresh'] or {}).get('why', 'not reported')}",
                 *([f"- revert: `{result['revert_command']}`"] if landed else [])]
    else:
        head = (f"run {run['id']} could not land `{result['branch']}` on "
                f"`{result['base']}` — escalated at {result['stage']}.")
        lines = [f"- reason: {result['escalation']}",
                 *(["- conflicted files: "
                    + ", ".join(f"`{f}`" for f in result["conflicts"])]
                   if result["conflicts"] else []),
                 *lines,
                 f"- branch `{result['branch']}` is kept; retry by hand with "
                 f"`orchestra merge {result['branch']}`"]
        if request_id:
            lines.append(f"- decision card: {request_id}")
    return "\n".join([head, "", *lines])[:19000]


def _note(run: dict, result: dict, request_id: str | None) -> str:
    """The one summary line. The detail is in the run thread and on the item."""
    if result["ok"] and result["commit"] is None:
        return (f"{result['branch']} was already on {result['base']}; nothing "
                f"to merge, no commit made")
    if result["ok"]:
        return (f"Merged {result['branch']} into {result['base']} as "
                f"{result['commit'][:12]}; revert with "
                f"`{result['revert_command']}`")
    tail = f" Nod card {request_id}." if request_id else ""
    if not tail and result.get("auto_resolver"):
        tail = (f" Resolver run {result['auto_resolver']} dispatched "
                f"automatically; a card follows only if it fails.")
    return (f"Merge escalated at {result['stage']}: {result['escalation']}. "
            f"Branch {result['branch']} kept." + tail)


def _file_card(con, cfg: dict, run: dict, result: dict) -> str | None:
    """A merge escalation is a needs-you event (DESIGN §8/§9).

    The card offers the three options §9 names — retry, dispatch a resolver,
    leave it — and its request id is recorded in ``nod_requests`` so the
    answer can be mirrored later. Answering it is not built here.
    """
    channels = nod.from_cfg(cfg)
    if channels is None:
        return None  # the human loop is off; the source item still carries it
    detail = [f"`{result['branch']}` did not land on `{result['base']}`.",
              f"Stage `{result['stage']}`: {result['escalation']}"]
    if result["conflicts"]:
        detail.append("Conflicted files:\n"
                      + "\n".join(f"- `{f}`" for f in result["conflicts"]))
    detail.append(f"The branch is kept. `orchestra merge {result['branch']}` "
                  f"retries it by hand.")
    # A tripwire card and a conflict card stopped looking identical when
    # merge_conflict grew `stage`; pass it only once it exists, so this
    # branch runs against a nod.py that has not grown it yet.
    staged = {}
    if "stage" in inspect.signature(nod.merge_conflict).parameters:
        staged["stage"] = result["stage"]
    try:
        created = nod.merge_conflict(
            channels, "\n\n".join(detail), con=con, run_id=int(run["id"]),
            ref=run.get("ref"),
            title=f"{result['branch']} did not land on {result['base']}",
            summary=result["escalation"][:200], **staged)
    except (nod.NodError, nod.NodChannelError) as exc:
        print(f"orchestra: run {run['id']} merge escalation not filed: {exc}",
              file=sys.stderr)
        return None
    return created.get("request_id")
