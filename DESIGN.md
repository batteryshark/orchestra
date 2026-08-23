# Orchestra: current architecture

This document describes the current code. It separates the execution core from
optional policy and calls out incomplete paths.

## Mission

Orchestra is a local execution plane for agent work. It accepts a mission from
a person, an agent, or Work, starts the selected harness, and keeps a durable
record of its lifecycle, trace, controls, and outcome.

Codex, Claude Code, OpenCode, and Reasonix are the supported harnesses today.
Orchestra normalizes how their runs are launched, observed, controlled, and
reported. Harness-native tools, permissions, configuration, and model catalogs
remain harness-specific.

Work automation, git landing, routing, conducting, human escalation, provider
runway, and the iOS client are policies or integrations. Direct dispatch does
not require them.

## Product boundary

Orchestra owns execution after it receives a mission. The caller owns intent. A
Work-backed conductor may decompose a human-delegated goal, but that is an
optional policy rather than the definition of a run.

Each run has a durable numeric id. A profile selects its harness, model, effort,
and launch settings. As work proceeds, the run record accumulates raw output,
normalized events, messages, usage when available, and a terminal summary.
These remain separate records; there is no single `RunResult` type.

The twelve sections retain the old DESIGN section numbers because source
comments use them as architectural signposts. Their content now describes
current behavior rather than the original build plan.

## 1. Optional Work integration

A project can enter Orchestra's central registry in two ways:

- `orchestra project add .` creates a local project with a generated UUID.
- With Work enabled, Orchestra caches Work's project list and immutable project
  ids. A Work mapping takes precedence if Work later names a locally adopted
  directory.

The Work adapter claims delegated items, freezes an item snapshot into the run
brief, records checklist accounting, and writes outcomes back through Work's
agent API. Work is the intent and ledger system in that mode. It is not needed
for local project registration or direct dispatch.

Work sign-off is separate and disabled by default. When enabled, it records a
verification run under `verify_profile`, executes each acceptance criterion's
stated method against landed main, and writes the result to Work.

## 2. Daemon and central state

SQLite state, briefs, raw logs, and worktrees live under `~/.orchestra/`.
Configuration lives at `~/.config/orchestra/config.toml`. Projects do not need
a local Orchestra state directory.

A direct run follows this path:

```text
human or program                         Work adapter (optional)
       \                                      /
                    mission + profile
                            |
                    durable run row
                            |
             prepare brief, log, and optional worktree
                            |
                  detached supervisor
                            |
          Codex | Claude | OpenCode | Reasonix
                            |
             raw log + normalized trace + controls
                            |
                 terminal status and summary
                            |
          optional landing and Work/Nod writeback
```

Manual CLI dispatch, Work sweeping, conductor dispatch, retries, and resolver
runs create run rows through several paths. They converge on `prepare_launch`
and the same supervisor. W-0293 tracks the missing single
`RunRequest -> Run` admission seam.

Rehome is required before a continuation or retry whose original worktree has
been released. This keeps a resumed harness session out of a stale path.

## 3. HTTP and clients

The daemon serves a versioned snapshot API, trace streams, action routes, and
the static dashboard on one port. Reads and actions require either the human
secret or a scoped per-run token. A run token can read shared state but can act
only on its own run; termination revokes it.

The dashboard and iOS app are optional clients of that API. The iOS project and
bundle identifiers retain the Dromond name for upgrade and Keychain
compatibility. User-facing copy and protocol names use Orchestra.

The HTTP server can bind to loopback or a discovered Tailscale address. Host
checks and the shared key still apply on a tailnet.

## 4. Dispatch and isolation

Manual dispatch uses an isolated worktree by default. `--shared` is an explicit
choice for read-only work or another case where the caller accepts use of the
registered checkout. Concurrent mutation is safe only when each run has an
isolated checkout.

An isolated run uses a branch named `orchestra/run-N`. The Work sweeper and
conductor also request isolation by default. If worktree setup or rehoming
fails, launch fails closed; unattended execution never changes to the owner's
checkout. A project's `[work] worktree = false` is the explicit shared mode.

Orchestra has no global, per-project, or per-profile concurrency cap. The
visible live count and pause switch are the admission controls. Pause stops
manual and policy-driven admission, including continuations and ready dependency
launches. Live work, automatic landing, completion reporting, failed dependency
settlement, completion-only Nod actions, runway and project refresh, Work message
ferrying, and health maintenance continue. Ordinary conductor events,
infrastructure retries, resolver answers, and completion judgments are retained
for resume. A paused judgment does not spend its planner turn.

## 5. Profiles and supported harnesses

