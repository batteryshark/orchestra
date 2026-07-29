"""Private, durable control-plane storage for Operator contracts.

Project databases remain authoritative for project-local runs and messages.
This database is user-level state: it records immutable contract versions,
hash-bound approvals, project bindings at draft time, and an append-only audit
trail. Runtime operation state is layered onto the same private database by
``operator_runtime`` and remains separate from project-local run databases.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from orchestra_cli import operator_contract, paths

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS operators (
  id TEXT PRIMARY KEY,
  name TEXT COLLATE NOCASE UNIQUE NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contract_versions (
  operator_id TEXT NOT NULL REFERENCES operators(id),
  version INTEGER NOT NULL CHECK(version > 0),
  schema TEXT NOT NULL,
  content_json TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(operator_id, version),
  UNIQUE(operator_id, version, content_sha256)
);

CREATE TABLE IF NOT EXISTS contract_projects (
  operator_id TEXT NOT NULL,
  contract_version INTEGER NOT NULL,
  project_id TEXT NOT NULL,
  project_name TEXT NOT NULL,
  project_root TEXT NOT NULL,
  available INTEGER NOT NULL CHECK(available IN (0, 1)),
  PRIMARY KEY(operator_id, contract_version, project_id),
  FOREIGN KEY(operator_id, contract_version)
    REFERENCES contract_versions(operator_id, version)
);

CREATE TABLE IF NOT EXISTS contract_approvals (
  operator_id TEXT NOT NULL,
  contract_version INTEGER NOT NULL,
  content_sha256 TEXT NOT NULL,
  approved_at TEXT NOT NULL,
  approved_by TEXT NOT NULL,
  PRIMARY KEY(operator_id, contract_version),
  FOREIGN KEY(operator_id, contract_version, content_sha256)
    REFERENCES contract_versions(operator_id, version, content_sha256)
);

CREATE TABLE IF NOT EXISTS operator_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  operator_id TEXT NOT NULL REFERENCES operators(id),
  kind TEXT NOT NULL,
  contract_version INTEGER,
  details_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_contract_versions_latest
  ON contract_versions(operator_id, version DESC);
CREATE INDEX IF NOT EXISTS idx_operator_events_operator
  ON operator_events(operator_id, id);

CREATE TRIGGER IF NOT EXISTS immutable_contract_versions_update
BEFORE UPDATE ON contract_versions
BEGIN
  SELECT RAISE(ABORT, 'contract versions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS immutable_contract_versions_delete
BEFORE DELETE ON contract_versions
BEGIN
  SELECT RAISE(ABORT, 'contract versions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS immutable_contract_projects_update
BEFORE UPDATE ON contract_projects
BEGIN
  SELECT RAISE(ABORT, 'contract project snapshots are immutable');
END;

CREATE TRIGGER IF NOT EXISTS immutable_contract_projects_delete
BEFORE DELETE ON contract_projects
BEGIN
  SELECT RAISE(ABORT, 'contract project snapshots are immutable');
END;

CREATE TRIGGER IF NOT EXISTS immutable_contract_approvals_update
BEFORE UPDATE ON contract_approvals
BEGIN
  SELECT RAISE(ABORT, 'contract approvals are immutable');
END;

CREATE TRIGGER IF NOT EXISTS immutable_contract_approvals_delete
BEFORE DELETE ON contract_approvals
BEGIN
  SELECT RAISE(ABORT, 'contract approvals are immutable');
END;

CREATE TRIGGER IF NOT EXISTS immutable_operator_events_update
BEFORE UPDATE ON operator_events
BEGIN
  SELECT RAISE(ABORT, 'operator events are immutable');
END;

CREATE TRIGGER IF NOT EXISTS immutable_operator_events_delete
BEFORE DELETE ON operator_events
BEGIN
  SELECT RAISE(ABORT, 'operator events are immutable');
END;
"""


