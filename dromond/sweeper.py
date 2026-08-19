"""The intervention loop (CONTRACT §4): Dromond's front door to Work.

Deterministic code only (DESIGN principle 6) — one pass:

- **Report**: a finished run posts an attributed comment and transitions its
  item (success → review/resolved; anything else → blocked/needs_human).
  Never done/closed — the human closes (CONTRACT §3 verb 2).
- **Claim**: an item a human marked ``delegated`` (CONTRACT v0.2 §2; a
  legacy ``agents`` list naming our identity still counts) that sits
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
import sqlite3
import time
from pathlib import Path

from dromond import (brief, config, db, dispatch, names, paths, project,
                         router, runners, supervise)
from dromond.work_client import WorkClient, WorkError, from_cfg

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
    conversation to continue. `killed` is not: a human or the observer STOPPED
    it, so its context is deliberately abandoned and its backend session may
    not exist at all (live run 27 resumed one, and the backend answered `no
    session matches` in under a second). A kill stops the ITEM, not just one
    process, so nothing behind it is resumed either — the item gets a fresh run.
    """
    for row in con.execute(
            f"SELECT * FROM runs WHERE work_item=? AND status IN {db.TERMINAL_SQL} "
            "ORDER BY id DESC", (item_id,)):
        if row["status"] == "killed":
            return None
        if row["session_ref"]:
            return row
    return None


def _insert_run(con, proj, profile_name: str, profile: dict, title: str,
                item_id: str, seen_ts: str | None) -> tuple[int, str]:
    """Fresh run row mapped to a Work item (same slug-mint loop as dispatch)."""
    for _ in range(names.MAX_ATTEMPTS + 4):
        slug = names.assign_slug(con)
        try:
            cur = con.execute(
                "INSERT INTO runs(slug, profile, backend, model, title, "
                "requested_by, workdir, project_id, status, started_at, work_item, "
                "work_seen_ts) VALUES(?,?,?,?,?,?,?,?, 'spawning', ?,?,?)",
                (slug, profile_name, profile["backend"], profile.get("model"),
                 title, "work", str(proj.path), proj.project_id, db.now(),
                 item_id, seen_ts))
            return int(cur.lastrowid), slug
        except sqlite3.IntegrityError as exc:
            if not names.is_unique_violation(exc):
                raise
            names.reset_memory_cache()
    raise RuntimeError("dromond: could not mint a unique run slug")


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
    return run["status"] == "done", summary or f"run {run['id']} {run['status']}"


def _headline(run, summary: str) -> str:
    """One board-readable line: what happened and why."""
    first = (summary or "").strip().splitlines()[0] if summary else ""
    reason = f": {first}" if first and not first.startswith(f"run {run['id']}") else ""
    return f"run {run['id']} {run['status']}{reason}"[:300]


def _decline_unaccounted(client: WorkClient, item_id: str, run, tag: str) -> int:
    """Answer, on the run's behalf, every criterion the run left open.

    Work refuses a move to review or blocked while any requirement or
    acceptance criterion is neither ticked nor declined, so that nothing is
    abandoned silently. The worker owns that answer and is told to give it. A
    run that dies, is killed, or simply ignores the protocol gives none — and
    the item would then be stuck outside every state it could move to.

    So the sweeper answers the only way it honestly can: it declines what is
    left, saying who did not account for it. It never ticks anything; a tick
    is a claim that work was verified, and the sweeper verified nothing.
    """
    task = client.task(item_id)
    if task is None:
        return 0
    reason = f"not accounted for by run {run['id']} ({run['status']})"[:2000]
    declined = 0
    for section, key in (("requirements", "requirements"),
                         ("acceptance", "acceptanceCriteria")):
        for index, entry in enumerate(task.get(key) or []):
            if entry.get("checked") or entry.get("declined"):
                continue
            if client.check_task_item(item_id, section, index,
                                      reason=reason) is not None:
                declined += 1
    if declined:
        print(f"dromond sweep: {tag} declined {declined} unaccounted "
              f"checklist item(s) on {item_id}")
    return declined


