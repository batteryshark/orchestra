"""Best-effort notification command for low-volume fleet events.

The database and API feeds are the durable record. This module only wakes an
external integrator by starting one argv command with a small JSON document on
stdin. It deliberately has no shell, webhook client, or retry subsystem. When
given the caller's database connection, admission and the bounded process
outcome are appended to the normal control audit.
"""
from __future__ import annotations

import json
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import threading
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

from orchestra import db

EVENTS = frozenset({"attention.opened", "run.terminal", "observer.stopped"})
MAX_EVENT_BYTES = 32 * 1024
CALLBACK_TIMEOUT = 15


def argv(value: str | Sequence[str] | None) -> tuple[str, ...]:
    """Normalize the managed callback setting without invoking a shell."""
    if value is None:
        return ()
    if isinstance(value, str) and not value.strip():
        return ()
    parts = shlex.split(value) if isinstance(value, str) else list(value)
    if not parts:
        return ()
    if any(not isinstance(part, str) or not part or "\0" in part
           for part in parts):
        raise ValueError("callback command must be a non-empty argv")
    return tuple(parts)


def envelope(event: str, data: Mapping) -> bytes:
    if event not in EVENTS:
        raise ValueError(f"unsupported callback event: {event}")
    payload = json.dumps({
        "version": 2,
        "event": event,
        "occurred_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data": dict(data),
    }, ensure_ascii=False, separators=(",", ":")).encode()
    if len(payload) > MAX_EVENT_BYTES:
        raise ValueError(f"callback event exceeds {MAX_EVENT_BYTES} bytes")
    return payload


def _database_file(value: sqlite3.Connection | str | Path | None) -> Path | None:
    if isinstance(value, sqlite3.Connection):
        row = value.execute("PRAGMA database_list").fetchone()
        raw = row[2] if row else ""
    else:
        raw = str(value or "")
    return Path(raw) if raw and raw != ":memory:" else None


def _audit(database: Path | None, event: str, data: Mapping, outcome: str,
           detail: Mapping) -> None:
    """Append without ever creating/replacing a missing fleet database."""
    if database is None or not database.is_file():
        return
    try:
        con = sqlite3.connect(f"file:{database}?mode=rw", uri=True, timeout=10)
        try:
            con.execute("PRAGMA busy_timeout=10000")
            target_id = data.get("run_id") or data.get("attention_id")
            db.record_control(
                con, actor="orchestra:callback", action=f"callback.{event}",
                outcome=outcome,
                target_type="run" if data.get("run_id") is not None else "callback",
                target_id=target_id or event, detail=dict(detail))
            con.commit()
        finally:
            con.close()
    except (OSError, sqlite3.Error) as exc:
        print(f"orchestra: callback audit failed: {exc}", file=sys.stderr)


def _wait_and_audit(process: subprocess.Popen, database: Path | None,
                    event: str, data: Mapping, timeout: float) -> None:
    try:
        exit_code = process.wait(timeout=timeout)
        outcome = "delivered" if exit_code == 0 else "failed"
        detail = {"exit_code": exit_code}
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        outcome = "timed_out"
        detail = {"timeout_seconds": timeout}
    _audit(database, event, data, outcome, detail)


def emit(command: str | Sequence[str] | None, event: str,
         data: Mapping, *, audit_db: sqlite3.Connection | str | Path | None = None,
         timeout: float = CALLBACK_TIMEOUT) -> bool:
    """Start the callback and return whether it was admitted to the OS.

    A temporary file is used for stdin so a callback that never reads cannot
    block Orchestra on a full pipe. The child inherits an open descriptor;
    closing the parent's handle after ``Popen`` is safe.
    """
    database = _database_file(audit_db)
    try:
        command_argv = argv(command)
        payload = envelope(event, data)
    except (TypeError, ValueError) as exc:
        print(f"orchestra: invalid callback event: {exc}", file=sys.stderr)
        return False
    if not command_argv:
        return False
    try:
        with tempfile.TemporaryFile() as stdin:
            stdin.write(payload)
            stdin.seek(0)
            process = subprocess.Popen(
                command_argv,
                stdin=stdin,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
    except OSError as exc:
        print(f"orchestra: callback could not start: {exc}", file=sys.stderr)
        _audit(database, event, data, "failed", {"error": str(exc)[:500]})
        return False
    if database is not None:
        _audit(database, event, data, "started",
               {"pid": getattr(process, "pid", None)})
        threading.Thread(
            target=_wait_and_audit,
            args=(process, database, event, dict(data), max(float(timeout), 0.1)),
            name=f"orchestra-callback-{event}", daemon=True,
        ).start()
    return True
