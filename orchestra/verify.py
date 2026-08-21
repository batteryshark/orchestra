"""Sign-off is a run (W-0269): verification earns done, or blocks with reasons.

When a swept item reaches ``review``, and ``[work] verify`` is on, the runner
records a verification run on ``verify_profile`` — never the worker's profile
or session — and executes each acceptance criterion's stated method against
landed main. All pass: it ticks them and appends ``fact: verified``, which
reads **done** on top of the worker's landing. Any fail: ``fact: halted``
naming those criteria, which reads blocked. Surface lane, no ring.

The facts belong to the WORKER's claim window: a sign-off never claims, so a
human move made in the meantime dismisses this run's narrative too.

ponytail: stated methods are mechanical (command / grep / test / read), so
code runs them. A model turn waits until a criterion needs judgment.
"""
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path

from orchestra import config, db, names, project
from orchestra.work_client import WorkClient, fact_line, verifier_identity

METHOD_TIMEOUT = 60
REQUESTED_BY = "verify"


def after_report(con, cfg: dict, client: WorkClient, actions: list) -> None:
    """Sign off each item this pass just moved to review."""
    for action in list(actions):
        if action.get("action") != "report" or action.get("to") != "review":
            continue
        try:
            sign_off(con, cfg, client, action["item"], action["run"], actions)
        except Exception as exc:
            print(f"orchestra verify: {action['item']} failed: {exc}")


def sign_off(con, cfg: dict, client: WorkClient, item_id: str, worker_id: int,
             actions: list) -> None:
    worker = con.execute("SELECT * FROM runs WHERE id=?", (worker_id,)).fetchone()
    if worker is None or worker["requested_by"] == REQUESTED_BY:
        return
    already = con.execute(
        "SELECT id FROM runs WHERE work_item=? AND requested_by=?",
        (item_id, REQUESTED_BY)).fetchone()
    if already:
        return
    pcfg = config.load(worker["project_id"]) if worker["project_id"] else cfg
    work = dict(pcfg.get("work") or {})
    if not work.get("verify"):
        return
    profile_name = (work.get("verify_profile") or "").strip()
    if not profile_name:
        print(f"orchestra verify: {item_id} skipped — no verify_profile")
        return
    if profile_name == worker["profile"]:
        print(f"orchestra verify: {item_id} skipped — verify_profile is the "
              f"worker's profile ({profile_name})")
        return
    try:
        profile = config.staff_profile(pcfg, profile_name)
    except SystemExit as exc:
        print(f"orchestra verify: {item_id} not staffed: {exc}")
        return
    root = project.root_for(con, worker)
    run_id, slug = _insert(con, worker, root, profile_name, profile, item_id)
    verifier = WorkClient(client.api_url, identity=verifier_identity(slug),
                          timeout=client.timeout)
    try:
        results = _execute(verifier, item_id, root)
        failed = [r for r in results if not r["ok"]]
        _writeback(verifier, item_id, run_id, results, failed)
        status = "done" if not failed else "failed"
        summary = _summary(item_id, results, failed)
        target = "done" if not failed else "blocked"
    except Exception as exc:
        status, summary, target = "failed", str(exc)[:300], None
        print(f"orchestra verify: {item_id} aborted: {exc}")
    _finalize(con, run_id, status, summary)
    actions.append({"action": "verify", "item": item_id, "run": run_id,
                    "to": target})


def stated_method(text: str) -> str:
    """The method a criterion names: the clause after an em dash, else all of it."""
    for sep in (" — ", " – ", " -- "):
        if sep in text:
            return text.rsplit(sep, 1)[1].strip()
    return text.strip()


def run_method(root: Path, method: str) -> tuple[bool, str]:
    """Execute one stated method against landed main. Returns (ok, one-line note)."""
    kind, arg = _classify(method)
    root = Path(root).resolve()
    if kind == "read":
        path = (root / arg).resolve()
        if not path.is_relative_to(root):
            return False, f"read {arg}: path outside project"
        if path.is_file():
            return True, f"read {arg}: present"
        return False, f"read {arg}: not a file"
    if kind == "grep":
        r = subprocess.run(
            ["grep", "-R", "-n", "-I", "--exclude-dir=.git", "-e", arg, "."],
            cwd=str(root), capture_output=True, text=True, timeout=METHOD_TIMEOUT)
        note = f"grep {arg}: " + ("matched" if r.returncode == 0 else "no match")
        return r.returncode == 0, note
    if kind == "test":
        cmd = [sys.executable, "-m", "unittest"] + (shlex.split(arg) if arg else ["discover"])
        return _command(root, cmd, method)
    return _command(root, shlex.split(method), method)


