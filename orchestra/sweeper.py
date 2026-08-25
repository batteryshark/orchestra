"""The intervention loop (CONTRACT §4): Orchestra's front door to Work.

Deterministic code only (DESIGN principle 6) — one pass:

- **Report**: a finished run posts an attributed comment and appends its
  fact — ``landed`` / ``halted`` / ``failed``, or ``resolved`` /
  ``needs_human`` on an issue. A run never writes status (CONTRACT 0.8);
  Work derives the board from the facts and the human's own move.
- **Claim**: an item a human marked ``delegated`` (CONTRACT §2; a legacy
  ``agents`` list is history and never counts as delegation) that sits
  in ``ready`` (task) / ``queued`` (issue) with no live run gets one: a
  fresh dispatch, or a session continuation when a prior run for the item
  left a resumable session (the "answer from the phone, the run picks it
  up" path of §4 step 5).
- **Ferry**: a new human comment on an in-flight item is delivered to the
  owning run as a safe-boundary interrupt (the phase-1 delivery path).
- **Cursor**: ``updatedSince`` watermarks live in the meta table and only
  advance after a fully successful pass.
"""
import datetime
import time
from pathlib import Path

from orchestra import (brief, config, db, dispatch, messaging, paths, project,
                         router, supervise, traces, verify, work_client)
from orchestra.work_client import WorkClient, WorkError, fact_line, from_cfg

CURSOR_KEYS = {"task": "work_cursor_tasks", "issue": "work_cursor_issues"}

# Progress-log lines Work itself writes (move/update/checklist/create).
# ponytail: the task log has no author field, so human detection is this
# prefix heuristic; upgrade path is an authored-comment field in Work.
SYSTEM_LOG_PREFIXES = (
    "Moved from ", "Created in ", "Updated ",
    "Completed requirement", "Completed acceptance",
    "Reopened requirement", "Reopened acceptance",
)


def work_cfg(cfg: dict) -> dict:
    """[work] section with defaults applied (config.DEFAULT_CONFIG owns them)."""
    return dict(cfg.get("work", {}))


client_from_cfg = from_cfg  # one Work server; work_client owns the construction


def item_kind(item_id: str) -> str:
    return "task" if item_id.startswith("W-") else "issue"


def is_delegated(item: dict, identity: str) -> bool:
    """CONTRACT §2: the ``delegated`` boolean, and nothing else, hands an item
    to automation.

    A legacy ``agents`` name list used to count here as a tolerant read. It no
    longer does, and must not: that list is history — it recorded which agent
    did the work in an older system — so reading it as delegation once offered
    96 finished records to the runner (Work, lib/local-workspace.mjs). Work no
    longer emits the key at all, which made this branch both dead and pointed
    the wrong way. Delegation is only ever an explicit human tick.
    """
    return bool(item.get("delegated"))


# --- snapshot ---------------------------------------------------------------

def render_snapshot(item: dict, kind: str) -> str:
    """Compact Work-item snapshot for the brief; frozen at dispatch, capped
    at brief.WORK_SNAPSHOT_MAX_CHARS (D6)."""
    state = item.get("status") if kind == "task" else item.get("state")
    lines = [f"{item['id']} · {item.get('title', '')} [{state}]"]
    if item.get("projectPath"):
        lines.append(f"project: {item['projectPath']}")
    if kind == "task":
        sections = item.get("sections") or {}
        for label in ("goal", "requirements", "acceptanceCriteria", "notes"):
            text = (sections.get(label) or "").strip()
            if text:
                lines.append(f"\n## {label}\n{text}")
        log = item.get("log") or []
        if log:
            lines.append("\n## recent log")
            lines += [f"- {e['at']} — {e['message']}" for e in log[-5:]]
    else:
        body = (item.get("body") or "").strip()
        if body:
            lines.append(f"\n{body}")
        messages = item.get("messages") or []
        if messages:
            lines.append("\n## recent thread")
            for m in messages[-5:]:
                author = (m.get("author") or {}).get("name") or \
                    (m.get("author") or {}).get("kind", "?")
                lines.append(f"- {m.get('createdAt', '')} {author}: {m.get('body', '')}")
    return "\n".join(lines)[:brief.WORK_SNAPSHOT_MAX_CHARS]


