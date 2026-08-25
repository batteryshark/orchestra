"""The refine lane (W-0309): a tag asks for shaping, not execution.

A human tags a Work item `refine`. That tag IS the request, and it is the
whole request: the lane dispatches one refinement run whatever the item's
status, and whether or not it is delegated. Refinement happens BEFORE
execution, so waiting for `delegated` + `ready` would starve exactly the thin
items that need it — the owner's real creation flow is a title plus a riff.

This is neither the worker lane nor the verify lane. The run never claims the
item and appends no lifecycle fact: it writes `fact: refined`, a verb Work's
status derivation ignores, so nothing here moves a board column and no
checklist is ever ticked or declined on its behalf. ``sweeper.report_result``
skips these runs for that reason — a refinement has nothing to land.

The tag is also the receipt. The run drops it as its last act, so a tag still
present after a run finished means the pass did not finish (or the human
tagged it again), and the next sweep dispatches again. Only a LIVE refine run
stops a dispatch.
"""
from orchestra import brief, config, db, project, supervise
from orchestra.work_client import WorkClient

TAG = "refine"
REQUESTED_BY = "refine"
REFINE_TIER = 1  # workhorse: shaping is bounded work, same tier as sign-off


def _profile_name(pcfg: dict, item_id: str) -> str | None:
    """``[work] refine_profile``, else the one ENABLED tier-1 profile.

    The same volunteer rule the sign-off pass uses: ambiguity is reported,
    never guessed — two tier-1 profiles is a config the owner resolves.
    """
    name = (dict(pcfg.get("work") or {}).get("refine_profile") or "").strip()
    if name:
        return name
    cheap = sorted(name for name, p in config.enabled_profiles(pcfg).items()
                   if config.tier_of(p.get("tier")) == REFINE_TIER)
    if len(cheap) == 1:
        return cheap[0]
    why = (f"several tier-1 profiles ({', '.join(cheap)})" if cheap else
           "no profile marked tier = 1 (workhorse)")
    print(f"orchestra refine: {item_id} skipped — {why}; set "
          '[work] refine_profile = "NAME"')
    return None


def _live(con, item_id: str):
    """A refine run still in flight for this item — the ONE thing that stops
    a dispatch. A worker run's own liveness is not this lane's business."""
    return con.execute(
        f"SELECT id FROM runs WHERE work_item=? AND requested_by=? "
        f"AND status NOT IN {db.TERMINAL_SQL} LIMIT 1",
        (item_id, REQUESTED_BY)).fetchone()


def after_fetch(con, cfg: dict, client: WorkClient, items: list,
                launcher, actions: list) -> bool:
    """Dispatch a refinement run for every refine-tagged task in this pass.

    Returns False when something deferred, which holds the sweep cursor so
    the next pass sees the tag again. A tagged item that is never re-fetched
    is a dropped human request.
    """
    ok = True
    for kind, item in items:
        if kind != "task" or TAG not in (item.get("tags") or []):
            continue
        try:
            ok &= dispatch_one(con, cfg, client, item, launcher, actions)
        except Exception as exc:
            print(f"orchestra refine: {item['id']} failed: {exc}")
            ok = False
    return ok


def dispatch_one(con, cfg: dict, client: WorkClient, item: dict,
                 launcher, actions: list) -> bool:
    item_id = item["id"]
    if _live(con, item_id):
        return True
    proj = project.by_work_path(con, item.get("projectPath"))
    if proj is None and project.refresh(con, cfg):
        proj = project.by_work_path(con, item.get("projectPath"))
    if proj is None:
        print(f"orchestra refine: {item_id} has no known project "
              f"({item.get('projectPath')!r}) — skipped")
        return True
    pcfg = config.load(proj.project_id)
    profile_name = _profile_name(pcfg, item_id)
    if not profile_name:
        return False
    try:
        profile = config.staff_profile(pcfg, profile_name)
    except SystemExit as exc:
        print(f"orchestra refine: {item_id} not staffed: {exc}")
        return False
    run, blocked = supervise.create_run(
        con, profile=profile_name, backend=profile["backend"],
        model=profile.get("model"), title=f"Refine {item_id}"[:80],
        requested_by=REQUESTED_BY, workdir=str(proj.path),
        project_id=proj.project_id, work_item=item_id)
    if run is None:
        # Paused, or another run holds the item. The tag stays put and the
        # held cursor brings the item back next pass.
        print(f"orchestra refine: {item_id} deferred ({blocked})")
        return False
    run_id, slug = int(run["id"]), run["slug"]
    agent = f"{client.identity}/{slug}"
    tag = f"[{agent}]"
    try:
        # No worktree and no Work snapshot: the pass edits the ITEM, not the
        # repository, so there is no branch to land — and a snapshot would
        # hand it the checklist protocol, which is the one thing a
        # refinement must not answer.
        supervise.prepare_launch(
            con, proj.path, pcfg, run, use_worktree=False,
            mission=brief.refine_mission(item=item_id, root=proj.path,
                                         api=client.api_url, agent=agent,
                                         tag=tag))
        launcher(proj.path, run_id)
    except BaseException as exc:
        error = str(exc)[:1000] or exc.__class__.__name__
        supervise.fail_launch(con, proj.path, run_id, error)
        print(f"orchestra refine: {item_id} launch failed: {error}")
        actions.append({"action": "refine_failed", "item": item_id,
                        "run": run_id, "reason": error})
        return True
    client.log_task(item_id, f"{tag} dispatched refinement run {run_id}")
    actions.append({"action": "refine", "item": item_id, "run": run_id})
    return True