def _classify(method: str) -> tuple[str, str]:
    low = method.lower()
    if low.startswith("read "):
        return "read", method.split(None, 1)[1]
    if low.startswith("grep "):
        return "grep", method.split(None, 1)[1]
    if low == "test" or low.startswith("test "):
        return "test", method.split(None, 1)[1] if " " in method else ""
    return "command", method


def _command(root: Path, cmd: list[str], label: str) -> tuple[bool, str]:
    if not cmd:
        return False, f"{label}: empty command"
    try:
        r = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True,
                           timeout=METHOD_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, f"{label}: timed out after {METHOD_TIMEOUT}s"
    except OSError as exc:
        return False, f"{label}: {exc}"
    return r.returncode == 0, f"{label}: exit {r.returncode}"


def _execute(client: WorkClient, item_id: str, root: Path) -> list[dict]:
    task = client.task(item_id) or {}
    results = []
    for index, entry in enumerate(task.get("acceptanceCriteria") or []):
        text = (entry.get("text") or "").strip()
        method = stated_method(text)
        if not method:
            ok, note = False, f"{text or '(empty)'}: no stated method"
        else:
            try:
                ok, note = run_method(root, method)
            except subprocess.TimeoutExpired:
                ok, note = False, f"{method}: timed out after {METHOD_TIMEOUT}s"
        client.log_task(item_id, note[:19000])
        if ok:
            client.check_task_item(item_id, "acceptance", index, checked=True)
        results.append({"index": index, "text": text, "ok": ok, "note": note})
    return results


def _summary(item_id: str, results: list[dict], failed: list[dict]) -> str:
    if not failed:
        return f"Verified. {item_id} is done."
    names_ = ", ".join(r["text"] or f"AC{r['index']}" for r in failed)
    return f"Blocked. {item_id} failed: {names_}"[:300]


def _writeback(client: WorkClient, item_id: str, run_id: int,
               results: list[dict], failed: list[dict]) -> None:
    tag = f"[{client.identity}/{run_id}]"
    if not failed:
        body = (f"{tag} Verified. {item_id} is done.\n\n"
                + ("\n".join(r["note"] for r in results) or "No acceptance criteria.")
                + "\n\nNothing waits.")
        fact = fact_line(tag, "verified")
    else:
        named = "\n".join(f"- {r['text']}: {r['note']}" for r in failed)
        body = (f"{tag} Blocked. {item_id} failed verification.\n\n"
                f"{named}\n\nOpen the blocked lane.")
        fact = fact_line(tag, "halted", reason=(
            f"{item_id} failed verification: " + ", ".join(
                r["text"] or f"AC{r['index']}" for r in failed))[:300])
    client.log_task(item_id, body[:19000])
    client.log_task(item_id, fact)


def _insert(con, worker, root: Path, profile_name: str, profile: dict,
            item_id: str) -> tuple[int, str]:
    title = f"Verify {item_id}"[:80]
    for _ in range(names.MAX_ATTEMPTS + 4):
        slug = names.assign_slug(con)
        try:
            cur = con.execute(
                "INSERT INTO runs(slug, profile, backend, model, title, "
                "requested_by, workdir, project_id, status, started_at, "
                "work_item, parent_run) "
                "VALUES(?,?,?,?,?,?,?,?, 'running', ?,?,?)",
                (slug, profile_name, profile["backend"], profile.get("model"),
                 title, REQUESTED_BY, str(root), worker["project_id"], db.now(),
                 item_id, int(worker["id"])))
            con.commit()
            return int(cur.lastrowid), slug
        except sqlite3.IntegrityError as exc:
            if not names.is_unique_violation(exc):
                raise
            names.reset_memory_cache()
    raise RuntimeError("orchestra: could not mint a unique verify-run slug")


def _finalize(con, run_id: int, status: str, summary: str) -> None:
    con.execute(
        "UPDATE runs SET status=?, summary=?, finished_at=?, work_reported_at=? "
        "WHERE id=?", (status, summary, db.now(), db.now(), run_id))
    con.commit()
