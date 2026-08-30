"""Durable per-run threads and delivery receipts."""
from __future__ import annotations

import os
import sqlite3

from orchestra import db

DIRECTIONS = frozenset({"inbound", "outbound", "system"})
STATUSES = frozenset({"pending", "delivered", "undeliverable"})
ACTIVE_RUNS = frozenset({"queued", "starting", "running", "waiting"})
TELL_KIND = "tell"
MAX_BODY = 128 * 1024


class RunClosed(RuntimeError):
    pass


class CorrelationConflict(ValueError):
    pass


def _text(value: str, label: str, limit: int) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise ValueError(f"{label} is required")
    if len(clean) > limit:
        raise ValueError(f"{label} exceeds {limit} characters")
    return clean


def post(con: sqlite3.Connection, run_id: int, *, direction: str, sender: str,
         body: str, kind: str = "message", status: str = "pending",
         correlation_id: str | None = None, reply_to: int | None = None,
         delivery_offset: int | None = None, commit: bool = True) -> int:
    """Append one message, validating its run and reply boundary."""
    if direction not in DIRECTIONS:
        raise ValueError(f"unknown message direction: {direction}")
    if status not in STATUSES:
        raise ValueError(f"unknown message status: {status}")
    sender = _text(sender, "sender", 200)
    body = _text(body, "body", MAX_BODY)
    kind = _text(kind, "kind", 100)
    correlation_id = (correlation_id or "").strip() or None
    if commit:
        if con.in_transaction and not db.in_api_mutation(con):
            raise RuntimeError("message admission requires a clean transaction")
        if not con.in_transaction:
            con.execute("BEGIN IMMEDIATE")
    elif not con.in_transaction:
        raise RuntimeError("commit=False requires a caller-owned transaction")
    try:
        run = con.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise KeyError(f"no run {run_id}")
        if direction == "inbound" and status == "pending" and (
                run["status"] not in ACTIVE_RUNS):
            raise RunClosed(f"run {run_id} is {run['status']}")
        if reply_to is not None and con.execute(
                "SELECT 1 FROM messages WHERE id=? AND run_id=?",
                (reply_to, run_id)).fetchone() is None:
            raise ValueError("reply_to must name a message on the same run")
        conflict = (
            " ON CONFLICT(run_id,kind,correlation_id) "
            "WHERE correlation_id IS NOT NULL DO NOTHING"
            if correlation_id else "")
        timestamp = db.now()
        cursor = con.execute(
            "INSERT INTO messages("
            "run_id,direction,sender,body,kind,status,correlation_id,reply_to,"
            "created_at,delivery_offset,delivered_at,undeliverable_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)" + conflict,
            (run_id, direction, sender, body, kind, status, correlation_id,
             reply_to, timestamp, delivery_offset,
             timestamp if status == "delivered" else None,
             timestamp if status == "undeliverable" else None),
        )
        if cursor.rowcount:
            message_id = int(cursor.lastrowid)
        else:
            existing = con.execute(
                "SELECT * FROM messages WHERE run_id=? AND kind=? "
                "AND correlation_id=?", (run_id, kind, correlation_id)
            ).fetchone()
            if existing is None:
                raise RuntimeError("correlated message insert was ignored unexpectedly")
            expected = {
                "direction": direction, "sender": sender, "body": body,
                "reply_to": reply_to,
            }
            mismatched = [key for key, value in expected.items()
                          if existing[key] != value]
            if mismatched:
                raise CorrelationConflict(
                    "correlation_id already names a different message: "
                    + ", ".join(mismatched))
            message_id = int(existing["id"])
        if commit:
            con.commit()
    except BaseException:
        if commit and con.in_transaction:
            con.rollback()
        raise
    return message_id


def queue_tell(con: sqlite3.Connection, run_id: int, sender: str, body: str,
               log_path: str | None = None, *, boundary: bool = True,
               correlation_id: str | None = None, commit: bool = True) -> int:
    """Queue a Tell for live steering or the next safe process boundary."""
    offset = None
    if boundary:
        try:
            offset = os.path.getsize(log_path) if log_path else 0
        except OSError:
            offset = 0
    return post(con, run_id, direction="inbound", sender=sender, body=body,
                kind=TELL_KIND, correlation_id=correlation_id,
                delivery_offset=offset, commit=commit)


