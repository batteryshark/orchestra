"""Detached supervisor: runs one worker process to completion.

DESIGN D3, simplified from Orchestra's 1,388-LOC counterpart:
- Liveness is process-level and costs zero tokens: a growing log is
  progress; silence past ``stall_timeout`` kills; the hard ``timeout``
  caps everything. No periodic check-in injection (killed), no quota
  checks (phase 4), no child runs (later phase).
- ``orchestra tell`` / ``orchestra interrupt`` record a pending delivery; the
  worker is stopped at the next safe action boundary (or immediately with
  --now) and its session is resumed with the message embedded directly in
  the prompt, so delivery is guaranteed without an inbox tool call. The
  harness's Stop hook claims the same rows when it reaches a stop first
  (DESIGN §6, ``messaging.claim_pending``); whichever gets there first wins,
  and anything still queued when the run ends is marked undeliverable.
- Dependency release is event-driven: finalization launches any deferred
  run whose prerequisites just settled.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from orchestra import (acp, auth, brief, config, db, dispatch, findings,
                         merge, messaging, names, observer, paths, project,
                         runners, traces, worktree)
from orchestra.proc import (enrich_path, process_identity, resolve_cmd,
                            session_kwargs, terminate_group)

EARLY_REF_WINDOW = 90  # seconds to keep scanning the log for a session ref
POLL_INTERVAL = 0.5
LAUNCH_FAILURE_PREFIXES = (
    "Launch setup failed:", "Deferred launch failed:", "Retry launch failed:")


def create_run(con, *, profile: str, backend: str, requested_by: str,
               workdir: str, model: str | None = None,
               title: str | None = None, project_id: str | None = None,
               status: str = "spawning", work_item: str | None = None,
               work_seen_ts: str | None = None, parent_run: int | None = None,
               session_ref: str | None = None, retry_of: int | None = None,
               routed_reason: str | None = None, pause_gate: bool = True,
               commit: bool = True) -> tuple[sqlite3.Row | None, str | None]:
    """Atomically reserve and create one worker run.

    Admission starts with SQLite's write lock, then reads the pause switch and
    live reservations. That ordering is the contract: two sweepers, replies,
    or retry finalizers may race, but only one can commit the same work,
    session, parent, or retry lineage. Callers enter with a clean connection;
    ``commit=False`` intentionally leaves the transaction this function opened
    available so related local rows can be attached atomically.

    A policy refusal is data, not an exception: ``(None, reason)``. Database
    and programming errors still raise.
    """
    if status not in {"pending", "spawning", "running"}:
        raise ValueError(f"worker run cannot start in status {status!r}")
    if con.in_transaction:
        raise RuntimeError("run admission requires a clean database transaction")
    con.execute("BEGIN IMMEDIATE")
    try:
        blocked = None
        if pause_gate and dispatch.paused(con):
            blocked = "paused"
        elif work_item:
            if retry_of is not None:
                # A delayed retry may wake after a newer attempt already
                # settled. That newer result owns the item; repeating stale
                # work would be a regression, not resilience.
                active = con.execute(
                    "SELECT id FROM runs WHERE work_item=? AND layer IS NULL "
                    "AND id>? ORDER BY id DESC LIMIT 1",
                    (work_item, int(retry_of))).fetchone()
            else:
                active = con.execute(
                    f"SELECT id FROM runs WHERE work_item=? AND layer IS NULL "
                    f"AND status NOT IN {db.TERMINAL_SQL} ORDER BY id LIMIT 1",
                    (work_item,)).fetchone()
            if active is not None:
                blocked = f"work_item:{active['id']}"
        if blocked is None and session_ref:
            active = con.execute(
                f"SELECT id FROM runs WHERE session_ref=? AND layer IS NULL "
                f"AND status NOT IN {db.TERMINAL_SQL} ORDER BY id LIMIT 1",
                (session_ref,)).fetchone()
            if active is not None:
                blocked = f"session:{active['id']}"
        if blocked is None and parent_run is not None:
            active = con.execute(
                f"SELECT id FROM runs WHERE parent_run=? AND layer IS NULL "
                f"AND status NOT IN {db.TERMINAL_SQL} ORDER BY id LIMIT 1",
                (int(parent_run),)).fetchone()
            if active is not None:
                blocked = f"parent:{active['id']}"
        if blocked is None and retry_of is not None:
            prior = con.execute(
                "SELECT id FROM runs WHERE retry_of=? AND layer IS NULL "
                "ORDER BY id LIMIT 1", (int(retry_of),)).fetchone()
            if prior is not None:
                blocked = f"retry:{prior['id']}"
        if blocked is not None:
            con.rollback()
            return None, blocked

        run_id = None
        for _ in range(names.MAX_ATTEMPTS + 4):
            slug = names.assign_slug(con)
            try:
                cur = con.execute(
                    "INSERT INTO runs(slug, profile, backend, model, title, "
                    "requested_by, workdir, project_id, status, started_at, "
                    "work_item, work_seen_ts, parent_run, session_ref, retry_of, "
                    "routed_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (slug, profile, backend, model, title, requested_by,
                     str(workdir), project_id, status, db.now(), work_item,
                     work_seen_ts, parent_run, session_ref, retry_of,
                     routed_reason))
                run_id = int(cur.lastrowid)
                break
            except sqlite3.IntegrityError as exc:
                if not names.is_unique_violation(exc):
                    raise
                names.reset_memory_cache()
        if run_id is None:
            raise RuntimeError("orchestra: could not mint a unique run slug")
        row = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if commit:
            con.commit()
        return row, None
    except BaseException:
        con.rollback()
        raise


def admit_pending(con, run_id: int) -> tuple[sqlite3.Row | None, str | None]:
    """Atomically move one dependency-ready request into launch preparation."""
    if con.in_transaction:
        raise RuntimeError("deferred admission requires a clean database transaction")
    con.execute("BEGIN IMMEDIATE")
    try:
        if dispatch.paused(con):
            con.rollback()
            return None, "paused"
        req = con.execute(
            "SELECT d.run_id, d.mission, d.context, d.use_worktree "
            "FROM deferred_dispatches d JOIN runs r ON r.id=d.run_id "
            "WHERE d.run_id=? AND d.status='pending' AND r.status='pending' "
            "AND NOT EXISTS (SELECT 1 FROM dispatch_dependencies e "
            "JOIN runs p ON p.id=e.depends_on_run WHERE e.run_id=d.run_id "
            "AND NOT (p.status='done' AND "
            "(p.branch IS NULL OR p.landing_status='ok')))",
            (int(run_id),)).fetchone()
        if req is None:
            con.rollback()
            return None, "not_ready"
        claimed = con.execute(
            "UPDATE deferred_dispatches SET status='processing' "
            "WHERE run_id=? AND status='pending'", (int(run_id),))
        admitted = con.execute(
            "UPDATE runs SET status='spawning', started_at=? "
            "WHERE id=? AND status='pending'", (db.now(), int(run_id)))
        if claimed.rowcount != 1 or admitted.rowcount != 1:
            con.rollback()
            return None, "not_ready"
        con.commit()
        return req, None
    except BaseException:
        con.rollback()
        raise


def never_started(run) -> bool:
    """True when a durable run row never reached a supervisor process."""
    return (run is not None and run["status"] == "failed"
            and str(run["summary"] or "").startswith(LAUNCH_FAILURE_PREFIXES))


def _lineage_was_isolated(con, run) -> bool:
    """Recover isolation intent through failed continuation/retry rows."""
    current, seen = run, set()
    while current is not None and int(current["id"]) not in seen:
        seen.add(int(current["id"]))
        if current["branch"]:
            return True
        if not never_started(current):
            return False
        previous_id = current["parent_run"] or current["retry_of"]
        current = con.execute("SELECT * FROM runs WHERE id=?", (previous_id,)).fetchone() \
            if previous_id else None
    return False


def spawn_supervisor(root: Path, run_id: int) -> None:
    exe = shutil.which("orchestra")
    cmd = [exe, "_supervise", str(run_id), "--root", str(root)] if exe else \
        [sys.executable, "-m", "orchestra", "_supervise", str(run_id), "--root", str(root)]
    # A supervisor that dies before writing anything used to leave no trace at
    # all: stderr went to /dev/null, so run 9's instant death was invisible
    # until its workdir was inspected by hand. Keep the last words.
    err = paths.logs_dir() / f"supervisor-{run_id}.log"
    try:
        handle = open(err, "ab")
    except OSError:
        handle = subprocess.DEVNULL
    try:
        proc = subprocess.Popen(resolve_cmd(cmd), stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=handle,
                                **session_kwargs(detached=True))
    finally:
        if handle is not subprocess.DEVNULL:
            handle.close()
    # Keep Popen alive and reap the detached child without delaying dispatch.
    try:
        threading.Thread(target=proc.wait, daemon=True).start()
    except RuntimeError:
        pass  # the detached child is already running; reaping is best-effort


# --- launch preparation (shared by dispatch and deferred release) ----------

def prepare_launch(con, root: Path, cfg: dict, run, *, mission: str,
                   context: str | None = None, use_worktree: bool = False,
                   work_snapshot: str | None = None) -> None:
    """Create the workdir, brief, and log for a run row about to launch."""
    run_id = int(run["id"])
    profile = config.profile_cfg(cfg, run["profile"])
    workdir, branch = str(root), None
    base_commit = None
    bp = paths.briefs_dir() / f"run-{run_id}.md"
    lp = paths.logs_dir() / f"run-{run_id}.jsonl"
    created = None
    try:
        if use_worktree:
            created, branch = worktree.create(
                root, run_id, project.dir_key_for(con, run),
                backend=profile["backend"])
            workdir = str(created)
        if (Path(workdir) / ".git").exists():
            try:
                base_commit = worktree.head(Path(workdir))
            except RuntimeError:
                base_commit = None  # fresh repository with no commits yet
        # The Work snapshot is frozen here, at dispatch; it is never re-read at
        # resume (the immutability is load-bearing — Orchestra learned it the
        # hard way).
        # Only a Work TASK carries a checklist; issues do not, so only a task id
        # earns the checklist protocol in the brief.
        item = run["work_item"] if "work_item" in run.keys() else None
        text = brief.compose(
            run_id=run_id, slug=run["slug"], profile=profile,
            mission=mission, requester=run["requested_by"], root=root,
            workdir=workdir, extra_context=context, work_snapshot=work_snapshot,
            work_item=item if (item or "").startswith("W-") else None,
            recent_commits=worktree.recent_commits(Path(workdir)))
        bp.write_text(text, encoding="utf-8")
        lp.touch()
        prepared = con.execute(
            "UPDATE runs SET brief_path=?, log_path=?, workdir=?, branch=?, "
            "base_commit=?, started_at=? WHERE id=? AND status='spawning'",
            (str(bp), str(lp), workdir, branch, base_commit, db.now(), run_id),
        )
        if prepared.rowcount != 1:
            raise RuntimeError("run admission expired during launch preparation")
    except BaseException:
        for artifact in (bp, lp):
            try:
                artifact.unlink(missing_ok=True)
            except OSError:
                pass
        if created is not None and branch is not None:
            try:
                cleanup = worktree.discard_created(created, root, branch)
            except Exception as cleanup_error:
                cleanup = {"removed": False, "branch_deleted": False,
                           "error": str(cleanup_error)}
            if not cleanup["removed"] or not cleanup["branch_deleted"]:
                con.execute(
                    "UPDATE runs SET workdir=?, branch=?, base_commit=? WHERE id=?",
                    (str(created) if not cleanup["removed"] else str(root),
                     branch, base_commit, run_id))
        raise


def fail_launch(con, root: Path, run_id: int, error: BaseException | str,
                prefix: str = "Launch setup failed") -> dict:
    """Finalize a prepared run that never reached a supervisor."""
    root = Path(root)
    run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    cleanup = None
    workdir = run["workdir"] if run is not None else str(root)
    branch = run["branch"] if run is not None else None
    base_commit = run["base_commit"] if run is not None else None
    owns_checkout = (run is not None and run["supervisor_pid"] is None
                     and branch == f"orchestra/run-{run_id}"
                     and Path(workdir).name == f"run-{run_id}")
    if owns_checkout:
        try:
            cleanup = worktree.discard_created(Path(workdir), root, branch)
        except Exception as exc:
            cleanup = {"removed": False, "branch_deleted": False,
                       "error": str(exc)}
        if cleanup["removed"]:
            workdir = str(root)
        if cleanup["branch_deleted"]:
            branch = None
            base_commit = None
    reason = str(error)[:1000] or error.__class__.__name__
    con.execute("UPDATE runs SET workdir=?, branch=?, base_commit=? WHERE id=?",
                (workdir, branch, base_commit, run_id))
    con.commit()
    run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if run is not None:
        finalize_run(con, run, "failed", None,
                     restart_note=f"{prefix}: {reason}")
    return cleanup or {"removed": False, "branch_deleted": False, "error": None}


def rehome(con, root: Path, previous, run_id: int) \
        -> tuple[str, str | None, str | None, bool]:
    """SEAM (W-0191): where a run started FROM ``previous`` stands, and on what
    branch. Returns ``(workdir, branch, base_commit, created)``.

    Every path that re-dispatches from an earlier run goes through here —
    ``create_followup`` (continuation) and ``observer._retry_row`` (retry) —
    because the earlier run's world may be GONE: a terminal run gives its
    worktree back (DESIGN §2) and a merged run's branch is deleted (§9). A row
    that copies both fields starts a process in a directory that no longer
    exists and dies instantly with ``FileNotFoundError`` (live runs 9 and 28).

    An isolated previous run whose worktree was released gets a fresh worktree
    on a fresh branch. Failure is returned to the caller; it never downgrades
    an isolated lineage to the owner's checkout. A shared-checkout run returns
    to the project root, and an existing workdir is kept unchanged.
    """
    workdir = Path(previous["workdir"] or root)
    branch = previous["branch"]
    created = False
    root = Path(root)
    isolated = _lineage_was_isolated(con, previous)
    lost_isolation = (isolated and never_started(previous)
                      and workdir.resolve() == root.resolve())
    if not workdir.exists() or lost_isolation:
        # The caller's root is the live checkout. project.root_for falls back
        # to the run's own workdir when the project is unknown, which is the
        # very path that is gone — so only consult it if the caller's is not
        # a repository.
        base = Path(root)
        if not (base / ".git").exists():
            base = project.root_for(con, previous)
        workdir, branch = base, None
        if isolated:  # it was isolated; give it isolation again
            workdir, branch = worktree.create(
                base, run_id, project.dir_key_for(con, previous),
                backend=previous["backend"])
            created = True
    base_commit = previous["base_commit"]
    if (workdir / ".git").exists():
        try:
            base_commit = worktree.head(workdir)
        except RuntimeError:
            base_commit = None
    return str(workdir), branch, base_commit, created


def reserve_followup(con, root: Path, parent, requester: str,
                     title: str | None = None, *, commit: bool = True):
    """Reserve a continuation before any Work claim or filesystem setup."""
    return create_run(
        con, profile=parent["profile"], backend=parent["backend"],
        model=parent["model"],
        title=title or f"continuation of run {parent['id']}",
        requested_by=requester, workdir=str(root),
        project_id=parent["project_id"], parent_run=int(parent["id"]),
        session_ref=parent["session_ref"], work_item=parent["work_item"],
        work_seen_ts=parent["work_seen_ts"], commit=commit)


def prepare_followup(con, root: Path, parent, run, text: str) -> int:
    """Freeze and re-home an already reserved continuation."""
    run_id = int(run["id"])
    bp = paths.briefs_dir() / f"run-{run_id}.md"
    lp = paths.logs_dir() / f"run-{run_id}.jsonl"
    workdir, branch, base_commit, created = str(root), None, None, False
    try:
        workdir, branch, base_commit, created = rehome(
            con, root, parent, run_id)
        landed = (worktree.recent_commits(Path(root), since=parent["base_commit"])
                  if parent["base_commit"] and (Path(root) / ".git").exists() else [])
        bp.write_text(
            brief.compose_continuation(
                run_id=run_id, parent_run=parent["id"], instructions=text,
                landed=landed), encoding="utf-8")
        lp.touch()
        prepared = con.execute(
            "UPDATE runs SET brief_path=?, log_path=?, workdir=?, branch=?, "
            "base_commit=?, started_at=? WHERE id=? AND status='spawning'",
            (str(bp), str(lp), workdir, branch, base_commit, db.now(), run_id))
        if prepared.rowcount != 1:
            raise RuntimeError("run admission expired during continuation setup")
        con.commit()
        return run_id
    except BaseException as exc:
        for artifact in (bp, lp):
            try:
                artifact.unlink(missing_ok=True)
            except OSError:
                pass
        if created and branch:
            con.execute("UPDATE runs SET workdir=?, branch=?, base_commit=? WHERE id=?",
                        (workdir, branch, base_commit, run_id))
        fail_launch(con, root, run_id, exc, prefix="Deferred launch failed")
        raise


def create_followup(con, root: Path, parent, requester: str, text: str,
                    title: str | None = None) -> int | None:
    """Reserve and prepare a run that resumes the parent's backend session."""
    run, _blocked = reserve_followup(con, root, parent, requester, title)
    if run is None:
        return None
    return prepare_followup(con, root, parent, run, text)


