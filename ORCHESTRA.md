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
2. **Dispatch.** `orchestra dispatch --to glm --work W-0003 --as claude "mission text"`
   - Fan out one mission to several agents: repeat `--to` (e.g. `--to glm --to minimax`).
   - Independent missions: separate dispatch calls — they all run concurrently in the background.
   - `--worktree` gives the worker an isolated git worktree (skills folders auto-synced).
   - `--to ensemble` dispatches an opencode-ensemble LEAD that spawns its own model-pool team.
     Ensemble runs ride a persistent opencode host (`orchestra host status`); the mission is
     complete when the lead's HANDOFF arrives, not when the client process exits.
3. **Monitor without blocking.** `orchestra wait` blocks until runs finish (run it in a
   background shell and keep working); `orchestra status` for a snapshot; `orchestra runs --active`.
4. **Harvest.** `orchestra inbox <you> --unread --mark-read` for handoffs and completions;
   `orchestra feed` for findings workers logged; `orchestra logs <run> --pretty` for full output.
5. **Review & iterate.** Follow up in the SAME worker session: `orchestra reply <run> "feedback" `.
   Workers log `VERIFIED: <criterion> — <evidence>` lines instead of flipping checklist boxes
   (Work enforces checked boxes before `review`, and boxes are only togglable via the Work UI/API).
   Verify their evidence, check the boxes in the Work UI (or via `POST /api/tasks/<id>/checklist`
   when this workspace is being served), then `work move W-XXXX review` / `done`.
6. **Close the loop.** Log outcomes to the work item (`work log`), merge worktree branches
   (`orchestra run show <run>` shows branch), and keep the tracker current so any future
   session (yours or another orchestrator's) can resume cold.

## Roster (see `orchestra roster` for live view)

- `glm` — opencode / GLM-5.2 · generalist implementation
- `minimax` — opencode / MiniMax-M3 · generalist implementation
- `codex` — codex CLI / gpt-5.6 default · hard problems
- `codex-55` — codex CLI / gpt-5.5 · medium tasks, faster
- `claude` — claude CLI worker (useful when Codex orchestrates)
- `ensemble` — opencode-ensemble lead; spawns a GLM/MiniMax team internally

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

## Codex-as-orchestrator sandbox note

`orchestra dispatch` spawns other agent CLIs that need network access and write to their own
state dirs (outside the workspace). Interactive Codex: approve the escalation when dispatching.
Headless: `codex exec --sandbox danger-full-access` (or `--dangerously-bypass-approvals-and-sandbox`)
for orchestration sessions. Claude Code needs no special handling.

## Cheatsheet

```
orchestra status                      # snapshot: runs, inboxes, feed
orchestra dispatch --to glm --work W-0001 --as claude "mission"
orchestra dispatch --to glm --to minimax --as claude "same mission, two takes"
orchestra wait                        # block until active runs finish
orchestra inbox claude --unread --mark-read
orchestra reply 7 "looks good; also add tests"
orchestra send glm "heads up: schema changed" --as claude
orchestra broadcast "stop touching db.py" --team core --as claude
orchestra note "auth flow uses PKCE, not implicit" --as claude --tags arch
orchestra feed                        # what everyone has been finding
orchestra logs 7 --pretty             # full worker transcript
orchestra kill 7
```
