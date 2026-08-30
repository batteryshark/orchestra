"""Neutral, bounded instructions frozen for one v2 run."""
from __future__ import annotations

from pathlib import Path


PROTOCOL = """## Orchestra protocol

- Work only inside the supplied working directory and declared external access.
- Do not alter the owner's checkout or rewrite git history. Orchestra snapshots changes.
- Operator messages may arrive between actions; apply them and continue.
- Use `orchestra ask` only when progress genuinely requires a decision or missing input.
- Publish only intentional outputs with `orchestra artifact PATH`.
- End with a concise result: what happened, useful outputs, and unresolved caveats.
"""

DELEGATION = """## Delegation

You may delegate bounded pieces with `orchestra child --profile PROFILE -- MISSION`.
Name each child profile explicitly. Children may use your tier or a lower tier; they
inherit this run's group and working directory. You remain responsible for combining
their results.
"""


def compose(*, run_id: int, display_number: str, profile_name: str,
            runtime_name: str, request: str, requester: str, group_name: str,
            workdir: str | Path, context: str | None = None,
            may_delegate: bool = False) -> str:
    parts = [f"""# {display_number}

- Run ID: `{run_id}`
- Group: **{group_name}**
- Profile: **{profile_name}** via **{runtime_name}**
- Requested by: **{requester}**
- Working directory: `{workdir}`

## Request

{request.strip()}
"""]
    if context and context.strip():
        parts.append(f"## Context\n\n{context.strip()}\n")
    parts.append(PROTOCOL)
    if may_delegate:
        parts.append(DELEGATION)
    return "\n".join(parts).rstrip() + "\n"


def resume_message(*, reason: str, messages: list[str], child_results: list[str] | None = None,
                   replay_risk: bool = False) -> str:
    parts = [f"# Resume this run\n\nReason: {reason.strip()}"]
    if replay_risk:
        parts.append(
            "The prior turn ended before a reliable session reference was captured. "
            "This is a replay from the frozen brief; inspect existing files before "
            "repeating any side effect.")
    if messages:
        parts.append("## New direction\n\n" + "\n\n".join(messages))
    if child_results:
        parts.append("## Child results\n\n" + "\n\n".join(child_results))
    return "\n\n".join(parts).rstrip() + "\n"