def _report(con, client: WorkClient, actions: list) -> bool:
    ok = True
    rows = list(con.execute(
        f"SELECT * FROM runs WHERE work_item IS NOT NULL AND status IN {db.TERMINAL_SQL} "
        "AND work_reported_at IS NULL ORDER BY id"))
    for run in rows:
        item_id, kind = run["work_item"], item_kind(run["work_item"])
        success, summary = _completion_outcome(run)
        tag = f"[{client.identity}/{run['slug'] or run['id']}]"
        comment = (f"{tag} run {run['id']} finished: {run['status']}\n\n"
                   f"{summary}")[:19000]
        try:
            posted = (client.log_task(item_id, comment) if kind == "task"
                      else client.reply_issue(item_id, comment))
            if posted is None:
                ok = False
                continue
            if kind == "task":
                target = "review" if success else "blocked"
                try:
                    # The transition note is the line the human reads on the
                    # board, so it carries the reason, not just a run id.
                    moved = client.move_task(item_id, target,
                                             note=f"{tag} {_headline(run, summary)}")
                except WorkError as exc:
                    if exc.code != "review_checklist_incomplete":
                        raise
                    if target == "review":
                        # The run said it succeeded but left criteria open, so
                        # its own claim is unverified. That is the human's to
                        # judge: park it rather than declining on its behalf.
                        target = "blocked"
                        note = f"{tag} could not enter review: {exc.code}"
                    else:
                        note = f"{tag} {_headline(run, summary)}"
                    _decline_unaccounted(client, item_id, run, tag)
                    moved = client.move_task(item_id, "blocked", note=note)
            elif success:
                # Work collapsed "resolved" into "closed" (2026-08-14): a run
                # closes its claimed issue with a summary; humans can reopen.
                target = "closed"
                moved = client.set_issue_state(item_id, "closed",
                                               resolution_summary=summary)
            else:
                target = "needs_human"
                moved = client.set_issue_state(item_id, "needs_human",
                                               reason=f"{tag} {summary}"[:19000])
            if moved is None:  # Work went away mid-pass; retry next pass
                ok = False
                continue
        except WorkError as exc:
            print(f"dromond sweep: report {item_id} rejected: {exc}")
            ok = False
            continue
        con.execute("UPDATE runs SET work_reported_at=? WHERE id=?",
                    (db.now(), run["id"]))
        con.commit()
        actions.append({"action": "report", "item": item_id,
                        "run": run["id"], "to": target})
    return ok


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