Profiles are global launch templates, not durable worker identities. A profile
names a harness, model, effort, priority, tier, and launch settings. A project
may restrict which global profiles it can use.

Orchestra does not expose child-run settings or delegation guidance. There is
no child launcher; a real parent/child lifecycle should be added only with an
end-to-end use case.

One static capability table records discovery, launch, resume, traces,
correction, usage, additional-directory support, and transport for the four
supported harnesses. A completeness test checks the concrete integration
surfaces against that table. It is product data, not a plug-in framework.

There is no team, roster, council, squad, or other grouping layer.

## 6. Execution and messaging

`prepare_launch` freezes the brief and Work snapshot, creates the raw log, and
optionally prepares an isolated worktree. The detached supervisor mints a
per-run control token, builds the harness command, starts it, and records its
terminal status and handoff.

Queued `tell` messages reach an exec harness at a safe turn boundary. Immediate
stop, resume, and session continuation use each harness's supported mechanism.
The optional ACP transport provides a persistent protocol session for configured
OpenCode or Reasonix profiles; ordinary process execution is the default.

OpenCode native subagents are denied by default because Orchestra cannot observe
or control them. `opencode_native_subagents = true` restores that
harness-native behavior. Orchestra has no replacement child launcher yet.

## 7. Traces and supervision

Parsers turn raw harness output into common trace events for the CLI, dashboard,
iOS client, messages, and usage accounting. Normalized events provide the
durable shared view. Raw logs preserve harness-specific detail while retained
and may be pruned for terminal runs. Normalization does not make harness-native
tools identical.

The supervisor enforces configurable hard and stall timeouts and runs
mechanical loop checks. A manual `check` can add an out-of-band model judgment
when an observer profile is available. Neither replaces deterministic
timeouts.

## 8. Optional human loop

Nod can deliver blocking decisions and alerts outside the worker session. It is
disabled by default. Run summaries remain local, and Work-backed runs also keep
their comments in Work.

The CLI retains direct `ask`, `show`, and `cancel` operations for this
adapter. Nod is not part of the minimum execution path.

## 9. Completion and landing

Completion records the final handoff and usage, checkpoints isolated changes,
and releases the worktree. A completed shared-checkout run has no Orchestra
branch to land.

Landing operates in a scratch worktree. It rebases onto the base, runs declared
project checks, evaluates mechanical tripwires, and updates the base ref with
compare-and-swap. The owner's checkout is not used as the merge worktree.

Landing has no acceptance-criteria review stage. The enforceable policy is
declared checks plus mechanical tripwires, with an optional mission-alignment
judgment for tripped limits. Output identifies the checks and tripwires that
actually ran.

Every successful run's final handoff is parsed. Findings and proposals are filed
only when Work is configured and the run has Work context. They have no local
durable collection, so the dashboard and iOS client do not present one.

## 10. Optional Work conductor

The conductor takes episodic planning turns inside a human-delegated Work goal.
It can dispatch existing tasks, propose children, ask the human, wait, or finish
the goal. Direct dispatch does not use it.

This policy is deliberately smaller than the retired Operator. It does not
introduce immutable contracts, councils, rosters, capacity budgets, or a
persistent orchestrator identity.

## 11. Statistics and runway

Run rows retain elapsed worker time, harness usage, token counts, and cost when
the harness reports them. `stats` and the clients aggregate those records.

Runway polls provider capacity through provider-specific mechanisms and can feed
the optional router. It is auxiliary telemetry, not an execution gate.

## 12. Run environment and platform support

Each harness keeps its native tools and sandbox. Declared `add_dirs` request
additional-directory access through harness-native flags; the resulting
permissions vary by harness. The run brief reserves git writes for Orchestra.
Process-level containment is not implemented.

`orchestra service` installs a LaunchAgent on macOS or a per-user Scheduled
Task on Windows. There is no service installer for other platforms. The
repository contains zero third-party Python runtime dependencies.

## Result and policy boundary

Supervisor finalization records the outcome and may checkpoint changes, release
a worktree, land a branch, report to Work or Nod, file findings, retry an
infrastructure failure, or release dependencies. Optional policy ownership is
currently tangled with the execution result.

W-0295 tracks separating this boundary: supervision records a durable outcome,
then landing and external adapters consume it.

## Non-goals

- Rebuilding the legacy Operator, immutable contracts, councils, rosters,
  staffing teams, capacity budgets, or replay machinery.
- Making harness-native tools look identical.
- A dynamic harness plug-in system before another real adapter needs it.
- Process-level containment or distributed multi-machine scheduling.
- Making Work mandatory for direct execution.
- Linux service-manager integration in the current release.
