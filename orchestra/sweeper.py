"""The intervention loop (CONTRACT §4): Orchestra's front door to Work.

Deterministic code only (DESIGN principle 6) — one pass:

- **Report**: a finished run posts an attributed comment and appends its
  fact — ``landed`` / ``halted`` / ``failed``, or ``resolved`` /
  ``needs_human`` on an issue. A run never writes status (CONTRACT 0.8);
  Work derives the board from the facts and the human's own move. The comment
  carries the landing detail and any correction the run never received; the
  ``landed`` fact carries the sha and the revert line, read off the receipt
  ``merge.py`` stamped (CONTRACT §7 Enforcement: the landing path posts
  nothing).
- **Mirror**: both sides of an ``ask`` reach the item's thread. ``messaging``
  records them on the run and posts nothing; this pass carries them.
- **Escalate**: a profile change an agent may not make itself becomes a Work
  decision. ``profile_edit`` files the escalation and posts nothing; this
  pass reads the record and files the decision (DESIGN §5).
- **Claim**: an item a human marked ``delegated`` (CONTRACT §2; a legacy
  ``agents`` list is history and never counts as delegation) that sits
  in ``ready`` (task) / ``queued`` (issue) with no live run gets one: a
  fresh dispatch, or a session continuation when a prior run for the item
  left a resumable session (the "answer from the phone, the run picks it
  up" path of §4 step 5).
- **Refine**: an item carrying the `refine` tag gets a shaping run (W-0309,
  ``refine.py``) — any status, delegated or not, because refinement comes
  before execution. It claims nothing and lands nothing.
- **Ferry**: a new human comment on an in-flight item is delivered to the
  owning run as a safe-boundary interrupt (the phase-1 delivery path).
- **Cursor**: ``updatedSince`` watermarks live in the meta table and only
  advance after a fully successful pass.
"""
import datetime
import time
from pathlib import Path
from typing import NamedTuple

from orchestra import (brief, config, db, dispatch, merge, messaging, nod,
                         paths, project, refine, router, supervise, traces,
                         verify, work_client)
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


# --- work_marks: this adapter's bookkeeping (schema v21) --------------------
# CONTRACT §7 Enforcement 1: the run row carries an opaque ``ref`` and nothing
# else about Work. How far a thread has been read, whether its writeback
# landed, and when the last heartbeat posted are facts about WORK'S BOARD, so
# they live in this adapter's own table. Every write goes through here.

def mark(con, run_id: int, column: str, value: str, *,
         once: bool = False, commit: bool = True) -> None:
    """Stamp one mark for a run. ``once=True`` keeps the first value — a
    report is never posted twice, so its receipt is never overwritten."""
    keep = (f"COALESCE(work_marks.{column}, excluded.{column})" if once
            else f"excluded.{column}")
    con.execute(f"INSERT INTO work_marks(run_id, {column}) VALUES(?,?) "
                f"ON CONFLICT(run_id) DO UPDATE SET {column}={keep}",
                (run_id, value))
    if commit:
        con.commit()


# --- the adapter's label-to-folder map (CONTRACT §7, schema v29) ------------
# The core ``projects`` table is IDENTITY only; the ``checkouts`` table is
# THIS adapter's, like ``work_marks``: the source's cached project list plus
# the owner's ``link`` bindings. Only this module knows Work's entry shape —
# the workspace root a relative path resolves against, ``projectId``, ``id``,
# ``aliasPaths``, and the ``archived`` flag Work owns (DESIGN §1). Identity
# rows are written through the one core seam, ``project.remember_identity``.

class Sited(NamedTuple):
    """A project identity WITH the folder its unattended runs work in.

    The core registry stores no paths; an unattended dispatch is this
    adapter resolving its own labels, so the pairing lives here.
    """
    project_id: str
    slug: str
    name: str | None
    archived: bool
    path: Path


