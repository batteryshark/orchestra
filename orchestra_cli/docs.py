"""Project doc templates written by `orchestra init`."""

ORCHESTRA_MD = """\
# ORCHESTRA — multi-agent orchestration playbook

You (the agent reading this in an interactive session — Claude Code or Codex) are the
**orchestrator** for this project. You delegate implementation work to a roster of worker
agents and coordinate them through two CLIs that are both on PATH:

- **`work`** — the slash-work project tracker. THE durable source of truth: tasks (W-XXXX),
  notes, decisions, ideas, progress logs. Everything that must survive this session goes here.
  Bootstrap: `work agent operations`, then `work agent instructions <operation>`.
- **`orchestra`** — the execution layer: agent roster, async dispatch, run supervision,
  teams, per-agent inboxes, findings feed.

Your identity: use `--as claude` (Claude Code) or `--as codex` (Codex) on orchestra
commands. Run completions and worker handoffs arrive in YOUR inbox under that name.

## The orchestration loop

1. **Plan in the tracker.** Break the goal into work items:
   `work task "title" --type feature --priority high --goal "..." --requirement "..." --acceptance "..."`
   Record decisions worth surfacing to the human: `work decision "question" --option A --option B --recommend A`.
2. **Dispatch.** First `orchestra roster` — the roster is LIVE config that may have changed
   since you last looked (models added/removed/re-tiered); route against what it says NOW.
   Check `orchestra usage` when planning heavy work (codex plan quota, per-agent token burn).
   Then `orchestra dispatch --to glm --work W-0003 --as claude "mission text"`
   - Fan out one mission to several agents: repeat `--to` (e.g. `--to glm --to minimax`).
   - Independent missions: separate dispatch calls — they all run concurrently in the background.
   - `--worktree` gives the worker an isolated git worktree (skills folders auto-synced).
   - `--to ensemble` dispatches an opencode-ensemble LEAD that spawns its own model-pool team.
     Ensemble runs ride a persistent opencode host (`orchestra host status`); the mission is
     complete when the lead's HANDOFF arrives, not when the client process exits.
3. **Messaging semantics — know which tool delivers.**
   - `orchestra send <agent>` to a RUNNING worker is BEST-EFFORT (workers only check
     their inbox at start and between steps). If the worker never checks, the run's
     end bounces an UNDELIVERED notice back to your inbox.
   - URGENT (changes what the worker is doing right now): `orchestra interrupt <run> "msg"`
     — pauses the worker, injects the message, resumes the same session and mission.
   - NOT urgent (fine to land after current work): `orchestra queue <run> "msg"` — auto-
     delivered as a session-resume follow-up the moment the run completes.
   - After a run finished: `orchestra reply <run> "msg"` resumes the session manually.
   - Corrections to in-flight missions must use interrupt or queue — never bare send.
4. **Monitor without blocking.** `orchestra wait` blocks until runs finish (run it in a
   background shell and keep working); `orchestra status` for a snapshot; `orchestra runs --active`.
5. **Harvest.** `orchestra inbox <you> --unread --mark-read` for handoffs and completions;
   `orchestra feed` for findings workers logged; `orchestra logs <run> --pretty` for full output.
6. **Review & iterate.** Follow up in the SAME worker session: `orchestra reply <run> "feedback" `.
   Workers log `VERIFIED: <criterion> — <evidence>` lines instead of flipping checklist boxes
   (Work enforces checked boxes before `review`, and boxes are only togglable via the Work UI/API).
   Verify their evidence, check the boxes in the Work UI (or via `POST /api/tasks/<id>/checklist`
   when this workspace is being served), then `work move W-XXXX review` / `done`.
7. **Close the loop.** Log outcomes to the work item (`work log`), merge worktree branches
   (`orchestra run show <run>` shows branch), and keep the tracker current so any future
   session (yours or another orchestrator's) can resume cold.

## Roster & routing (see `orchestra roster` for live view)

Route by difficulty — don't burn the heavy tiers on grunt work:

- `minimax` — MiniMax-M3 · THE DEFAULT. Routine implementation, ports, mechanical
  refactors, test writing (the "Sonnet" tier).
- `glm` — GLM-5.2 · standard tier for normal feature work needing more judgment.
- `codex-55` — gpt-5.5 (high) · fast solid engineer for medium tasks.
- `glm-max` — GLM-5.2 variant=max · heavy reasoning: hard design, gnarly debugging.
- `codex` — gpt-5.6 (xhigh) · REALLY tough thinking only; slow and expensive — use sparingly.
- `claude` — claude CLI worker (useful when Codex orchestrates).
- `ensemble` — opencode-ensemble lead; spawns a GLM/MiniMax team internally for
  parallelizable multi-part missions.

Good pattern: minimax implements → glm reviews; or glm-max/codex design → minimax executes.

## Rules of engagement

- Never do large implementation work inline while workers idle — delegate, then verify.
- One work item per dispatched mission whenever possible (`--work W-XXXX`) so progress
  logs land on the right card automatically.
- Workers were briefed to end with a `HANDOFF` message to you and move items to `review`;
  if a run completes without one, read `orchestra logs <run> --pretty` and treat the
  result as unverified.
- Verify worker output before marking anything done. Prefer dispatching a second agent to
  review large changes (e.g. `--to minimax "review the diff on branch orchestra/run-N ..."`).
- Record every notable finding or decision in `work` — sessions are disposable, the tracker is not.

## Supervisor recovery / upgrade

Supervisors are detached parents of their worker process — they can't be hot-swapped.
If a run's supervisor is gone (machine reboot, crash) or predates a code upgrade you need:
`orchestra kill <run>` then `orchestra reply <run> "continue where you left off"` — the
reply resumes the same worker session under a freshly spawned supervisor; no work is lost.
Never run `orchestra _supervise` against a live run (it would spawn a duplicate worker).

## Codex-as-orchestrator sandbox note

`orchestra dispatch` spawns other agent CLIs that need network access and write to their own
state dirs (outside the workspace). Interactive Codex: approve the escalation when dispatching.
Headless: `codex exec --sandbox danger-full-access` (or `--dangerously-bypass-approvals-and-sandbox`)
for orchestration sessions. Claude Code needs no special handling.

## Cheatsheet

```
orchestra ui                          # shared read-only dashboard (:4764; project picker)
orchestra ui --tailscale              # bind only to this machine's Tailnet IPv4
orchestra project list                # roots registered with the shared dashboard
orchestra project register /path      # add a root while the dashboard is running
orchestra host start --port 4763      # persistent opencode host for ensemble runs (--port configurable)
orchestra status                      # snapshot: runs, inboxes, feed
orchestra dispatch --to glm --work W-0001 --as claude "mission"
orchestra dispatch --to glm --to minimax --as claude "same mission, two takes"
orchestra wait                        # block until active runs finish
orchestra inbox claude --unread --mark-read
orchestra reply 7 "looks good; also add tests"
orchestra interrupt 7 "stop - the schema changed, read W-0012 first" --as claude
orchestra queue 7 "when done: also update the README section" --as claude
orchestra send glm "heads up: schema changed" --as claude
orchestra broadcast "stop touching db.py" --team core --as claude
orchestra note "auth flow uses PKCE, not implicit" --as claude --tags arch
orchestra feed                        # what everyone has been finding
orchestra logs 7 --pretty             # full worker transcript
orchestra kill 7
```
"""

POINTER = """
<!-- orchestra -->
## Multi-agent orchestration

This project is orchestrated with the `orchestra` CLI + the `work` tracker.
If you are running interactively here, you are the ORCHESTRATOR: read `ORCHESTRA.md`
before doing substantial work, track state in `work`, and delegate to worker agents
with `orchestra dispatch`.
"""

PROJECT_CONFIG_STUB = """\
# Project-level orchestra overrides (merged over ~/.config/orchestra/config.toml).
# [agents.myagent]
# backend = "opencode"
# model = "provider/model"
# role = "..."

[settings]
# timeout = 3600
"""

STATE_GITIGNORE = """\
logs/
worktrees/
briefs/
*.db
*.db-shm
*.db-wal
"""
