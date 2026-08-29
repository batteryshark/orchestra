"""The completion handoff PROTOCOL (DESIGN §9) — parsing and enforcement.

Two sibling **required** fields in a run's final message, ``findings: []``
and ``proposals: []``: empty is a valid answer, absent is not. This module
parses the handoff off the raw log, records any protocol failure on the run
row and in its thread, and stamps ``handoff_processed_at``. That is the
WHOLE core job: what a consumer does with the parsed entries — filing them
anywhere, deduplicating them, judging them — is the consumer's own policy,
built on this parse and the stamped receipt. The core files nothing and
knows no record system.
"""
import json
import re

from orchestra import db, runners

CONFIDENCE = ("observed", "suspected")
_BLOCK = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)


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
    so a claimless entry is dropped; anything else is kept with a note,
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


def record_problems(con, run_id: int, problems: list[str]) -> None:
    """A protocol failure is recorded, not silently passed: it goes in the
    run thread and on the run summary every consumer reads."""
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


def mark_processed(con, run_id: int) -> None:
    """Durably settle this consumer without rewriting an earlier stamp."""
    con.execute(
        "UPDATE runs SET handoff_processed_at=COALESCE(handoff_processed_at, ?) "
        "WHERE id=?", (db.now(), run_id))
    con.commit()


def read(run) -> tuple[dict, list[str]]:
    """Parse a terminal run's handoff off its raw log: (handoff, problems).

    The one read a policy consumer builds on. The raw log is the source of
    truth (DESIGN rule 12); a pruned or missing log parses as an absent
    handoff, which the problems list says out loud.
    """
    _, final_text = (runners.parse_log(run["log_path"])
                     if run.get("log_path") else (None, None))
    return parse_handoff(final_text)


def at_completion(con, run_result) -> dict:
    """The one seam finalization calls: enforce the protocol, stamp the row.

    Parses the handoff, records any protocol failure, and marks the row
    processed. A run that did not finish cleanly never had a handoff turn,
    so it is not held to the protocol. Filing what the run handed off is a
    consumer's own pass, built on ``read`` and the stamp.
    """
    result = {"parsed": False, "problems": [], "findings": [], "proposals": []}
    run = dict(run_result)
    if run.get("handoff_processed_at"):
        return result
    if run["status"] != "done":
        mark_processed(con, run["id"])
        return result
    handoff, problems = read(run)
    result["parsed"] = True
    result["problems"] = problems
    result["findings"] = clean_findings(handoff["findings"], problems)
    result["proposals"] = handoff["proposals"]
    if problems:
        record_problems(con, run["id"], problems)
    mark_processed(con, run["id"])
    return result
