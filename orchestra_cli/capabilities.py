"""Durable, per-launch-lane environment capability observations.

Capabilities describe the environment a worker actually reached, rather than
what a backend is assumed to support.  They are deliberately scoped to a
single project database and a precise launch lane: host, backend, profile, and
sandbox mode.  Routing can consume this module later without having to parse
old run transcripts again.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterable, Literal

from orchestra_cli import db


CapabilityState = Literal["supported", "unsupported", "unknown"]
CAPABILITY_STATES = frozenset({"supported", "unsupported", "unknown"})
# Capability evidence gets stale as runners, operating systems, and launch
# policy change.  Callers can deliberately supply a different TTL or no expiry
# for a fact that is intended to be permanent.
DEFAULT_TTL = timedelta(days=7)
_DEFAULT_TTL = object()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS capability_observations (
  host_identity TEXT NOT NULL,
  backend TEXT NOT NULL,
  profile TEXT NOT NULL,
  sandbox_mode TEXT NOT NULL,
  capability TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('supported', 'unsupported', 'unknown')),
  evidence TEXT NOT NULL,
  probe TEXT,
  observed_at TEXT NOT NULL,
  expires_at TEXT,
  PRIMARY KEY(host_identity, backend, profile, sandbox_mode, capability)
) WITHOUT ROWID;
"""


@dataclass(frozen=True, slots=True)
class CapabilityObservation:
    """The newest recorded observation for one capability in one launch lane."""

    host_identity: str
    backend: str
    profile: str
    sandbox_mode: str
    capability: str
    state: CapabilityState
    evidence: str
    probe: str | None
    observed_at: datetime
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class CapabilityCheck:
    """Whether all requested capabilities have fresh positive evidence.

    ``unsupported`` is affirmative negative evidence.  The other three sets
    tell an operator what evidence is absent: an explicit inconclusive probe,
    no observation, or evidence that has aged out.
    """

    supported: frozenset[str]
    unsupported: frozenset[str]
    unknown: frozenset[str]
    missing: frozenset[str]
    expired: frozenset[str]

    @property
    def satisfied(self) -> bool:
        return not (self.unsupported or self.unknown or self.missing or self.expired)


def record_observation(
    root: Path,
    *,
    host_identity: str,
    backend: str,
    profile: str,
    sandbox_mode: str,
    capability: str,
    state: CapabilityState,
    evidence: str,
    probe: str | None = None,
    observed_at: datetime | None = None,
    ttl: timedelta | None | object = _DEFAULT_TTL,
    expires_at: datetime | None = None,
) -> CapabilityObservation:
    """Store an observation and return the newest one for its exact key.

    An observation with an older ``observed_at`` cannot replace newer evidence
    that may have arrived from another runner.  Equal timestamps are allowed
    to replace the stored row so an operator can correct its metadata.
    """
    con = _connect(root)
    try:
        observation = record_observation_in_connection(
            con,
            host_identity=host_identity,
            backend=backend,
            profile=profile,
            sandbox_mode=sandbox_mode,
            capability=capability,
            state=state,
            evidence=evidence,
            probe=probe,
            observed_at=observed_at,
            ttl=ttl,
            expires_at=expires_at,
        )
        con.commit()
        return observation
    finally:
        con.close()


