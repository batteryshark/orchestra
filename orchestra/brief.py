"""Compose worker briefs.

DESIGN D6 budget: the fixed portion of every dispatch brief stays at or
under 300 tokens (tested as <= 1,200 chars); the protocol card renders in
<= 10 lines; the continuation wrapper stays ~130 tokens.
"""
import re
from pathlib import Path

# Must stay <= 10 lines and inside the D6 budget (tests enforce both).
PROTOCOL_CARD = """\
## Protocol

- Keep file changes inside the working directory.
- Never run git write commands. Orchestra checkpoints isolated runs and may land their branches.
- Sandbox: working directory yes, .git host-owned, /tmp yes. Leave files; do not work around it.
- Operator messages may arrive between actions; apply them, then continue the mission.
- Never wait or poll for external state. The daemon delivers relevant events as messages.
- Your final message is the handoff: what changed, how you verified it, what remains.
- End it with a ```json block: {"findings": [], "proposals": []} — both keys required, [] is fine. "halt": "reason" marks the run halted.
- finding: {claim, where, confidence: observed|suspected, why_not_fixed}. proposal: {title, why}.
"""

WORK_SNAPSHOT_MAX_CHARS = 2000
RECENT_COMMITS_MAX_CHARS = 900
POSTCOMPACT_MAX_CHARS = 5000  # about 1,000 tokens; never inject the full brief

# House style for every writeback. Read from disk so an edit to the doc
# is an edit to every brief, with no code change (W-0250).
# ponytail: repo-relative path; a wheel install would need package_data.
WRITEBACK_STYLE = Path(__file__).resolve().parent.parent / "docs/WRITEBACK-STYLE.md"

# Only runs carrying a Work item get this: a worker with no item has no
# checklist to answer, and a brief never teaches a verb it cannot use.
WORK_CHECKLIST_PROTOCOL = """\
Before you stop, account for every requirement and acceptance criterion above.
Tick each one you verified: `work check {item} requirement|acceptance <index> --root {root}`
(indexes count from 0, as `work show {item} --root {root}` lists them). Decline
each one you did not: `work check {item} requirement|acceptance <index> --root {root} --decline "not attempted, blocked on X"`.
Declining is expected and is not a failure — leaving an item unanswered is.
Whatever you leave unanswered is declined for you, naming your run as the one
that did not account for it.
"""


# The goal standard the refine lane writes to (W-0309), vendored beside the
# house style and referenced the same way: by path, never copied into a
# string here.
# ponytail: the run reads it from outside its working directory. Inline the
# 5 KB into the mission if a harness sandbox ever refuses that read.
GOAL_STANDARD = Path(__file__).resolve().parent.parent / "docs/GOAL-STANDARD.md"

