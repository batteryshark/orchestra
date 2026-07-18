# orchestra

Multi-agent orchestration for heterogeneous coding agents. An interactive orchestrator
(**Claude Code** or **Codex** — both drive it the same way) delegates missions to a roster of
worker agents running on other CLIs/models, with real task tracking, async supervision,
teams, and per-agent inboxes.

```
                 ┌────────────────────────────┐
                 │  ORCHESTRATOR (interactive)│
                 │  Claude Code  or  Codex    │
                 └──────────┬─────────────────┘
                            │  orchestra CLI  +  work CLI
        ┌───────────┬───────┴──────┬─────────────┬──────────────┐
        ▼           ▼              ▼             ▼              ▼
   glm (opencode) minimax     kimi (opencode)  codex CLI    claude -p
   GLM-5.2       MiniMax-M3   Kimi K3          gpt-5.6/5.5  worker
        │
        ▼
   ensemble lead (opencode + opencode-ensemble plugin)
   → spawns its own teammate sessions over a model pool
```

## The two layers

- **[slash-work](https://github.com/batteryshark/slash-work) (`work`)** — durable source of
  truth: tasks (`W-XXXX`), notes, decisions, progress logs. Survives sessions; long projects
  resume cold from the tracker.
- **`orchestra`** (this repo) — the execution layer: agent roster, async dispatch, detached
  run supervision, session resume, teams, inboxes, and a shared findings feed. State lives in
  `.orchestra/` (SQLite) at the project root.

Every dispatched run can be pinned to a work item (`--work W-0003`): dispatch and completion
are logged to the item automatically, and the worker's brief instructs it to `work log`
progress and `VERIFIED:` evidence for acceptance criteria.

## Install

```sh
uv tool install -e /path/to/orchestra     # provides `orchestra` on PATH
```

Roster/config: `~/.config/orchestra/config.toml` (global), `.orchestra/config.toml`
(per-project overlay). Run `orchestra doctor` to check backends, models, and plugins.

## Per-project setup

```sh
cd your-project
orchestra init --work    # .orchestra/ + ORCHESTRA.md + AGENTS.md/CLAUDE.md pointers + work tracker
```

`ORCHESTRA.md` is the orchestrator playbook; `AGENTS.md`/`CLAUDE.md` get a pointer section so
whichever agent opens the project knows it is the orchestrator.

## Core flow

```sh
orchestra dispatch --to glm --work W-0003 --as claude "implement the parser per W-0003"
orchestra dispatch --to glm --to minimax --as claude "same mission, two independent takes"
orchestra dispatch --to ensemble --as claude "big mission — lead spawns a model-pool team"
orchestra wait                     # block until runs finish (run in a background shell)
orchestra inbox claude --unread --mark-read     # HANDOFF messages + completion notices
orchestra feed                     # findings/decisions workers logged along the way
orchestra reply 7 "good — now add tests"        # resume the SAME worker session
orchestra status                   # runs, inboxes, feed, work board snapshot
```

Workers are briefed with a standard coordination protocol: check inbox → log progress to the
work item → post findings to the feed → message peers → end with a `HANDOFF` to the requester.
Supervisors are detached processes: dispatches survive the orchestrator session ending, and
completions land in the requester's inbox (plus the work item log).

## Backends

| roster entry | backend | model | notes |
|---|---|---|---|
| `glm` | opencode | zhipuai-coding-plan/glm-5.2 | `--auto`, JSON event log |
| `minimax` | opencode | minimax-coding-plan/MiniMax-M3 | |
| `kimi` | opencode | kimi-for-coding/k3 | reviewer/second opinion |
| `codex` | codex exec | config default (gpt-5.6) | workspace-write sandbox |
| `codex-55` | codex exec | gpt-5.5 | |
| `claude` | claude -p | default | for when Codex orchestrates |
| `ensemble` | opencode | glm-5.2 lead | opencode-ensemble team over a model pool |

Session refs (opencode `ses_…`, codex thread id, claude session id) are parsed from run logs
so `orchestra reply` can resume any worker's session with follow-up instructions.

## Isolation & skills

`--worktree` runs a worker in a fresh git worktree on branch `orchestra/run-N`. Because
worktrees only carry tracked files, orchestra mirrors the project's skills folders
(`.agents/`, `.claude/`, `.codex/`, `.opencode/`) and agent docs into the worktree so
delegated tools keep their skills. Committing those folders to git makes this automatic
everywhere (including ensemble's own worktrees).

## opencode-ensemble

Installed globally in `~/.config/opencode/opencode.json` (`@hueyexe/opencode-ensemble`),
model pool configured in `~/.config/opencode/ensemble.json`. Dispatching `--to ensemble`
sends an opencode lead that uses `team_*` tools to spawn teammates across the pool
(GLM-5.2 / MiniMax-M3 / Kimi K3), with its own dashboard at `http://localhost:4747`.

## Both-ways orchestration

There is no privileged orchestrator. Claude Code identifies as `--as claude`, Codex as
`--as codex`; completion notices route to whoever dispatched. Codex can dispatch a `claude`
worker and vice versa. Inboxes are just names — a human can read any of them.
