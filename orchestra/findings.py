"""Completion handoff: findings and proposals (DESIGN §9).

Two sibling **required** fields in a run's final message, ``findings: []``
and ``proposals: []``: empty is a valid answer, absent is not. **Code files
them** (principle 6) — the worker never writes to Work, so it cannot forget,
cannot self-approve, and cannot file itself work.

- A finding (``claim`` / ``where`` / ``confidence`` / ``why_not_fixed``)
  becomes a Work issue attributed to the run and never delegated, so it
  lands in triage. Work's issue create has no ``delegated`` field at all:
  not-delegated is the server-side default and the flag is human-only, so
  the invariant is held by not sending anything.
- A repeat of the same ``(project, where, normalized claim)`` fingerprint
  increments an occurrence count and comments on the original issue instead
  of filing a duplicate.
- A proposal is contract verb 5: a task parented to the run's delegated goal
  item, never top-level, attributed to the run. Work gates agent task
  creation on that parent; a rejection is recorded, never retried blind.
- Alignment is a **planner turn, not code** (``PLANNER``, W-0099). Until a
  planner exists every proposal is unevaluated, and an unevaluated proposal
  goes to the human — nothing here approves anything. The mechanical
  tripwires below force human review regardless of any verdict.
"""
import hashlib
import json
import re

from orchestra import db, project, runners, work_client
from orchestra.work_client import WorkError

CONFIDENCE = ("observed", "suspected")
# ponytail: a fixed default rather than a documented [settings] key; add the
# key to config.DEFAULT_CONFIG when someone actually wants to tune it.
DEFAULT_CHILD_CEILING = 12
_BLOCK = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)
_WORDS = re.compile(r"[^a-z0-9]+")


# --- parsing ----------------------------------------------------------------

def halt_reason(text: str | None) -> str | None:
    """Non-empty ``halt`` from the last handoff-shaped JSON block, else None.

    A halt-only block counts: a doomed run need not also file a handoff.
    """
    for raw in reversed(_BLOCK.findall(text or "")):
        try:
            candidate = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(candidate, dict):
            continue
        if "halt" not in candidate and "findings" not in candidate \
                and "proposals" not in candidate:
            continue
        reason = candidate.get("halt")
        if isinstance(reason, str) and reason.strip():
            return reason.strip()[:2000]
        return None
    return None


def parse_handoff(text: str | None) -> tuple[dict, list[str]]:
    """Split a final message into (handoff, protocol problems).

    Both fields are required, so a missing one is a problem the supervisor
    records — never a silent pass. Returns usable empty lists either way:
    one malformed field must not lose the other.
    """
    problems: list[str] = []
    data = None
    for raw in reversed(_BLOCK.findall(text or "")):
        try:
            candidate = json.loads(raw)
        except ValueError:
            continue
        if isinstance(candidate, dict) and ("findings" in candidate
                                            or "proposals" in candidate):
            data = candidate
            break
    if data is None:
        return ({"findings": [], "proposals": []},
                ["no handoff block in the final message: `findings` and "
                 "`proposals` are both required (empty lists are valid)"])
    out = {}
    for key in ("findings", "proposals"):
        value = data.get(key)
        if key not in data:
            problems.append(f"handoff is missing the required `{key}` field")
            value = []
        elif not isinstance(value, list):
            problems.append(f"handoff `{key}` is not a list")
            value = []
        entries = [e for e in value if isinstance(e, dict)]
        if len(entries) != len(value):
            problems.append(f"handoff `{key}` had non-object entries; they were dropped")
        out[key] = entries
    return out, problems


def clean_findings(entries: list, problems: list[str]) -> list[dict]:
    """Normalize to the four DESIGN §9 fields. A claim is the whole finding,
    so a claimless entry is dropped; anything else is filed with a note,
    because throwing away what a run noticed is worse than a typo."""
    out = []
    for index, raw in enumerate(entries, 1):
        claim = str(raw.get("claim") or "").strip()
        if not claim:
            problems.append(f"finding {index} has no `claim`; dropped")
            continue
        confidence = str(raw.get("confidence") or "").strip().lower()
        if confidence not in CONFIDENCE:
            problems.append(
                f"finding {index} confidence {confidence or 'missing'!r} is not "
                "observed|suspected; recorded as suspected")
            confidence = "suspected"
        why = str(raw.get("why_not_fixed") or "").strip()
        if not why:
            problems.append(f"finding {index} has no `why_not_fixed`")
        out.append({"claim": claim,
                    "where": str(raw.get("where") or "").strip() or "unspecified",
                    "confidence": confidence,
                    "why_not_fixed": why or "not stated"})
    return out


