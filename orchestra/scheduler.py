"""Deterministic FIFO admission for the one authoritative daemon."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from orchestra import db, runway


ACTIVE = ("starting", "running")


def setting(con, key: str, default=None):
    row = con.execute(
        "SELECT value_json FROM fleet_settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value_json"])
    except (TypeError, ValueError):
        return default


def _active(con) -> tuple[int, dict[str, int]]:
    rows = con.execute(
        "SELECT profile_id,COUNT(*) AS n FROM runs "
        "WHERE status IN ('starting','running') GROUP BY profile_id"
    ).fetchall()
    per_profile = {row["profile_id"]: int(row["n"]) for row in rows}
    return sum(per_profile.values()), per_profile


def _dependencies(con, run_id: int) -> tuple[str | None, str | None]:
    """(hold, terminal skip reason)."""
    rows = con.execute(
        "SELECT d.condition,r.status,r.id,g.name AS group_name,r.group_seq "
        "FROM run_dependencies d JOIN runs r ON r.id=d.depends_on_run_id "
        "JOIN run_groups g ON g.group_id=r.group_id "
        "WHERE d.run_id=? ORDER BY r.id", (run_id,),
    ).fetchall()
    for dependency in rows:
        label = db.run_no(dependency)
        terminal = dependency["status"] in db.RUN_TERMINAL
        if not terminal:
            return f"waiting for {label} to finish", None
        if dependency["condition"] == "success" and \
                dependency["status"] != "completed":
            return None, f"skipped because {label} ended {dependency['status']}"
    return None, None


def _runway_hold(con, source_id: str | None) -> str | None:
    if not source_id:
        return None
    row = con.execute(
        "SELECT * FROM runway_readings WHERE source_id=? ORDER BY id DESC LIMIT 1",
        (source_id,),
    ).fetchone()
    return runway.source_hold(dict(row) if row else None)


def _time_hold(value: str | None) -> str | None:
    if not value:
        return None
    try:
        target = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    return f"scheduled for {target.isoformat()}" if target > datetime.now(timezone.utc) \
        else None


def _set_hold(con, run_id: int, reason: str | None) -> None:
    con.execute(
        "UPDATE runs SET hold_reason=? WHERE id=? AND hold_reason IS NOT ?",
        (reason, run_id, reason),
    )


def admit(con) -> dict:
    """Claim every runnable queued row that fits, in global creation order."""
    con.execute("BEGIN IMMEDIATE")
    try:
        paused = bool(setting(con, "paused", False))
        raw_limit = setting(con, "max_active_runs", 8)
        try:
            global_limit = int(raw_limit)
        except (TypeError, ValueError):
            global_limit = 8
        global_limit = None if global_limit <= 0 else global_limit
        global_active, profile_active = _active(con)
        admitted, skipped, held = [], [], []

        queued = con.execute(
            "SELECT r.*,p.max_concurrency "
            "FROM runs r JOIN profiles p ON p.profile_id=r.profile_id "
            "WHERE r.status='queued' ORDER BY r.id"
        ).fetchall()
        for run in queued:
            run_id = int(run["id"])
            dependency_hold, skip_reason = _dependencies(con, run_id)
            if skip_reason:
                con.execute(
                    "UPDATE runs SET status='skipped',hold_reason=NULL,summary=?,"
                    "finished_at=? WHERE id=? AND status='queued'",
                    (skip_reason, db.now(), run_id),
                )
                skipped.append(run_id)
                continue

            reason = dependency_hold or _time_hold(run["not_before"])
            if reason is None and paused:
                reason = "fleet paused"
            if reason is None:
                reason = _runway_hold(con, run["runway_source_id"])
            if reason is None and global_limit is not None and \
                    global_active >= global_limit:
                reason = f"global capacity {global_active}/{global_limit}"
            profile_limit = run["max_concurrency"]
            profile_count = profile_active.get(run["profile_id"], 0)
            if reason is None and profile_limit is not None and \
                    profile_count >= int(profile_limit):
                reason = f"profile capacity {profile_count}/{profile_limit}"
            if reason:
                _set_hold(con, run_id, reason)
                held.append({"run_id": run_id, "reason": reason})
                continue

            changed = con.execute(
                "UPDATE runs SET status='starting',hold_reason=NULL,started_at=? "
                "WHERE id=? AND status='queued'", (db.now(), run_id),
            )
            if changed.rowcount == 1:
                admitted.append(run_id)
                global_active += 1
                profile_active[run["profile_id"]] = profile_count + 1
        con.commit()
    except BaseException:
        con.rollback()
        raise
    return {"admitted": admitted, "held": held, "skipped": skipped}


def state(con) -> dict:
    active, by_profile = _active(con)
    queued = [dict(row) for row in con.execute(
        "SELECT id,group_id,group_seq,profile_id,title,hold_reason,queued_at "
        "FROM runs WHERE status='queued' ORDER BY id")]
    return {
        "paused": bool(setting(con, "paused", False)),
        "max_active_runs": setting(con, "max_active_runs", 8),
        "active_runs": active,
        "active_by_profile": by_profile,
        "queued_count": len(queued),
        "queued_runs": queued,
    }


def set_paused(con, paused: bool, *, actor: str, request_id: str | None = None,
               note: str | None = None) -> dict:
    now = db.now()
    with con:
        con.execute(
            "INSERT INTO fleet_settings(key,value_json,updated_by,updated_at) "
            "VALUES('paused',?,?,?) ON CONFLICT(key) DO UPDATE SET "
            "value_json=excluded.value_json,revision=fleet_settings.revision+1,"
            "updated_by=excluded.updated_by,updated_at=excluded.updated_at",
            (json.dumps(bool(paused)), actor, now),
        )
        db.record_control(
            con, actor=actor, action="fleet.pause" if paused else "fleet.resume",
            target_type="fleet", target_id=db.instance_id(con), request_id=request_id,
            detail={"note": note} if note else None, outcome="ok",
        )
        db.bump_board_revision(con)
    return state(con)
