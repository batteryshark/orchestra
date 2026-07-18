"""Compose the worker brief injected into every dispatched agent."""
import shutil
import subprocess
from pathlib import Path


def work_snapshot(root: Path, item: str) -> str:
    if not shutil.which("work"):
        return ""
    try:
        out = subprocess.run(["work", "show", item], cwd=root, capture_output=True,
                             text=True, timeout=20).stdout.strip()
        return out[:6000]
    except Exception:
        return ""


def compose(*, root: Path, run_id: int, agent: dict, mission: str,
            work_item: str | None, team: str | None, requester: str,
            workdir: str, extra_context: str | None = None) -> str:
    name = agent["name"]
    parts = [f"""# Orchestra worker brief — run {run_id}

You are **{name}** ({agent.get('role', 'worker agent')}), a worker agent in the project at `{root}`.
Team: {team or '(none)'} · Dispatched by: **{requester}**

Work autonomously. Do not ask questions or wait for replies — make reasonable choices and record them. Your working directory is `{workdir}`; keep all file changes inside it.

## Mission

{mission}
"""]
    if work_item:
        snap = work_snapshot(root, work_item)
        parts.append(f"""## Tracked work item: {work_item}

This mission is tracked in the slash-work project tracker (the durable source of truth).
Run `work show {work_item}` for full context and `work agent operations` if you need the protocol.
""" + (f"Snapshot at dispatch time:\n\n```\n{snap}\n```\n" if snap else ""))
    if extra_context:
        parts.append(f"## Additional context\n\n{extra_context}\n")
    parts.append(f"""## Coordination protocol (required)

Coordination runs through the `orchestra` CLI and the `work` CLI, both on PATH. Identify yourself with `--as {name}` on orchestra commands (ORCHESTRA_SELF is also exported for you).

1. **Start** — read pending messages: `orchestra inbox {name} --unread --mark-read`
2. **Progress** — {'append to the work item log after each meaningful step: `work log ' + work_item + ' "<what happened>"`' if work_item else 'record meaningful progress with `orchestra note "<progress>" --as ' + name + '`'}
3. **Findings** — anything teammates or the orchestrator should know (discoveries, gotchas, decisions made, dead ends): `orchestra note "<finding>" --as {name} --tags <tag,...>`
4. **Peers** — message a teammate: `orchestra send <agent> "<msg>" --as {name}`; see who exists: `orchestra roster`. Check your inbox between major steps.
5. **Blockers** — `orchestra send {requester} "<question/blocker>" --as {name}`, then continue with a documented assumption rather than blocking.
6. **Finish (mandatory)** — send a handoff before you stop:
   `orchestra send {requester} "HANDOFF run {run_id}: <what you did, files touched, what remains / follow-ups>" --as {name} --run {run_id}`
""" + (f"""   Then update the tracker: log verification evidence for EACH requirement/acceptance criterion you satisfied:
   `work log {work_item} "VERIFIED: <criterion> — <evidence>"`
   Then try `work move {work_item} review --note "<summary>"`. If Work refuses because checklist boxes are
   unchecked (they are only togglable via the Work UI/API), that is fine — leave the status as-is; your
   VERIFIED log lines are what the reviewer needs. Never move an item to done.
""" if work_item else "") + """
## Skills

Before starting, check the project's skill folders for relevant guides and follow any that apply:
`.agents/skills/`, `.claude/skills/`, `.opencode/skill/`, `.codex/skills/` (each skill is a folder with a SKILL.md).
""")
    if agent.get("ensemble"):
        pool = agent.get("model_pool", [])
        parts.append(f"""## Ensemble lead instructions

You have opencode-ensemble team tools (team_create, team_spawn, team_message, team_broadcast, team_tasks_add, team_tasks_list, team_claim, team_tasks_complete, team_results, team_merge, team_status, team_shutdown, team_cleanup).
You are the LEAD: split the mission into parallel tasks on the team task board, spawn teammates over this model pool: {', '.join(pool) or '(configured pool)'}, coordinate via team messages, merge results with team_merge, then team_shutdown and team_cleanup. Report the consolidated outcome through the normal handoff protocol above.

IMPORTANT — lifecycle: your session runs on a persistent orchestra host, so your team survives even if your turn ends; teammate reports will wake you in a new turn. Still, PREFER finishing in one turn: after spawning, poll team_status/team_results until every task is complete, then team_merge, verify the merged files exist in the project, team_shutdown, team_cleanup, and only then hand off. If you are woken by a teammate message instead, continue from where the team actually is (check team_status first). The mission is complete ONLY when you send the HANDOFF message — orchestra detects completion by it.
""")
    return "\n".join(parts)
