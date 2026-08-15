"""Compose worker briefs.

DESIGN D6 budget: the fixed portion of every dispatch brief stays at or
under 300 tokens (tested as <= 1,200 chars); the protocol card renders in
<= 10 lines; the continuation wrapper stays ~130 tokens.
"""
from pathlib import Path

# Must stay <= 10 lines and inside the D6 budget (tests enforce both).
PROTOCOL_CARD = """\
## Protocol

- Keep file changes inside the working directory.
- Commit your git changes before you stop.
- Operator messages may arrive between actions; apply them, then continue the mission.
- Your final message is the handoff: what changed, how you verified it, what remains.
- End it with a ```json block: {"findings": [], "proposals": []} — both keys required, [] is fine.
- finding: {claim, where, confidence: observed|suspected, why_not_fixed}. proposal: {title, why}.
"""

WORK_SNAPSHOT_MAX_CHARS = 2000
RECENT_COMMITS_MAX_CHARS = 900

# Only runs carrying a Work item get this: a worker with no item has no
# checklist to answer, and a brief never teaches a verb it cannot use.
WORK_CHECKLIST_PROTOCOL = """\
Before you stop, account for every requirement and acceptance criterion above.
Tick each one you verified: `work check {item} requirement|acceptance <index>`
(indexes count from 0, as `work show {item}` lists them). Decline each one you
did not, with the reason: `--decline "not attempted, blocked on X"`. Declining
is expected and is not a failure — leaving an item unanswered is, and the item
cannot move to review or blocked while any is unanswered.
"""


def _protocol_card(profile: dict) -> str:
    """D11: mention spawning ONLY when this profile may delegate, then name
    the permitted profiles — a worker is never taught a forbidden verb."""
    spawn = profile.get("spawn_profiles") or []
    if not spawn:
        return PROTOCOL_CARD
    return PROTOCOL_CARD + \
        f"- You may delegate child runs, only to these profiles: {', '.join(spawn)}.\n"


def compose(*, run_id: int, slug: str | None, profile: dict, mission: str,
            requester: str, root: Path, workdir: str,
            extra_context: str | None = None,
            work_snapshot: str | None = None,
            work_item: str | None = None,
            recent_commits: list[str] | None = None) -> str:
    run_label = f"{run_id} · {slug}" if slug else str(run_id)
    parts = [f"""# Run {run_label}

Profile: **{profile['name']}** · Requested by: **{requester}**

Work autonomously; make reasonable assumptions and document them.
Project: `{root}` · Working directory: `{workdir}`.

## Mission

{mission}
"""]
    if recent_commits:
        # Frozen at dispatch like the Work snapshot, and for the same reason:
        # a run reads its brief again on resume, and a brief that changed
        # underneath it describes a project it never worked on.
        listed = "\n".join(f"- {line}" for line in recent_commits)
        parts.append("## Recently landed here\n\n"
                     f"{listed[:RECENT_COMMITS_MAX_CHARS]}\n\n"
                     "Read this before you start. Work already done is not "
                     "your mission, and repeating it is worse than skipping "
                     "it.\n")
    if work_snapshot:
        # Phase-2 seam: the sweeper freezes the Work item snapshot at
        # dispatch and passes it here, capped at 2,000 chars (D6).
        parts.append(f"## Work item snapshot\n\n{work_snapshot[:WORK_SNAPSHOT_MAX_CHARS]}\n")
        if work_item:
            parts.append(WORK_CHECKLIST_PROTOCOL.format(item=work_item))
    if extra_context:
        parts.append(f"## Additional context\n\n{extra_context}\n")
    parts.append(_protocol_card(profile))
    return "\n".join(parts)


def compose_continuation(*, run_id: int, parent_run: int, instructions: str,
                         landed: list[str] | None = None) -> str:
    """Wrap incremental instructions for a real backend-session continuation."""
    # A resumed run is the one most likely to redo finished work: its worktree
    # branched before these commits and cannot see them, and its session
    # remembers a project that has since moved on.
    since = ""
    if landed:
        listed = "\n".join(f"- {line}" for line in landed)
        since = ("\n## Landed on the base branch since you started\n\n"
                 f"{listed[:RECENT_COMMITS_MAX_CHARS]}\n\n"
                 "Your checkout does not contain these. Do not rebuild them.\n")
    return f"""# Run {run_id} — continuation of run {parent_run}

The original mission and protocol remain in this session. Apply this follow-up;
it overrides earlier instructions only where they conflict.

{instructions.strip()}
{since}
Commit your git changes before you stop; end with the usual handoff summary."""