def remember_projects(con, workspace_root: str, entries: list) -> int:
    """Cache Work's project list: identities through the core seam, paths
    into this adapter's own ``checkouts`` table. Returns identities written.

    Relative paths resolve against the workspace root; each aliasPath gets
    its own row so it resolves too. Every refresh copies ``archived``, so
    archiving in Work parks the project here and unarchiving brings it back
    with no local action (the 2026-08-27 bill CONTRACT 0.10 names was a
    cached copy of this list that never learned). A cached path the source
    no longer names is pruned; ``link`` rows (``source_ref`` NULL) are the
    owner's and never a source's to delete.
    """
    root = Path(workspace_root).expanduser()
    idents: dict[str, tuple] = {}
    rows, seen, ts = [], [], db.now()
    for entry in entries:
        project_id = entry.get("projectId")
        if not project_id:
            continue  # a project Work has not stamped yet is not addressable
        idents.setdefault(project_id, (entry.get("name"),
                                       entry.get("archived") is True))
        for rel in [entry.get("path"), *(entry.get("aliasPaths") or [])]:
            if not rel or not entry.get("id"):
                continue
            p = Path(rel).expanduser()
            rows.append((str(p if p.is_absolute() else root / p),
                         project_id, entry.get("id")))
    count = project.remember_identity(
        con, [(pid, name, archived) for pid, (name, archived) in idents.items()])
    for path, project_id, ref in rows:
        con.execute(
            "INSERT INTO checkouts(path, project_id, source_ref, refreshed_at) "
            "VALUES(?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
            "project_id=excluded.project_id, source_ref=excluded.source_ref, "
            "refreshed_at=excluded.refreshed_at",
            (path, project_id, ref, ts))
        seen.append(path)
    marks = ",".join("?" * len(seen)) or "''"
    con.execute("DELETE FROM checkouts WHERE source_ref IS NOT NULL "
                f"AND path NOT IN ({marks})", seen)
    con.commit()
    return count


def by_source_ref(con, ref: str | None) -> Sited | None:
    """Resolve the source's own project reference to a working folder.

    The ref is matched as itself first and as an absolute local path second,
    because Work is free to use either. The folder, in order (W-0312):
    the cached path when it IS a directory; a ``link`` binding, which wins
    EVEN WHEN ITS DIRECTORY IS GONE (a claimed checkout on an unmounted
    volume must fail where a human sees it, never be replaced by an empty
    workspace an agent would fill); the discovered folder the grouped ref
    names without its grouping; else the project's ephemeral workspace.
    """
    if not ref:
        return None
    row = con.execute(
        "SELECT * FROM checkouts WHERE source_ref=? "
        "ORDER BY LENGTH(path) LIMIT 1", (ref,)).fetchone()
    if row is None:
        row = con.execute("SELECT * FROM checkouts WHERE path=?",
                          (str(Path(ref).expanduser()),)).fetchone()
    if row is None:
        return None
    proj = project.by_id(con, row["project_id"])
    if proj is None:
        return None
    return _site(con, proj, Path(row["path"]), row["source_ref"])


def _site(con, proj, cached: Path, ref: str | None) -> Sited:
    place = Sited(proj.project_id, proj.slug, proj.name, proj.archived, cached)
    if cached.is_dir():
        return place
    linked = con.execute(
        "SELECT path FROM checkouts WHERE project_id=? AND source_ref IS NULL",
        (proj.project_id,)).fetchone()
    if linked is not None:
        return place._replace(path=Path(linked["path"]))
    found = _discover(con, proj.project_id, cached, ref)
    if found is not None:
        return place._replace(path=found)
    # Pre-v27 workspaces were keyed by project id under ~/.orchestra/
    # workspaces; one that exists keeps its state where it already lives.
    legacy = paths.home().expanduser() / "workspaces" \
        / paths.slugify(proj.project_id)
    return place._replace(
        path=legacy if legacy.is_dir() else paths.workspace_dir(proj.slug))


def _discover(con, project_id: str, cached: Path, ref: str | None) -> Path | None:
    """The folder the grouped ref names WITHOUT its grouping: the workspace
    root plus the ref's LAST segment ("Group/orchestra" finds
    ~/Projects/orchestra by itself). A folder another project's checkout
    already claims is declined — two projects named "docs" under different
    groups must not collapse onto one checkout."""
    rel = Path(ref or "")
    depth = len(rel.parts)
    if depth < 2 or depth >= len(cached.parts):
        return None  # nothing was grouped, so there is nothing to strip
    candidate = cached.parents[depth - 1] / rel.parts[-1]
    if not candidate.is_dir():
        return None
    taken = con.execute(
        "SELECT 1 FROM checkouts WHERE path=? AND project_id<>?",
        (str(candidate), project_id)).fetchone()
    return None if taken else candidate


def link(con, selector: str, root: Path):
    """Bind a project to the checkout it actually lives in: the escape hatch
    for a repository whose location its source label does not give away.
    The binding is this adapter's row (``source_ref`` NULL), so a refresh
    neither replaces nor prunes it."""
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"orchestra: {root} is not a directory")
    row = con.execute("SELECT project_id FROM checkouts WHERE source_ref=? "
                      "LIMIT 1", (selector,)).fetchone()
    proj = (project.by_id(con, row["project_id"]) if row
            else project.find(con, selector))
    if proj is None:
        raise SystemExit(f"orchestra: no project matches {selector!r} — "
                         "`orchestra project list` names them")
    con.execute(
        "INSERT OR REPLACE INTO checkouts(path, project_id, source_ref, "
        "refreshed_at) VALUES(?,?,NULL,?)",
        (str(root), proj.project_id, db.now()))
    con.commit()
    return proj