def _mission(item: dict, kind: str) -> str:
    # Substance stays in the capped snapshot; the mission line stays bounded
    # (D6 keeps every fixed injection under a measured ceiling).
    return (f"Work {kind} {item['id']}: {item.get('title', '')}\n\n"
            "The Work item snapshot below carries the details.")


# --- thread reading ---------------------------------------------------------

def _new_human_comments(item: dict, kind: str, since: str | None,
                        identity: str) -> list[dict]:
    """Comments a human added after ``since`` (ISO timestamps compare
    lexicographically). Our own posts and Work's system log lines are not
    human comments."""
    floor = since or ""
    found = []
    if kind == "issue":
        for m in item.get("messages") or []:
            if (m.get("author") or {}).get("kind") == "human" \
                    and (m.get("createdAt") or "") > floor:
                found.append({"at": m["createdAt"], "text": m.get("body", "")})
        return found
    for e in item.get("log") or []:
        msg = e.get("message", "")
        if (e.get("at") or "") <= floor or msg.startswith(f"[{identity}") \
                or msg.startswith(SYSTEM_LOG_PREFIXES):
            continue
        found.append({"at": e["at"], "text": msg})
    return found


def _comments_text(comments: list[dict], item_id: str) -> str:
    return "\n\n".join(
        f"[Work {item_id} · {c['at']}]\n{c['text']}" for c in comments)


# --- run rows ---------------------------------------------------------------

def _live_run(con, item_id: str):
    return con.execute(
        f"SELECT * FROM runs WHERE work_item=? AND status NOT IN {db.TERMINAL_SQL} "
        "ORDER BY id DESC LIMIT 1", (item_id,)).fetchone()


def _last_session_run(con, item_id: str):
    """Latest run for the item whose backend session can resume, newest first.

    A run that ended ON ITS OWN — `done`, `failed`, `timeout` — is a
    conversation to continue. `killed` and `halted` are not: a human, the
    observer, or the worker itself STOPPED it, so its context is deliberately
    abandoned and its backend session may not exist at all (live run 27
    resumed a killed one, and the backend answered `no session matches` in
    under a second). A stop stops the ITEM, not just one process, so nothing
    behind it is resumed either — the item gets a fresh run.
    """
    for row in con.execute(
            f"SELECT * FROM runs WHERE work_item=? AND status IN {db.TERMINAL_SQL} "
            "ORDER BY id DESC", (item_id,)):
        if row["status"] in ("killed", "halted"):
            return None
        if row["session_ref"]:
            return row
    return None


def _insert_run(con, proj, profile_name: str, profile: dict, title: str,
                item_id: str, seen_ts: str | None,
                routed_reason: str | None = None, *, commit: bool = True):
    """Reserve one fresh Work run through the common admission boundary."""
    return supervise.create_run(
        con, profile=profile_name, backend=profile["backend"],
        model=profile.get("model"), title=title, requested_by="work",
        workdir=str(proj.path), project_id=proj.project_id,
        work_item=item_id, work_seen_ts=seen_ts,
        routed_reason=routed_reason, commit=commit)


# --- seams (W-0099, the conductor) ------------------------------------------
# The conductor dispatches against a goal and reads its thread; both must use
# the shapes THIS pass uses, or a conducted run and a swept run would differ.
insert_run = _insert_run
human_comments = _new_human_comments

# --- seam (W-0183, the staffing turn) ---------------------------------------
# WHICH enabled profile a fresh dispatch gets. One call, at the one moment a
# run is staffed. It returns the [work] profile unchanged on every failure, so
# this seam can never stop a dispatch — see router.choose.
staff = router.choose


# --- the three sub-passes ---------------------------------------------------