def _claim(con, cfg: dict, client: WorkClient, items: list,
           launcher, actions: list) -> bool:
    """Dispatch every claimable item. No cap of any kind gates this loop
    (DESIGN §4): all of it goes at once. Order and the waiting queue exist
    for the items that *cannot* go — blocked by a dependency, or held behind
    the pause switch."""
    ok = True
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
            print(f"dromond sweep: {item_id} has no known project "
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
            print(f"dromond sweep: {item_id} not staffed: {exc}")
            ok = False
            continue
        routed = None
        try:
            # Transition first: a claim the server refuses must not spawn.
            if kind == "issue":
                if client.claim_issue(item_id) is None:
                    ok = False
                    continue
            else:
                if client.move_task(
                        item_id, "in_progress",
                        note=f"[{client.identity}] claimed") is None:
                    ok = False
                    continue
            prior = _last_session_run(con, item_id)
            if prior:
                news = _new_human_comments(item, kind, prior["work_seen_ts"],
                                           client.identity)
                text = _comments_text(news, item_id) or \
                    f"Work {kind} {item_id} was handed back; continue the mission."
                run_id = supervise.create_followup(con, root, dict(prior), "work",
                                                   text, title=prior["title"])
                slug = None
            else:
                # SEAM (W-0183): the staffing turn. A fresh dispatch is the
                # one moment a profile is CHOSEN — a continuation above keeps
                # the preset its lineage launched with, which is the same
                # reason nothing revalidates a run in flight (W-0187).
                profile_name, profile, routed = staff(
                    con, pcfg, render_snapshot(item, kind), profile_name, profile)
                # The run TITLE is the item's own title. The id lives on the
                # row (`work_item`) and at the head of the brief, so repeating
                # "Work issue issue_abc123:" here only eats the width the
                # board has for saying what the run is about.
                run_id, slug = _insert_run(con, proj, profile_name, profile,
                                           (item.get("title") or item_id)[:80],
                                           item_id, None)
                run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
                # Swept runs are isolated by default: nobody watches a swept
                # dispatch, and a shared checkout means an unattended agent
                # and a working human edit the same files. A project that
                # cannot host a worktree still runs -- Orchestra lost real
                # delegations to a hard failure here.
                isolate = bool(work_cfg(pcfg).get("worktree", True))
                try:
                    supervise.prepare_launch(
                        con, root, pcfg, run, mission=_mission(item, kind),
                        use_worktree=isolate,
                        work_snapshot=render_snapshot(item, kind))
                # worktree.create exits for CLI use, so SystemExit counts too.
                except (Exception, SystemExit) as exc:
                    if not isolate:
                        raise
                    print(f"dromond: shared checkout for run {run_id} ({exc})")
                    supervise.prepare_launch(
                        con, root, pcfg, run, mission=_mission(item, kind),
                        use_worktree=False,
                        work_snapshot=render_snapshot(item, kind))
            con.execute("UPDATE runs SET work_item=?, work_seen_ts=?, "
                        "routed_reason=? WHERE id=?",
                        (item_id, item.get("updatedAt") or db.now(), routed, run_id))
            con.commit()
            tag = f"[{client.identity}/{slug or run_id}]"
            note = f"{tag} dispatched run {run_id}" + (" (session resumed)" if prior else "")
            # The board says WHY a heavy model was chosen, not just that one
            # was. Absent when routing is off — there is no decision to report.
            if routed:
                note += f"\nstaffing: {routed}"
            if kind == "task":
                client.log_task(item_id, note)
            else:
                client.reply_issue(item_id, note)
            launcher(root, run_id)
            actions.append({"action": "dispatch", "item": item_id, "run": run_id,
                            "resumed": bool(prior)})
        except WorkError as exc:
            print(f"dromond sweep: claim {item_id} rejected: {exc}")
            ok = False
    return ok


def _ferry(con, client: WorkClient, items: list, actions: list) -> bool:
    for kind, item in items:
        run = _live_run(con, item["id"])
        if not run or run["work_seen_ts"] is None:
            continue
        news = _new_human_comments(item, kind, run["work_seen_ts"], client.identity)
        if not news:
            continue
        if not run["session_ref"]:
            continue  # not resumable yet; the next pass retries
        try:
            offset = Path(run["log_path"]).stat().st_size
        except (OSError, TypeError):
            offset = 0
        con.execute(
            "INSERT INTO messages(run_id, sender, body, kind, created_at, "
            "delivery_offset) VALUES(?,?,?, 'interrupt', ?, ?)",
            (run["id"], f"work:{item['id']}", _comments_text(news, item["id"]),
             db.now(), offset))
        con.execute("UPDATE runs SET work_seen_ts=? WHERE id=?",
                    (max(n["at"] for n in news), run["id"]))
        con.commit()
        actions.append({"action": "ferry", "item": item["id"], "run": run["id"],
                        "comments": len(news)})
    return True


def _iso_ago(seconds: int) -> str:
    return datetime.datetime.fromtimestamp(
        time.time() - seconds, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _progress(con, cfg: dict, client: WorkClient, actions: list) -> None:
    """Post a heartbeat for live runs so the board is not silent mid-run.

    Derived from the worker's own log (runners.parse_progress), so it costs
    a file read rather than a model turn -- the worker is never asked to
    report. Rate-limited by [work] progress_interval; 0 disables.
    """
    interval = int(work_cfg(cfg).get("progress_interval", 900) or 0)
    if interval <= 0:
        return
    cutoff = _iso_ago(interval)
    rows = con.execute(
        f"SELECT * FROM runs WHERE work_item IS NOT NULL AND status NOT IN {db.TERMINAL_SQL} "
        "AND (work_progress_at IS NULL OR work_progress_at < ?)", (cutoff,))
    for run in list(rows):
        note = runners.parse_progress(run["log_path"])
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
        _progress(con, cfg, client, actions)
        fetched: list = []
        cursors: dict[str, str | None] = {}
        # A held item need never change again, so an incremental fetch would
        # not show it to us a second time and it would wait forever. While
        # anything waits — or dispatch is paused, which makes waiters — the
        # pass reads the whole board. That read is also what supplies
        # ready-lane order, since Work serves the lane in board order.
        full = dispatch.paused(con) or bool(dispatch.waiting_ids(con))
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
    `dromond daemon` runs this pass on its own schedule; this stays for a
    foreground sweep without the daemon."""
    while True:
        for action in sweep(cfg, client):
            print(action)
        time.sleep(max(1, interval))
