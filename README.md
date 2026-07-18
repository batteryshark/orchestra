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
   glm (opencode) minimax        codex CLI       claude -p
   GLM-5.2        MiniMax-M3     gpt-5.6/5.5     worker
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
orchestra ui                       # live web dashboard at http://localhost:4764
orchestra usage                    # cached provider runway + per-agent token burn
```

The dashboard has a top-level **provider runway** link in the header; clicking
it opens `/runway`, a dedicated page that visualises cached coding-plan quota
for MiniMax, Claude, Z.AI, and Codex (multi-bucket, with Codex rate-limit
reset-credit count + expiry and per-window reset countdowns). The dashboard
itself does not render quota cards — they live on `/runway`.

Workers are briefed with a standard coordination protocol: check inbox → log progress to the
work item → post findings to the feed → message peers → end with a `HANDOFF` to the requester.
Supervisors are detached processes: dispatches survive the orchestrator session ending, and
completions land in the requester's inbox (plus the work item log).

## Backends

| roster entry | backend | model | notes |
|---|---|---|---|
| `minimax` | opencode | minimax-coding-plan/MiniMax-M3 | default workhorse ("Sonnet" tier) |
| `glm` | opencode | zhipuai-coding-plan/glm-5.2 | standard tier |
| `glm-max` | opencode | glm-5.2 `--variant max` | heavy reasoning tier |
| `codex-55` | codex exec | gpt-5.5 (effort high) | medium tasks |
| `codex` | codex exec | gpt-5.6-sol (xhigh) | toughest problems only |
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
(GLM-5.2 / MiniMax-M3). Ensemble's own dashboard (:4747) is redundant: `orchestra ui`
reads ensemble's SQLite and the host API directly, showing teams, the team task board,
team messages, and full teammate transcripts in the same pane as everything else.

**Ensemble runs go through a persistent host.** Teammates live inside the lead's opencode
process, so a one-shot `opencode run` would kill the team the moment the lead's turn ends.
Orchestra therefore keeps a long-lived `opencode serve` (managed via `orchestra host
status|start|stop`, state in `~/.local/state/orchestra/`) and dispatches leads with
`--attach`: the team survives client exits, teammate reports wake the lead server-side, and
the supervisor treats the lead's `HANDOFF` message — not process exit — as mission
completion. Killing an ensemble run's client does not stop the server-side team; use
`orchestra reply <run> "team_shutdown and team_cleanup"` or `orchestra host stop`.

## Provider runway

`orchestra usage` and `GET /api/usage` (served by `orchestra ui`) both read
from a single per-process `UsageService` cache. The cache fans out to four
server-side collectors that never expose credentials to the browser:

| Provider | Quota source | Credential discovery |
|---|---|---|
| MiniMax | `GET /v1/token_plan/remains` | `MINIMAX_API_KEY` / OpenCode `minimax-coding-plan` |
| Z.AI | `GET /api/monitor/usage/quota/limit` | `ZAI_API_KEY` / OpenCode `zhipuai-coding-plan` |
| Codex | `account/rateLimits/read` via the installed `codex` CLI | existing Codex login |
| Claude | Claude Code's `/usage` cache (`~/.claude.json`) | existing Claude Code login |

The runway page is served at `/runway` (linked from the dashboard header) and
reads its dashboard, recommendation, and reset-credit card from the
self-same `/api/usage` endpoint. Burn rates sample in memory and only show
after the same reset window has been observed for ≥5 minutes — they vanish
on process restart so we never write account telemetry to disk. Codex
multi-bucket limits (e.g. Codex Spark) are preserved as separate rows, and
when an account has rate-limit reset credits the Codex card shows the count
and earliest known expiry; credit IDs and descriptions are discarded.

When you `orchestra dispatch`, Orchestra also runs a single cached snapshot
to flag targets whose coding-plan headroom is at-or-below 20%. The advisory
prints to stderr before any run row is inserted, never reroutes, never
consumes a Codex reset credit, and fails open if the snapshot is stale or
unavailable (dispatch still proceeds). Configure `quota_warn = false` in
`~/.config/orchestra/config.toml` or `.orchestra/config.toml` to opt out,
or pass `--no-quota-warn` per-dispatch.

## Both-ways orchestration

There is no privileged orchestrator. Claude Code identifies as `--as claude`, Codex as
`--as codex`; completion notices route to whoever dispatched. Codex can dispatch a `claude`
worker and vice versa. Inboxes are just names — a human can read any of them.
