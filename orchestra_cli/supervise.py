"""Detached supervisor: runs one worker process, tracks it, reports back.

Messaging semantics it enforces:
- `orchestra interrupt` records a pending delivery and waits for a completed
  backend action before stopping the worker. `--now` retains the immediate
  stop path. Both RESUME the same session with an instruction to read the
  inbox, so delivery to a running worker is guaranteed (not best-effort).
- A run that finishes with unread inbox messages bounces a notice back to each
  sender — a message to a worker can never rot silently.
"""
import os
import json
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from orchestra_cli import config, db, host, paths, runners
from orchestra_cli.usage import DEFAULT_COLLECTORS, infer_from_agent

EARLY_REF_WINDOW = 90  # seconds to keep scanning the log for a session ref
PROC_POLL_INTERVAL = 2
CONTROL_POLL_INTERVAL = 0.1
DEFAULT_CHECKIN_INTERVAL = 600
MIN_CHECKIN_INTERVAL = 1
MAX_CHECKIN_INTERVAL = 3600

_USAGE_LIMIT_RE = re.compile(
    r"(?i)("
    r"(usage|quota|credit|token|rate)[\w\s-]{0,40}(exhausted|exceeded|reached|depleted)|"
    r"(exhausted|exceeded|reached|depleted|insufficient|out of)[\w\s-]{0,40}"
    r"(usage|quota|credit|token|rate limit)|"
    r"(monthly|daily|weekly)[\w\s-]{0,20}(limit|quota)[\w\s-]{0,30}"
    r"(exhausted|exceeded|reached)"
    r")"
)
_RAW_ERROR_LINE_RE = re.compile(r"(?i)^\s*(error|fatal|provider error|api error)\b")


def spawn_supervisor(root: Path, run_id: int) -> None:
    exe = shutil.which("orchestra")
    cmd = [exe, "_supervise", str(run_id), "--root", str(root)] if exe else \
        [sys.executable, "-m", "orchestra_cli", "_supervise", str(run_id), "--root", str(root)]
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def create_followup(con, root: Path, parent: dict, requester: str, text: str,
                    title: str | None = None, *, commit: bool = True) -> int:
    """New run row that resumes parent's session with `text` as the prompt."""
    cur = con.execute(
        "INSERT INTO runs(agent, backend, model, title, work_item, team, requested_by, "
        "workdir, branch, parent_run, lead_run, child_depth, session_ref, status, started_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?, 'spawning', ?)",
        (parent["agent"], parent["backend"], parent["model"],
         title or f"follow-up to run {parent['id']}", parent["work_item"], parent["team"],
         requester, parent["workdir"], parent["branch"], parent["id"],
         parent.get("lead_run"), parent.get("child_depth", 0), parent["session_ref"], db.now()))
    run_id = cur.lastrowid
    bp = paths.briefs_dir(root) / f"run-{run_id}.md"
    bp.write_text(text)
    lp = paths.logs_dir(root) / f"run-{run_id}.jsonl"
    lp.touch()
    con.execute("UPDATE runs SET brief_path=?, log_path=? WHERE id=?",
                (str(bp), str(lp), run_id))
    if commit:
        con.commit()
    return run_id


def _work_log(root: Path, item: str, text: str) -> None:
    if item and shutil.which("work"):
        try:
            subprocess.run(["work", "log", item, text], cwd=root,
                           capture_output=True, timeout=20)
        except Exception:
            pass


