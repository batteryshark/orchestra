"""Dispatch policy (DESIGN §4): order, honest queue state, the pause switch.

**There are no concurrency caps here** — not global, not per project, not
per profile, and none may be added. Ten to fifteen concurrent runs, several
inside one project, is normal and correct: every run has its own worktree so
concurrent runs never interfere, merges are sequential and rebase on their
own, and provider rate limits are the harnesses' problem. What replaces a
ceiling is the pair below: a pause switch you can hit, next to a live run
count you can see.

Three things live here, and the HTTP surface (§3, W-0100) imports the same
functions rather than reimplementing them:

- **The pause switch** — ``paused`` / ``pause`` / ``resume`` / ``state``.
  Persisted in ``meta``, so a daemon restart does not silently resume.
  Pausing stops new runs *starting*; runs already in flight are never
  touched, signalled, or counted against anything.
- **The waiting queue** (``dispatch_queue``) — an item that cannot start yet
  is recorded with the reason it waits. It stays out of ``in_progress``: an
  item transitions there only at actual dispatch, because a board that
  claims something is running while it waits stops being trusted.
- **Order**, which matters only for items that must wait: dependencies first
  (a hard constraint), then ready-lane board order exactly as Work returned
  it — Work has no priority field by design, so the board's order *is* the
  priority signal — then FIFO by how long the item has waited.
"""
import json

from dromond import db

PAUSE_KEY = "dispatch_paused"

# A prerequisite in any of these is settled; anything else still blocks.
DONE_STATUSES = frozenset({"done", "closed", "cancelled", "resolved"})

# Sorts after every real board position, so an item Work did not return in
# this pass falls to the end and is ordered by FIFO alone.
NO_LANE = 1 << 30


# --- the pause switch -------------------------------------------------------

def pause_state(con) -> dict | None:
    """``{"at": iso, "note": str|None}`` while paused, else None.

    Tolerant of what is already in the key. This module writes a JSON object,
    but ``http.py`` used to write a bare ``"1"``/``"0"`` flag against the SAME
    key, and json.loads turns those into ints -- on which ``.get`` raises,
    which killed the whole daemon tick on every pass. Anything that is not an
    object is read for its truthiness alone and the timestamp is simply
    unknown; a "0" means not paused, like the flag it was.
    """
    raw = db.meta_get(con, PAUSE_KEY)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        value = raw  # not JSON at all: a legacy flag, or hand-edited
    if not isinstance(value, dict):
        # A falsey legacy flag ("0", 0, "") is NOT paused.
        if not value or value in ("0", "false", "False"):
            return None
        return {"at": db.meta_get(con, "dispatch_paused_at") or None, "note": None}
    return {"at": value.get("at"), "note": value.get("note")}


def paused(con) -> bool:
    return pause_state(con) is not None


def pause(con, note: str | None = None) -> dict:
    """Stop new dispatches. In-flight runs are deliberately untouched."""
    state_ = {"at": db.now(), "note": note or None}
    db.meta_set(con, PAUSE_KEY, json.dumps(state_))
    con.commit()
    return state_


def resume(con) -> dict | None:
    """Allow dispatch again. Returns the pause it lifted, or None if the
    switch was already off. Nothing is launched here: the next sweeper pass
    and the next daemon tick release what waited, in order."""
    was = pause_state(con)
    db.meta_set(con, PAUSE_KEY, "")
    con.commit()
    return was


def live_runs(con) -> int:
    """Runs actually executing. ``pending`` rows are deferred dispatches that
    have not started, so they are queue, not run count."""
    row = con.execute(
        f"SELECT COUNT(*) AS n FROM runs WHERE status NOT IN {db.TERMINAL_SQL} "
        "AND status != 'pending'").fetchone()
    return int(row["n"])


def state(con) -> dict:
    """One dict for `dromond status` and the dashboard: is dispatch paused,
    how many runs are live, and what waits for what."""
    p = pause_state(con)
    return {"paused": p is not None,
            "paused_at": (p or {}).get("at"),
            "pause_note": (p or {}).get("note"),
            "live_runs": live_runs(con),
            "waiting": waiting(con)}


# --- the waiting queue ------------------------------------------------------

def waiting(con) -> list[dict]:
    """Everything that cannot start yet, in the order it would start."""
    rows = con.execute(
        "SELECT * FROM dispatch_queue ORDER BY COALESCE(lane_index, ?), id",
        (NO_LANE,))
    return [dict(r) for r in rows]


