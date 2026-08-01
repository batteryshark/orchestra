"""Native, backend-neutral child-run creation and lead wakeups."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Callable

from orchestra_cli import brief, config, db, names, paths, worktree


def _limit(cfg: dict, name: str, default: int) -> int:
    value = cfg.get("settings", {}).get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SystemExit(f"orchestra: settings.{name} must be a non-negative integer")
    return value


def limits(cfg: dict) -> tuple[int, int, int]:
    return (
        _limit(cfg, "child_max_depth", 1),
        _limit(cfg, "child_max_per_run", 3),
        _limit(cfg, "child_max_active", 3),
    )


def _tier(agent: dict, name: str) -> int | None:
    value = agent.get("tier")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SystemExit(f"orchestra: agent '{name}' tier must be a non-negative integer")
    return value


def validate_tiers(cfg: dict, parent: sqlite3.Row, targets: list[str]) -> None:
    """Prevent a tiered parent from escalating to a stronger tiered child."""
    parent_agent = config.agent_cfg(cfg, parent["agent"])
    parent_tier = _tier(parent_agent, parent["agent"])
    for target in targets:
        target_tier = _tier(config.agent_cfg(cfg, target), target)
        if parent_tier is not None and target_tier is not None and target_tier > parent_tier:
            raise SystemExit(
                f"orchestra: target '{target}' tier {target_tier} exceeds parent "
                f"'{parent['agent']}' tier {parent_tier}; use `orchestra consult` to ask "
                "the requester for a stronger decomposition instead"
            )


def validate_parent(con: sqlite3.Connection, cfg: dict, run_id: int,
                    identity: str | None) -> sqlite3.Row:
    parent = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not parent:
        raise SystemExit(f"orchestra: active parent run {run_id} not found")
    if not identity or identity != parent["agent"]:
        raise SystemExit("orchestra: spawn identity does not match the active lead run")
    if parent["status"] != "running":
        raise SystemExit(f"orchestra: lead run {run_id} is {parent['status']}, not running")
    if "containment_mode" in parent.keys() and parent["containment_mode"]:
        raise SystemExit(
            "orchestra: Operator workers cannot spawn child runs; "
            "the controller owns all fan-out and must authorize each run"
        )
    max_depth, _, _ = limits(cfg)
    if int(parent["child_depth"] or 0) + 1 > max_depth:
        raise SystemExit(
            f"orchestra: child depth limit reached ({max_depth}); "
            "raise settings.child_max_depth deliberately to allow recursion"
        )
    return parent


def create(con: sqlite3.Connection, root: Path, cfg: dict, parent: sqlite3.Row,
           targets: list[str], mission: str, *, title: str | None = None,
           context: str | None = None, shared_workdir: bool = False,
           spawn_request_id: int | None = None) -> list[int]:
    """Create one bounded child batch. Caller starts the supervisors."""
    if not targets:
        raise SystemExit("orchestra: spawn needs at least one --to target")
    _, max_total, max_active = limits(cfg)
    validate_tiers(cfg, parent, targets)
    agents = [(name, config.agent_cfg(cfg, name)) for name in targets]
    prepared: list[tuple[str, dict, str | None]] = []
    for target, agent in agents:
        display_model = agent.get("model")
        if agent["backend"] == "codex":
            dm, de = config.codex_defaults()
            effort = agent.get("effort") or de
            display_model = (display_model or dm or "codex-default") + \
                (f" ({effort})" if effort else "")
        elif agent.get("variant"):
            display_model = f"{display_model} ({agent['variant']})"
        prepared.append((target, agent, display_model))

    # Reserve the entire batch under a write lock so two concurrent spawn
    # calls cannot both pass the limits and over-allocate children.
    run_ids: list[int] = []
    con.execute("BEGIN IMMEDIATE")
    try:
        current = con.execute("SELECT * FROM runs WHERE id=?", (parent["id"],)).fetchone()
        if not current or current["status"] != "running":
            raise SystemExit(f"orchestra: lead run {parent['id']} is no longer running")
        total = con.execute("SELECT COUNT(*) n FROM runs WHERE lead_run=?",
                            (parent["id"],)).fetchone()["n"]
        active = con.execute(
            "SELECT COUNT(*) n FROM runs WHERE lead_run=? "
            "AND status NOT IN ('done','failed','timeout','killed')",
            (parent["id"],),
        ).fetchone()["n"]
        if total + len(targets) > max_total:
            raise SystemExit(f"orchestra: child count limit exceeded ({max_total} per lead run)")
        if active + len(targets) > max_active:
            raise SystemExit(f"orchestra: active child limit exceeded ({max_active} per lead run)")
        for target, agent, display_model in prepared:
            run_id = None
            for _ in range(names.MAX_ATTEMPTS + 4):
                slug = names.assign_slug(con)
                try:
                    cur = con.execute(
                        "INSERT INTO runs(agent, backend, model, title, work_item, team, "
                        "requested_by, workdir, slug, lead_run, spawn_request_id, child_depth, "
                        "status, started_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'spawning', ?)",
                        (target, agent["backend"], display_model, title or mission[:80],
                         parent["work_item"], parent["team"], parent["agent"], str(root), slug,
                         parent["id"], spawn_request_id,
                         int(parent["child_depth"] or 0) + 1, db.now()),
                    )
                    run_id = int(cur.lastrowid)
                    break
                except sqlite3.IntegrityError as exc:
                    if not names.is_unique_violation(exc):
                        raise
                    names.reset_memory_cache()
            if run_id is None:
                raise SystemExit(f"orchestra: could not mint a unique child run for {target}")
            run_ids.append(run_id)
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise

    try:
        for run_id, (_, agent, _) in zip(run_ids, prepared):
            child = con.execute("SELECT slug FROM runs WHERE id=?", (run_id,)).fetchone()
            child_workdir, branch = str(parent["workdir"] if shared_workdir else root), None
            if not shared_workdir:
                start_point = parent["branch"] or None
                wt, branch = worktree.create(root, run_id, start_point=start_point)
                child_workdir = str(wt)
            text = brief.compose(
                root=root, run_id=run_id, agent=agent, mission=mission,
                work_item=parent["work_item"], team=parent["team"],
                requester=parent["agent"], workdir=child_workdir,
                extra_context=context, lead_run=parent["id"], slug=child["slug"],
            )
            bp = paths.briefs_dir(root) / f"run-{run_id}.md"
            bp.write_text(text)
            lp = paths.logs_dir(root) / f"run-{run_id}.jsonl"
            lp.touch()
            con.execute(
                "UPDATE runs SET brief_path=?, log_path=?, workdir=?, branch=? WHERE id=?",
                (str(bp), str(lp), child_workdir, branch, run_id),
            )
            con.commit()
    except BaseException as exc:
        con.execute(
            f"UPDATE runs SET status='failed', finished_at=?, summary=? "
            f"WHERE id IN ({','.join('?' for _ in run_ids)}) AND status='spawning'",
            (db.now(), f"Child batch setup failed: {str(exc)[:500]}", *run_ids),
        )
        con.commit()
        raise
    return run_ids


def enqueue(con: sqlite3.Connection, parent: sqlite3.Row, targets: list[str],
            mission: str, *, title: str | None = None, context: str | None = None,
            shared_workdir: bool = False) -> int:
    """Record a worker request without launching from inside its sandbox."""
    cur = con.execute(
        "INSERT INTO spawn_requests(lead_run, requested_by, targets_json, mission, "
        "title, context, shared_workdir, status, created_at) "
        "VALUES(?,?,?,?,?,?,?,'pending',?)",
        (parent["id"], parent["agent"], json.dumps(targets), mission, title, context,
         int(shared_workdir), db.now()),
    )
    con.commit()
    return int(cur.lastrowid)


def process_pending(con: sqlite3.Connection, root: Path, cfg: dict, lead_run: int,
                    launcher: Callable[[Path, int], None]) -> list[dict]:
    """Claim and launch this lead's requests from the outer supervisor."""
    results: list[dict] = []
    requests = list(con.execute(
        "SELECT * FROM spawn_requests WHERE lead_run=? AND status='pending' ORDER BY id",
        (lead_run,),
    ))
    for request in requests:
        claimed = con.execute(
            "UPDATE spawn_requests SET status='processing' "
            "WHERE id=? AND status='pending'",
            (request["id"],),
        )
        con.commit()
        if claimed.rowcount != 1:
            continue
        child_ids: list[int] = []
        try:
            targets = json.loads(request["targets_json"])
            if not isinstance(targets, list) or not all(
                    isinstance(target, str) and target for target in targets):
                raise ValueError("spawn request has invalid targets")
            parent = con.execute(
                "SELECT * FROM runs WHERE id=?", (lead_run,)
            ).fetchone()
            if not parent or parent["status"] != "running":
                raise RuntimeError(f"lead run {lead_run} is no longer running")
            fallback_shared = not bool(request["shared_workdir"]) and not (
                root / ".git"
            ).exists()
            warning = (
                "project is not a git repository; using the lead's shared workdir "
                "instead of an isolated child worktree"
                if fallback_shared else None
            )
            child_ids = create(
                con, root, cfg, parent, targets, request["mission"],
                title=request["title"], context=request["context"],
                shared_workdir=bool(request["shared_workdir"]) or fallback_shared,
                spawn_request_id=int(request["id"]),
            )
            for child_id in child_ids:
                try:
                    launcher(root, child_id)
                except Exception as exc:
                    con.execute(
                        "UPDATE runs SET status='failed', finished_at=?, summary=? "
                        "WHERE id=? AND status='spawning'",
                        (db.now(), f"Child supervisor launch failed: {exc}", child_id),
                    )
            con.execute(
                "UPDATE spawn_requests SET status='accepted', child_run_ids_json=?, "
                "error=?, processed_at=? WHERE id=?",
                (json.dumps(child_ids), warning, db.now(), request["id"]),
            )
            con.commit()
            results.append({
                "id": int(request["id"]),
                "status": "accepted",
                "child_run_ids": child_ids,
                "warning": warning,
            })
        except (Exception, SystemExit) as exc:
            error = str(exc)[:1000] or exc.__class__.__name__
            con.execute(
                "UPDATE spawn_requests SET status='failed', child_run_ids_json=?, "
                "error=?, processed_at=? WHERE id=?",
                (json.dumps(child_ids), error, db.now(), request["id"]),
            )
            con.commit()
            results.append({
                "id": int(request["id"]),
                "status": "failed",
                "child_run_ids": child_ids,
                "error": error,
            })
    return results