def _ts_to_epoch(ts: str) -> float:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def _wait_for_question(con, run, *, poll_interval: float = PROC_POLL_INTERVAL) -> tuple[dict | None, float]:
    """Wait cheaply for a human answer or atomically apply the declared fallback."""
    started = time.time()
    while True:
        current = con.execute("SELECT status FROM runs WHERE id=?", (run["id"],)).fetchone()
        if not current or current["status"] == "killed":
            return None, time.time() - started
        question = con.execute(
            "SELECT * FROM questions WHERE run_id=?", (run["id"],)
        ).fetchone()
        if not question:
            return None, time.time() - started
        if question["status"] != "waiting":
            return dict(question), time.time() - started
        if time.time() >= _ts_to_epoch(question["deadline_at"]):
            resolved_at = db.now()
            con.execute("BEGIN IMMEDIATE")
            try:
                fresh = con.execute(
                    "SELECT * FROM questions WHERE run_id=?", (run["id"],)
                ).fetchone()
                if fresh and fresh["status"] == "waiting":
                    con.execute(
                        "UPDATE questions SET status='defaulted', answered_at=?, "
                        "answered_by='orchestra', answer=recommended_default "
                        "WHERE run_id=? AND status='waiting'",
                        (resolved_at, run["id"]),
                    )
                    con.execute(
                        "UPDATE messages SET read_at=COALESCE(read_at, ?) "
                        "WHERE run_id=? AND kind='question'",
                        (resolved_at, run["id"]),
                    )
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise
            continue
        time.sleep(poll_interval)


def _checkin_interval_seconds(raw) -> int | None:
    if raw is False or raw is None:
        return None
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        seconds = DEFAULT_CHECKIN_INTERVAL
    if seconds <= 0:
        return None
    return min(max(seconds, MIN_CHECKIN_INTERVAL), MAX_CHECKIN_INTERVAL)


def _terminate_process_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
        return
    except ProcessLookupError:
        return
    except Exception:
        pass
    try:
        os.killpg(pid, signal.SIGKILL)
    except Exception:
        pass


def _wait_after_term(proc: subprocess.Popen, timeout: float = 15) -> None:
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass
        try:
            proc.wait(timeout=2)
        except Exception:
            pass


def _json_strings(obj, *, keys: set[str] | None = None) -> list[str]:
    if isinstance(obj, dict):
        out = []
        for k, v in obj.items():
            if keys is None or k in keys:
                out.extend(_json_strings(v))
            elif isinstance(v, (dict, list)):
                out.extend(_json_strings(v, keys=keys))
        return out
    if isinstance(obj, list):
        out = []
        for v in obj:
            out.extend(_json_strings(v, keys=keys))
        return out
    if isinstance(obj, str):
        return [obj]
    return []


def _is_error_event(obj: dict) -> bool:
    marker_keys = {"type", "event", "level", "status", "kind", "subtype"}
    for key in marker_keys:
        value = obj.get(key)
        if isinstance(value, str) and value.lower() in {
            "error", "fatal", "failed", "failure", "exception"
        }:
            return True
    if "error" in obj:
        return True
    nested = obj.get("data") or obj.get("payload")
    return isinstance(nested, dict) and _is_error_event(nested)


def _error_event_strings(obj) -> list[str]:
    if not isinstance(obj, dict) or not _is_error_event(obj):
        return []
    return _json_strings(
        obj,
        keys={"error", "message", "detail", "details", "reason", "description", "result"},
    )


def _usage_limit_text(log_path: str, *, max_bytes: int = 262144) -> str | None:
    try:
        with open(log_path, "rb") as f:
            try:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - max_bytes))
            except OSError:
                pass
            lines = f.read(max_bytes).decode(errors="replace").splitlines()
    except OSError:
        return None

    for line in reversed(lines):
        text = line.strip()
        if not text:
            continue
        candidates = [text] if _RAW_ERROR_LINE_RE.search(text) else []
        if text.startswith("{"):
            try:
                obj = json.loads(text)
            except ValueError:
                obj = None
            if obj is not None:
                candidates = _error_event_strings(obj)
        for candidate in candidates:
            compact = " ".join(candidate.split())
            if compact and _USAGE_LIMIT_RE.search(compact):
                return compact[:800]
    return None