def _completion_outcome(run) -> tuple[bool, str]:
    """Map a terminal run to (success, summary).

    The handoff (``findings``/``proposals``) is filed by the supervisor at
    completion, not here — see ``findings.at_completion`` (DESIGN §9). Any
    protocol failure it recorded is already on the summary, so the comment
    this pass posts carries it to the board.
    """
    summary = (run["summary"] or "").strip()
    landing_failed = run["status"] == "done" \
        and run["landing_status"] == "failed"
    if landing_failed:
        reason = next((line.strip() for line in summary.splitlines()
                       if line.strip().startswith(("Merge escalated", "Merge failed"))),
                      None)
        summary = reason or summary
    return run["status"] == "done" and not landing_failed, \
        summary or f"run {run['id']} {run['status']}"


def _headline(run, summary: str) -> str:
    """One board-readable line: what happened and why."""
    first = (summary or "").strip().splitlines()[0] if summary else ""
    reason = f": {first}" if first and not first.startswith(f"run {run['id']}") else ""
    outcome = ("landing failed" if run["status"] == "done"
               and run["landing_status"] == "failed" else run["status"])
    return f"run {run['id']} {outcome}{reason}"[:300]


def _decline_unaccounted(client: WorkClient, item_id: str, run,
                         tag: str) -> int | None:
    """Answer, on the run's behalf, every criterion it left open, and say so.
    Runs before the terminal fact: Work refuses a landing with anything
    unaccounted."""
    declined = client.decline_unaccounted(
        item_id, f"not accounted for by run {run['id']} ({run['status']})")
    if declined:
        print(f"orchestra sweep: {tag} declined {declined} unaccounted "
              f"checklist item(s) on {item_id}")
    return declined


def _report_ready(con, result) -> bool:
    """True once this is the Work item's latest settled result."""
    if not result["handoff_processed_at"]:
        return False
    completed = con.execute(
        "SELECT 1 FROM messages WHERE run_id=? AND kind='completion' LIMIT 1",
        (result["id"],)).fetchone()
    if completed is None:
        return False
    newer = con.execute(
        "SELECT 1 FROM runs WHERE work_item=? AND layer IS NULL AND id>? LIMIT 1",
        (result["work_item"], result["id"])).fetchone()
    if newer is not None:
        return False
    retry = con.execute(
        "SELECT action FROM observations WHERE run_id=? AND layer='retry' "
        "ORDER BY id DESC LIMIT 1", (result["id"],)).fetchone()
    if retry is not None and retry["action"] == "deferred":
        return False
    if result["status"] == "done" and result["branch"] \
            and result["landing_status"] not in ("ok", "failed"):
        return False
    return True


def report_result(con, client: WorkClient, result) -> tuple[bool, dict | None]:
    """Report one terminal run result.

    ``(False, None)`` asks the batch to retry after Work is available;
    ``(True, None)`` means no append is due; ``(True, action)`` is an appended
    report.
    """
    if not result["work_item"]:
        return True, None
    if not _report_ready(con, result):
        return True, None
    item_id, kind = result["work_item"], item_kind(result["work_item"])
    success, summary = _completion_outcome(result)
    tag = f"[{client.identity}/{result['slug'] or result['id']}]"
    comment = (f"{tag} run {result['id']} finished: {result['status']}\n\n"
               f"{summary}")[:19000]
    target = ("review" if success else "blocked") if kind == "task" \
        else ("closed" if success else "needs_human")
    verb = ("landed" if success else
            "halted" if result["status"] == "halted"
            or result["landing_status"] == "failed" else "failed") \
        if kind == "task" else ("resolved" if success else "needs_human")
    fact = (fact_line(
        tag, verb,
        reason=None if success else _headline(result, summary))
        if kind == "task" else fact_line(
            tag, verb,
            **({"summary": summary} if success else {"reason": summary})))
    try:
        remote = (client.task(item_id) if kind == "task"
                  else client.issue(item_id))
        if remote is None:
            return False, None
        entries = (remote.get("log") if kind == "task"
                   else remote.get("messages")) or []
        sent = {entry.get("message" if kind == "task" else "body")
                for entry in entries}
        fact_prefix = f"{tag} fact: {verb}"
        if any(message == fact_prefix or (message or "").startswith(fact_prefix + " ")
               for message in sent):
            _mark_reported(con, result)
            return True, {"action": "report", "item": item_id,
                          "run": result["id"], "to": target}
        if kind == "task":
            if comment not in sent and client.log_task(item_id, comment) is None:
                return False, None
            if _decline_unaccounted(client, item_id, result, tag) is None:
                return False, None
            posted = client.log_task(item_id, fact)
        else:
            # Work collapsed "resolved" into "closed" (2026-08-14): a
            # resolved fact reads closed with its summary; humans reopen.
            if comment not in sent and client.reply_issue(item_id, comment) is None:
                return False, None
            posted = client.reply_issue(item_id, fact)
        if posted is None:  # Work went away mid-pass; retry next pass
            return False, None
    except WorkError as exc:
        if work_client.retryable(exc):
            print(f"orchestra sweep: report {item_id} temporarily refused ({exc})")
            return False, None
        # Terminal: Work refused the append on its own authority (a human
        # closed the issue, the id is not a work item). The outcome is
        # settled without us, so the report stamps reported and never
        # retries — retrying forever turned two closed issues into
        # permanent sweep noise (2026-08-20).
        print(f"orchestra sweep: report {item_id} refused ({exc}) — "
              f"marked reported")
        _mark_reported(con, result)
        return True, None
    _mark_reported(con, result)
    return True, {"action": "report", "item": item_id,
                  "run": result["id"], "to": target}