def locate(con, proj) -> Path | None:
    """The folder this adapter's map names for a project, or None when it
    knows none. The CLI's fallback when a project has no run history yet."""
    row = con.execute(
        "SELECT * FROM checkouts WHERE project_id=? "
        "ORDER BY source_ref IS NULL, LENGTH(path) LIMIT 1",
        (proj.project_id,)).fetchone()
    if row is None:
        return None
    return _site(con, proj, Path(row["path"]), row["source_ref"]).path


def project_for_dir(con, start: Path):
    """The project whose cached checkout contains this directory — the
    adapter's half of cwd resolution, for a folder no run has taught the
    run history yet. Deepest path wins, like the history's own rule."""
    best, best_len = None, -1
    for row in con.execute("SELECT path, project_id FROM checkouts"):
        p = Path(row["path"])
        if (start == p or p in start.parents) and len(row["path"]) > best_len:
            best, best_len = row["project_id"], len(row["path"])
    return project.by_id(con, best)


def refresh_projects(con, cfg: dict) -> int:
    """Re-read the project list from Work. Returns rows cached (0 when Work
    is off or unreachable — an offline miss must not crash the CLI)."""
    client = from_cfg(cfg)
    if client is None:
        return 0
    root = client.workspace_root()
    entries = client.projects()
    if root is None or entries is None:
        return 0
    return remember_projects(con, root, entries)


def seen_ts(con, run_id: int) -> str | None:
    """How far this run had read its ref's thread."""
    row = con.execute("SELECT seen_ts FROM work_marks WHERE run_id=?",
                      (run_id,)).fetchone()
    return row["seen_ts"] if row else None


def thread_seen_ts(con, ref: str) -> str | None:
    """How far ANY run has read this ref's thread.

    A watermark belongs to the THREAD, not to one process against it: a retry
    or a continuation carries on the same conversation under a new run id.
    Reading the newest mark on the ref is why a fresh run needs nothing copied
    into it at creation — which is what let ``supervise.create_run`` and
    ``messaging.queue_tell`` drop their Work-shaped parameters (CONTRACT §7).
    """
    row = con.execute(
        "SELECT MAX(m.seen_ts) AS ts FROM work_marks m "
        "JOIN runs r ON r.id = m.run_id WHERE r.ref = ?", (ref,)).fetchone()
    return row["ts"] if row else None


def status_rows(con) -> list:
    """``orchestra work status``: every run carrying a ref, with the writeback
    receipt. The join lives HERE because ``work_marks`` is this adapter's
    table and the CLI is not allowed to know it (CONTRACT §7)."""
    return list(con.execute(
        "SELECT r.ref, r.id, r.slug, r.status, m.reported_at FROM runs r "
        "LEFT JOIN work_marks m ON m.run_id = r.id "
        "WHERE r.ref IS NOT NULL ORDER BY r.id"))


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

# Only a run carrying a Work TASK gets this — issues have no checklist, and a
# brief never teaches a verb it cannot use. Lives in the adapter because the
# `work check` verbs are Work's own CLI (CONTRACT §7); the core injects it
# verbatim as the snapshot's protocol.
CHECKLIST_PROTOCOL = """\
Before you stop, account for every requirement and acceptance criterion above.
Tick each one you verified: `work check {item} requirement|acceptance <index> --root {root}`
(indexes count from 0, as `work show {item} --root {root}` lists them). Decline
each one you did not: `work check {item} requirement|acceptance <index> --root {root} --decline "not attempted, blocked on X"`.
Declining is expected and is not a failure — leaving an item unanswered is.
Whatever you leave unanswered is declined for you, naming your run as the one
that did not account for it.
"""


def checklist_protocol(kind: str, item_id: str, root) -> str | None:
    """The checklist card for a task, None for anything else."""
    if kind != "task":
        return None
    return CHECKLIST_PROTOCOL.format(item=item_id, root=root)


