"""Read-only project observation and historical replay for Operator decisions."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Iterable

from orchestra_cli import (
    checkpoint,
    db,
    operator_runtime,
    operator_store,
    paths,
)

MAX_ARCHIVE_DB_BYTES = 512 * 1024 * 1024
MAX_IMPORTED_RUNS = 20_000
SNAPSHOT_ROW_LIMIT = 25
SNAPSHOT_BODY_PREVIEW_CHARS = 1024

REPLAY_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS replay_sources (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  label TEXT NOT NULL,
  source_path TEXT NOT NULL,
  content_sha256 TEXT UNIQUE NOT NULL,
  run_count INTEGER NOT NULL,
  message_count INTEGER NOT NULL,
  feed_count INTEGER NOT NULL,
  imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS replay_runs (
  source_id TEXT NOT NULL REFERENCES replay_sources(id),
  source_run_id INTEGER NOT NULL,
  agent TEXT NOT NULL,
  backend TEXT NOT NULL,
  model TEXT,
  title TEXT,
  work_item TEXT,
  requested_by TEXT,
  status TEXT NOT NULL,
  parent_run INTEGER,
  lead_run INTEGER,
  child_depth INTEGER,
  started_at TEXT,
  finished_at TEXT,
  PRIMARY KEY(source_id, source_run_id)
);

CREATE INDEX IF NOT EXISTS idx_replay_runs_clock
  ON replay_runs(source_id, started_at, source_run_id);

CREATE TABLE IF NOT EXISTS replay_messages (
  source_id TEXT NOT NULL REFERENCES replay_sources(id),
  source_message_id INTEGER NOT NULL,
  sender TEXT,
  recipient TEXT,
  kind TEXT,
  work_item TEXT,
  run_id INTEGER,
  created_at TEXT,
  PRIMARY KEY(source_id, source_message_id)
);

CREATE TABLE IF NOT EXISTS replay_feed (
  source_id TEXT NOT NULL REFERENCES replay_sources(id),
  source_feed_id INTEGER NOT NULL,
  author TEXT,
  tags TEXT,
  work_item TEXT,
  run_id INTEGER,
  created_at TEXT,
  PRIMARY KEY(source_id, source_feed_id)
);
"""


class ReplayError(Exception):
    pass


def connect(path: Path | None = None) -> sqlite3.Connection:
    con = operator_runtime.connect(path)
    con.executescript(REPLAY_SCHEMA)
    return con