def claim_pending(con: sqlite3.Connection, run_id: int) -> list[dict]:
    """Atomically claim pending Tells and accepted answers exactly once."""
    if con.in_transaction:
        raise RuntimeError("message claim requires a clean transaction")
    con.execute("BEGIN IMMEDIATE")
    try:
        rows = list(con.execute(
            "SELECT * FROM messages WHERE run_id=? AND direction='inbound' "
            "AND kind IN ('tell','answer') AND status='pending' ORDER BY id",
            (run_id,)
        ))
        if rows:
            ids = [int(row["id"]) for row in rows]
            placeholders = ",".join("?" for _ in ids)
            con.execute(
                f"UPDATE messages SET status='delivered',delivered_at=? "
                f"WHERE status='pending' AND id IN ({placeholders})",
                (db.now(), *ids),
            )
        con.commit()
    except BaseException:
        if con.in_transaction:
            con.rollback()
        raise
    return [dict(row) for row in rows]


def acknowledge(con: sqlite3.Connection, message_id: int) -> bool:
    """Record delivery of any pending message."""
    if con.in_transaction:
        raise RuntimeError("message acknowledgement requires a clean transaction")
    con.execute("BEGIN IMMEDIATE")
    try:
        changed = con.execute(
            "UPDATE messages SET status='delivered',delivered_at=? "
            "WHERE id=? AND status='pending'", (db.now(), message_id)
        ).rowcount
        con.commit()
    except BaseException:
        if con.in_transaction:
            con.rollback()
        raise
    return changed == 1


def mark_undeliverable(con: sqlite3.Connection, run_id: int, reason: str, *,
                       commit: bool = True) -> int:
    """Close only messages aimed at a run; its outbound thread remains intact."""
    reason = _text(reason, "reason", 500)
    if commit:
        if con.in_transaction and not db.in_api_mutation(con):
            raise RuntimeError("message finalization requires a clean transaction")
        if not con.in_transaction:
            con.execute("BEGIN IMMEDIATE")
    elif not con.in_transaction:
        raise RuntimeError("commit=False requires a caller-owned transaction")
    try:
        changed = con.execute(
            "UPDATE messages SET status='undeliverable',undeliverable_at=?,"
            "undeliverable_reason=? WHERE run_id=? AND direction='inbound' "
            "AND status='pending'", (db.now(), reason, run_id)
        ).rowcount
        if commit:
            con.commit()
    except BaseException:
        if commit and con.in_transaction:
            con.rollback()
        raise
    return changed


def thread(con: sqlite3.Connection, run_id: int, *, after: int = 0,
           limit: int = 200) -> list[dict]:
    limit = max(1, min(int(limit), 500))
    return [dict(row) for row in con.execute(
        "SELECT * FROM messages WHERE run_id=? AND id>? ORDER BY id LIMIT ?",
        (run_id, int(after), limit),
    )]


def outbox(con: sqlite3.Connection, *, after: int = 0,
           limit: int = 100) -> list[dict]:
    """Fleet-wide worker-to-operator messages, suitable for cursor paging."""
    limit = max(1, min(int(limit), 500))
    return [dict(row) for row in con.execute(
        "SELECT * FROM messages WHERE direction='outbound' AND id>? "
        "ORDER BY id LIMIT ?", (int(after), limit)
    )]


def undeliverable(con: sqlite3.Connection,
                  run_id: int | None = None) -> list[dict]:
    if run_id is None:
        rows = con.execute(
            "SELECT * FROM messages WHERE status='undeliverable' ORDER BY id")
    else:
        rows = con.execute(
            "SELECT * FROM messages WHERE run_id=? AND status='undeliverable' "
            "ORDER BY id", (run_id,))
    return [dict(row) for row in rows]


def render_delivery(rows) -> str:
    joined = "\n\n".join(
        f"[message from {row['sender']}]\n{row['body']}" for row in rows)
    return ("Apply the following delivered message(s), then continue the "
            f"mission.\n\n{joined}")