def _report(con, client: WorkClient, actions: list) -> bool:
    ok = True
    rows = list(con.execute(
        f"SELECT * FROM runs WHERE work_item IS NOT NULL AND status IN {db.TERMINAL_SQL} "
        "AND work_reported_at IS NULL ORDER BY id"))
    for result in rows:
        settled, action = report_result(con, client, result)
        ok &= settled
        if action is not None:
            actions.append(action)
    return ok


def _mark_reported(con, run) -> None:
    """Once only, however it ended (DESIGN: a report is never posted twice)."""
    con.execute("UPDATE runs SET work_reported_at=COALESCE(work_reported_at, ?) WHERE id=?",
                (db.now(), run["id"]))
    con.commit()


def _dep_lookup(client: WorkClient):
    """Status source for a prerequisite the pass did not fetch. One GET, and
    only when an item actually declares a dependency."""
    def lookup(dep_id: str):
        try:
            return (client.task(dep_id) if item_kind(dep_id) == "task"
                    else client.issue(dep_id))
        except WorkError:
            return None
    return lookup


def _mark_claim_pending(con, run):
    """Commit the local reservation and its remote-claim phase together."""
    try:
        changed = con.execute(
            "UPDATE runs SET status='pending', work_claim_status='pending' "
            "WHERE id=?", (run["id"],))
        if changed.rowcount != 1:
            raise RuntimeError(f"run {run['id']} disappeared during claim reservation")
        con.commit()
    except BaseException:
        con.rollback()
        raise
    return con.execute("SELECT * FROM runs WHERE id=?", (run["id"],)).fetchone()


def _claim_state(kind: str, item: dict, client: WorkClient, run) -> str:
    """Return ``claimed``, ``retry``, or ``stale`` from authoritative Work data."""
    if not is_delegated(item, client.identity):
        return "stale"
    if kind == "task":
        expected = fact_line(
            f"[{client.identity}/{run['slug']}]", "claimed", run=int(run["id"]))
        present = any(entry.get("message") == expected
                      for entry in item.get("log") or [])
        if present:
            return "claimed" if item.get("status") == "in_progress" else "stale"
        return "retry" if item.get("status") == "ready" else "stale"
    owner = item.get("claimedBy") or {}
    if owner.get("kind") == "agent" and owner.get("name") == client.identity:
        return "claimed" if item.get("state") == "in_progress" else "stale"
    return "retry" if item.get("state") == "queued" and not owner else "stale"