def record_observation_in_connection(
    con: sqlite3.Connection,
    *,
    host_identity: str,
    backend: str,
    profile: str,
    sandbox_mode: str,
    capability: str,
    state: CapabilityState,
    evidence: str,
    probe: str | None = None,
    observed_at: datetime | None = None,
    ttl: timedelta | None | object = _DEFAULT_TTL,
    expires_at: datetime | None = None,
) -> CapabilityObservation:
    """Record an observation in an existing transaction.

    A blocked-environment handoff updates its run, capability evidence, and
    systemic incident together.  This variant lets that boundary stay atomic;
    callers own the connection and commit/rollback themselves.
    """
    ensure_schema(con)
    key = _validate_key(
        host_identity=host_identity,
        backend=backend,
        profile=profile,
        sandbox_mode=sandbox_mode,
        capability=capability,
    )
    if not isinstance(state, str) or state not in CAPABILITY_STATES:
        raise ValueError(f"invalid capability state {state!r}")
    evidence = _require_text("evidence", evidence)
    if probe is not None:
        probe = _require_text("probe", probe)
    # An explicit absolute expiry should work without callers having to know
    # that ordinary observations default to a seven-day TTL.
    if ttl is _DEFAULT_TTL:
        ttl = None if expires_at is not None else DEFAULT_TTL
    if ttl is not None and expires_at is not None:
        raise ValueError("provide either ttl or expires_at, not both")

    observed = _normalise_time(observed_at or datetime.now(UTC), "observed_at")
    if ttl is not None:
        if not isinstance(ttl, timedelta) or ttl <= timedelta(0):
            raise ValueError("ttl must be a positive timedelta or None")
        expiry = observed + ttl
    elif expires_at is not None:
        expiry = _normalise_time(expires_at, "expires_at")
        if expiry <= observed:
            raise ValueError("expires_at must be after observed_at")
    else:
        expiry = None

    con.execute(
        """
        INSERT INTO capability_observations(
          host_identity, backend, profile, sandbox_mode, capability,
          state, evidence, probe, observed_at, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(host_identity, backend, profile, sandbox_mode, capability)
        DO UPDATE SET
          state=excluded.state,
          evidence=excluded.evidence,
          probe=excluded.probe,
          observed_at=excluded.observed_at,
          expires_at=excluded.expires_at
        WHERE excluded.observed_at >= capability_observations.observed_at
        """,
        (*key, state, evidence, probe, _format_time(observed), _format_time(expiry)),
    )
    row = _fetch_observation(con, key)
    assert row is not None  # The INSERT above guarantees the row exists.
    return _row_to_observation(row)


def get_effective_observation(
    root: Path,
    *,
    host_identity: str,
    backend: str,
    profile: str,
    sandbox_mode: str,
    capability: str,
    at: datetime | None = None,
) -> CapabilityObservation | None:
    """Return fresh evidence for an exact launch lane, or ``None`` if stale/missing."""
    key = _validate_key(
        host_identity=host_identity,
        backend=backend,
        profile=profile,
        sandbox_mode=sandbox_mode,
        capability=capability,
    )
    instant = _normalise_time(at or datetime.now(UTC), "at")
    con = _connect(root)
    try:
        row = _fetch_observation(con, key)
        if row is None:
            return None
        observation = _row_to_observation(row)
        return observation if _is_effective(observation, instant) else None
    finally:
        con.close()


def list_observations(
    root: Path,
    *,
    host_identity: str | None = None,
    backend: str | None = None,
    profile: str | None = None,
    sandbox_mode: str | None = None,
    capability: str | None = None,
    include_expired: bool = True,
    at: datetime | None = None,
) -> list[CapabilityObservation]:
    """List observations newest-first, optionally restricted to exact fields.

    Filters are intentionally exact and limited to the launch-lane key.  This
    gives an operator useful visibility without exposing an arbitrary SQL query
    surface or conflating similar profiles and sandbox modes.
    """
    if not isinstance(include_expired, bool):
        raise ValueError("include_expired must be a bool")
    instant = _normalise_time(at, "at") if at is not None else datetime.now(UTC)
    filters = {
        "host_identity": host_identity,
        "backend": backend,
        "profile": profile,
        "sandbox_mode": sandbox_mode,
        "capability": capability,
    }
    clauses: list[str] = []
    values: list[str] = []
    for column, value in filters.items():
        if value is not None:
            clauses.append(f"{column}=?")
            values.append(_require_text(column, value))
    if not include_expired:
        clauses.append("(expires_at IS NULL OR expires_at > ?)")
        values.append(instant.isoformat())

    query = """
        SELECT host_identity, backend, profile, sandbox_mode, capability,
               state, evidence, probe, observed_at, expires_at
        FROM capability_observations
    """
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    # All stored timestamps are normalized to UTC.  Key columns make ties
    # deterministic for an operator display and tests without changing what
    # "newest" means.
    query += " ORDER BY observed_at DESC, host_identity, backend, profile, sandbox_mode, capability"

    con = _connect(root)
    try:
        return [_row_to_observation(row) for row in con.execute(query, values).fetchall()]
    finally:
        con.close()