def _insert_checkin_message(con, run, run_id: int) -> dict:
    created_at = db.now()
    body = (
        f"PROGRESS CHECK-IN run {run_id}: send {run['requested_by']} a short progress "
        f"update with `orchestra send {run['requested_by']} \"PROGRESS run {run_id}: ...\" "
        f"--as {run['agent']} --run {run_id}`, then continue the original mission."
    )
    if run["work_item"]:
        body += f" Also record durable progress with `work log {run['work_item']} ...`."
    cur = con.execute(
        "INSERT INTO messages(sender, recipient, body, work_item, run_id, kind, created_at) "
        "VALUES('orchestra', ?, ?, ?, ?, 'checkin', ?)",
        (run["agent"], body, run["work_item"], run_id, created_at),
    )
    return {
        "message_id": int(cur.lastrowid),
        "delivery": "checkin",
        "sender": "orchestra",
        "recipient": run["agent"],
        "body": body,
        "created_at": created_at,
        "phase": "pending",
    }


def _write_json_event(log, event: dict) -> int:
    payload = dict(event)
    event_type = payload.pop("type", "orchestra.delivery")
    log.write((json.dumps({"type": event_type, **payload},
                          ensure_ascii=False, separators=(",", ":")) + "\n").encode())
    log.flush()
    return os.fstat(log.fileno()).st_size


def append_delivery_event(log_path: str | None, event: dict) -> int | None:
    """Place a delivery marker at its actual point in the runner timeline.

    The SQLite row remains authoritative. A failed append therefore does not
    lose the message; the UI falls back to the row, albeit without exact
    transcript placement.
    """
    if not log_path:
        return None
    try:
        with open(log_path, "ab") as log:
            return _write_json_event(log, event)
    except OSError:
        return None