def render_snapshot(item: dict, kind: str) -> str:
    """Compact Work-item snapshot for the brief; frozen at dispatch, capped
    at brief.SNAPSHOT_MAX_CHARS (D6)."""
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
    return "\n".join(lines)[:brief.SNAPSHOT_MAX_CHARS]


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
        f"SELECT * FROM runs WHERE ref=? AND status NOT IN {db.TERMINAL_SQL} "
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
            f"SELECT * FROM runs WHERE ref=? AND status IN {db.TERMINAL_SQL} "
            "ORDER BY id DESC", (item_id,)):
        if row["status"] in ("killed", "halted"):
            return None
        if row["session_ref"]:
            return row
    return None


def _insert_run(con, proj, profile_name: str, profile: dict, title: str,
                item_id: str, seen: str | None,
                routed_reason: str | None = None, *, commit: bool = True):
    """Reserve one fresh Work run through the common admission boundary."""
    run, blocked = supervise.create_run(
        con, profile=profile_name, backend=profile["backend"],
        model=profile.get("model"), title=title, requested_by="work",
        workdir=str(proj.path), project_id=proj.project_id,
        ref=item_id, routed_reason=routed_reason, commit=commit)
    if run is not None and seen is not None:
        # The seed watermark rides the caller's commit policy: under
        # ``commit=False`` create_run's transaction is still open, so the mark
        # is admitted with the row it belongs to.
        mark(con, int(run["id"]), "seen_ts", seen, commit=commit)
    return run, blocked


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


# --- claim order (Work's item schema; moved out of dispatch.py, which is
# core and must not know a source's edges or status vocabulary — CONTRACT §7)

# A prerequisite in any of these is settled; anything else still blocks.
DONE_STATUSES = frozenset({"done", "closed", "cancelled", "resolved"})


def prerequisites(item: dict) -> list[str]:
    """Work item ids this item must wait for. ``dependsOn`` is the one edge
    Work serves; the legacy reverse-edge record folds into it on Work's read
    (Work, lib/local-workspace.mjs), so the runner never reads a
    second key. Tolerant of both a bare id list and a list of
    ``{"id": ...}``."""
    found: list[str] = []
    for dep in item.get("dependsOn") or []:
        if isinstance(dep, dict):
            dep = dep.get("id")
        if dep and dep != item.get("id") and dep not in found:
            found.append(dep)
    return found


def statuses(items) -> dict[str, str | None]:
    """Status by id for everything this pass fetched (tasks carry ``status``,
    issues carry ``state``)."""
    return {item["id"]: (item.get("status") or item.get("state"))
            for _, item in items}


def unmet(item: dict, known: dict, lookup=None) -> list[str]:
    """Prerequisites that are not finished yet.

    ``known`` is filled in as it goes, so one lookup serves every dependent.
    ponytail: a prerequisite id Work does not know (deleted, typo) reads as
    unfinished and holds the item forever — visibly, in the waiting queue
    with its reason. Upgrade path is a distinct 'unknown dependency' reason
    once Work can tell 404 from unfinished.
    """
    blocked = []
    for dep in prerequisites(item):
        if dep not in known and lookup is not None:
            fetched = lookup(dep) or {}
            known[dep] = fetched.get("status") or fetched.get("state")
        if known.get(dep) not in DONE_STATUSES:
            blocked.append(dep)
    return blocked


def plan(con, candidates, known: dict, lookup=None) -> list[tuple]:
    """Order claimable items and say which of them must wait.

    ``candidates`` is ``[(kind, item, lane_index)]`` in the order Work served
    them. Returns ``[(kind, item, lane_index, reason)]`` where ``reason`` is
    None for anything that starts now, else ``(reason, detail)``.

    Sort key is dependencies first (nothing blocked outranks something
    ready), then ready-lane board order, then FIFO by how long the item has
    already waited. Order only ever matters for the items that must wait —
    everything else dispatches as soon as it is claimed, all of it at once,
    with no cap and no artificial delay.
    """
    pause = dispatch.pause_state(con)
    seq = dispatch.queue_seq(con)
    planned = []
    for kind, item, lane in candidates:
        blocked = unmet(item, known, lookup)
        if blocked:
            reason = ("dependency", ", ".join(blocked))
        elif pause is not None:
            reason = ("paused", pause.get("note"))
        else:
            reason = None
        planned.append((kind, item, lane, reason))
    planned.sort(key=lambda e: (e[3] is not None,
                                e[2] if e[2] is not None else dispatch.NO_LANE,
                                seq.get(e[1]["id"], dispatch.NO_LANE)))
    return planned


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
        summary or f"{db.run_no(run)} {run['status']}"


