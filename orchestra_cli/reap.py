"""Reconcile runs whose supervisor died before it could finish them.

`supervise.supervise` writes the terminal status, summary and completion
message as its very last act. If that process dies first — crash, OOM, host
reboot, a terminal window closed on a `--sync` run — the row keeps whatever
non-terminal status it had. Nothing else in the system ever revisits it, so:

* `orchestra runs` reports work as still running hours after it finished,
* `orchestra wait` blocks forever on a run that will never change state,
* the summary the worker actually produced is never surfaced, even though it
  is sitting complete in the JSONL log,
* and the work-tracker entry is never written.

This module detects those orphans and settles them from the log. It is
deliberately conservative: a run is only reaped on positive evidence that
nothing is supervising it, because wrongly terminalizing a live run would
detach a working agent from its own bookkeeping.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from orchestra_cli import db, runners

# How long a legacy row (no supervisor_pid) must sit with a dead agent process
# and a silent log before we believe nobody is coming back for it. The
# supervisor relaunches the agent between loop iterations — resume, check-in,
# queued-message delivery — and during that gap the agent pid is legitimately
# dead while the supervisor is alive and about to spawn the next one. The log
# keeps advancing across those gaps, so quiescence is the reliable signal; this
# only has to outlast the longest launch gap.
DEFAULT_GRACE_SECONDS = 600


def _alive(pid: int | None) -> bool:
    """Is this pid a live process we could signal?"""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists, owned by someone else. Treat as alive: refusing to reap is
        # always the safe error here.
        return True
    except OSError:
        return True
    return True


def _log_quiet_for(log_path: str | None, seconds: float) -> bool:
    """True when the log exists and has not been written for `seconds`."""
    if not log_path:
        return False
    try:
        mtime = Path(log_path).stat().st_mtime
    except OSError:
        return False
    return (time.time() - mtime) >= seconds


def is_orphan(run, *, grace_seconds: int = DEFAULT_GRACE_SECONDS) -> bool:
    """Has this non-terminal run lost its supervisor?

    Two rules, because rows written before supervisor_pid existed cannot use
    the precise one:

    * supervisor_pid recorded -> orphaned exactly when that process is gone.
      No grace period is needed; the pid IS the supervisor, and a dead
      supervisor can never write the completion row.
    * supervisor_pid NULL (legacy) -> fall back to "agent process dead AND the
      log has been silent for the grace period". Weaker, hence the wait.
    """
    if run["status"] in db.RUN_TERMINAL:
        return False
    supervisor_pid = run["supervisor_pid"] if "supervisor_pid" in run.keys() else None
    if supervisor_pid:
        return not _alive(supervisor_pid)
    if _alive(run["pid"]):
        return False
    # 'spawning' means the supervisor had not yet launched the agent, so there
    # is no agent pid to have died and no log to have gone quiet. Require the
    # row itself to be old enough instead.
    if not run["pid"]:
        return _log_quiet_for(run["log_path"], grace_seconds)
    return _log_quiet_for(run["log_path"], grace_seconds)


def reap_orphans(con, root: Path | None = None, *,
                 grace_seconds: int = DEFAULT_GRACE_SECONDS) -> list[dict]:
    """Settle every orphaned run. Returns what was reaped, for reporting.

    The outcome comes from the worker's own log rather than being assumed:
    a log with a clean terminal record settles as the status it recorded, and
    one without becomes 'failed' with a NULL exit code, which is the honest
    reading of "we do not know how it ended".
    """
    reaped: list[dict] = []
    rows = list(con.execute(
        "SELECT * FROM runs WHERE status NOT IN "
        f"({','.join('?' * len(db.RUN_TERMINAL))}) ORDER BY id",
        db.RUN_TERMINAL,
    ))
    for run in rows:
        if not is_orphan(run, grace_seconds=grace_seconds):
            continue
        log_path = run["log_path"]
        outcome = runners.log_outcome(log_path) if log_path else None
        session_ref, last_text = (runners.parse_log(log_path) if log_path
                                  else (None, None))
        status = outcome or "failed"
        terminal_failure = (
            runners.claude_terminal_failure(log_path)
            if log_path and run["backend"] == "claude" else None
        )
        terminal_text = (
            runners.claude_terminal_failure_text(terminal_failure)
            if terminal_failure is not None else None
        )
        if status == "failed" and terminal_text:
            last_text = terminal_text
        exit_code = 0 if status == "done" else None
        summary = (last_text or "").strip()[:2000] or None
        con.execute(
            "UPDATE runs SET status=?, exit_code=?, "
            "session_ref=COALESCE(?, session_ref), "
            "summary=COALESCE(?, summary), finished_at=COALESCE(finished_at, ?) "
            "WHERE id=? AND status NOT IN "
            f"({','.join('?' * len(db.RUN_TERMINAL))})",
            (status, exit_code, session_ref, summary, db.now(), run["id"],
             *db.RUN_TERMINAL),
        )
        note = (
            f"run {run['id']} ({run['agent']}) reconciled -> {status}: its "
            "supervisor died before recording the result"
            + ("; the worker's own log shows it finished, so the summary above "
               "is real work that was never reported."
               if outcome == "done" else
               "; the log carries no terminal record, so how it ended is "
               "unknown.")
        )
        con.execute(
            "INSERT INTO messages(sender, recipient, body, work_item, run_id, "
            "created_at) VALUES('orchestra', ?, ?, ?, ?, ?)",
            (run["requested_by"], note + f"\nDetails: `orchestra run show "
             f"{run['id']}` · logs: `orchestra logs {run['id']}`",
             run["work_item"], run["id"], db.now()),
        )
        con.execute(
            "INSERT INTO feed(author, body, work_item, run_id, created_at, tags) "
            "VALUES('orchestra', ?, ?, ?, ?, 'run')",
            (note, run["work_item"], run["id"], db.now()),
        )
        con.execute(
            "UPDATE spawn_requests SET status='failed', error=?, processed_at=? "
            "WHERE lead_run=? AND status IN ('pending','processing')",
            (f"lead supervisor died before accepting this spawn request", db.now(),
             run["id"]),
        )
        reaped.append({"id": run["id"], "agent": run["agent"], "status": status,
                       "work_item": run["work_item"], "note": note})
    if reaped:
        con.commit()
    # The tracker gets the same entry the normal completion path writes, so a
    # reconciled run is not silently missing from its work item's history.
    if reaped and root is not None:
        from orchestra_cli import supervise
        for item in reaped:
            if item["work_item"]:
                supervise._work_log(root, item["work_item"], item["note"] + ".")
    return reaped