# --- fingerprint dedup -------------------------------------------------------

def fingerprint(project_id: str | None, where: str, claim: str) -> str:
    """Stable id for (project, where, normalized claim). Normalization is
    lowercase alphanumeric words, so punctuation and spacing drift do not
    file the same finding twice."""
    claim_words = " ".join(_WORDS.sub(" ", claim.lower()).split())
    where_words = " ".join(_WORDS.sub(" ", (where or "").lower()).split())
    key = "\x1f".join([project_id or "", where_words, claim_words])
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _seen(con, fp: str):
    return con.execute("SELECT * FROM finding_fingerprints WHERE fingerprint=?",
                       (fp,)).fetchone()


def _remember(con, fp: str, run, finding: dict, issue_id: str | None) -> None:
    ts = db.now()
    con.execute(
        "INSERT INTO finding_fingerprints(fingerprint, project_id, location, claim, "
        "issue_id, occurrences, first_run, last_run, first_seen_at, last_seen_at) "
        "VALUES(?,?,?,?,?,1,?,?,?,?)",
        (fp, run["project_id"], finding["where"], finding["claim"], issue_id,
         run["id"], run["id"], ts, ts))
    con.commit()


def _bump(con, fp: str, run_id: int) -> int:
    con.execute(
        "UPDATE finding_fingerprints SET occurrences=occurrences+1, last_run=?, "
        "last_seen_at=? WHERE fingerprint=?", (run_id, db.now(), fp))
    con.commit()
    row = _seen(con, fp)
    return int(row["occurrences"]) if row else 1


# --- filing ------------------------------------------------------------------

def _tag(client, run) -> str:
    return f"[{client.identity}/{run['slug'] or run['id']}]"


def _record_with(records: list[dict], marker: str) -> dict | None:
    return next((record for record in records
                 if marker in json.dumps(record, sort_keys=True)), None)


def _issue_body(marker: str, run, finding: dict) -> str:
    return (f"{marker} filed by run {run['id']} — not delegated, for triage.\n\n"
            f"**Claim.** {finding['claim']}\n\n"
            f"- where: `{finding['where']}`\n"
            f"- confidence: {finding['confidence']}\n"
            f"- why not fixed: {finding['why_not_fixed']}\n")[:19000]


def file_findings(con, client, run, entries: list[dict],
                  project_path: str | None) -> list[dict]:
    """One Work issue per new finding; a repeat comments on the original."""
    results = []
    remote_issues = None
    for finding in entries:
        fp = fingerprint(run["project_id"], finding["where"], finding["claim"])
        tag = _tag(client, run)
        marker = f"{tag} finding:{fp}"
        row = _seen(con, fp)
        if row is not None:
            count = (int(row["occurrences"]) if row["last_run"] == run["id"]
                     else _bump(con, fp, run["id"]))
            if row["first_run"] == run["id"]:
                results.append({"action": "filed", "fingerprint": fp,
                                "issue": row["issue_id"],
                                "confidence": finding["confidence"]})
                continue
            note = None
            if row["issue_id"]:
                repeat = (f"{tag} run {run['id']} hit this again (occurrence {count}) "
                          f"at `{finding['where']}` — {finding['confidence']}.")
                try:
                    issue = client.issue(row["issue_id"])
                    if issue is None:
                        results.append({"action": "deferred", "fingerprint": fp,
                                        "occurrences": count})
                        continue
                    messages = [m.get("body") for m in issue.get("messages") or []]
                    if repeat not in messages:
                        posted = client.reply_issue(row["issue_id"], repeat)
                        if posted is None:
                            results.append({"action": "deferred", "fingerprint": fp,
                                            "occurrences": count})
                            continue
                except WorkError as exc:
                    if work_client.retryable(exc):
                        results.append({"action": "deferred", "fingerprint": fp,
                                        "occurrences": count})
                        continue
                    # An issue nobody claimed (or a closed one) refuses agent
                    # replies. The occurrence count is still recorded, which is
                    # the part a duplicate issue would have destroyed.
                    note = exc.code
            results.append({"action": "duplicate", "fingerprint": fp,
                            "issue": row["issue_id"], "occurrences": count,
                            "comment_skipped": note})
            continue
        title = finding["claim"].splitlines()[0][:120]
        try:
            if remote_issues is None:
                remote_issues = client.issues()
            if remote_issues is None:
                results.append({"action": "deferred", "fingerprint": fp})
                continue
            created = _record_with(remote_issues, marker)
            if created is None:
                created = client.create_issue(
                    _issue_body(marker, run, finding), title=title,
                    project_path=project_path)
        except WorkError as exc:
            action = "deferred" if work_client.retryable(exc) else "rejected"
            results.append({"action": action, "fingerprint": fp,
                            "error": exc.code})
            continue
        if created is None:  # Work unreachable: no fingerprint, so a retry files it
            results.append({"action": "deferred", "fingerprint": fp})
            continue
        issue_id = created.get("id") if isinstance(created, dict) else None
        _remember(con, fp, run, finding, issue_id)
        remote_issues.append(created)
        results.append({"action": "filed", "fingerprint": fp, "issue": issue_id,
                        "confidence": finding["confidence"]})
    return results