def _headline(run, summary: str) -> str:
    """One board-readable line: what happened and why."""
    first = (summary or "").strip().splitlines()[0] if summary else ""
    reason = f": {first}" if first and not first.startswith(db.run_no(run)) else ""
    outcome = ("landing failed" if run["status"] == "done"
               and run["landing_status"] == "failed" else run["status"])
    return f"{db.run_no(run)} {outcome}{reason}"[:300]


def _decline_unaccounted(client: WorkClient, item_id: str, run,
                         tag: str) -> int | None:
    """Answer, on the run's behalf, every criterion it left open, and say so.
    Runs before the terminal fact: Work refuses a landing with anything
    unaccounted."""
    declined = client.decline_unaccounted(
        item_id, f"not accounted for by {db.run_no(run)} ({run['status']})")
    if declined:
        print(f"orchestra sweep: {tag} declined {declined} unaccounted "
              f"checklist item(s) on {item_id}")
    return declined


def _landing_detail(con, run) -> list[str]:
    """The landing report ``merge.py`` wrote into the run's own thread.

    DESIGN §9 names its contents — merge commit, files changed, check results,
    the revert command — and the landing path is the only writer that has
    them. It stopped posting them itself (CONTRACT §7 Enforcement), so the
    report comment carries them, once, beside the run's summary.
    """
    return [row["body"] for row in con.execute(
        "SELECT body FROM messages WHERE run_id=? AND kind='merge' ORDER BY id",
        (int(run["id"]),))]


def _stranded_detail(con, run) -> list[str]:
    """Corrections the run ended before receiving.

    They live in ``messages`` and never on the run row, so a cursored read of
    run rows cannot surface them on its own. The report comment carries them
    instead of a second channel: it posts exactly once per run, gated by the
    same ``work_marks.reported_at`` receipt as everything else in it, so the
    notice inherits that delivery guarantee rather than inventing one.
    """
    rows = messaging.undeliverable(con, int(run["id"]))
    if not rows:
        return []
    lines = [f"{len(rows)} message(s) never reached the run:"]
    lines += [f"- from {row['sender']}: "
              f"{' '.join((row['body'] or '').split())[:200]} "
              f"({row['undeliverable_reason']})" for row in rows[:5]]
    if len(rows) > 5:
        lines.append(f"- {len(rows) - 5} more: `orchestra show {run['id']}`")
    return ["\n".join(lines)]


def _landed_fields(con, run) -> dict:
    """The sha a ``landed`` fact names, and the line that reverts it.

    ``runs.landing_commit`` is the receipt (schema v23). It is NULL when the
    base already contained the branch: then no merge commit exists, and a
    revert line aimed at one would undo somebody else's work (I-0077).
    """
    sha = run["landing_commit"]
    if not sha:
        return {}
    return {"sha": sha,
            "revert": merge.revert_command(project.root_for(con, run), sha)}


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
        "SELECT 1 FROM runs WHERE ref=? AND layer IS NULL AND id>? LIMIT 1",
        (result["ref"], result["id"])).fetchone()
    if newer is not None:
        return False
    retry = con.execute(
        "SELECT action FROM observations WHERE run_id=? AND layer='retry' "
        "ORDER BY id DESC LIMIT 1", (result["id"],)).fetchone()
    if retry is not None and retry["action"] == "deferred":
        return False
    if result["status"] == "done" and result["branch"] \
            and result["landing_status"] not in ("ok", "failed", "skipped"):
        return False
    return True