# The refine lane's mission (W-0309). A refinement run shapes ONE Work item
# and touches no code: it reads the item and the repository, rewrites the six
# sections around the owner's own words, and drops the tag that asked for it.
# The rules are the refine-work-item skill's, distilled — evidence or a `Q:`
# line, never an invention.
REFINE_MISSION = """\
Refine Work item {item} to the goal standard. This is a shaping pass, not
execution: change no file in this project, run no build, write no code.

1. Read the item whole: `work show {item} --json --root {root}` — every
   section, both checklists, and the whole progress log. A late log line
   outranks the description where the two disagree.
2. Read the standard at `{standard}`. It defines every term below.
3. Gather evidence: the files, modules and commands the item names,
   `work list --root {root}` for neighbouring or blocking items, and
   `git log` on the named paths. Recon that finds little is a result.
4. Rewrite the six sections — description, goal, requirements,
   acceptanceCriteria, plan, notes — as far as that evidence reaches.

Rules that outrank the standard:

- Preserve the owner's phrasing VERBATIM inside the description. Clarify
  around their words. Never delete, reword, or tidy what they wrote.
- Never invent an answer. Every point the evidence cannot settle becomes one
  `Q:` line in notes, phrased so a one-line answer closes it; add your
  recommended answer on the same line when recon supports one. An empty
  section beats a fabricated one.
- Every acceptance criterion states its verification method — a command or
  an observation. No method available, no criterion: write a `Q:` line.
- At most 6 requirements and 5 acceptance criteria, one condition each.
  Sending either list REPLACES the whole list and drops its ticks, so send
  every item you keep, in order.
- Never tick, untick or decline a checkbox. Never set delegated, never move
  status, never change the title. Those stay the owner's.

Then write back, in this order, and stop. Every call carries your identity:

    curl -sS -X PATCH {api}/api/tasks/{item} \\
      -H 'X-Work-Agent: {agent}' -H 'Content-Type: application/json' -d @BODY.json

1. Sections: one PATCH whose body carries only the sections you changed
   (`requirements` and `acceptanceCriteria` are lists of strings). Work
   permits this edit ONLY while the `refine` tag is present.
2. Confirm with `work show {item} --json --root {root}` that your text
   landed and both checklists survived.
3. Two log entries — `work log {item} "<text>" --root {root}`:
   - `{tag} refined {item}` — what you filled, then one line per open `Q:`,
     in the house style below.
   - `{tag} fact: refined`
4. The tag, last: one PATCH sending `tags` as the item's current tags minus
   `refine`, with nothing else in the body. The allowance dies with the tag,
   so this call ends the pass. Drop the tag even when the evidence settled
   nothing — say so in the comment instead. A tag left in place dispatches
   this whole pass again.
"""


def refine_mission(*, item: str, root: Path, api: str, agent: str,
                   tag: str) -> str:
    """Fill the refine template for one item (W-0309)."""
    return REFINE_MISSION.format(item=item, root=root, api=api.rstrip("/"),
                                 agent=agent, tag=tag,
                                 standard=GOAL_STANDARD)


def writeback_section() -> str:
    """Load the house style. The path is the source; this is not a copy."""
    return WRITEBACK_STYLE.read_text(encoding="utf-8").rstrip() + "\n"


def _protocol_card() -> str:
    """Return the fixed protocol."""
    return PROTOCOL_CARD


def _section(text: str, heading: str) -> str:
    match = re.search(rf"(?ms)^## {re.escape(heading)}\s*\n(.*?)(?=^## |\Z)", text)
    return match.group(1).strip() if match else ""


def postcompact_context(text: str) -> str:
    """The frozen parts of a run brief that must survive compaction."""
    item = re.search(r"(?m)^(W-[0-9]+)\s*·\s*(.*?)\s*\[[^\n]*\]$", text)
    identity = (f"Item: {item.group(1)}\nTitle: {item.group(2)}" if item else
                _section(text, "Mission").split("\n\n", 1)[0])
    checklist = re.search(
        r"(?ms)^Before you stop, account for every requirement.*?account for it\.",
        text)
    writeback = re.search(r"(?ms)^## Writeback\s*$.*?(?=^## Protocol\s*$)", text)
    protocol = re.search(r"(?ms)^## Protocol\s*$.*\Z", text)
    rules = "\n\n".join(match.group(0).strip() for match in
                          (checklist, writeback, protocol) if match)
    parts = ["# Run brief after compaction", identity]
    acceptance = _section(text, "acceptanceCriteria").split(
        "\n\nBefore you stop", 1)[0]
    for heading, body in (("Goal", _section(text, "goal")),
                          ("Acceptance criteria", acceptance),
                          ("Contract rules", rules)):
        if body:
            parts.append(f"## {heading}\n\n{body}")
    return "\n\n".join(parts)[:POSTCOMPACT_MAX_CHARS]


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
            parts.append(WORK_CHECKLIST_PROTOCOL.format(item=work_item, root=root))
    if extra_context:
        parts.append(f"## Additional context\n\n{extra_context}\n")
    parts.append(writeback_section())
    parts.append(_protocol_card())
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
Never run git write commands; the host checkpoints. End with the usual handoff summary."""