# --- safe-boundary message delivery ----------------------------------------

def _pending_delivery_offset(con, run_id: int) -> int | None:
    row = con.execute(
        "SELECT MAX(delivery_offset) AS offset FROM messages "
        "WHERE run_id=? AND kind='interrupt' AND delivered_at IS NULL "
        "AND undeliverable_at IS NULL AND delivery_offset IS NOT NULL",
        (run_id,),
    ).fetchone()
    return int(row["offset"]) if row and row["offset"] is not None else None


def _mark_pending_delivered(con, run_id: int) -> list:
    """W-0098 seam: the Stop hook claims the same rows, so the claim (and the
    trace it writes) lives in one place — ``messaging.claim_pending``."""
    return messaging.claim_pending(con, run_id)


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
        # One unusually large event must not stall boundary watching forever;
        # skipping its first bounded chunk never makes an unsafe interruption.
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


def _resume_prompt(messages: list) -> str:
    """Render delivered message bodies for a backend-session resume. The
    supervisor already owns the bodies, so no inbox round-trip is needed."""
    joined = "\n\n".join(f"[message from {m['sender']}]\n{m['body']}" for m in messages)
    return (
        "Apply the following delivered message(s) now, then continue the original "
        f"mission.\n\n{joined}\n\n"
        "End with the usual handoff summary as your final message."
    )


