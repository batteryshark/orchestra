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

## 1. External automation and the registry

Orchestra hosts NO source integration. The Work automation — the sweep, the
conductor, the verify lane, the findings filer, the router — lives in the
sibling **work-bridge** project: a consumer that knows both sides so that
neither knows the other. The bridge drives Orchestra through its library
and API, keeps its own tables in its own ATTACHed database file, and runs
as its own process (`workbridge sweep --watch`). Any other source (Linear,
a cron script) integrates the same way: dispatch with a path, a brief, an
opaque `ref` and `requester`; read the cursored results feed and the
receipts.

Orchestra's registry holds project IDENTITIES and nothing else (schema v29):
one row per project — slug, name, provenance, parked flags. NO PATHS.
Orchestra is a runner: the checkout a run works in is the caller's to supply
at dispatch, and "where does this project usually run" is answered by the
run history (`runs.repo`), not by stored configuration. A project enters the
registry two ways:

- `orchestra project add <name>` mints an owner-local identity (a directory
  argument contributes only its name).
- An external caller (the work-bridge) caches its source's project
  identities through the one core seam, `project.remember_identity`. Cached
  identities the source stops naming are pruned unless runs or an owner's
  override hold them; owner-minted rows are never a caller's to delete.

The label-to-folder map is the BRIDGE's own `checkouts` table, in the
bridge's own database file. An unattended dispatch is the bridge resolving
its own labels; nothing in this repository reads that table.

Every project carries a SLUG (v27): lowercase kebab-case, minted once from
the name, unique, never rewritten by a refresh or rename. The slug is the
HUMAN address: `dispatch --project <slug>` targets a project from anywhere,
`POST /api/dispatch` addresses it the same way, and the project's own state
directory is keyed by it, so troubleshooting starts from a name instead of
a UUID. `project_id` stays the machine key everything joins on. Identity
minting reaches every surface: the CLI, `POST /api/projects/add`, and the
dashboard's projects panel.

A registered project may be ARCHIVED, which means parked rather than hidden.
The source's flag is the DEFAULT, not the owner: a source marks its own
projects archived and serves the flag, the adapter's refresh copies it into
`projects.archived`, so archiving a project there parks it in Orchestra with
no local action. The owner's own answer sits above it in
`projects.archived_override` — NULL follows the source, 0 or 1 is a decision
made here and wins. Effective archived is
`COALESCE(archived_override, archived, 0)`, and core code reads that derived
boolean and never asks who set it. `orchestra project archive` parks ANY
project, source-backed or not, because archiving means "hide this from
Orchestra and stop dispatching for it" — Orchestra's own decision about its
own surface, which no refresh may overwrite. `project forget` still refuses a
source-cached identity, since the next refresh would put the row back and the
removal would look broken. A parked project is off `orchestra project list` (`--all` shows
it, marked) and off the dashboard's project picker, and the bridge's
unattended lanes skip its items. Nothing else changes: manual
`orchestra dispatch` still runs and only prints a notice, a run already in
flight is untouched, and statistics, run listings, `orchestra show`, and every
run the project already owns read exactly as before.

What the bridge does with Work — claiming, ferrying, reporting, sign-off,
the conductor's planner turns — is the bridge's own documentation, tested
in its own repository. Orchestra needs none of it for registration or
direct dispatch.

## 2. Daemon and central state

SQLite state, briefs, raw logs, and worktrees live under `~/.orchestra/`.
Configuration lives at `~/.config/orchestra/config.toml`. Projects do not need
a local Orchestra state directory. On POSIX systems, the state root and its
managed containers are owner-only (`0700`) traversal boundaries.

Per-project state lives under one slug-keyed area,
`~/.orchestra/projects/<slug>/`. A worker run's own artifacts — `brief.md`
and the raw `log.jsonl`, with room for future outputs beside them — file at
`runs/run-<seq>/`, named by the PROJECT's run number because that is the
number humans quote from the board; `worktrees/run-<id>` holds isolated
checkouts (named by the row id, which is also the branch number), and
`workspace` serves a project with no folder of its own. A row with no
project number — a control turn, a pre-v27 run — keeps the flat `briefs/`
and `logs/` layout keyed by the globally unique row id, and readers never
derive either path: the run row's `brief_path`/`log_path` are the record.
Pre-v27 directories (`worktrees/<id>`, `workspaces/<id>`) are still read
and pruned where they already exist; nothing is moved under a live run.

Raw logs are kept forever by default (`raw_log_retention_days = 0`): they
are the full-detail record of every run's input and output, held for later
analysis. Pruning is opt-in — a positive day count, plus a human running
`orchestra traces prune`.

A direct run follows this path:

```text
human or program                         work-bridge (optional)
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
          optional landing; receipts for any consumer
```