def waiting_ids(con) -> set[str]:
    return {r["item_id"] for r in con.execute("SELECT item_id FROM dispatch_queue")}


def hold(con, item_id: str, kind: str, lane_index: int | None,
         reason: str, detail: str | None) -> bool:
    """Record that ``item_id`` waits, and why. Returns True when this is new
    or the reason changed, so a caller logs the transition once instead of
    every pass. The Work item is NOT moved to in_progress — that happens at
    actual dispatch and nowhere else."""
    now = db.now()
    row = con.execute("SELECT reason, detail FROM dispatch_queue WHERE item_id=?",
                      (item_id,)).fetchone()
    if row is None:
        con.execute(
            "INSERT INTO dispatch_queue(item_id, kind, reason, detail, lane_index, "
            "enqueued_at, updated_at) VALUES(?,?,?,?,?,?,?)",
            (item_id, kind, reason, detail, lane_index, now, now))
        con.commit()
        return True
    changed = (row["reason"], row["detail"]) != (reason, detail)
    con.execute(
        "UPDATE dispatch_queue SET reason=?, detail=?, lane_index=?, updated_at=? "
        "WHERE item_id=?", (reason, detail, lane_index, now, item_id))
    con.commit()
    return changed


def release(con, item_id: str) -> None:
    """Drop an item from the waiting queue — it dispatched, or stopped being
    a candidate at all."""
    con.execute("DELETE FROM dispatch_queue WHERE item_id=?", (item_id,))
    con.commit()


# --- dependencies -----------------------------------------------------------

def prerequisites(item: dict) -> list[str]:
    """Work item ids this item must wait for. ``dependsOn`` is the declared
    edge; ``blockedBy`` is the same constraint recorded from the other side.
    Tolerant of both a bare id list and a list of ``{"id": ...}``."""
    found: list[str] = []
    for key in ("dependsOn", "blockedBy"):
        for dep in item.get(key) or []:
            if isinstance(dep, dict):
                dep = dep.get("id")
            if dep and dep != item.get("id") and dep not in found:
                found.append(dep)
    return found


def statuses(items) -> dict[str, str | None]:
    """Status by id for everything this pass fetched (tasks carry ``status``,
    issues carry ``state``)."""
    return {item["id"]: (item.get("status") or item.get("state"))
            for _, item in items}


def unmet(item: dict, known: dict, lookup=None) -> list[str]:
    """Prerequisites that are not finished yet.

    ``known`` is filled in as it goes, so one lookup serves every dependent.
    ponytail: a prerequisite id Work does not know (deleted, typo) reads as
    unfinished and holds the item forever — visibly, in the waiting queue
    with its reason. Upgrade path is a distinct 'unknown dependency' reason
    once Work can tell 404 from unfinished.
    """
    blocked = []
    for dep in prerequisites(item):
        if dep not in known and lookup is not None:
            fetched = lookup(dep) or {}
            known[dep] = fetched.get("status") or fetched.get("state")
        if known.get(dep) not in DONE_STATUSES:
            blocked.append(dep)
    return blocked


# --- order ------------------------------------------------------------------

def plan(con, candidates, known: dict, lookup=None) -> list[tuple]:
    """Order claimable items and say which of them must wait.

    ``candidates`` is ``[(kind, item, lane_index)]`` in the order Work served
    them. Returns ``[(kind, item, lane_index, reason)]`` where ``reason`` is
    None for anything that starts now, else ``(reason, detail)``.

    Sort key is dependencies first (nothing blocked outranks something
    ready), then ready-lane board order, then FIFO by how long the item has
    already waited. Order only ever matters for the items that must wait —
    everything else dispatches as soon as it is claimed, all of it at once,
    with no cap and no artificial delay.
    """
    pause = pause_state(con)
    seq = {r["item_id"]: r["id"]
           for r in con.execute("SELECT id, item_id FROM dispatch_queue")}
    planned = []
    for kind, item, lane in candidates:
        blocked = unmet(item, known, lookup)
        if blocked:
            reason = ("dependency", ", ".join(blocked))
        elif pause is not None:
            reason = ("paused", pause.get("note"))
        else:
            reason = None
        planned.append((kind, item, lane, reason))
    planned.sort(key=lambda e: (e[3] is not None,
                                e[2] if e[2] is not None else NO_LANE,
                                seq.get(e[1]["id"], NO_LANE)))
    return planned