# --- process control --------------------------------------------------------

def _ts_to_epoch(ts: str) -> float:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def _stall_seconds(raw) -> int | None:
    if raw is False or raw is None:
        return None
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        raise SystemExit("orchestra: stall_timeout must be an integer number of seconds")
    if seconds < 0:
        raise SystemExit("orchestra: stall_timeout must be zero or positive")
    return seconds or None


def _terminate_process_group(pid: int) -> None:
    terminate_group(pid)


def _wait_after_term(child: subprocess.Popen, timeout: float = 15) -> None:
    try:
        child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        # It already had its SIGTERM and its grace period. This is the kill.
        terminate_group(child.pid, force=True)
        try:
            child.wait(timeout=2)
        except Exception:
            pass


def _run_proc(con, run, cmd, env, log_path, run_id, deadline,
              stall_timeout: int | None) -> tuple[str, int | None]:
    """Start one worker process; wait with stall detection + hard timeout +
    early session-ref capture + safe-boundary interrupt watching.
    Returns (outcome, exit_code) where outcome is 'exit' | 'timeout'."""
    backend = run["backend"]
    # SEAM (W-0166): the spin observer. Layers (b) and (c) — mechanical loop
    # detection and the out-of-band observer turn — hang off this one object;
    # layer (a) is the stall detection already in the loop below.
    spin = observer.Watcher(run_id, run["project_id"])
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(
            resolve_cmd(cmd), stdin=subprocess.DEVNULL, stdout=log,
            stderr=subprocess.STDOUT, cwd=run["workdir"],
            env=env, **session_kwargs())
    cur = con.execute(
        "UPDATE runs SET pid=?, pid_identity=?, status='running' "
        f"WHERE id=? AND status NOT IN {db.TERMINAL_SQL}",
        (proc.pid, process_identity(proc.pid), run_id))
    con.commit()
    if cur.rowcount == 0:  # killed before we could claim it
        _terminate_process_group(proc.pid)
        _wait_after_term(proc)
        return "exit", proc.poll()
    started = time.time()
    have_ref = bool(run["session_ref"])
    try:
        last_size = os.path.getsize(log_path)
    except OSError:
        last_size = 0
    last_progress = started
    pending_after: int | None = None
    scan_offset = 0
    while True:
        try:
            exit_code = proc.wait(timeout=POLL_INTERVAL)
            return "exit", exit_code
        except subprocess.TimeoutExpired:
            pass
        now = time.time()
        latest = con.execute("SELECT status, session_ref FROM runs WHERE id=?",
                             (run_id,)).fetchone()
        if latest and (latest["status"] == "interrupt"
                       or latest["status"] in db.RUN_TERMINAL):
            _terminate_process_group(proc.pid)
            _wait_after_term(proc)
            return "exit", proc.poll()
        if latest and latest["session_ref"]:
            have_ref = True
        elif not have_ref and now - started < EARLY_REF_WINDOW:
            ref, _ = runners.parse_log(log_path, max_bytes=65536)
            if ref:
                con.execute("UPDATE runs SET session_ref=? WHERE id=?", (ref, run_id))
                con.commit()
                have_ref = True
        pending = _pending_delivery_offset(con, run_id)
        if pending is not None:
            if pending_after != pending:
                pending_after = pending
                scan_offset = pending
            events, scan_offset = _read_log_events(log_path, scan_offset)
            # Never stop before the session is resumable; a later boundary or
            # the natural process exit will deliver the message instead.
            if have_ref and any(_is_safe_boundary(backend, e) for e in events):
                con.execute("UPDATE runs SET status='interrupt' WHERE id=?", (run_id,))
                con.commit()
                _terminate_process_group(proc.pid)
                _wait_after_term(proc)
                return "exit", proc.poll()
        else:
            pending_after = None
        # A growing log is the backend-neutral progress signal.
        try:
            size = os.path.getsize(log_path)
        except OSError:
            size = last_size
        if size > last_size:
            last_size = size
            last_progress = now
            # Normalize what just arrived (DESIGN §7). Reads only the new
            # bytes, so the trace is live without a second tailer.
            traces.ingest(con, run_id, log_path, backend)
        spin.poll(con)  # rate-limited; the model turn runs on its own thread
        # W-0098 seam: a run held open by its Stop hook waiting on an `ask`
        # produces no output for as long as the human takes. That is not a
        # stall — killing it would throw away the answer. The hard timeout
        # still caps it.
        if stall_timeout and now - last_progress >= stall_timeout \
                and messaging.open_ask(con, run_id) is None:
            con.execute(
                "UPDATE runs SET summary=? WHERE id=?",
                (f"Stalled: no worker output for {int(now - last_progress)}s "
                 "(stall_timeout)", run_id))
            con.commit()
            _terminate_process_group(proc.pid)
            _wait_after_term(proc)
            return "timeout", None
        if now > deadline:
            _terminate_process_group(proc.pid)
            _wait_after_term(proc)
            return "timeout", None


