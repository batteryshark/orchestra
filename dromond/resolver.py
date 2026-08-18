"""Act on a merge-escalation card's two verbs (DESIGN §9).

The card a failed landing files offers retry, dispatch a resolver, and leave
it. The answer loop in ``nod.py`` calls in here for the first two, from a
context where an exception helps nobody: ``retry_landing`` always returns a
one-line outcome, and ``dispatch_resolver`` returns the new run id or None
with the reason on stderr.

The retry is ``merge.at_completion`` again — the SAME pipeline an automatic
landing takes (dirty-base guard, ignored-path dropping, tripwire judge,
checks, compare-and-swap), the same Work report, and a fresh card if it
escalates again.

The resolver dispatch is a synchronous ``sweeper._claim``-shaped launch:
insert the run row, ``supervise.prepare_launch``, spawn the supervisor. The
deferred path ('pending' rows the daemon releases) exists for dependency-gated
dispatches, and a resolver has none — its parent is already terminal.
"""
import sqlite3
import sys

from dromond import config, db, dispatch, merge, names, paths, project, supervise

# Seam for tests: a test never launches a real harness.
launcher = supervise.spawn_supervisor

RESOLVER_MISSION = """\
Land the work on `{branch}` onto `{base}`.

Run {run_id} finished, but its branch did not land:
{reason}

Facts:
- The branch `{branch}` is kept, exactly as the run left it. Nothing landed
  on `{base}` and nothing was reverted.
- Your working directory is a fresh worktree of the current `{base}`; the
  failed branch is not checked out here.

Definition of done — the branch's WORK lands on `{base}`:
- Rebase and reconcile `{branch}` against the current `{base}`. Keep both
  sides' intent; neither side's work is the one to lose.
- Run the repository's checks.
- Land it with `dromond merge {branch}`, or commit the reconciled result on a
  fresh branch and land that with `dromond merge <fresh-branch>`.

Do not force-push. Do not delete `{branch}` or its kept ref. If you land a
fresh branch, the original `{branch}` stays untouched.
"""


def _branch_exists(root, branch: str) -> bool:
    return merge._git(["rev-parse", "--verify", f"refs/heads/{branch}"],
                      root, check=False).returncode == 0


# --- retry --------------------------------------------------------------------

def retry_landing(con, cfg: dict, run_id: int) -> str:
    """Re-run the landing for a terminal run whose branch was kept.

    Refusals are returned sentences, never exceptions: the caller is relaying
    a card answer, and the human who tapped Retry gets told why not.
    """
    try:
        run = con.execute("SELECT * FROM runs WHERE id=?",
                          (int(run_id),)).fetchone()
        if run is None:
            return f"run {run_id} does not exist; nothing to retry"
        if run["status"] not in db.RUN_TERMINAL:
            return (f"run {run_id} is still {run['status']}; its landing "
                    "happens at completion")
        branch = run["branch"]
        if not branch:
            return (f"run {run_id} ran in the shared checkout; "
                    "there is no branch to land")
        root = project.root_for(con, run)
        if not _branch_exists(root, branch):
            return f"branch {branch} is gone from {root}; nothing to retry"
        # The shared landing path: same guards, same report to the Work
        # thread, a fresh card if it escalates again, the stale-card
        # withdrawal if it lands. at_completion never raises.
        return merge.at_completion(con, cfg, dict(run), "done") \
            or f"run {run_id} had nothing to land"
    except Exception as exc:
        return f"retry of run {run_id} failed: {exc}"


# --- dispatch a resolver --------------------------------------------------------

def _refuse(reason: str) -> None:
    print(f"dromond: resolver not dispatched: {reason}", file=sys.stderr)


