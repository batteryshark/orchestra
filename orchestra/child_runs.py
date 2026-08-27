"""A run asks for help: bounded child runs on weaker profiles.

Ported from the original Orchestra (``orchestra_cli/child_runs.py``), where
this worked, and lost in the move — the current tree kept only the validator,
which W-0291 later removed as a phantom surface because nothing launched
behind it. The reason to have it back is the one the owner names: sometimes a
cheap model should take a bounded piece while the expensive one keeps the
mission.

BROKERED, NEVER SELF-LAUNCHED. A worker calls ``orchestra spawn`` and all
that does is WRITE a request. The parent's own supervisor claims it, checks
the bounds, creates the batch, and starts it. The enforcement point is this
code, never the model's judgment, and a worker cannot start a process from
inside its own sandbox.

Three bounds, all in code and all reserved under one write lock so two
concurrent requests cannot both pass: how deep the tree may go, how many
children one run may ever have, and how many may run at once. Dispatch has
no concurrency cap of its own, so the spawn tree is the one path by which
run count grows without a human ticking anything.

WEAKER ONLY. A tiered parent may hand work down, never up: asking for a
stronger model is asking for a different decomposition, which is a question
for the human who set the mission, not a thing a run may award itself.

When a batch settles the lead is told ONCE: a message if it is still running,
a continuation run if it already finished. Nothing is merged for it — the
lead reads the branches and decides.
"""
import json
import os
import sqlite3
from pathlib import Path
from typing import Callable

from orchestra import brief, config, db, paths, worktree

REQUESTED_BY = "spawn"
DEFAULTS = {"child_max_depth": 1, "child_max_per_run": 3, "child_max_active": 3}


def _limit(cfg: dict, name: str) -> int:
    value = (cfg.get("settings") or {}).get(name, DEFAULTS[name])
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SystemExit(f"orchestra: settings.{name} must be a non-negative integer")
    return value


def limits(cfg: dict) -> tuple[int, int, int]:
    """``(max_depth, max_per_run, max_active)``."""
    return tuple(_limit(cfg, name) for name in
                 ("child_max_depth", "child_max_per_run", "child_max_active"))


def validate_targets(cfg: dict, parent, targets: list[str]) -> None:
    """Every target must be staffable here, and none may outrank the parent.

    The project's ENABLED SET binds a second time here (W-0187): a running
    agent staffing a fresh child is staffing, so the child's profile must be
    one this project enabled. The PARENT keeps whatever preset it launched
    with, whatever the enabled set has done since.
    """
    enabled = config.enabled_profiles(cfg)
    parent_tier = config.tier_of((cfg.get("profiles") or {})
                                 .get(parent["profile"], {}).get("tier"))
    for target in targets:
        if target == parent["profile"]:
            raise SystemExit(
                f"orchestra: run {parent['id']} is already {target}; a child "
                "run is for handing work DOWN, not for a second copy")
        if target not in enabled:
            raise SystemExit(
                f"orchestra: {target} is not enabled for this project — "
                f"enabled: {', '.join(sorted(enabled)) or '(none)'}")
        target_tier = config.tier_of(enabled[target].get("tier"))
        if parent_tier is not None and target_tier is not None \
                and target_tier > parent_tier:
            raise SystemExit(
                f"orchestra: {target} (tier {target_tier}) outranks "
                f"{parent['profile']} (tier {parent_tier}). A stronger model "
                "is a different decomposition — ask the human who set the "
                "mission, do not award it to yourself")


def validate_parent(con, cfg: dict, run_id: int, identity_run: int | None):
    """The row this request may act for, or SystemExit saying why not."""
    parent = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if parent is None:
        raise SystemExit(f"orchestra: no run {run_id}")
    if identity_run is not None and identity_run != run_id:
        raise SystemExit(f"orchestra: run {identity_run} may ask for help for "
                         f"itself, not for run {run_id}")
    if parent["status"] != "running":
        raise SystemExit(f"orchestra: run {run_id} is {parent['status']}, "
                         "not running")
    if parent["layer"]:
        raise SystemExit("orchestra: a control turn does not spawn work")
    max_depth, _, _ = limits(cfg)
    if int(parent["child_depth"] or 0) + 1 > max_depth:
        raise SystemExit(
            f"orchestra: child depth limit reached ({max_depth}); raise "
            "settings.child_max_depth deliberately to allow recursion")
    return parent