def _recent_logged_delivery_ids(log_path: str | None, max_bytes: int = 1_000_000) -> set[int]:
    """Return delivery IDs from a bounded tail scan of a worker log."""
    if not log_path:
        return set()
    try:
        with open(log_path, "rb") as log:
            log.seek(0, os.SEEK_END)
            size = log.tell()
            start = max(0, size - max_bytes)
            log.seek(start)
            if start:
                log.readline()  # discard a possibly partial first line
            lines = log.readlines()
    except OSError:
        return set()
    ids = set()
    for raw in lines:
        try:
            event = json.loads(raw)
        except (UnicodeDecodeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") != "orchestra.delivery":
            continue
        try:
            ids.add(int(event["message_id"]))
        except (KeyError, TypeError, ValueError):
            continue
    return ids


def _pending_delivery_offset(con, run_id: int) -> int | None:
    row = con.execute(
        "SELECT MAX(delivery_offset) AS offset FROM messages "
        "WHERE run_id=? AND kind IN ('interrupt','checkin') "
        "AND delivery_offset IS NOT NULL AND delivered_at IS NULL",
        (run_id,),
    ).fetchone()
    return int(row["offset"]) if row and row["offset"] is not None else None


def _read_log_events(log_path: str, offset: int,
                     max_bytes: int = 4_000_000) -> tuple[list[dict], int]:
    """Read complete JSONL events after ``offset`` without consuming a partial line."""
    try:
        with open(log_path, "rb") as source:
            source.seek(offset)
            data = source.read(max_bytes)
    except OSError:
        return [], offset
    end = data.rfind(b"\n")
    if end < 0:
        # Do not let one unusually large JSON event stall boundary watching
        # forever. Skipping its first bounded chunk may delay delivery until
        # the following boundary, but it never makes an unsafe interruption.
        return [], offset + len(data) if len(data) == max_bytes else offset
    events = []
    for raw in data[:end].splitlines():
        try:
            event = json.loads(raw)
        except (UnicodeDecodeError, ValueError):
            continue
        if isinstance(event, dict):
            events.append(event)
    return events, offset + end + 1


def _is_safe_boundary(backend: str, event: dict) -> bool:
    """Recognize a completed action boundary in each runner's JSONL protocol."""
    event_type = event.get("type")
    part = event.get("part") or {}
    if backend == "opencode":
        return event_type == "step_finish" or part.get("type") == "step-finish"
    if backend == "codex":
        item = event.get("item") or {}
        return event_type == "item.completed" and item.get("type") in {
            "command_execution", "file_change", "patch", "mcp_tool_call", "web_search",
        }
    if backend == "claude" and event_type == "user":
        content = (event.get("message") or {}).get("content") or []
        return any(isinstance(item, dict) and item.get("type") == "tool_result"
                   for item in content)
    return False


def _delivery_event_from_message(message, *, phase: str) -> dict:
    kind = "checkin" if message["kind"] == "checkin" else "interrupt"
    body = message["body"]
    if kind == "interrupt" and body.startswith("[INTERRUPT]"):
        body = body.removeprefix("[INTERRUPT]").lstrip()
    return {
        "message_id": int(message["id"]),
        "delivery": kind,
        "sender": message["sender"],
        "recipient": message["recipient"],
        "body": body,
        "created_at": message["created_at"],
        "phase": phase,
    }


def _mark_pending_delivered(con, run_id: int) -> list[dict]:
    rows = list(con.execute(
        "SELECT id, sender, recipient, body, kind, created_at FROM messages "
        "WHERE run_id=? AND kind IN ('interrupt','checkin') "
        "AND delivery_offset IS NOT NULL AND delivered_at IS NULL ORDER BY id",
        (run_id,),
    ))
    if not rows:
        return []
    delivered_at = db.now()
    con.execute(
        "UPDATE messages SET delivered_at=? WHERE run_id=? "
        "AND kind IN ('interrupt','checkin') "
        "AND delivery_offset IS NOT NULL AND delivered_at IS NULL",
        (delivered_at, run_id),
    )
    return [_delivery_event_from_message(row, phase="delivered") for row in rows]


def _quota_exhausted_text(agent: dict) -> str | None:
    provider_id = infer_from_agent(agent)
    if not provider_id:
        return None
    for candidate_id, provider_name, collector in DEFAULT_COLLECTORS:
        if candidate_id != provider_id:
            continue
        try:
            result = collector()
        except Exception:
            return None
        provider = result.to_dict() if hasattr(result, "to_dict") else result
        if not isinstance(provider, dict) or provider.get("status") != "ok":
            return None
        headroom = provider.get("headroom_percent")
        if not isinstance(headroom, (int, float)) or float(headroom) > 0:
            return None
        name = provider.get("name") or provider_name or provider_id
        return f"{name} coding headroom is {float(headroom):.0f}%"
    return None


def _command_preview(cmd: list[str]) -> str:
    """Return a short log-safe runner command without embedding Claude's prompt."""
    preview = list(cmd[:6])
    if preview[:2] == ["claude", "-p"] and len(preview) > 2:
        preview[2] = "<prompt>"
    return " ".join(preview) + " ..."


def _run_proc(con, run, cmd, workdir, env, log_path, run_id, deadline, *,
              agent: dict | None = None,
              checkin_interval: int | None = None,
              checkin_state: dict | None = None,
              delivery_events: list[dict] | None = None,
              poll_interval: float = PROC_POLL_INTERVAL) -> tuple[str, int | None]:
    """Start one worker process; wait with timeout + early session-ref capture.
    Returns (outcome, exit_code) where outcome is
    'exit'|'timeout'|'usage_limit'|'waiting_input'."""
    with open(log_path, "ab") as log:
        latest = con.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        if latest and latest["status"] in db.RUN_TERMINAL:
            return "exit", None
        for event in delivery_events or []:
            _write_json_event(log, event)
        log.write((_command_preview(cmd) + "\n").encode())
        log.flush()
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT,
                                cwd=workdir, env=env, start_new_session=True)
        cur = con.execute(
            "UPDATE runs SET pid=?, status='running' "
            "WHERE id=? AND status NOT IN ('done','failed','timeout','killed')",
            (proc.pid, run_id))
        con.commit()
        if cur.rowcount == 0:
            latest = con.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
            if latest and latest["status"] in db.RUN_TERMINAL:
                _terminate_process_group(proc.pid)
                _wait_after_term(proc)
            return "exit", proc.poll()
        started = time.time()
        next_health_check = started
        have_ref = bool(run["session_ref"])
        pending_after: int | None = None
        boundary_scan_offset = 0
        while True:
            try:
                exit_code = proc.wait(timeout=max(0.01, min(CONTROL_POLL_INTERVAL,
                                                            poll_interval)))
                break
            except subprocess.TimeoutExpired:
                pass
            now = time.time()
            latest = con.execute("SELECT status, session_ref FROM runs WHERE id=?", (run_id,)).fetchone()
            if latest and latest["status"] == "waiting_input":
                _terminate_process_group(proc.pid)
                _wait_after_term(proc)
                return "waiting_input", None
            if latest and latest["status"] == "interrupt":
                _terminate_process_group(proc.pid)
                _wait_after_term(proc)
                return "exit", proc.poll()
            if latest and latest["status"] in db.RUN_TERMINAL:
                _terminate_process_group(proc.pid)
                _wait_after_term(proc)
                return "exit", proc.poll()
            if latest and latest["session_ref"]:
                have_ref = True

            pending_offset = _pending_delivery_offset(con, run_id)
            if pending_offset is not None:
                if pending_after != pending_offset:
                    pending_after = pending_offset
                    boundary_scan_offset = pending_offset
                events, boundary_scan_offset = _read_log_events(
                    log_path, boundary_scan_offset
                )
                if any(_is_safe_boundary(run["backend"], event) for event in events):
                    delivered = _mark_pending_delivered(con, run_id)
                    con.execute("UPDATE runs SET status='interrupt' WHERE id=?", (run_id,))
                    con.commit()
                    for event in delivered:
                        _write_json_event(log, event)
                    if checkin_state is not None:
                        checkin_state["last_sent_at"] = now
                    _terminate_process_group(proc.pid)
                    _wait_after_term(proc)
                    return "exit", proc.poll()
            else:
                pending_after = None

            if now >= next_health_check:
                next_health_check = now + max(0.05, poll_interval)
                if not have_ref and now - started < EARLY_REF_WINDOW:
                    ref, _ = runners.parse_log(log_path, max_bytes=65536)
                    if ref:
                        con.execute("UPDATE runs SET session_ref=? WHERE id=?", (ref, run_id))
                        con.commit()
                        have_ref = True
                usage_text = _usage_limit_text(log_path)
                if usage_text:
                    con.execute(
                        "UPDATE runs SET summary=? WHERE id=?",
                        (f"Provider usage limit exhausted: {usage_text}", run_id),
                    )
                    con.commit()
                    _terminate_process_group(proc.pid)
                    _wait_after_term(proc)
                    return "usage_limit", None
            if checkin_interval and have_ref and checkin_state is not None:
                last_sent_at = float(checkin_state.get("last_sent_at") or started)
                if now - last_sent_at >= checkin_interval and pending_offset is None:
                    latest_run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
                    if latest_run and latest_run["status"] == "running":
                        quota_text = _quota_exhausted_text(agent or {})
                        if quota_text:
                            con.execute(
                                "UPDATE runs SET summary=? WHERE id=?",
                                (f"Provider usage limit exhausted: {quota_text}", run_id),
                            )
                            con.commit()
                            checkin_state["last_sent_at"] = time.time()
                            _terminate_process_group(proc.pid)
                            _wait_after_term(proc)
                            return "usage_limit", None
                        checkin_event = _insert_checkin_message(con, latest_run, run_id)
                        delivery_offset = _write_json_event(log, checkin_event)
                        con.execute(
                            "UPDATE messages SET delivery_offset=? WHERE id=?",
                            (delivery_offset, checkin_event["message_id"]),
                        )
                        con.commit()
                        checkin_state["last_sent_at"] = now
            if now > deadline:
                _terminate_process_group(proc.pid)
                _wait_after_term(proc)
                return "timeout", None
        if exit_code not in (None, 0):
            usage_text = _usage_limit_text(log_path)
            if usage_text:
                con.execute(
                    "UPDATE runs SET summary=? WHERE id=?",
                    (f"Provider usage limit exhausted: {usage_text}", run_id),
                )
                con.commit()
                return "usage_limit", exit_code
        return "exit", exit_code