def import_live_database(
    database: Path,
    *,
    label: str | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    database = database.expanduser().resolve()
    if not database.is_file():
        raise ReplayError(f"project database not found: {database}")
    source = _open_readonly(database, immutable=False)
    try:
        with tempfile.TemporaryDirectory(prefix="orchestra-live-replay-") as temp:
            snapshot_path = Path(temp) / "orchestra.db"
            destination = sqlite3.connect(snapshot_path)
            try:
                try:
                    source.backup(destination)
                except sqlite3.Error as exc:
                    raise ReplayError(
                        f"cannot create a consistent read-only snapshot of {database}: {exc}"
                    ) from exc
            finally:
                destination.close()
            if snapshot_path.stat().st_size > MAX_ARCHIVE_DB_BYTES:
                raise ReplayError(
                    f"database snapshot exceeds {MAX_ARCHIVE_DB_BYTES} bytes"
                )
            digest = _file_digest(snapshot_path)
            snapshot = _open_readonly(snapshot_path, immutable=True)
            try:
                return _import_connection(
                    snapshot,
                    kind="live",
                    label=label or database.parent.parent.name,
                    source_path=str(database),
                    content_sha256=digest,
                    path=path,
                )
            finally:
                snapshot.close()
    finally:
        source.close()


def import_project(
    root: Path,
    *,
    path: Path | None = None,
) -> dict[str, Any]:
    return import_live_database(
        paths.db_path(root),
        label=root.name,
        path=path,
    )


def import_archive(
    archive: Path,
    *,
    member: str = ".orchestra/orchestra.db",
    label: str | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    archive = archive.expanduser().resolve()
    if not archive.is_file():
        raise ReplayError(f"replay archive not found: {archive}")
    try:
        with zipfile.ZipFile(archive) as bundle:
            try:
                info = bundle.getinfo(member)
            except KeyError as exc:
                raise ReplayError(
                    f"archive {archive} does not contain {member!r}"
                ) from exc
            if info.file_size > MAX_ARCHIVE_DB_BYTES:
                raise ReplayError(
                    f"archive database is {info.file_size} bytes; "
                    f"limit is {MAX_ARCHIVE_DB_BYTES}"
                )
            digest = hashlib.sha256()
            with tempfile.TemporaryDirectory(prefix="orchestra-replay-") as temp:
                target = Path(temp) / "orchestra.db"
                with bundle.open(info) as source, target.open("wb") as output:
                    copied = 0
                    while True:
                        block = source.read(1024 * 1024)
                        if not block:
                            break
                        copied += len(block)
                        if copied > MAX_ARCHIVE_DB_BYTES:
                            raise ReplayError("archive database expanded beyond its limit")
                        digest.update(block)
                        output.write(block)
                source_db = _open_readonly(target, immutable=True)
                try:
                    return _import_connection(
                        source_db,
                        kind="archive",
                        label=label or archive.stem,
                        source_path=f"{archive}!/{member}",
                        content_sha256=digest.hexdigest(),
                        path=path,
                    )
                finally:
                    source_db.close()
    except zipfile.BadZipFile as exc:
        raise ReplayError(f"invalid replay zip archive: {archive}") from exc


def list_sources(*, path: Path | None = None) -> list[dict[str, Any]]:
    con = connect(path)
    try:
        return [
            dict(row)
            for row in con.execute(
                "SELECT * FROM replay_sources ORDER BY imported_at, id"
            )
        ]
    finally:
        con.close()


def replay_state(
    source_id: str,
    *,
    at: str | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    con = connect(path)
    try:
        source = con.execute(
            "SELECT * FROM replay_sources WHERE id=?",
            (source_id,),
        ).fetchone()
        if source is None:
            raise ReplayError(f"unknown replay source {source_id!r}")
        where = "source_id=?"
        params: list[Any] = [source_id]
        if at:
            where += " AND started_at<=?"
            params.append(at)
        rows = list(
            con.execute(
                f"SELECT * FROM replay_runs WHERE {where} "
                "ORDER BY started_at, source_run_id",
                params,
            )
        )
        status_counts: dict[str, int] = {}
        agents: dict[str, int] = {}
        active = []
        for row in rows:
            status = row["status"]
            if at and row["finished_at"] and row["finished_at"] > at:
                status = "running"
            status_counts[status] = status_counts.get(status, 0) + 1
            agents[row["agent"]] = agents.get(row["agent"], 0) + 1
            if status not in db.RUN_TERMINAL:
                active.append(int(row["source_run_id"]))
        return {
            "source": dict(source),
            "clock": at,
            "observed_runs": len(rows),
            "status_counts": status_counts,
            "agent_counts": agents,
            "active_run_ids": active,
            "latest_run_id": (
                max((int(row["source_run_id"]) for row in rows), default=None)
            ),
        }
    finally:
        con.close()


def operation_snapshot(
    operation_id: str,
    *,
    advance_cursors: bool = False,
    path: Path | None = None,
) -> dict[str, Any]:
    operation = operator_runtime.get_operation(operation_id, path=path)
    control = operator_runtime.connect(path)
    projects: list[dict[str, Any]] = []
    try:
        for project in operation["projects"]:
            cursor = control.execute(
                "SELECT max_run_id, max_message_id, max_feed_id "
                "FROM operator_event_cursors WHERE operation_id=? AND project_key=?",
                (operation["id"], project["project_key"]),
            ).fetchone()
            after_run = int(cursor["max_run_id"] or 0) if cursor else 0
            after_message = int(cursor["max_message_id"] or 0) if cursor else 0
            after_feed = int(cursor["max_feed_id"] or 0) if cursor else 0
            root = Path(project["root"])
            try:
                source = db.connect_readonly(root)
            except (OSError, sqlite3.Error) as exc:
                projects.append({
                    **project,
                    "available": False,
                    "error": checkpoint.redact_sensitive_text(str(exc)),
                    "runs": [],
                    "messages": [],
                    "feed": [],
                })
                continue
            try:
                runs = _bounded_rows(
                    source,
                    "runs",
                    after_run,
                    (
                        "id", "slug", "agent", "backend", "model", "title",
                        "work_item", "team", "requested_by", "status",
                        "parent_run", "lead_run", "child_depth", "branch",
                        "started_at", "finished_at",
                    ),
                )
                messages = _bounded_rows(
                    source,
                    "messages",
                    after_message,
                    (
                        "id", "sender", "recipient", "kind", "work_item",
                        "run_id", "created_at", "read_at", "body",
                    ),
                    body_preview=True,
                )
                feed = _bounded_rows(
                    source,
                    "feed",
                    after_feed,
                    (
                        "id", "author", "tags", "work_item", "run_id",
                        "created_at", "body",
                    ),
                    body_preview=True,
                )
                source_high_water = {
                    "max_run_id": _max_id(source, "runs"),
                    "max_message_id": _max_id(source, "messages"),
                    "max_feed_id": _max_id(source, "feed"),
                }
                high_water = {
                    "max_run_id": max(
                        (int(row["id"]) for row in runs), default=after_run
                    ),
                    "max_message_id": max(
                        (int(row["id"]) for row in messages), default=after_message
                    ),
                    "max_feed_id": max(
                        (int(row["id"]) for row in feed), default=after_feed
                    ),
                }
                projects.append({
                    **project,
                    "available": True,
                    "runs": runs,
                    "messages": messages,
                    "feed": feed,
                    "high_water": high_water,
                    "source_high_water": source_high_water,
                })
                if advance_cursors:
                    operator_runtime.update_event_cursor(
                        operation["id"],
                        project["project_key"],
                        **high_water,
                        path=path,
                    )
            finally:
                source.close()
    finally:
        control.close()
    return {
        "operation": {
            key: operation[key]
            for key in (
                "id", "operator_id", "contract_version", "mode", "state",
                "priority", "goals", "work_counts", "open_decisions",
            )
        },
        "projects": projects,
    }


def _import_connection(
    source: sqlite3.Connection,
    *,
    kind: str,
    label: str,
    source_path: str,
    content_sha256: str,
    path: Path | None,
) -> dict[str, Any]:
    run_count = _count(source, "runs")
    if run_count > MAX_IMPORTED_RUNS:
        raise ReplayError(
            f"source has {run_count} runs; replay limit is {MAX_IMPORTED_RUNS}"
        )
    message_count = _count(source, "messages")
    feed_count = _count(source, "feed")
    control = connect(path)
    try:
        existing = control.execute(
            "SELECT * FROM replay_sources WHERE content_sha256=?",
            (content_sha256,),
        ).fetchone()
        if existing is not None:
            return dict(existing)
        source_id = f"replay_{content_sha256[:16]}"
        control.execute("BEGIN IMMEDIATE")
        control.execute(
            "INSERT INTO replay_sources("
            "id, kind, label, source_path, content_sha256, run_count, "
            "message_count, feed_count, imported_at"
            ") VALUES(?,?,?,?,?,?,?,?,?)",
            (
                source_id,
                kind,
                _safe_text(label, 240),
                _safe_text(source_path, 4096),
                content_sha256,
                run_count,
                message_count,
                feed_count,
                operator_store.now(),
            ),
        )
        for row in _all_rows(source, "runs", MAX_IMPORTED_RUNS):
            control.execute(
                "INSERT INTO replay_runs("
                "source_id, source_run_id, agent, backend, model, title, "
                "work_item, requested_by, status, parent_run, lead_run, child_depth, "
                "started_at, finished_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    source_id,
                    row.get("id"),
                    _safe_text(row.get("agent"), 160),
                    _safe_text(row.get("backend"), 80),
                    _safe_text(row.get("model"), 240) if row.get("model") else None,
                    _safe_text(row.get("title"), 512) if row.get("title") else None,
                    _safe_text(row.get("work_item"), 160) if row.get("work_item") else None,
                    _safe_text(row.get("requested_by"), 160)
                    if row.get("requested_by") else None,
                    _safe_text(row.get("status") or "unknown", 80),
                    row.get("parent_run"),
                    row.get("lead_run"),
                    row.get("child_depth"),
                    row.get("started_at"),
                    row.get("finished_at"),
                ),
            )
        for row in _all_rows(source, "messages", MAX_IMPORTED_RUNS * 10):
            control.execute(
                "INSERT INTO replay_messages("
                "source_id, source_message_id, sender, recipient, kind, "
                "work_item, run_id, created_at"
                ") VALUES(?,?,?,?,?,?,?,?)",
                (
                    source_id,
                    row.get("id"),
                    _safe_text(row.get("sender"), 160) if row.get("sender") else None,
                    _safe_text(row.get("recipient"), 160)
                    if row.get("recipient") else None,
                    _safe_text(row.get("kind"), 80) if row.get("kind") else None,
                    _safe_text(row.get("work_item"), 160)
                    if row.get("work_item") else None,
                    row.get("run_id"),
                    row.get("created_at"),
                ),
            )
        for row in _all_rows(source, "feed", MAX_IMPORTED_RUNS * 10):
            control.execute(
                "INSERT INTO replay_feed("
                "source_id, source_feed_id, author, tags, work_item, run_id, created_at"
                ") VALUES(?,?,?,?,?,?,?)",
                (
                    source_id,
                    row.get("id"),
                    _safe_text(row.get("author"), 160) if row.get("author") else None,
                    _safe_text(row.get("tags"), 512) if row.get("tags") else None,
                    _safe_text(row.get("work_item"), 160)
                    if row.get("work_item") else None,
                    row.get("run_id"),
                    row.get("created_at"),
                ),
            )
        control.commit()
        return dict(
            control.execute(
                "SELECT * FROM replay_sources WHERE id=?",
                (source_id,),
            ).fetchone()
        )
    except Exception:
        control.rollback()
        raise
    finally:
        control.close()


def _open_readonly(database: Path, *, immutable: bool) -> sqlite3.Connection:
    suffix = "?mode=ro&immutable=1" if immutable else "?mode=ro"
    try:
        con = sqlite3.connect(database.resolve().as_uri() + suffix, uri=True, timeout=15)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only = ON")
        con.execute("PRAGMA busy_timeout = 10000")
        return con
    except sqlite3.Error as exc:
        raise ReplayError(f"cannot open replay database {database}: {exc}") from exc


def _file_digest(database: Path) -> str:
    digest = hashlib.sha256()
    total = 0
    for candidate in (database, Path(str(database) + "-wal")):
        if not candidate.is_file():
            continue
        with candidate.open("rb") as source:
            while True:
                block = source.read(1024 * 1024)
                if not block:
                    break
                total += len(block)
                if total > MAX_ARCHIVE_DB_BYTES:
                    raise ReplayError(
                        f"database snapshot exceeds {MAX_ARCHIVE_DB_BYTES} bytes"
                    )
                digest.update(block)
    return digest.hexdigest()


def _tables(con: sqlite3.Connection) -> set[str]:
    return {
        row["name"]
        for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _count(con: sqlite3.Connection, table: str) -> int:
    if table not in _tables(con):
        return 0
    return int(con.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])


def _all_rows(
    con: sqlite3.Connection,
    table: str,
    limit: int,
) -> Iterable[dict[str, Any]]:
    if table not in _tables(con):
        return []
    return (
        dict(row)
        for row in con.execute(
            f"SELECT * FROM {table} ORDER BY id LIMIT ?",
            (limit,),
        )
    )


def _bounded_rows(
    con: sqlite3.Connection,
    table: str,
    after_id: int,
    fields: tuple[str, ...],
    *,
    body_preview: bool = False,
) -> list[dict[str, Any]]:
    if table not in _tables(con):
        return []
    columns = {
        row["name"] for row in con.execute(f"PRAGMA table_info({table})")
    }
    selected = [field for field in fields if field in columns]
    if "id" not in selected:
        return []
    rows = con.execute(
        f"SELECT {','.join(selected)} FROM {table} WHERE id>? "
        "ORDER BY id LIMIT ?",
        (after_id, SNAPSHOT_ROW_LIMIT),
    )
    output: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        body = row.pop("body", None)
        for key, value in list(row.items()):
            if isinstance(value, str):
                row[key] = _safe_text(value, 1024)
        if body_preview:
            row["body_preview"] = (
                _safe_text(body, SNAPSHOT_BODY_PREVIEW_CHARS) if body else None
            )
        output.append(row)
    return output


def _max_id(con: sqlite3.Connection, table: str) -> int | None:
    if table not in _tables(con):
        return None
    row = con.execute(f"SELECT MAX(id) AS value FROM {table}").fetchone()
    return int(row["value"]) if row["value"] is not None else None


def _safe_text(value: Any, limit: int) -> str:
    text = checkpoint.redact_sensitive_text(str(value)) or ""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "\u2026"
