"""Durable operation state and controller coordination.

This module is the state machine beneath the Operator controller.  It owns no
model judgment and performs no project mutation: it records goals, bounded
work, decisions, action intents, wakeups, attempts, and the single-controller
lease that deterministic brokers act on.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from orchestra_cli import checkpoint, operator_contract, operator_store

MAX_RUNTIME_JSON_BYTES = 128 * 1024
LEASE_SECONDS = 45

OPERATION_TERMINAL = {"achieved", "stopped", "failed"}
OPERATION_STATES = {
    "active",
    "waiting",
    "needs_decision",
    "paused",
    "maintaining",
    *OPERATION_TERMINAL,
}
WORK_STATES = {
    "proposed",
    "ready",
    "dispatched",
    "running",
    "handed_off",
    "verifying",
    "integrating",
    "accepted",
    "blocked",
    "needs_decision",
    "failed_retryable",
    "failed_terminal",
    "needs_revision",
    "superseded",
    "cancelled",
}
WORK_TERMINAL = {
    "accepted",
    "failed_terminal",
    "superseded",
    "cancelled",
}

RUNTIME_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS logical_projects (
  id TEXT PRIMARY KEY,
  registry_id TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  root TEXT NOT NULL,
  created_at TEXT NOT NULL,
  rebound_at TEXT
);

CREATE TABLE IF NOT EXISTS operations (
  id TEXT PRIMARY KEY,
  operator_id TEXT NOT NULL REFERENCES operators(id),
  contract_version INTEGER NOT NULL,
  mode TEXT NOT NULL CHECK(mode IN ('shadow', 'live')),
  state TEXT NOT NULL,
  priority INTEGER NOT NULL CHECK(priority BETWEEN 0 AND 100),
  controller_pid INTEGER,
  created_at TEXT NOT NULL,
  activated_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  stopped_at TEXT,
  last_error TEXT,
  FOREIGN KEY(operator_id, contract_version)
    REFERENCES contract_versions(operator_id, version)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_one_live_operation_per_operator
  ON operations(operator_id)
  WHERE state NOT IN ('achieved', 'stopped', 'failed');

CREATE TABLE IF NOT EXISTS operation_projects (
  operation_id TEXT NOT NULL REFERENCES operations(id),
  project_key TEXT NOT NULL REFERENCES logical_projects(id),
  contract_project_id TEXT NOT NULL,
  name TEXT NOT NULL,
  root TEXT NOT NULL,
  target_branch TEXT NOT NULL,
  integration_branch TEXT NOT NULL,
  expected_head TEXT,
  PRIMARY KEY(operation_id, project_key),
  UNIQUE(operation_id, contract_project_id)
);

CREATE TABLE IF NOT EXISTS operator_goals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  operation_id TEXT NOT NULL REFERENCES operations(id),
  goal_key TEXT NOT NULL,
  outcome TEXT NOT NULL,
  priority INTEGER NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending',
  acceptance_evidence_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(operation_id, goal_key)
);

CREATE TABLE IF NOT EXISTS operator_work_items (
  id TEXT PRIMARY KEY,
  operation_id TEXT NOT NULL REFERENCES operations(id),
  goal_id INTEGER NOT NULL REFERENCES operator_goals(id),
  project_key TEXT NOT NULL REFERENCES logical_projects(id),
  title TEXT NOT NULL,
  description TEXT NOT NULL,
  state TEXT NOT NULL,
  task_class TEXT NOT NULL,
  minimum_tier TEXT NOT NULL,
  actuation_mode TEXT NOT NULL,
  risk TEXT NOT NULL,
  requirements_json TEXT NOT NULL,
  dependencies_json TEXT NOT NULL,
  change_budget_json TEXT NOT NULL,
  requires_review INTEGER NOT NULL DEFAULT 0,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  failure_fingerprint TEXT,
  selected_profile TEXT,
  project_run_id INTEGER,
  branch TEXT,
  base_head TEXT,
  handoff_json TEXT,
  complexity_json TEXT,
  verification_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_operator_work_ready
  ON operator_work_items(operation_id, state, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_operator_work_run
  ON operator_work_items(project_key, project_run_id)
  WHERE project_run_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS operator_decisions (
  id TEXT PRIMARY KEY,
  operation_id TEXT NOT NULL REFERENCES operations(id),
  work_item_id TEXT REFERENCES operator_work_items(id),
  idempotency_key TEXT UNIQUE NOT NULL,
  question TEXT NOT NULL,
  why_now TEXT NOT NULL,
  options_json TEXT NOT NULL,
  recommendation TEXT NOT NULL,
  safe_default TEXT,
  evidence_json TEXT NOT NULL,
  blocking_scope_json TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'open',
  deadline_at TEXT,
  answer TEXT,
  answered_by TEXT,
  created_at TEXT NOT NULL,
  answered_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_operator_decisions_open
  ON operator_decisions(operation_id, state);

CREATE TABLE IF NOT EXISTS operator_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  operation_id TEXT NOT NULL REFERENCES operations(id),
  generation INTEGER NOT NULL,
  snapshot_sha256 TEXT NOT NULL,
  snapshot_json TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  outcome TEXT,
  summary TEXT
);

CREATE TABLE IF NOT EXISTS operator_action_intents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  operation_id TEXT NOT NULL REFERENCES operations(id),
  attempt_id INTEGER REFERENCES operator_attempts(id),
  work_item_id TEXT REFERENCES operator_work_items(id),
  idempotency_key TEXT UNIQUE NOT NULL,
  kind TEXT NOT NULL,
  authority_action TEXT NOT NULL,
  target_json TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  state TEXT NOT NULL,
  result_json TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_operator_actions_pending
  ON operator_action_intents(operation_id, state, id);

CREATE TABLE IF NOT EXISTS controller_leases (
  operation_id TEXT PRIMARY KEY REFERENCES operations(id),
  holder TEXT NOT NULL,
  generation INTEGER NOT NULL,
  acquired_at TEXT NOT NULL,
  heartbeat_at TEXT NOT NULL,
  expires_at_epoch REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS operator_project_leases (
  project_key TEXT PRIMARY KEY REFERENCES logical_projects(id),
  operation_id TEXT NOT NULL REFERENCES operations(id),
  holder TEXT NOT NULL,
  generation INTEGER NOT NULL,
  purpose TEXT NOT NULL,
  acquired_at TEXT NOT NULL,
  expires_at_epoch REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS operator_wakeups (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  operation_id TEXT NOT NULL REFERENCES operations(id),
  kind TEXT NOT NULL,
  event_key TEXT,
  due_at_epoch REAL,
  state TEXT NOT NULL DEFAULT 'scheduled',
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL,
  fired_at TEXT,
  UNIQUE(operation_id, kind, event_key, due_at_epoch, state)
);

CREATE INDEX IF NOT EXISTS idx_operator_wakeups_due
  ON operator_wakeups(state, due_at_epoch);

CREATE TABLE IF NOT EXISTS operator_event_cursors (
  operation_id TEXT NOT NULL REFERENCES operations(id),
  project_key TEXT NOT NULL REFERENCES logical_projects(id),
  max_run_id INTEGER,
  max_message_id INTEGER,
  max_feed_id INTEGER,
  observed_at TEXT NOT NULL,
  PRIMARY KEY(operation_id, project_key)
);

CREATE TABLE IF NOT EXISTS operator_observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  operation_id TEXT NOT NULL REFERENCES operations(id),
  kind TEXT NOT NULL,
  subject TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  observed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_operator_observations_recent
  ON operator_observations(operation_id, kind, id DESC);

CREATE TABLE IF NOT EXISTS operator_resource_leases (
  id TEXT PRIMARY KEY,
  operation_id TEXT NOT NULL REFERENCES operations(id),
  work_item_id TEXT REFERENCES operator_work_items(id),
  project_key TEXT NOT NULL REFERENCES logical_projects(id),
  kind TEXT NOT NULL,
  resource_path TEXT NOT NULL,
  project_run_id INTEGER,
  state TEXT NOT NULL,
  measured_bytes INTEGER,
  unique_state INTEGER NOT NULL DEFAULT 1,
  details_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  released_at TEXT,
  UNIQUE(operation_id, kind, resource_path)
);

CREATE INDEX IF NOT EXISTS idx_operator_resources_active
  ON operator_resource_leases(operation_id, state, kind);

CREATE TABLE IF NOT EXISTS operator_runtime_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  operation_id TEXT NOT NULL REFERENCES operations(id),
  kind TEXT NOT NULL,
  subject TEXT,
  details_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_operator_runtime_events
  ON operator_runtime_events(operation_id, id);

CREATE TRIGGER IF NOT EXISTS immutable_operator_runtime_events_update
BEFORE UPDATE ON operator_runtime_events
BEGIN
  SELECT RAISE(ABORT, 'operator runtime events are immutable');
END;

CREATE TRIGGER IF NOT EXISTS immutable_operator_runtime_events_delete
BEFORE DELETE ON operator_runtime_events
BEGIN
  SELECT RAISE(ABORT, 'operator runtime events are immutable');
END;

CREATE TRIGGER IF NOT EXISTS immutable_operator_attempts_delete
BEFORE DELETE ON operator_attempts
BEGIN
  SELECT RAISE(ABORT, 'operator attempts are immutable');
END;
"""