def supervise(root: Path, run_id: int) -> int:
    con = db.connect(root)
    run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not run:
        raise SystemExit(f"orchestra: run {run_id} not found")
    con.execute("UPDATE runs SET supervisor_protocol=1 WHERE id=?", (run_id,))
    con.commit()
    run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    cfg = config.load(root)
    agent = config.agent_cfg(cfg, run["agent"])
    timeout = int(agent.get("timeout") or cfg["settings"].get("timeout", 3600))
    checkin_interval = _checkin_interval_seconds(
        agent.get("supervisor_checkin_interval",
                  cfg["settings"].get("supervisor_checkin_interval", DEFAULT_CHECKIN_INTERVAL))
    )
    checkin_state = {"last_sent_at": _ts_to_epoch(run["started_at"])}
    deadline = _ts_to_epoch(run["started_at"]) + timeout

    prompt = Path(run["brief_path"]).read_text() if run["brief_path"] else run["title"]
    add_dirs = []
    if run["workdir"] != str(root):
        add_dirs.append(str(root))  # isolated runs still write .orchestra/.work at root
    attach = host.ensure() if agent.get("ensemble") else None

    status, exit_code = "done", None
    resume_ref = run["session_ref"] if run["parent_run"] else None
    delivery_events: list[dict] = []
    announced_message_ids: set[int] = set()
    while True:
        last_msg_file = None
        cmd = runners.build_cmd(agent, workdir=run["workdir"], title=f"orchestra-run-{run_id}",
                                prompt=prompt, resume_ref=resume_ref,
                                add_dirs=add_dirs, attach=attach)
        if agent["backend"] == "codex" and not resume_ref:
            last_msg_file = tempfile.NamedTemporaryFile(delete=False, suffix=".txt").name
            cmd = cmd[:2] + ["-o", last_msg_file] + cmd[2:]  # `codex exec -o FILE ...`

        env = config.apply_env_passthrough(
            cfg, dict(os.environ, ORCHESTRA_SELF=run["agent"], ORCHESTRA_ROOT=str(root),
                      ORCHESTRA_RUN_ID=str(run_id)))
        outcome, exit_code = _run_proc(con, run, cmd, run["workdir"], env,
                                       run["log_path"], run_id, deadline,
                                       agent=agent,
                                       checkin_interval=checkin_interval,
                                       checkin_state=checkin_state,
                                       delivery_events=delivery_events,
                                       poll_interval=PROC_POLL_INTERVAL)
        delivery_events = []
        run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()

        if outcome == "waiting_input" or run["status"] == "waiting_input":
            if not run["session_ref"]:
                status = "failed"
                break
            question, waited = _wait_for_question(con, run)
            deadline += waited
            checkin_state["last_sent_at"] = float(
                checkin_state.get("last_sent_at") or time.time()
            ) + waited
            latest = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if latest and latest["status"] == "killed":
                status = "killed"
                break
            if not question:
                status = "failed"
                break
            resume_ref = run["session_ref"]
            resolution = question["status"]
            answer = question["answer"] or question["recommended_default"]
            prompt = (
                f"Your one blocking question was {resolution}.\n\n"
                f"Question: {question['question']}\n"
                f"Recommended default: {question['recommended_default']}\n"
                f"Answer to apply: {answer}\n\n"
                "Continue the original mission now, applying that answer. Do not ask another "
                "blocking question; make reasonable assumptions and finish with the normal HANDOFF."
            )
            delivery_events = [{
                "type": "orchestra.question",
                "question_id": question["id"],
                "sender": question["sender"],
                "recipient": question["recipient"],
                "question": question["question"],
                "recommended_default": question["recommended_default"],
                "status": resolution,
                "answer": answer,
                "answered_by": question["answered_by"],
                "asked_at": question["asked_at"],
                "deadline_at": question["deadline_at"],
                "answered_at": question["answered_at"],
            }]
            con.execute("UPDATE runs SET status='running' WHERE id=?", (run_id,))
            con.commit()
            continue

        if outcome == "timeout":
            status = "timeout"
            break
        if outcome == "usage_limit":
            status = "failed"
            break
        if run["status"] == "killed":
            status = "killed"
            break
        pending_delivery = _pending_delivery_offset(con, run_id) is not None
        if run["status"] == "interrupt" or pending_delivery:
            # Resume the same session after an immediate stop, a safe boundary,
            # or a natural process exit that beat the next boundary.
            if not run["session_ref"]:
                status = "failed"  # can't resume; cli guards against this
                break
            resume_ref = run["session_ref"]
            claimed = con.execute(
                "UPDATE runs SET status='running' WHERE id=? AND status=?",
                (run_id, run["status"]),
            )
            con.commit()
            if claimed.rowcount != 1:
                latest = con.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
                status = latest["status"] if latest and latest["status"] in db.RUN_TERMINAL \
                    else "failed"
                break
            newly_delivered = _mark_pending_delivered(con, run_id)
            con.commit()
            for event in newly_delivered:
                append_delivery_event(run["log_path"], event)
            announced_message_ids.update(_recent_logged_delivery_ids(run["log_path"]))
            messages = con.execute(
                "SELECT id, sender, recipient, body, kind, created_at FROM messages "
                "WHERE run_id=? AND recipient=? "
                "AND (kind IN ('interrupt','checkin') OR body LIKE '[INTERRUPT]%') "
                "ORDER BY id",
                (run_id, run["agent"]),
            ).fetchall()
            for message in messages:
                message_id = int(message["id"])
                if message_id in announced_message_ids:
                    continue
                delivery_events.append(
                    _delivery_event_from_message(message, phase="delivered")
                )
                announced_message_ids.add(message_id)
            prompt = (f"The orchestrator delivered an in-flight message. "
                      f"IMMEDIATELY run `orchestra inbox {run['agent']} --unread --mark-read`, "
                      f"apply what it says, then continue your original mission. Finish with the "
                      f"normal HANDOFF to {run['requested_by']} "
                      f"(`orchestra send {run['requested_by']} \"HANDOFF run {run_id}: ...\" "
                      f"--as {run['agent']} --run {run_id}`).")
            continue
        status = "done" if exit_code == 0 else "failed"
        break

    handoff_body = None

    def _handoff():
        return con.execute(
            "SELECT body FROM messages WHERE sender=? AND created_at>=? "
            "AND (run_id=? OR body LIKE ?) ORDER BY id DESC LIMIT 1",
            (run["agent"], run["started_at"], run_id, f"HANDOFF run {run_id}:%")).fetchone()

    if agent.get("ensemble") and status != "killed":
        # attach mode: the mission may continue server-side after the client
        # exits (teammate wake-ups re-prompt the lead). Completion = HANDOFF.
        hard_deadline = max(deadline, time.time() + 120)
        while not _handoff() and time.time() < hard_deadline:
            cur = con.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
            if cur["status"] == "killed":
                break
            time.sleep(5)
        ho = _handoff()
        if ho:
            status, exit_code, handoff_body = "done", 0, ho["body"]
        elif status == "done":
            status = "timeout"

    latest = con.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
    if latest and latest["status"] == "killed":
        status = "killed"
        exit_code = None

    session_ref, last_text = runners.parse_log(run["log_path"])
    if status == "failed" and run["summary"] and "usage limit" in run["summary"].lower():
        last_text = (
            f"{run['summary']}\n\n"
            "The worker stopped because its provider quota appears exhausted. "
            "Resume this run after capacity resets or reroute the work to another agent."
        )
    if last_msg_file and Path(last_msg_file).is_file():
        txt = Path(last_msg_file).read_text(errors="replace").strip()
        if txt:
            last_text = txt
        os.unlink(last_msg_file)
    if handoff_body:
        last_text = handoff_body
    summary = (last_text or "").strip()[:2000] or None
    con.execute(
        "UPDATE runs SET status=?, exit_code=?, session_ref=COALESCE(?, session_ref), "
        "summary=?, finished_at=? WHERE id=?",
        (status, exit_code, session_ref, summary, db.now(), run_id))
    # queued follow-ups: deliver by resuming the session in a fresh run
    followup_id = None
    ref_final = session_ref or run["session_ref"]
    queued = list(con.execute("SELECT * FROM messages WHERE COALESCE(kind,'')='queued' "
                              "AND run_id=? AND read_at IS NULL", (run_id,)))
    if queued and ref_final and status in ("done", "failed"):
        joined = "\n\n".join(f"From {q['sender']}: {q['body']}" for q in queued)
        text = (f"Your previous run finished ({status}). Follow-up instructions were queued "
                f"for you while you worked — apply them now:\n\n{joined}\n\n"
                f"Also check `orchestra inbox {run['agent']} --unread --mark-read` for anything "
                f"else. Finish with `orchestra send {queued[0]['sender']} \"HANDOFF: ...\" "
                f"--as {run['agent']}`"
                + (f", and log progress with `work log {run['work_item']} ...`"
                   if run["work_item"] else "") + ".")
        parent = dict(run)
        parent["session_ref"] = ref_final
        followup_id = create_followup(con, root, parent, queued[0]["sender"], text)
        con.execute(f"UPDATE messages SET read_at=? WHERE id IN "
                    f"({','.join(str(q['id']) for q in queued)})", (db.now(),))

    body = (f"[run {run_id}] {run['agent']} finished: {status}"
            f"{f' (exit {exit_code})' if exit_code not in (None, 0) else ''}."
            f"{chr(10) + 'Last output: ' + summary[:800] if summary else ''}\n"
            f"Details: `orchestra run show {run_id}` · logs: `orchestra logs {run_id}`"
            + (f" · follow up: `orchestra reply {run_id} \"...\"`" if ref_final else "")
            + (f"\nQueued follow-up auto-dispatched as run {followup_id}." if followup_id else ""))
    con.execute("INSERT INTO messages(sender, recipient, body, work_item, run_id, created_at) "
                "VALUES('orchestra', ?, ?, ?, ?, ?)",
                (run["requested_by"], body, run["work_item"], run_id, db.now()))
    # bounce unread mail: a finished worker will never read its inbox again
    for m in con.execute("SELECT * FROM messages WHERE recipient=? AND read_at IS NULL "
                         "AND created_at>=? AND sender != 'orchestra' "
                         "AND COALESCE(kind,'') != 'queued'",
                         (run["agent"], run["started_at"])):
        con.execute("INSERT INTO messages(sender, recipient, body, run_id, created_at) "
                    "VALUES('orchestra', ?, ?, ?, ?)",
                    (m["sender"],
                     f"UNDELIVERED: your message #{m['id']} to {run['agent']} "
                     f"(\"{m['body'][:120]}…\") was never read — run {run_id} finished ({status}) "
                     f"without checking its inbox. Deliver it with `orchestra reply {run_id} \"...\"`, "
                     f"or use `orchestra interrupt <run> \"...\"` next time for guaranteed delivery.",
                     run_id, db.now()))
    con.execute("INSERT INTO feed(author, body, work_item, run_id, created_at, tags) "
                "VALUES('orchestra', ?, ?, ?, ?, 'run')",
                (f"run {run_id} ({run['agent']}) -> {status}", run["work_item"], run_id, db.now()))
    con.commit()
    # A child finishing may settle its lead's batch; a lead finishing after
    # already-settled children also needs the same check. The DB claim makes
    # concurrent child supervisors produce at most one continuation.
    from orchestra_cli import child_runs
    child_wakeup_id = child_runs.maybe_wake_lead(con, root, run_id)
    if run["work_item"]:
        _work_log(root, run["work_item"],
                  f"orchestra run {run_id} ({run['agent']}) finished: {status}."
                  + (f" {summary[:300]}" if summary else ""))
    if followup_id:
        spawn_supervisor(root, followup_id)
    if child_wakeup_id:
        spawn_supervisor(root, child_wakeup_id)
    con.close()
    return 0 if status == "done" else 1
