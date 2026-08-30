"""Durable, bounded child-run delegation.

A worker can request children, but it never launches them. The daemon claims
the request, admits ordinary v2 runs, and lets the normal FIFO scheduler decide
when they start.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Sequence

from orchestra import db, fleet_config, runs
from orchestra.contracts import RunRequest, child_tier_allowed


class DelegationError(ValueError):
    pass


def _parent(con: sqlite3.Connection, run_id: int):
    row = runs.find(con, int(run_id))
    if row is None:
        raise DelegationError(f"no run {run_id}")
    return row


def _targets(value: Sequence[str]) -> list[str]:
    if isinstance(value, (str, bytes)):
        raise DelegationError("profiles must be an array")
    targets = [str(item or "").strip() for item in value]
    if not targets or any(not item for item in targets):
        raise DelegationError("at least one explicit child profile is required")
    return targets


def _setting(con: sqlite3.Connection, key: str, default: int) -> int:
    try:
        return int(fleet_config.fleet_setting(con, key, default))
    except (TypeError, ValueError):
        return default


def _depth(con: sqlite3.Connection, run_id: int) -> int:
    """Count only delegation edges; retries and continuations keep depth."""
    row = con.execute(
        "WITH RECURSIVE ancestry(id,depth) AS ("
        "SELECT id,0 FROM runs WHERE id=? UNION ALL "
        "SELECT source.id,ancestry.depth + CASE "
        "WHEN current.parent_run_id IS NOT NULL THEN 1 ELSE 0 END "
        "FROM ancestry JOIN runs current ON current.id=ancestry.id "
        "JOIN runs source ON source.id=COALESCE(current.parent_run_id,"
        "current.retry_of_run_id,current.continuation_of_run_id)) "
        "SELECT MAX(depth) FROM ancestry",
        (int(run_id),),
    ).fetchone()
    return int(row[0] or 0)


def _tier(parent) -> int:
    try:
        snapshot = json.loads(parent["profile_snapshot"] or "{}")
    except (TypeError, ValueError):
        snapshot = {}
    try:
        return int(snapshot.get("tier", parent["profile_tier"]))
    except (TypeError, ValueError) as exc:
        raise DelegationError("parent profile snapshot has no valid tier") from exc


def _validate(con: sqlite3.Connection, parent, targets: list[str], *,
              already_reserved: bool = False) -> None:
    if parent["status"] in db.RUN_TERMINAL:
        raise DelegationError(f"run {parent['id']} is {parent['status']}")
    if _depth(con, int(parent["id"])) + 1 > _setting(
            con, "delegation_max_depth", 2):
        raise DelegationError("delegation depth limit reached")
    maximum = _setting(con, "delegation_max_children", 3)
    existing = int(con.execute(
        "SELECT COUNT(*) FROM runs WHERE parent_run_id=?", (parent["id"],)
    ).fetchone()[0])
    pending = int(con.execute(
        "SELECT COALESCE(SUM(json_array_length(targets_json)-COALESCE("
        "json_array_length(child_run_ids_json),0)),0) "
        "FROM child_requests WHERE parent_run_id=? AND status IN "
        "('pending','processing')", (parent["id"],)
    ).fetchone()[0])
    reserved = existing + pending + (0 if already_reserved else len(targets))
    if reserved > maximum:
        raise DelegationError(
            f"parent child-run limit is {maximum}; {existing + pending} already reserved")
    active = len(active_children(con, int(parent["id"])))
    active_limit = _setting(con, "delegation_max_active_children", 3)
    active_reserved = active + pending + (0 if already_reserved else len(targets))
    if active_reserved > active_limit:
        raise DelegationError(
            f"parent active-child limit is {active_limit}; "
            f"{active + pending} already reserved")
    parent_tier = _tier(parent)
    for selector in targets:
        profile = fleet_config.find_profile(con, selector)
        if profile is None or profile["archived"] or not profile["enabled"]:
            raise DelegationError(f"child profile {selector!r} is unavailable")
        runtime = fleet_config.find_runtime(con, profile["runtime_id"])
        if runtime is None or runtime["archived"] or not runtime["enabled"]:
            raise DelegationError(
                f"child profile {selector!r} runtime is unavailable")
        if not child_tier_allowed(parent_tier, int(profile["tier"])):
            raise DelegationError(
                f"tier {parent_tier} parent cannot delegate upward "
                f"to tier {profile['tier']}")


def enqueue(
    con: sqlite3.Connection,
    parent_run_id: int,
    profiles: Sequence[str],
    context: str,
    *,
    requested_by: str | None = None,
    title: str | None = None,
    request_id: str | None = None,
) -> tuple[dict, bool]:
    """Persist an idempotent request for explicitly profiled child runs."""
    context = str(context or "").strip()
    if not context:
        raise DelegationError("child context is required")
    targets = _targets(profiles)
    request_id = str(request_id or "").strip() or str(uuid.uuid4())
    requested_by = str(requested_by or f"run:{int(parent_run_id)}").strip()
    if len(request_id) > 200:
        raise DelegationError("request_id exceeds 200 characters")
    if not requested_by or len(requested_by) > 128:
        raise DelegationError("requested_by must be 1 to 128 characters")
    clean_title = (title or "").strip() or None
    if clean_title and len(clean_title) > 200:
        raise DelegationError("title exceeds 200 characters")
    owns_transaction = not con.in_transaction
    if not owns_transaction and not db.in_api_mutation(con):
        raise RuntimeError("child request admission requires a clean transaction")
    if owns_transaction:
        con.execute("BEGIN IMMEDIATE")
    try:
        existing = con.execute(
            "SELECT * FROM child_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        if existing is not None:
            expected = {
                "parent_run_id": int(parent_run_id),
                "requested_by": requested_by,
                "targets_json": json.dumps(targets, separators=(",", ":")),
                "mission": context,
                "title": clean_title,
                "context": None,
            }
            changed = [key for key, value in expected.items()
                       if existing[key] != value]
            if changed:
                raise DelegationError(
                    "request_id already names a different child request: "
                    + ", ".join(changed))
            con.commit()
            return dict(existing), False
        parent = _parent(con, int(parent_run_id))
        _validate(con, parent, targets)
        cursor = con.execute(
            "INSERT INTO child_requests(request_id,parent_run_id,requested_by,"
             "targets_json,mission,title,context,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (request_id, int(parent_run_id), requested_by,
             json.dumps(targets, separators=(",", ":")), context,
             clean_title, None, db.now()),
        )
        db.record_control(
            con, actor=requested_by, action="run.delegate", outcome="queued",
            target_type="run", target_id=parent_run_id, request_id=request_id,
            detail={"profiles": targets},
        )
        row = con.execute(
            "SELECT * FROM child_requests WHERE id=?", (cursor.lastrowid,)
        ).fetchone()
        con.commit()
    except BaseException:
        if con.in_transaction:
            con.rollback()
        raise
    return dict(row), True


def process_pending(con: sqlite3.Connection, parent_run_id: int | None = None,
                    *, limit: int = 20) -> list[dict]:
    """Turn durable requests into ordinary queued runs; launch nothing."""
    where = " AND parent_run_id=?" if parent_run_id is not None else ""
    bounded = max(1, min(int(limit), 100))
    params = (int(parent_run_id), bounded) if parent_run_id is not None \
        else (bounded,)
    requests = list(con.execute(
        "SELECT * FROM child_requests WHERE (status='pending' OR "
        "(status='processing' AND COALESCE(json_array_length("
        "child_run_ids_json),0)<json_array_length(targets_json)))" + where +
        " ORDER BY id LIMIT ?", params))
    results: list[dict] = []
    for request in requests:
        if request["status"] == "pending":
            claimed = con.execute(
                "UPDATE child_requests SET status='processing',processed_at=? "
                "WHERE id=? AND status='pending'", (db.now(), request["id"])
            )
            con.commit()
            if claimed.rowcount != 1:
                continue
        try:
            child_ids = [int(value) for value in json.loads(
                request["child_run_ids_json"] or "[]")]
        except (TypeError, ValueError):
            child_ids = []
        try:
            parent = _parent(con, int(request["parent_run_id"]))
            targets = json.loads(request["targets_json"])
            for index in range(1, len(targets) + 1):
                existing = runs.find_by_request(
                    con, f"child-request:{request['id']}:{index}")
                if existing is None:
                    continue
                existing_id = int(existing["id"])
                if index <= len(child_ids):
                    if child_ids[index - 1] != existing_id:
                        raise DelegationError(
                            "child request recovery found conflicting run ids")
                elif index == len(child_ids) + 1:
                    child_ids.append(existing_id)
            con.execute(
                "UPDATE child_requests SET child_run_ids_json=? WHERE id=?",
                (json.dumps(child_ids, separators=(",", ":")), request["id"]),
            )
            con.commit()
            _validate(con, parent, targets, already_reserved=True)
            for index, profile in enumerate(targets, start=1):
                if index <= len(child_ids):
                    continue
                child, _ = runs.submit(con, RunRequest.from_mapping({
                    "request_id": f"child-request:{request['id']}:{index}",
                    "group": parent["group_slug"],
                    "profile": profile,
                    "context": request["mission"],
                    "title": request["title"],
                    "requested_by": request["requested_by"],
                    "observer": "inherit",
                    "parent_run_id": int(parent["id"]),
                }))
                child_ids.append(int(child["id"]))
                con.execute(
                    "UPDATE child_requests SET child_run_ids_json=? WHERE id=?",
                    (json.dumps(child_ids, separators=(",", ":")), request["id"]),
                )
                con.commit()
            results.append({"request_id": int(request["id"]),
                            "child_run_ids": child_ids, "status": "processing"})
        except BaseException as exc:
            con.execute(
                "UPDATE child_requests SET status='failed',error=?,"
                "child_run_ids_json=? WHERE id=?",
                (str(exc)[:1000], json.dumps(child_ids, separators=(",", ":")),
                 request["id"]),
            )
            con.commit()
            results.append({"request_id": int(request["id"]),
                            "child_run_ids": child_ids, "status": "failed",
                            "error": str(exc)[:1000]})
    return results


def settle_requests(con: sqlite3.Connection) -> list[int]:
    """Mark spawned batches settled once every named child is terminal."""
    settled: list[int] = []
    for request in con.execute(
        "SELECT * FROM child_requests WHERE status='processing' ORDER BY id"
    ):
        try:
            ids = [int(value) for value in json.loads(
                request["child_run_ids_json"] or "[]")]
            target_count = len(json.loads(request["targets_json"] or "[]"))
        except (TypeError, ValueError):
            ids = []
            target_count = 0
        if not ids or len(ids) != target_count:
            continue
        placeholders = ",".join("?" for _ in ids)
        active = con.execute(
            "WITH RECURSIVE lineage(id) AS ("
            f"SELECT id FROM runs WHERE id IN ({placeholders}) UNION ALL "
            "SELECT r.id FROM runs r JOIN lineage l ON "
            "r.retry_of_run_id=l.id OR r.continuation_of_run_id=l.id) "
            f"SELECT 1 FROM runs WHERE id IN (SELECT id FROM lineage) "
            f"AND status NOT IN "
            f"{db.TERMINAL_SQL} LIMIT 1", ids,
        ).fetchone()
        if active is None:
            con.execute(
                "UPDATE child_requests SET status='settled' WHERE id=? "
                "AND status='processing'", (request["id"],)
            )
            settled.append(int(request["id"]))
    if settled:
        con.commit()
    return settled


def active_children(con: sqlite3.Connection, parent_run_id: int) -> list[dict]:
    return [dict(row) for row in con.execute(
        "WITH RECURSIVE lineage(id) AS ("
        "SELECT id FROM runs WHERE parent_run_id=? UNION ALL "
        "SELECT r.id FROM runs r JOIN lineage l ON "
        "r.retry_of_run_id=l.id OR r.continuation_of_run_id=l.id) "
        f"SELECT * FROM runs WHERE id IN (SELECT id FROM lineage) "
        f"AND status NOT IN "
        f"{db.TERMINAL_SQL} ORDER BY id", (int(parent_run_id),)
    )]


def unsettled_requests(con: sqlite3.Connection, parent_run_id: int) -> bool:
    return con.execute(
        "SELECT 1 FROM child_requests WHERE parent_run_id=? "
        "AND status IN ('pending','processing') LIMIT 1",
        (int(parent_run_id),),
    ).fetchone() is not None


def result_generation(con: sqlite3.Connection,
                      parent_run_id: int) -> str | None:
    """Stable marker that changes for every request or direct-child wave."""
    row = con.execute(
        "SELECT (SELECT MAX(id) FROM child_requests WHERE parent_run_id=?) AS "
        "request_id,(SELECT MAX(id) FROM runs WHERE parent_run_id=?) AS child_id",
        (int(parent_run_id), int(parent_run_id)),
    ).fetchone()
    if row is None or (row["request_id"] is None and row["child_id"] is None):
        return None
    return f"r{int(row['request_id'] or 0)}-c{int(row['child_id'] or 0)}"


def results_prompt(con: sqlite3.Connection, parent_run_id: int) -> str:
    children = list(con.execute(
        "WITH RECURSIVE lineage(id) AS ("
        "SELECT id FROM runs WHERE parent_run_id=? UNION ALL "
        "SELECT r.id FROM runs r JOIN lineage l ON "
        "r.retry_of_run_id=l.id OR r.continuation_of_run_id=l.id) "
        "SELECT r.*,g.name AS group_name FROM runs r JOIN run_groups g "
        "ON g.group_id=r.group_id WHERE r.id IN (SELECT id FROM lineage) "
        "ORDER BY r.id",
        (int(parent_run_id),),
    ))
    failed = list(con.execute(
        "SELECT id,error FROM child_requests WHERE parent_run_id=? "
        "AND status='failed' ORDER BY id", (int(parent_run_id),)))
    if not children and not failed:
        return "Continue the original mission."
    lines = ["Your delegated child runs have settled. Use their results and "
             "continue the original mission:"]
    for child in children:
        evidence = []
        if child["branch"]:
            evidence.append(f"branch {child['branch']}")
        if child["diff_path"]:
            evidence.append(f"patch {child['diff_path']}")
        suffix = f" ({', '.join(evidence)})" if evidence else ""
        lines.append(
            f"- {db.run_no(child)}: {child['status']}{suffix}\n"
            f"  {str(child['summary'] or 'No summary captured.').strip()[:2000]}")
    for request in failed:
        lines.append(
            f"- Delegation request {request['id']}: failed\n"
            f"  {str(request['error'] or 'No error captured.').strip()[:1000]}")
    return "\n".join(lines)


def fail_unprocessed(con: sqlite3.Connection, parent_run_id: int,
                     reason: str) -> int:
    changed = con.execute(
        "UPDATE child_requests SET status='failed',error=?,processed_at=COALESCE("
        "processed_at,?) WHERE parent_run_id=? AND (status='pending' OR "
        "(status='processing' AND COALESCE(json_array_length("
        "child_run_ids_json),0)<json_array_length(targets_json)))",
        (str(reason)[:1000], db.now(), int(parent_run_id)),
    ).rowcount
    con.commit()
    return int(changed)