# --- completion -------------------------------------------------------------

def _checkpoint_commit(run: dict, terminal_status: str) -> str | None:
    """Commit an isolated worktree's leftover changes so nothing is lost.

    Shared-tree runs write into the human's checkout; Orchestra never
    auto-commits there. Isolated runs leave files; this is the only commit
    on the run branch, for every backend — including ones that could commit.
    """
    if not run.get("branch"):
        return None
    workdir = Path(run["workdir"])
    if not (workdir / ".git").exists():
        return None
    if worktree.status(workdir):
        pathspec = ["."]
        for name in worktree.untracked_context_paths(workdir):
            pathspec += [f":(exclude){name}", f":(exclude){name}/**"]
        added = subprocess.run(
            ["git", "-C", str(workdir), "add", "-A", "--", *pathspec],
            capture_output=True, text=True, timeout=60)
        if added.returncode != 0:
            raise RuntimeError(f"automatic checkpoint staging failed: {added.stderr.strip()}")
        staged = subprocess.run(
            ["git", "-C", str(workdir), "diff", "--cached", "--quiet"], timeout=60)
        if staged.returncode not in (0, 1):
            raise RuntimeError("cannot inspect staged checkpoint changes")
        if staged.returncode == 1:
            committed = subprocess.run(
                ["git", "-C", str(workdir),
                 "-c", "user.name=Orchestra", "-c", "user.email=orchestra@localhost",
                 "-c", "commit.gpgSign=false",
                 "commit", "--no-verify", "-m",
                 f"orchestra: checkpoint run {run['id']} ({terminal_status})"],
                capture_output=True, text=True, timeout=60)
            if committed.returncode != 0:
                raise RuntimeError(
                    f"automatic checkpoint commit failed: {committed.stderr.strip()}")
    # HEAD is also the durable "checkpoint attempted" receipt when there was
    # no diff. NULL means finalization never reached this boundary.
    return worktree.head(workdir)