def report_result(con, client: WorkClient, result) -> tuple[bool, dict | None]:
    """Report one terminal run result.

    ``(False, None)`` asks the batch to retry after Work is available;
    ``(True, None)`` means no append is due; ``(True, action)`` is an appended
    report.
    """
    if not result["ref"]:
        return True, None
    if result["requested_by"] == refine.REQUESTED_BY:
        # The refine lane reports itself, from inside the run: the sections,
        # a `fact: refined` line, and the tag drop. It has no branch to land
        # and no checklist to answer, so the worker report path — which would
        # decline every open criterion and append `fact: landed` — must never
        # run for one.
        _mark_reported(con, result)
        return True, None
    if not _report_ready(con, result):
        return True, None
    item_id, kind = result["ref"], item_kind(result["ref"])
    success, summary = _completion_outcome(result)
    tag = f"[{client.identity}/{result['slug'] or result['id']}]"
    comment = "\n\n".join(
        [f"{tag} run {result['id']} finished: {result['status']}", summary]
        + _landing_detail(con, result) + _stranded_detail(con, result))[:19000]
    if result["status"] == "done" and result["branch"] \
            and result["landing_status"] == "skipped":
        # Landing is off ([merge] enabled = false): the branch is the
        # deliverable and the lifecycle fact belongs to whatever lands it —
        # an external lander posts its own `fact: landed`. The result
        # comment still reaches the thread, once, and the criteria stay
        # unanswered for that later landing to account for.
        try:
            remote = (client.task(item_id) if kind == "task"
                      else client.issue(item_id))
            if remote is None:
                return False, None
            entries = (remote.get("log") if kind == "task"
                       else remote.get("messages")) or []
            sent = {entry.get("message" if kind == "task" else "body")
                    for entry in entries}
            if comment not in sent:
                posted = (client.log_task(item_id, comment) if kind == "task"
                          else client.reply_issue(item_id, comment))
                if posted is None:
                    return False, None
        except WorkError as exc:
            if work_client.retryable(exc):
                print(f"orchestra sweep: report {item_id} temporarily "
                      f"refused ({exc})")
                return False, None
            print(f"orchestra sweep: report {item_id} refused ({exc}) — "
                  f"marked reported")
        _mark_reported(con, result)
        return True, {"action": "report", "item": item_id,
                      "run": result["id"], "to": "kept"}
    target = ("review" if success else "blocked") if kind == "task" \
        else ("closed" if success else "needs_human")
    verb = ("landed" if success else
            "halted" if result["status"] == "halted"
            or result["landing_status"] == "failed" else "failed") \
        if kind == "task" else ("resolved" if success else "needs_human")
    fact = (fact_line(
        tag, verb,
        **(_landed_fields(con, result) if verb == "landed" else {}),
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
        f"SELECT * FROM runs WHERE ref IS NOT NULL AND status IN {db.TERMINAL_SQL} "
        "AND id NOT IN (SELECT run_id FROM work_marks "
        "               WHERE reported_at IS NOT NULL) ORDER BY id"))
    for result in rows:
        settled, action = report_result(con, client, result)
        ok &= settled
        if action is not None:
            actions.append(action)
    return ok