# --- the planner seam (W-0099) -----------------------------------------------

PLANNER = None
"""Set by the conductor (W-0099) to a callable

    PLANNER(goal: dict, proposal: dict, run: dict) -> {
        "verdict": "aligned" | "pivot",   # forced binary, nothing else
        "rationale": str,                 # why, in the planner's words
    }

It is a separate episodic session holding the goal — never the worker that
proposed, never this module. Anything but the two verdicts (no planner, an
error, a hedge) means *unevaluated*, and an unevaluated proposal goes to the
human. Code never supplies the judgement, only the plumbing.
"""


def evaluate_alignment(goal: dict, proposal: dict, run) -> dict | None:
    """Ask the planner. ``None`` means unevaluated — the human decides."""
    if PLANNER is None:
        return None
    try:
        result = PLANNER(goal=goal, proposal=proposal, run=run)
    except Exception as exc:  # a planner turn failing is not an approval
        print(f"orchestra: planner turn failed: {exc}")
        return None
    verdict = (result or {}).get("verdict")
    if verdict not in ("aligned", "pivot"):
        return None
    return {"verdict": verdict,
            "rationale": str((result or {}).get("rationale") or "").strip()[:2000]}


def tripwires(proposal: dict, *, project_path: str | None, child_count: int,
              ceiling: int) -> list[str]:
    """Mechanical checks that force human review whatever the planner said."""
    fired = []
    other = str(proposal.get("project") or "").strip()
    # An unknown project_path is not a safe one: a named other project fires.
    if other and other != project_path:
        fired.append(f"touches another project ({other})")
    if proposal.get("changes_acceptance") or proposal.get("acceptance_criteria"):
        fired.append("changes the goal's acceptance criteria")
    if child_count + 1 > ceiling:
        fired.append(f"goal already has {child_count} children (ceiling {ceiling})")
    return fired


# --- proposals ---------------------------------------------------------------

def _child_count(tasks: list[dict], goal_id: str) -> int:
    return sum(1 for t in (tasks or []) if t.get("parentId") == goal_id)


