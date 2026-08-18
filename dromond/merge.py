"""Land a run branch on the base branch (DESIGN §9, "Landing the work").

A base that moves between the rebase and the ref swap is a race between two
runs landing at once; ``SWAP_ATTEMPTS`` says how many times to rebase onto the
new base and try again before it stops being self-correcting and becomes the
human's problem.

Verification runs in authority order — declared checks, mechanical tripwires,
then a cheap agent review — and stops at the first failure. The merge itself
happens in a THROWAWAY WORKTREE: the base branch ref is updated with
``git update-ref``, so the owner's checkout, which routinely holds
uncommitted work, is never touched.

Self-contained by design: the git helpers live here, not in worktree.py.

Per-project configuration, in ``.dromond/config.toml``::

    [merge]
    base = "main"            # default: the branch the project root has checked out
    max_files = 50           # tripwire: files touched by the diff
    max_lines = 2000         # tripwire: insertions + deletions
    allow_deletions = false  # tripwire: any deleted file
    project_paths = ["src"]  # tripwire: a touched file outside these prefixes
    check_timeout = 1800     # per-check seconds
    require_clean = true     # refuse to land while the base checkout is dirty
    judge_tripwires = true   # a model judges tripwired facts against the mission first
    resolver_profile = "big" # staffs the resolver run a merge escalation can
                             # dispatch (default: the highest-priority tier-3 profile)

    [merge.checks]           # declared checks, run in declared order
    test = "uv run python -m unittest discover -s tests"
    lint = "ruff check ."

Result shape (a plain dict, ready for the caller to post to Work)::

    {"ok", "stage", "escalation", "base", "branch", "commit", "files_changed",
     "checks", "checks_skipped", "tripwires", "review", "conflicts",
     "revert_command", "branch_deleted", "refresh", "note", "kept_ref",
     "dropped", "dirty"}

``stage`` is where the run stopped: dirty | rebase | checks | tripwires |
review | merged. ``refresh`` reports what happened to a checkout sitting on the base
branch (refreshed | skipped | refused, always with a reason). ``merge_run``
never posts to Work and never resolves a conflict.

``at_completion`` is the other half (W-0174): the seam ``supervise.py`` calls
when a run reaches ``done``, which lands the branch and then *reports* —
Work thread, item transition, Nod card. Nothing in it may break finalization.
"""
import inspect
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from dromond import db, dispatch, messaging, nod, paths, project, work_client
from dromond.work_client import WorkError

SWAP_ATTEMPTS = 3
MAX_REPLAY_DROPS = 100

DEFAULTS = {
    "base": None,
    "checks": {},
    "max_files": 50,
    "max_lines": 2000,
    "allow_deletions": False,
    "project_paths": [],
    "check_timeout": 1800,
    # On by default. A run landing while the owner edits is rare and confusing;
    # turn it off for a checkout nobody works in by hand.
    "require_clean": True,
    # Tripwires yield to a judgment turn before they yield to the phone: a run
    # dispatched to delete dead code must not escalate its own deletions. Off
    # means every tripwire escalates, as before.
    "judge_tripwires": True,
    # Which profile a "Dispatch a resolver" answer staffs (resolver.py). None
    # means the highest-priority enabled tier-3 profile.
    "resolver_profile": None,
}
CHECK_OUTPUT_MAX_CHARS = 4000
# ponytail: whole diff into the review prompt, capped. Chunk it only if real
# items start exceeding the cap.
REVIEW_DIFF_MAX_CHARS = 100_000


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
You are Dromond's merge judge. A finished run's branch tripped a mechanical
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
    from dromond import observer  # SEAM (W-0166): observer imports this module

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
        reply = (turn or observer.model_turn)(profile, prompt)
    except Exception as exc:
        return {"verdict": "escalate", "rationale": f"judge turn failed: {exc}"}
    found = observer.last_json_object(reply or "", "verdict") or {}
    verdict = str(found.get("verdict", "")).strip().lower()
    if verdict != "mission_work":
        verdict = "escalate"
    return {"verdict": verdict,
            "rationale": str(found.get("rationale") or "no rationale given")[:2000]}