def release_worktree(con, run: dict, status: str) -> str | None:
    """SEAM (W-0172): a terminal run gives its isolated checkout back.

    Called once at finalization, after the checkpoint commit and before any
    merge step needs the run branch deletable — a branch still checked out
    here cannot be deleted. The branch is untouched; only the checkout goes.
    Returns a note for the run summary when the worktree was KEPT, else None.
    """
    if status not in db.RUN_TERMINAL or not run.get("branch"):
        return None  # a shared-tree run works in the human's own checkout
    workdir = Path(run["workdir"])
    root = worktree.main_root(workdir)
    if root is None or root == workdir.resolve():
        return None  # gone already, or the run ran in the main checkout
    if worktree.live_holders(con, workdir, ignore_run=int(run["id"])):
        return None  # a follow-up run is still working in this checkout
    report = worktree.remove(workdir, root, branch=run["branch"])
    if report["kept"]:
        return f"Worktree kept at {workdir}: {report['kept']}"
    if not report["removed"]:
        return f"Worktree at {workdir} could not be removed: {report['error']}"
    return None


def record_usage(con, run_id: int, backend: str, log_path: str | None) -> None:
    """DESIGN §11 seam: stamp the run row with the backend's own token/cost
    totals at completion, so the dashboard is a query and not a re-parse.
    Uncapturable usage writes nulls — it never blocks finalization.
    """
    usage = runners.parse_usage(log_path, backend) if log_path else runners.EMPTY_USAGE
    con.execute(
        "UPDATE runs SET tokens_in=COALESCE(?, tokens_in), "
        "tokens_out=COALESCE(?, tokens_out), "
        "tokens_total=COALESCE(?, tokens_total), "
        "cost_usd=COALESCE(?, cost_usd), "
        "usage_source=COALESCE(?, usage_source) WHERE id=?",
        (usage["tokens_in"], usage["tokens_out"], usage["tokens_total"],
         usage["cost_usd"], usage["usage_source"], run_id))


