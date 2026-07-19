"""Native, backend-neutral child-run creation and lead wakeups."""
from __future__ import annotations

import sqlite3
from pathlib import Path

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


def validate_parent(con: sqlite3.Connection, cfg: dict, run_id: int,
                    identity: str | None) -> sqlite3.Row:
    parent = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not parent:
        raise SystemExit(f"orchestra: active parent run {run_id} not found")
    if not identity or identity != parent["agent"]:
        raise SystemExit("orchestra: spawn identity does not match the active lead run")
    if parent["status"] != "running":
        raise SystemExit(f"orchestra: lead run {run_id} is {parent['status']}, not running")
    max_depth, _, _ = limits(cfg)
    if int(parent["child_depth"] or 0) + 1 > max_depth:
        raise SystemExit(
            f"orchestra: child depth limit reached ({max_depth}); "
            "raise settings.child_max_depth deliberately to allow recursion"
        )
    return parent


def create(con: sqlite3.Connection, root: Path, cfg: dict, parent: sqlite3.Row,
           targets: list[str], mission: str, *, title: str | None = None,
           context: str | None = None, shared_workdir: bool = False) -> list[int]:
    """Create one bounded child batch. Caller starts the supervisors."""
    if not targets:
        raise SystemExit("orchestra: spawn needs at least one --to target")
    _, max_total, max_active = limits(cfg)
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
                        "requested_by, workdir, slug, lead_run, child_depth, status, started_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?, 'spawning', ?)",
                        (target, agent["backend"], display_model, title or mission[:80],
                         parent["work_item"], parent["team"], parent["agent"], str(root), slug,
                         parent["id"], int(parent["child_depth"] or 0) + 1, db.now()),
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
            child_workdir, branch = str(parent["workdir"] if shared_workdir else root), None
            if not shared_workdir:
                start_point = parent["branch"] or None
                wt, branch = worktree.create(root, run_id, start_point=start_point)
                child_workdir = str(wt)
            text = brief.compose(
                root=root, run_id=run_id, agent=agent, mission=mission,
                work_item=parent["work_item"], team=parent["team"],
                requester=parent["agent"], workdir=child_workdir,
                extra_context=context, lead_run=parent["id"],
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


def maybe_wake_lead(con: sqlite3.Connection, root: Path, trigger_run_id: int) -> int | None:
    """Atomically create one lead continuation when its child batch settles."""
    trigger = con.execute("SELECT * FROM runs WHERE id=?", (trigger_run_id,)).fetchone()
    if not trigger:
        return None
    candidates = []
    if trigger["lead_run"]:
        candidates.append(int(trigger["lead_run"]))
    if con.execute("SELECT 1 FROM runs WHERE lead_run=? LIMIT 1",
                   (trigger_run_id,)).fetchone():
        candidates.append(trigger_run_id)

    for lead_id in dict.fromkeys(candidates):
        con.execute("BEGIN IMMEDIATE")
        try:
            lead = con.execute("SELECT * FROM runs WHERE id=?", (lead_id,)).fetchone()
            children = list(con.execute("SELECT * FROM runs WHERE lead_run=? ORDER BY id",
                                        (lead_id,)))
            ready = bool(lead and children and lead["status"] in ("done", "failed")
                         and lead["session_ref"] and lead["child_wakeup_run"] is None
                         and all(c["status"] in db.RUN_TERMINAL for c in children))
            if not ready:
                con.execute("COMMIT")
                continue
            summaries = "\n".join(
                f"- run {c['id']} ({c['agent']}) {c['status']}"
                f"; branch {c['branch'] or '(shared workdir)'}"
                f"; summary: {(c['summary'] or '(none)')[:500]}"
                for c in children
            )
            prompt = (
                f"All child runs spawned by run {lead_id} have settled. Review their results "
                f"and branches, integrate what is useful, and verify the combined outcome. "
                f"Do not merge blindly.\n\n{summaries}\n\n"
                f"Check `orchestra inbox {lead['agent']} --unread --mark-read` for full notices."
            )
            from orchestra_cli import supervise  # avoid module cycle
            wake_id = supervise.create_followup(
                con, root, dict(lead), lead["requested_by"], prompt,
                title=f"child results for run {lead_id}", commit=False,
            )
            con.execute("UPDATE runs SET child_wakeup_run=? WHERE id=? AND child_wakeup_run IS NULL",
                        (wake_id, lead_id))
            con.execute("COMMIT")
            return wake_id
        except Exception:
            con.execute("ROLLBACK")
            raise
    return None