def agent_review(diff: str, criteria: str) -> dict:
    """Stage 3 SEAM: cheap agent review of the diff against acceptance criteria.

    Wire this to a short-lived cheap run (dispatch is being restructured in
    parallel) and keep the returned shape: ``ok`` gates the merge, ``verdict``
    is one of pass | fail | unwired, ``notes`` is the human-readable reason
    that goes into the Work comment.

    ponytail: stub verdict, no model call — a non-blocking pass, so a merge
    never depends on an unwired stage. The caller can inject its own
    reviewer via merge_run(review=...).
    """
    return {"ok": True, "verdict": "unwired",
            "notes": "agent review not wired to dispatch yet"}


# --- the merge --------------------------------------------------------------

def blank_result(base: str, branch: str, checks_skipped: bool = True) -> dict:
    """The result shape, before anything has happened to it."""
    return {"ok": False, "stage": "rebase", "escalation": None,
            "base": base, "branch": branch, "commit": None, "files_changed": [],
            "checks": [], "checks_skipped": checks_skipped, "tripwires": [],
            "review": None, "conflicts": [], "revert_command": None,
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

    A run commits with `git add -A`, so it sweeps up whatever sits in its
    worktree — including a service's live record store. Work rewrites those
    files continuously while the run holds its branch, so both sides edit the
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


def merge_run(root: Path, branch: str, criteria: str = "",
              item_id: str | None = None, review=None,
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

    # A dirty base checkout is the owner mid-edit. The merge itself cannot
    # touch those files -- it happens in a scratch worktree and the ref moves
    # by update-ref -- but landing anyway moves the ground under someone who is
    # working: the refresh then declines, correctly, and leaves them on a tree
    # that is older than the branch just merged, with no obvious reason why.
    # Refusing is one owner action away from resolved; landing is a puzzle.
    dirty = dirty_paths(root, base)
    if cfg["require_clean"] and dirty:
        result["dirty"] = dirty
        shown = ", ".join(dirty[:5]) + (f", plus {len(dirty) - 5} more"
                                        if len(dirty) > 5 else "")
        return _escalate(result, "dirty",
                         f"{base} has uncommitted changes ({shown}); "
                         "commit or stash them and merge again")

    holder = Path(tempfile.mkdtemp(prefix="dromond-merge-"))
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
                                 + f" — judge: {verdict['rationale']}")
            # The facts stay on the result: a landed merge still SAYS it
            # deleted six files, it just no longer asks permission to have
            # done what the mission ordered.

        if criteria.strip():
            diff = _out(["diff", base_sha, rebased_sha], scratch)[:REVIEW_DIFF_MAX_CHARS]
            result["review"] = (review or agent_review)(diff, criteria)
            if not result["review"].get("ok"):
                return _escalate(result, "review",
                                 result["review"].get("notes", "agent review rejected the diff"))

        subject = f"dromond: merge {branch}" + (f" ({item_id})" if item_id else "")
        # A base that moved between the rebase and the swap is a RACE, not a
        # conflict: two runs finished close together and the other landed
        # first. The compare-and-swap is right to refuse — but the answer is
        # to rebase onto the new base and try again, not to ask a human
        # whether to retry (owner, 2026-08-14; principle 6). Only a real
        # conflict, or losing the race repeatedly, is worth their attention.
        for attempt in range(SWAP_ATTEMPTS):
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
    result["refresh"] = _refresh_base_checkout(root, base, base_sha, merge_sha)
    if result["refresh"]["command"]:
        result["note"] = (f"{result['refresh']['why']}; refresh it with "
                          f"`{result['refresh']['command']}`")
    result["revert_command"] = f"git -C {root} revert -m 1 {merge_sha}"
    # Anchor the run's own commits before the branch name goes away. A run that
    # committed its own work leaves nothing for the checkpoint to record, so
    # the branch WAS the only pointer and `dromond show --changes` lost the
    # diff the moment it was deleted. A ref costs 41 bytes, keeps the objects
    # off the garbage collector, and survives deletion by design.
    head_sha = _out(["rev-parse", "--verify", f"{branch}^{{commit}}"], root).strip()
    if head_sha:
        ref = f"refs/dromond/{branch.rsplit('/', 1)[-1]}"
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
    second checkout of the base branch is not a shape Dromond creates.
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


def at_completion(con, cfg: dict, run, status: str) -> str | None:
    """SEAM: land a verified run's branch and report where it went.

    ``supervise.py`` calls this once at finalization, AFTER
    ``release_worktree`` — git refuses to delete a branch a worktree still
    has checked out (W-0172, DESIGN §9 "Ordering").

    Returns one line for the run summary, or ``None`` when there was nothing
    to land. A merge is never worth losing a finalization, so every failure
    in here becomes a recorded note instead of an exception.
    """
    try:
        return _land(con, cfg, dict(run), status)
    except Exception as exc:
        note = f"Merge failed: {exc}"
        print(f"dromond: run {run['id']} {note}", file=sys.stderr)
        try:
            _thread(con, int(run["id"]), note)
        except Exception:  # the database is the last thing left; say nothing
            pass
        return note


def _land(con, cfg: dict, run: dict, status: str) -> str | None:
    if status != "done" or not run.get("branch"):
        return None  # only a verified success lands; a shared-tree run has no branch
    branch = run["branch"]
    if dispatch.paused(con):
        # A paused daemon must not be landing code (DESIGN §4). In-flight runs
        # finish; their branches wait for a human `dromond merge`.
        note = f"Merge skipped: dispatch is paused. Branch {branch} kept."
        _thread(con, int(run["id"]), note)
        return note
    # ponytail: no acceptance criteria passed, so the (stubbed, non-blocking)
    # agent-review stage stays out of the way. Feed the item's criteria in when
    # merge.agent_review is wired to a real dispatch.
    try:
        result = merge_run(project.root_for(con, run), branch,
                           item_id=run.get("work_item"), settings=cfg,
                           mission=run_mission(run))
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
            nod.withdraw_merge_cards(con, cfg, int(run["id"]),
                                     note=f"landed as {result['commit'][:12]}")
        except Exception as exc:
            print(f"dromond: run {run['id']} stale merge card not withdrawn: "
                  f"{exc}", file=sys.stderr)
    request_id = None if result["ok"] else _file_card(con, cfg, run, result)
    report = _report_text(run, result, request_id)
    _thread(con, int(run["id"]), report)
    _post_to_work(con, cfg, run, result, report)
    return _note(run, result, request_id)


def _thread(con, run_id: int, body: str) -> None:
    """The run's own thread. This is the whole report for a hand-dispatched
    run, which has no Work item to post to."""
    con.execute("INSERT INTO messages(run_id, sender, body, kind, created_at) "
                "VALUES(?, 'dromond', ?, 'merge', ?)", (run_id, body, db.now()))
    con.commit()


def _checks_line(result: dict) -> str:
    if result["checks_skipped"] or not result["checks"]:
        return "none declared"
    return "; ".join(
        c["name"] + (" ok" if c["ok"] else f" FAILED (exit {c['exit_code']})")
        for c in result["checks"])


def _report_text(run: dict, result: dict, request_id: str | None) -> str:
    """The comment that goes in the Work thread and the run thread.

    DESIGN §9 names its contents: merge commit, files changed, check results,
    and the revert command.
    """
    files = result["files_changed"]
    lines = [f"- files changed ({len(files)}): "
             + (", ".join(f"`{f}`" for f in files[:20])
                + (" …" if len(files) > 20 else "") if files else "none"),
             f"- checks: {_checks_line(result)}"]
    if result["review"]:
        lines.append(f"- review: {result['review'].get('verdict')} — "
                     f"{result['review'].get('notes', '')}")
    if result["ok"]:
        head = (f"run {run['id']} landed `{result['branch']}` on "
                f"`{result['base']}`.")
        lines = [f"- merge commit: `{result['commit']}`", *lines,
                 f"- branch: {'deleted' if result['branch_deleted'] else 'kept'}",
                 f"- checkout: {(result['refresh'] or {}).get('why', 'not reported')}",
                 f"- revert: `{result['revert_command']}`"]
    else:
        head = (f"run {run['id']} could not land `{result['branch']}` on "
                f"`{result['base']}` — escalated at {result['stage']}.")
        lines = [f"- reason: {result['escalation']}",
                 *(["- conflicted files: "
                    + ", ".join(f"`{f}`" for f in result["conflicts"])]
                   if result["conflicts"] else []),
                 *lines,
                 f"- branch `{result['branch']}` is kept; retry by hand with "
                 f"`dromond merge {result['branch']}`"]
        if request_id:
            lines.append(f"- decision card: {request_id}")
    return "\n".join([head, "", *lines])[:19000]


def _note(run: dict, result: dict, request_id: str | None) -> str:
    """The one summary line. The detail is in the run thread and on the item."""
    if result["ok"]:
        return (f"Merged {result['branch']} into {result['base']} as "
                f"{result['commit'][:12]}; revert with "
                f"`{result['revert_command']}`")
    return (f"Merge escalated at {result['stage']}: {result['escalation']}. "
            f"Branch {result['branch']} kept."
            + (f" Nod card {request_id}." if request_id else ""))


def _file_card(con, cfg: dict, run: dict, result: dict) -> str | None:
    """A merge escalation is a needs-you event (DESIGN §8/§9).

    The card offers the three options §9 names — retry, dispatch a resolver,
    leave it — and its request id is recorded in ``nod_requests`` so the
    answer can be mirrored later. Answering it is not built here.
    """
    channels = nod.from_cfg(cfg)
    if channels is None:
        return None  # the human loop is off; the Work item still carries it
    detail = [f"`{result['branch']}` did not land on `{result['base']}`.",
              f"Stage `{result['stage']}`: {result['escalation']}"]
    if result["conflicts"]:
        detail.append("Conflicted files:\n"
                      + "\n".join(f"- `{f}`" for f in result["conflicts"]))
    detail.append(f"The branch is kept. `dromond merge {result['branch']}` "
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
            work_item=run.get("work_item"),
            title=f"{result['branch']} did not land on {result['base']}",
            summary=result["escalation"][:200], **staged)
    except (nod.NodError, nod.NodChannelError) as exc:
        print(f"dromond: run {run['id']} merge escalation not filed: {exc}",
              file=sys.stderr)
        return None
    return created.get("request_id")


def _post_to_work(con, cfg: dict, run: dict, result: dict, report: str) -> None:
    """Thread comment, then the transition: ``review`` landed, ``blocked`` not."""
    item = run.get("work_item")
    if not messaging.mirror_to_work(cfg, item, report):
        return  # no item, Work off, or Work down — the run thread still has it
    if not item.startswith("W-"):
        # ponytail: an issue has no `review` state, and its resolution is the
        # sweeper's own report. The merge outcome is in its thread either way.
        return
    client = work_client.from_cfg(cfg)
    target = "review" if result["ok"] else "blocked"
    try:
        moved = client.move_task(item, target, note=_note(run, result, None)[:300])
    except WorkError as exc:
        print(f"dromond: run {run['id']} could not move {item} to {target}: {exc}",
              file=sys.stderr)
        return
    if moved is not None and not result["ok"]:
        # The run itself succeeded, so the sweeper's completion report would
        # move this item to `review` and undo the escalation. Claim the
        # writeback here: this comment IS the report for that run.
        con.execute("UPDATE runs SET work_reported_at=? WHERE id=?",
                    (db.now(), run["id"]))
        con.commit()