def finalize_run(con, run, status: str, exit_code: int | None, *,
                 last_msg_file: str | None = None,
                 restart_note: str | None = None) -> dict:
    """Persist one terminal worker result and return its refreshed run row.

    The terminal row, checkpoint identity, usage, delivery state, completion
    notice, and retry hold become visible together before checkout release.
    Repeating finalization enriches an existing terminal row without reopening
    its outcome, checkpointing later edits, or duplicating its completion notice.
    """
    run = dict(run)
    run_id = int(run["id"])
    log_path = run.get("log_path")
    preferred_text = None
    preferred_output = None
    preferred_durable = False

    # Record what the worker returned before parsing, trace enrichment, or Git
    # can fail. This is not the public terminal result; it is the receipt an
    # orphan recovery pass uses to finish the same outcome instead of guessing
    # `failed` merely because the supervisor died during finalization.
    con.execute(
        "UPDATE runs SET worker_status=COALESCE(worker_status, ?), "
        "worker_exit_code=COALESCE(worker_exit_code, ?) WHERE id=?",
        (status, exit_code, run_id))
    con.commit()

    # Codex's -o file is the authoritative last message. Preserve a preferred
    # copy in the raw JSONL before the final ingest so every later consumer can
    # derive the same result from result["log_path"].
    if last_msg_file and Path(last_msg_file).is_file():
        output = Path(last_msg_file)
        preferred_output = output
        try:
            preferred_text = output.read_text(
                encoding="utf-8", errors="replace").strip() or None
            _, logged_text = runners.parse_log(log_path) if log_path else (None, None)
            if not preferred_text or preferred_text == (logged_text or "").strip():
                preferred_durable = True
            elif log_path:
                event = json.dumps({
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": preferred_text},
                    "_orchestra": {"source": "codex-last-message"},
                }, ensure_ascii=False).encode("utf-8") + b"\n"
                with Path(log_path).open("ab+") as log:
                    log.seek(0, os.SEEK_END)
                    if log.tell():
                        log.seek(-1, os.SEEK_END)
                        if log.read(1) not in (b"\n", b"\r"):
                            event = b"\n" + event
                    log.write(event)
                preferred_durable = True
        except OSError as exc:
            print(f"orchestra: run {run_id} could not preserve final output; "
                  f"retained {output}: {exc}", file=sys.stderr)

    if log_path:
        traces.ingest(con, run_id, log_path, run["backend"])
        session_ref, last_text = runners.parse_log(log_path)
    else:
        session_ref, last_text = None, preferred_text
    if preferred_text and preferred_text != (last_text or "").strip():
        # Even when the raw append failed, this process still has Codex's
        # authoritative answer and can persist the terminal summary.
        last_text = preferred_text
    latest = con.execute(
        "SELECT status, exit_code, summary, finished_at, checkpoint_commit, "
        "EXISTS(SELECT 1 FROM messages WHERE run_id=runs.id "
        "AND kind='completion') AS finalized FROM runs WHERE id=?",
        (run_id,)).fetchone()
    if latest is None:
        raise RuntimeError(f"run {run_id} disappeared during finalization")
    already_terminal = latest["status"] in db.RUN_TERMINAL
    if already_terminal:
        status = latest["status"]
        if latest["exit_code"] is not None or status in ("killed", "halted"):
            exit_code = latest["exit_code"]
    if latest["summary"] and (
            (status == "timeout" and latest["summary"].startswith("Stalled:"))
            or latest["summary"].startswith(acp.FAILURE_PREFIX)):
        last_text = latest["summary"]

    summary = (last_text or "").strip()[:2000] or None
    reason = findings.halt_reason(last_text)
    if reason and not already_terminal and status != "killed":
        status = "halted"
        summary = reason
    if status != "done" and not summary:
        summary = runners.parse_failure(log_path) if log_path else None
    if restart_note and not already_terminal:
        summary = (f"{summary}\n\n{restart_note}" if summary else restart_note)[:2000]

    checkpoint_commit = latest["checkpoint_commit"]
    checkpoint_note = None
    if checkpoint_commit is None and not latest["finalized"]:
        try:
            checkpoint_commit = _checkpoint_commit(run, status)
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            if status == "done":
                status, exit_code = "failed", exit_code or 1
            checkpoint_note = f"Checkpoint error: {exc}"

    # The result must exist before any external cleanup. A process death after
    # this commit leaves one terminal fact the daemon can safely replay from;
    # it must never infer an outcome from a vanished checkout.
    con.execute("BEGIN IMMEDIATE")
    try:
        current = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if current is None:
            raise RuntimeError(f"run {run_id} disappeared during finalization")
        if current["status"] in db.RUN_TERMINAL:
            status = current["status"]
            if current["exit_code"] is not None or status in ("killed", "halted"):
                exit_code = current["exit_code"]
            if current["summary"]:
                summary = current["summary"]
        if checkpoint_note and checkpoint_note not in (summary or ""):
            summary = (f"{summary}\n\n{checkpoint_note}"
                       if summary else checkpoint_note)[:2000]

        con.execute(
            "UPDATE runs SET status=?, exit_code=?, "
            "session_ref=COALESCE(?, session_ref), summary=?, "
            "checkpoint_commit=COALESCE(checkpoint_commit, ?), "
            "finished_at=COALESCE(finished_at, ?) WHERE id=?",
            (status, exit_code, session_ref, summary, checkpoint_commit,
             db.now(), run_id))
        observer.defer_retry(con, run_id)
        stranded = messaging.mark_undeliverable(
            con, run_id,
            f"run ended ({status}) before the message reached a boundary")
        record_usage(con, run_id, run["backend"], run["log_path"])
        body = (f"run {run_id} finished: {status}"
                + (f" (exit {exit_code})" if exit_code not in (None, 0) else "")
                + (f"\n{stranded} message(s) were never delivered — see "
                   f"`orchestra show {run_id}`" if stranded else "")
                + (f"\n{summary[:800]}" if summary else ""))
        con.execute(
            "INSERT INTO messages(run_id, sender, body, kind, created_at) "
            "SELECT ?, 'orchestra', ?, 'completion', ? WHERE NOT EXISTS ("
            "SELECT 1 FROM messages WHERE run_id=? AND kind='completion')",
            (run_id, body, db.now(), run_id))
        con.commit()
    except BaseException:
        con.rollback()
        raise

    if preferred_output is not None and preferred_durable:
        try:
            preferred_output.unlink()
        except OSError:
            pass

    # Cleanup is a consequence of the durable result. It is intentionally
    # outside the transaction and may be retried by daemon policy recovery.
    kept = release_worktree(con, run, status)
    if kept:
        current = con.execute("SELECT summary FROM runs WHERE id=?", (run_id,)).fetchone()
        current_summary = current["summary"] if current else None
        if kept not in (current_summary or ""):
            current_summary = (f"{current_summary}\n\n{kept}"
                               if current_summary else kept)[:2000]
            con.execute("UPDATE runs SET summary=? WHERE id=?",
                        (current_summary, run_id))
            con.commit()
    return dict(con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())


def finalize_if_unowned(con, run_id: int, *, worker_gone: bool = False) -> bool:
    """Finish a terminal row that has no process left to own finalization.

    ``worker_gone`` is proof from the process-group probe, not an inference
    from a stored PID. A live supervisor still owns finalization either way.
    """
    run = con.execute(
        "SELECT *, EXISTS(SELECT 1 FROM messages WHERE run_id=runs.id "
        "AND kind='completion') AS finalized FROM runs WHERE id=?",
        (run_id,)).fetchone()
    if run is None or run["status"] not in db.RUN_TERMINAL or run["finalized"] \
            or run["supervisor_pid"] is not None \
            or (run["pid"] is not None and not worker_gone):
        return False
    finalize_run(con, run, run["status"], run["exit_code"])
    return True


