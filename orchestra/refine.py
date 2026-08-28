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
from pathlib import Path

from orchestra import config, db, project, supervise
from orchestra.work_client import WorkClient

TAG = "refine"
REQUESTED_BY = "refine"
REFINE_TIER = 1  # workhorse: shaping is bounded work, same tier as sign-off

# The goal standard the refine lane writes to (W-0309), vendored beside the
# house style and referenced the same way: by path, never copied into a
# string here.
# ponytail: the run reads it from outside its working directory. Inline the
# 5 KB into the mission if a harness sandbox ever refuses that read.
GOAL_STANDARD = Path(__file__).resolve().parent.parent / "docs/GOAL-STANDARD.md"

# The refine mission (W-0309). A refinement run shapes ONE Work item and
# touches no code: it reads the item and the repository, rewrites the six
# sections around the owner's own words, and drops the tag that asked for it.
# The rules are the refine-work-item skill's, distilled — evidence or a `Q:`
# line, never an invention. Lives HERE, not in brief.py: the `work` CLI, the
# PATCH route and the section names are Work's own (CONTRACT §7).
REFINE_MISSION = """\
Refine Work item {item} to the goal standard. This is a shaping pass, not
execution: change no file in this project, run no build, write no code.

1. Read the item whole: `work show {item} --json --root {root}` — every
   section, both checklists, and the whole progress log. A late log line
   outranks the description where the two disagree.
2. Read the standard at `{standard}`. It defines every term below.
3. Gather evidence: the files, modules and commands the item names,
   `work list --root {root}` for neighbouring or blocking items, and
   `git log` on the named paths. Recon that finds little is a result.
4. Rewrite the six sections — description, goal, requirements,
   acceptanceCriteria, plan, notes — as far as that evidence reaches.

Rules that outrank the standard:

- Preserve the owner's phrasing VERBATIM inside the description. Clarify
  around their words. Never delete, reword, or tidy what they wrote.
- Never invent an answer. Every point the evidence cannot settle becomes one
  `Q:` line in notes, phrased so a one-line answer closes it; add your
  recommended answer on the same line when recon supports one. An empty
  section beats a fabricated one.
- Every acceptance criterion states its verification method — a command or
  an observation. No method available, no criterion: write a `Q:` line.
- At most 6 requirements and 5 acceptance criteria, one condition each.
  Sending either list REPLACES the whole list and drops its ticks, so send
  every item you keep, in order.
- Never tick, untick or decline a checkbox. Never set delegated, never move
  status, never change the title. Those stay the owner's.

Then write back, in this order, and stop. Every call carries your identity:

    curl -sS -X PATCH {api}/api/tasks/{item} \\
      -H 'X-Work-Agent: {agent}' -H 'Content-Type: application/json' -d @BODY.json

1. Sections: one PATCH whose body carries only the sections you changed
   (`requirements` and `acceptanceCriteria` are lists of strings). Work
   permits this edit ONLY while the `refine` tag is present.
2. Confirm with `work show {item} --json --root {root}` that your text
   landed and both checklists survived.
3. Two log entries — `work log {item} "<text>" --root {root}`:
   - `{tag} refined {item}` — what you filled, then one line per open `Q:`,
     in the house style below.
   - `{tag} fact: refined`
4. The tag, last: one PATCH sending `tags` as the item's current tags minus
   `refine`, with nothing else in the body. The allowance dies with the tag,
   so this call ends the pass. Drop the tag even when the evidence settled
   nothing — say so in the comment instead. A tag left in place dispatches
   this whole pass again.
"""


def refine_mission(*, item: str, root: Path, api: str, agent: str,
                   tag: str) -> str:
    """Fill the refine template for one item (W-0309)."""
    return REFINE_MISSION.format(item=item, root=root, api=api.rstrip("/"),
                                 agent=agent, tag=tag,
                                 standard=GOAL_STANDARD)


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
        f"SELECT id FROM runs WHERE ref=? AND requested_by=? "
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
    from orchestra import sweeper  # local: sweeper imports this module

    proj = sweeper.by_source_ref(con, item.get("projectPath"))
    if proj is None and sweeper.refresh_projects(con, cfg):
        proj = sweeper.by_source_ref(con, item.get("projectPath"))
    if proj is None:
        print(f"orchestra refine: {item_id} has no known project "
              f"({item.get('projectPath')!r}) — skipped")
        return True
    if proj.archived:
        # DESIGN §1: parked project, so this lane leaves it alone. True and
        # not False: holding the sweep cursor for a project that may stay
        # parked for months freezes the watermark for the whole workspace.
        # ponytail: the tag stays on the item, so unarchiving revives it on
        # the next full board read or the next edit to the item, not
        # instantly. Give the lane its own waiting row if that ever matters.
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
        project_id=proj.project_id, ref=item_id)
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
            mission=refine_mission(item=item_id, root=proj.path,
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