def _confirm_claim(con, client: WorkClient, kind: str, item: dict, run) -> str:
    """Reconcile before mutating; a lost response leaves the same row pending."""
    state = _claim_state(kind, item, client, run)
    if state == "stale":
        return state
    if state == "retry":
        if kind == "task":
            tag = f"[{client.identity}/{run['slug']}]"
            claimed = client.log_task(
                item["id"], fact_line(tag, "claimed", run=int(run["id"])))
        else:
            claimed = client.claim_issue(item["id"])
        if claimed is None:
            return "deferred"
    con.execute(
        "UPDATE runs SET status='spawning', started_at=? WHERE id=? "
        "AND work_claim_status='pending' AND status IN ('pending','spawning')",
        (db.now(), run["id"]))
    con.commit()
    return "claimed"


def _thread_contains(item: dict, kind: str, body: str) -> bool:
    entries = item.get("log") if kind == "task" else item.get("messages")
    field = "message" if kind == "task" else "body"
    return any(entry.get(field) == body for entry in entries or [])


def _abandon_claim(con, run) -> None:
    """Drop an unprepared refusal, or cleanly terminalize prepared residue."""
    current = con.execute("SELECT * FROM runs WHERE id=?", (run["id"],)).fetchone()
    if current is None:
        return
    if current["brief_path"] is None:
        con.execute("DELETE FROM runs WHERE id=?", (current["id"],))
    else:
        reason = "Work claim no longer authorizes launch"
        con.execute(
            "UPDATE runs SET status='killed', "
            "worker_status=COALESCE(worker_status, 'killed'), "
            "work_claim_status='abandoned', "
            "work_reported_at=COALESCE(work_reported_at, ?), "
            "summary=?, finished_at=COALESCE(finished_at, ?) WHERE id=?",
            (db.now(), reason, db.now(), current["id"]))
        con.commit()
        supervise.fail_launch(
            con, project.root_for(con, current), int(current["id"]),
            reason, prefix="Work claim abandoned")
        # The brief was written for a launch that will never happen; residue
        # of an abandoned claim leaves no file behind.
        Path(current["brief_path"]).unlink(missing_ok=True)
    con.commit()


def _finish_claim(con, client: WorkClient, kind: str, item: dict, run,
                  launcher, actions: list) -> str:
    """Finish one reserved claim. Returns dispatched, deferred, or stale."""
    claim = _confirm_claim(con, client, kind, item, run)
    if claim != "claimed":
        return claim

    run_id = int(run["id"])
    run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    root = project.root_for(con, run)
    pcfg = config.load(run["project_id"])
    prior = (con.execute("SELECT * FROM runs WHERE id=?", (run["parent_run"],)).fetchone()
             if run["parent_run"] is not None else None)
    try:
        if run["brief_path"] is None:
            if prior is not None:
                news = _new_human_comments(
                    item, kind, prior["work_seen_ts"], client.identity)
                text = _comments_text(news, item["id"]) or \
                    f"Work {kind} {item['id']} was handed back; continue the mission."
                supervise.prepare_followup(con, root, prior, run, text)
            else:
                supervise.prepare_launch(
                    con, root, pcfg, run, mission=_mission(item, kind),
                    use_worktree=bool(work_cfg(pcfg).get("worktree", True)),
                    work_snapshot=render_snapshot(item, kind))
        con.execute(
            "UPDATE runs SET work_seen_ts=? WHERE id=?",
            (item.get("updatedAt") or db.now(), run_id))
        con.commit()
    except (Exception, SystemExit) as exc:
        error = str(exc)[:1000] or exc.__class__.__name__
        if prior is None:
            supervise.fail_launch(con, root, run_id, error)
        con.execute("UPDATE runs SET work_claim_status='claimed' WHERE id=?",
                    (run_id,))
        con.commit()
        print(f"orchestra sweep: {item['id']} launch setup failed: {error}")
        actions.append({"action": "launch_failed", "item": item["id"],
                        "run": run_id, "reason": error})
        _report(con, client, actions)
        return "failed"

    tag = f"[{client.identity}/{run['slug'] or run_id}]"
    note = f"{tag} dispatched run {run_id}" + \
        (" (session resumed)" if prior is not None else "")
    if run["routed_reason"]:
        note += f"\nstaffing: {run['routed_reason']}"
    if not _thread_contains(item, kind, note):
        try:
            posted = (client.log_task(item["id"], note) if kind == "task" else
                      client.reply_issue(item["id"], note))
            if posted is None:
                print(f"orchestra sweep: dispatch note for {item['id']} deferred")
        except WorkError as exc:
            print(f"orchestra sweep: dispatch note for {item['id']} rejected: {exc}")
    try:
        launcher(root, run_id)
    except BaseException as exc:
        error = str(exc)[:1000] or exc.__class__.__name__
        supervise.fail_launch(con, root, run_id, error)
        con.execute("UPDATE runs SET work_claim_status='claimed' WHERE id=?",
                    (run_id,))
        con.commit()
        actions.append({"action": "launch_failed", "item": item["id"],
                        "run": run_id, "reason": error})
        _report(con, client, actions)
        return "failed"
    # A crash before this receipt may start another detached supervisor, but
    # supervise() admits exactly one of them before either can start a worker.
    con.execute("UPDATE runs SET work_claim_status='claimed' WHERE id=?",
                (run_id,))
    con.commit()
    actions.append({"action": "dispatch", "item": item["id"], "run": run_id,
                    "resumed": prior is not None})
    return "dispatched"


