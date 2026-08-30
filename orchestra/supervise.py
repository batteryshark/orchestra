"""One-run execution and operator controls for Orchestra v2.

The daemon alone claims FIFO capacity. A detached supervisor owns one admitted
run, executes frozen runtime/profile snapshots, and writes only that run's
lifecycle, trace, evidence, messages, and lineage consequences.
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orchestra import (acp, attention, auth, callbacks, child_runs, config, db,
                       messaging, paths, retry, runners, runway, runtime, runs, traces,
                       worktree)
from orchestra.proc import (enrich_path, process_identity, raise_file_limit,
                            resolve_cmd, session_kwargs, signal_owned_group,
                            terminate_group)

POLL_INTERVAL = 0.25
EARLY_REF_WINDOW = 90
DEFAULT_TIMEOUT_SECONDS = 4 * 60 * 60
MAX_DIFF_BYTES = 16 * 1024 * 1024
TERMINAL = frozenset(db.RUN_TERMINAL)


class ExecutionError(RuntimeError):
    pass


def _json(raw, fallback=None):
    if isinstance(raw, dict):
        return dict(raw)
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {} if fallback is None else fallback
    return value if isinstance(value, dict) else ({} if fallback is None else fallback)


def _snapshots(run) -> tuple[dict, dict]:
    return _json(run["profile_snapshot"]), _json(run["runtime_snapshot"])


def _identity(pid: int) -> str | None:
    for _ in range(10):
        value = process_identity(pid)
        if value:
            return value
        time.sleep(0.01)
    return None


def spawn_supervisor(root: Path, run_id: int) -> int:
    """Start one detached supervisor and durably bind its PID identity."""
    command = [sys.executable, "-m", "orchestra", "_supervise",
               str(int(run_id)), "--root", str(root)]
    error_log = paths.logs_dir() / f"supervisor-{int(run_id)}.log"
    handle = open(error_log, "ab")
    try:
        process = subprocess.Popen(
            resolve_cmd(command), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=handle,
            **session_kwargs(detached=True))
    finally:
        handle.close()
    identity = _identity(process.pid)
    try:
        error_log.chmod(0o600)
    except OSError:
        pass
    if identity is None:
        terminate_group(process.pid, force=True)
        raise ExecutionError(
            f"supervisor {process.pid} has no durable process identity")
    con = db.connect()
    try:
        changed = con.execute(
            "UPDATE runs SET supervisor_pid=?,supervisor_pid_identity=? "
            "WHERE id=? AND status='starting' AND "
            "(supervisor_pid IS NULL OR supervisor_pid=?)",
            (process.pid, identity, int(run_id), process.pid),
        )
        con.commit()
    finally:
        con.close()
    if changed.rowcount != 1:
        terminate_group(process.pid, force=True)
        raise ExecutionError(f"run {run_id} was no longer available to launch")
    try:
        threading.Thread(target=process.wait, daemon=True).start()
    except RuntimeError:
        pass
    return process.pid


def _active(run) -> bool:
    return run is not None and run["status"] not in TERMINAL


def _record_control(con, run_id: int, actor: str, action: str, outcome: str,
                    request_id: str | None = None, detail=None) -> int:
    with con:
        audit_id = db.record_control(
            con, actor=actor, action=action, outcome=outcome,
            target_type="run", target_id=run_id, request_id=request_id,
            detail=detail)
    return audit_id


def tell(con: sqlite3.Connection, run_id: int, body: str,
         actor: str = "operator", *, request_id: str | None = None) -> dict:
    run = runs.find(con, int(run_id))
    if not _active(run):
        raise ExecutionError(
            f"run {run_id} is {run['status'] if run else 'missing'}")
    message_id = messaging.queue_tell(
        con, int(run_id), actor, body, run["log_path"], boundary=True,
        correlation_id=request_id)
    audit_id = _record_control(
        con, int(run_id), actor, "run.tell", "queued", request_id,
        {"message_id": message_id})
    result = dict(con.execute("SELECT * FROM messages WHERE id=?",
                              (message_id,)).fetchone())
    result["control_audit_id"] = audit_id
    _, runtime_snapshot = _snapshots(run)
    capabilities = _json(runtime_snapshot.get("capabilities"))
    result["delivery_mode"] = (
        "live" if run["status"] == "running"
        and runtime_snapshot.get("adapter") == "acp"
        and capabilities.get("steer_method") else
        "safe_boundary" if run["status"] == "running" else "next_turn")
    return result


def interrupt(con: sqlite3.Connection, run_id: int, body: str,
              actor: str = "operator", *, request_id: str | None = None) -> dict:
    """Cancel the current turn; its supervisor resumes the same run."""
    run = runs.find(con, int(run_id))
    if not _active(run):
        raise ExecutionError(
            f"run {run_id} is {run['status'] if run else 'missing'}")
    try:
        offset = os.path.getsize(run["log_path"]) if run["log_path"] else 0
    except OSError:
        offset = 0
    message_id = messaging.post(
        con, int(run_id), direction="inbound", sender=actor, body=body,
        kind="interrupt", correlation_id=request_id, delivery_offset=offset)
    _, runtime_snapshot = _snapshots(run)
    if run["session_ref"] and _runtime_can_resume(runtime_snapshot):
        resume_mode = "same_session"
    elif run["session_ref"]:
        resume_mode = "frozen_brief_replay_risk"
    else:
        resume_mode = "pending_session_capture"
    audit_id = _record_control(
        con, int(run_id), actor, "run.interrupt", "queued", request_id,
        {"message_id": message_id, "resume_mode": resume_mode,
         "fallback": "frozen_brief_replay_risk"})
    result = dict(con.execute("SELECT * FROM messages WHERE id=?",
                              (message_id,)).fetchone())
    result.update({
        "control_audit_id": audit_id,
        "delivery_mode": "cancel_current_turn",
        "resume_mode": resume_mode,
        "fallback": "frozen_brief_replay_risk",
    })
    return result


def _tree(con: sqlite3.Connection, run_id: int) -> list[int]:
    return [int(row["id"]) for row in con.execute(
        "WITH RECURSIVE tree(id) AS (SELECT id FROM runs WHERE id=? UNION ALL "
        "SELECT r.id FROM runs r JOIN tree ON r.parent_run_id=tree.id "
        "OR r.retry_of_run_id=tree.id OR r.continuation_of_run_id=tree.id) "
        "SELECT id FROM tree ORDER BY id DESC", (int(run_id),))]


def stop(con: sqlite3.Connection, run_id: int, actor: str = "operator", *,
         tree: bool = False, request_id: str | None = None,
         reason: str = "Stopped by operator") -> dict:
    """Stop exactly one run, or its explicit descendant tree."""
    ids = _tree(con, run_id) if tree else [int(run_id)]
    if not ids:
        raise ExecutionError(f"no run {run_id}")
    rows = [con.execute("SELECT * FROM runs WHERE id=?", (item,)).fetchone()
            for item in ids]
    if rows[0] is None:
        raise ExecutionError(f"no run {run_id}")
    signals: list[tuple[object, str, str]] = []
    for run in rows:
        if run is None or run["status"] in TERMINAL or run["pid"] is None:
            continue
        outcome, detail = signal_owned_group(
            int(run["pid"]), run["pid_identity"], 0)
        if outcome == "refused":
            _record_control(con, int(run["id"]), actor, "run.stop", "refused",
                            request_id, {"reason": detail})
            raise ExecutionError(detail)
        signals.append((run, outcome, detail))
    changed_ids: list[int] = []
    timestamp = db.now()
    with con:
        for run in rows:
            if run is None:
                continue
            changed = con.execute(
                f"UPDATE runs SET status='stopped',waiting_kind=NULL,hold_reason=NULL,"
                f"finished_at=?,summary=?,run_token_hash=NULL "
                f"WHERE id=? AND status NOT IN {db.TERMINAL_SQL}",
                (timestamp, reason[:2000], int(run["id"])),
            )
            if changed.rowcount:
                changed_ids.append(int(run["id"]))
        audit_id = db.record_control(
            con, actor=actor, action="run.stop_tree" if tree else "run.stop",
            outcome="ok", target_type="run", target_id=run_id,
            request_id=request_id, detail={"stopped_run_ids": changed_ids})
    for run, outcome, _ in signals:
        if int(run["id"]) in changed_ids and outcome == "signalled":
            signal_owned_group(int(run["pid"]), run["pid_identity"], signal.SIGTERM)
    for item in changed_ids:
        _after_terminal(con, item)
    return {"run_id": int(run_id), "tree": bool(tree),
            "control_audit_id": audit_id,
            "stopped_run_ids": changed_ids}


def check(con: sqlite3.Connection, run_id: int, actor: str = "operator", *,
          request_id: str | None = None) -> dict:
    """Return deterministic live facts; this spends no model turn."""
    run = runs.find(con, int(run_id))
    if run is None:
        raise ExecutionError(f"no run {run_id}")
    process = "none"
    if run["pid"] is not None:
        process, _ = signal_owned_group(
            int(run["pid"]), run["pid_identity"], 0)
        if process == "signalled":
            process = "alive"
    supervisor = "none"
    if run["supervisor_pid"] is not None:
        supervisor, _ = signal_owned_group(
            int(run["supervisor_pid"]), run["supervisor_pid_identity"], 0)
        if supervisor == "signalled":
            supervisor = "alive"
    latest = con.execute(
        "SELECT MAX(seq) AS seq,COUNT(*) AS count FROM events WHERE run_id=?",
        (int(run_id),),).fetchone()
    blockers = con.execute(
        "SELECT COUNT(*) FROM attention_requests WHERE run_id=? AND status='open' "
        "AND blocking=1", (int(run_id),)).fetchone()[0]
    pending = con.execute(
        "SELECT COUNT(*) FROM messages WHERE run_id=? AND direction='inbound' "
        "AND status='pending'", (int(run_id),)).fetchone()[0]
    _, runtime_snapshot = _snapshots(run)
    result = {
        "run_id": int(run_id), "status": run["status"],
        "waiting_kind": run["waiting_kind"], "hold_reason": run["hold_reason"],
        "process": process, "pid": run["pid"],
        "supervisor": supervisor, "supervisor_pid": run["supervisor_pid"],
        "progress": traces.progress(run["log_path"],
                                    runtime_snapshot.get("adapter", "")),
        "event_count": int(latest["count"] or 0),
        "last_event_seq": int(latest["seq"] or 0),
        "pending_messages": int(pending), "blocking_attention": int(blockers),
        "active_children": [child["id"] for child in
                            child_runs.active_children(con, int(run_id))],
    }
    result["control_audit_id"] = _record_control(
        con, int(run_id), actor, "run.check", "ok", request_id,
        {"status": run["status"], "process": process,
         "supervisor": supervisor})
    return result


def _git_head(path: Path) -> str | None:
    try:
        return worktree.head(path)
    except RuntimeError:
        return None


def _repo_root(path: Path) -> Path | None:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, timeout=30)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip()).resolve()


def _is_repo(path: Path) -> bool:
    return _repo_root(path) is not None


def _prepare_workdir(con: sqlite3.Connection, run) -> object:
    """Allocate or restore only Orchestra-owned isolation."""
    run = runs.find(con, int(run["id"]))
    _, runtime_snapshot = _snapshots(run)
    adapter = runtime_snapshot.get("adapter")
    # Admission freezes the requested CWD. A later group edit affects future
    # runs only; an admitted run must never silently execute elsewhere.
    source_cwd = Path(run["cwd"])
    root = Path(run["repo"]) if run["repo"] else _repo_root(source_cwd)
    state_home = paths.home().resolve()
    if (root is not None and not run["repo"]
            and source_cwd.resolve().is_relative_to(state_home)
            and not root.resolve().is_relative_to(state_home)):
        # A managed workspace inside ORCHESTRA_HOME must never ride an
        # enclosing user repository (a git-init'd $HOME, most commonly).
        root = None
    relative = source_cwd.relative_to(root) if root else Path()
    workdir = Path(run["workdir"] or source_cwd)
    branch = run["branch"]
    if branch:
        if not workdir.is_dir():
            if root is None:
                raise ExecutionError("retained worktree has no repository root")
            worktree_root = worktree.restore(
                root, int(run["id"]), run["group_slug"], branch, adapter)
            workdir = worktree_root / relative
    elif root is not None and run["isolation"] != "shared":
        worktree_root, branch = worktree.create(
            root, int(run["id"]), run["group_slug"], backend=adapter)
        workdir = worktree_root / relative
        con.execute(
            "UPDATE runs SET repo=?,workdir=?,branch=?,base_commit=? WHERE id=?",
            (str(root), str(workdir), branch, _git_head(root), int(run["id"])))
        con.commit()
    elif run["isolation"] == "worktree":
        raise ExecutionError("worktree isolation requires a git repository CWD")
    else:
        workdir = source_cwd
        con.execute(
            "UPDATE runs SET repo=NULL,workdir=?,base_commit=NULL WHERE id=?",
            (str(workdir), int(run["id"])))
        con.commit()
    if not workdir.is_dir():
        raise ExecutionError(f"run workdir is unavailable: {workdir}")
    try:
        # macOS TCC lets stat succeed on a protected volume while the read
        # itself gets EPERM, so is_dir() alone admits a workdir the worker
        # cannot use. Fail here with remediation instead of a worker EPERM.
        os.listdir(workdir)
    except PermissionError as exc:
        # macOS attributes file access to the executed Mach-O binary, so the
        # grant must name the resolved interpreter, never a wrapper script.
        raise ExecutionError(
            f"the daemon was denied read access to {workdir} "
            f"({exc.strerror}). On macOS, grant Full Disk Access to "
            f"{Path(sys.executable).resolve()} in System Settings > Privacy & "
            "Security, then run `orchestra service restart`. Or use a CWD on "
            "the internal disk.") from exc
    if str(workdir) != run["workdir"]:
        con.execute("UPDATE runs SET workdir=? WHERE id=?",
                    (str(workdir), int(run["id"])))
        con.commit()
    runs._write_brief(con, int(run["id"]))  # the isolated path is now frozen for this turn
    con.commit()
    log_path = Path(
        run["log_path"] or paths.run_dir(int(run["id"])) / "worker.jsonl")
    log_path.touch()
    try:
        log_path.chmod(0o600)
    except OSError:
        pass
    return runs.find(con, int(run["id"]))


def _pending(con: sqlite3.Connection, run_id: int) -> list[dict]:
    return [dict(row) for row in con.execute(
        "SELECT * FROM messages WHERE run_id=? AND direction='inbound' "
        "AND status='pending' ORDER BY id", (int(run_id),))]


def _trace_context(con: sqlite3.Connection, run_id: int) -> str:
    rows = list(con.execute(
        "SELECT kind,name,payload FROM events WHERE run_id=? ORDER BY seq DESC "
        "LIMIT 20", (int(run_id),)))[::-1]
    if not rows:
        return "No normalized trace was captured before the interruption."
    lines = []
    for row in rows:
        name = f" ({row['name']})" if row["name"] else ""
        payload = " ".join((row["payload"] or "").split())[:300]
        lines.append(f"- {row['kind']}{name}: {payload}")
    return "\n".join(lines)


def _replay_audit(con: sqlite3.Connection, run_id: int, reason: str) -> None:
    existing = con.execute(
        "SELECT 1 FROM supervision_events WHERE run_id=? AND detector='resume' "
        "AND action='replay_risk' AND reason=?", (int(run_id), reason)).fetchone()
    if existing:
        return
    con.execute(
        "INSERT INTO supervision_events(run_id,detector,action,reason,detail_json,"
        "created_at) VALUES(?,'resume','replay_risk',?,?,?)",
        (int(run_id), reason,
         json.dumps({"risk": "the previous turn may have made non-idempotent "
                              "external changes before a session reference existed"},
                    separators=(",", ":")), db.now()),
    )
    con.commit()


def _restart_prompt(con: sqlite3.Connection, run, pending: list[dict],
                    reason: str) -> str:
    brief_text = Path(run["brief_path"]).read_text(encoding="utf-8")
    _replay_audit(con, int(run["id"]), reason)
    delivery = messaging.render_delivery(pending) if pending else (
        "Continue the original mission and verify prior external side effects.")
    return (f"{brief_text}\n\n--- restart context ---\n{reason}. The prior turn "
            "may have been cancelled after non-idempotent external actions; "
            "verify their state before repeating them.\n\nRecent trace:\n"
            f"{_trace_context(con, int(run['id']))}\n\n{delivery}")


def _prompt(con: sqlite3.Connection, run, pending: list[dict]) -> str:
    brief_text = Path(run["brief_path"]).read_text(encoding="utf-8")
    if not pending:
        return brief_text if not run["session_ref"] else (
            "Continue the original mission from the current session and end with "
            "a concise result summary.")
    delivery = messaging.render_delivery(pending)
    if run["session_ref"]:
        return delivery + "\n\nEnd with a concise result summary."
    prior_turn = run["worker_status"] is not None or any(
        row["kind"] == "interrupt" for row in pending)
    if not prior_turn:
        try:
            prior_turn = os.path.getsize(run["log_path"]) > 0
        except OSError:
            pass
    if prior_turn:
        return _restart_prompt(
            con, run, pending,
            "resume requested before a reliable session reference was captured")
    return f"{brief_text}\n\n--- initial messages ---\n{delivery}"


def _runtime_can_resume(snapshot: dict) -> bool:
    adapter = str(snapshot.get("adapter") or "")
    if adapter in runtime.BUILTIN_ADAPTERS or adapter == "acp":
        return True
    capabilities = _json(snapshot.get("capabilities"))
    if "resume" in capabilities:
        return bool(capabilities["resume"])
    command = snapshot.get("command", snapshot.get("command_json", ()))
    if isinstance(command, str):
        try:
            command = json.loads(command)
        except ValueError:
            command = ()
    return isinstance(command, list) and any(
        "{session_ref}" in part for part in command if isinstance(part, str))


def _claim_exact(con: sqlite3.Connection, run_id: int,
                 rows: list[dict]) -> None:
    if not rows:
        return
    ids = [int(row["id"]) for row in rows]
    placeholders = ",".join("?" for _ in ids)
    con.execute(
        f"UPDATE messages SET status='delivered',delivered_at=? WHERE run_id=? "
        f"AND status='pending' AND id IN ({placeholders})",
        (db.now(), int(run_id), *ids),)
    con.commit()
    for row in rows:
        traces.record_injection(con, int(run_id), row["sender"], row["body"])


def _requeue_exact(con: sqlite3.Connection, run_id: int,
                   rows: list[dict]) -> None:
    if not rows:
        return
    ids = [int(row["id"]) for row in rows]
    placeholders = ",".join("?" for _ in ids)
    con.execute(
        f"UPDATE messages SET status='pending',delivered_at=NULL WHERE run_id=? "
        f"AND direction='inbound' AND status='delivered' "
        f"AND id IN ({placeholders})",
        (int(run_id), *ids),
    )
    con.commit()


def _session_missing_since(log_path: str, offset: int) -> str | None:
    try:
        with open(log_path, "rb") as source:
            source.seek(offset)
            text = source.read().decode("utf-8", "replace")
    except OSError:
        return None
    for line in text.splitlines():
        if any(marker in line.casefold() for marker in runners.SESSION_GONE):
            return line.strip()[:300]
    return None


def _read_log_events(log_path: str, offset: int,
                     max_bytes: int = 4_000_000) -> tuple[list[dict], int]:
    try:
        with open(log_path, "rb") as source:
            source.seek(offset)
            data = source.read(max_bytes)
    except OSError:
        return [], offset
    end = data.rfind(b"\n")
    if end < 0:
        return [], offset + len(data) if len(data) == max_bytes else offset
    events = []
    for raw in data[:end].splitlines():
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, ValueError):
            continue
        if isinstance(value, dict):
            events.append(value)
    return events, offset + end + 1


def _safe_boundary(adapter: str, event: dict) -> bool:
    kind = event.get("type")
    part = event.get("part") or {}
    if adapter == "opencode":
        return kind == "step_finish" or part.get("type") == "step-finish"
    if adapter == "codex":
        item = event.get("item") or {}
        return kind == "item.completed" and item.get("type") in {
            "command_execution", "file_change", "patch", "mcp_tool_call",
            "web_search"}
    if adapter == "claude" and kind == "user":
        content = (event.get("message") or {}).get("content") or []
        return any(isinstance(item, dict) and item.get("type") == "tool_result"
                   for item in content)
    return False


def _terminate(process: subprocess.Popen, grace: float = 10) -> None:
    terminate_group(process.pid)
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        terminate_group(process.pid, force=True)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def _set_session(con: sqlite3.Connection, run_id: int, log_path: str) -> None:
    current = con.execute("SELECT session_ref FROM runs WHERE id=?",
                          (int(run_id),)).fetchone()
    if current and not current["session_ref"]:
        session_ref, _ = runners.parse_log(log_path)
        if session_ref:
            con.execute("UPDATE runs SET session_ref=? WHERE id=?",
                        (session_ref, int(run_id)))
            con.commit()


def _run_exec_turn(con: sqlite3.Connection, run, plan: runtime.LaunchPlan,
                   pending: list[dict], timeout: int,
                   stall_timeout: int | None) -> tuple[str, int | None]:
    run_id, adapter = int(run["id"]), plan.adapter
    log_path = run["log_path"]
    try:
        turn_offset = os.path.getsize(log_path)
    except OSError:
        turn_offset = 0
    stdin = subprocess.PIPE if plan.stdin is not None else subprocess.DEVNULL
    with open(log_path, "ab") as log:
        process = subprocess.Popen(
            resolve_cmd(list(plan.argv)), stdin=stdin, stdout=log,
            stderr=subprocess.STDOUT, cwd=run["workdir"], env=plan.env,
            **session_kwargs())
    identity = _identity(process.pid)
    if identity is None and process.poll() is None:
        _terminate(process)
        raise ExecutionError(
            f"worker {process.pid} has no durable process identity")
    changed = con.execute(
        "UPDATE runs SET pid=?,pid_identity=?,status='running',worker_status=NULL,"
        "worker_exit_code=NULL WHERE id=? AND status='starting'",
        (process.pid, identity, run_id),)
    con.commit()
    if changed.rowcount != 1:
        _terminate(process)
        return "stopped", process.poll()
    if plan.stdin is not None and process.stdin is not None:
        def feed() -> None:
            try:
                process.stdin.write(plan.stdin.encode())
            except (BrokenPipeError, OSError):
                pass
            finally:
                try:
                    process.stdin.close()
                except OSError:
                    pass

        threading.Thread(target=feed, daemon=True).start()
    _claim_exact(con, run_id, pending)
    started = last_progress = time.monotonic()
    try:
        last_size = os.path.getsize(log_path)
    except OSError:
        last_size = 0
    initial_tells = [row for row in pending if row["kind"] == "tell"]
    tell_ids = {int(row["id"]) for row in initial_tells}
    scan_offset = min((int(row["delivery_offset"] or 0)
                       for row in initial_tells), default=last_size)
    while True:
        code = process.poll()
        try:
            size = os.path.getsize(log_path)
        except OSError:
            size = last_size
        if size > last_size:
            last_size, last_progress = size, time.monotonic()
            if time.monotonic() - started <= EARLY_REF_WINDOW:
                _set_session(con, run_id, log_path)
        traces.ingest(con, run_id, log_path, adapter)
        child_runs.process_pending(con, run_id)
        current = con.execute(
            "SELECT status,session_ref FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        waiting = current and current["status"] == "waiting"
        terminal = current and current["status"] in TERMINAL
        queued = _pending(con, run_id)
        interrupts = [row for row in queued if row["kind"] == "interrupt"]
        tells = [row for row in queued if row["kind"] == "tell"]
        if code is not None:
            traces.drain(con, run_id, log_path, adapter)
            _set_session(con, run_id, log_path)
            if terminal:
                return current["status"], code
            if waiting:
                return "suspend", code
            missing = _session_missing_since(log_path, turn_offset) \
                if code != 0 and run["session_ref"] else None
            if missing:
                _replay_audit(
                    con, run_id, f"saved session could not be loaded: {missing}")
                _requeue_exact(con, run_id, pending)
                con.execute("UPDATE runs SET session_ref=NULL WHERE id=?", (run_id,))
                con.commit()
                return "resume", code
            return ("resume", code) if queued else (
                "completed" if code == 0 else "failed", code)
        if terminal or waiting or interrupts:
            _terminate(process)
            traces.drain(con, run_id, log_path, adapter)
            _set_session(con, run_id, log_path)
            if terminal:
                return current["status"], process.poll()
            return ("suspend" if waiting else "resume"), process.poll()
        if tells:
            current_tell_ids = {int(row["id"]) for row in tells}
            offset = min(int(row["delivery_offset"] or 0) for row in tells)
            if current_tell_ids != tell_ids:
                tell_ids, scan_offset = current_tell_ids, offset
            events, scan_offset = _read_log_events(log_path, scan_offset)
            if any(_safe_boundary(adapter, event) for event in events):
                _terminate(process)
                traces.drain(con, run_id, log_path, adapter)
                _set_session(con, run_id, log_path)
                return "resume", process.poll()
        now = time.monotonic()
        if now - started >= timeout:
            _terminate(process)
            traces.drain(con, run_id, log_path, adapter)
            _set_session(con, run_id, log_path)
            return "timed_out", process.poll()
        if stall_timeout and now - last_progress >= stall_timeout:
            _terminate(process)
            traces.drain(con, run_id, log_path, adapter)
            _set_session(con, run_id, log_path)
            return "timed_out", process.poll()
        time.sleep(POLL_INTERVAL)


def _acp_result(method: str, frame: dict):
    if "error" in frame:
        error = frame.get("error") or {}
        raise ExecutionError(f"{method}: {error.get('message') or error}")
    return frame.get("result")


def _acp_drain(peer: acp.Peer, turn: int, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if peer.response(turn) is not None:
                return
        except acp.AcpError:
            return
        time.sleep(0.05)


def _run_acp(con: sqlite3.Connection, run, plan: runtime.LaunchPlan,
             initial_prompt: str, initial_pending: list[dict], timeout: int,
             stall_timeout: int | None, profile: dict,
             runtime_snapshot: dict) -> tuple[str, int | None]:
    """Run ACP turns without coupling execution to any one harness."""
    run_id = int(run["id"])
    peer = acp.Peer(
        plan.argv, cwd=run["workdir"], env=plan.env, log_path=run["log_path"],
        on_request=lambda method, params: (
            acp.permission_answer(profile, params)
            if method == "session/request_permission" else None))
    started = time.monotonic()
    try:
        peer.start()
        if peer.proc is None:
            raise ExecutionError("ACP runtime started without a process")
        identity = _identity(peer.proc.pid)
        if identity is None and peer.proc.poll() is None:
            terminate_group(peer.proc.pid, force=True)
            raise ExecutionError(
                f"ACP worker {peer.proc.pid} has no durable process identity")
        changed = con.execute(
            "UPDATE runs SET pid=?,pid_identity=?,status='running',"
            "worker_status=NULL,worker_exit_code=NULL WHERE id=? AND status='starting'",
            (peer.proc.pid, identity, run_id),)
        con.commit()
        if changed.rowcount != 1:
            return "stopped", None
        initialized = peer.call("initialize", {
            "protocolVersion": acp.PROTOCOL_VERSION,
            "clientCapabilities": {"fs": {"readTextFile": False,
                                           "writeTextFile": False},
                                   "terminal": False},
            "clientInfo": {"name": "orchestra", "version": "2"},
        }) or {}
        if initialized.get("protocolVersion") != acp.PROTOCOL_VERSION:
            raise ExecutionError("ACP protocol version mismatch")
        capabilities = initialized.get("agentCapabilities") or {}
        prompt, pending = initial_prompt, initial_pending
        session_ref = run["session_ref"]
        loaded = False
        if session_ref and capabilities.get("loadSession"):
            try:
                peer.call("session/load", {"sessionId": session_ref,
                                           "cwd": run["workdir"],
                                           "mcpServers": []})
            except acp.AcpError as exc:
                reason = f"ACP saved session could not be loaded: {exc}"
                prompt = _restart_prompt(con, run, pending, reason)
            else:
                loaded = True
        if not loaded:
            if session_ref:
                reason = "ACP runtime does not support loading the saved session"
                if not capabilities.get("loadSession"):
                    prompt = _restart_prompt(con, run, pending, reason)
            created = peer.call("session/new", {"cwd": run["workdir"],
                                                "mcpServers": []}) or {}
            session_ref = created.get("sessionId")
        if not session_ref:
            raise ExecutionError("ACP runtime created no session")
        con.execute("UPDATE runs SET session_ref=? WHERE id=?",
                    (session_ref, run_id))
        con.commit()
        if profile.get("model"):
            try:
                peer.call("session/set_model", {"sessionId": session_ref,
                                                "modelId": profile["model"]})
            except acp.AcpError:
                pass
        last_rx, last_progress = peer.last_rx, time.monotonic()
        steer_method = (_json(runtime_snapshot.get("capabilities"))
                        .get("steer_method"))
        while True:
            turn = peer.request("session/prompt", {
                "sessionId": session_ref,
                "prompt": [{"type": "text", "text": prompt}],
            })
            _claim_exact(con, run_id, pending)
            while True:
                frame = peer.response(turn)
                if frame is not None:
                    _acp_result("session/prompt", frame)
                    break
                traces.ingest(con, run_id, run["log_path"], "acp")
                child_runs.process_pending(con, run_id)
                current = con.execute(
                    "SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
                queued = _pending(con, run_id)
                interrupts = [row for row in queued if row["kind"] == "interrupt"]
                if current and (current["status"] in TERMINAL or
                                current["status"] == "waiting" or interrupts):
                    peer.notify("session/cancel", {"sessionId": session_ref})
                    _acp_drain(peer, turn)
                    if current["status"] in TERMINAL:
                        return current["status"], None
                    if current["status"] == "waiting":
                        return "suspend", None
                    prompt, pending = _prompt(con, runs.find(con, run_id), queued), queued
                    break
                tells = [row for row in queued if row["kind"] == "tell"]
                if tells and isinstance(steer_method, str) and steer_method:
                    try:
                        peer.call(steer_method, {
                            "sessionId": session_ref,
                            "prompt": [{"type": "text",
                                        "text": messaging.render_delivery(tells)}],
                        }, timeout=30)
                    except acp.AcpError:
                        pass
                    else:
                        _claim_exact(con, run_id, tells)
                now = time.monotonic()
                if peer.last_rx > last_rx:
                    last_rx, last_progress = peer.last_rx, time.monotonic()
                if now - started >= timeout or (
                        stall_timeout and now - last_progress >= stall_timeout):
                    peer.notify("session/cancel", {"sessionId": session_ref})
                    _acp_drain(peer, turn)
                    return "timed_out", None
                time.sleep(POLL_INTERVAL)
            queued = _pending(con, run_id)
            if queued:
                prompt, pending = _prompt(con, runs.find(con, run_id), queued), queued
                continue
            return "completed", 0
    except acp.AcpError as exc:
        current = runs.find(con, run_id)
        if current is not None and current["status"] in TERMINAL:
            return current["status"], None
        if current is not None and current["status"] == "waiting":
            return "suspend", None
        con.execute("UPDATE runs SET summary=? WHERE id=?",
                    (f"ACP runtime failed: {exc}"[:2000], run_id))
        con.commit()
        return "failed", None
    finally:
        if peer.proc is not None and peer.proc.poll() is None:
            terminate_group(peer.proc.pid)
        peer.close()
        traces.drain(con, run_id, run["log_path"], "acp")


def _checkpoint(run: dict, status: str) -> str | None:
    if not run.get("branch"):
        return None
    workdir = Path(run["workdir"])
    if not _is_repo(workdir):
        return None
    if worktree.status(workdir):
        pathspec = ["."]
        for name in worktree.untracked_context_paths(workdir):
            pathspec += [f":(exclude){name}", f":(exclude){name}/**"]
        added = subprocess.run(
            ["git", "-C", str(workdir), "add", "-A", "--", *pathspec],
            capture_output=True, text=True, timeout=60)
        if added.returncode != 0:
            raise ExecutionError(
                f"checkpoint staging failed: {added.stderr.strip()}")
        staged = subprocess.run(
            ["git", "-C", str(workdir), "diff", "--cached", "--quiet"],
            timeout=60)
        if staged.returncode not in (0, 1):
            raise ExecutionError("cannot inspect staged checkpoint")
        if staged.returncode == 1:
            committed = subprocess.run(
                ["git", "-C", str(workdir), "-c", "user.name=Orchestra",
                 "-c", "user.email=orchestra@localhost",
                 "-c", "commit.gpgSign=false", "commit", "--no-verify", "-m",
                 f"orchestra: checkpoint run {run['id']} ({status})"],
                capture_output=True, text=True, timeout=60)
            if committed.returncode != 0:
                raise ExecutionError(
                    f"checkpoint commit failed: {committed.stderr.strip()}")
    return worktree.head(workdir)


def _write_patch(run: dict, head: str | None) -> str | None:
    workdir = Path(run["workdir"])
    base = run.get("base_commit")
    if not base or not head or not _is_repo(workdir):
        return None
    target = paths.run_dir(int(run["id"])) / "git.patch"
    command = ["git", "-C", str(workdir), "diff", "--binary", "--no-ext-diff"]
    command += [f"{base}..{head}"] if run.get("branch") else [base]
    with open(target, "wb") as output:
        process = subprocess.Popen(command, stdout=output, stderr=subprocess.PIPE,
                                   **session_kwargs())
        try:
            _, error = process.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            terminate_group(process.pid, force=True)
            process.wait()
            raise ExecutionError("git patch capture timed out")
    if process.returncode != 0:
        raise ExecutionError(
            f"git patch capture failed: {(error or b'').decode(errors='replace')[:500]}")
    size = target.stat().st_size
    if size > MAX_DIFF_BYTES:
        marker = b"\n# Orchestra: patch truncated at 16 MiB\n"
        with open(target, "r+b") as output:
            output.truncate(MAX_DIFF_BYTES - len(marker))
            output.seek(0, os.SEEK_END)
            output.write(marker)
    target.chmod(0o600)
    return str(target)


def _capture_evidence(con: sqlite3.Connection, run_id: int,
                      status: str) -> str | None:
    run = dict(runs.find(con, run_id))
    if not _is_repo(Path(run["workdir"])):
        return None
    try:
        checkpoint = _checkpoint(run, status)
        head = checkpoint or _git_head(Path(run["workdir"]))
        patch = _write_patch(run, head)
        con.execute(
            "UPDATE runs SET checkpoint_commit=COALESCE(?,checkpoint_commit),"
            "head_commit=COALESCE(?,head_commit),diff_path=COALESCE(?,diff_path) "
            "WHERE id=?", (checkpoint, head, patch, run_id))
        con.commit()
        return None
    except (ExecutionError, OSError, subprocess.SubprocessError) as exc:
        return f"Git evidence capture failed; worktree retained: {exc}"


def _release_worktree(con: sqlite3.Connection, run_id: int) -> str | None:
    run = dict(runs.find(con, run_id))
    if not run.get("branch"):
        return None
    location = _repo_root(Path(run["workdir"])) or Path(run["workdir"])
    root = worktree.main_root(location)
    if root is None or root == location.resolve():
        return None
    if worktree.live_holders(con, location, ignore_run=run_id):
        return None
    report = worktree.remove(location, root, branch=run["branch"])
    if report["kept"]:
        return f"Worktree retained at {location}: {report['kept']}"
    if not report["removed"]:
        return f"Worktree could not be released: {report['error']}"
    return None


def _usage(con: sqlite3.Connection, run) -> None:
    _, runtime_snapshot = _snapshots(run)
    adapter = runtime_snapshot.get("adapter", "")
    profile_snapshot, _ = _snapshots(run)
    value = runners.parse_usage(
        run["log_path"], adapter, profile_snapshot.get("model"))
    plan_provider = runway.provider_of(adapter, profile_snapshot.get("model"))
    cost_sql = "?" if runway.kind_of(plan_provider) == "plan" else "COALESCE(?,cost_usd)"
    con.execute(
        "UPDATE runs SET tokens_in=COALESCE(?,tokens_in),"
        "tokens_out=COALESCE(?,tokens_out),tokens_total=COALESCE(?,tokens_total),"
        "tokens_cache_read=COALESCE(?,tokens_cache_read),"
        "tokens_cache_write=COALESCE(?,tokens_cache_write),"
        f"cost_usd={cost_sql},usage_source=COALESCE(?,usage_source) "
        "WHERE id=?", (value["tokens_in"], value["tokens_out"],
                       value["tokens_total"], value["tokens_cache_read"],
                       value["tokens_cache_write"], value["cost_usd"],
                       value["usage_source"], int(run["id"])))


def _result_summary(run, status: str) -> str:
    _, text = runners.parse_log(run["log_path"])
    if status == "completed":
        return (text or run["summary"] or "Completed.")[:4000]
    if status == "timed_out":
        return (run["summary"] or "Run exceeded its configured time limit.")[:4000]
    return (run["summary"] or runners.parse_failure(run["log_path"])
            or f"Run ended {status}.")[:4000]


def _completion_message(con: sqlite3.Connection, run) -> bool:
    if con.execute(
        "SELECT 1 FROM messages WHERE run_id=? AND kind='completion' LIMIT 1",
        (int(run["id"]),)).fetchone():
        return False
    messaging.post(
        con, int(run["id"]), direction="system", sender="orchestra",
        body=f"{db.run_no(run)} ended {run['status']}.\n\n{run['summary'] or ''}",
        kind="completion", status="delivered")
    return True


def _after_terminal(con: sqlite3.Connection, run_id: int) -> None:
    run = runs.find(con, run_id)
    if run is None or run["status"] not in TERMINAL:
        return
    first = _completion_message(con, run)
    messaging.mark_undeliverable(
        con, run_id, f"run ended {run['status']} before delivery")
    child_runs.fail_unprocessed(
        con, run_id, f"run ended {run['status']} before delegation was admitted")
    for request in con.execute(
            "SELECT id FROM attention_requests WHERE run_id=? AND status='open' "
            "AND blocking=1 ORDER BY id", (run_id,)).fetchall():
        attention.cancel(
            con, int(request["id"]), actor="orchestra:terminal",
            reason=f"run ended {run['status']}", notify_run=False)
    if not first:
        return
    callbacks.emit(config.callback_command(), "run.terminal", {
        "run_id": run_id, "status": run["status"],
        "group_id": run["group_id"], "group_seq": run["group_seq"],
        "summary": run["summary"],
    }, audit_db=con)
    decision = retry.decide(
        run["status"], run["summary"],
        automatic_retries=max(0, int(run["attempt"] or 1) - 1))
    if decision["action"] == "retry":
        runs.clone(
            con, run_id, request_id=f"auto-retry:{run_id}", kind="retry",
            requested_by="orchestra:retry",
            not_before=(datetime.now(timezone.utc) + timedelta(seconds=5))
            .strftime("%Y-%m-%dT%H:%M:%SZ"))
    elif decision["action"] == "alert":
        attention.open_request(
            con, kind="alert", run_id=run_id,
            title=f"{db.run_no(run)} needs attention",
            body=decision["reason"] + (f"\n\n{run['summary']}" if run["summary"] else ""),
            created_by="orchestra:retry",
            correlation_id=f"terminal-alert:{run_id}",
            callback_command=config.callback_command())


def finalize_run(con: sqlite3.Connection, run, status: str,
                 exit_code: int | None = None, *, summary: str | None = None) -> dict:
    """Persist one terminal outcome, checkpoint evidence, and retry policy."""
    if status not in TERMINAL:
        raise ValueError(f"invalid terminal status: {status}")
    run_id = int(run["id"])
    current = runs.find(con, run_id)
    if current is None:
        raise ExecutionError(f"no run {run_id}")
    traces.drain(con, run_id)
    _set_session(con, run_id, current["log_path"])
    evidence_note = _capture_evidence(con, run_id, status)
    current = runs.find(con, run_id)
    result_summary = summary or _result_summary(current, status)
    if evidence_note:
        result_summary = f"{result_summary}\n\n{evidence_note}"[:4000]
    with con:
        _usage(con, current)
        con.execute(
            "UPDATE runs SET status=?,waiting_kind=NULL,hold_reason=NULL,summary=?,"
            "exit_code=?,worker_status=?,worker_exit_code=?,finished_at=COALESCE("
            "finished_at,?),pid=NULL,pid_identity=NULL,supervisor_pid=NULL,"
            "supervisor_pid_identity=NULL,run_token_hash=NULL WHERE id=?",
            (status, result_summary, exit_code, status, exit_code, db.now(), run_id))
    release_note = None if evidence_note else _release_worktree(con, run_id)
    if release_note:
        con.execute("UPDATE runs SET summary=substr(summary || ?,1,4000) WHERE id=?",
                    ("\n\n" + release_note, run_id))
        con.commit()
    _after_terminal(con, run_id)
    return dict(runs.find(con, run_id))


def _wait_for_children(con: sqlite3.Connection, run, exit_code: int | None) -> bool:
    run_id = int(run["id"])
    generation = child_runs.result_generation(con, run_id)
    if generation is None:
        return False
    delivered = con.execute(
        "SELECT 1 FROM messages WHERE run_id=? AND kind='child_results' "
        "AND correlation_id=? AND status='delivered' LIMIT 1",
        (run_id, f"children:{run_id}:{generation}"),
    ).fetchone()
    if delivered is not None:
        return False
    evidence_note = _capture_evidence(con, run_id, "waiting")
    summary = _result_summary(run, "completed")
    if evidence_note:
        summary = f"{summary}\n\n{evidence_note}"[:4000]
    with con:
        _usage(con, run)
        con.execute(
            "UPDATE runs SET status='waiting',waiting_kind='children',summary=?,"
            "worker_status='completed',worker_exit_code=?,pid=NULL,pid_identity=NULL,"
            "supervisor_pid=NULL,supervisor_pid_identity=NULL,run_token_hash=NULL "
            "WHERE id=? AND status='running'",
            (summary, exit_code, run_id))
    if not evidence_note:
        _release_worktree(con, run_id)
    return True


def _suspend(con: sqlite3.Connection, run_id: int) -> None:
    note = _capture_evidence(con, run_id, "waiting")
    with con:
        con.execute(
            "UPDATE runs SET pid=NULL,pid_identity=NULL,supervisor_pid=NULL,"
            "supervisor_pid_identity=NULL,run_token_hash=NULL WHERE id=? "
            "AND status='waiting'", (run_id,))
        if note:
            con.execute(
                "UPDATE runs SET summary=substr(COALESCE(summary || char(10) || "
                "char(10),'') || ?,1,4000) WHERE id=?", (note, run_id))
    if not note:
        _release_worktree(con, run_id)


def _limits(profile: dict) -> tuple[int, int | None]:
    raw_timeout = profile.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS
    try:
        timeout = int(raw_timeout)
    except (TypeError, ValueError) as exc:
        raise ExecutionError("profile timeout_seconds must be an integer") from exc
    if timeout <= 0:
        raise ExecutionError("profile timeout_seconds must be positive")
    profile_config = _json(profile.get("config"))
    raw_stall = profile_config.get("stall_timeout_seconds")
    if raw_stall in (None, 0, False):
        return timeout, None
    try:
        stall = int(raw_stall)
    except (TypeError, ValueError) as exc:
        raise ExecutionError("stall_timeout_seconds must be an integer") from exc
    if stall < 0:
        raise ExecutionError("stall_timeout_seconds cannot be negative")
    return timeout, stall or None


def _claim_supervisor(con: sqlite3.Connection, run_id: int) -> bool:
    pid = os.getpid()
    current = con.execute(
        "SELECT supervisor_pid,supervisor_pid_identity,status FROM runs WHERE id=?",
        (run_id,),).fetchone()
    if current is None or current["status"] != "starting":
        return False
    if current["supervisor_pid"] not in (None, pid):
        return False
    identity = _identity(pid) or current["supervisor_pid_identity"]
    changed = con.execute(
        "UPDATE runs SET supervisor_pid=?,supervisor_pid_identity=? WHERE id=? "
        "AND status='starting' AND (supervisor_pid IS NULL OR supervisor_pid=?)",
        (pid, identity, run_id, pid),)
    con.commit()
    return changed.rowcount == 1


def supervise(root: Path, run_id: int) -> int:
    """Execute an admitted run until it waits or reaches a terminal state."""
    del root  # the run's frozen workdir/repo fields, never a caller path, win
    raise_file_limit()
    con = db.connect()
    try:
        if not _claim_supervisor(con, int(run_id)):
            return 1
        run = _prepare_workdir(con, runs.find(con, int(run_id)))
        profile, runtime_snapshot = _snapshots(run)
        timeout, stall_timeout = _limits(profile)
        token = auth.mint_run(con, int(run_id))
        base_env = {
            key: value for key, value in enrich_path(
                config.worker_environment()).items()
            if not key.startswith("ORCHESTRA_")
        }
        base_env.update({
            "ORCHESTRA_RUN_ID": str(int(run_id)),
            "ORCHESTRA_RUN_TOKEN": token,
            "ORCHESTRA_URL": config.api_url(),
        })
        while True:
            run = runs.find(con, int(run_id))
            pending = _pending(con, int(run_id))
            can_resume = _runtime_can_resume(runtime_snapshot)
            if run["session_ref"] and pending and not can_resume:
                prompt = _restart_prompt(
                    con, run, pending,
                    "the configured runtime cannot resume its saved session")
                con.execute("UPDATE runs SET session_ref=NULL WHERE id=?",
                            (int(run_id),))
                con.commit()
                run = runs.find(con, int(run_id))
            else:
                prompt = _prompt(con, run, pending)
            plan = runtime.launch_plan(
                runtime_snapshot, profile, workdir=run["workdir"],
                title=run["title"] or f"orchestra-run-{run_id}", prompt=prompt,
                run_id=int(run_id), session_ref=run["session_ref"],
                inherited_env=base_env)
            if plan.adapter == "acp":
                outcome, exit_code = _run_acp(
                    con, run, plan, prompt, pending, timeout, stall_timeout,
                    profile, runtime_snapshot)
            else:
                outcome, exit_code = _run_exec_turn(
                    con, run, plan, pending, timeout, stall_timeout)
            run = runs.find(con, int(run_id))
            if outcome == "resume":
                con.execute(
                    "UPDATE runs SET status='starting',pid=NULL,pid_identity=NULL "
                    "WHERE id=? AND status='running'", (int(run_id),))
                con.commit()
                continue
            if outcome == "suspend":
                _suspend(con, int(run_id))
                return 0
            if outcome == "completed" and _wait_for_children(con, run, exit_code):
                return 0
            if outcome not in TERMINAL:
                outcome = "failed"
            result = finalize_run(con, run, outcome, exit_code)
            return 0 if result["status"] == "completed" else 1
    except BaseException as exc:
        run = runs.find(con, int(run_id))
        if run is not None and run["status"] not in TERMINAL:
            finalize_run(con, run, "failed", None,
                         summary=f"Execution failed: {exc}"[:4000])
        print(f"orchestra: run {run_id}: {exc}", file=sys.stderr)
        return 1
    finally:
        con.close()


def never_started(run) -> bool:
    return bool(run and run["status"] == "failed" and run["pid"] is None
                and run["session_ref"] is None)