Manual CLI dispatch, the bridge's sweeping and conducting, retries, and
resolver runs create run rows through several paths. They converge on `prepare_launch`
and the same supervisor. W-0293 tracks the missing single
`RunRequest -> Run` admission seam.

Rehome is required before a continuation or retry whose original worktree has
been released. This keeps a resumed harness session out of a stale path.

## 3. HTTP and clients

The daemon serves a versioned snapshot API, trace streams, action routes, and
the static dashboard on one port. Reads and actions require either the human
secret or a scoped per-run token. A run token can read shared state but can act
only on its own run; termination revokes it. `POST /api/projects/add`
mints a project identity and `POST /api/dispatch` starts a run against a
named project — both human-key only, the same admission path the CLI uses,
so a client that is not a terminal can create a project and put work into it.

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
checks still apply on a tailnet — but the shared key does not have to: a
peer `tailscale whois` resolves to a login is treated as the human, on by
default, because the tailnet is the boundary and the phone should not carry
a key. Loopback and this machine's OWN tailscale address never count
(workers run on this machine — the same hole `trust_local` documents), and
`[http] tailnet_logins` narrows trust to listed logins. `tailnet_auth =
false` restores key-only access.

## 4. Dispatch and isolation

Manual dispatch uses an isolated worktree by default. `--shared` is an explicit
choice for read-only work or another case where the caller accepts use of the
registered checkout. Concurrent mutation is safe only when each run has an
isolated checkout.

A project is not one checkout, and the core stores no checkout at all
(schema v29). The caller supplies the repository: `dispatch --path` (and
`path` on `POST /api/dispatch`) names it outright; a bare dispatch uses the
current directory; a dispatch that only NAMES a project (`--project`) uses
the checkout the project last ran in — the run history, not a setting —
else the adapter's map, else it asks for `--path` once. The run row records
its checkout as `runs.repo` (v28, backfilled at v29), and `project.root_for`
reads it, so landing, retries, and diffs return to the checkout the run
actually came from. With no `--project`, the checkout also names the
project: the run history first, then the adapter's map. The worktree and
artifacts still file under `~/.orchestra/projects/<slug>/`, and a
caller-named path inside `~/.orchestra` itself is refused (the workspace
excepted) — a worktree of the run database is never what anyone meant.

An isolated run uses a branch named `orchestra/run-N`. The bridge's
unattended dispatches request isolation by default too. If worktree setup or rehoming
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
durable shared view. Raw logs preserve harness-specific detail and are kept
forever by default; pruning terminal runs' logs is opt-in (§2). Normalization
does not make harness-native tools identical.

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

Landing is a POLICY, and `[merge] enabled = false` turns the automatic path
off: the run then ends at its branch and a `landing_status` of `skipped`,
kept for whatever lands it — a human, or an external agentic lander that
reads the receipts and posts its own facts. Dependents still release on a
skipped landing, the adapter's report posts the result comment but no
lifecycle fact, and no checklist criterion is declined on the run's behalf.
An explicit retry (`retry_landing`) still lands: the switch gates the
automatic path, never the owner's own hand.

Every successful run's final handoff is parsed. Findings and proposals are filed
only when Work is configured and the run has Work context. They have no local
durable collection, so the dashboard and iOS client do not present one.

## 10. The conductor (moved out)

The conductor lives in the work-bridge project with the rest of the Work
automation. Its planner turns still record as `layer` rows in `runs`
(through the library), so the dashboard shows them like any control turn —
but no conductor code exists in this repository.

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
   move (CONTRACT 0.8); the bridge's client offers no status call.
4. Never let the worker report to a source — a worker that files its own
   findings can forget, self-approve, or hand itself work; `handoff.py`
   parses and enforces the protocol after the run, and the bridge files the
   entries from the stamped receipt.
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
13. Never let the core know a source — the adapter LEFT the repository
    (the work-bridge project), so the rule is absolute: no module imports a
    source client, none is named for one, none reads a `[work]` table, and
    `runs.ref` / `nod_requests.ref` are opaque strings the core stores and
    never parses; `tests/test_boundary.py` asserts all of it.
14. Never attempt delivery before the durable write — a record that exists
    only in a return value dies with the first unreachable server, and the
    caller is told a human is needed while what they asked for is destroyed;
    `profile_edit` writes its escalation row first and the bridge files it
    later.

## Non-goals

- Rebuilding the legacy Operator, immutable contracts, councils, rosters,
  staffing teams, capacity budgets, or a general event-replay engine.
- Making harness-native tools look identical.
- A dynamic harness plug-in system before another real adapter needs it.
- Process-level containment or distributed multi-machine scheduling.
- Making Work mandatory for direct execution.
- Linux service-manager integration in the current release.