class RuntimeError(Exception):
    """The durable operation state could not satisfy a requested transition."""


class LeaseBusyError(RuntimeError):
    pass


class LeaseLostError(RuntimeError):
    pass


@dataclass(frozen=True)
class Lease:
    operation_id: str
    holder: str
    generation: int
    expires_at_epoch: float


def connect(path: Path | None = None) -> sqlite3.Connection:
    con = operator_store.connect(path)
    con.executescript(RUNTIME_SCHEMA)
    columns = {
        row["name"] for row in con.execute("PRAGMA table_info(operation_projects)")
    }
    if "expected_head" not in columns:
        try:
            con.execute("ALTER TABLE operation_projects ADD COLUMN expected_head TEXT")
        except sqlite3.OperationalError:
            refreshed = {
                row["name"]
                for row in con.execute("PRAGMA table_info(operation_projects)")
            }
            if "expected_head" not in refreshed:
                raise
        con.commit()
    return con


def controller_holder() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(4)}"


def start_operation(
    operator_identifier: str,
    *,
    mode: str,
    priority: int,
    registered_projects: Iterable[dict[str, Any]],
    path: Path | None = None,
) -> dict[str, Any]:
    if mode not in {"shadow", "live"}:
        raise RuntimeError("operation mode must be 'shadow' or 'live'")
    if not isinstance(priority, int) or isinstance(priority, bool) or not 0 <= priority <= 100:
        raise RuntimeError("operation priority must be an integer between 0 and 100")
    operator_status = operator_store.get_status(operator_identifier, path=path)
    if operator_status["state"] != "approved":
        raise RuntimeError(
            f"Operator {operator_status['id']} latest contract is not approved"
        )
    contract = operator_store.get_contract(operator_status["id"], path=path)
    registered = {
        row["id"]: dict(row)
        for row in registered_projects
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    scoped_ids = list(operator_contract.project_ids(contract.data))
    missing = [project_id for project_id in scoped_ids if project_id not in registered]
    if missing:
        raise RuntimeError(
            "operation scope is no longer registered: " + ", ".join(missing)
        )
    if mode == "live":
        if (
            contract.data.get("schema") == operator_contract.SCHEMA_TAG_V1
            and len(scoped_ids) > 1
        ):
            raise RuntimeError(
                "live multi-project operations require an "
                "orchestra.operator-contract/v2 contract"
            )
        if contract.data["resources"]["max_cost_usd"] is not None:
            raise RuntimeError(
                "live activation cannot enforce max_cost_usd because project runs "
                "do not expose authoritative cost metering"
            )
        unavailable = [
            project_id
            for project_id in scoped_ids
            if not bool(registered[project_id].get("available"))
        ]
        if unavailable:
            raise RuntimeError(
                "live operation projects are unavailable: " + ", ".join(unavailable)
            )
        covered = {
            item["project_id"]
            for item in contract.data["quality"]["verification"]
            if item.get("required")
        }
        uncovered = [project_id for project_id in scoped_ids if project_id not in covered]
        if uncovered:
            raise RuntimeError(
                "live activation requires a required verification command for "
                "every project; missing: " + ", ".join(uncovered)
            )
        live_heads = {}
        for project_id in scoped_ids:
            live_heads[project_id] = _live_project_preflight(
                Path(str(registered[project_id]["root"])),
                expected_branch=contract.data["scope"]["integration_branch"],
            )
    else:
        live_heads = {}

    con = connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        existing = con.execute(
            "SELECT id FROM operations WHERE operator_id=? "
            "AND state NOT IN ('achieved','stopped','failed')",
            (operator_status["id"],),
        ).fetchone()
        if existing is not None:
            raise RuntimeError(
                f"Operator {operator_status['id']} already has live operation "
                f"{existing['id']}"
            )
        claimed_projects = list(
            con.execute(
                "SELECT DISTINCT p.contract_project_id, o.id "
                "FROM operation_projects p JOIN operations o ON o.id=p.operation_id "
                "WHERE p.contract_project_id IN ("
                + ",".join("?" for _ in scoped_ids)
                + ") AND o.mode='live' "
                "AND o.state NOT IN ('achieved','stopped','failed')",
                scoped_ids,
            )
        ) if mode == "live" else []
        if claimed_projects:
            claims = ", ".join(
                f"{row['contract_project_id']} by {row['id']}"
                for row in claimed_projects
            )
            raise RuntimeError(
                "live project ownership is already held by another operation: "
                + claims
            )
        operation_id = _new_id("opn")
        timestamp = operator_store.now()
        con.execute(
            "INSERT INTO operations("
            "id, operator_id, contract_version, mode, state, priority, "
            "created_at, activated_at, updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?)",
            (
                operation_id,
                operator_status["id"],
                operator_status["contract_version"],
                mode,
                "active",
                priority,
                timestamp,
                timestamp,
                timestamp,
            ),
        )
        project_keys: dict[str, str] = {}
        for project_id in scoped_ids:
            row = registered[project_id]
            project_key = _sync_logical_project(con, row, timestamp)
            project_keys[project_id] = project_key
            con.execute(
                "INSERT INTO operation_projects("
                "operation_id, project_key, contract_project_id, name, root, "
                "target_branch, integration_branch, expected_head"
                ") VALUES(?,?,?,?,?,?,?,?)",
                (
                    operation_id,
                    project_key,
                    project_id,
                    str(row.get("name") or project_id)[:160],
                    str(row["root"])[:4096],
                    contract.data["scope"]["target_branch"],
                    contract.data["scope"]["integration_branch"],
                    live_heads.get(project_id),
                ),
            )
        primary_project = project_keys[scoped_ids[0]]
        goal_rows: list[tuple[dict[str, Any], int, str]] = []
        for goal in contract.data["intent"]["goals"]:
            cursor = con.execute(
                "INSERT INTO operator_goals("
                "operation_id, goal_key, outcome, priority, state, created_at, updated_at"
                ") VALUES(?,?,?,?,?,?,?)",
                (
                    operation_id,
                    goal["id"],
                    goal["outcome"],
                    goal["priority"],
                    "active",
                    timestamp,
                    timestamp,
                ),
            )
            goal_project_id = (
                goal["project_id"]
                if operator_contract.is_v2(contract.data)
                else scoped_ids[0]
            )
            work_id = _insert_initial_work(
                con,
                operation_id=operation_id,
                goal_id=int(cursor.lastrowid),
                project_key=project_keys.get(goal_project_id, primary_project),
                goal=goal,
                contract=contract.data,
                timestamp=timestamp,
            )
            goal_rows.append((goal, int(cursor.lastrowid), work_id))
        work_by_goal = {goal["id"]: work_id for goal, _goal_id, work_id in goal_rows}
        for goal, _goal_id, work_id in goal_rows:
            dependencies = [
                work_by_goal[dependency]
                for dependency in goal.get("depends_on", [])
            ]
            con.execute(
                "UPDATE operator_work_items SET dependencies_json=? WHERE id=?",
                (_json(dependencies), work_id),
            )
        _event(
            con,
            operation_id,
            "operation_started",
            operation_id,
            {
                "mode": mode,
                "priority": priority,
                "contract_version": operator_status["contract_version"],
                "contract_sha256": operator_status["contract_sha256"],
                "projects": scoped_ids,
            },
            timestamp,
        )
        con.commit()
        return get_operation(operation_id, path=path)
    except sqlite3.IntegrityError as exc:
        con.rollback()
        raise RuntimeError(f"could not start operation: {exc}") from exc
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def get_operation(identifier: str, *, path: Path | None = None) -> dict[str, Any]:
    con = connect(path)
    try:
        operation = _resolve_operation(con, identifier)
        return _operation_dict(con, operation)
    finally:
        con.close()


def list_operations(*, path: Path | None = None) -> list[dict[str, Any]]:
    con = connect(path)
    try:
        rows = con.execute(
            "SELECT * FROM operations ORDER BY created_at DESC, id DESC"
        ).fetchall()
        return [_operation_dict(con, row) for row in rows]
    finally:
        con.close()


def set_operation_state(
    identifier: str,
    state: str,
    *,
    reason: str,
    path: Path | None = None,
) -> dict[str, Any]:
    if state not in OPERATION_STATES:
        raise RuntimeError(f"unknown operation state {state!r}")
    con = connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        operation = _resolve_operation(con, identifier)
        current = operation["state"]
        if current in OPERATION_TERMINAL:
            if current == state:
                con.commit()
                return _operation_dict(con, operation)
            raise RuntimeError(
                f"terminal operation {operation['id']} cannot move from {current} to {state}"
            )
        timestamp = operator_store.now()
        stopped_at = timestamp if state in OPERATION_TERMINAL else None
        con.execute(
            "UPDATE operations SET state=?, updated_at=?, stopped_at=COALESCE(?, stopped_at) "
            "WHERE id=?",
            (state, timestamp, stopped_at, operation["id"]),
        )
        _event(
            con,
            operation["id"],
            f"operation_{state}",
            operation["id"],
            {"from": current, "reason": _bounded_text(reason, 4096)},
            timestamp,
        )
        con.commit()
        return _operation_dict(
            con,
            con.execute("SELECT * FROM operations WHERE id=?", (operation["id"],)).fetchone(),
        )
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def set_controller_pid(
    operation_id: str,
    pid: int | None,
    *,
    path: Path | None = None,
) -> None:
    con = connect(path)
    try:
        con.execute(
            "UPDATE operations SET controller_pid=?, updated_at=? WHERE id=?",
            (pid, operator_store.now(), operation_id),
        )
        con.commit()
    finally:
        con.close()


def record_controller_error(
    operation_id: str,
    error: str | None,
    *,
    path: Path | None = None,
) -> None:
    con = connect(path)
    try:
        con.execute(
            "UPDATE operations SET last_error=?, updated_at=? WHERE id=?",
            (_bounded_text(error, 4096) if error else None, operator_store.now(), operation_id),
        )
        con.commit()
    finally:
        con.close()


def add_work_item(
    operation_id: str,
    *,
    goal_key: str,
    contract_project_id: str,
    title: str,
    description: str,
    task_class: str,
    minimum_tier: str,
    actuation_mode: str,
    risk: str = "normal",
    requirements: dict[str, Any] | None = None,
    dependencies: list[str] | None = None,
    change_budget: dict[str, Any] | None = None,
    requires_review: bool = False,
    path: Path | None = None,
) -> str:
    if minimum_tier not in {"workhorse", "generalist", "heavy"}:
        raise RuntimeError("minimum tier must be workhorse, generalist, or heavy")
    if actuation_mode not in {
        "diagnose_only",
        "review_only",
        "bounded_patch",
        "general_implementation",
    }:
        raise RuntimeError("unsupported actuation mode")
    con = connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        operation = _resolve_operation(con, operation_id)
        goal = con.execute(
            "SELECT id FROM operator_goals WHERE operation_id=? AND goal_key=?",
            (operation["id"], goal_key),
        ).fetchone()
        project = con.execute(
            "SELECT project_key FROM operation_projects "
            "WHERE operation_id=? AND contract_project_id=?",
            (operation["id"], contract_project_id),
        ).fetchone()
        if goal is None:
            raise RuntimeError(f"unknown goal {goal_key!r}")
        if project is None:
            raise RuntimeError(f"project {contract_project_id!r} is outside operation scope")
        work_id = _new_id("ow")
        timestamp = operator_store.now()
        con.execute(
            "INSERT INTO operator_work_items("
            "id, operation_id, goal_id, project_key, title, description, state, "
            "task_class, minimum_tier, actuation_mode, risk, requirements_json, "
            "dependencies_json, change_budget_json, requires_review, created_at, updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                work_id,
                operation["id"],
                goal["id"],
                project["project_key"],
                _bounded_text(title, 240),
                _bounded_text(description, 16_384),
                "ready",
                _bounded_text(task_class, 64),
                minimum_tier,
                actuation_mode,
                _bounded_text(risk, 32),
                _json(requirements or {}),
                _json(dependencies or []),
                _json(change_budget or {}),
                int(requires_review),
                timestamp,
                timestamp,
            ),
        )
        _event(
            con,
            operation["id"],
            "work_created",
            work_id,
            {"goal": goal_key, "project": contract_project_id},
            timestamp,
        )
        con.commit()
        return work_id
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def work_items(
    operation_id: str,
    *,
    states: Iterable[str] | None = None,
    path: Path | None = None,
) -> list[dict[str, Any]]:
    con = connect(path)
    try:
        operation = _resolve_operation(con, operation_id)
        query = (
            "SELECT w.*, g.goal_key, p.contract_project_id, p.root, "
            "p.target_branch, p.integration_branch "
            "FROM operator_work_items w "
            "JOIN operator_goals g ON g.id=w.goal_id "
            "JOIN operation_projects p ON p.operation_id=w.operation_id "
            "AND p.project_key=w.project_key "
            "WHERE w.operation_id=?"
        )
        params: list[Any] = [operation["id"]]
        selected = list(states or [])
        if selected:
            unknown = set(selected) - WORK_STATES
            if unknown:
                raise RuntimeError(f"unknown work states: {', '.join(sorted(unknown))}")
            query += " AND w.state IN (" + ",".join("?" for _ in selected) + ")"
            params.extend(selected)
        query += " ORDER BY g.priority, w.created_at, w.id"
        return [_work_dict(row) for row in con.execute(query, params)]
    finally:
        con.close()