def enqueue(con, parent, targets: list[str], mission: str, *,
            title: str | None = None, context: str | None = None,
            shared_workdir: bool = False) -> int:
    """Record the request. This is all a worker may do."""
    cur = con.execute(
        "INSERT INTO spawn_requests(lead_run, requested_by, targets_json, "
        "mission, title, context, shared_workdir, status, created_at) "
        "VALUES(?,?,?,?,?,?,?,'pending',?)",
        (parent["id"], parent["profile"], json.dumps(targets), mission, title,
         context, int(shared_workdir), db.now()))
    con.commit()
    return int(cur.lastrowid)


def create(con, root: Path, cfg: dict, parent, targets: list[str],
           mission: str, *, title: str | None = None,
           context: str | None = None, shared_workdir: bool = False,
           spawn_request_id: int | None = None) -> list[int]:
    """Create one bounded batch of child rows. The caller starts them."""
    if not targets:
        raise SystemExit("orchestra: a spawn needs at least one --to target")
    _, max_total, max_active = limits(cfg)
    validate_targets(cfg, parent, targets)
    staffed = [(name, config.staff_profile(cfg, name)) for name in targets]

    # The whole batch is reserved under ONE write lock: two concurrent
    # requests must not both read the counts, both pass, and both allocate.
    run_ids: list[int] = []
    con.execute("BEGIN IMMEDIATE")
    try:
        current = con.execute("SELECT status FROM runs WHERE id=?",
                              (parent["id"],)).fetchone()
        if current is None or current["status"] != "running":
            raise SystemExit(f"orchestra: run {parent['id']} is no longer running")
        total = con.execute("SELECT COUNT(*) n FROM runs WHERE parent_run=? "
                            "AND requested_by=?",
                            (parent["id"], REQUESTED_BY)).fetchone()["n"]
        active = con.execute(
            f"SELECT COUNT(*) n FROM runs WHERE parent_run=? AND requested_by=? "
            f"AND status NOT IN {db.TERMINAL_SQL}",
            (parent["id"], REQUESTED_BY)).fetchone()["n"]
        if total + len(targets) > max_total:
            raise SystemExit(f"orchestra: run {parent['id']} may have "
                             f"{max_total} children in total")
        if active + len(targets) > max_active:
            raise SystemExit(f"orchestra: run {parent['id']} may have "
                             f"{max_active} children running at once")
        for name, profile in staffed:
            cur = con.execute(
                "INSERT INTO runs(profile, backend, model, title, work_item, "
                "requested_by, workdir, project_id, parent_run, "
                "spawn_request_id, child_depth, status, started_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,'spawning',?)",
                (name, profile["backend"], profile.get("model"),
                 (title or mission)[:80], parent["work_item"], REQUESTED_BY,
                 str(root), parent["project_id"], parent["id"],
                 spawn_request_id, int(parent["child_depth"] or 0) + 1,
                 db.now()))
            run_ids.append(int(cur.lastrowid))
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise

    try:
        for run_id, (_name, profile) in zip(run_ids, staffed):
            run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            workdir, branch = str(parent["workdir"]), None
            if not shared_workdir:
                # Branched from the PARENT's branch: the child starts from the
                # work its lead has already done, not from main.
                wt, branch = worktree.create(
                    root, run_id, parent["project_id"] or str(root),
                    start_point=parent["branch"] or None,
                    backend=profile["backend"])
                workdir = str(wt)
            text = brief.compose_child(root, run, profile, mission,
                                       parent=parent, context=context,
                                       workdir=workdir, cfg=cfg)
            bp = paths.briefs_dir() / f"run-{run_id}.md"
            bp.write_text(text)
            lp = paths.logs_dir() / f"run-{run_id}.jsonl"
            lp.touch()
            con.execute(
                "UPDATE runs SET brief_path=?, log_path=?, workdir=?, branch=? "
                "WHERE id=?", (str(bp), str(lp), workdir, branch, run_id))
            con.commit()
    except BaseException as exc:
        con.execute(
            "UPDATE runs SET status='failed', finished_at=?, summary=? "
            f"WHERE id IN ({','.join('?' * len(run_ids))}) AND status='spawning'",
            (db.now(), f"Child batch setup failed: {str(exc)[:500]}", *run_ids))
        con.commit()
        raise
    return run_ids