def _claim(con, cfg: dict, client: WorkClient, items: list,
           launcher, actions: list) -> bool:
    """Dispatch every claimable item. No cap of any kind gates this loop
    (DESIGN §4): all of it goes at once. Order and the waiting queue exist
    for the items that *cannot* go — blocked by a dependency, or held behind
    the pause switch."""
    ok = True
    by_id = {item["id"]: (kind, item) for kind, item in items}
    pending = list(con.execute(
        f"SELECT * FROM runs WHERE work_claim_status='pending' "
        f"AND status NOT IN {db.TERMINAL_SQL} ORDER BY id"))
    for run in pending:
        found = by_id.get(run["work_item"])
        if found is None:
            ok = False
            continue
        kind, item = found
        try:
            outcome = _finish_claim(con, client, kind, item, run,
                                    launcher, actions)
        except WorkError as exc:
            print(f"orchestra sweep: claim {item['id']} rejected: {exc}")
            outcome = "deferred" if work_client.retryable(exc) else "stale"
        if outcome == "stale":
            _abandon_claim(con, run)
        if outcome not in {"dispatched", "failed"}:
            ok = False
    queued = dispatch.waiting_ids(con)
    candidates = []
    for lane, (kind, item) in enumerate(items):
        state = item.get("status") if kind == "task" else item.get("state")
        wanted = "ready" if kind == "task" else "queued"
        # A live run means it is already dispatched — never double-dispatch.
        if not is_delegated(item, client.identity) or state != wanted \
                or _live_run(con, item["id"]):
            if item["id"] in queued:
                dispatch.release(con, item["id"])  # no longer a candidate
            continue
        candidates.append((kind, item, lane))
    planned = dispatch.plan(con, candidates, dispatch.statuses(items),
                            lookup=_dep_lookup(client))
    for kind, item, _lane, reason in planned:
        item_id = item["id"]
        if reason:
            # Honest queue state: it waits, it is not "running", and the row
            # says why. Logged once per change, not once per pass.
            if dispatch.hold(con, item_id, kind, _lane, *reason):
                actions.append({"action": "hold", "item": item_id,
                                "reason": reason[0], "detail": reason[1]})
            continue
        if item_id in queued:
            dispatch.release(con, item_id)
        # Which checkout the run gets is the item's project, not the caller's
        # directory: one daemon sweeps the whole workspace (DESIGN §2).
        proj = project.by_work_path(con, item.get("projectPath"))
        if proj is None and project.refresh(con, cfg):
            proj = project.by_work_path(con, item.get("projectPath"))
        if proj is None:
            print(f"orchestra sweep: {item_id} has no known project "
                  f"({item.get('projectPath')!r}) — skipped")
            continue
        root = proj.path
        # Per-project settings key on projectId (DESIGN §2), and so does the
        # project's ENABLED SET (W-0187) — this is a staffing moment, so the
        # profile is resolved through the gate rather than around it.
        pcfg = config.load(proj.project_id)
        # No default: "claude" was one of the stub profiles removed with the
        # rest (W-0173), so falling back to it now fails with "unknown
        # profile" instead of naming the setting that is actually missing.
        profile_name = work_cfg(pcfg).get("profile")
        if not profile_name:
            actions.append(f"{proj.slug}: no [work] profile set — swept items "
                           "have nothing to staff; set it in "
                           f"{paths.global_config_path()}")
            continue
        try:
            profile = config.staff_profile(pcfg, profile_name)
        except SystemExit as exc:
            # A profile this project has not enabled — or a [work] profile
            # that is not configured at all. One misconfigured project must
            # not end the pass for the whole workspace, and the refusal is
            # never a silent fallback to some other profile.
            print(f"orchestra sweep: {item_id} not staffed: {exc}")
            ok = False
            continue
        routed = None
        run = None
        try:
            prior = _last_session_run(con, item_id)
            if prior:
                run, blocked = supervise.reserve_followup(
                    con, root, prior, "work", title=prior["title"], commit=False)
            else:
                # SEAM (W-0183): the staffing turn. A fresh dispatch is the
                # one moment a profile is CHOSEN — a continuation above keeps
                # the preset its lineage launched with, which is the same
                # reason nothing revalidates a run in flight (W-0187).
                profile_name, profile, routed = staff(
                    con, pcfg, render_snapshot(item, kind), profile_name, profile)
                late_pause = dispatch.pause_state(con)
                if late_pause is not None:
                    if dispatch.hold(con, item_id, kind, _lane, "paused",
                                     late_pause.get("note")):
                        actions.append({"action": "hold", "item": item_id,
                                        "reason": "paused",
                                        "detail": late_pause.get("note")})
                    continue
                # The run TITLE is the item's own title. The id lives on the
                # row (`work_item`) and at the head of the brief, so repeating
                # "Work issue issue_abc123:" here only eats the width the
                # board has for saying what the run is about.
                run, blocked = _insert_run(
                    con, proj, profile_name, profile,
                    (item.get("title") or item_id)[:80], item_id,
                    item.get("updatedAt"), routed, commit=False)
            if run is None:
                if blocked == "paused":
                    late_pause = dispatch.pause_state(con) or {}
                    if dispatch.hold(con, item_id, kind, _lane, "paused",
                                     late_pause.get("note")):
                        actions.append({"action": "hold", "item": item_id,
                                        "reason": "paused",
                                        "detail": late_pause.get("note")})
                continue
            run = _mark_claim_pending(con, run)
            outcome = _finish_claim(
                con, client, kind, item, run, launcher, actions)
            if outcome == "stale":
                _abandon_claim(con, run)
            if outcome not in {"dispatched", "failed"}:
                ok = False
        except WorkError as exc:
            print(f"orchestra sweep: claim {item_id} rejected: {exc}")
            if run is not None and not work_client.retryable(exc):
                _abandon_claim(con, run)
            ok = False
    return ok


