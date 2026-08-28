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
At completion, the refreshed run row is the result contract. Orchestra does not
copy the same state into a second `RunResult` model.

The twelve sections retain the old DESIGN section numbers because source
comments use them as architectural signposts. Their content now describes
current behavior rather than the original build plan.

## 1. Optional Work integration

A project can enter Orchestra's central registry in two ways:

- `orchestra project add .` creates a local project with a generated UUID.
- With Work enabled, the Work adapter caches Work's project list and immutable
  project ids into the same registry. A source mapping takes precedence if the
  source later names a locally adopted directory.

The registry is Orchestra's own and knows no source. A row carries
`source_ref` — the source's OWN identifier for that project, opaque exactly
like `runs.ref` — or NULL when the row was adopted locally. Filling
source-backed rows is adapter work: `project.remember_source` is the one seam
it writes through, and nothing in the core learns a source's entry shape
(CONTRACT §7 Enforcement).

A registered project may be ARCHIVED, which means parked rather than hidden.
A source marks its own projects archived and serves the flag; the adapter's
refresh copies it, so archiving a project there parks it in Orchestra with no
local action. Core code reads `archived` as a plain boolean and never asks who
set it. A locally adopted project, which has no source behind it, is parked
with `orchestra project archive` — Orchestra refuses that for a source-backed
project the same way it refuses `project forget`, because the source owns the
flag. A parked project is off `orchestra project list` (`--all` shows
it, marked) and off the dashboard's project picker, and the three unattended
lanes — the sweep's claim path, the conductor, and refine — skip its items.
The sweep says so once per item, through the waiting queue, so a forgotten
`delegated` tick is not silently dropped. Nothing else changes: manual
`orchestra dispatch` still runs and only prints a notice, a run already in
flight is untouched, and statistics, run listings, `orchestra show`, and every
run the project already owns read exactly as before.

The Work adapter claims delegated items, freezes an item snapshot into the run
brief, records checklist accounting, and writes outcomes back through Work's
agent API. Work is the intent and ledger system in that mode. It is not needed
for local project registration or direct dispatch.

Work sign-off is separate and on by default (W-0299): the sweep that posts a
landed fact dispatches a verification run in the same pass, under `[verify]
profile` — defaulting to the one enabled workhorse-tier profile, a cheaper
model than the worker's. It executes each acceptance criterion's stated
method against landed main and writes the result to Work; `[verify] enabled
= false` turns it off. A criterion with no mechanical method may get a
capped two-seat dialogue under `[verify] second_opinion`; unset, that path is off.

The refine lane sits on the other side of execution (W-0309): a human tags an
item `refine` and the next sweep dispatches one shaping run under `[work]
refine_profile`, whatever the item's status and whether or not it is
delegated — refinement comes before execution, so it waits for neither
signal. The run rewrites the item's six sections to `docs/GOAL-STANDARD.md`
around the owner's own words, leaves every undecidable point as a `Q:` line,
appends `fact: refined`, and drops the tag that asked for it. It claims
nothing, ticks nothing, and lands nothing. The tag is the receipt: a tag
still present with no refine run live dispatches the pass again.

## 2. Daemon and central state

SQLite state, briefs, raw logs, and worktrees live under `~/.orchestra/`.
Configuration lives at `~/.config/orchestra/config.toml`. Projects do not need
a local Orchestra state directory. On POSIX systems, the state root and its
managed containers are owner-only (`0700`) traversal boundaries.

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

The dashboard and iOS app are optional clients of that API. The iOS bundle
identifier and Keychain service keep their shipped string for upgrade and
Keychain compatibility; every name in the project and on the phone is
Orchestra.

Run outcomes leave through a CURSORED READ, not a callback. Every run row
carries `revision`, a monotonic marker the same triggers stamp that bump the
board counter, and `GET /api/runs?since=<revision>` pages the rows past a
caller's cursor. Orchestra holds no subscriber list, no endpoint and no
delivery state, so it cannot learn who consumes it; the consumer's own cursor
is the delivery guarantee, which is why no retry queue exists on this side.
The Work adapter reads it like any other consumer would. A push relay could
subscribe and POST without Orchestra learning anything (CONTRACT §7
Enforcement).

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

Write authority is split by cost. An agent may retune a note or lower an
effort; anything that commits spend — a model, a harness, a new profile, a
raised effort, or the tier/priority a planner routes on — is refused and
RECORDED instead. Config editing knows no record system: `profile_edit`
writes one escalation record (§8's `nod_requests`, carrying the profile, the
keys and their values) and stops. Delivery is a later, retryable read of that
record: a source adapter files the decision the human answers, and
`orchestra profiles` prints the request until then, so the values are
readable from the CLI with every source down.