def _mark_reported(con, run) -> None:
    """Once only, however it ended (DESIGN: a report is never posted twice)."""
    mark(con, int(run["id"]), "reported_at", db.now(), once=True)


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
            "UPDATE runs SET status='pending', claim_status='pending' "
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
        "AND claim_status='pending' AND status IN ('pending','spawning')",
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
            "claim_status='abandoned', "
            "summary=?, finished_at=COALESCE(finished_at, ?) WHERE id=?",
            (reason, db.now(), current["id"]))
        mark(con, int(current["id"]), "reported_at", db.now(), once=True)
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
                    item, kind, seen_ts(con, int(prior["id"])), client.identity)
                text = _comments_text(news, item["id"]) or \
                    f"Work {kind} {item['id']} was handed back; continue the mission."
                supervise.prepare_followup(con, root, prior, run, text)
            else:
                supervise.prepare_launch(
                    con, root, pcfg, run, mission=_mission(item, kind),
                    # An ephemeral workspace has no repository to branch
                    # from and never had one, so the run works in it directly
                    # (W-0312). A real checkout that is not a repository still
                    # fails closed — that guard stays exactly as it was.
                    use_worktree=bool(work_cfg(pcfg).get("worktree", True))
                    and not project.is_workspace(root),
                    snapshot=render_snapshot(item, kind),
                    snapshot_protocol=checklist_protocol(kind, item["id"],
                                                         root))
        mark(con, run_id, "seen_ts", item.get("updatedAt") or db.now())
    except (Exception, SystemExit) as exc:
        error = str(exc)[:1000] or exc.__class__.__name__
        if prior is None:
            supervise.fail_launch(con, root, run_id, error)
        con.execute("UPDATE runs SET claim_status='claimed' WHERE id=?",
                    (run_id,))
        con.commit()
        print(f"orchestra sweep: {item['id']} launch setup failed: {error}")
        actions.append({"action": "launch_failed", "item": item["id"],
                        "run": run_id, "reason": error})
        _report(con, client, actions)
        return "failed"

    tag = f"[{client.identity}/{run['slug'] or run_id}]"
    note = f"{tag} dispatched {db.run_no(run)}" + \
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
        con.execute("UPDATE runs SET claim_status='claimed' WHERE id=?",
                    (run_id,))
        con.commit()
        actions.append({"action": "launch_failed", "item": item["id"],
                        "run": run_id, "reason": error})
        _report(con, client, actions)
        return "failed"
    # A crash before this receipt may start another detached supervisor, but
    # supervise() admits exactly one of them before either can start a worker.
    con.execute("UPDATE runs SET claim_status='claimed' WHERE id=?",
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
        f"SELECT * FROM runs WHERE claim_status='pending' "
        f"AND status NOT IN {db.TERMINAL_SQL} ORDER BY id"))
    for run in pending:
        found = by_id.get(run["ref"])
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
    planned = plan(con, candidates, statuses(items),
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
        # Which checkout the run gets is the item's project, not the caller's
        # directory: one daemon sweeps the whole workspace (DESIGN §2).
        proj = by_source_ref(con, item.get("projectPath"))
        if proj is None and refresh_projects(con, cfg):
            proj = by_source_ref(con, item.get("projectPath"))
        if proj is not None and proj.archived:
            # DESIGN §1: the project is parked, so this lane leaves its items
            # alone. Held rather than printed — the queue row is what makes
            # the notice once-only, so a 60s sweep does not repeat it, and a
            # forgotten `delegated` tick is still said out loud once. Checked
            # BEFORE the release below, which would reset that row every pass.
            # The row also keeps this pass reading the WHOLE board, so
            # unarchiving revives the item with no human touching it.
            if dispatch.hold(con, item_id, kind, _lane, "archived", proj.slug):
                print(f"orchestra sweep: {item_id} skipped — project "
                      f"{proj.slug} is archived")
                actions.append({"action": "hold", "item": item_id,
                                "reason": "archived", "detail": proj.slug})
            continue
        if item_id in queued:
            dispatch.release(con, item_id)
        if proj is None:
            print(f"orchestra sweep: {item_id} has no known project "
                  f"({item.get('projectPath')!r}) — skipped")
            continue
        # A store-only project has no checkout of its own; by_source_ref
        # hands it an ephemeral workspace instead (W-0312).
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
                # row (`ref`) and at the head of the brief, so repeating
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
        if not run:
            continue
        seen = thread_seen_ts(con, item["id"])
        if seen is None:
            continue
        # A claim still pending confirmation never receives tells: nothing has
        # launched, and _finish_claim folds these same comments into the
        # mission it launches with — ferrying now would deliver them twice.
        if run["claim_status"] == "pending":
            continue
        news = _new_human_comments(item, kind, seen, client.identity)
        if not news:
            continue
        if not run["session_ref"]:
            continue  # not resumable yet; the next pass retries
        try:
            # One admission: the ferry cursor and the message it explains
            # commit together, so a crash can cause neither a duplicate nor a
            # lost Work comment. ``commit=False`` is what keeps that true
            # across the core/adapter line (CONTRACT §7).
            messaging.queue_tell(
                con, run["id"], f"work:{item['id']}",
                _comments_text(news, item["id"]), run["log_path"],
                commit=False)
            mark(con, int(run["id"]), "seen_ts",
                 max(n["at"] for n in news), commit=False)
            con.commit()
        except messaging.RunClosed:
            continue
        actions.append({"action": "ferry", "item": item["id"], "run": run["id"],
                        "comments": len(news)})
    return True


def _mark_answers_mirrored(con, run_id: int) -> None:
    """``nod.unmirrored`` lists answered cards whose decision has not reached
    the thread. This run's answer just did, so stop listing them."""
    for request in nod.unmirrored(con):
        if request["run_id"] == run_id:
            nod.mark_mirrored(con, request["request_id"])


def _escalations(con, client: WorkClient, actions: list) -> None:
    """Profile-change escalations reach Work as decisions (DESIGN §5).

    ``profile_edit`` refuses an agent-authority change that commits spend and
    files ONE escalation; config editing knows no record system (CONTRACT §7
    Enforcement). This pass reads the record it left and files the decision
    the human answers.

    ``mirrored_at`` is the watermark — the same column an answered card's
    decision uses, meaning the same thing: a record system has this. Nothing
    new is invented, and the row is read, never consumed. Best effort: Work
    being down leaves the row for the next pass, and the Nod card already
    reached the human either way.
    """
    for row in nod.unmirrored_of_kind(con, nod.PROFILE_CHANGE):
        try:
            created = client.create_decision(
                title=row["title"] or "Orchestra profile change",
                detail=row["body"] or "",
                options=["Apply it", "Decline"],
                # Work refuses an agent decision without a recommendationReason.
                recommendation_reason=("No lean: this change commits spend, "
                                       "which DESIGN §5 keeps a human call."))
        except WorkError as exc:
            print(f"orchestra sweep: profile decision refused ({exc})")
            return
        if created is None:
            return  # Work went away; the next pass carries it
        nod.mark_mirrored(con, row["request_id"])
        actions.append({"action": "decide", "escalation": row["request_id"],
                        "decision": created.get("id")})


def _mirror(con, client: WorkClient, actions: list) -> None:
    """Put both sides of an ``ask`` in the item's thread (CONTRACT §4).

    ``messaging`` records the question and the human's answer as ``ask`` and
    ``answer`` rows on the run and posts nothing, because the ask verb is
    core and the thread is Work's (CONTRACT §7 Enforcement). This pass reads
    those rows and appends what the thread does not already hold.

    THE THREAD IS THE WATERMARK. The same content check ``report_result``
    makes is what stops a second post, so neither side keeps delivery state
    and a crash between the post and the next pass costs nothing. The rows
    are never consumed or moved, so this is a read, not a queue.

    Best effort, exactly as the inline mirror was: Work being down must never
    break a live session, and the next pass carries what this one could not.
    The scan is bounded by work in flight — live runs, plus terminal runs
    whose report has not gone up — never by history.
    """
    rows = list(con.execute(
        "SELECT m.kind, m.body, r.ref, r.id AS run_id FROM messages m "
        "JOIN runs r ON r.id = m.run_id "
        "WHERE r.ref IS NOT NULL AND m.kind IN (?,?) "
        f"AND (r.status NOT IN {db.TERMINAL_SQL} OR r.id NOT IN "
        "(SELECT run_id FROM work_marks WHERE reported_at IS NOT NULL)) "
        "ORDER BY m.id", (messaging.ASK_KIND, messaging.ANSWER_KIND)))
    for item_id in dict.fromkeys(row["ref"] for row in rows):
        kind = item_kind(item_id)
        try:
            remote = (client.task(item_id) if kind == "task"
                      else client.issue(item_id))
            if remote is None:
                continue
            for row in (r for r in rows if r["ref"] == item_id):
                body = (f"Question for the human:\n\n{row['body']}"
                        if row["kind"] == messaging.ASK_KIND else row["body"])
                if _thread_contains(remote, kind, body):
                    continue
                posted = (client.log_task(item_id, body) if kind == "task"
                          else client.reply_issue(item_id, body))
                if posted is None:
                    break   # Work went away mid-item; the next pass retries
                if row["kind"] == messaging.ANSWER_KIND:
                    _mark_answers_mirrored(con, int(row["run_id"]))
                actions.append({"action": "mirror", "item": item_id,
                                "run": row["run_id"], "kind": row["kind"]})
        except WorkError as exc:
            print(f"orchestra sweep: mirror {item_id} refused ({exc})")


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
        f"SELECT * FROM runs WHERE ref IS NOT NULL AND status NOT IN {db.TERMINAL_SQL} "
        "AND id NOT IN (SELECT run_id FROM work_marks "
        "               WHERE progress_at >= ?)", (cutoff,))
    for run in list(rows):
        note = traces.progress(run["log_path"], run["backend"])
        if not note:
            continue
        item_id, tag = run["ref"], f"[{client.identity}/{run['slug'] or run['id']}]"
        body = f"{tag} still working — {note}"
        posted = (client.log_task(item_id, body) if item_kind(item_id) == "task"
                  else client.reply_issue(item_id, body))
        if posted is None:
            continue
        mark(con, int(run["id"]), "progress_at", db.now())
        actions.append({"action": "progress", "item": item_id, "run": run["id"]})