class OperatorStoreError(RuntimeError):
    """A durable Operator state operation could not be completed."""


class UnknownOperatorError(OperatorStoreError):
    pass


class ApprovalError(OperatorStoreError):
    pass


class CorruptOperatorStoreError(OperatorStoreError):
    pass


@dataclass(frozen=True)
class DraftResult:
    operator_id: str
    name: str
    version: int
    sha256: str
    created: bool
    changed_paths: tuple[str, ...]


@dataclass(frozen=True)
class ApprovalResult:
    operator_id: str
    name: str
    version: int
    sha256: str
    approved_at: str
    approved_by: str
    created: bool


def operator_db_path() -> Path:
    override = os.environ.get("ORCHESTRA_OPERATOR_DB")
    if override:
        return Path(override).expanduser()
    return paths.global_config_path().parent / "operator.db"


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: Path | None = None) -> sqlite3.Connection:
    db_file = Path(path or operator_db_path()).expanduser()
    _prepare_private_file(db_file)
    con = sqlite3.connect(db_file, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 10000")
    con.execute("PRAGMA journal_mode = WAL")
    con.executescript(SCHEMA)
    return con


def save_draft(
    contract: operator_contract.ValidatedContract,
    registered_projects: Iterable[dict[str, Any]],
    *,
    path: Path | None = None,
) -> DraftResult:
    """Store an immutable next contract version.

    The project registry snapshot is required here, not only in the CLI, so a
    caller cannot persist an authority contract for an arbitrary path.  Saving
    identical canonical content is idempotent and returns the existing latest
    version.
    """
    # Re-parse canonical bytes at the storage boundary.  ValidatedContract is
    # intentionally a small value object, not a recursively frozen mapping;
    # this prevents a programmatic caller from mutating ``data`` after
    # validation and smuggling content that differs from the approved hash.
    verified = operator_contract.parse_contract(
        contract.canonical_json,
        source="contract storage boundary",
    )
    if verified.sha256 != contract.sha256:
        raise OperatorStoreError("validated contract hash does not match its canonical bytes")
    contract = verified
    snapshots = _project_snapshots(contract, registered_projects)
    name = contract.data["name"]
    con = connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        operator = con.execute(
            "SELECT id, name FROM operators WHERE name=? COLLATE NOCASE",
            (name,),
        ).fetchone()
        if operator is None:
            operator_id = _insert_operator(con, name)
            stored_name = name
            latest = None
        else:
            operator_id = operator["id"]
            stored_name = operator["name"]
            latest = con.execute(
                "SELECT version, content_json, content_sha256 "
                "FROM contract_versions WHERE operator_id=? "
                "ORDER BY version DESC LIMIT 1",
                (operator_id,),
            ).fetchone()

        if latest is not None and latest["content_sha256"] == contract.sha256:
            con.commit()
            return DraftResult(
                operator_id=operator_id,
                name=stored_name,
                version=int(latest["version"]),
                sha256=contract.sha256,
                created=False,
                changed_paths=(),
            )

        version = 1 if latest is None else int(latest["version"]) + 1
        changed = (
            ()
            if latest is None
            else tuple(
                _changed_paths(
                    json.loads(latest["content_json"]),
                    contract.data,
                )
            )
        )
        created_at = now()
        con.execute(
            "INSERT INTO contract_versions("
            "operator_id, version, schema, content_json, content_sha256, created_at"
            ") VALUES(?,?,?,?,?,?)",
            (
                operator_id,
                version,
                operator_contract.SCHEMA_TAG,
                contract.canonical_json,
                contract.sha256,
                created_at,
            ),
        )
        for snapshot in snapshots:
            con.execute(
                "INSERT INTO contract_projects("
                "operator_id, contract_version, project_id, project_name, "
                "project_root, available"
                ") VALUES(?,?,?,?,?,?)",
                (
                    operator_id,
                    version,
                    snapshot["id"],
                    snapshot["name"],
                    snapshot["root"],
                    int(snapshot["available"]),
                ),
            )
        _append_event(
            con,
            operator_id,
            "contract_drafted",
            version,
            {
                "sha256": contract.sha256,
                "changed_paths": list(changed),
                "projects": [row["id"] for row in snapshots],
            },
            created_at,
        )
        con.commit()
        return DraftResult(
            operator_id=operator_id,
            name=stored_name,
            version=version,
            sha256=contract.sha256,
            created=True,
            changed_paths=changed,
        )
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def approve(
    identifier: str,
    *,
    version: int,
    sha256: str,
    approved_by: str,
    path: Path | None = None,
) -> ApprovalResult:
    if len(sha256) != 64 or any(ch not in "0123456789abcdef" for ch in sha256):
        raise ApprovalError("approval hash must be a full lowercase SHA-256 digest")
    approved_by = approved_by.strip()
    if not approved_by or len(approved_by) > 160:
        raise ApprovalError("approver must contain between 1 and 160 characters")

    con = connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        operator = _resolve(con, identifier)
        latest = con.execute(
            "SELECT version, content_sha256 FROM contract_versions "
            "WHERE operator_id=? ORDER BY version DESC LIMIT 1",
            (operator["id"],),
        ).fetchone()
        if latest is None:
            raise ApprovalError(f"Operator {operator['id']} has no contract draft")
        if version != int(latest["version"]):
            raise ApprovalError(
                f"contract v{version} is not the latest draft; "
                f"review and approve v{latest['version']}"
            )
        if sha256 != latest["content_sha256"]:
            raise ApprovalError(
                "approval hash does not match the stored canonical contract"
            )
        existing = con.execute(
            "SELECT approved_at, approved_by, content_sha256 "
            "FROM contract_approvals WHERE operator_id=? AND contract_version=?",
            (operator["id"], version),
        ).fetchone()
        if existing is not None:
            if existing["content_sha256"] != sha256:
                raise CorruptOperatorStoreError(
                    "stored approval hash differs from its contract version"
                )
            con.commit()
            return ApprovalResult(
                operator_id=operator["id"],
                name=operator["name"],
                version=version,
                sha256=sha256,
                approved_at=existing["approved_at"],
                approved_by=existing["approved_by"],
                created=False,
            )

        approved_at = now()
        con.execute(
            "INSERT INTO contract_approvals("
            "operator_id, contract_version, content_sha256, approved_at, approved_by"
            ") VALUES(?,?,?,?,?)",
            (operator["id"], version, sha256, approved_at, approved_by),
        )
        _append_event(
            con,
            operator["id"],
            "contract_approved",
            version,
            {"sha256": sha256, "approved_by": approved_by},
            approved_at,
        )
        con.commit()
        return ApprovalResult(
            operator_id=operator["id"],
            name=operator["name"],
            version=version,
            sha256=sha256,
            approved_at=approved_at,
            approved_by=approved_by,
            created=True,
        )
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def get_contract(
    identifier: str,
    *,
    version: int | None = None,
    path: Path | None = None,
) -> operator_contract.ValidatedContract:
    con = connect(path)
    try:
        operator = _resolve(con, identifier)
        if version is None:
            row = con.execute(
                "SELECT version, content_json, content_sha256 "
                "FROM contract_versions WHERE operator_id=? "
                "ORDER BY version DESC LIMIT 1",
                (operator["id"],),
            ).fetchone()
        else:
            row = con.execute(
                "SELECT version, content_json, content_sha256 "
                "FROM contract_versions WHERE operator_id=? AND version=?",
                (operator["id"], version),
            ).fetchone()
        if row is None:
            suffix = "latest" if version is None else f"v{version}"
            raise OperatorStoreError(
                f"Operator {operator['id']} has no {suffix} contract"
            )
        validated = operator_contract.parse_contract(
            row["content_json"],
            source=f"Operator {operator['id']} contract v{row['version']}",
        )
        if validated.sha256 != row["content_sha256"]:
            raise CorruptOperatorStoreError(
                f"Operator {operator['id']} contract v{row['version']} "
                "does not match its stored hash"
            )
        return validated
    finally:
        con.close()


def get_status(
    identifier: str,
    *,
    path: Path | None = None,
) -> dict[str, Any]:
    con = connect(path)
    try:
        operator = _resolve(con, identifier)
        return _status_row(con, operator)
    finally:
        con.close()


def list_statuses(*, path: Path | None = None) -> list[dict[str, Any]]:
    con = connect(path)
    try:
        operators = con.execute(
            "SELECT id, name, created_at FROM operators ORDER BY name COLLATE NOCASE, id"
        ).fetchall()
        return [_status_row(con, row) for row in operators]
    finally:
        con.close()


def render_status(status: dict[str, Any]) -> str:
    authority = status["authority"]
    resources = status["resources"]
    lines = [
        f"Operator {status['id']}: {status['name']}",
        f"  state: {status['state']}",
        f"  contract: v{status['contract_version']} sha256:{status['contract_sha256']}",
        f"  projects: {', '.join(status['projects'])}",
        f"  goals: {status['accepted_goals']}/{status['goal_count']} accepted",
        (
            "  authority: "
            f"{authority['auto']} auto · {authority['ask']} ask · "
            f"{authority['deny']} deny"
        ),
        (
            "  ceilings: "
            f"{resources['max_active_runs']} active runs · "
            f"{resources['max_worktrees']} worktrees · "
            f"{resources['max_attempts_per_item']} attempts/item"
        ),
        f"  next: {status['next_action']}",
    ]
    return "\n".join(lines)


def _prepare_private_file(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o700)
    except OSError as exc:
        raise OperatorStoreError(
            f"cannot prepare Operator state directory {path.parent}: {exc}"
        ) from exc
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
    except OSError as exc:
        raise OperatorStoreError(f"cannot prepare Operator database {path}: {exc}") from exc


def _insert_operator(con: sqlite3.Connection, name: str) -> str:
    created_at = now()
    for _ in range(16):
        operator_id = f"op_{secrets.token_hex(6)}"
        try:
            con.execute(
                "INSERT INTO operators(id, name, created_at) VALUES(?,?,?)",
                (operator_id, name, created_at),
            )
            return operator_id
        except sqlite3.IntegrityError as exc:
            if "operators.id" not in str(exc):
                raise
    raise OperatorStoreError("could not allocate a unique Operator id")


def _resolve(con: sqlite3.Connection, identifier: str) -> sqlite3.Row:
    row = con.execute(
        "SELECT id, name, created_at FROM operators WHERE id=?",
        (identifier,),
    ).fetchone()
    if row is None:
        row = con.execute(
            "SELECT id, name, created_at FROM operators "
            "WHERE name=? COLLATE NOCASE",
            (identifier,),
        ).fetchone()
    if row is None:
        raise UnknownOperatorError(f"no Operator matches {identifier!r}")
    return row


def _project_snapshots(
    contract: operator_contract.ValidatedContract,
    registered_projects: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for row in registered_projects:
        project_id = row.get("id")
        name = row.get("name")
        root = row.get("root")
        if (
            isinstance(project_id, str)
            and isinstance(name, str)
            and isinstance(root, str)
        ):
            by_id[project_id] = {
                "id": project_id,
                "name": name[:160],
                "root": root[:4096],
                "available": bool(row.get("available", True)),
            }
    missing = [pid for pid in operator_contract.project_ids(contract.data) if pid not in by_id]
    if missing:
        raise OperatorStoreError(
            "contract references unregistered project ids: " + ", ".join(missing)
        )
    return [by_id[pid] for pid in operator_contract.project_ids(contract.data)]


def _append_event(
    con: sqlite3.Connection,
    operator_id: str,
    kind: str,
    version: int | None,
    details: dict[str, Any],
    created_at: str,
) -> None:
    con.execute(
        "INSERT INTO operator_events("
        "operator_id, kind, contract_version, details_json, created_at"
        ") VALUES(?,?,?,?,?)",
        (
            operator_id,
            kind,
            version,
            json.dumps(details, sort_keys=True, separators=(",", ":")),
            created_at,
        ),
    )


def _changed_paths(old: Any, new: Any, path: str = "$") -> list[str]:
    """Return a bounded semantic diff for owner review."""
    if old == new:
        return []
    if isinstance(old, dict) and isinstance(new, dict):
        out: list[str] = []
        for key in sorted(old.keys() | new.keys()):
            child = f"{path}.{key}"
            if key not in old or key not in new:
                out.append(child)
            else:
                out.extend(_changed_paths(old[key], new[key], child))
            if len(out) >= 128:
                return out[:128]
        return out
    # Lists are policy-bearing ordered units. Reporting an index-by-index diff
    # is noisy and can obscure reorderings, so mark the containing field.
    return [path]


def _status_row(con: sqlite3.Connection, operator: sqlite3.Row) -> dict[str, Any]:
    latest = con.execute(
        "SELECT version, content_json, content_sha256, created_at "
        "FROM contract_versions WHERE operator_id=? ORDER BY version DESC LIMIT 1",
        (operator["id"],),
    ).fetchone()
    if latest is None:
        raise CorruptOperatorStoreError(
            f"Operator {operator['id']} has no contract version"
        )
    validated = operator_contract.parse_contract(
        latest["content_json"],
        source=f"Operator {operator['id']} contract v{latest['version']}",
    )
    if validated.sha256 != latest["content_sha256"]:
        raise CorruptOperatorStoreError(
            f"Operator {operator['id']} latest contract hash mismatch"
        )
    approval = con.execute(
        "SELECT approved_at, approved_by, content_sha256 "
        "FROM contract_approvals WHERE operator_id=? AND contract_version=?",
        (operator["id"], latest["version"]),
    ).fetchone()
    state = "approved" if approval is not None else "awaiting_approval"
    if approval is not None and approval["content_sha256"] != latest["content_sha256"]:
        raise CorruptOperatorStoreError(
            f"Operator {operator['id']} approval hash mismatch"
        )
    data = validated.data
    modes = {"auto": 0, "ask": 0, "deny": 0}
    for mode in data["authority"].values():
        modes[mode] += 1
    goals = data["intent"]["goals"]
    if state == "approved":
        next_action = "approved contract is ready; no operation is active"
    else:
        next_action = (
            f"approve v{latest['version']} with sha256:{latest['content_sha256']}"
        )
    approved_versions = [
        int(row["contract_version"])
        for row in con.execute(
            "SELECT contract_version FROM contract_approvals "
            "WHERE operator_id=? ORDER BY contract_version",
            (operator["id"],),
        )
    ]
    return {
        "id": operator["id"],
        "name": operator["name"],
        "state": state,
        "created_at": operator["created_at"],
        "contract_version": int(latest["version"]),
        "contract_sha256": latest["content_sha256"],
        "contract_created_at": latest["created_at"],
        "approved_versions": approved_versions,
        "approved_at": approval["approved_at"] if approval is not None else None,
        "approved_by": approval["approved_by"] if approval is not None else None,
        "projects": list(data["scope"]["projects"]),
        "goal_count": len(goals),
        "accepted_goals": 0,
        "authority": modes,
        "resources": {
            key: data["resources"][key]
            for key in (
                "max_active_runs",
                "max_worktrees",
                "max_worktree_bytes",
                "min_free_disk_bytes",
                "max_attempts_per_item",
                "max_wall_clock_seconds",
                "max_cost_usd",
            )
        },
        "next_action": next_action,
    }