The durable write comes FIRST, before any delivery is attempted. The earlier
shape filed the decision and kept the request only in its return value, so
every failure path reported that a human was needed while destroying what it
asked for (2026-08-28: an agent's request reached neither Work nor any local
row, and the values were unrecoverable).

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

A transient infrastructure failure gets one automatic retry of the same
brief. A recognized non-refreshable authentication failure does not: Orchestra
escalates it until the operator reauthenticates, then the work must be
dispatched afresh.

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

Landing reports to no source. It stamps `landing_status` and `landing_commit`
on the run row and writes its report into the run's own thread. A source
adapter reads that receipt and posts the fact, because rebasing a branch and
moving a ref must not know a record system exists (CONTRACT §7 Enforcement).

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

On entry, supervisor finalization records the worker's outcome so a crash during
enrichment cannot change success into an inferred failure. It then ingests the
last trace, checkpoints isolated changes, and atomically records the terminal
row, usage, message delivery state, completion notice, and any retry hold. That
commit precedes worktree release. The refreshed row is the result.

Landing, findings, retry handling, dependency release, and Work reporting then
consume it. Landing and handoff completion leave structured receipts on the same
row; the daemon replays any missing consumer after proving the old supervisor
and worker are gone. Work reporting waits for completion, landing, handoff, and
retry policy to settle, so an execution outcome cannot be mistaken for a landed
result or a failure that is already being retried.

## Load-bearing rules

These invariants hold in the current code. Changing one breaks an assumption
another subsystem already depends on.

1. Never downgrade an isolated run to the owner's checkout — a failed worktree
   setup would silently let unattended runs mutate the human's tree; enforced in
   `supervise.py` (`prepare_launch` raises, `rehome` returns the failure).
2. Never merge in the owner's checkout — the merge would collide with the
   uncommitted work a checkout routinely holds; `merge.py` rebases in a
   throwaway worktree and moves the base with `git update-ref`.
3. Never write a Work item's status from a run — status has one writer class,
   the human, and a run that transitions an item overwrites the human's own
   move (CONTRACT 0.8); runs append `fact:` comments through `work_client.py`,
   which offers no status call.
4. Never let the worker report to Work — a worker that files its own findings
   can forget, self-approve, or hand itself work; `findings.py` parses the
   final handoff after the run and files the issues and proposals.
5. Never let a run token act on a sibling run — the shared secret is in every
   worker's environment, so route authority is the only containment; `auth.py`
   holds the whole table, and an unlisted route is the human's.
6. Never store a run token, only its SHA-256 — a stored token outlives the run
   that held it; `auth.py` writes the hash, and the `revoke_run_token` trigger
   in `db.py` nulls it at every terminal status.
7. Never stop a run without recording why — a stop that reached nobody is
   indistinguishable from a crash; `observer.py` writes the observation and
   escalates before it signals the process.
8. Never let a run launch its own children, and never above its own tier — a
   self-launching worker escapes the depth, per-run, and concurrency bounds,
   and an upward hand-off is a decomposition the human never approved;
   `child_runs.py` checks both under one write lock in the parent's supervisor.
9. Never let a runway adapter raise — capacity telemetry is auxiliary, and a
   scraper fault must not block dispatch; every adapter in `runway.py` is
   wrapped by `@soft` and returns `remaining=None` with a reason.
10. Never touch a worktree a live run holds — the run loses the directory its
    process is standing in; `worktree.py` checks `live_holders` before `remove`
    and `prune`.
11. Never build a second result model — the refreshed run row is the result,
    and a parallel object would drift from the row that landing, findings,
    retry, and source reporting all read; `supervise.py` commits the terminal
    row and hands back the row itself.
12. Never treat the normalized event as the record — `events` carries a
    truncated payload, so a viewer or parser that trusts it loses detail; the
    raw log is the source of truth and `traces.py` stores the byte offset and
    length of the line each event came from.
13. Never let the core know a source — only `sweeper`, `conductor`, `verify`,
    `refine` and `findings` may import a source client, and `runs.ref`,
    `projects.source_ref` and `nod_requests.ref` are opaque strings the core
    stores and never parses; the rule is the contract's (§7 Enforcement) and
    `tests/test_boundary.py` asserts it against the import graph.
14. Never attempt delivery before the durable write — a record that exists
    only in a return value dies with the first unreachable server, and the
    caller is told a human is needed while what they asked for is destroyed;
    `profile_edit` writes its escalation row first and an adapter files it
    later.

## Non-goals

- Rebuilding the legacy Operator, immutable contracts, councils, rosters,
  staffing teams, capacity budgets, or a general event-replay engine.
- Making harness-native tools look identical.
- A dynamic harness plug-in system before another real adapter needs it.
- Process-level containment or distributed multi-machine scheduling.
- Making Work mandatory for direct execution.
- Linux service-manager integration in the current release.