def process_ready(con, launcher) -> list[dict]:
    """Event-driven dependency release: decline broken chains, launch ready runs.

    Called after every run finalization and after every ``--after`` dispatch.
    One central database holds every project's runs, so root and config are
    resolved per released run rather than passed in.
    """
    results: list[dict] = []
    # Decline, cascading: a requires_success edge from an unsuccessful terminal
    # prerequisite fails the dependent, which may fail its own dependents.
    # ponytail: wait_for edges are schema-reserved; implement their release
    # rule when something can create them.
    while True:
        con.execute("BEGIN IMMEDIATE")
        try:
            broken = [int(r["run_id"]) for r in con.execute(
                "SELECT DISTINCT d.run_id FROM deferred_dispatches d "
                "JOIN dispatch_dependencies e ON e.run_id=d.run_id "
                "JOIN runs p ON p.id=e.depends_on_run "
                "WHERE d.status='pending' AND e.kind='requires_success' "
                f"AND p.status IN {db.TERMINAL_SQL} AND (p.status != 'done' "
                "OR (p.branch IS NOT NULL AND p.landing_status='failed')) "
                "AND NOT EXISTS (SELECT 1 FROM observations o WHERE o.run_id=p.id "
                "AND o.layer='retry' AND o.action='deferred' AND NOT EXISTS ("
                "SELECT 1 FROM observations newer WHERE newer.run_id=o.run_id "
                "AND newer.layer='retry' AND newer.id>o.id))")]
            if not broken:
                con.commit()
                break
            ts = db.now()
            for rid in broken:
                con.execute("UPDATE deferred_dispatches SET status='declined', "
                            "processed_at=? WHERE run_id=?", (ts, rid))
                con.execute("UPDATE runs SET status='failed', finished_at=?, "
                            "summary='Declined: a prerequisite run did not succeed' "
                            "WHERE id=? AND status='pending'", (ts, rid))
                results.append({"run_id": rid, "status": "declined"})
            con.commit()
        except BaseException:
            con.rollback()
            raise
    ready = list(con.execute(
        "SELECT d.run_id "
        "FROM deferred_dispatches d JOIN runs r ON r.id=d.run_id "
        "WHERE d.status='pending' AND r.status='pending' AND NOT EXISTS ("
        "  SELECT 1 FROM dispatch_dependencies e "
        "  JOIN runs p ON p.id=e.depends_on_run "
        "  WHERE e.run_id=d.run_id AND NOT (p.status='done' AND "
        "  (p.branch IS NULL OR p.landing_status='ok'))) "
        "ORDER BY d.run_id"))
    for req in ready:
        run_id = int(req["run_id"])
        admitted, blocked = admit_pending(con, run_id)
        if admitted is None:
            if blocked == "paused":
                break
            continue
        run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        root = project.root_for(con, run)
        try:
            prepare_launch(con, root, config.load(run["project_id"]), run,
                           mission=admitted["mission"],
                           context=admitted["context"],
                           use_worktree=bool(admitted["use_worktree"]))
            con.execute("UPDATE deferred_dispatches SET status='fired', "
                        "processed_at=? WHERE run_id=?", (db.now(), run_id))
            con.commit()
            launcher(root, run_id)
            results.append({"run_id": run_id, "status": "fired"})
        except BaseException as exc:
            error = str(exc)[:1000] or exc.__class__.__name__
            con.execute("UPDATE deferred_dispatches SET status='failed', "
                        "processed_at=?, error=? WHERE run_id=?",
                        (db.now(), error, run_id))
            fail_launch(con, root, run_id, error, prefix="Deferred launch failed")
            results.append({"run_id": run_id, "status": "failed", "error": error})
    return results


# --- the run loop -----------------------------------------------------------

