"""Bounded, out-of-band Observer checks.

Observer is a named runtime subsystem, not a worker run or generic policy
abstraction. It receives only a bounded mission and normalized
trace tail.  This module records checks and constrains authority; the daemon
owns runtime invocation, Tell, and Stop execution.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from orchestra import attention, callbacks, db

FIRST_LOOK_SECONDS = 5 * 60
INTERVAL_SECONDS = 30 * 60
MIN_EVENTS = 5
MAX_EVENTS = 60
MAX_EVENT_CHARS = 400
MAX_MISSION_CHARS = 4_000
MAX_REASON_CHARS = 2_000
MIN_CONCURRENCY = 1
MAX_CONCURRENCY = 8
ACTIVE_STATUS = "running"

INSTRUCTIONS = """\
You are Orchestra's Observer, examining a worker from outside its session.
Judge only whether the run is converging on its mission. Length is not a
fault. Use the supplied normalized trace as evidence. You have no workspace,
tools, credentials, run token, or delegation authority.

Reply with one JSON object and nothing else:
{"action":"ok|tell|stop","reason":"one evidence-based sentence",\
"message":"a concise correction when action is tell or stop"}

Use ok when progress is plausible or evidence is weak. Use tell to correct
drift, repetition, or exploration without progress. Use stop only when new
evidence shows the run remained adverse after a prior correction.
"""


class CheckNotDue(RuntimeError):
    pass


def _epoch(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _utc_epoch(value: str | None = None) -> float:
    parsed = _epoch(value)
    return parsed if parsed is not None else datetime.now(timezone.utc).timestamp()


def _redact(value):
    """Defensively remove obvious secrets from the stored profile snapshot."""
    if isinstance(value, dict):
        return {key: ("[redacted]" if any(part in key.casefold() for part in
                                          ("secret", "token", "password",
                                           "credential", "api_key"))
                      else _redact(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _events(con: sqlite3.Connection, run_id: int,
            limit: int = MAX_EVENTS) -> list[dict]:
    rows = list(con.execute(
        "SELECT seq,kind,name,payload,ts FROM events WHERE run_id=? "
        "ORDER BY seq DESC LIMIT ?", (run_id, max(1, min(limit, MAX_EVENTS)))
    ))[::-1]
    return [{
        "seq": int(row["seq"]),
        "kind": row["kind"],
        "name": row["name"],
        "payload": " ".join((row["payload"] or "").split())[:MAX_EVENT_CHARS],
        "ts": row["ts"],
    } for row in rows]


def bounded_input(con: sqlite3.Connection, run, *,
                  limit: int = MAX_EVENTS) -> dict:
    """Build the entire payload an Observer runtime may receive."""
    events = _events(con, int(run["id"]), limit)
    mission = str(run["mission"] or "")[:MAX_MISSION_CHARS]
    return {
        "mission": mission,
        "title": str(run["title"] or "")[:300],
        "event_seq_start": events[0]["seq"] if events else None,
        "event_seq_end": events[-1]["seq"] if events else None,
        "event_count": len(events),
        "events": events,
    }


def prompt(payload: dict) -> str:
    return INSTRUCTIONS + "\n--- bounded input ---\n" + json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"))


def due(con: sqlite3.Connection, run_id: int, *, at: str | None = None,
        first_look: int = FIRST_LOOK_SECONDS,
        interval: int = INTERVAL_SECONDS,
        min_events: int = MIN_EVENTS) -> tuple[bool, str]:
    """Return whether a scheduled check is due and the reason."""
    run = con.execute(
        "SELECT id,status,started_at FROM runs WHERE id=?", (run_id,)
    ).fetchone()
    if run is None:
        raise KeyError(f"no run {run_id}")
    if run["status"] != ACTIVE_STATUS:
        return False, f"run is {run['status']}"
    facts = con.execute(
        "SELECT COUNT(*) AS count,MAX(seq) AS last_seq FROM events WHERE run_id=?",
        (run_id,),
    ).fetchone()
    if int(facts["count"] or 0) < min_events:
        return False, "not enough trace evidence"
    now_epoch = _utc_epoch(at)
    last = con.execute(
        "SELECT event_seq_end,finished_at FROM observer_checks WHERE run_id=? "
        "AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 1", (run_id,)
    ).fetchone()
    if last is None:
        started = _epoch(run["started_at"])
        if started is None or now_epoch - started < first_look:
            return False, "first-look delay"
        return True, "first look"
    if int(facts["last_seq"] or 0) <= int(last["event_seq_end"] or 0):
        return False, "no new evidence"
    finished = _epoch(last["finished_at"])
    if finished is None or now_epoch - finished < interval:
        return False, "check interval"
    return True, "new evidence"


def prepare_check(con: sqlite3.Connection, run_id: int, *, profile_id=None,
                  profile_snapshot: dict | None = None,
                  runtime_snapshot: dict | None = None,
                  authority: str = "correct_then_stop",
                  trigger: str = "scheduled",
                  max_concurrency: int = MIN_CONCURRENCY) -> dict:
    """Persist the start of a check and return its bounded runtime prompt."""
    if trigger not in {"scheduled", "manual"}:
        raise ValueError("trigger must be scheduled or manual")
    if authority not in {"advisory", "tell_only", "correct_then_stop"}:
        raise ValueError(f"unknown Observer authority: {authority}")
    if (isinstance(max_concurrency, bool) or
            not isinstance(max_concurrency, int) or
            not MIN_CONCURRENCY <= max_concurrency <= MAX_CONCURRENCY):
        raise ValueError(
            f"Observer concurrency must be an integer from {MIN_CONCURRENCY} "
            f"to {MAX_CONCURRENCY}")
    if con.in_transaction:
        raise RuntimeError("Observer check admission requires a clean transaction")
    con.execute("BEGIN IMMEDIATE")
    try:
        run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise KeyError(f"no run {run_id}")
        if run["status"] != ACTIVE_STATUS:
            raise CheckNotDue(f"run {run_id} is {run['status']}")
        active = con.execute(
            "SELECT id FROM observer_checks WHERE run_id=? "
            "AND finished_at IS NULL LIMIT 1", (run_id,)
        ).fetchone()
        if active is not None:
            raise CheckNotDue(
                f"Observer check {active['id']} is already active for run {run_id}")
        active_count = int(con.execute(
            "SELECT COUNT(*) FROM observer_checks WHERE finished_at IS NULL"
        ).fetchone()[0])
        if active_count >= max_concurrency:
            raise CheckNotDue(
                f"Observer concurrency limit {max_concurrency} is full")
        payload = bounded_input(con, run)
        if payload["event_count"] < MIN_EVENTS:
            raise CheckNotDue("not enough trace evidence")
        profile_json = json.dumps(
            _redact(profile_snapshot or {}), ensure_ascii=False,
            separators=(",", ":"), sort_keys=True)
        runtime_json = json.dumps(
            _redact(runtime_snapshot or {}), ensure_ascii=False,
            separators=(",", ":"), sort_keys=True)
        input_json = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        cursor = con.execute(
            "INSERT INTO observer_checks("
            "run_id,profile_id,profile_snapshot,runtime_snapshot,input_json,"
            "trigger,authority,event_seq_start,event_seq_end,event_count,started_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, profile_id, profile_json, runtime_json, input_json,
             trigger, authority, payload["event_seq_start"],
             payload["event_seq_end"], payload["event_count"], db.now()),
        )
        con.commit()
    except BaseException:
        if con.in_transaction:
            con.rollback()
        raise
    return {"check_id": int(cursor.lastrowid), "input": payload,
            "prompt": prompt(payload)}


def find_check(con: sqlite3.Connection, check_id: int):
    return con.execute(
        "SELECT * FROM observer_checks WHERE id=?", (int(check_id),)
    ).fetchone()


def active_check(con: sqlite3.Connection):
    return con.execute(
        "SELECT * FROM observer_checks WHERE finished_at IS NULL ORDER BY id LIMIT 1"
    ).fetchone()


def active_checks(con: sqlite3.Connection):
    return con.execute(
        "SELECT * FROM observer_checks WHERE finished_at IS NULL ORDER BY id"
    ).fetchall()


def check_prompt(check) -> str:
    try:
        payload = json.loads(check["input_json"] or "{}")
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Observer check has invalid frozen input") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Observer check has invalid frozen input")
    return prompt(payload)


def _last_json_object(text: str) -> dict | None:
    decoder = json.JSONDecoder()
    raw = (text or "").strip()
    candidates = []
    for index, char in enumerate(raw):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(raw[index:])
        except ValueError:
            continue
        if isinstance(value, dict):
            candidates.append(value)
    return candidates[-1] if candidates else None


def parse_verdict(text: str) -> dict:
    """Parse a cautious verdict; malformed output can never stop a run."""
    value = _last_json_object(text)
    if value is None:
        return {"action": "ok", "reason": "Observer output was not JSON",
                "message": "", "unparsed": (text or "")[:500]}
    action = str(value.get("action") or "ok").strip().lower()
    if action not in {"ok", "tell", "stop"}:
        action = "ok"
    return {
        "action": action,
        "reason": str(value.get("reason") or "")[:MAX_REASON_CHARS],
        "message": str(value.get("message") or "")[:MAX_REASON_CHARS],
    }


def enforce_authority(con: sqlite3.Connection, run_id: int, verdict: dict,
                      event_seq_end: int | None,
                      authority: str = "correct_then_stop") -> dict:
    """A first adverse verdict corrects; only later evidence may stop."""
    if authority not in {"advisory", "tell_only", "correct_then_stop"}:
        raise ValueError(f"unknown Observer authority: {authority}")
    result = dict(verdict)
    if authority == "advisory" and result.get("action") in {"tell", "stop"}:
        result["action"] = "ok"
        return result
    if authority == "tell_only" and result.get("action") == "stop":
        result["action"] = "tell"
        result["message"] = result.get("message") or result.get("reason") or (
            "Return to the stated mission and report plainly if blocked.")
        return result
    if result.get("action") != "stop":
        return result
    previous = con.execute(
        "SELECT event_seq_end FROM observer_checks WHERE run_id=? "
        "AND action='tell' AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    if previous is None:
        result["action"] = "tell"
        result["message"] = result.get("message") or result.get("reason") or (
            "Stop the current loop, return to the stated mission, and report "
            "plainly if you are blocked.")
        return result
    if int(event_seq_end or 0) <= int(previous["event_seq_end"] or 0):
        return {"action": "ok", "reason": "no new evidence since correction",
                "message": ""}
    return result


def finish_check(con: sqlite3.Connection, check_id: int, verdict: dict | str, *,
                 usage: dict | None = None, error: str | None = None,
                 authority: str | None = None) -> dict:
    """Finish a check, applying Observer's correct-then-stop authority."""
    if con.in_transaction:
        raise RuntimeError("Observer check completion requires a clean transaction")
    con.execute("BEGIN IMMEDIATE")
    try:
        check = con.execute(
            "SELECT * FROM observer_checks WHERE id=?", (check_id,)
        ).fetchone()
        if check is None:
            raise KeyError(f"no Observer check {check_id}")
        if check["finished_at"] is not None:
            raise RuntimeError(f"Observer check {check_id} is already finished")
        authority = authority or check["authority"]
        if error:
            final = {"action": "error", "reason": str(error)[:MAX_REASON_CHARS],
                     "message": ""}
            verdict_name, proposed_action = "error", "error"
        else:
            parsed = parse_verdict(verdict) if isinstance(verdict, str) else dict(verdict)
            if parsed.get("action") not in {"ok", "tell", "stop"}:
                raise ValueError("Observer action must be ok, tell, or stop")
            proposed_action = parsed["action"]
            verdict_name = "converging" if proposed_action == "ok" else "adverse"
            final = enforce_authority(
                con, int(check["run_id"]), parsed, check["event_seq_end"], authority)
        usage = usage or {}
        detail = json.dumps({"message": final.get("message", ""),
                             "proposed_action": proposed_action,
                             "delivery_status": (
                                 "pending" if final["action"] in {"tell", "stop"}
                                 else "not_required")},
                            ensure_ascii=False, separators=(",", ":"))
        changed = con.execute(
            "UPDATE observer_checks SET verdict=?,action=?,reason=?,detail_json=?,"
            "delivery_status=?,tokens_in=?,tokens_out=?,tokens_total=?,cost_usd=?,"
            "finished_at=?,error=? "
            "WHERE id=? AND finished_at IS NULL",
            (verdict_name, final["action"],
             final.get("reason", "")[:MAX_REASON_CHARS], detail,
             "pending" if final["action"] in {"tell", "stop"}
             else "not_required",
             usage.get("tokens_in"), usage.get("tokens_out"),
             usage.get("tokens_total"), usage.get("cost_usd"), db.now(),
             str(error)[:MAX_REASON_CHARS] if error else None, check_id),
        ).rowcount
        if changed != 1:
            raise RuntimeError(f"Observer check {check_id} changed concurrently")
        con.commit()
    except BaseException:
        if con.in_transaction:
            con.rollback()
        raise
    return {"check_id": check_id, **final}


def checks(con: sqlite3.Connection, run_id: int) -> list[dict]:
    return [dict(row) for row in con.execute(
        "SELECT * FROM observer_checks WHERE run_id=? ORDER BY id", (run_id,)
    )]


def publish_stop(con: sqlite3.Connection, *, run_id: int, check_id: int,
                 reason: str,
                 callback_command=None) -> dict:
    """Open the durable alert after the daemon successfully stops a run."""
    request, created = attention.open_request(
        con,
        kind="alert",
        title=f"Observer stopped run {run_id}",
        body=reason or "Observer stopped this run after a prior correction.",
        created_by="observer",
        run_id=run_id,
        correlation_id=f"observer-stop:{check_id}",
    )
    if created:
        callbacks.emit(callback_command, "observer.stopped", {
            "run_id": run_id,
            "check_id": check_id,
            "attention_id": request["id"],
            "reason": reason[:MAX_REASON_CHARS],
        }, audit_db=con)
    return request
