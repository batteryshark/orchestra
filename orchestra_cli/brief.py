"""Compose the worker brief injected into every dispatched agent."""
import json
import shutil
import subprocess
from pathlib import Path


def _checked_lines(items: object) -> list[str]:
    if not isinstance(items, list):
        return []
    lines = []
    for item in items:
        if not isinstance(item, dict) or not item.get("text"):
            continue
        mark = "x" if item.get("checked") else " "
        lines.append(f"- [{mark}] {item['text']}")
    return lines


def _render_work_snapshot(raw: str) -> str:
    """Turn ``work show`` JSON into a small, human-oriented dispatch snapshot.

    Unknown/non-JSON output is preserved because older slash-work versions may
    already provide a readable display.
    """
    try:
        item = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    if not isinstance(item, dict):
        return raw

    title = item.get("title")
    heading = f"**{item.get('id', 'Work item')}**"
    if title:
        heading += f" — {title}"
    lines = [heading]

    summary = [
        str(value) for value in (
            item.get("status"),
            item.get("priority"),
            item.get("type"),
        ) if value not in (None, "", [], {})
    ]
    if summary:
        lines.append(" · ".join(summary))

    for label, key in (
        ("Project", "projectPath"),
        ("Parent", "parentId"),
        ("Depends on", "dependsOn"),
        ("Blocked by", "blockedBy"),
        ("Tags", "tags"),
    ):
        value = item.get(key)
        if value in (None, "", [], {}):
            continue
        display = ", ".join(map(str, value)) if isinstance(value, list) else str(value)
        lines.append(f"- {label}: {display}")

    sections = item.get("sections") if isinstance(item.get("sections"), dict) else {}
    goal = sections.get("goal")
    if goal:
        lines.extend(("", "**Goal**", str(goal).strip()))

    for label, key in (
        ("Requirements", "requirements"),
        ("Acceptance criteria", "acceptanceCriteria"),
    ):
        checklist = _checked_lines(item.get(key))
        if checklist:
            lines.extend(("", f"**{label}**", *checklist))

    return "\n".join(lines).strip()


def work_snapshot(root: Path, item: str) -> str:
    if not shutil.which("work"):
        return ""
    try:
        out = subprocess.run(["work", "show", item], cwd=root, capture_output=True,
                             text=True, timeout=20).stdout.strip()
        return _render_work_snapshot(out)[:6000]
    except Exception:
        return ""


def _coordination(*, run_id: int, requester: str, work_item: str | None,
                  allow_question: bool, question_wait_seconds: int) -> str:
    question = (
        f'You may pause once with `orchestra ask "<question>" --default "<safe fallback>"`; '
        f"Orchestra waits up to {question_wait_seconds} seconds, then resumes with the answer "
        "or fallback."
        if allow_question else
        'For a blocker, notify the requester with `orchestra report "BLOCKER: <details>"` '
        "and continue with a documented assumption."
    )
    progress = (
        'Send meaningful progress with `orchestra report "<update>"`; Orchestra attaches '
        f"it to {work_item} automatically."
        if work_item else
        'Send meaningful progress with `orchestra report "<update>"`.'
    )
    return f"""## Coordination

Orchestra exports this process's identity and run ID; commands infer them automatically.

- Start with `orchestra inbox --unread --mark-read`.
- Batch independent read-only searches, file reads, and diagnostics in one tool-call group.
  Keep dependent operations and overlapping writes sequential.
- If you lead a decomposable mission, delegate bounded pieces with
  `orchestra spawn --to <agent> "<mission>"`. Never call top-level
  `orchestra dispatch` from a supervised run.
- {progress}
- Send findings or peer messages with `orchestra note` / `orchestra send`; check `orchestra roster` when needed.
- When upstream context could prevent likely rework, ask with
  `orchestra consult "<question>"`. It routes to the requester without pausing this run;
  keep working on your best documented assumption until guidance arrives.
- {question}
- Before stopping, send `orchestra handoff "<files, verification, remaining work>"`.
- Do not update or move tracker items directly; Orchestra records the run-bound report/handoff,
  and the requester owns tracker state transitions.

Follow any applicable project `SKILL.md` in `.agents/skills/`, `.claude/skills/`,
`.opencode/skill/`, or `.codex/skills/`.
"""


def compose_continuation(*, run_id: int, parent_run: int, requester: str,
                         instructions: str, work_item: str | None,
                         allow_question: bool = False,
                         question_wait_seconds: int = 1800) -> str:
    """Wrap incremental instructions for a real backend-session continuation."""
    question = (
        f'\nYou may still pause once with `orchestra ask "<question>" --default '
        f'"<safe fallback>"`; the wait is {question_wait_seconds} seconds.'
        if allow_question else ""
    )
    return f"""# Run {run_id} — continuation of run {parent_run}

The original mission, project instructions, and coordination contract remain in this
session. Apply this follow-up; it overrides earlier instructions only where they conflict.

{instructions.strip()}
{question}

For non-blocking guidance, use `orchestra consult "<question>"` and keep working on
your best documented assumption.

Before stopping, send `orchestra handoff "<result, verification, remaining risks>"`."""


def compose(*, root: Path, run_id: int, agent: dict, mission: str,
            work_item: str | None, team: str | None, requester: str,
            workdir: str, extra_context: str | None = None,
            lead_run: int | None = None, allow_question: bool = False,
            question_wait_seconds: int = 1800, slug: str | None = None) -> str:
    name = agent["name"]
    run_label = f"{run_id} · {slug}" if slug else str(run_id)
    autonomy = (
        "Work autonomously and make reasonable assumptions. You have ONE blocking-question "
        "option for unsafe or materially wasteful ambiguity; see Coordination."
        if allow_question else
        "Work autonomously. Seek advisory guidance when it can prevent likely rework, but keep "
        "moving on a reasonable documented assumption."
    )
    location = f"Project and working directory: `{workdir}`."
    if str(root) != workdir:
        location = f"Project: `{root}` · Working directory: `{workdir}`."
    team_text = f" · Team: {team}" if team else ""
    parts = [f"""# Run {run_label}

Profile: **{name}** · Requested by: **{requester}**{team_text}

{autonomy} {location} Keep file changes inside the working directory.

## Mission

{mission}
"""]
    if work_item:
        snap = work_snapshot(root, work_item)
        parts.append(f"""## Tracked work item: {work_item}

The tracker is the durable source of truth. Run `work show {work_item}` for full context.
""" + (f"\n{snap}\n" if snap else ""))
    if extra_context:
        parts.append(f"## Additional context\n\n{extra_context}\n")
    if lead_run is not None:
        parts.append(f"""## Child-run contract

This is an isolated child of lead run **{lead_run}**. Return a focused result; do not
merge your branch automatically. Your completion and branch are reported to the lead.
You may use `orchestra spawn` only if project policy permits another depth level.
""")
    parts.append(_coordination(
        run_id=run_id, requester=requester, work_item=work_item,
        allow_question=allow_question, question_wait_seconds=question_wait_seconds,
    ))
    if agent.get("ensemble"):
        pool = agent.get("model_pool", [])
        parts.append(f"""## Team lead

Use the OpenCode Ensemble tools to split independent tasks across this pool:
{', '.join(pool) or '(configured pool)'}. Track results, merge deliberately, verify the combined
work, then shut down and clean up the team before sending the normal handoff. The persistent
host may wake this session when teammates finish; check team status before continuing.
""")
    return "\n".join(parts)