def transition_work(
    work_id: str,
    state: str,
    *,
    details: dict[str, Any] | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    if state not in WORK_STATES:
        raise RuntimeError(f"unknown work state {state!r}")
    con = connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM operator_work_items WHERE id=?",
            (work_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"unknown work item {work_id!r}")
        if row["state"] in WORK_TERMINAL and row["state"] != state:
            raise RuntimeError(
                f"terminal work item {work_id} cannot move from {row['state']} to {state}"
            )
        timestamp = operator_store.now()
        con.execute(
            "UPDATE operator_work_items SET state=?, updated_at=? WHERE id=?",
            (state, timestamp, work_id),
        )
        _event(
            con,
            row["operation_id"],
            f"work_{state}",
            work_id,
            {"from": row["state"], **(details or {})},
            timestamp,
        )
        if state == "accepted":
            _maybe_accept_goal(con, int(row["goal_id"]), timestamp)
        con.commit()
        updated = con.execute(
            "SELECT w.*, g.goal_key, p.contract_project_id, p.root, "
            "p.target_branch, p.integration_branch "
            "FROM operator_work_items w "
            "JOIN operator_goals g ON g.id=w.goal_id "
            "JOIN operation_projects p ON p.operation_id=w.operation_id "
            "AND p.project_key=w.project_key WHERE w.id=?",
            (work_id,),
        ).fetchone()
        return _work_dict(updated)
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def bind_work_run(
    work_id: str,
    *,
    profile: str,
    run_id: int,
    branch: str | None,
    base_head: str | None,
    path: Path | None = None,
) -> None:
    con = connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT operation_id, state, attempt_count FROM operator_work_items WHERE id=?",
            (work_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"unknown work item {work_id!r}")
        if row["state"] not in {"ready", "failed_retryable", "needs_revision"}:
            raise RuntimeError(
                f"work item {work_id} in {row['state']} cannot bind a new run"
            )
        timestamp = operator_store.now()
        con.execute(
            "UPDATE operator_work_items SET state='dispatched', selected_profile=?, "
            "project_run_id=?, branch=?, base_head=?, attempt_count=?, updated_at=? "
            "WHERE id=?",
            (
                _bounded_text(profile, 160),
                run_id,
                branch,
                base_head,
                int(row["attempt_count"]) + 1,
                timestamp,
                work_id,
            ),
        )
        _event(
            con,
            row["operation_id"],
            "work_dispatched",
            work_id,
            {"profile": profile, "run_id": run_id, "branch": branch},
            timestamp,
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def record_work_result(
    work_id: str,
    *,
    state: str,
    handoff: dict[str, Any] | None = None,
    complexity: dict[str, Any] | None = None,
    verification: dict[str, Any] | None = None,
    failure_fingerprint: str | None = None,
    path: Path | None = None,
) -> None:
    if state not in WORK_STATES:
        raise RuntimeError(f"unknown work state {state!r}")
    con = connect(path)
    try:
        row = con.execute(
            "SELECT operation_id FROM operator_work_items WHERE id=?",
            (work_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"unknown work item {work_id!r}")
        timestamp = operator_store.now()
        con.execute(
            "UPDATE operator_work_items SET state=?, handoff_json=COALESCE(?,handoff_json), "
            "complexity_json=COALESCE(?,complexity_json), "
            "verification_json=COALESCE(?,verification_json), "
            "failure_fingerprint=?, updated_at=? WHERE id=?",
            (
                state,
                _json(handoff) if handoff is not None else None,
                _json(complexity) if complexity is not None else None,
                _json(verification) if verification is not None else None,
                failure_fingerprint,
                timestamp,
                work_id,
            ),
        )
        _event(
            con,
            row["operation_id"],
            f"work_{state}",
            work_id,
            {
                "failure_fingerprint": failure_fingerprint,
                "has_handoff": handoff is not None,
                "has_verification": verification is not None,
            },
            timestamp,
        )
        if state == "accepted":
            goal = con.execute(
                "SELECT goal_id FROM operator_work_items WHERE id=?",
                (work_id,),
            ).fetchone()
            _maybe_accept_goal(con, int(goal["goal_id"]), timestamp)
        con.commit()
    finally:
        con.close()


def create_decision(
    operation_id: str,
    *,
    idempotency_key: str,
    question: str,
    why_now: str,
    options: list[dict[str, Any]],
    recommendation: str,
    evidence: dict[str, Any],
    blocking_scope: dict[str, Any],
    work_item_id: str | None = None,
    safe_default: str | None = None,
    deadline_at: str | None = None,
    path: Path | None = None,
) -> str:
    if len(options) < 1:
        raise RuntimeError("a decision requires at least one concrete option")
    con = connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        operation = _resolve_operation(con, operation_id)
        existing = con.execute(
            "SELECT id FROM operator_decisions WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if existing is not None:
            con.commit()
            return existing["id"]
        decision_id = _new_id("od")
        timestamp = operator_store.now()
        con.execute(
            "INSERT INTO operator_decisions("
            "id, operation_id, work_item_id, idempotency_key, question, why_now, "
            "options_json, recommendation, safe_default, evidence_json, "
            "blocking_scope_json, state, deadline_at, created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                decision_id,
                operation["id"],
                work_item_id,
                _bounded_text(idempotency_key, 240),
                _bounded_text(question, 4096),
                _bounded_text(why_now, 4096),
                _json(options),
                _bounded_text(recommendation, 4096),
                _bounded_text(safe_default, 4096) if safe_default else None,
                _json(evidence),
                _json(blocking_scope),
                "open",
                deadline_at,
                timestamp,
            ),
        )
        if work_item_id:
            con.execute(
                "UPDATE operator_work_items SET state='needs_decision', updated_at=? "
                "WHERE id=? AND operation_id=?",
                (timestamp, work_item_id, operation["id"]),
            )
        _event(
            con,
            operation["id"],
            "decision_opened",
            decision_id,
            {"question": _bounded_text(question, 512), "work_item_id": work_item_id},
            timestamp,
        )
        con.commit()
        return decision_id
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def answer_decision(
    decision_id: str,
    *,
    answer: str,
    answered_by: str,
    path: Path | None = None,
) -> None:
    con = connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        decision = con.execute(
            "SELECT * FROM operator_decisions WHERE id=?",
            (decision_id,),
        ).fetchone()
        if decision is None:
            raise RuntimeError(f"unknown decision {decision_id!r}")
        if decision["state"] != "open":
            raise RuntimeError(f"decision {decision_id} is already {decision['state']}")
        timestamp = operator_store.now()
        normalized_answer = answer.strip().casefold()
        con.execute(
            "UPDATE operator_decisions SET state='answered', answer=?, answered_by=?, "
            "answered_at=? WHERE id=?",
            (
                _bounded_text(answer, 16_384),
                _bounded_text(answered_by, 160),
                timestamp,
                decision_id,
            ),
        )
        blocking_scope = json.loads(decision["blocking_scope_json"])
        action_id = blocking_scope.get("action_id")
        action_state = None
        action_kind = None
        if isinstance(action_id, int):
            action_state = "authorized" if normalized_answer in {
                "approve",
                "approved",
                "yes",
                "proceed",
            } else "denied"
            action_row = con.execute(
                "SELECT kind FROM operator_action_intents WHERE id=?",
                (action_id,),
            ).fetchone()
            action_kind = action_row["kind"] if action_row else None
            con.execute(
                "UPDATE operator_action_intents SET state=?, updated_at=? "
                "WHERE id=? AND state='waiting'",
                (action_state, timestamp, action_id),
            )
        if decision["work_item_id"]:
            if normalized_answer in {"stop", "stop operation", "abort"}:
                work_state = "failed_terminal"
                con.execute(
                    "UPDATE operations SET state='failed', stopped_at=?, updated_at=? "
                    "WHERE id=? AND state NOT IN ('achieved','stopped','failed')",
                    (timestamp, timestamp, decision["operation_id"]),
                )
            elif action_state == "denied":
                work_state = "blocked"
            elif action_kind in {
                "exceed bounded change budget",
                "accept out-of-scope change",
            }:
                work_state = "handed_off"
            elif action_kind == "merge verified isolated branch":
                work_state = "integrating"
            else:
                work_state = "ready"
            con.execute(
                "UPDATE operator_work_items SET state=?, updated_at=? "
                "WHERE id=? AND state='needs_decision'",
                (work_state, timestamp, decision["work_item_id"]),
            )
        remaining = int(
            con.execute(
                "SELECT COUNT(*) AS n FROM operator_decisions "
                "WHERE operation_id=? AND state='open'",
                (decision["operation_id"],),
            ).fetchone()["n"]
        )
        if remaining == 0:
            con.execute(
                "UPDATE operations SET state='active', updated_at=? "
                "WHERE id=? AND state='needs_decision'",
                (timestamp, decision["operation_id"]),
            )
        _event(
            con,
            decision["operation_id"],
            "decision_answered",
            decision_id,
            {"answered_by": answered_by},
            timestamp,
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def decisions(
    operation_id: str,
    *,
    state: str | None = None,
    path: Path | None = None,
) -> list[dict[str, Any]]:
    con = connect(path)
    try:
        operation = _resolve_operation(con, operation_id)
        query = "SELECT * FROM operator_decisions WHERE operation_id=?"
        params: list[Any] = [operation["id"]]
        if state:
            query += " AND state=?"
            params.append(state)
        query += " ORDER BY created_at, id"
        return [_decision_dict(row) for row in con.execute(query, params)]
    finally:
        con.close()


def propose_action(
    operation_id: str,
    *,
    attempt_id: int | None,
    idempotency_key: str,
    kind: str,
    authority_action: str,
    target: dict[str, Any],
    evidence: dict[str, Any],
    work_item_id: str | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    if authority_action not in operator_contract.AUTHORITY_ACTIONS:
        raise RuntimeError(f"unknown authority action {authority_action!r}")
    operation = get_operation(operation_id, path=path)
    contract = operator_store.get_contract(
        operation["operator_id"],
        version=operation["contract_version"],
        path=path,
    )
    mode = contract.data["authority"][authority_action]
    con = connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        existing = con.execute(
            "SELECT * FROM operator_action_intents WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if existing is not None:
            con.commit()
            return _action_dict(existing)
        timestamp = operator_store.now()
        state = "authorized" if mode == "auto" else ("waiting" if mode == "ask" else "denied")
        cursor = con.execute(
            "INSERT INTO operator_action_intents("
            "operation_id, attempt_id, work_item_id, idempotency_key, kind, "
            "authority_action, target_json, evidence_json, state, created_at, updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                operation["id"],
                attempt_id,
                work_item_id,
                _bounded_text(idempotency_key, 240),
                _bounded_text(kind, 80),
                authority_action,
                _json(target),
                _json(evidence),
                state,
                timestamp,
                timestamp,
            ),
        )
        action_id = int(cursor.lastrowid)
        if mode == "ask":
            create_key = hashlib.sha256(
                f"action:{operation['id']}:{idempotency_key}".encode()
            ).hexdigest()
            # Insert in this transaction instead of recursively opening the DB.
            decision_id = _new_id("od")
            con.execute(
                "INSERT INTO operator_decisions("
                "id, operation_id, work_item_id, idempotency_key, question, why_now, "
                "options_json, recommendation, evidence_json, blocking_scope_json, "
                "state, created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    decision_id,
                    operation["id"],
                    work_item_id,
                    create_key,
                    f"Approve Operator action: {kind}?",
                    f"The active contract marks {authority_action} as ask.",
                    _json([
                        {"id": "approve", "label": "Approve this bounded action"},
                        {"id": "deny", "label": "Do not perform it"},
                    ]),
                    "Approve only if the target and evidence match the intended scope.",
                    _json(evidence),
                    _json({"action_id": action_id, "work_item_id": work_item_id}),
                    "open",
                    timestamp,
                ),
            )
            if work_item_id:
                con.execute(
                    "UPDATE operator_work_items SET state='needs_decision', updated_at=? "
                    "WHERE id=? AND operation_id=?",
                    (timestamp, work_item_id, operation["id"]),
                )
        _event(
            con,
            operation["id"],
            f"action_{state}",
            str(action_id),
            {
                "kind": kind,
                "authority_action": authority_action,
                "mode": mode,
            },
            timestamp,
        )
        con.commit()
        row = con.execute(
            "SELECT * FROM operator_action_intents WHERE id=?",
            (action_id,),
        ).fetchone()
        return _action_dict(row)
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def claim_action(
    action_id: int,
    *,
    path: Path | None = None,
) -> dict[str, Any] | None:
    con = connect(path)
    try:
        timestamp = operator_store.now()
        changed = con.execute(
            "UPDATE operator_action_intents SET state='applying', updated_at=? "
            "WHERE id=? AND state='authorized'",
            (timestamp, action_id),
        )
        con.commit()
        if changed.rowcount != 1:
            return None
        return _action_dict(
            con.execute(
                "SELECT * FROM operator_action_intents WHERE id=?",
                (action_id,),
            ).fetchone()
        )
    finally:
        con.close()


def finish_action(
    action_id: int,
    *,
    state: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
    path: Path | None = None,
) -> None:
    if state not in {"applied", "failed", "waiting"}:
        raise RuntimeError("action result state must be applied, failed, or waiting")
    con = connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        action = con.execute(
            "SELECT * FROM operator_action_intents WHERE id=?",
            (action_id,),
        ).fetchone()
        if action is None:
            raise RuntimeError(f"unknown action {action_id}")
        if action["state"] not in {"applying", "authorized"}:
            if action["state"] == state:
                con.commit()
                return
            raise RuntimeError(
                f"action {action_id} in {action['state']} cannot finish as {state}"
            )
        timestamp = operator_store.now()
        con.execute(
            "UPDATE operator_action_intents SET state=?, result_json=?, error=?, "
            "updated_at=? WHERE id=?",
            (
                state,
                _json(result) if result is not None else None,
                _bounded_text(error, 4096) if error else None,
                timestamp,
                action_id,
            ),
        )
        _event(
            con,
            action["operation_id"],
            f"action_{state}",
            str(action_id),
            {"error": _bounded_text(error, 512) if error else None},
            timestamp,
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def pending_actions(
    operation_id: str,
    *,
    path: Path | None = None,
) -> list[dict[str, Any]]:
    con = connect(path)
    try:
        operation = _resolve_operation(con, operation_id)
        return [
            _action_dict(row)
            for row in con.execute(
                "SELECT * FROM operator_action_intents "
                "WHERE operation_id=? AND state='authorized' ORDER BY id",
                (operation["id"],),
            )
        ]
    finally:
        con.close()


def acquire_lease(
    operation_id: str,
    *,
    holder: str,
    lease_seconds: int = LEASE_SECONDS,
    now_epoch: float | None = None,
    path: Path | None = None,
) -> Lease:
    if not 5 <= lease_seconds <= 300:
        raise RuntimeError("controller lease must be between 5 and 300 seconds")
    instant = time.time() if now_epoch is None else now_epoch
    con = connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        operation = _resolve_operation(con, operation_id)
        if operation["state"] in OPERATION_TERMINAL:
            raise RuntimeError(f"operation {operation['id']} is {operation['state']}")
        existing = con.execute(
            "SELECT * FROM controller_leases WHERE operation_id=?",
            (operation["id"],),
        ).fetchone()
        if (
            existing is not None
            and existing["holder"] != holder
            and float(existing["expires_at_epoch"]) > instant
        ):
            raise LeaseBusyError(
                f"operation {operation['id']} is leased by {existing['holder']}"
            )
        generation = 1 if existing is None else int(existing["generation"]) + 1
        timestamp = operator_store.now()
        expires = instant + lease_seconds
        con.execute(
            "INSERT INTO controller_leases("
            "operation_id, holder, generation, acquired_at, heartbeat_at, expires_at_epoch"
            ") VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(operation_id) DO UPDATE SET "
            "holder=excluded.holder, generation=excluded.generation, "
            "acquired_at=excluded.acquired_at, heartbeat_at=excluded.heartbeat_at, "
            "expires_at_epoch=excluded.expires_at_epoch",
            (
                operation["id"],
                _bounded_text(holder, 240),
                generation,
                timestamp,
                timestamp,
                expires,
            ),
        )
        _event(
            con,
            operation["id"],
            "controller_lease_acquired",
            holder,
            {"generation": generation, "expires_at_epoch": expires},
            timestamp,
        )
        con.commit()
        return Lease(operation["id"], holder, generation, expires)
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def heartbeat_lease(
    lease: Lease,
    *,
    lease_seconds: int = LEASE_SECONDS,
    now_epoch: float | None = None,
    path: Path | None = None,
) -> Lease:
    instant = time.time() if now_epoch is None else now_epoch
    con = connect(path)
    try:
        timestamp = operator_store.now()
        expires = instant + lease_seconds
        changed = con.execute(
            "UPDATE controller_leases SET heartbeat_at=?, expires_at_epoch=? "
            "WHERE operation_id=? AND holder=? AND generation=? "
            "AND expires_at_epoch>=?",
            (
                timestamp,
                expires,
                lease.operation_id,
                lease.holder,
                lease.generation,
                instant,
            ),
        )
        con.commit()
        if changed.rowcount != 1:
            raise LeaseLostError(
                f"controller lease for {lease.operation_id} was lost or expired"
            )
        return Lease(lease.operation_id, lease.holder, lease.generation, expires)
    finally:
        con.close()


def release_lease(lease: Lease, *, path: Path | None = None) -> None:
    con = connect(path)
    try:
        con.execute(
            "DELETE FROM controller_leases "
            "WHERE operation_id=? AND holder=? AND generation=?",
            (lease.operation_id, lease.holder, lease.generation),
        )
        con.commit()
    finally:
        con.close()


def acquire_project_lease(
    operation_id: str,
    project_key: str,
    *,
    holder: str,
    purpose: str,
    lease_seconds: int = 300,
    now_epoch: float | None = None,
    path: Path | None = None,
) -> Lease:
    if not 10 <= lease_seconds <= 3600:
        raise RuntimeError("project lease must be between 10 and 3600 seconds")
    instant = time.time() if now_epoch is None else now_epoch
    con = connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        operation = _resolve_operation(con, operation_id)
        if not con.execute(
            "SELECT 1 FROM operation_projects WHERE operation_id=? AND project_key=?",
            (operation["id"], project_key),
        ).fetchone():
            raise RuntimeError("project lease target is outside operation scope")
        current = con.execute(
            "SELECT * FROM operator_project_leases WHERE project_key=?",
            (project_key,),
        ).fetchone()
        if current and float(current["expires_at_epoch"]) > instant:
            raise LeaseBusyError(
                f"project {project_key} is leased by operation {current['operation_id']} "
                f"for {current['purpose']}"
            )
        generation = int(current["generation"]) + 1 if current else 1
        timestamp = operator_store.now()
        con.execute(
            "INSERT INTO operator_project_leases("
            "project_key, operation_id, holder, generation, purpose, acquired_at, "
            "expires_at_epoch"
            ") VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(project_key) DO UPDATE SET "
            "operation_id=excluded.operation_id, holder=excluded.holder, "
            "generation=excluded.generation, purpose=excluded.purpose, "
            "acquired_at=excluded.acquired_at, expires_at_epoch=excluded.expires_at_epoch",
            (
                project_key,
                operation["id"],
                _bounded_text(holder, 240),
                generation,
                _bounded_text(purpose, 160),
                timestamp,
                instant + lease_seconds,
            ),
        )
        con.commit()
        return Lease(operation["id"], holder, generation, instant + lease_seconds)
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def release_project_lease(
    project_key: str,
    lease: Lease,
    *,
    path: Path | None = None,
) -> None:
    con = connect(path)
    try:
        con.execute(
            "DELETE FROM operator_project_leases WHERE project_key=? "
            "AND operation_id=? AND holder=? AND generation=?",
            (project_key, lease.operation_id, lease.holder, lease.generation),
        )
        con.commit()
    finally:
        con.close()


def heartbeat_project_lease(
    project_key: str,
    lease: Lease,
    *,
    lease_seconds: int = 300,
    now_epoch: float | None = None,
    path: Path | None = None,
) -> Lease:
    if not 10 <= lease_seconds <= 3600:
        raise RuntimeError("project lease must be between 10 and 3600 seconds")
    instant = time.time() if now_epoch is None else now_epoch
    con = connect(path)
    try:
        expires = instant + lease_seconds
        changed = con.execute(
            "UPDATE operator_project_leases SET expires_at_epoch=? "
            "WHERE project_key=? AND operation_id=? AND holder=? AND generation=? "
            "AND expires_at_epoch>=?",
            (
                expires,
                project_key,
                lease.operation_id,
                lease.holder,
                lease.generation,
                instant,
            ),
        )
        con.commit()
        if changed.rowcount != 1:
            raise LeaseLostError(f"project lease for {project_key} was lost or expired")
        return Lease(lease.operation_id, lease.holder, lease.generation, expires)
    finally:
        con.close()


def begin_attempt(
    lease: Lease,
    snapshot: dict[str, Any],
    *,
    path: Path | None = None,
) -> int:
    payload = _json(snapshot)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    con = connect(path)
    try:
        _assert_lease(con, lease)
        cursor = con.execute(
            "INSERT INTO operator_attempts("
            "operation_id, generation, snapshot_sha256, snapshot_json, started_at"
            ") VALUES(?,?,?,?,?)",
            (
                lease.operation_id,
                lease.generation,
                digest,
                payload,
                operator_store.now(),
            ),
        )
        con.commit()
        return int(cursor.lastrowid)
    finally:
        con.close()


def finish_attempt(
    attempt_id: int,
    *,
    outcome: str,
    summary: str,
    path: Path | None = None,
) -> None:
    con = connect(path)
    try:
        changed = con.execute(
            "UPDATE operator_attempts SET finished_at=?, outcome=?, summary=? "
            "WHERE id=? AND finished_at IS NULL",
            (
                operator_store.now(),
                _bounded_text(outcome, 80),
                _bounded_text(summary, 4096),
                attempt_id,
            ),
        )
        con.commit()
        if changed.rowcount != 1:
            raise RuntimeError(f"attempt {attempt_id} is missing or already finished")
    finally:
        con.close()


def schedule_wakeup(
    operation_id: str,
    *,
    kind: str,
    reason: str,
    event_key: str | None = None,
    due_at_epoch: float | None = None,
    path: Path | None = None,
) -> int:
    if event_key is None and due_at_epoch is None:
        raise RuntimeError("wakeup requires an event key or due time")
    con = connect(path)
    try:
        operation = _resolve_operation(con, operation_id)
        cursor = con.execute(
            "INSERT OR IGNORE INTO operator_wakeups("
            "operation_id, kind, event_key, due_at_epoch, state, reason, created_at"
            ") VALUES(?,?,?,?, 'scheduled', ?, ?)",
            (
                operation["id"],
                _bounded_text(kind, 80),
                _bounded_text(event_key, 240) if event_key else None,
                due_at_epoch,
                _bounded_text(reason, 4096),
                operator_store.now(),
            ),
        )
        con.commit()
        if cursor.lastrowid:
            return int(cursor.lastrowid)
        existing = con.execute(
            "SELECT id FROM operator_wakeups WHERE operation_id=? AND kind=? "
            "AND event_key IS ? AND due_at_epoch IS ? AND state='scheduled'",
            (operation["id"], kind, event_key, due_at_epoch),
        ).fetchone()
        return int(existing["id"])
    finally:
        con.close()


def fire_wakeups(
    operation_id: str,
    *,
    event_keys: Iterable[str] = (),
    now_epoch: float | None = None,
    path: Path | None = None,
) -> list[int]:
    instant = time.time() if now_epoch is None else now_epoch
    keys = list(dict.fromkeys(event_keys))
    con = connect(path)
    try:
        operation = _resolve_operation(con, operation_id)
        rows = list(
            con.execute(
                "SELECT id, event_key, due_at_epoch FROM operator_wakeups "
                "WHERE operation_id=? AND state='scheduled'",
                (operation["id"],),
            )
        )
        fired = [
            int(row["id"])
            for row in rows
            if (
                row["due_at_epoch"] is not None
                and float(row["due_at_epoch"]) <= instant
            )
            or (row["event_key"] is not None and row["event_key"] in keys)
        ]
        if fired:
            timestamp = operator_store.now()
            con.execute(
                "UPDATE operator_wakeups SET state='fired', fired_at=? "
                "WHERE id IN (" + ",".join("?" for _ in fired) + ")",
                [timestamp, *fired],
            )
            con.commit()
        return fired
    finally:
        con.close()


def record_observation(
    operation_id: str,
    *,
    kind: str,
    subject: str,
    payload: dict[str, Any],
    path: Path | None = None,
) -> int:
    con = connect(path)
    try:
        operation = _resolve_operation(con, operation_id)
        cursor = con.execute(
            "INSERT INTO operator_observations("
            "operation_id, kind, subject, payload_json, observed_at"
            ") VALUES(?,?,?,?,?)",
            (
                operation["id"],
                _bounded_text(kind, 80),
                _bounded_text(subject, 240),
                _json(payload),
                operator_store.now(),
            ),
        )
        con.commit()
        return int(cursor.lastrowid)
    finally:
        con.close()


def update_event_cursor(
    operation_id: str,
    project_key: str,
    *,
    max_run_id: int | None,
    max_message_id: int | None,
    max_feed_id: int | None,
    path: Path | None = None,
) -> None:
    con = connect(path)
    try:
        con.execute(
            "INSERT INTO operator_event_cursors("
            "operation_id, project_key, max_run_id, max_message_id, max_feed_id, observed_at"
            ") VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(operation_id, project_key) DO UPDATE SET "
            "max_run_id=MAX(COALESCE(max_run_id,0),COALESCE(excluded.max_run_id,0)), "
            "max_message_id=MAX(COALESCE(max_message_id,0),COALESCE(excluded.max_message_id,0)), "
            "max_feed_id=MAX(COALESCE(max_feed_id,0),COALESCE(excluded.max_feed_id,0)), "
            "observed_at=excluded.observed_at",
            (
                operation_id,
                project_key,
                max_run_id,
                max_message_id,
                max_feed_id,
                operator_store.now(),
            ),
        )
        con.commit()
    finally:
        con.close()


def register_resource_lease(
    operation_id: str,
    *,
    work_item_id: str | None,
    project_key: str,
    kind: str,
    resource_path: str,
    project_run_id: int | None,
    measured_bytes: int | None = None,
    unique_state: bool = True,
    details: dict[str, Any] | None = None,
    path: Path | None = None,
) -> str:
    con = connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        operation = _resolve_operation(con, operation_id)
        if not con.execute(
            "SELECT 1 FROM operation_projects WHERE operation_id=? AND project_key=?",
            (operation["id"], project_key),
        ).fetchone():
            raise RuntimeError("resource project is outside operation scope")
        existing = con.execute(
            "SELECT id FROM operator_resource_leases "
            "WHERE operation_id=? AND kind=? AND resource_path=?",
            (operation["id"], kind, resource_path),
        ).fetchone()
        if existing:
            con.commit()
            return existing["id"]
        resource_id = _new_id("orl")
        timestamp = operator_store.now()
        con.execute(
            "INSERT INTO operator_resource_leases("
            "id, operation_id, work_item_id, project_key, kind, resource_path, "
            "project_run_id, state, measured_bytes, unique_state, details_json, "
            "created_at, observed_at"
            ") VALUES(?,?,?,?,?,?,?,'active',?,?,?,?,?)",
            (
                resource_id,
                operation["id"],
                work_item_id,
                project_key,
                _bounded_text(kind, 80),
                _bounded_text(resource_path, 4096),
                project_run_id,
                measured_bytes,
                int(unique_state),
                _json(details or {}),
                timestamp,
                timestamp,
            ),
        )
        _event(
            con,
            operation["id"],
            "resource_leased",
            resource_id,
            {"kind": kind, "path": resource_path, "run_id": project_run_id},
            timestamp,
        )
        con.commit()
        return resource_id
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def release_resource_lease(
    resource_id: str,
    *,
    state: str,
    unique_state: bool,
    details: dict[str, Any],
    path: Path | None = None,
) -> None:
    if state not in {"released", "retained", "lost"}:
        raise RuntimeError("resource terminal state must be released, retained, or lost")
    if state == "released" and unique_state:
        raise RuntimeError("cannot release a resource that still contains unique state")
    con = connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM operator_resource_leases WHERE id=?", (resource_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError(f"unknown resource lease {resource_id!r}")
        if row["state"] != "active":
            if row["state"] == state:
                con.commit()
                return
            raise RuntimeError(f"resource lease {resource_id} is already {row['state']}")
        timestamp = operator_store.now()
        con.execute(
            "UPDATE operator_resource_leases SET state=?, unique_state=?, "
            "details_json=?, observed_at=?, released_at=? WHERE id=?",
            (
                state,
                int(unique_state),
                _json(details),
                timestamp,
                timestamp if state == "released" else None,
                resource_id,
            ),
        )
        _event(
            con,
            row["operation_id"],
            f"resource_{state}",
            resource_id,
            {"unique_state": unique_state, **details},
            timestamp,
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def resource_leases(
    operation_id: str,
    *,
    active_only: bool = False,
    path: Path | None = None,
) -> list[dict[str, Any]]:
    con = connect(path)
    try:
        operation = _resolve_operation(con, operation_id)
        query = "SELECT * FROM operator_resource_leases WHERE operation_id=?"
        params: list[Any] = [operation["id"]]
        if active_only:
            query += " AND state='active'"
        query += " ORDER BY created_at, id"
        rows = []
        for row in con.execute(query, params):
            item = dict(row)
            item["unique_state"] = bool(item["unique_state"])
            item["details"] = json.loads(item.pop("details_json"))
            rows.append(item)
        return rows
    finally:
        con.close()


def _sync_logical_project(
    con: sqlite3.Connection,
    row: dict[str, Any],
    timestamp: str,
) -> str:
    existing = con.execute(
        "SELECT id FROM logical_projects WHERE registry_id=?",
        (row["id"],),
    ).fetchone()
    if existing is not None:
        return existing["id"]
    project_key = _new_id("prj")
    con.execute(
        "INSERT INTO logical_projects("
        "id, registry_id, name, root, created_at"
        ") VALUES(?,?,?,?,?)",
        (
            project_key,
            row["id"],
            _bounded_text(str(row.get("name") or row["id"]), 160),
            _bounded_text(str(row["root"]), 4096),
            timestamp,
        ),
    )
    return project_key


def _insert_initial_work(
    con: sqlite3.Connection,
    *,
    operation_id: str,
    goal_id: int,
    project_key: str,
    goal: dict[str, Any],
    contract: dict[str, Any],
    timestamp: str,
) -> str:
    outcome = goal["outcome"]
    lowered = outcome.casefold()
    if any(word in lowered for word in ("architecture", "redesign", "integration")):
        task_class = "architecture"
    elif any(word in lowered for word in ("rename", "format", "mechanical", "sweep")):
        task_class = "mechanical"
    else:
        task_class = "feature"
    minimum_tier = contract["routing"]["minimum_tier"].get(
        task_class,
        "generalist",
    )
    actuation_mode = (
        "bounded_patch" if task_class == "architecture" else "general_implementation"
    )
    requires_review = (
        bool(goal["requires_review"])
        if operator_contract.is_v2(contract)
        else task_class == "architecture"
        or any(word in lowered for word in ("security", "release", "migration"))
    )
    work_id = _new_id("ow")
    project_id = goal.get("project_id")
    include, exclude = (
        operator_contract.project_scope(contract, project_id)
        if project_id
        else (contract["scope"]["include"], contract["scope"]["exclude"])
    )
    con.execute(
        "INSERT INTO operator_work_items("
        "id, operation_id, goal_id, project_key, title, description, state, "
        "task_class, minimum_tier, actuation_mode, risk, requirements_json, "
        "dependencies_json, change_budget_json, requires_review, created_at, updated_at"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            work_id,
            operation_id,
            goal_id,
            project_key,
            _bounded_text(outcome, 240),
            _bounded_text(outcome, 16_384),
            "ready",
            task_class,
            minimum_tier,
            actuation_mode,
            "high" if requires_review else "normal",
            _json({
                "goal_key": goal["id"],
                "scope": {"include": include, "exclude": exclude},
                "read_dependencies": goal.get("read_dependencies", []),
                "quality_gates": contract["quality"]["gates"],
            }),
            "[]",
            _json(contract["quality"]["change_budget"]),
            int(requires_review),
            timestamp,
            timestamp,
        ),
    )
    return work_id


def dependencies_satisfied(work: dict[str, Any], *, path: Path | None = None) -> bool:
    dependencies = list(work.get("dependencies") or [])
    if not dependencies:
        return True
    con = connect(path)
    try:
        rows = list(
            con.execute(
                "SELECT id, state FROM operator_work_items WHERE id IN ("
                + ",".join("?" for _ in dependencies)
                + ")",
                dependencies,
            )
        )
        return (
            len(rows) == len(dependencies)
            and all(row["state"] == "accepted" for row in rows)
        )
    finally:
        con.close()


def assert_live_operation_safe(operation: dict[str, Any]) -> None:
    if operation["mode"] != "live":
        return
    for project in operation["projects"]:
        actual_head = _live_project_preflight(
            Path(project["root"]),
            expected_branch=project["integration_branch"],
        )
        expected_head = project.get("expected_head")
        if not expected_head:
            raise RuntimeError(
                f"live project has no activation HEAD baseline: {project['root']}"
            )
        if actual_head != expected_head:
            raise RuntimeError(
                f"live integration checkout HEAD drifted for {project['root']}: "
                f"expected {expected_head}, found {actual_head}"
            )


def update_project_expected_head(
    operation_id: str,
    project_key: str,
    head: str,
    *,
    path: Path | None = None,
) -> None:
    con = connect(path)
    try:
        cursor = con.execute(
            "UPDATE operation_projects SET expected_head=? "
            "WHERE operation_id=? AND project_key=?",
            (_bounded_text(head, 128), operation_id, project_key),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("operation project baseline could not be advanced")
        con.commit()
    finally:
        con.close()


def _live_project_preflight(root: Path, *, expected_branch: str) -> str:
    root = root.resolve()
    if not root.is_dir():
        raise RuntimeError(f"live project root does not exist: {root}")
    try:
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        branch = subprocess.run(
            ["git", "-C", str(root), "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"cannot inspect live project {root}: {exc}") from exc
    if status.returncode != 0 or branch.returncode != 0:
        detail = (status.stderr or branch.stderr).strip()
        raise RuntimeError(f"live project is not a readable Git checkout: {root}: {detail}")
    if status.stdout.strip():
        raise RuntimeError(f"live integration checkout is dirty: {root}")
    current = branch.stdout.strip()
    if current != expected_branch:
        raise RuntimeError(
            f"live integration checkout {root} is on {current!r}, "
            f"expected {expected_branch!r}"
        )
    worktrees = root / ".orchestra" / "worktrees"
    if worktrees.is_symlink():
        raise RuntimeError(
            f"unsafe symlink used as Operator worktree namespace: {worktrees}"
        )
    if worktrees.is_dir():
        for entry in worktrees.iterdir():
            if entry.is_symlink():
                raise RuntimeError(
                    f"unsafe symlink in Operator worktree namespace: {entry}"
                )
    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"cannot resolve live integration HEAD for {root}: {exc}") from exc
    if head.returncode != 0:
        raise RuntimeError(f"cannot resolve live integration HEAD for {root}")
    return head.stdout.strip()


def _maybe_accept_goal(
    con: sqlite3.Connection,
    goal_id: int,
    timestamp: str,
) -> None:
    remaining = con.execute(
        "SELECT COUNT(*) AS n FROM operator_work_items "
        "WHERE goal_id=? AND state NOT IN ('accepted','superseded','cancelled')",
        (goal_id,),
    ).fetchone()["n"]
    if int(remaining) == 0:
        evidence = [
            row["verification_json"]
            for row in con.execute(
                "SELECT verification_json FROM operator_work_items "
                "WHERE goal_id=? AND state='accepted' ORDER BY created_at",
                (goal_id,),
            )
            if row["verification_json"]
        ]
        con.execute(
            "UPDATE operator_goals SET state='accepted', acceptance_evidence_json=?, "
            "updated_at=? WHERE id=?",
            (_json(evidence), timestamp, goal_id),
        )


def _resolve_operation(con: sqlite3.Connection, identifier: str) -> sqlite3.Row:
    row = con.execute("SELECT * FROM operations WHERE id=?", (identifier,)).fetchone()
    if row is not None:
        return row
    rows = con.execute(
        "SELECT o.* FROM operations o JOIN operators p ON p.id=o.operator_id "
        "WHERE p.id=? OR p.name=? COLLATE NOCASE "
        "ORDER BY CASE WHEN o.state NOT IN ('achieved','stopped','failed') "
        "THEN 0 ELSE 1 END, o.created_at DESC LIMIT 2",
        (identifier, identifier),
    ).fetchall()
    if not rows:
        raise RuntimeError(f"no operation matches {identifier!r}")
    if len(rows) > 1 and rows[0]["state"] in OPERATION_TERMINAL:
        raise RuntimeError(
            f"multiple historical operations match {identifier!r}; use an operation id"
        )
    return rows[0]


def _operation_dict(con: sqlite3.Connection, operation: sqlite3.Row) -> dict[str, Any]:
    goal_rows = list(
        con.execute(
            "SELECT goal_key, outcome, priority, state, acceptance_evidence_json "
            "FROM operator_goals WHERE operation_id=? ORDER BY priority, goal_key",
            (operation["id"],),
        )
    )
    work_counts = {
        row["state"]: int(row["n"])
        for row in con.execute(
            "SELECT state, COUNT(*) AS n FROM operator_work_items "
            "WHERE operation_id=? GROUP BY state",
            (operation["id"],),
        )
    }
    open_decisions = int(
        con.execute(
            "SELECT COUNT(*) AS n FROM operator_decisions "
            "WHERE operation_id=? AND state='open'",
            (operation["id"],),
        ).fetchone()["n"]
    )
    active_actions = int(
        con.execute(
            "SELECT COUNT(*) AS n FROM operator_action_intents "
            "WHERE operation_id=? AND state IN ('authorized','applying')",
            (operation["id"],),
        ).fetchone()["n"]
    )
    projects = [
        {
            "project_key": row["project_key"],
            "project_id": row["contract_project_id"],
            "name": row["name"],
            "root": row["root"],
            "target_branch": row["target_branch"],
            "integration_branch": row["integration_branch"],
            "expected_head": row["expected_head"],
        }
        for row in con.execute(
            "SELECT * FROM operation_projects WHERE operation_id=? "
            "ORDER BY contract_project_id",
            (operation["id"],),
        )
    ]
    state = operation["state"]
    if state == "active" and open_decisions:
        state = "needs_decision"
    return {
        "id": operation["id"],
        "operator_id": operation["operator_id"],
        "contract_version": int(operation["contract_version"]),
        "mode": operation["mode"],
        "state": state,
        "stored_state": operation["state"],
        "priority": int(operation["priority"]),
        "controller_pid": operation["controller_pid"],
        "created_at": operation["created_at"],
        "activated_at": operation["activated_at"],
        "updated_at": operation["updated_at"],
        "stopped_at": operation["stopped_at"],
        "last_error": operation["last_error"],
        "goals": [
            {
                "id": row["goal_key"],
                "outcome": row["outcome"],
                "priority": int(row["priority"]),
                "state": row["state"],
                "acceptance_evidence": (
                    json.loads(row["acceptance_evidence_json"])
                    if row["acceptance_evidence_json"]
                    else None
                ),
            }
            for row in goal_rows
        ],
        "work_counts": work_counts,
        "open_decisions": open_decisions,
        "active_actions": active_actions,
        "projects": projects,
    }


def _work_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for key in (
        "requirements_json",
        "dependencies_json",
        "change_budget_json",
        "handoff_json",
        "complexity_json",
        "verification_json",
    ):
        target = key.removesuffix("_json")
        raw = data.pop(key)
        data[target] = json.loads(raw) if raw else None
    data["requires_review"] = bool(data["requires_review"])
    return data


def _decision_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for key in ("options_json", "evidence_json", "blocking_scope_json"):
        data[key.removesuffix("_json")] = json.loads(data.pop(key))
    return data


def _action_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for key in ("target_json", "evidence_json", "result_json"):
        raw = data.pop(key)
        data[key.removesuffix("_json")] = json.loads(raw) if raw else None
    return data


def _assert_lease(con: sqlite3.Connection, lease: Lease) -> None:
    row = con.execute(
        "SELECT holder, generation, expires_at_epoch FROM controller_leases "
        "WHERE operation_id=?",
        (lease.operation_id,),
    ).fetchone()
    if (
        row is None
        or row["holder"] != lease.holder
        or int(row["generation"]) != lease.generation
        or float(row["expires_at_epoch"]) < time.time()
    ):
        raise LeaseLostError(f"controller lease for {lease.operation_id} is not current")


def _event(
    con: sqlite3.Connection,
    operation_id: str,
    kind: str,
    subject: str | None,
    details: dict[str, Any],
    timestamp: str,
) -> None:
    con.execute(
        "INSERT INTO operator_runtime_events("
        "operation_id, kind, subject, details_json, created_at"
        ") VALUES(?,?,?,?,?)",
        (
            operation_id,
            _bounded_text(kind, 80),
            _bounded_text(subject, 240) if subject else None,
            _json(details),
            timestamp,
        ),
    )


def _json(value: Any) -> str:
    try:
        payload = json.dumps(
            _sanitize(value),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"runtime value is not safe JSON: {exc}") from exc
    size = len(payload.encode("utf-8"))
    if size > MAX_RUNTIME_JSON_BYTES:
        raise RuntimeError(
            f"runtime JSON is {size} bytes; limit is {MAX_RUNTIME_JSON_BYTES}"
        )
    return payload


def safe_json(value: Any) -> str:
    """Serialize bounded, credential-redacted runtime state."""
    return _json(value)


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            _bounded_text(str(key), 160): _sanitize(child)
            for key, child in list(value.items())[:256]
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize(child) for child in list(value)[:256]]
    if isinstance(value, str):
        return _bounded_text(value, 16_384)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _bounded_text(str(value), 4096)


def _bounded_text(value: str | None, limit: int) -> str:
    text = checkpoint.redact_sensitive_text(
        "" if value is None else str(value)
    ) or ""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "\u2026"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"
