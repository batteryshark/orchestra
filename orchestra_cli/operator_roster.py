"""Versioned roster policy, shared capacity, routing, and recovery councils."""
from __future__ import annotations

import hashlib
import json
import math
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from orchestra_cli import availability, config, operator_runtime, operator_store
from orchestra_cli.usage import infer_provider

ROSTER_SCHEMA_TAG = "orchestra.roster-policy/v1"
MAX_POLICY_BYTES = 256 * 1024
TIERS = {"workhorse": 1, "generalist": 2, "heavy": 3}
ACTUATION_MODES = {
    "diagnose_only",
    "review_only",
    "bounded_patch",
    "general_implementation",
}
HEALTH_STATES = {
    "available",
    "unknown",
    "degraded",
    "quarantined",
    "disabled",
    "unavailable",
}
BURN_PERCENT = {"small": 2.0, "normal": 5.0, "heavy": 10.0, "unknown": 15.0}

ROSTER_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS roster_policy_versions (
  version INTEGER PRIMARY KEY,
  schema TEXT NOT NULL,
  content_json TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  source TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(version, content_sha256)
);

CREATE TABLE IF NOT EXISTS roster_policy_approvals (
  version INTEGER PRIMARY KEY REFERENCES roster_policy_versions(version),
  content_sha256 TEXT NOT NULL,
  approved_by TEXT NOT NULL,
  approved_at TEXT NOT NULL,
  FOREIGN KEY(version, content_sha256)
    REFERENCES roster_policy_versions(version, content_sha256)
);

CREATE TABLE IF NOT EXISTS roster_profiles (
  policy_version INTEGER NOT NULL REFERENCES roster_policy_versions(version),
  name TEXT NOT NULL,
  backend TEXT NOT NULL,
  model TEXT,
  role TEXT NOT NULL,
  tier TEXT NOT NULL,
  capabilities_json TEXT NOT NULL,
  contraindications_json TEXT NOT NULL,
  access_json TEXT NOT NULL,
  actuation_modes_json TEXT NOT NULL,
  enabled INTEGER NOT NULL,
  uncertainty TEXT,
  PRIMARY KEY(policy_version, name)
);

CREATE TABLE IF NOT EXISTS capacity_pools (
  policy_version INTEGER NOT NULL REFERENCES roster_policy_versions(version),
  id TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  max_concurrency INTEGER NOT NULL,
  reserve_percent REAL NOT NULL,
  PRIMARY KEY(policy_version, id)
);

CREATE TABLE IF NOT EXISTS profile_capacity_pools (
  policy_version INTEGER NOT NULL,
  profile_name TEXT NOT NULL,
  pool_id TEXT NOT NULL,
  PRIMARY KEY(policy_version, profile_name, pool_id),
  FOREIGN KEY(policy_version, profile_name)
    REFERENCES roster_profiles(policy_version, name),
  FOREIGN KEY(policy_version, pool_id)
    REFERENCES capacity_pools(policy_version, id)
);

CREATE TABLE IF NOT EXISTS profile_health (
  profile_name TEXT PRIMARY KEY,
  state TEXT NOT NULL,
  consecutive_infra_failures INTEGER NOT NULL DEFAULT 0,
  reason TEXT,
  cooldown_until_epoch REAL,
  probe_condition TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profile_health_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_name TEXT NOT NULL,
  signal TEXT NOT NULL,
  context_json TEXT NOT NULL,
  resulting_state TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS capacity_observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pool_id TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  status TEXT NOT NULL,
  headroom_percent REAL,
  windows_json TEXT NOT NULL,
  balance_json TEXT,
  certainty TEXT NOT NULL,
  observed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_capacity_observations_latest
  ON capacity_observations(pool_id, id DESC);

CREATE TABLE IF NOT EXISTS capacity_reservations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  reservation_group TEXT NOT NULL,
  pool_id TEXT NOT NULL,
  operation_id TEXT NOT NULL REFERENCES operations(id),
  work_item_id TEXT NOT NULL REFERENCES operator_work_items(id),
  profile_name TEXT NOT NULL,
  burn_band TEXT NOT NULL,
  estimated_percent REAL NOT NULL,
  state TEXT NOT NULL,
  expires_at_epoch REAL NOT NULL,
  project_run_id INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(reservation_group, pool_id)
);

CREATE INDEX IF NOT EXISTS idx_capacity_reservations_active
  ON capacity_reservations(pool_id, state, expires_at_epoch);

CREATE TABLE IF NOT EXISTS routing_decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  operation_id TEXT NOT NULL REFERENCES operations(id),
  work_item_id TEXT NOT NULL REFERENCES operator_work_items(id),
  requirements_json TEXT NOT NULL,
  considered_json TEXT NOT NULL,
  capacity_json TEXT NOT NULL,
  selected_profile TEXT,
  fallback_json TEXT NOT NULL,
  explanation TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recovery_councils (
  id TEXT PRIMARY KEY,
  operation_id TEXT NOT NULL REFERENCES operations(id),
  work_item_id TEXT NOT NULL REFERENCES operator_work_items(id),
  evidence_sha256 TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  failure_fingerprint TEXT NOT NULL,
  minimum_members INTEGER NOT NULL,
  quorum INTEGER NOT NULL,
  state TEXT NOT NULL,
  synthesis_json TEXT,
  created_at TEXT NOT NULL,
  completed_at TEXT,
  UNIQUE(operation_id, work_item_id, failure_fingerprint, evidence_sha256)
);