def _decision(client, run, goal_id: str, proposal: dict, tag: str,
              reasons: list[str], verdict: dict | None, marker: str,
              decisions: list[dict], project_path: str | None = None) -> dict:
    """A proposal the human must rule on: a Work decision in needs-you."""
    existing = _record_with(decisions, marker)
    if existing is not None:
        return {"action": "decision", "reasons": reasons,
                "verdict": (verdict or {}).get("verdict"),
                "decision": existing.get("id")}
    # The options say what actually HAPPENS, not what it is called. "Add as a
    # child" told the owner nothing: the choice is whether a new task exists.
    detail = [marker,
              f"{tag} run {run['id']} proposed: {proposal.get('why') or '(no rationale given)'}",
              f"Choosing to create it files a NEW backlog task titled "
              f"\u201c{proposal['title']}\u201d, parented to {goal_id}. "
              f"Declining creates nothing and records that it was proposed."]
    if verdict:
        detail.append(f"Planner verdict: {verdict['verdict']} — {verdict['rationale']}")
    else:
        detail.append("Planner verdict: none (unevaluated — no planner turn ran).")
    if reasons:
        detail.append("Tripwires: " + "; ".join(reasons))
    try:
        created = client.create_decision(
            title=f"Adopt proposal from run {run['id']}: {proposal['title']}"[:300],
            detail="\n\n".join(detail)[:19000],
            options=[f"Create it as a new backlog task under {goal_id}",
                     "Decline — create nothing"],
            # Work refuses an agent decision without a recommendationReason.
            # No lean is the honest answer: adopting scope is the owner's call.
            recommendation_reason=("No lean: whether new scope is adopted is "
                                   "the owner's call. The planner verdict and "
                                   "tripwires above are evidence, not a "
                                   "recommendation."),
            # The decision files where the run worked unless the proposal
            # names another project — same rule as the adopted-task path.
            # Runs 38's two projectless orphans are the offense record.
            refs=[goal_id], project_path=proposal.get("project") or project_path)
    except WorkError as exc:
        return {"action": ("deferred" if work_client.retryable(exc)
                           else "rejected"),
                "stage": "decision", "error": exc.code}
    if created is None:
        return {"action": "deferred", "stage": "decision"}
    decisions.append(created)
    return {"action": "decision", "reasons": reasons,
            "verdict": (verdict or {}).get("verdict"),
            "decision": created.get("id") if isinstance(created, dict) else None}


def _child_result(client, goal_id: str, marker: str, title: str,
                  verdict: dict, child: dict) -> dict:
    """Finish the one informational parent comment without repeating it."""
    child_id = child.get("id") if isinstance(child, dict) else None
    note = (f"{marker} added child {child_id or ''} — {title} "
            f"(aligned: {verdict['rationale']})").strip()[:19000]
    try:
        goal = client.task(goal_id)
        if goal is None:
            return {"action": "deferred", "stage": "child_comment"}
        comments = [entry.get("message") for entry in goal.get("log") or []]
        if not any(marker in (comment or "") for comment in comments) \
                and client.log_task(goal_id, note) is None:
            return {"action": "deferred", "stage": "child_comment"}
    except WorkError as exc:
        if work_client.retryable(exc):
            return {"action": "deferred", "stage": "child_comment"}
        return {"action": "child", "task": child_id, "verdict": "aligned",
                "comment_skipped": exc.code}
    return {"action": "child", "task": child_id, "verdict": "aligned"}


def file_proposals(con, client, run, entries: list[dict], project_path: str | None,
                   ceiling: int = DEFAULT_CHILD_CEILING) -> list[dict]:
    """Contract verb 5, with alignment judged elsewhere and tripwires here."""
    results = []
    goal_id = run["work_item"] if (run["work_item"] or "").startswith("W-") else None
    tag = _tag(client, run)
    children = None
    tasks = decisions = None
    for index, raw in enumerate(entries, 1):
        title = str(raw.get("title") or "").strip()
        if not title:
            results.append({"action": "dropped", "reason": "proposal has no `title`"})
            continue
        proposal = dict(raw, title=title)
        if goal_id is None:
            # Verb 5 is *always* parented to a delegated goal. A run serving an
            # issue (or nothing) has no goal, so there is nothing to parent to.
            results.append({"action": "dropped", "reason": "run serves no goal task"})
            continue
        if tasks is None or decisions is None:
            tasks, decisions = client.tasks(), client.decisions()
            if tasks is None or decisions is None:
                results.append({"action": "deferred", "stage": "preflight"})
                continue
        marker = f"{tag} proposal:{index}"
        existing = next((task for task in tasks
                         if task.get("parentId") == goal_id
                         and marker in json.dumps(task, sort_keys=True)), None)
        if existing is not None:
            verdict = {"verdict": "aligned", "rationale": "already created"}
            results.append(_child_result(client, goal_id, marker, title,
                                         verdict, existing))
            continue
        existing = _record_with(decisions, marker)
        if existing is not None:
            results.append({"action": "decision", "reasons": [],
                            "verdict": None, "decision": existing.get("id")})
            continue
        if children is None:
            children = _child_count(tasks, goal_id)
        goal = client.task(goal_id)
        if goal is None:
            results.append({"action": "deferred", "stage": "goal"})
            continue
        fired = tripwires(proposal, project_path=project_path,
                          child_count=children, ceiling=ceiling)
        verdict = evaluate_alignment(goal, proposal, run)
        if fired or verdict is None or verdict["verdict"] == "pivot":
            results.append(_decision(client, run, goal_id, proposal, tag, fired,
                                     verdict, marker, decisions, project_path))
            continue
        try:
            created = client.create_task(
                title=title, parent_id=goal_id, project_path=project_path,
                description=(f"{marker}\n\n{tag} proposed by run {run['id']} "
                             f"under {goal_id}.\n\n"
                             f"{proposal.get('why') or ''}").strip()[:19000])
        except WorkError as exc:
            if work_client.retryable(exc):
                results.append({"action": "deferred", "stage": "create_task",
                                "error": exc.code})
                continue
            # Work's gate (W-0158) refuses an agent task with no delegated goal
            # parent. That is Orchestra proposing wrongly, not a human decision:
            # record it, never retry blind, never fall back to top-level.
            print(f"orchestra: proposal from run {run['id']} rejected: {exc}")
            results.append({"action": "rejected", "stage": "create_task",
                            "error": exc.code})
            continue
        if created is None:
            results.append({"action": "deferred", "stage": "create_task"})
            continue
        children += 1
        tasks.append(created)
        results.append(_child_result(client, goal_id, marker, title,
                                     verdict, created))
    return results