def fail_unprocessed(con: sqlite3.Connection, lead_run: int, reason: str) -> None:
    con.execute(
        "UPDATE spawn_requests SET status='failed', error=?, processed_at=? "
        "WHERE lead_run=? AND status IN ('pending','processing')",
        (reason[:1000], db.now(), lead_run),
    )
    con.commit()


def _batch_prompt(lead_id: int, children: list[sqlite3.Row]) -> str:
    summaries = "\n".join(
        f"- run {child['id']} ({child['agent']}) {child['status']}"
        f"; branch {child['branch'] or '(shared workdir)'}"
        f"; summary: {(child['summary'] or '(none)')[:500]}"
        for child in children
    )
    return (
        f"All child runs spawned by run {lead_id} have settled. Review their results "
        f"and branches, integrate what is useful, and verify the combined outcome. "
        f"Do not merge blindly.\n\n{summaries}"
    )


def maybe_wake_lead(con: sqlite3.Connection, root: Path, trigger_run_id: int) -> int | None:
    """Atomically create one lead continuation when its child batch settles."""
    trigger = con.execute("SELECT * FROM runs WHERE id=?", (trigger_run_id,)).fetchone()
    if not trigger:
        return None
    candidates: list[tuple[int, int | None]] = []
    if trigger["lead_run"]:
        candidates.append((
            int(trigger["lead_run"]),
            int(trigger["spawn_request_id"]) if trigger["spawn_request_id"] else None,
        ))
    for request in con.execute(
            "SELECT id FROM spawn_requests WHERE lead_run=?", (trigger_run_id,)
    ):
        candidates.append((trigger_run_id, int(request["id"])))
    if not candidates and con.execute(
            "SELECT 1 FROM runs WHERE lead_run=? LIMIT 1", (trigger_run_id,)
    ).fetchone():
        candidates.append((trigger_run_id, None))

    for lead_id, request_id in dict.fromkeys(candidates):
        con.execute("BEGIN IMMEDIATE")
        try:
            lead = con.execute("SELECT * FROM runs WHERE id=?", (lead_id,)).fetchone()
            request = con.execute(
                "SELECT * FROM spawn_requests WHERE id=?", (request_id,)
            ).fetchone() if request_id else None
            if request_id:
                children = list(con.execute(
                    "SELECT * FROM runs WHERE spawn_request_id=? ORDER BY id",
                    (request_id,),
                ))
                already_notified = bool(request and request["notified_at"])
            else:
                children = list(con.execute(
                    "SELECT * FROM runs WHERE lead_run=? ORDER BY id", (lead_id,)
                ))
                already_notified = bool(
                    lead and (lead["child_wakeup_run"] or lead["child_wakeup_message"])
                )
            batch_exists = bool(
                lead and children and (
                    request_id is None
                    or (request and request["status"] == "accepted")
                )
            )
            ready = bool(
                batch_exists and not already_notified and lead["session_ref"]
                and lead["status"] in ("running", "interrupt", "done", "failed")
                and all(child["status"] in db.RUN_TERMINAL for child in children)
            )
            if not ready:
                con.execute("COMMIT")
                continue
            prompt = _batch_prompt(lead_id, children)
            if lead["status"] in ("running", "interrupt"):
                try:
                    delivery_offset = os.path.getsize(lead["log_path"])
                except (OSError, TypeError):
                    delivery_offset = 0
                cur = con.execute(
                    "INSERT INTO messages(sender, recipient, body, work_item, run_id, kind, "
                    "created_at, delivery_offset) VALUES('orchestra',?,?,?,?,"
                    "'interrupt',?,?)",
                    (lead["agent"], prompt, lead["work_item"], lead_id, db.now(),
                     delivery_offset),
                )
                message_id = int(cur.lastrowid)
                if request_id:
                    con.execute(
                        "UPDATE spawn_requests SET wakeup_message=?, notified_at=? "
                        "WHERE id=? AND notified_at IS NULL",
                        (message_id, db.now(), request_id),
                    )
                else:
                    con.execute(
                        "UPDATE runs SET child_wakeup_message=? "
                        "WHERE id=? AND child_wakeup_message IS NULL",
                        (message_id, lead_id),
                    )
                con.execute("COMMIT")
                return None
            from orchestra_cli import supervise  # avoid module cycle
            wake_id = supervise.create_followup(
                con, root, dict(lead), lead["requested_by"], prompt,
                title=f"child results for run {lead_id}", commit=False,
            )
            if request_id:
                con.execute(
                    "UPDATE spawn_requests SET wakeup_run=?, notified_at=? "
                    "WHERE id=? AND notified_at IS NULL",
                    (wake_id, db.now(), request_id),
                )
            else:
                con.execute(
                    "UPDATE runs SET child_wakeup_run=? "
                    "WHERE id=? AND child_wakeup_run IS NULL",
                    (wake_id, lead_id),
                )
            con.execute("COMMIT")
            return wake_id
        except Exception:
            con.execute("ROLLBACK")
            raise
    return None
