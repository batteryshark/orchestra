"""Small durable replay guard for HTTP mutations."""
from __future__ import annotations

import hashlib
import json

from orchestra import db


class Conflict(RuntimeError):
    pass


def body_hash(body) -> str:
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def reserve(con, request_id: str, method: str, path: str,
            body) -> dict | None:
    """Reserve inside a caller-owned transaction without publishing a gap."""
    request_id = (request_id or "").strip()
    if not request_id:
        raise ValueError("request_id is required")
    if not con.in_transaction:
        raise RuntimeError("reserve requires a caller-owned transaction")
    digest = body_hash(body)
    existing = con.execute(
        "SELECT * FROM request_replays WHERE request_id=?", (request_id,)
    ).fetchone()
    if existing:
        if existing["method"] != method or existing["path"] != path or \
                existing["body_hash"] != digest:
            raise Conflict("request_id was already used for a different mutation")
        if existing["response_json"] is None:
            raise Conflict("request_id is already in progress")
        return json.loads(existing["response_json"])
    con.execute(
        "INSERT INTO request_replays(request_id,method,path,body_hash,created_at) "
        "VALUES(?,?,?,?,?)", (request_id, method, path, digest, db.now()))
    return None


def begin_atomic(con, request_id: str, method: str, path: str,
                 body) -> dict | None:
    """Reserve without committing so a one-shot secret and marker commit together.

    A matching unfinished row can only be residue from the older split-commit
    implementation: atomic callers never publish an unfinished reservation.
    Reusing it is therefore safe and makes crash recovery deterministic.
    """
    request_id = (request_id or "").strip()
    if not request_id:
        raise ValueError("request_id is required")
    digest = body_hash(body)
    con.execute("BEGIN IMMEDIATE")
    try:
        existing = con.execute(
            "SELECT * FROM request_replays WHERE request_id=?", (request_id,)
        ).fetchone()
        if existing:
            if existing["method"] != method or existing["path"] != path or \
                    existing["body_hash"] != digest:
                raise Conflict("request_id was already used for a different mutation")
            if existing["response_json"] is not None:
                value = json.loads(existing["response_json"])
                con.commit()
                return value
            con.execute(
                "DELETE FROM request_replays WHERE request_id=?", (request_id,))
        con.execute(
            "INSERT INTO request_replays(request_id,method,path,body_hash,created_at) "
            "VALUES(?,?,?,?,?)", (request_id, method, path, digest, db.now()))
        return None
    except BaseException:
        con.rollback()
        raise


def finish(con, request_id: str, response, *, commit: bool = True) -> None:
    encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
    def apply():
        changed = con.execute(
            "UPDATE request_replays SET response_json=?,finished_at=? "
            "WHERE request_id=? AND response_json IS NULL",
            (encoded, db.now(), request_id),
        )
        if changed.rowcount != 1:
            raise Conflict("request replay reservation is missing")
    if commit:
        with con:
            apply()
    else:
        if not con.in_transaction:
            raise RuntimeError("commit=False requires a caller-owned transaction")
        apply()