# --- the completion seam -----------------------------------------------------

def _record_problems(con, run_id: int, problems: list[str]) -> None:
    """A protocol failure is recorded, not silently passed: it goes in the run
    thread and on the run summary the sweeper posts to the Work item."""
    body = ("handoff protocol failure:\n"
            + "\n".join(f"- {p}" for p in problems))
    con.execute(
        "INSERT INTO messages(run_id, sender, body, kind, created_at) "
        "SELECT ?, 'orchestra', ?, 'protocol', ? WHERE NOT EXISTS ("
        "SELECT 1 FROM messages WHERE run_id=? AND kind='protocol' AND body=?)",
        (run_id, body, db.now(), run_id, body))
    row = con.execute("SELECT summary FROM runs WHERE id=?", (run_id,)).fetchone()
    summary = row["summary"] if row else None
    if body not in (summary or ""):
        summary = ((summary + "\n\n") if summary else "") + body
        con.execute("UPDATE runs SET summary=? WHERE id=?", (summary[:2000], run_id))
    con.commit()


def _mark_processed(con, run_id: int) -> None:
    """Durably settle this consumer without rewriting an earlier stamp."""
    con.execute(
        "UPDATE runs SET handoff_processed_at=COALESCE(handoff_processed_at, ?) "
        "WHERE id=?", (db.now(), run_id))
    con.commit()


def _settled(entries: list[dict]) -> bool:
    """False only when Work was unreachable and a later tick should retry."""
    return all(entry.get("action") != "deferred" for entry in entries)


def at_completion(con, cfg: dict, run_result) -> dict:
    """The one seam ``supervise.py`` calls at finalization.

    The terminal run row is the contract; the raw log holds its handoff.
    Parses that handoff, records any protocol failure, and files what the run
    handed off. A run that did not finish cleanly never had a handoff turn, so
    it is not held to the protocol.
    """
    result = {"parsed": False, "problems": [], "findings": [], "proposals": []}
    run = dict(run_result)
    if run.get("handoff_processed_at"):
        return result
    if run["status"] != "done":
        _mark_processed(con, run["id"])
        return result
    _, final_text = (runners.parse_log(run["log_path"])
                     if run.get("log_path") else (None, None))
    handoff, problems = parse_handoff(final_text)
    entries = clean_findings(handoff["findings"], problems)
    result["parsed"] = True
    result["problems"] = problems
    if problems:
        _record_problems(con, run["id"], problems)
    client = work_client.from_cfg(cfg)
    if client is None:
        _mark_processed(con, run["id"])
        return result  # Work off: the protocol is still enforced, locally
    hit = project.by_id(con, run["project_id"])
    if hit is None and project.refresh(con, cfg):  # a supervisor may hold a cold cache
        hit = project.by_id(con, run["project_id"])
    project_path = hit.work_id if hit else None
    result["findings"] = file_findings(con, client, run, entries, project_path)
    ceiling = int(cfg.get("settings", {}).get("proposal_child_ceiling",
                                              DEFAULT_CHILD_CEILING))
    result["proposals"] = file_proposals(con, client, run, handoff["proposals"],
                                         project_path, ceiling=ceiling)
    if _settled(result["findings"]) and _settled(result["proposals"]):
        _mark_processed(con, run["id"])
    return result