def resolver_profile(cfg: dict) -> tuple[str, dict] | None:
    """``[merge] resolver_profile`` when set; otherwise the highest-priority
    (lowest number) enabled tier-3 profile. None, with the reason on stderr,
    when neither exists."""
    named = merge.merge_cfg(cfg).get("resolver_profile")
    if named:
        try:  # a staffing moment (W-0187): the enabled set gates it
            return named, config.staff_profile(cfg, named)
        except SystemExit as exc:
            _refuse(str(exc))
            return None
    heavy = sorted(((name, p) for name, p in config.enabled_profiles(cfg).items()
                    if config.tier_of(p.get("tier")) == 3),
                   key=lambda kv: (config.priority_of(kv[1]), kv[0]))
    if not heavy:
        _refuse("no [merge] resolver_profile is set and no enabled profile is "
                f"tier = 3; set one in {paths.global_config_path()}")
        return None
    return heavy[0][0], config.profile_cfg(cfg, heavy[0][0])


def _insert(con, failed, root, profile_name: str, profile: dict) -> int:
    """A run row with lineage: ``parent_run`` is the failed run and
    ``work_item`` carries over, so the sweeper's writeback and the dashboard's
    lineage rendering keep working. Same slug-mint loop as dispatch."""
    title = f"Resolve the landing of {failed['branch']}"[:80]
    for _ in range(names.MAX_ATTEMPTS + 4):
        slug = names.assign_slug(con)
        try:
            cur = con.execute(
                "INSERT INTO runs(slug, profile, backend, model, title, "
                "requested_by, workdir, project_id, status, started_at, "
                "work_item, parent_run) VALUES(?,?,?,?,?,'nod',?,?,'spawning',?,?,?)",
                (slug, profile_name, profile["backend"], profile.get("model"),
                 title, str(root), failed["project_id"], db.now(),
                 failed["work_item"], int(failed["id"])))
            con.commit()
            return int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            if not names.is_unique_violation(exc):
                raise
            names.reset_memory_cache()
    raise RuntimeError("dromond: could not mint a unique run slug")


def dispatch_resolver(con, cfg: dict, run_id: int, reason: str) -> int | None:
    """Spawn a resolver run for ``run_id``'s kept branch.

    Returns the new run id, or None with the reason on stderr when it cannot.
    The resolver works in its OWN fresh worktree off the current base — it
    never adopts the failed branch as its checkout.
    """
    try:
        failed = con.execute("SELECT * FROM runs WHERE id=?",
                             (int(run_id),)).fetchone()
        if failed is None:
            _refuse(f"run {run_id} does not exist")
            return None
        branch = failed["branch"]
        if not branch:
            _refuse(f"run {run_id} ran in the shared checkout; "
                    "there is no branch to resolve")
            return None
        if dispatch.paused(con):
            # The pause switch is the one gate on a new run (DESIGN §4),
            # and a card answer does not get around it.
            _refuse("dispatch is paused; `dromond resume`, then "
                    f"`dromond merge {branch}` or a fresh card answer")
            return None
        root = project.root_for(con, failed)
        if not _branch_exists(root, branch):
            _refuse(f"branch {branch} is gone from {root}")
            return None
        picked = resolver_profile(cfg)
        if picked is None:
            return None
        profile_name, profile = picked
        base = merge.merge_cfg(cfg)["base"] or \
            merge._out(["symbolic-ref", "--short", "HEAD"], root)
        new_id = _insert(con, failed, root, profile_name, profile)
        mission = RESOLVER_MISSION.format(
            run_id=int(failed["id"]), branch=branch, base=base,
            reason=(reason or "").strip() or "the landing escalated")
        run = con.execute("SELECT * FROM runs WHERE id=?", (new_id,)).fetchone()
        try:
            supervise.prepare_launch(con, root, cfg, run, mission=mission,
                                     use_worktree=True)
            con.commit()
        except (Exception, SystemExit) as exc:
            # No shared-checkout fallback here on purpose: a resolver in the
            # owner's own checkout is worse than no resolver.
            con.execute("UPDATE runs SET status='failed', summary=?, "
                        "finished_at=? WHERE id=?",
                        (f"Launch setup failed: {exc}", db.now(), new_id))
            con.commit()
            _refuse(f"launch setup for run {new_id} failed: {exc}")
            return None
        launcher(root, new_id)
        return new_id
    except Exception as exc:
        _refuse(str(exc))
        return None
