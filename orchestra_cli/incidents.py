"""Durable, project-local ledger for recurring systemic incidents.

An incident is identified by the caller-provided ``fingerprint`` and its
``scope`` (for example, a profile plus its sandbox mode).  Every observation
is retained as evidence; recording the same pair updates one incident instead
of producing duplicate tickets.

State policy is deliberately small: callers may move an incident among
``open``, ``mitigated``, and ``resolved``.  A new observation automatically
reopens a mitigated or resolved incident, because the mitigation no longer
holds.  Resolution evidence is retained when it reopens so the previous
attempt remains auditable.

This module owns its two tables and creates them lazily.  Its functions accept
the normal project connection from :func:`orchestra_cli.db.connect`; callers
own transaction boundaries and must commit successful mutations.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from orchestra_cli import db


INCIDENT_STATES = frozenset({"open", "mitigated", "resolved"})

_SCHEMA_STATEMENTS = (
    """
CREATE TABLE IF NOT EXISTS systemic_incidents (
  id INTEGER PRIMARY KEY,
  fingerprint TEXT NOT NULL,
  scope TEXT NOT NULL,
  title TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  occurrence_count INTEGER NOT NULL CHECK(occurrence_count > 0),
  estimated_lost_seconds INTEGER NOT NULL CHECK(estimated_lost_seconds >= 0),
  state TEXT NOT NULL CHECK(state IN ('open', 'mitigated', 'resolved')),
  remediation TEXT,
  resolution_evidence TEXT,
  UNIQUE(fingerprint, scope)
);
""",
    """

CREATE TABLE IF NOT EXISTS systemic_incident_evidence (
  id INTEGER PRIMARY KEY,
  incident_id INTEGER NOT NULL REFERENCES systemic_incidents(id),
  evidence TEXT NOT NULL,
  run_id INTEGER REFERENCES runs(id),
  work_item TEXT,
  estimated_lost_seconds INTEGER NOT NULL CHECK(estimated_lost_seconds >= 0),
  seen_at TEXT NOT NULL
);
""",
    """

CREATE INDEX IF NOT EXISTS idx_systemic_incidents_state_last_seen
  ON systemic_incidents(state, last_seen_at DESC, id DESC);
""",
    """
CREATE INDEX IF NOT EXISTS idx_systemic_incident_evidence_incident
  ON systemic_incident_evidence(incident_id, id DESC);