def process_pending(con, root: Path, cfg: dict, lead_run: int,
                    launcher: Callable[[Path, int], None]) -> list[dict]:
    """Claim and launch this lead's requests, from OUTSIDE its sandbox."""
    results: list[dict] = []
    for request in list(con.execute(
            "SELECT * FROM spawn_requests WHERE lead_run=? AND status='pending' "
            "ORDER BY id", (lead_run,))):
        claimed = con.execute(
            "UPDATE spawn_requests SET status='processing' "
            "WHERE id=? AND status='pending'", (request["id"],))
        con.commit()
        if claimed.rowcount != 1:
            continue  # another supervisor pass took it
        child_ids: list[int] = []
        try:
            targets = json.loads(request["targets_json"])
            if not isinstance(targets, list) or not all(
                    isinstance(t, str) and t for t in targets):
                raise ValueError("spawn request has invalid targets")
            parent = con.execute("SELECT * FROM runs WHERE id=?",
                                 (lead_run,)).fetchone()
            if parent is None or parent["status"] != "running":
                raise RuntimeError(f"run {lead_run} is no longer running")
            # A project that is not a repository has no worktree to give, and
            # that is a note on the request, not a refusal of the help.
            fallback = not bool(request["shared_workdir"]) \
                and not (root / ".git").exists()
            warning = ("project is not a git repository; the children share "
                       "the lead's workdir") if fallback else None
            child_ids = create(
                con, root, cfg, parent, targets, request["mission"],
                title=request["title"], context=request["context"],
                shared_workdir=bool(request["shared_workdir"]) or fallback,
                spawn_request_id=int(request["id"]))
            for child_id in child_ids:
                try:
                    launcher(root, child_id)
                except Exception as exc:
                    con.execute(
                        "UPDATE runs SET status='failed', finished_at=?, "
                        "summary=? WHERE id=? AND status='spawning'",
                        (db.now(), f"Child supervisor launch failed: {exc}",
                         child_id))
            con.execute(
                "UPDATE spawn_requests SET status='accepted', "
                "child_run_ids_json=?, error=?, processed_at=? WHERE id=?",
                (json.dumps(child_ids), warning, db.now(), request["id"]))
            con.commit()
            results.append({"id": int(request["id"]), "status": "accepted",
                            "child_run_ids": child_ids, "warning": warning})
        except (Exception, SystemExit) as exc:
            error = str(exc)[:1000] or exc.__class__.__name__
            con.execute(
                "UPDATE spawn_requests SET status='failed', "
                "child_run_ids_json=?, error=?, processed_at=? WHERE id=?",
                (json.dumps(child_ids), error, db.now(), request["id"]))
            con.commit()
            results.append({"id": int(request["id"]), "status": "failed",
                            "child_run_ids": child_ids, "error": error})
    return results


def fail_unprocessed(con, lead_run: int, reason: str) -> None:
    """A lead that ended owes its unclaimed requests an answer."""
    con.execute(
        "UPDATE spawn_requests SET status='failed', error=?, processed_at=? "
        "WHERE lead_run=? AND status IN ('pending','processing')",
        (reason[:1000], db.now(), lead_run))
    con.commit()


def _batch_prompt(lead_id: int, children: list) -> str:
    said = "\n".join(
        f"- run {c['id']} ({c['profile']}) {c['status']}"
        f"; branch {c['branch'] or '(shared workdir)'}"
        f"; summary: {(c['summary'] or '(none)')[:500]}" for c in children)
    return (f"Every child run you asked for has settled. Read their branches "
            f"and results, take what is useful, and verify the combined "
            f"outcome yourself. Do not merge blindly.\n\n{said}")