def check_requirements(
    root: Path,
    *,
    host_identity: str,
    backend: str,
    profile: str,
    sandbox_mode: str,
    capabilities: Iterable[str],
    at: datetime | None = None,
) -> CapabilityCheck:
    """Classify required capabilities for one exact launch lane.

    Only current ``supported`` observations satisfy a requirement.  In
    particular, an absent or expired observation is not treated as evidence of
    support, and an explicit ``unsupported`` observation stays distinguishable
    from that lack of evidence.
    """
    lane = _validate_lane(
        host_identity=host_identity,
        backend=backend,
        profile=profile,
        sandbox_mode=sandbox_mode,
    )
    required = {_require_text("capability", capability) for capability in capabilities}
    instant = _normalise_time(at or datetime.now(UTC), "at")
    buckets: dict[str, set[str]] = {
        "supported": set(),
        "unsupported": set(),
        "unknown": set(),
        "missing": set(),
        "expired": set(),
    }
    con = _connect(root)
    try:
        for capability in required:
            row = _fetch_observation(con, (*lane, capability))
            if row is None:
                buckets["missing"].add(capability)
                continue
            observation = _row_to_observation(row)
            if not _is_effective(observation, instant):
                buckets["expired"].add(capability)
            else:
                buckets[observation.state].add(capability)
    finally:
        con.close()
    return CapabilityCheck(**{name: frozenset(values) for name, values in buckets.items()})


def _connect(root: Path) -> sqlite3.Connection:
    """Open the project database and lazily install this independent schema."""
    con = db.connect(root)
    ensure_schema(con)
    return con


def ensure_schema(con: sqlite3.Connection) -> None:
    """Install capability tables without committing a caller's transaction."""
    con.execute(_SCHEMA)


def _validate_key(
    *, host_identity: str, backend: str, profile: str, sandbox_mode: str, capability: str
) -> tuple[str, str, str, str, str]:
    return (
        _require_text("host_identity", host_identity),
        _require_text("backend", backend),
        _require_text("profile", profile),
        _require_text("sandbox_mode", sandbox_mode),
        _require_text("capability", capability),
    )


def _validate_lane(
    *, host_identity: str, backend: str, profile: str, sandbox_mode: str
) -> tuple[str, str, str, str]:
    return (
        _require_text("host_identity", host_identity),
        _require_text("backend", backend),
        _require_text("profile", profile),
        _require_text("sandbox_mode", sandbox_mode),
    )


def _require_text(name: str, value: object) -> str:
    if not isinstance(value, str) or not (text := value.strip()):
        raise ValueError(f"{name} must be a non-empty string")
    return text


def _normalise_time(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _format_time(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _fetch_observation(
    con: sqlite3.Connection, key: tuple[str, str, str, str, str]
) -> sqlite3.Row | None:
    return con.execute(
        """
        SELECT host_identity, backend, profile, sandbox_mode, capability,
               state, evidence, probe, observed_at, expires_at
        FROM capability_observations
        WHERE host_identity=? AND backend=? AND profile=? AND sandbox_mode=?
          AND capability=?
        """,
        key,
    ).fetchone()


def _row_to_observation(row: sqlite3.Row) -> CapabilityObservation:
    return CapabilityObservation(
        host_identity=row["host_identity"],
        backend=row["backend"],
        profile=row["profile"],
        sandbox_mode=row["sandbox_mode"],
        capability=row["capability"],
        state=row["state"],
        evidence=row["evidence"],
        probe=row["probe"],
        observed_at=_parse_time(row["observed_at"]),
        expires_at=_parse_time(row["expires_at"]) if row["expires_at"] else None,
    )


def _parse_time(value: str) -> datetime:
    # Only this module writes these ISO-8601 values.  A corrupt manual DB edit
    # should fail visibly rather than being mistaken for a fresh observation.
    return _normalise_time(datetime.fromisoformat(value), "stored timestamp")


def _is_effective(observation: CapabilityObservation, instant: datetime) -> bool:
    return observation.expires_at is None or observation.expires_at > instant
