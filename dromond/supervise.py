"""Detached supervisor: runs one worker process to completion.

DESIGN D3, simplified from Orchestra's 1,388-LOC counterpart:
- Liveness is process-level and costs zero tokens: a growing log is
  progress; silence past ``stall_timeout`` kills; the hard ``timeout``
  caps everything. No periodic check-in injection (killed), no quota
  checks (phase 4), no child runs (later phase).
- ``dromond tell`` / ``dromond interrupt`` record a pending delivery; the
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
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from dromond import (acp, auth, brief, config, db, dispatch, findings,
                         merge, messaging, observer, paths, project, runners,
                         traces, worktree)

EARLY_REF_WINDOW = 90  # seconds to keep scanning the log for a session ref
POLL_INTERVAL = 0.5


def spawn_supervisor(root: Path, run_id: int) -> None:
    exe = shutil.which("dromond")
    cmd = [exe, "_supervise", str(run_id), "--root", str(root)] if exe else \
        [sys.executable, "-m", "dromond", "_supervise", str(run_id), "--root", str(root)]
    # A supervisor that dies before writing anything used to leave no trace at
    # all: stderr went to /dev/null, so run 9's instant death was invisible
    # until its workdir was inspected by hand. Keep the last words.
    err = paths.logs_dir() / f"supervisor-{run_id}.log"
    try:
        handle = open(err, "ab")
    except OSError:
        handle = subprocess.DEVNULL
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=handle, start_new_session=True)
    if handle is not subprocess.DEVNULL:
        handle.close()
    # Keep Popen alive and reap the detached child without delaying dispatch.
    threading.Thread(target=proc.wait, daemon=True).start()


# --- launch preparation (shared by dispatch and deferred release) ----------

def prepare_launch(con, root: Path, cfg: dict, run, *, mission: str,
                   context: str | None = None, use_worktree: bool = False,
                   work_snapshot: str | None = None) -> None:
    """Create the workdir, brief, and log for a run row about to launch."""
    run_id = int(run["id"])
    profile = config.profile_cfg(cfg, run["profile"])
    workdir, branch = str(root), None
    if use_worktree:
        wt, branch = worktree.create(root, run_id, project.dir_key_for(con, run),
                                     backend=profile["backend"])
        workdir = str(wt)
    base_commit = None
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
    text = brief.compose(run_id=run_id, slug=run["slug"], profile=profile,
                         mission=mission, requester=run["requested_by"],
                         root=root, workdir=workdir, extra_context=context,
                         work_snapshot=work_snapshot,
                         work_item=item if (item or "").startswith("W-") else None,
                         recent_commits=worktree.recent_commits(Path(workdir)))
    bp = paths.briefs_dir() / f"run-{run_id}.md"
    bp.write_text(text)
    lp = paths.logs_dir() / f"run-{run_id}.jsonl"
    lp.touch()
    con.execute(
        "UPDATE runs SET brief_path=?, log_path=?, workdir=?, branch=?, base_commit=? "
        "WHERE id=?",
        (str(bp), str(lp), workdir, branch, base_commit, run_id),
    )


def rehome(con, root: Path, previous) -> tuple[str, str | None, str | None]:
    """SEAM (W-0191): where a run started FROM ``previous`` stands, and on what
    branch. Returns ``(workdir, branch, base_commit)``.

    Every path that re-dispatches from an earlier run goes through here —
    ``create_followup`` (continuation) and ``observer._retry_row`` (retry) —
    because the earlier run's world may be GONE: a terminal run gives its
    worktree back (DESIGN §2) and a merged run's branch is deleted (§9). A row
    that copies both fields starts a process in a directory that no longer
    exists and dies instantly with ``FileNotFoundError`` (live runs 9 and 28).

    So: an isolated previous run whose worktree was released gets a FRESH
    worktree on a fresh branch; a shared-checkout one falls back to the project
    root, which is always there. A workdir that still exists is kept unchanged.
    """
    workdir = Path(previous["workdir"] or root)
    branch = previous["branch"]
    if not workdir.exists():
        # The caller's root is the live checkout. project.root_for falls back
        # to the run's own workdir when the project is unknown, which is the
        # very path that is gone — so only consult it if the caller's is not
        # a repository.
        base = Path(root)
        if not (base / ".git").exists():
            base = project.root_for(con, previous)
        workdir, branch = base, None
        if previous["branch"]:  # it was isolated; give it isolation again
            seat = con.execute(
                "SELECT COALESCE(MAX(id), 0) + 1 AS n FROM runs").fetchone()["n"]
            try:
                workdir, branch = worktree.create(
                    base, int(seat), project.dir_key_for(con, previous))
            except (RuntimeError, SystemExit) as exc:
                print(f"dromond: re-dispatch falls back to the shared checkout "
                      f"({exc})", file=sys.stderr)
    base_commit = previous["base_commit"]
    if (workdir / ".git").exists():
        try:
            base_commit = worktree.head(workdir)
        except RuntimeError:
            base_commit = None
    return str(workdir), branch, base_commit


def create_followup(con, root: Path, parent, requester: str, text: str,
                    title: str | None = None) -> int:
    """New run row that resumes the parent's backend session with ``text``."""
    workdir, branch, base_commit = rehome(con, root, parent)
    cur = con.execute(
        "INSERT INTO runs(profile, backend, model, title, requested_by, workdir, "
        "project_id, branch, base_commit, parent_run, session_ref, status, started_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?, 'spawning', ?)",
        (parent["profile"], parent["backend"], parent["model"],
         title or f"continuation of run {parent['id']}", requester,
         str(workdir), parent["project_id"], branch, base_commit,
         parent["id"], parent["session_ref"], db.now()))
    run_id = int(cur.lastrowid)
    bp = paths.briefs_dir() / f"run-{run_id}.md"
    landed = (worktree.recent_commits(Path(root), since=parent["base_commit"])
              if parent["base_commit"] and (Path(root) / ".git").exists() else [])
    bp.write_text(brief.compose_continuation(run_id=run_id, parent_run=parent["id"],
                                             instructions=text, landed=landed))
    lp = paths.logs_dir() / f"run-{run_id}.jsonl"
    lp.touch()
    con.execute("UPDATE runs SET brief_path=?, log_path=? WHERE id=?",
                (str(bp), str(lp), run_id))
    con.commit()
    return run_id


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
        raise SystemExit("dromond: stall_timeout must be an integer number of seconds")
    if seconds < 0:
        raise SystemExit("dromond: stall_timeout must be zero or positive")
    return seconds or None


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
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, cwd=run["workdir"],
                                env=env, start_new_session=True)
    cur = con.execute(
        "UPDATE runs SET pid=?, status='running' "
        f"WHERE id=? AND status NOT IN {db.TERMINAL_SQL}",
        (proc.pid, run_id))
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

    Shared-tree runs write into the human's checkout; Dromond never
    auto-commits there — the worker is told to commit before stopping.
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
                 "-c", "user.name=Dromond", "-c", "user.email=dromond@localhost",
                 "-c", "commit.gpgSign=false",
                 "commit", "--no-verify", "-m",
                 f"dromond: checkpoint run {run['id']} ({terminal_status})"],
                capture_output=True, text=True, timeout=60)
            if committed.returncode != 0:
                raise RuntimeError(
                    f"automatic checkpoint commit failed: {committed.stderr.strip()}")
    head = worktree.head(workdir)
    return head if head != run.get("base_commit") else None


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

    ponytail: only this path stamps usage, so a run finalized by the daemon's
    orphan reap (a supervisor that died) keeps nulls. Call this from the reap
    too if abandoned runs ever hold enough spend to matter.
    """
    usage = runners.parse_usage(log_path, backend) if log_path else runners.EMPTY_USAGE
    con.execute(
        "UPDATE runs SET tokens_in=?, tokens_out=?, tokens_total=?, cost_usd=?, "
        "usage_source=? WHERE id=?",
        (usage["tokens_in"], usage["tokens_out"], usage["tokens_total"],
         usage["cost_usd"], usage["usage_source"], run_id))


def process_ready(con, launcher) -> list[dict]:
    """Event-driven dependency release: decline broken chains, launch ready runs.

    Called after every run finalization and after every ``--after`` dispatch.
    One central database holds every project's runs, so root and config are
    resolved per released run rather than passed in.
    """
    results: list[dict] = []
    if dispatch.paused(con):
        return results  # W-0164: pause stops new runs starting, in-flight ones run on
    # Decline, cascading: a requires_success edge from an unsuccessful terminal
    # prerequisite fails the dependent, which may fail its own dependents.
    # ponytail: wait_for edges are schema-reserved; implement their release
    # rule when something can create them.
    while True:
        broken = [int(r["run_id"]) for r in con.execute(
            "SELECT DISTINCT d.run_id FROM deferred_dispatches d "
            "JOIN dispatch_dependencies e ON e.run_id=d.run_id "
            "JOIN runs p ON p.id=e.depends_on_run "
            "WHERE d.status='pending' AND e.kind='requires_success' "
            "AND p.status IN ('failed','timeout','killed')")]
        if not broken:
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
    ready = list(con.execute(
        "SELECT d.run_id, d.mission, d.context, d.use_worktree "
        "FROM deferred_dispatches d JOIN runs r ON r.id=d.run_id "
        "WHERE d.status='pending' AND r.status='pending' AND NOT EXISTS ("
        "  SELECT 1 FROM dispatch_dependencies e "
        "  JOIN runs p ON p.id=e.depends_on_run "
        "  WHERE e.run_id=d.run_id AND p.status!='done') "
        "ORDER BY d.run_id"))
    for req in ready:
        run_id = int(req["run_id"])
        claimed = con.execute(
            "UPDATE deferred_dispatches SET status='processing' "
            "WHERE run_id=? AND status='pending'", (run_id,))
        con.commit()
        if claimed.rowcount != 1:
            continue  # a concurrent finalizer claimed it first
        con.execute("UPDATE runs SET status='spawning', started_at=? "
                    "WHERE id=? AND status='pending'", (db.now(), run_id))
        con.commit()
        run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        root = project.root_for(con, run)
        try:
            prepare_launch(con, root, config.load(run["project_id"]), run,
                           mission=req["mission"], context=req["context"],
                           use_worktree=bool(req["use_worktree"]))
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
            con.execute("UPDATE runs SET status='failed', summary=?, finished_at=? "
                        "WHERE id=? AND status='spawning'",
                        (f"Deferred launch failed: {error}", db.now(), run_id))
            con.commit()
            results.append({"run_id": run_id, "status": "failed", "error": error})
    return results


# --- the run loop -----------------------------------------------------------

def supervise(root: Path, run_id: int) -> int:
    con = db.connect()
    run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not run:
        raise SystemExit(f"dromond: run {run_id} not found")
    # Claim the run for THIS supervisor process so a supervisor that dies
    # before the completion UPDATE leaves a detectable orphan.
    con.execute("UPDATE runs SET supervisor_pid=? WHERE id=?", (os.getpid(), run_id))
    con.commit()
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
    prompt = Path(run["brief_path"]).read_text() if run["brief_path"] else (run["title"] or "")
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
    while transport == "exec":
        cmd = runners.build_cmd(profile, workdir=run["workdir"],
                                title=f"dromond-run-{run_id}", prompt=prompt,
                                resume_ref=resume_ref)
        last_msg_file = None
        if profile["backend"] == "codex" and not resume_ref:
            last_msg_file = tempfile.NamedTemporaryFile(delete=False, suffix=".txt").name
            cmd = cmd[:2] + ["-o", last_msg_file] + cmd[2:]  # `codex exec -o FILE ...`
        env = dict(os.environ, DROMOND_ROOT=str(root), DROMOND_RUN_ID=str(run_id),
                   **{auth.TOKEN_ENV: run_token})
        env = config.apply_worker_env(cfg, env, root)
        env = runners.apply_backend_env(profile, env)
        outcome, exit_code = _run_proc(con, run, cmd, env, run["log_path"],
                                       run_id, deadline, stall_timeout)
        run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if outcome == "timeout":
            status = "timeout"
            break
        if run["status"] == "killed":
            status = "killed"
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
            print(f"dromond: run {run_id}: {restart_note}", file=sys.stderr)
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
        break

    if transport == "acp":
        env = runners.apply_backend_env(
            profile, config.apply_worker_env(
                cfg, dict(os.environ, DROMOND_ROOT=str(root),
                          DROMOND_RUN_ID=str(run_id),
                          **{auth.TOKEN_ENV: run_token}), root))
        status, exit_code = acp.supervise_run(
            con, run, profile, prompt=prompt, run_id=run_id, env=env,
            deadline=deadline, stall_timeout=stall_timeout)
        run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()

    # --- finalization ---
    latest = con.execute("SELECT status, summary FROM runs WHERE id=?",
                         (run_id,)).fetchone()
    if latest and latest["status"] == "killed":
        status, exit_code = "killed", None
    traces.ingest(con, run_id, run["log_path"], run["backend"])  # final tail
    session_ref, last_text = runners.parse_log(run["log_path"])
    if latest and latest["summary"] and (
            (status == "timeout" and latest["summary"].startswith("Stalled:"))
            # SEAM (W-0104): same rule for the ACP transport's own reason. A
            # dead peer produced no assistant text, so the last line in the
            # log is not what went wrong — the recorded reason is.
            or latest["summary"].startswith(acp.FAILURE_PREFIX)):
        last_text = latest["summary"]
    if last_msg_file and Path(last_msg_file).is_file():
        txt = Path(last_msg_file).read_text(errors="replace").strip()
        if txt:
            last_text = txt
        os.unlink(last_msg_file)
    summary = (last_text or "").strip()[:2000] or None
    if status != "done" and not summary:
        # A backend that dies before speaking (revoked auth, dead quota) still
        # logs why. Without this the human sees "failed" and nothing else.
        summary = runners.parse_failure(run["log_path"])
    if restart_note:
        # The transcript's last words say nothing about the session that was
        # missing, so the row a human reads first has to carry it.
        summary = (f"{summary}\n\n{restart_note}" if summary else restart_note)[:2000]
    checkpoint_commit = None
    try:
        checkpoint_commit = _checkpoint_commit(dict(run), status)
    except RuntimeError as exc:
        if status == "done":
            status, exit_code = "failed", exit_code or 1
        note = f"Checkpoint error: {exc}"
        summary = (f"{summary}\n\n{note}" if summary else note)[:2000]
    kept = release_worktree(con, dict(run), status)
    if kept:
        summary = (f"{summary}\n\n{kept}" if summary else kept)[:2000]
    # SEAM (W-0174): DESIGN §9 — the supervisor lands a verified run's branch.
    # Strictly after release_worktree: git refuses to delete a branch a
    # worktree still holds. It reports itself and never raises.
    landed = merge.at_completion(con, cfg, dict(run), status)
    if landed:
        summary = (f"{summary}\n\n{landed}" if summary else landed)[:2000]
    con.execute(
        "UPDATE runs SET status=?, exit_code=?, session_ref=COALESCE(?, session_ref), "
        "summary=?, checkpoint_commit=?, finished_at=? WHERE id=?",
        (status, exit_code, session_ref, summary, checkpoint_commit, db.now(), run_id))
    # W-0098 seam: the run is over, so anything still queued for it will never
    # arrive. Mark it with the reason and surface it; never move it to a later
    # run, which would hand a correction to a run that never saw the context.
    stranded = messaging.mark_undeliverable(
        con, run_id, f"run ended ({status}) before the message reached a boundary")
    record_usage(con, run_id, run["backend"], run["log_path"])
    body = (f"run {run_id} finished: {status}"
            + (f" (exit {exit_code})" if exit_code not in (None, 0) else "")
            + (f"\n{stranded} message(s) were never delivered — see "
               f"`dromond show {run_id}`" if stranded else "")
            + (f"\n{summary[:800]}" if summary else ""))
    con.execute(
        "INSERT INTO messages(run_id, sender, body, kind, created_at) "
        "VALUES(?, 'dromond', ?, 'completion', ?)", (run_id, body, db.now()))
    con.commit()
    try:  # DESIGN §9: code files the handoff, never the agent
        findings.at_completion(con, cfg, dict(run), last_text, status)
    except Exception as exc:  # filing must never break finalization
        print(f"dromond: run {run_id} handoff filing failed: {exc}", file=sys.stderr)

    # SEAM (W-0166): the §7 retry rule. Infrastructure-shaped failures are
    # retried once with the same brief; a second one escalates. Runs BEFORE
    # the dependency release, so a retry re-points the waiting dependents
    # instead of letting them be declined.
    observer.after_terminal(con, run_id)
    process_ready(con, spawn_supervisor)  # dependency release (D3)
    con.close()
    return 0 if status == "done" else 1