def maybe_wake_lead(con, root: Path, trigger_run_id: int) -> int | None:
    """Tell a lead ONCE that its batch settled. Returns a run to launch, if any.

    A lead still running gets a message on its next safe boundary; a lead
    that already finished gets a continuation of its own session. The
    ``notified_at`` / ``child_wakeup_*`` stamps are claimed inside the write
    lock, so two children settling together wake it once.
    """
    trigger = con.execute("SELECT * FROM runs WHERE id=?",
                          (trigger_run_id,)).fetchone()
    if trigger is None:
        return None
    candidates: list[tuple[int, int | None]] = []
    if trigger["parent_run"] and trigger["requested_by"] == REQUESTED_BY:
        candidates.append((int(trigger["parent_run"]),
                           int(trigger["spawn_request_id"])
                           if trigger["spawn_request_id"] else None))
    for request in con.execute("SELECT id FROM spawn_requests WHERE lead_run=?",
                               (trigger_run_id,)):
        candidates.append((trigger_run_id, int(request["id"])))

    for lead_id, request_id in dict.fromkeys(candidates):
        con.execute("BEGIN IMMEDIATE")
        try:
            lead = con.execute("SELECT * FROM runs WHERE id=?",
                               (lead_id,)).fetchone()
            request = con.execute("SELECT * FROM spawn_requests WHERE id=?",
                                  (request_id,)).fetchone() if request_id else None
            children = list(con.execute(
                "SELECT * FROM runs WHERE spawn_request_id=? ORDER BY id",
                (request_id,))) if request_id else list(con.execute(
                    "SELECT * FROM runs WHERE parent_run=? AND requested_by=? "
                    "ORDER BY id", (lead_id, REQUESTED_BY)))
            notified = bool(request["notified_at"]) if request else bool(
                lead and (lead["child_wakeup_run"] or lead["child_wakeup_message"]))
            ready = bool(
                lead is not None and children and not notified
                and lead["session_ref"]
                and (request is None or request["status"] == "accepted")
                and lead["status"] in ("running", "interrupt", "done", "failed")
                and all(c["status"] in db.RUN_TERMINAL for c in children))
            if not ready:
                con.execute("COMMIT")
                continue
            prompt = _batch_prompt(lead_id, children)
            if lead["status"] in ("running", "interrupt"):
                try:
                    offset = os.path.getsize(lead["log_path"])
                except (OSError, TypeError):
                    offset = 0
                cur = con.execute(
                    "INSERT INTO messages(sender, body, run_id, kind, "
                    "created_at, delivery_offset) "
                    "VALUES('orchestra',?,?,'interrupt',?,?)",
                    (prompt, lead_id, db.now(), offset))
                _claim(con, request_id, lead_id, "message", int(cur.lastrowid))
                con.execute("COMMIT")
                return None
            from orchestra import supervise  # cycle: supervise imports this
            wake_id = supervise.create_followup(
                con, root, dict(lead), lead["requested_by"], prompt,
                title=f"child results for run {lead_id}")
            if wake_id is not None:
                _claim(con, request_id, lead_id, "run", int(wake_id))
            con.execute("COMMIT")
            return wake_id
        except BaseException:
            con.execute("ROLLBACK")
            raise
    return None


def _claim(con, request_id: int | None, lead_id: int, kind: str, value: int) -> None:
    """Stamp the one wakeup, so a second child settling cannot repeat it."""
    if request_id:
        con.execute(
            f"UPDATE spawn_requests SET wakeup_{kind}=?, notified_at=? "
            "WHERE id=? AND notified_at IS NULL", (value, db.now(), request_id))
    else:
        con.execute(
            f"UPDATE runs SET child_wakeup_{kind}=? "
            f"WHERE id=? AND child_wakeup_{kind} IS NULL", (value, lead_id))
