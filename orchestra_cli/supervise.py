"""Detached supervisor: runs one worker process, tracks it, reports back."""
import os
import shutil
import signal
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from orchestra_cli import config, db, host, runners


def _work_log(root: Path, item: str, text: str) -> None:
    if item and shutil.which("work"):
        try:
            subprocess.run(["work", "log", item, text], cwd=root,
                           capture_output=True, timeout=20)
        except Exception:
            pass


def supervise(root: Path, run_id: int) -> int:
    con = db.connect(root)
    run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not run:
        raise SystemExit(f"orchestra: run {run_id} not found")
    cfg = config.load(root)
    agent = config.agent_cfg(cfg, run["agent"])
    timeout = int(agent.get("timeout") or cfg["settings"].get("timeout", 3600))

    prompt = Path(run["brief_path"]).read_text() if run["brief_path"] else run["title"]
    add_dirs = []
    if run["workdir"] != str(root):
        add_dirs.append(str(root))  # isolated runs still write .orchestra/.work at root
    last_msg_file = None
    # ensemble leads attach to the persistent host so their teammate sessions
    # survive the client process exiting mid-mission
    attach = host.ensure() if agent.get("ensemble") else None
    cmd = runners.build_cmd(agent, workdir=run["workdir"], title=f"orchestra-run-{run_id}",
                            prompt=prompt, resume_ref=run["session_ref"] if run["parent_run"] else None,
                            add_dirs=add_dirs, attach=attach)
    if agent["backend"] == "codex" and not run["parent_run"]:
        last_msg_file = tempfile.NamedTemporaryFile(delete=False, suffix=".txt").name
        cmd = cmd[:2] + ["-o", last_msg_file] + cmd[2:]  # `codex exec -o FILE ...` (fresh runs only)

    env = dict(os.environ, ORCHESTRA_SELF=run["agent"], ORCHESTRA_ROOT=str(root))
    log_path = run["log_path"]
    status, exit_code = "done", None
    with open(log_path, "ab") as log:
        log.write((" ".join(cmd[:6]) + " ...\n").encode())
        # stdin MUST be closed: codex exec (and possibly others) block reading a
        # piped stdin to EOF, which never comes from a detached/background parent
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT,
                                cwd=run["workdir"], env=env, start_new_session=True)
        con.execute("UPDATE runs SET pid=?, status='running' WHERE id=?", (proc.pid, run_id))
        con.commit()
        try:
            exit_code = proc.wait(timeout=timeout)
            status = "done" if exit_code == 0 else "failed"
        except subprocess.TimeoutExpired:
            status = "timeout"
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                exit_code = proc.wait(timeout=15)
            except Exception:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    pass

    handoff_body = None

    def _handoff():
        return con.execute(
            "SELECT body FROM messages WHERE sender=? AND created_at>=? "
            "AND (run_id=? OR body LIKE ?) ORDER BY id DESC LIMIT 1",
            (run["agent"], run["started_at"], run_id, f"HANDOFF run {run_id}:%")).fetchone()

    if agent.get("ensemble") and status != "killed":
        # attach mode: the mission may continue server-side after the client
        # exits (teammate wake-ups re-prompt the lead). Completion = HANDOFF.
        started = datetime.strptime(run["started_at"], "%Y-%m-%dT%H:%M:%SZ") \
            .replace(tzinfo=timezone.utc).timestamp()
        deadline = max(started + timeout, time.time() + 120)
        while not _handoff() and time.time() < deadline:
            cur = con.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
            if cur["status"] == "killed":
                break
            time.sleep(5)
        ho = _handoff()
        if ho:
            status, exit_code, handoff_body = "done", 0, ho["body"]
        elif status == "done":
            status = "timeout" if time.time() >= deadline else status

    session_ref, last_text = runners.parse_log(log_path)
    if handoff_body:
        last_text = handoff_body
    if last_msg_file and Path(last_msg_file).is_file():
        txt = Path(last_msg_file).read_text(errors="replace").strip()
        if txt:
            last_text = txt
        os.unlink(last_msg_file)
    summary = (last_text or "").strip()[:2000] or None
    # a killed run may have been marked by `orchestra kill`
    cur_status = con.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()["status"]
    if cur_status == "killed":
        status = "killed"
    con.execute(
        "UPDATE runs SET status=?, exit_code=?, session_ref=COALESCE(?, session_ref), "
        "summary=?, finished_at=? WHERE id=?",
        (status, exit_code, session_ref, summary, db.now(), run_id))
    body = (f"[run {run_id}] {run['agent']} finished: {status}"
            f"{f' (exit {exit_code})' if exit_code not in (None, 0) else ''}."
            f"{chr(10) + 'Last output: ' + summary[:800] if summary else ''}\n"
            f"Details: `orchestra run show {run_id}` · logs: `orchestra logs {run_id}`"
            + (f" · follow up: `orchestra reply {run_id} \"...\"`" if session_ref else ""))
    con.execute("INSERT INTO messages(sender, recipient, body, work_item, run_id, created_at) "
                "VALUES('orchestra', ?, ?, ?, ?, ?)",
                (run["requested_by"], body, run["work_item"], run_id, db.now()))
    con.execute("INSERT INTO feed(author, body, work_item, run_id, created_at, tags) "
                "VALUES('orchestra', ?, ?, ?, ?, 'run')",
                (f"run {run_id} ({run['agent']}) -> {status}", run["work_item"], run_id, db.now()))
    con.commit()
    if run["work_item"]:
        _work_log(root, run["work_item"],
                  f"orchestra run {run_id} ({run['agent']}) finished: {status}."
                  + (f" {summary[:300]}" if summary else ""))
    con.close()
    return 0 if status == "done" else 1
