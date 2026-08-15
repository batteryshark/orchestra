"""Delegation allowlist and spawn bounds, enforced at the broker (DESIGN D11).

A profile's ``spawn_profiles`` names exactly which profiles it may delegate
to; absent or empty means it may not delegate at all. Dromond's spawn is
brokered — a worker writes a request, the supervisor decides — so the
enforcement point is this code, never the model's judgment.

The allowlist is intersected with the PROJECT'S ENABLED SET (W-0187):
delegation is the second moment a project's choice of profiles binds, since
a child run is a run being staffed. The parent is not re-checked — it keeps
the preset it launched with.

``spawn_profiles`` answers *who*; ``max_spawn_depth`` and
``max_children_per_run`` answer *how many* (DESIGN §5). Those two bounds
matter because dispatch has no concurrency cap of any kind (§4): the spawn
tree is the only path by which run count grows without a human ticking
something, so it is the one thing that has to be bounded.

ponytail: a spawned child run is identified by ``requested_by = 'spawn'``,
which is what keeps continuations (which also set ``parent_run``) out of
both counts. The child-launch seam below must set it; nothing else may.
"""
from dromond import config, db

SPAWN_REQUESTER = "spawn"


def allowed_targets(cfg: dict, parent_profile: str) -> list[str]:
    """``spawn_profiles`` ∩ the project's ENABLED SET (W-0187).

    Delegation is the second of the two moments enablement binds: a running
    agent staffing a fresh child run is staffing, so the child's profile must
    be one the project enabled — even though the PARENT keeps the preset it
    launched with whatever happened to the enabled set since.

    ``cfg`` must therefore be the project's config (``config.load(
    run["project_id"])``); a global load enables everything, which is the
    right answer for a run that belongs to no project.
    """
    parent = cfg.get("profiles", {}).get(parent_profile) or {}
    return [name for name in (parent.get("spawn_profiles") or [])
            if config.is_enabled(cfg, name)]


def depth(con, run_id: int) -> int:
    """How many spawn hops deep this run already is. A root run is 0."""
    hops = 0
    seen = {run_id}
    row = con.execute("SELECT parent_run, requested_by FROM runs WHERE id=?",
                      (run_id,)).fetchone()
    while row and row["parent_run"] is not None:
        if row["requested_by"] == SPAWN_REQUESTER:
            hops += 1
        parent = int(row["parent_run"])
        if parent in seen:  # defensive: a cycle must not hang the broker
            break
        seen.add(parent)
        row = con.execute("SELECT parent_run, requested_by FROM runs WHERE id=?",
                          (parent,)).fetchone()
    return hops


def child_count(con, run_id: int) -> int:
    """Children this run has spawned, over the run's whole life — a limit
    that only counted live children would reset itself by finishing them."""
    row = con.execute(
        "SELECT COUNT(*) AS n FROM runs WHERE parent_run=? AND requested_by=?",
        (run_id, SPAWN_REQUESTER)).fetchone()
    return int(row["n"])


def check_bounds(con, cfg: dict, run) -> tuple[bool, str | None]:
    """(ok, error) for the spawn-tree bounds. Not a concurrency cap: these
    bound the tree's shape, not how many runs execute at once."""
    settings = cfg.get("settings", {})
    max_depth = int(settings.get("max_spawn_depth",
                                 config.DEFAULT_MAX_SPAWN_DEPTH))
    max_children = int(settings.get("max_children_per_run",
                                    config.DEFAULT_MAX_CHILDREN_PER_RUN))
    have_depth = depth(con, run["id"])
    if have_depth >= max_depth:
        return False, (f"spawn depth limit reached: run {run['id']} is "
                       f"{have_depth} deep, max_spawn_depth is {max_depth}")
    have_children = child_count(con, run["id"])
    if have_children >= max_children:
        return False, (f"child limit reached: run {run['id']} has spawned "
                       f"{have_children}, max_children_per_run is {max_children}")
    return True, None


def check_target(cfg: dict, parent_profile: str,
                 requested: str) -> tuple[bool, str | None]:
    """(ok, error). The error names the permitted list so a rejected worker
    can self-correct instead of guessing again."""
    listed = list((cfg.get("profiles", {}).get(parent_profile)
                   or {}).get("spawn_profiles") or [])
    allowed = allowed_targets(cfg, parent_profile)
    project = cfg.get("project_id") or "this project"
    if not allowed:
        if listed:  # allowlisted, but the project enables none of them
            return False, (f"profile '{parent_profile}' may not delegate here: "
                           f"project {project} enables none of its "
                           f"spawn_profiles ({', '.join(listed)})")
        return False, f"profile '{parent_profile}' may not delegate (no spawn_profiles)"
    if requested not in allowed:
        if requested in listed:
            return False, (f"profile '{parent_profile}' may not spawn "
                           f"'{requested}': project {project} has not enabled "
                           f"it; permitted here: {', '.join(allowed)}")
        return False, (f"profile '{parent_profile}' may not spawn "
                       f"'{requested}'; permitted: {', '.join(allowed)}")
    if requested not in cfg.get("profiles", {}):
        return False, (f"spawn target '{requested}' is allowlisted but not a "
                       f"configured profile; permitted: {', '.join(allowed)}")
    return True, None


def request_spawn(con, cfg: dict, run, requested: str) -> tuple[bool, str | None]:
    """Broker seam: validate one spawn request from ``run``.

    A rejection is recorded on the run's messages as kind='finding' so the
    D8 findings pipeline surfaces repeat offenders later; nothing is
    spawned and the run continues. Phase 1 launches nothing on success —
    the child request/launch path hooks in below when child runs land.
    """
    ok, error = check_target(cfg, run["profile"], requested)
    if ok:
        ok, error = check_bounds(con, cfg, run)
    if not ok:
        con.execute(
            "INSERT INTO messages(run_id, sender, body, kind, created_at) "
            "VALUES(?, 'dromond', ?, 'finding', ?)",
            (run["id"], f"spawn rejected: {error}", db.now()))
        con.commit()
        return False, error
    # Seam (phase: child runs): create the child run row and launch here.
    return True, None