# --- one pass ---------------------------------------------------------------

def sweep(cfg: dict, client: WorkClient | None = None,
          launcher=supervise.spawn_supervisor) -> list[dict]:
    """One incremental pass over the whole workspace. Returns the actions
    taken. Never raises for a Work-side failure; the cursor simply does not
    advance.

    The client is BUILT HERE when the caller does not hand one in, and an
    unconfigured Work is an empty pass, not an error. That is what lets the
    daemon schedule this pass without knowing a source exists: it decides
    WHEN, the adapter decides WHO to talk to (CONTRACT §7 Enforcement 3).
    ``refresh_projects`` above has worked this way since v21.
    """
    actions: list[dict] = []
    if client is None:
        client = from_cfg(cfg)
        if client is None:
            return actions
    con = db.connect()
    try:
        # Before the report: the report's own receipt is what bounds the
        # mirror's scan, so a just-reported run must still get its ask across.
        _mirror(con, client, actions)
        _escalations(con, client, actions)
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
            f"SELECT 1 FROM runs WHERE claim_status='pending' "
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
        # Shaping runs before building: a refine-tagged item takes its run
        # slot first, so the worker that claims it next dispatches against
        # the rewritten sections.
        ok &= refine.after_fetch(con, cfg, client, fetched, launcher, actions)
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
