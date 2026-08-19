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

from dromond import db, project, work_client
from dromond.work_client import WorkError

CONFIDENCE = ("observed", "suspected")
# ponytail: a fixed default rather than a documented [settings] key; add the
# key to config.DEFAULT_CONFIG when someone actually wants to tune it.
DEFAULT_CHILD_CEILING = 12
_BLOCK = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)
_WORDS = re.compile(r"[^a-z0-9]+")


# --- parsing ----------------------------------------------------------------

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


def _issue_body(tag: str, run, finding: dict) -> str:
    return (f"{tag} filed by run {run['id']} — not delegated, for triage.\n\n"
            f"**Claim.** {finding['claim']}\n\n"
            f"- where: `{finding['where']}`\n"
            f"- confidence: {finding['confidence']}\n"
            f"- why not fixed: {finding['why_not_fixed']}\n")[:19000]


def file_findings(con, client, run, entries: list[dict],
                  project_path: str | None) -> list[dict]:
    """One Work issue per new finding; a repeat comments on the original."""
    results = []
    for finding in entries:
        fp = fingerprint(run["project_id"], finding["where"], finding["claim"])
        tag = _tag(client, run)
        row = _seen(con, fp)
        if row is not None:
            count = _bump(con, fp, run["id"])
            note = None
            if row["issue_id"]:
                try:
                    posted = client.reply_issue(
                        row["issue_id"],
                        f"{tag} run {run['id']} hit this again (occurrence {count}) "
                        f"at `{finding['where']}` — {finding['confidence']}.")
                    note = None if posted is not None else "work unreachable"
                except WorkError as exc:
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
            created = client.create_issue(_issue_body(tag, run, finding),
                                          title=title, project_path=project_path)
        except WorkError as exc:
            results.append({"action": "rejected", "fingerprint": fp, "error": exc.code})
            continue
        if created is None:  # Work unreachable: no fingerprint, so a retry files it
            results.append({"action": "deferred", "fingerprint": fp})
            continue
        issue_id = created.get("id") if isinstance(created, dict) else None
        _remember(con, fp, run, finding, issue_id)
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
        print(f"dromond: planner turn failed: {exc}")
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

def _child_count(client, goal_id: str) -> int:
    # ponytail: one whole-board GET per completion that proposes; Work has no
    # children-of route, so ask for one when it grows one.
    tasks = client.tasks()
    return sum(1 for t in (tasks or []) if t.get("parentId") == goal_id)


def _decision(client, run, goal_id: str, proposal: dict, tag: str,
              reasons: list[str], verdict: dict | None) -> dict:
    """A proposal the human must rule on: a Work decision in needs-you."""
    # The options say what actually HAPPENS, not what it is called. "Add as a
    # child" told the owner nothing: the choice is whether a new task exists.
    detail = [f"{tag} run {run['id']} proposed: {proposal.get('why') or '(no rationale given)'}",
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
            refs=[goal_id], project_path=proposal.get("project"))
    except WorkError as exc:
        return {"action": "rejected", "stage": "decision", "error": exc.code}
    if created is None:
        return {"action": "deferred", "stage": "decision"}
    return {"action": "decision", "reasons": reasons,
            "verdict": (verdict or {}).get("verdict"),
            "decision": created.get("id") if isinstance(created, dict) else None}


def file_proposals(con, client, run, entries: list[dict], project_path: str | None,
                   ceiling: int = DEFAULT_CHILD_CEILING) -> list[dict]:
    """Contract verb 5, with alignment judged elsewhere and tripwires here."""
    results = []
    goal_id = run["work_item"] if (run["work_item"] or "").startswith("W-") else None
    tag = _tag(client, run)
    children = None
    for raw in entries:
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
        if children is None:
            children = _child_count(client, goal_id)
        goal = client.task(goal_id) or {"id": goal_id}
        fired = tripwires(proposal, project_path=project_path,
                          child_count=children, ceiling=ceiling)
        verdict = evaluate_alignment(goal, proposal, run)
        if fired or verdict is None or verdict["verdict"] == "pivot":
            results.append(_decision(client, run, goal_id, proposal, tag, fired, verdict))
            continue
        try:
            created = client.create_task(
                title=title, parent_id=goal_id, project_path=project_path,
                description=(f"{tag} proposed by run {run['id']} under {goal_id}.\n\n"
                             f"{proposal.get('why') or ''}").strip()[:19000])
        except WorkError as exc:
            # Work's gate (W-0158) refuses an agent task with no delegated goal
            # parent. That is Dromond proposing wrongly, not a human decision:
            # record it, never retry blind, never fall back to top-level.
            print(f"dromond: proposal from run {run['id']} rejected: {exc}")
            results.append({"action": "rejected", "stage": "create_task",
                            "error": exc.code})
            continue
        if created is None:
            results.append({"action": "deferred", "stage": "create_task"})
            continue
        children += 1
        child_id = created.get("id") if isinstance(created, dict) else None
        client.log_task(goal_id, f"{tag} added child {child_id or ''} — {title} "
                                 f"(aligned: {verdict['rationale']})".strip()[:19000])
        results.append({"action": "child", "task": child_id,
                        "verdict": "aligned"})
    return results


# --- the confirm pass seam (DESIGN §9, per-goal opt-in) ----------------------

def confirm_candidates(filed: list[dict]) -> list[str]:
    """Fingerprints a confirm pass would take: newly filed `suspected`
    findings. Dispatch is deliberately not built — the seam is the shape a
    caller needs: for each fingerprint, a cheap short-lived run whose only
    mission is confirm-or-deny with evidence, gated on the goal opting in,
    reporting back by commenting on the finding's issue."""
    return [f["fingerprint"] for f in filed
            if f["action"] == "filed" and f.get("confidence") == "suspected"]


# --- the completion seam -----------------------------------------------------

def _record_problems(con, run_id: int, problems: list[str]) -> None:
    """A protocol failure is recorded, not silently passed: it goes in the run
    thread and on the run summary the sweeper posts to the Work item."""
    body = ("handoff protocol failure:\n"
            + "\n".join(f"- {p}" for p in problems))
    con.execute("INSERT INTO messages(run_id, sender, body, kind, created_at) "
                "VALUES(?, 'dromond', ?, 'protocol', ?)", (run_id, body, db.now()))
    row = con.execute("SELECT summary FROM runs WHERE id=?", (run_id,)).fetchone()
    summary = ((row["summary"] + "\n\n") if row and row["summary"] else "") + body
    con.execute("UPDATE runs SET summary=? WHERE id=?", (summary[:2000], run_id))
    con.commit()


def at_completion(con, cfg: dict, run, final_text: str | None,
                  status: str) -> dict:
    """The one seam ``supervise.py`` calls at finalization.

    Parses the handoff, records any protocol failure, and files what the run
    handed off. A run that did not finish cleanly never had a handoff turn,
    so it is not held to the protocol.
    """
    result = {"parsed": False, "problems": [], "findings": [], "proposals": []}
    if status != "done":
        return result
    handoff, problems = parse_handoff(final_text)
    entries = clean_findings(handoff["findings"], problems)
    result["parsed"] = True
    result["problems"] = problems
    if problems:
        _record_problems(con, run["id"], problems)
    client = work_client.from_cfg(cfg)
    if client is None:
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
    result["confirm_candidates"] = confirm_candidates(result["findings"])
    return result