""",
)


class IncidentValidationError(ValueError):
    """Raised when a ledger caller supplies an invalid incident value."""


class UnknownIncidentError(LookupError):
    """Raised when an incident id is not present in this project ledger."""


def ensure_schema(con: sqlite3.Connection) -> None:
    """Create the ledger tables if this project database does not have them."""
    # Do not use ``executescript`` here: sqlite3 commits any pending
    # transaction before running a script.  Ledger operations may be composed
    # with run updates, so schema setup must respect the caller's transaction.
    for statement in _SCHEMA_STATEMENTS:
        con.execute(statement)


def record_incident(
    con: sqlite3.Connection,
    *,
    fingerprint: str,
    scope: str,
    title: str,
    evidence: str,
    run_id: int | None = None,
    work_item: str | None = None,
    estimated_lost_seconds: int = 0,
    remediation: str | None = None,
) -> dict[str, Any]:
    """Record one observation and return its accumulated incident.

    ``fingerprint`` and ``scope`` are the stable identity.  Re-recording that
    identity increments its occurrence and impact totals, retains the new
    evidence as a separate row, and changes a mitigated/resolved incident back
    to ``open``.
    """
    fingerprint = _required_text("fingerprint", fingerprint, 512)
    scope = _required_text("scope", scope, 512)
    title = _required_text("title", title, 512)
    evidence = _required_text("evidence", evidence, 16_384)
    run_id = _optional_positive_int("run_id", run_id)
    work_item = _optional_text("work_item", work_item, 512)
    estimated_lost_seconds = _nonnegative_int(
        "estimated_lost_seconds", estimated_lost_seconds
    )
    remediation = _optional_text("remediation", remediation, 16_384)

    ensure_schema(con)
    timestamp = db.now()
    con.execute(
        "INSERT INTO systemic_incidents("
        "fingerprint, scope, title, first_seen_at, last_seen_at, "
        "occurrence_count, estimated_lost_seconds, state, remediation"
        ") VALUES(?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(fingerprint, scope) DO UPDATE SET "
        "title=excluded.title, "
        "last_seen_at=excluded.last_seen_at, "
        "occurrence_count=systemic_incidents.occurrence_count + 1, "
        "estimated_lost_seconds="
        "systemic_incidents.estimated_lost_seconds + excluded.estimated_lost_seconds, "
        "state='open', "
        "remediation=COALESCE(excluded.remediation, systemic_incidents.remediation)",
        (
            fingerprint,
            scope,
            title,
            timestamp,
            timestamp,
            1,
            estimated_lost_seconds,
            "open",
            remediation,
        ),
    )
    incident = con.execute(
        "SELECT id FROM systemic_incidents WHERE fingerprint=? AND scope=?",
        (fingerprint, scope),
    ).fetchone()
    assert incident is not None  # The preceding INSERT or UPSERT created it.
    incident_id = int(incident["id"])
    con.execute(
        "INSERT INTO systemic_incident_evidence("
        "incident_id, evidence, run_id, work_item, estimated_lost_seconds, seen_at"
        ") VALUES(?,?,?,?,?,?)",
        (incident_id, evidence, run_id, work_item, estimated_lost_seconds, timestamp),
    )
    return get_incident(con, incident_id)


def set_incident_state(
    con: sqlite3.Connection,
    incident_id: int,
    state: str,
    *,
    remediation: str | None = None,
    resolution_evidence: str | None = None,
) -> dict[str, Any]:
    """Set an incident state, requiring proof when moving into ``resolved``."""
    incident_id = _positive_int("incident_id", incident_id)
    state = _state(state)
    remediation = _optional_text("remediation", remediation, 16_384)
    resolution_evidence = _optional_text(
        "resolution_evidence", resolution_evidence, 16_384
    )

    ensure_schema(con)
    current = _incident_row(con, incident_id)
    if state == "resolved" and current["state"] != "resolved" and resolution_evidence is None:
        raise IncidentValidationError(
            "resolution_evidence is required when resolving an incident"
        )
    con.execute(
        "UPDATE systemic_incidents SET state=?, "
        "remediation=COALESCE(?, remediation), "
        "resolution_evidence=COALESCE(?, resolution_evidence) WHERE id=?",
        (state, remediation, resolution_evidence, incident_id),
    )
    return get_incident(con, incident_id)


def get_incident(con: sqlite3.Connection, incident_id: int) -> dict[str, Any]:
    """Return an incident and its newest-first evidence observations."""
    incident_id = _positive_int("incident_id", incident_id)
    ensure_schema(con)
    incident = dict(_incident_row(con, incident_id))
    evidence = con.execute(
        "SELECT id, evidence, run_id, work_item, estimated_lost_seconds, seen_at "
        "FROM systemic_incident_evidence WHERE incident_id=? ORDER BY id DESC",
        (incident_id,),
    ).fetchall()
    incident["evidence"] = [dict(row) for row in evidence]
    return incident


def list_incidents(
    con: sqlite3.Connection,
    *,
    state: str | None = None,
    scope: str | None = None,
    fingerprint: str | None = None,
) -> list[dict[str, Any]]:
    """List incidents, optionally narrowed by one stable ledger field."""
    if state is not None:
        state = _state(state)
    scope = _optional_text("scope", scope, 512)
    fingerprint = _optional_text("fingerprint", fingerprint, 512)

    ensure_schema(con)
    clauses: list[str] = []
    values: list[str] = []
    if state is not None:
        clauses.append("state=?")
        values.append(state)
    if scope is not None:
        clauses.append("scope=?")
        values.append(scope)
    if fingerprint is not None:
        clauses.append("fingerprint=?")
        values.append(fingerprint)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = con.execute(
        "SELECT * FROM systemic_incidents" + where + " ORDER BY last_seen_at DESC, id DESC",
        values,
    ).fetchall()
    return [dict(row) for row in rows]


def _incident_row(con: sqlite3.Connection, incident_id: int) -> sqlite3.Row:
    row = con.execute(
        "SELECT * FROM systemic_incidents WHERE id=?", (incident_id,)
    ).fetchone()
    if row is None:
        raise UnknownIncidentError(f"unknown systemic incident {incident_id}")
    return row


def _required_text(name: str, value: object, maximum: int) -> str:
    if not isinstance(value, str):
        raise IncidentValidationError(f"{name} must be text")
    value = value.strip()
    if not value:
        raise IncidentValidationError(f"{name} must not be blank")
    if len(value) > maximum:
        raise IncidentValidationError(f"{name} must be at most {maximum} characters")
    return value


def _optional_text(name: str, value: str | None, maximum: int) -> str | None:
    if value is None:
        return None
    return _required_text(name, value, maximum)


def _positive_int(name: str, value: object) -> int:
    if type(value) is not int or value <= 0:
        raise IncidentValidationError(f"{name} must be a positive integer")
    return value


def _optional_positive_int(name: str, value: int | None) -> int | None:
    if value is None:
        return None
    return _positive_int(name, value)


def _nonnegative_int(name: str, value: object) -> int:
    if type(value) is not int or value < 0:
        raise IncidentValidationError(f"{name} must be a non-negative integer")
    return value


def _state(value: object) -> str:
    if not isinstance(value, str) or value not in INCIDENT_STATES:
        choices = ", ".join(sorted(INCIDENT_STATES))
        raise IncidentValidationError(f"state must be one of: {choices}")
    return value
