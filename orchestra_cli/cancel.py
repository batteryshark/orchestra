"""Shared run stop semantics for CLI and UI callers."""
from __future__ import annotations

import os
import signal
import sqlite3
from dataclasses import dataclass

from orchestra_cli import db


@dataclass(frozen=True)
class StopResult:
    run_id: int
    status: str
    previous_status: str
    stopped: bool
    signal_sent: bool
    reason: str
    pid: int | None = None

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "previous_status": self.previous_status,
            "stopped": self.stopped,
            "signal_sent": self.signal_sent,
            "reason": self.reason,
            "pid": self.pid,
        }


def _safe_pid(value) -> int | None:
    if value is None:
        return None
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return None
    return pid if pid > 1 else None


def _signal_process_group(pid: int | None) -> tuple[bool, str]:
    if pid is None:
        return False, "no_pid"
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return False, "process_gone"
    except PermissionError:
        return False, "permission_denied"
    if pgid != pid:
        return False, "pid_not_process_group_leader"
    try:
        os.killpg(pid, signal.SIGTERM)
        return True, "sigterm_sent"
    except ProcessLookupError:
        return False, "process_gone"
    except PermissionError:
        return False, "permission_denied"


def stop_run(con: sqlite3.Connection, run_id: int) -> StopResult | None:
    """Mark a non-terminal run as user-stopped and signal its worker group.

    The persisted terminal state remains ``killed`` for compatibility with
    existing run queries. The row update happens before signaling so stale
    supervisors observe the user's terminal decision instead of racing it.
    """
    con.execute("BEGIN IMMEDIATE")
    try:
        row = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            con.execute("ROLLBACK")
            return None
        previous = row["status"]
        if previous in db.RUN_TERMINAL:
            con.execute("COMMIT")
            return StopResult(
                run_id=run_id,
                status=previous,
                previous_status=previous,
                stopped=False,
                signal_sent=False,
                reason="already_terminal",
                pid=_safe_pid(row["pid"]),
            )
        pid = _safe_pid(row["pid"])
        con.execute(
            "UPDATE runs SET status='killed', finished_at=COALESCE(finished_at, ?) "
            "WHERE id=? AND status NOT IN ('done','failed','timeout','killed')",
            (db.now(), run_id),
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise

    signal_sent, reason = _signal_process_group(pid)
    return StopResult(
        run_id=run_id,
        status="killed",
        previous_status=previous,
        stopped=True,
        signal_sent=signal_sent,
        reason=reason,
        pid=pid,
    )