def supervise(root: Path, run_id: int) -> int:
    con = db.connect()
    run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not run:
        raise SystemExit(f"orchestra: run {run_id} not found")
    # Claim the run for THIS supervisor process so a supervisor that dies
    # before the completion UPDATE leaves a detectable orphan.
    supervisor_pid = os.getpid()
    claimed = con.execute(
        "UPDATE runs SET supervisor_pid=?, supervisor_pid_identity=? "
        "WHERE id=? AND status='spawning' AND supervisor_pid IS NULL",
        (supervisor_pid, process_identity(supervisor_pid), run_id))
    con.commit()
    if claimed.rowcount != 1:
        con.close()
        return 1  # recovery or another supervisor won; never launch the worker
    run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    cfg = config.load(run["project_id"])
    profile = config.profile_cfg(cfg, run["profile"])
    settings = cfg.get("settings", {})
    timeout = int(profile.get("timeout")
                  or settings.get("timeout", config.DEFAULT_RUN_TIMEOUT_SECONDS))
    stall_timeout = _stall_seconds(profile.get(
        "stall_timeout",
        settings.get("stall_timeout", config.DEFAULT_STALL_TIMEOUT_SECONDS)))
    deadline = _ts_to_epoch(run["started_at"]) + timeout
    prompt = Path(run["brief_path"]).read_text(encoding="utf-8") if run["brief_path"] else (run["title"] or "")
    brief_prompt = prompt
    resume_ref = run["session_ref"] if run["parent_run"] else None
    restart_note = None  # set when a dead session sent the work back to square one
    # SEAM (W-0176): the run's own credential, minted once here — this is
    # where a worker's environment is built, for both transports — and
    # revoked by the database trigger when the run turns terminal. The raw
    # value goes into the environment and nowhere else; only its hash is
    # stored. A resume keeps the same token, so a resumed session is still
    # the same caller to the API.
    run_token = auth.mint(con, run_id)

    status, exit_code, last_msg_file = "done", None, None
    # SEAM (W-0104, DESIGN §6): the second transport. A profile with
    # transport = "acp" runs over one persistent ACP peer instead of the exec
    # loop below; absent means exec, unchanged for all four backends. There is
    # no mid-run fallback between the two — see acp.supervise_run.
    transport = acp.transport_for(profile)
    parent_env = dict(os.environ, ORCHESTRA_ROOT=str(root), ORCHESTRA_RUN_ID=str(run_id),
                      **{auth.TOKEN_ENV: run_token})
    parent_env = enrich_path(parent_env)
    parent_env = config.apply_worker_env(cfg, parent_env, root)
    quota_fell_back = False
    while transport == "exec":
        cmd = runners.build_cmd(profile, workdir=run["workdir"],
                                title=f"orchestra-run-{run_id}", prompt=prompt,
                                resume_ref=resume_ref)
        last_msg_file = None
        if profile["backend"] == "codex" and not resume_ref:
            last_msg_file = tempfile.NamedTemporaryFile(delete=False, suffix=".txt").name
            cmd = cmd[:2] + ["-o", last_msg_file] + cmd[2:]  # `codex exec -o FILE ...`
        env = runners.apply_backend_env(profile, parent_env)
        if lane := runners.lane_of(profile):
            runners.write_lane(run["log_path"], lane)
        outcome, exit_code = _run_proc(con, run, cmd, env, run["log_path"],
                                       run_id, deadline, stall_timeout)
        run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if outcome == "timeout":
            status = "timeout"
            break
        if run["status"] in ("killed", "halted"):
            status = run["status"]
            break
        if run["status"] == "interrupt" or _pending_delivery_offset(con, run_id) is not None:
            # Resume the same session after a safe-boundary stop, a --now stop,
            # or a natural process exit that beat the next boundary.
            if not run["session_ref"]:
                status = "failed"  # cli guards against this; defensive only
                break
            resume_ref = run["session_ref"]
            claimed = con.execute(
                "UPDATE runs SET status='running' WHERE id=? AND status IN "
                "('interrupt','running')", (run_id,))
            con.commit()
            if claimed.rowcount != 1:  # killed while we were deciding
                latest = con.execute("SELECT status FROM runs WHERE id=?",
                                     (run_id,)).fetchone()
                status = latest["status"] if latest and latest["status"] in db.RUN_TERMINAL \
                    else "failed"
                break
            delivered = _mark_pending_delivered(con, run_id)
            con.commit()
            prompt = _resume_prompt(delivered) if delivered else (
                "Continue the original mission after the completed action boundary. "
                "End with the usual handoff summary.")
            continue
        status = "done" if exit_code == 0 else "failed"
        # SEAM (W-0191): a resume that CANNOT resume. The backend answered that
        # the session is gone (a killed run's may never have survived, a
        # harness prunes its own history), so there is no conversation to
        # continue and no amount of retrying finds one. Start the same brief
        # FRESH instead of failing the item — but exactly once, so a backend
        # that says this every time still fails normally the second round.
        gone = runners.session_missing(run["log_path"]) if (
            status == "failed" and resume_ref and restart_note is None) else None
        if gone:
            restart_note = (f"Session {resume_ref} was gone ({gone}); "
                            "the work restarted fresh in a new session.")
            print(f"orchestra: run {run_id}: {restart_note}", file=sys.stderr)
            resume_ref = None
            # Clear the dead ref AND re-read the row: _run_proc decides whether
            # to sniff the log for a session id from what it is handed, so a
            # stale row would leave the fresh session unrecorded.
            con.execute("UPDATE runs SET session_ref=NULL WHERE id=?", (run_id,))
            con.commit()
            run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            # Back to the brief — the fresh session has none of the context a
            # resume prompt assumes. A message already claimed for delivery
            # rides along; nothing else has seen it.
            prompt = brief_prompt if prompt == brief_prompt \
                else f"{brief_prompt}\n\n{prompt}"
            continue
        retry = runners.next_lane(profile, parent_env, run["log_path"],
                                  quota_fell_back) if status == "failed" else None
        if retry:
            quota_fell_back = True
            profile = retry
            print(f"orchestra: run {run_id}: quota exhausted; retrying on the api lane",
                  file=sys.stderr)
            continue
        break

    if transport == "acp":
        env = runners.apply_backend_env(
            profile, config.apply_worker_env(
                cfg, enrich_path(dict(os.environ, ORCHESTRA_ROOT=str(root),
                                      ORCHESTRA_RUN_ID=str(run_id),
                                      **{auth.TOKEN_ENV: run_token})), root))
        status, exit_code = acp.supervise_run(
            con, run, profile, prompt=prompt, run_id=run_id, env=env,
            deadline=deadline, stall_timeout=stall_timeout)
        run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()

    # --- finalization ---
    result = finalize_run(
        con, run, status, exit_code, last_msg_file=last_msg_file,
        restart_note=restart_note)
    status = result["status"]

    # Optional policy consumes the durable result. Landing remains strictly
    # after release_worktree, which finalize_run owns.
    landed = merge.at_completion(con, cfg, result)
    if landed:
        summary = result["summary"]
        summary = (f"{summary}\n\n{landed}" if summary else landed)[:2000]
        con.execute("UPDATE runs SET summary=? WHERE id=?", (summary, run_id))
        con.commit()
        result = dict(con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
    try:  # DESIGN §9: code files the handoff, never the agent
        findings.at_completion(con, cfg, result)
    except Exception as exc:  # filing must never break finalization
        print(f"orchestra: run {run_id} handoff filing failed: {exc}", file=sys.stderr)

    # SEAM (W-0166): the §7 retry rule. Infrastructure-shaped failures are
    # retried once with the same brief; a second one escalates. Runs BEFORE
    # the dependency release, so a retry re-points the waiting dependents
    # instead of letting them be declined.
    observer.after_terminal(con, run_id)
    process_ready(con, spawn_supervisor)  # dependency release (D3)
    con.close()
    return 0 if status == "done" else 1