def _ferry(con, client: WorkClient, items: list, actions: list) -> bool:
    for kind, item in items:
        run = _live_run(con, item["id"])
        if not run or run["work_seen_ts"] is None:
            continue
        # A claim still pending confirmation never receives tells: nothing has
        # launched, and _finish_claim folds these same comments into the
        # mission it launches with — ferrying now would deliver them twice.
        if run["work_claim_status"] == "pending":
            continue
        news = _new_human_comments(item, kind, run["work_seen_ts"], client.identity)
        if not news:
            continue
        if not run["session_ref"]:
            continue  # not resumable yet; the next pass retries
        try:
            messaging.queue_tell(
                con, run["id"], f"work:{item['id']}",
                _comments_text(news, item["id"]), run["log_path"],
                work_seen_ts=max(n["at"] for n in news))
        except messaging.RunClosed:
            continue
        actions.append({"action": "ferry", "item": item["id"], "run": run["id"],
                        "comments": len(news)})
    return True


def _iso_ago(seconds: int) -> str:
    return datetime.datetime.fromtimestamp(
        time.time() - seconds, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _progress(con, cfg: dict, client: WorkClient, actions: list) -> None:
    """Post a heartbeat for live runs so the board is not silent mid-run.

    Derived from the run's trace at posting time (traces.progress), so it
    costs a file read rather than a model turn -- the worker is never asked
    to report. Rate-limited by [work] progress_interval; 0 disables.
    """
    interval = int(work_cfg(cfg).get("progress_interval", 900) or 0)
    if interval <= 0:
        return
    cutoff = _iso_ago(interval)
    rows = con.execute(
        f"SELECT * FROM runs WHERE work_item IS NOT NULL AND status NOT IN {db.TERMINAL_SQL} "
        "AND (work_progress_at IS NULL OR work_progress_at < ?)", (cutoff,))
    for run in list(rows):
        note = traces.progress(run["log_path"], run["backend"])
        if not note:
            continue
        item_id, tag = run["work_item"], f"[{client.identity}/{run['slug'] or run['id']}]"
        body = f"{tag} still working — {note}"
        posted = (client.log_task(item_id, body) if item_kind(item_id) == "task"
                  else client.reply_issue(item_id, body))
        if posted is None:
            continue
        con.execute("UPDATE runs SET work_progress_at=? WHERE id=?", (db.now(), run["id"]))
        con.commit()
        actions.append({"action": "progress", "item": item_id, "run": run["id"]})


# --- one pass ---------------------------------------------------------------

def sweep(cfg: dict, client: WorkClient,
          launcher=supervise.spawn_supervisor) -> list[dict]:
    """One incremental pass over the whole workspace. Returns the actions
    taken. Never raises for a Work-side failure; the cursor simply does not
    advance."""
    actions: list[dict] = []
    con = db.connect()
    try:
        ok = _report(con, client, actions)
        verify.after_report(con, cfg, client, actions)
        _progress(con, cfg, client, actions)
        fetched: list = []
        cursors: dict[str, str | None] = {}
        # A held item need never change again, so an incremental fetch would
        # not show it to us a second time and it would wait forever. While
        # anything waits — or dispatch is paused, which makes waiters — the
        # pass reads the whole board. That read is also what supplies
        # ready-lane order, since Work serves the lane in board order.
        unresolved_claim = con.execute(
            f"SELECT 1 FROM runs WHERE work_claim_status='pending' "
            f"AND status NOT IN {db.TERMINAL_SQL} LIMIT 1").fetchone()
        full = (dispatch.paused(con) or bool(dispatch.waiting_ids(con))
                or unresolved_claim is not None)
        for kind, lister in (("task", client.tasks), ("issue", client.issues)):
            since = db.meta_get(con, CURSOR_KEYS[kind])
            got = lister(updated_since=None if full else since)
            if got is None:
                ok = False
                continue
            fetched += [(kind, item) for item in got]
            stamps = [i.get("updatedAt") or "" for i in got]
            cursors[kind] = max([since or ""] + stamps) or None
        ok &= _claim(con, cfg, client, fetched, launcher, actions)
        ok &= _ferry(con, client, fetched, actions)
        if ok:
            for kind, value in cursors.items():
                if value:
                    db.meta_set(con, CURSOR_KEYS[kind], value)
            con.commit()
    finally:
        con.close()
    return actions


def watch(cfg: dict, client: WorkClient, interval: int) -> None:
    """Loop forever. The interval is a fallback heartbeat, not the primary
    signal — phase 3's event hooks (D3) will wake the sweeper directly.
    `orchestra daemon` runs this pass on its own schedule; this stays for a
    foreground sweep without the daemon."""
    while True:
        for action in sweep(cfg, client):
            print(action)
        time.sleep(max(1, interval))
