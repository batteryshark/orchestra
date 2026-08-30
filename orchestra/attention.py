"""Durable, transport-neutral attention requests.

Questions and decisions may suspend a run; alerts and profile proposals never
do.  Authentication happens at the API boundary, while this module enforces
the important storage rule: the first authorized response wins atomically and
later responses remain visible as rejected attempts.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone

from orchestra import callbacks, db, messaging

KINDS = frozenset({"question", "decision", "alert", "profile_proposal"})
BLOCKING_KINDS = frozenset({"question", "decision"})
MAX_TITLE = 300
MAX_BODY = 64 * 1024
MAX_JSON = 64 * 1024


class AttentionError(ValueError):
    pass


class CorrelationConflict(AttentionError):
    pass


def _required(value: str, label: str, limit: int) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise AttentionError(f"{label} is required")
    if len(clean) > limit:
        raise AttentionError(f"{label} exceeds {limit} characters")
    return clean


def _json(value, label: str) -> str | None:
    if value is None:
        return None
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                             sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise AttentionError(f"{label} must be JSON serializable") from exc
    if len(encoded.encode()) > MAX_JSON:
        raise AttentionError(f"{label} exceeds {MAX_JSON} bytes")
    return encoded


def _choices(values: Sequence[str | Mapping] | None) -> list[dict]:
    choices: list[dict] = []
    seen: set[str] = set()
    for value in values or ():
        if isinstance(value, str):
            item = {"id": value, "label": value}
        elif isinstance(value, Mapping):
            item = {"id": str(value.get("id") or "").strip(),
                    "label": str(value.get("label") or "").strip()}
        else:
            raise AttentionError("each choice must be text or an object")
        item["id"] = _required(item["id"], "choice id", 100)
        item["label"] = _required(item["label"], "choice label", 300)
        if item["id"] in seen:
            raise AttentionError(f"duplicate choice id: {item['id']}")
        seen.add(item["id"])
        choices.append(item)
    return choices


def _deadline(value: str | None) -> str | None:
    if not value:
        return None
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AttentionError("deadline must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise AttentionError("deadline must include a timezone")
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(con: sqlite3.Connection, request_id: int):
    return con.execute(
        "SELECT * FROM attention_requests WHERE id=?", (request_id,)
    ).fetchone()


def _response_body(response: Mapping) -> str:
    body = str(response.get("body") or "").strip()
    if body:
        return body
    choice = str(response.get("choice") or "").strip()
    if choice:
        return f"Selected: {choice}"
    if "fallback" in response:
        return "Fallback applied: " + json.dumps(
            response["fallback"], ensure_ascii=False, separators=(",", ":"))
    return "Resolved"


def open_request(
    con: sqlite3.Connection,
    *,
    kind: str,
    title: str,
    body: str,
    created_by: str,
    run_id: int | None = None,
    blocking: bool = False,
    choices: Sequence[str | Mapping] | None = None,
    fallback: Mapping | None = None,
    proposal: Mapping | None = None,
    correlation_id: str | None = None,
    deadline: str | None = None,
    callback_command: str | Sequence[str] | None = None,
) -> tuple[dict, bool]:
    """Create a request, or return its idempotent correlation match."""
    if kind not in KINDS:
        raise AttentionError(f"unknown attention kind: {kind}")
    if blocking and kind not in BLOCKING_KINDS:
        raise AttentionError(f"{kind} attention cannot block a run")
    if blocking and run_id is None:
        raise AttentionError("blocking attention requires a run")
    title = _required(title, "title", MAX_TITLE)
    body = _required(body, "body", MAX_BODY)
    created_by = _required(created_by, "created_by", 200)
    correlation_id = (correlation_id or "").strip() or str(uuid.uuid4())
    if len(correlation_id) > 300:
        raise AttentionError("correlation_id exceeds 300 characters")
    normalized_choices = _choices(choices)
    if kind == "profile_proposal":
        if proposal is None:
            raise AttentionError("profile_proposal requires a proposal patch")
        if not normalized_choices:
            normalized_choices = [
                {"id": "approve", "label": "Approve"},
                {"id": "reject", "label": "Reject"},
            ]
    elif proposal is not None:
        raise AttentionError("proposal is only valid for profile_proposal")
    values = {
        "run_id": run_id,
        "kind": kind,
        "blocking": int(blocking),
        "title": title,
        "body": body,
        "choices_json": _json(normalized_choices, "choices"),
        "fallback_json": _json(fallback, "fallback"),
        "proposal_json": _json(proposal, "proposal"),
        "correlation_id": correlation_id,
        "deadline": _deadline(deadline),
        "created_by": created_by,
    }
    owns_transaction = not con.in_transaction
    if not owns_transaction and not db.in_api_mutation(con):
        raise RuntimeError("attention admission requires a clean transaction")
    if owns_transaction:
        con.execute("BEGIN IMMEDIATE")
    try:
        existing = None
        if correlation_id:
            existing = con.execute(
                "SELECT * FROM attention_requests WHERE correlation_id=?",
                (correlation_id,),
            ).fetchone()
        if existing:
            mismatched = [key for key, value in values.items()
                          if existing[key] != value]
            if mismatched:
                raise CorrelationConflict(
                    "correlation_id already names a different request: "
                    + ", ".join(mismatched))
            con.commit()
            return dict(existing), False
        if blocking:
            open_blocker = con.execute(
                "SELECT id FROM attention_requests WHERE run_id=? "
                "AND blocking=1 AND status='open' LIMIT 1", (run_id,)
            ).fetchone()
            if open_blocker:
                raise AttentionError(
                    f"run {run_id} already has blocking attention "
                    f"{open_blocker['id']}")
            changed = con.execute(
                "UPDATE runs SET status='waiting',waiting_kind='input',"
                "hold_reason=NULL WHERE id=? AND status='running'", (run_id,)
            ).rowcount
            if changed != 1:
                state = con.execute(
                    "SELECT status FROM runs WHERE id=?", (run_id,)
                ).fetchone()
                raise AttentionError(
                    f"blocking attention needs a running run; {run_id} is "
                    f"{state['status'] if state else 'missing'}")
        cursor = con.execute(
            "INSERT INTO attention_requests("
            "run_id,kind,status,blocking,title,body,choices_json,fallback_json,"
            "proposal_json,correlation_id,deadline,created_by,created_at) "
            "VALUES(?,?,'open',?,?,?,?,?,?,?,?,?,?)",
            (run_id, kind, int(blocking), title, body, values["choices_json"],
             values["fallback_json"], values["proposal_json"], correlation_id,
             values["deadline"], created_by, db.now()),
        )
        request = dict(_row(con, int(cursor.lastrowid)))
        if run_id is not None:
            messaging.post(
                con, run_id,
                direction="system" if kind == "alert" else "outbound",
                sender=created_by,
                body=f"{title}\n\n{body}",
                kind=kind,
                status="delivered",
                correlation_id=correlation_id,
                commit=False,
            )
        con.commit()
    except BaseException:
        if con.in_transaction:
            con.rollback()
        raise
    if callback_command:
        callbacks.emit(callback_command, "attention.opened", {
            "attention_id": request["id"],
            "run_id": request["run_id"],
            "kind": request["kind"],
            "blocking": bool(request["blocking"]),
            "title": request["title"],
        }, audit_db=con)
    return request, True


def answer(con: sqlite3.Connection, request_id: int, *, actor: str,
           response: Mapping, authorized: bool, on_accept=None) -> dict:
    """Persist an answer; exactly one authorized answer is accepted."""
    if not authorized:
        raise PermissionError("this credential cannot answer attention")
    actor = _required(actor, "actor", 200)
    if not isinstance(response, Mapping):
        raise AttentionError("response must be an object")
    if not str(response.get("body") or "").strip() and \
            not str(response.get("choice") or "").strip() and \
            "fallback" not in response:
        raise AttentionError("an answer or choice is required")
    response_json = _json(dict(response), "response")
    owns_transaction = not con.in_transaction
    if not owns_transaction and not db.in_api_mutation(con):
        raise RuntimeError("answer admission requires a clean transaction")
    if owns_transaction:
        con.execute("BEGIN IMMEDIATE")
    try:
        request = _row(con, request_id)
        if request is None:
            raise KeyError(f"no attention request {request_id}")
        choice = str(response.get("choice") or "").strip()
        choices = json.loads(request["choices_json"] or "[]")
        if choice and choice not in {item["id"] for item in choices}:
            raise AttentionError(f"unknown choice: {choice}")
        if request["kind"] == "profile_proposal" and choice not in {
                "approve", "reject"}:
            raise AttentionError("profile proposals require approve or reject")
        accepted = request["status"] == "open"
        if accepted:
            changed = con.execute(
                "UPDATE attention_requests SET status='resolved',resolved_at=?,"
                "resolution_json=?,resolved_by=? WHERE id=? AND status='open'",
                (db.now(), response_json, actor, request_id),
            ).rowcount
            accepted = changed == 1
            if accepted and on_accept is not None:
                on_accept(dict(request), dict(response))
            if accepted and request["run_id"] is not None:
                reply = con.execute(
                    "SELECT id FROM messages WHERE run_id=? AND correlation_id=? "
                    "ORDER BY id LIMIT 1",
                    (request["run_id"], request["correlation_id"]),
                ).fetchone()
                messaging.post(
                    con, int(request["run_id"]), direction="inbound",
                    sender=actor, body=_response_body(response), kind="answer",
                    status="pending" if request["blocking"] else "delivered",
                    correlation_id=request["correlation_id"],
                    reply_to=int(reply["id"]) if reply else None,
                    commit=False,
                )
        response_id = int(con.execute(
            "INSERT INTO attention_responses("
            "attention_id,actor,response_json,accepted,created_at) VALUES(?,?,?,?,?)",
            (request_id, actor, response_json, int(accepted), db.now()),
        ).lastrowid)
        request = dict(_row(con, request_id))
        con.commit()
    except BaseException:
        if con.in_transaction:
            con.rollback()
        raise
    return {"request": request, "accepted": accepted,
            "response_id": response_id}


def cancel(con: sqlite3.Connection, request_id: int, *, actor: str,
           reason: str = "", notify_run: bool = True) -> bool:
    """Cancel an unresolved request. Resolved answers are immutable."""
    actor = _required(actor, "actor", 200)
    resolution = _json({"reason": reason.strip()}, "cancellation")
    owns_transaction = not con.in_transaction
    if not owns_transaction and not db.in_api_mutation(con):
        raise RuntimeError("cancellation requires a clean transaction")
    if owns_transaction:
        con.execute("BEGIN IMMEDIATE")
    try:
        request = _row(con, request_id)
        if request is None:
            raise KeyError(f"no attention request {request_id}")
        changed = con.execute(
            "UPDATE attention_requests SET status='cancelled',resolved_at=?,"
            "resolution_json=?,resolved_by=? WHERE id=? AND status='open'",
            (db.now(), resolution, actor, request_id),
        ).rowcount
        if changed and notify_run and request["run_id"] is not None and \
                request["blocking"]:
            reply = con.execute(
                "SELECT id FROM messages WHERE run_id=? AND correlation_id=? "
                "ORDER BY id LIMIT 1",
                (request["run_id"], request["correlation_id"]),
            ).fetchone()
            messaging.post(
                con, int(request["run_id"]), direction="inbound", sender=actor,
                body="Cancelled" + (f": {reason.strip()}" if reason.strip() else ""),
                kind="answer", correlation_id=request["correlation_id"],
                reply_to=int(reply["id"]) if reply else None, commit=False,
            )
        con.commit()
    except BaseException:
        if con.in_transaction:
            con.rollback()
        raise
    return changed == 1


def apply_due_fallbacks(con: sqlite3.Connection, *, at: str | None = None) -> list[int]:
    """Resolve only requests that explicitly declare both deadline and fallback."""
    due = list(con.execute(
        "SELECT id, fallback_json FROM attention_requests WHERE status='open' "
        "AND deadline IS NOT NULL AND deadline<=? AND fallback_json IS NOT NULL "
        "ORDER BY id", (at or db.now(),)
    ))
    resolved = []
    for row in due:
        result = answer(
            con, int(row["id"]), actor="deadline",
            response={"fallback": json.loads(row["fallback_json"])},
            authorized=True,
        )
        if result["accepted"]:
            resolved.append(int(row["id"]))
    return resolved


def inbox(con: sqlite3.Connection, *, status: str = "open",
          after: int = 0, limit: int = 100) -> list[dict]:
    if status not in {"open", "resolved", "cancelled"}:
        raise AttentionError(f"unknown attention status: {status}")
    limit = max(1, min(int(limit), 500))
    return [dict(row) for row in con.execute(
        "SELECT * FROM attention_requests WHERE status=? AND id>? "
        "ORDER BY id LIMIT ?", (status, int(after), limit)
    )]