CREATE TABLE IF NOT EXISTS recovery_council_members (
  council_id TEXT NOT NULL REFERENCES recovery_councils(id),
  profile_name TEXT NOT NULL,
  model_family TEXT NOT NULL,
  project_run_id INTEGER,
  state TEXT NOT NULL,
  opinion_json TEXT,
  action_key TEXT,
  submitted_at TEXT,
  PRIMARY KEY(council_id, profile_name)
);

CREATE TRIGGER IF NOT EXISTS immutable_roster_policy_update
BEFORE UPDATE ON roster_policy_versions
BEGIN SELECT RAISE(ABORT, 'roster policy versions are immutable'); END;

CREATE TRIGGER IF NOT EXISTS immutable_roster_policy_delete
BEFORE DELETE ON roster_policy_versions
BEGIN SELECT RAISE(ABORT, 'roster policy versions are immutable'); END;

CREATE TRIGGER IF NOT EXISTS immutable_roster_approval_update
BEFORE UPDATE ON roster_policy_approvals
BEGIN SELECT RAISE(ABORT, 'roster policy approvals are immutable'); END;

CREATE TRIGGER IF NOT EXISTS immutable_roster_approval_delete
BEFORE DELETE ON roster_policy_approvals
BEGIN SELECT RAISE(ABORT, 'roster policy approvals are immutable'); END;
"""


class RosterError(Exception):
    pass


@dataclass(frozen=True)
class RosterPolicy:
    data: dict[str, Any]
    canonical_json: str
    sha256: str


@dataclass(frozen=True)
class Route:
    profile: str | None
    fallbacks: tuple[str, ...]
    considered: tuple[dict[str, Any], ...]
    explanation: str
    decision_id: int


@dataclass(frozen=True)
class Reservation:
    group: str
    profile: str
    pools: tuple[str, ...]
    expires_at_epoch: float


def connect(path: Path | None = None) -> sqlite3.Connection:
    con = operator_runtime.connect(path)
    con.executescript(ROSTER_SCHEMA)
    return con


def bootstrap_policy(cfg: Mapping[str, Any]) -> RosterPolicy:
    profiles: list[dict[str, Any]] = []
    pools: dict[str, dict[str, Any]] = {}
    codex_default_model, _ = config.codex_defaults()
    for name, raw in sorted((cfg.get("agents") or {}).items()):
        if not isinstance(raw, Mapping):
            continue
        backend = str(raw.get("backend") or "opencode")
        model = raw.get("model")
        if not isinstance(model, str):
            model = codex_default_model if backend == "codex" else None
        role = str(raw.get("role") or "")
        enabled = backend in availability.BACKENDS
        tier = _infer_tier(name, role)
        capabilities = _infer_capabilities(tier, role)
        modes = _infer_modes(name, tier, role)
        contraindications = _infer_contraindications(role)
        provider_id = infer_provider(backend, model)
        if provider_id is None:
            provider_id = backend
        if "spark" in name.casefold() or (model and "spark" in model.casefold()):
            provider_id = "codex-spark"
        pool_id = f"{provider_id}-capacity"
        pool = pools.setdefault(
            pool_id,
            {
                "id": pool_id,
                "provider_id": provider_id,
                "kind": "shared",
                "max_concurrency": 4,
                "reserve_percent": 0,
            },
        )
        if tier == "heavy":
            pool["reserve_percent"] = 25
        profiles.append({
            "name": name,
            "backend": backend,
            "model": model,
            "role": role,
            "tier": tier,
            "capabilities": capabilities,
            "contraindications": contraindications,
            "access": ["project", "git", "tests"],
            "actuation_modes": modes,
            "enabled": enabled,
            "pools": [pool_id],
            "uncertainty": "inferred from launch profile and role text; owner review required",
        })
    return validate_policy({
        "schema": ROSTER_SCHEMA_TAG,
        "profiles": profiles,
        "pools": sorted(pools.values(), key=lambda row: row["id"]),
    })


def parse_policy(text: str, *, source: str = "<memory>") -> RosterPolicy:
    if len(text.encode("utf-8")) > MAX_POLICY_BYTES:
        raise RosterError(f"{source}: roster policy exceeds {MAX_POLICY_BYTES} bytes")
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise RosterError(f"{source}: invalid roster policy JSON: {exc}") from exc
    return validate_policy(data, source=source)


def validate_policy(data: Any, *, source: str = "<memory>") -> RosterPolicy:
    errors: list[str] = []
    if not isinstance(data, dict):
        raise RosterError(f"{source}: roster policy must be an object")
    if set(data) != {"schema", "profiles", "pools"}:
        errors.append("top-level fields must be exactly schema, profiles, and pools")
    if data.get("schema") != ROSTER_SCHEMA_TAG:
        errors.append(f"schema must equal {ROSTER_SCHEMA_TAG!r}")
    profiles = data.get("profiles")
    pools = data.get("pools")
    if not isinstance(profiles, list) or not profiles:
        errors.append("profiles must be a non-empty array")
        profiles = []
    if not isinstance(pools, list) or not pools:
        errors.append("pools must be a non-empty array")
        pools = []
    pool_names: set[str] = set()
    for index, pool in enumerate(pools[:256]):
        path = f"pools[{index}]"
        required = {
            "id", "provider_id", "kind", "max_concurrency", "reserve_percent"
        }
        if not isinstance(pool, dict) or set(pool) != required:
            errors.append(f"{path} has an unexpected shape")
            continue
        _bounded_string(pool.get("id"), f"{path}.id", errors)
        _bounded_string(pool.get("provider_id"), f"{path}.provider_id", errors)
        if pool.get("kind") not in {"shared", "quota", "concurrency", "currency"}:
            errors.append(f"{path}.kind is unsupported")
        concurrency = pool.get("max_concurrency")
        if (
            not isinstance(concurrency, int)
            or isinstance(concurrency, bool)
            or not 1 <= concurrency <= 1000
        ):
            errors.append(f"{path}.max_concurrency must be between 1 and 1000")
        reserve = pool.get("reserve_percent")
        if (
            not isinstance(reserve, (int, float))
            or isinstance(reserve, bool)
            or not 0 <= reserve <= 100
        ):
            errors.append(f"{path}.reserve_percent must be between 0 and 100")
        if isinstance(pool.get("id"), str):
            if pool["id"] in pool_names:
                errors.append(f"{path}.id is duplicated")
            pool_names.add(pool["id"])
    profile_names: set[str] = set()
    expected_profile_keys = {
        "name", "backend", "model", "role", "tier", "capabilities",
        "contraindications", "access", "actuation_modes", "enabled", "pools",
        "uncertainty",
    }
    for index, profile in enumerate(profiles[:256]):
        path = f"profiles[{index}]"
        if not isinstance(profile, dict) or set(profile) != expected_profile_keys:
            errors.append(f"{path} has an unexpected shape")
            continue
        name = profile.get("name")
        _bounded_string(name, f"{path}.name", errors)
        _bounded_string(profile.get("backend"), f"{path}.backend", errors)
        if profile.get("model") is not None:
            _bounded_string(profile.get("model"), f"{path}.model", errors)
        _bounded_string(profile.get("role"), f"{path}.role", errors, maximum=4096)
        if profile.get("tier") not in TIERS:
            errors.append(f"{path}.tier must be workhorse, generalist, or heavy")
        for key in (
            "capabilities", "contraindications", "access", "actuation_modes", "pools"
        ):
            values = profile.get(key)
            if (
                not isinstance(values, list)
                or len(values) > 128
                or any(not isinstance(value, str) or not value for value in values)
                or len(values) != len(set(values))
            ):
                errors.append(f"{path}.{key} must be a unique bounded string array")
        modes = set(profile.get("actuation_modes") or [])
        if modes - ACTUATION_MODES:
            errors.append(f"{path}.actuation_modes contains an unsupported mode")
        unknown_pools = set(profile.get("pools") or []) - pool_names
        if unknown_pools:
            errors.append(f"{path}.pools references unknown pools")
        if not isinstance(profile.get("enabled"), bool):
            errors.append(f"{path}.enabled must be boolean")
        if profile.get("uncertainty") is not None:
            _bounded_string(
                profile.get("uncertainty"),
                f"{path}.uncertainty",
                errors,
                maximum=4096,
            )
        if isinstance(name, str):
            if name in profile_names:
                errors.append(f"{path}.name is duplicated")
            profile_names.add(name)
    if errors:
        raise RosterError(
            f"{source}: invalid roster policy:\n"
            + "\n".join(f"  - {error}" for error in errors[:30])
        )
    canonical = json.dumps(
        data,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(canonical.encode("utf-8")) > MAX_POLICY_BYTES:
        raise RosterError(f"{source}: canonical roster policy is too large")
    return RosterPolicy(
        data=json.loads(canonical),
        canonical_json=canonical,
        sha256=hashlib.sha256(canonical.encode()).hexdigest(),
    )


def save_policy(
    policy: RosterPolicy,
    *,
    source: str,
    path: Path | None = None,
) -> tuple[int, bool]:
    verified = parse_policy(policy.canonical_json, source="roster storage boundary")
    if verified.sha256 != policy.sha256:
        raise RosterError("roster policy hash does not match canonical bytes")
    con = connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        latest = con.execute(
            "SELECT version, content_sha256 FROM roster_policy_versions "
            "ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if latest is not None and latest["content_sha256"] == policy.sha256:
            con.commit()
            return int(latest["version"]), False
        version = 1 if latest is None else int(latest["version"]) + 1
        timestamp = operator_store.now()
        con.execute(
            "INSERT INTO roster_policy_versions("
            "version, schema, content_json, content_sha256, source, created_at"
            ") VALUES(?,?,?,?,?,?)",
            (
                version,
                ROSTER_SCHEMA_TAG,
                policy.canonical_json,
                policy.sha256,
                source[:160],
                timestamp,
            ),
        )
        for profile in policy.data["profiles"]:
            con.execute(
                "INSERT INTO roster_profiles("
                "policy_version, name, backend, model, role, tier, "
                "capabilities_json, contraindications_json, access_json, "
                "actuation_modes_json, enabled, uncertainty"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    version,
                    profile["name"],
                    profile["backend"],
                    profile["model"],
                    profile["role"],
                    profile["tier"],
                    _json(profile["capabilities"]),
                    _json(profile["contraindications"]),
                    _json(profile["access"]),
                    _json(profile["actuation_modes"]),
                    int(profile["enabled"]),
                    profile["uncertainty"],
                ),
            )
        for pool in policy.data["pools"]:
            con.execute(
                "INSERT INTO capacity_pools("
                "policy_version, id, provider_id, kind, max_concurrency, reserve_percent"
                ") VALUES(?,?,?,?,?,?)",
                (
                    version,
                    pool["id"],
                    pool["provider_id"],
                    pool["kind"],
                    pool["max_concurrency"],
                    pool["reserve_percent"],
                ),
            )
        for profile in policy.data["profiles"]:
            for pool_id in profile["pools"]:
                con.execute(
                    "INSERT INTO profile_capacity_pools("
                    "policy_version, profile_name, pool_id"
                    ") VALUES(?,?,?)",
                    (version, profile["name"], pool_id),
                )
        con.commit()
        return version, True
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def approve_policy(
    *,
    version: int,
    sha256: str,
    approved_by: str,
    path: Path | None = None,
) -> bool:
    con = connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        latest = con.execute(
            "SELECT version, content_sha256 FROM roster_policy_versions "
            "ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if latest is None:
            raise RosterError("no roster policy draft exists")
        if int(latest["version"]) != version:
            raise RosterError(f"review and approve latest roster policy v{latest['version']}")
        if latest["content_sha256"] != sha256:
            raise RosterError("roster approval hash does not match")
        existing = con.execute(
            "SELECT 1 FROM roster_policy_approvals WHERE version=?",
            (version,),
        ).fetchone()
        if existing:
            con.commit()
            return False
        con.execute(
            "INSERT INTO roster_policy_approvals("
            "version, content_sha256, approved_by, approved_at"
            ") VALUES(?,?,?,?)",
            (version, sha256, approved_by[:160], operator_store.now()),
        )
        con.commit()
        return True
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def latest_policy(
    *,
    require_approved: bool = True,
    path: Path | None = None,
) -> tuple[int, RosterPolicy]:
    con = connect(path)
    try:
        join = (
            "JOIN roster_policy_approvals a ON a.version=v.version "
            "AND a.content_sha256=v.content_sha256"
            if require_approved
            else ""
        )
        row = con.execute(
            "SELECT v.version, v.content_json, v.content_sha256 "
            f"FROM roster_policy_versions v {join} "
            "ORDER BY v.version DESC LIMIT 1"
        ).fetchone()
        if row is None:
            suffix = "approved " if require_approved else ""
            raise RosterError(f"no {suffix}roster policy exists")
        policy = parse_policy(row["content_json"], source=f"roster policy v{row['version']}")
        if policy.sha256 != row["content_sha256"]:
            raise RosterError(f"roster policy v{row['version']} hash mismatch")
        return int(row["version"]), policy
    finally:
        con.close()


def record_capacity_snapshot(
    policy: RosterPolicy,
    snapshot: Mapping[str, Any],
    *,
    path: Path | None = None,
) -> dict[str, dict[str, Any]]:
    providers = {
        row.get("id"): row
        for row in snapshot.get("providers") or []
        if isinstance(row, Mapping) and isinstance(row.get("id"), str)
    }
    recorded: dict[str, dict[str, Any]] = {}
    con = connect(path)
    try:
        timestamp = operator_store.now()
        for pool in policy.data["pools"]:
            provider = providers.get(pool["provider_id"])
            status = str(provider.get("status") or "unknown") if provider else "unknown"
            headroom = provider.get("headroom_percent") if provider else None
            if not isinstance(headroom, (int, float)) or isinstance(headroom, bool):
                headroom = None
            certainty = (
                "observed"
                if provider and status == "ok" and headroom is not None
                else ("stale" if status == "stale" else "unknown")
            )
            windows = provider.get("windows") if provider else []
            balance = provider.get("account_balance") if provider else None
            con.execute(
                "INSERT INTO capacity_observations("
                "pool_id, provider_id, status, headroom_percent, windows_json, "
                "balance_json, certainty, observed_at"
                ") VALUES(?,?,?,?,?,?,?,?)",
                (
                    pool["id"],
                    pool["provider_id"],
                    status,
                    headroom,
                    _json(windows or []),
                    _json(balance) if balance else None,
                    certainty,
                    timestamp,
                ),
            )
            recorded[pool["id"]] = {
                "pool_id": pool["id"],
                "provider_id": pool["provider_id"],
                "status": status,
                "headroom_percent": headroom,
                "certainty": certainty,
            }
        con.commit()
        return recorded
    finally:
        con.close()


def route(
    operation: Mapping[str, Any],
    work: Mapping[str, Any],
    contract: Mapping[str, Any],
    policy: RosterPolicy,
    *,
    availability_report: Mapping[str, Any],
    capacity: Mapping[str, Mapping[str, Any]],
    active_by_profile: Mapping[str, int] | None = None,
    reviewer_profile: str | None = None,
    path: Path | None = None,
) -> Route:
    active_by_profile = active_by_profile or {}
    discovered = {
        row.get("name"): row
        for row in availability_report.get("roster") or []
        if isinstance(row, Mapping)
    }
    health = health_states(path=path)
    forbidden = set(contract["routing"]["forbidden_profiles"])
    preferred = set(contract["routing"]["preferred_profiles"])
    required_tier = TIERS[work["minimum_tier"]]
    active_by_pool: dict[str, int] = {}
    for profile in policy.data["profiles"]:
        count = int(active_by_profile.get(profile["name"], 0))
        for pool_id in profile["pools"]:
            active_by_pool[pool_id] = active_by_pool.get(pool_id, 0) + count
    candidates: list[dict[str, Any]] = []
    for profile in policy.data["profiles"]:
        reasons: list[str] = []
        name = profile["name"]
        live = discovered.get(name, {})
        health_state = health.get(name, {}).get("state", "unknown")
        if not profile["enabled"]:
            reasons.append("disabled by owner policy")
        if name in forbidden:
            reasons.append("forbidden by contract")
        if live.get("state") == "unavailable":
            reasons.append("launch evidence proves unavailable")
        if health_state in {"quarantined", "disabled", "unavailable"}:
            reasons.append(f"health state is {health_state}")
        if TIERS[profile["tier"]] < required_tier:
            reasons.append(f"tier {profile['tier']} is below {work['minimum_tier']}")
        if work["task_class"] not in profile["capabilities"]:
            reasons.append(f"lacks task capability {work['task_class']}")
        if work["actuation_mode"] not in profile["actuation_modes"]:
            reasons.append(f"not qualified for {work['actuation_mode']}")
        if reviewer_profile and contract["routing"]["reviewer_must_differ"]:
            if name == reviewer_profile:
                reasons.append("reviewer must differ from implementer")
        if _contraindicated(profile, work):
            reasons.append("task matches an explicit contraindication")
        pools = profile["pools"]
        pool_rows = [capacity.get(pool_id, {}) for pool_id in pools]
        pool_limits = {
            pool["id"]: pool
            for pool in policy.data["pools"]
            if pool["id"] in pools
        }
        for pool_id, limit in pool_limits.items():
            active = _active_reservation_count(pool_id, path=path)
            if active + active_by_pool.get(pool_id, 0) >= limit["max_concurrency"]:
                reasons.append(f"capacity pool {pool_id} has no concurrency slot")
        score = 0.0
        if not reasons:
            score += 100
            score += 30 if name in preferred else 0
            score += 8 if profile["tier"] == work["minimum_tier"] else -5
            if work["task_class"].casefold() in profile["role"].casefold():
                score += 8
            score += 5 if live.get("state") == "available" else 0
            score += 3 if health_state == "available" else 0
            score -= int(active_by_profile.get(name, 0)) * 4
            observed = [
                float(row["headroom_percent"])
                for row in pool_rows
                if isinstance(row.get("headroom_percent"), (int, float))
            ]
            score += (min(observed) / 20) if observed else -2
            if (
                profile["tier"] == "heavy"
                and work["minimum_tier"] != "heavy"
                and work["actuation_mode"] == "general_implementation"
            ):
                score -= 20
        candidates.append({
            "profile": name,
            "eligible": not reasons,
            "reasons": reasons,
            "score": round(score, 3),
            "tier": profile["tier"],
            "pools": pools,
            "availability": live.get("state", "unknown"),
            "health": health_state,
        })
    eligible = sorted(
        (row for row in candidates if row["eligible"]),
        key=lambda row: (-row["score"], row["profile"]),
    )
    selected = eligible[0]["profile"] if eligible else None
    fallbacks = tuple(row["profile"] for row in eligible[1:])
    if selected:
        explanation = (
            f"{selected} is the highest-ranked profile that satisfies the "
            f"{work['minimum_tier']} quality floor, {work['task_class']} capability, "
            f"{work['actuation_mode']} mode, health, independence, and pool constraints"
        )
    else:
        explanation = "no profile satisfies every hard eligibility constraint"
    con = connect(path)
    try:
        cursor = con.execute(
            "INSERT INTO routing_decisions("
            "operation_id, work_item_id, requirements_json, considered_json, "
            "capacity_json, selected_profile, fallback_json, explanation, created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?)",
            (
                operation["id"],
                work["id"],
                _json({
                    "task_class": work["task_class"],
                    "minimum_tier": work["minimum_tier"],
                    "actuation_mode": work["actuation_mode"],
                    "risk": work["risk"],
                }),
                _json(candidates),
                _json(capacity),
                selected,
                _json(list(fallbacks)),
                explanation,
                operator_store.now(),
            ),
        )
        con.commit()
        decision_id = int(cursor.lastrowid)
    finally:
        con.close()
    return Route(selected, fallbacks, tuple(candidates), explanation, decision_id)


def reserve(
    operation_id: str,
    work_item_id: str,
    *,
    profile_name: str,
    policy: RosterPolicy,
    minimum_tier: str,
    burn_band: str = "normal",
    ttl_seconds: int = 900,
    now_epoch: float | None = None,
    path: Path | None = None,
) -> Reservation:
    if burn_band not in BURN_PERCENT:
        raise RosterError(f"unknown burn band {burn_band!r}")
    if not 30 <= ttl_seconds <= 86_400:
        raise RosterError("reservation TTL must be between 30 and 86400 seconds")
    profile = next(
        (row for row in policy.data["profiles"] if row["name"] == profile_name),
        None,
    )
    if profile is None:
        raise RosterError(f"profile {profile_name!r} is absent from roster policy")
    pools = {
        row["id"]: row
        for row in policy.data["pools"]
        if row["id"] in profile["pools"]
    }
    instant = time.time() if now_epoch is None else now_epoch
    expires = instant + ttl_seconds
    estimate = BURN_PERCENT[burn_band]
    group = f"res_{secrets.token_hex(8)}"
    con = connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            "UPDATE capacity_reservations SET state='expired', updated_at=? "
            "WHERE state IN ('reserved','bound') AND expires_at_epoch<=?",
            (operator_store.now(), instant),
        )
        for pool_id, pool in pools.items():
            active = int(
                con.execute(
                    "SELECT COUNT(DISTINCT reservation_group) AS n "
                    "FROM capacity_reservations WHERE pool_id=? "
                    "AND state IN ('reserved','bound') AND expires_at_epoch>?",
                    (pool_id, instant),
                ).fetchone()["n"]
            )
            if active >= pool["max_concurrency"]:
                raise RosterError(f"capacity pool {pool_id} has no concurrency slot")
            observation = con.execute(
                "SELECT headroom_percent, certainty FROM capacity_observations "
                "WHERE pool_id=? ORDER BY id DESC LIMIT 1",
                (pool_id,),
            ).fetchone()
            reserved = float(
                con.execute(
                    "SELECT COALESCE(SUM(estimated_percent),0) AS total "
                    "FROM capacity_reservations WHERE pool_id=? "
                    "AND state IN ('reserved','bound') AND expires_at_epoch>?",
                    (pool_id, instant),
                ).fetchone()["total"]
            )
            if observation and observation["headroom_percent"] is not None:
                after = float(observation["headroom_percent"]) - reserved - estimate
                if minimum_tier != "heavy" and after < float(pool["reserve_percent"]):
                    raise RosterError(
                        f"capacity pool {pool_id} reserve protects heavy/recovery work"
                    )
                if after < 0:
                    raise RosterError(f"capacity pool {pool_id} lacks observed headroom")
            elif active >= 1:
                raise RosterError(
                    f"capacity pool {pool_id} is uncertain and already has a reservation"
                )
        timestamp = operator_store.now()
        for pool_id in pools:
            con.execute(
                "INSERT INTO capacity_reservations("
                "reservation_group, pool_id, operation_id, work_item_id, profile_name, "
                "burn_band, estimated_percent, state, expires_at_epoch, created_at, updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    group,
                    pool_id,
                    operation_id,
                    work_item_id,
                    profile_name,
                    burn_band,
                    estimate,
                    "reserved",
                    expires,
                    timestamp,
                    timestamp,
                ),
            )
        con.commit()
        return Reservation(group, profile_name, tuple(sorted(pools)), expires)
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def bind_reservation(
    group: str,
    *,
    project_run_id: int,
    path: Path | None = None,
) -> None:
    con = connect(path)
    try:
        changed = con.execute(
            "UPDATE capacity_reservations SET state='bound', project_run_id=?, "
            "updated_at=? WHERE reservation_group=? AND state='reserved'",
            (project_run_id, operator_store.now(), group),
        )
        con.commit()
        if changed.rowcount < 1:
            raise RosterError(f"reservation group {group!r} is not reservable")
    finally:
        con.close()


def release_reservation(
    group: str,
    *,
    state: str = "released",
    path: Path | None = None,
) -> None:
    if state not in {"released", "consumed", "expired"}:
        raise RosterError("reservation terminal state is invalid")
    con = connect(path)
    try:
        con.execute(
            "UPDATE capacity_reservations SET state=?, updated_at=? "
            "WHERE reservation_group=? AND state IN ('reserved','bound')",
            (state, operator_store.now(), group),
        )
        con.commit()
    finally:
        con.close()


def release_run_reservations(
    project_run_id: int,
    *,
    state: str = "consumed",
    path: Path | None = None,
) -> int:
    if state not in {"released", "consumed", "expired"}:
        raise RosterError("reservation terminal state is invalid")
    con = connect(path)
    try:
        changed = con.execute(
            "UPDATE capacity_reservations SET state=?, updated_at=? "
            "WHERE project_run_id=? AND state IN ('reserved','bound')",
            (state, operator_store.now(), project_run_id),
        )
        con.commit()
        return int(changed.rowcount)
    finally:
        con.close()


def record_health_signal(
    profile_name: str,
    *,
    signal: str,
    context: dict[str, Any],
    cooldown_seconds: int = 1800,
    now_epoch: float | None = None,
    path: Path | None = None,
) -> str:
    infrastructure_failure = signal in {
        "launch_failure",
        "authentication_failure",
        "zero_output_stall",
        "malformed_event_stream",
        "rate_limit_error",
        "missing_tool",
        "missing_handoff",
    }
    success = signal in {"launch_success", "probe_success", "accepted_work"}
    instant = time.time() if now_epoch is None else now_epoch
    con = connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        current = con.execute(
            "SELECT * FROM profile_health WHERE profile_name=?",
            (profile_name,),
        ).fetchone()
        failures = int(current["consecutive_infra_failures"]) if current else 0
        state = current["state"] if current else "unknown"
        if success:
            failures = 0
            state = "available"
            cooldown = None
            reason = None
        elif infrastructure_failure:
            failures += 1
            if failures >= 3:
                state = "quarantined"
                cooldown = instant + cooldown_seconds
            else:
                state = "degraded"
                cooldown = None
            reason = signal
        else:
            # Task difficulty or over-broad output is an outcome signal, not
            # infrastructure health. Preserve the current launch state.
            cooldown = current["cooldown_until_epoch"] if current else None
            reason = current["reason"] if current else None
        timestamp = operator_store.now()
        con.execute(
            "INSERT INTO profile_health("
            "profile_name, state, consecutive_infra_failures, reason, "
            "cooldown_until_epoch, probe_condition, updated_at"
            ") VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(profile_name) DO UPDATE SET "
            "state=excluded.state, "
            "consecutive_infra_failures=excluded.consecutive_infra_failures, "
            "reason=excluded.reason, cooldown_until_epoch=excluded.cooldown_until_epoch, "
            "probe_condition=excluded.probe_condition, updated_at=excluded.updated_at",
            (
                profile_name,
                state,
                failures,
                reason,
                cooldown,
                "bounded launch probe after cooldown" if state == "quarantined" else None,
                timestamp,
            ),
        )
        con.execute(
            "INSERT INTO profile_health_events("
            "profile_name, signal, context_json, resulting_state, created_at"
            ") VALUES(?,?,?,?,?)",
            (profile_name, signal, _json(context), state, timestamp),
        )
        con.commit()
        return state
    finally:
        con.close()


def health_states(*, path: Path | None = None) -> dict[str, dict[str, Any]]:
    con = connect(path)
    try:
        return {
            row["profile_name"]: dict(row)
            for row in con.execute("SELECT * FROM profile_health ORDER BY profile_name")
        }
    finally:
        con.close()


def create_council(
    operation_id: str,
    work_item_id: str,
    *,
    failure_fingerprint: str,
    evidence: dict[str, Any],
    contract: Mapping[str, Any],
    policy: RosterPolicy,
    path: Path | None = None,
) -> dict[str, Any]:
    council_policy = contract["routing"]["recovery_council"]
    evidence_json = _json(evidence)
    evidence_sha = hashlib.sha256(evidence_json.encode()).hexdigest()
    configured = list(council_policy["members"])
    profiles = _resolve_council_profiles(configured, policy)
    minimum = int(council_policy["minimum_members"])
    quorum = int(council_policy["quorum"])
    if len(profiles) < minimum:
        raise RosterError(
            f"only {len(profiles)} recovery profiles resolve; {minimum} required"
        )
    selected = profiles[: max(minimum, quorum)]
    families = [_model_family(profile) for profile in selected]
    if len(set(families)) < 2:
        raise RosterError("recovery council requires at least two model families")
    con = connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        existing = con.execute(
            "SELECT id FROM recovery_councils WHERE operation_id=? "
            "AND work_item_id=? AND failure_fingerprint=? AND evidence_sha256=?",
            (operation_id, work_item_id, failure_fingerprint, evidence_sha),
        ).fetchone()
        if existing:
            con.commit()
            return get_council(existing["id"], path=path)
        council_id = f"rc_{secrets.token_hex(8)}"
        timestamp = operator_store.now()
        con.execute(
            "INSERT INTO recovery_councils("
            "id, operation_id, work_item_id, evidence_sha256, evidence_json, "
            "failure_fingerprint, minimum_members, quorum, state, created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                council_id,
                operation_id,
                work_item_id,
                evidence_sha,
                evidence_json,
                failure_fingerprint,
                minimum,
                quorum,
                "collecting",
                timestamp,
            ),
        )
        for profile in selected:
            con.execute(
                "INSERT INTO recovery_council_members("
                "council_id, profile_name, model_family, state"
                ") VALUES(?,?,?, 'pending')",
                (council_id, profile["name"], _model_family(profile)),
            )
        con.commit()
        return get_council(council_id, path=path)
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def bind_council_run(
    council_id: str,
    profile_name: str,
    run_id: int,
    *,
    path: Path | None = None,
) -> None:
    con = connect(path)
    try:
        changed = con.execute(
            "UPDATE recovery_council_members SET project_run_id=?, state='running' "
            "WHERE council_id=? AND profile_name=? AND state='pending'",
            (run_id, council_id, profile_name),
        )
        con.commit()
        if changed.rowcount != 1:
            raise RosterError("council member is not pending")
    finally:
        con.close()


def submit_council_opinion(
    council_id: str,
    profile_name: str,
    opinion: Mapping[str, Any],
    *,
    path: Path | None = None,
) -> dict[str, Any]:
    required = {
        "diagnosis",
        "evidence",
        "hypotheses",
        "action_key",
        "next_action",
        "smallest_surface",
        "deferred",
        "confidence",
        "risks",
    }
    if not isinstance(opinion, Mapping) or set(opinion) != required:
        raise RosterError("council opinion has an unexpected shape")
    action_key = opinion.get("action_key")
    if (
        not isinstance(action_key, str)
        or not action_key
        or len(action_key) > 120
        or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for char in action_key)
    ):
        raise RosterError("council action_key must be a lowercase stable identifier")
    if not isinstance(opinion.get("confidence"), (int, float)) or not 0 <= float(
        opinion["confidence"]
    ) <= 1:
        raise RosterError("council confidence must be between 0 and 1")
    con = connect(path)
    try:
        changed = con.execute(
            "UPDATE recovery_council_members SET opinion_json=?, action_key=?, "
            "state='submitted', submitted_at=? "
            "WHERE council_id=? AND profile_name=? AND state IN ('pending','running')",
            (
                _json(dict(opinion)),
                action_key,
                operator_store.now(),
                council_id,
                profile_name,
            ),
        )
        con.commit()
        if changed.rowcount != 1:
            raise RosterError("council member cannot submit in its current state")
        return synthesize_council(council_id, path=path)
    finally:
        con.close()


def synthesize_council(
    council_id: str,
    *,
    path: Path | None = None,
) -> dict[str, Any]:
    con = connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        council = con.execute(
            "SELECT * FROM recovery_councils WHERE id=?",
            (council_id,),
        ).fetchone()
        if council is None:
            raise RosterError(f"unknown recovery council {council_id!r}")
        members = list(
            con.execute(
                "SELECT * FROM recovery_council_members WHERE council_id=? "
                "ORDER BY profile_name",
                (council_id,),
            )
        )
        submitted = [row for row in members if row["state"] == "submitted"]
        if len(submitted) < int(council["minimum_members"]):
            con.commit()
            return _council_dict(council, members)
        counts: dict[str, int] = {}
        for row in submitted:
            counts[row["action_key"]] = counts.get(row["action_key"], 0) + 1
        winners = sorted(
            (
                (count, action)
                for action, count in counts.items()
                if count >= int(council["quorum"])
            ),
            reverse=True,
        )
        if winners:
            count, action_key = winners[0]
            agreeing = [
                {
                    "profile": row["profile_name"],
                    "opinion": json.loads(row["opinion_json"]),
                }
                for row in submitted
                if row["action_key"] == action_key
            ]
            synthesis = {
                "status": "quorum",
                "action_key": action_key,
                "votes": count,
                "agreeing_profiles": [row["profile"] for row in agreeing],
                "next_action": agreeing[0]["opinion"]["next_action"],
                "smallest_surface": agreeing[0]["opinion"]["smallest_surface"],
            }
            state = "quorum"
        elif len(submitted) == len(members):
            synthesis = {
                "status": "split",
                "positions": [
                    {
                        "profile": row["profile_name"],
                        "action_key": row["action_key"],
                        "opinion": json.loads(row["opinion_json"]),
                    }
                    for row in submitted
                ],
            }
            state = "split"
        else:
            con.commit()
            return _council_dict(council, members)
        timestamp = operator_store.now()
        con.execute(
            "UPDATE recovery_councils SET state=?, synthesis_json=?, completed_at=? "
            "WHERE id=?",
            (state, _json(synthesis), timestamp, council_id),
        )
        con.commit()
        updated = con.execute(
            "SELECT * FROM recovery_councils WHERE id=?",
            (council_id,),
        ).fetchone()
        return _council_dict(updated, members)
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def get_council(council_id: str, *, path: Path | None = None) -> dict[str, Any]:
    con = connect(path)
    try:
        council = con.execute(
            "SELECT * FROM recovery_councils WHERE id=?",
            (council_id,),
        ).fetchone()
        if council is None:
            raise RosterError(f"unknown recovery council {council_id!r}")
        members = list(
            con.execute(
                "SELECT * FROM recovery_council_members WHERE council_id=? "
                "ORDER BY profile_name",
                (council_id,),
            )
        )
        return _council_dict(council, members)
    finally:
        con.close()


def _council_dict(
    council: sqlite3.Row,
    members: Iterable[sqlite3.Row],
) -> dict[str, Any]:
    return {
        **dict(council),
        "evidence": json.loads(council["evidence_json"]),
        "synthesis": (
            json.loads(council["synthesis_json"]) if council["synthesis_json"] else None
        ),
        "members": [
            {
                **dict(row),
                "opinion": json.loads(row["opinion_json"]) if row["opinion_json"] else None,
            }
            for row in members
        ],
    }


def _resolve_council_profiles(
    configured: list[str],
    policy: RosterPolicy,
) -> list[dict[str, Any]]:
    by_name = {row["name"].casefold(): row for row in policy.data["profiles"]}
    selected: list[dict[str, Any]] = []
    for requested in configured:
        profile = by_name.get(requested.casefold())
        if profile is None:
            matches = [
                row
                for row in policy.data["profiles"]
                if isinstance(row.get("model"), str)
                and requested.casefold() in row["model"].casefold()
            ]
            profile = matches[0] if len(matches) == 1 else None
        if profile and profile["enabled"] and profile["tier"] == "heavy":
            if profile["name"] not in {row["name"] for row in selected}:
                selected.append(profile)
    if len(selected) < 2:
        extras = [
            row
            for row in policy.data["profiles"]
            if row["enabled"]
            and row["tier"] == "heavy"
            and "diagnose_only" in row["actuation_modes"]
            and row["name"] not in {item["name"] for item in selected}
        ]
        extras.sort(key=lambda row: row["name"])
        selected.extend(extras)
    return selected


def _model_family(profile: Mapping[str, Any]) -> str:
    backend = profile["backend"]
    model = str(profile.get("model") or "")
    if backend == "claude":
        return "anthropic"
    if backend == "codex":
        return "openai"
    return model.split("/", 1)[0] if "/" in model else backend


def _infer_tier(name: str, role: str) -> str:
    text = f"{name} {role}".casefold()
    if any(
        phrase in text
        for phrase in (
            "heavy reasoning",
            "heaviest tier",
            "hardest reasoning",
            "mythos",
            "opus",
            "max-thinking",
            "tough thinking",
            "recovery/handler",
        )
    ):
        return "heavy"
    if any(
        phrase in text
        for phrase in (
            "workhorse",
            "mechanical",
            "cheap",
            "basic sweep",
            "high-volume simple",
        )
    ):
        return "workhorse"
    return "generalist"


def _infer_capabilities(tier: str, role: str) -> list[str]:
    capabilities = {"documentation", "review"}
    if tier == "heavy":
        capabilities.update({"architecture", "feature", "investigation", "integration"})
    elif tier == "generalist":
        capabilities.update({"feature", "investigation", "mechanical"})
    else:
        capabilities.update({"feature", "mechanical"})
    lowered = role.casefold()
    if "visual" in lowered:
        capabilities.add("visual")
    if "security" in lowered and "do not" not in lowered:
        capabilities.add("security")
    return sorted(capabilities)


def _infer_modes(name: str, tier: str, role: str) -> list[str]:
    if tier == "heavy":
        modes = {"diagnose_only", "review_only", "bounded_patch"}
        if any(word in f"{name} {role}".casefold() for word in ("general", "implementation")):
            modes.add("general_implementation")
    elif tier == "generalist":
        modes = set(ACTUATION_MODES)
    else:
        modes = {"review_only", "bounded_patch", "general_implementation"}
    return sorted(modes)


def _infer_contraindications(role: str) -> list[str]:
    text = role.casefold()
    contraindications: list[str] = []
    if "do not route" in text and "security" in text:
        contraindications.append("security")
    if any(word in text for word in ("flaky", "broken", "do-not-use")):
        contraindications.append("critical")
    return contraindications


def _contraindicated(profile: Mapping[str, Any], work: Mapping[str, Any]) -> bool:
    haystack = " ".join(
        str(value)
        for value in (
            work.get("task_class"),
            work.get("risk"),
            work.get("title"),
            work.get("description"),
        )
    ).casefold()
    return any(term.casefold() in haystack for term in profile["contraindications"])


def _active_reservation_count(pool_id: str, *, path: Path | None) -> int:
    con = connect(path)
    try:
        return int(
            con.execute(
                "SELECT COUNT(DISTINCT reservation_group) AS n "
                "FROM capacity_reservations WHERE pool_id=? "
                "AND state IN ('reserved','bound') AND expires_at_epoch>?",
                (pool_id, time.time()),
            ).fetchone()["n"]
        )
    finally:
        con.close()


def _bounded_string(
    value: Any,
    path: str,
    errors: list[str],
    *,
    maximum: int = 240,
) -> None:
    if not isinstance(value, str) or not value or len(value) > maximum:
        errors.append(f"{path} must be a non-empty string up to {maximum} characters")


def _json(value: Any) -> str:
    return operator_runtime.safe_json(value)
