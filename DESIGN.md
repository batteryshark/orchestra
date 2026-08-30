# Orchestra v2 architecture

This is the hard-reset contract. It replaces every earlier Orchestra schema,
API, callback, client, and workflow assumption.

## 1. Product boundary

Orchestra is a standalone, single-host agent fleet runner and control plane. It
accepts neutral executable requests, schedules configured local execution capacity,
supervises agent runtimes, supports bounded delegation and operator control,
and retains complete execution evidence.

It deliberately does not model work. There are no tickets, task states,
claims, leases, handoffs, acceptance gates, source-system caches, writeback
queues, verification policy, review roles, or landing decisions. It is useful
for code, research, mail, documents, browsers, operations, and other requests
without any Workbridge or Slash Work installation.

The invariants are:

1. One explicit `RunRequest` admits every execution.
2. A run is the only scheduled execution object.
3. One daemon is the normal writer and SQLite is durable truth.
4. Grouping never changes execution policy.
5. A frozen working directory controls where execution occurs; profile controls how it occurs.
6. Routing and result acceptance belong to callers, never Orchestra.
7. Every mutation, wait, delivery failure, and terminal outcome is auditable.
8. Raw evidence is retained even if derived views fail.
9. Prefer SQLite, the standard library, process protocols, and native clients
   over framework, plugin, broker, and distributed-system layers.

## 2. One host, many clients

An Orchestra instance is one machine with:

- one v2 SQLite database and generated `instance_id`;
- one authoritative daemon, scheduler, and HTTP API;
- configured groups, runtimes, profiles, runway sources, and credentials;
- local workspaces, worktrees, logs, artifacts, and checkpoints;
- browser, CLI, iPhone, iPad, and native macOS clients.

“Fleet” means the configured harness/profile capacity on that host. Orchestra
has no node registry, leader election, membership protocol, remote worker
lease, distributed lock, cross-node database, or aggregate scheduler. A run
can operate another machine through tools such as SSH, but that does not make
the remote machine an Orchestra node.

Clients may save several independent endpoints and switch between them. They
must never imply that Orchestra balances or coordinates those instances.

## 3. Groups and numbering

A **Group** organizes runs. It has a stable id, unique name and slug, archive
state, creation metadata, atomic next-number counter, and an optional
write-only default working directory for future runs.
`General` is created at initialization and is the default.

Every admitted root, child, retry, or explicit continuation receives:

- a globally unique integer run id on the instance; and
- the next immutable sequence number within its group.

The human label is `Group Name #N`. Group assignment cannot change after
admission. Resuming the same suspended run and running an Observer check do not
consume numbers. Renaming a group changes the current display prefix but not
run identity. Archived groups remain filterable and retain their history.

Groups do not contain a default profile, runtime, runway source,
priority, routing rule, or concurrency policy.

## 4. Working directories and repository isolation

A root run may provide an explicit `cwd`. Otherwise Orchestra uses its group's
optional default; if neither exists it creates a persistent managed group
workspace. The daemon expands and canonicalizes the selected host path,
requires an existing directory, and freezes the result at admission. Children,
retries, and continuations inherit that frozen directory. Later group edits
affect only future root runs.

Paths are write-only configuration. Public projections expose only whether a
group has a default and whether a run selected its directory from the run,
group, managed fallback, or lineage inheritance. When the frozen directory is
inside a Git repository, Orchestra automatically creates a per-run worktree
at the repository root and runs in the corresponding relative subdirectory.
Repository detection and isolation are internal execution behavior, not a
caller-selected policy axis.

## 5. Runtimes and profiles

A **Runtime** describes a process/session protocol. Codex, Claude Code,
OpenCode, and Reasonix are first-class built-ins with normalized launch,
session, steering, interrupt, event, usage, and completion behavior. A
configurable argv/ACP runtime supports another harness without loading Python
plugins or granting an in-process extension surface.

V2 launches one adapter process or session per run. The runtime boundary keeps
a future proven PydanticAI or pi-agent implementation possible without
pretending that a resident worker pool exists today.

Observer is a stricter use of that boundary. Its process receives a minimal
OS/bootstrap environment, a bounded evidence prompt, no workspace, no run or
operator credential, and no delegation surface. A profile is Observer-capable
only when its adapter has an enforceable tool-free launch: Claude Code,
OpenCode, and Reasonix today. Codex, generic argv, and ACP are rejected for
Observer use until they can prove the same property.

A **Profile** is a managed launch configuration that references one runtime.
It includes model, effort, environment and sandbox policy, timeouts,
capabilities, tier, display priority, optional active cap, optional runway
source, and enabled state. A run snapshots the non-secret effective runtime
and profile configuration at admission. Secrets remain in provider stores,
daemon environment, or a local mode-0600 secret file and are always redacted
from API, export, logs, and snapshots.

Tiers are capability bounds:

- tier 1: workhorse;
- tier 2: generalist;
- tier 3: heavy.

The root profile is always explicit. Orchestra never routes between profiles.
A parent explicitly names each child profile, which may be the same tier or a
lower tier but never a higher tier. Profile priority is metadata for display
and external policy; it never changes FIFO scheduling.

## 6. Managed configuration

SQLite is authoritative for groups, runtimes, profiles,
runway source definitions, Observer settings, fleet settings, devices, and
service tokens. Configuration changes go through the daemon and create control
audit rows. Export is a redacted interchange document, not an alternate source
of truth.

Only bootstrap location, daemon/service startup values, and secrets remain
outside SQLite. V2 deletes the comment-preserving TOML editor and does not
maintain two configuration paths.

## 7. Admission contract

`RunRequest` v2 contains:

```text
request_id    required  caller idempotency key
profile       required  enabled profile id or name
context       required  self-contained executable request
group         optional  group id/name; defaults to General
title         optional  human display label
cwd           optional  write-only daemon-host directory override
ref           optional  opaque caller correlation value
after         optional  run dependencies with success|terminal condition
requested_by  optional  stable audit label
observer      optional  inherit|off|observer profile; defaults to inherit
```

Only `request_id` deduplicates. Replaying it returns the original admission;
`ref` is stored and indexed but never interpreted. Admission validates the
working directory, profile/runtime availability, dependency ids and conditions,
Observer selection, and lineage bounds in one transaction. It then
allocates the group number and freezes the run brief/configuration snapshot.

## 8. Scheduler and lifecycle

The daemon is the only scheduler. Ready runs start in admission order. The
global active cap defaults to eight; profiles may add smaller caps. A global
pause prevents starts but does not interrupt active runs or maintenance.

Queued runs expose exactly why they are held:

- dependency condition not yet met;
- global pause;
- global active capacity;
- profile active capacity;
- fresh definitive runway exhaustion; or
- scheduled retry time.

Stale, unknown, or unlinked runway never blocks a run. Orchestra never selects
a substitute profile.

The externally meaningful lifecycle is:

```text
queued -> starting -> running <-> waiting -> completed
                                      |----> failed
                                      |----> timed_out
                                      |----> stopped
                                      |----> skipped
```

`waiting` has a kind such as `input` or `children` and does not consume active
runtime capacity. A failed `success` dependency causes a dependent to become
`skipped`; a `terminal` dependency accepts any terminal predecessor.

Crash repair relies on durable process/session identity. It never equates a
PID, process creation, or an empty log with successful execution.

## 9. Delegation, continuation, and retries

A running worker may request child runs through its bounded run token. The
parent names the profile and supplies bounded executable Context. Default maximum
child depth is two; hard/configurable bounds include three children per parent
and three concurrently active children. All children are ordinary runs,
inherit the frozen group and CWD, receive their own group number, and record parent
lineage.

When a parent turn finishes before children settle, the parent becomes
`waiting:children`, releases runtime capacity, and resumes the same run/session
once results are ready. This resume does not create a continuation run.

Explicit **Retry** and **Continue** controls do create new runs. Retry freezes
the prior request/configuration plus requested adjustments; Continue uses the
prior result as bounded context for a new request. Both inherit group/CWD and
record their lineage type.

Orchestra may create one automatic retry for a conservatively recognized
transient infrastructure failure. It is a new, numbered run with the same
group, frozen CWD, and profile. Unknown or authentication failures do not retry;
they stop and create attention. No retry reroutes profiles.

## 10. Messages, attention, and controls

Every run has a durable thread with sender, kind, body, timestamps, delivery
state, receipt, and failure reason.

The fleet-wide Inbox is backed by first-class **Attention** records:

- blocking question or decision, with correlation id, optional choices,
  optional deadline/fallback, and no default expiry;
- profile-change proposal with an explicit patch and rationale; or
- nonblocking alert.

A blocking item suspends the run as `waiting:input`, releases its runtime slot,
and does not retain a sleeping harness process. The first authorized answer
wins transactionally. The response is inserted into the thread and the same
run/session resumes. Current-profile effort or note reductions may be applied
by the worker directly; other profile changes require an approve/reject
proposal. Orchestra validates and applies an approved patch.

Controls are:

- **Tell**: live steer when supported, otherwise safe-boundary delivery.
- **Interrupt**: cancel the active turn then resume the same run with new
  direction. Before a reliable session reference exists, restart from the
  frozen brief, trace summary, and new message, with explicit replay-risk
  evidence.
- **Stop**: target only the selected run.
- **Stop Tree**: target the selected run and descendants explicitly.
- **Check**: run mechanical inspection and optional Observer review.
- **Retry** and **Continue**: admit lineage runs as defined above.

Every control accepts an idempotency request id where replay matters and
creates a control audit record. Undeliverable messages remain visible; they are
never silently dropped or redirected.

## 11. Observer

Observer is one first-class, configurable agent-runtime subsystem. It is not a
control turn, seat, worker run, child, reviewer, router, or Nod integration.
There is one fleet default Observer profile or observation is disabled.

Observer receives only bounded Context and a trace window. It gets no
workspace, tools, worker run token, or delegation authority. Its activity and
checks are stored separately from worker runs, group numbering, and ordinary
run statistics. Observer usage/cost is separately visible and also included
in combined cost.

The default schedule is a first check after five minutes and at least five new
events, followed by checks no more often than every thirty minutes and only
when new events exist. Observer concurrency defaults to one.

Authority is “correct, then stop”: the first adverse judgment may Tell; a
repeated adverse judgment after new evidence may stop the run. An Observer
stop emits an Inbox alert and optional callback. Mechanical supervision,
failure classification, and retry remain deterministic subsystems.

## 12. Runway

A runway source is a named provider/account/lane measurement definition.
Multiple accounts and lanes are first class, and multiple profiles may link to
one source. Built-in adapters cover supported providers; a custom adapter is a
configured argv command that reads/writes bounded JSON. There is no generic
HTTP mapping language or plugin SDK.

Each observation records windows, reset times, value/remaining capacity,
freshness, adapter outcome, and timestamp. The UI starts with sources, shows
linked profiles, history and burn, and marks stale/unknown data honestly. Only
a fresh definitive zero creates a scheduler hold.

## 13. Evidence and artifacts

Evidence is retained indefinitely by default:

- run request, frozen profile/runtime snapshot, lifecycle, holds, and lineage;
- thread messages, attention, receipts, and control events;
- raw runtime logs and normalized trace events;
- worker and Observer usage/cost;
- Observer checks and decisions;
- explicitly published artifacts; and
- Git base/head/branch/checkpoint/patch/diff facts.

Artifact publication is explicit. A worker names a file under its work
directory; Orchestra rejects traversal and escaping symlinks, copies the bytes
to immutable run-owned storage, and records name, media type, size, SHA-256,
and timestamps. The API supports metadata, range reads, and download. Web and
Apple clients preview common text, Markdown, images, PDF, audio, and video
metadata and download everything else. Orchestra never scans or sweeps a
workspace for “interesting” output.

Storage management reports usage, provides a dry-run prune, and protects
pinned evidence. Worktrees may be released after checkpointing; branches and
checkpoints remain. Deletion is always an explicit operator action.

## 14. Git boundary

When the frozen CWD belongs to a repository, Orchestra automatically creates a
per-run worktree when needed without mutating the owner's checkout. Orchestra
records branch, base, head, checkpoints, patch, and diff as execution evidence.
Repository detection and containment are internal execution behavior, not a
public routing or isolation policy.

Orchestra does not run acceptance checks, assign reviewers, judge changes,
resolve conflicts, rebase, merge, land, update an owner checkout, or emit a
landing receipt. Workbridge, another integration, or a human owns those
decisions. A successful code run is complete with its result, artifacts, and
retained Git evidence.

## 15. Authentication and network boundary

V2 has three credential types:

- paired operator-device tokens, created through a short-lived one-time code or
  pairing URI, stored hashed, individually revocable, and held in Keychain or
  a secure same-origin cookie;
- service tokens with fixed authorities such as dispatch, read, control, and
  answer, but never device or daemon administration; and
- short-lived run tokens scoped to that run's worker routes and revoked at
  terminal state.

There is no general RBAC, user/org model, secret broker, or multi-tenant
sandbox claim. `/health` exposes only minimal liveness. Every v2 resource and
stream route is authenticated. Operators bring a trusted private network and
TLS/reverse proxy; Orchestra does not provide public relay or TLS automation.

Active first-party clients may badge or notify locally. Reliable background
push belongs to an external callback adapter; v2 does not contain APNs.

The bootstrap defaults to loopback port 8765 as a human-addressable local
choice, not an ecosystem reservation. The host setting is explicit and a bind
collision fails clearly; tunnel, proxy, and client-side forwarding collisions
remain operator-visible infrastructure concerns.

## 16. API and clients

The sole public contract is `/api/v2/...`, described by OpenAPI 3.1 at
`/api/v2/openapi.json`. Normal CLI operations use it too. Resource lists use
bounded pagination or cursors. Run and Inbox feeds are durable reconciliation
surfaces; SSE supplies invalidation and live evidence, with a slow fallback
refresh after suspension or loss.

Mutation endpoints use caller request ids for idempotency where duplication
would matter and always audit the acting credential. The API never returns a
secret, arbitrary workspace path, or unredacted environment.

The build-free web console uses ES modules and CSS. Shared SwiftUI code targets
iPhone, iPad, and native macOS rather than treating Catalyst as the only Mac
client. Both implement code/URI pairing, secure credential storage, adaptive
navigation, and native artifact previews.

## 17. Integration boundary

An integration may discover resources, choose a Group and Profile, optionally
supply a write-only CWD, submit self-contained executable Context, correlate
with opaque `ref`, control or answer the
run, and consume evidence. It must not read Orchestra's database, inject
source-system fields, or interpret callbacks as durable delivery.

Workbridge is the only component expected to know both Slash Work and
Orchestra. Workbridge owns root routing, source request/lease semantics, work
policy, profile choice, second opinions, verification, acceptance, source
lifecycle, and Git landing/merge. It communicates only through v2 HTTP with a
service token. Orchestra never imports Workbridge and never waits for it.

## 18. Explicit deletions

V2 contains no compatibility implementation for:

- Nod client, configuration, or schema;
- source caches, claims, leases, writeback, handoff, or delivery watermarks;
- generic control seats, control turns, or `runs.layer`;
- resolver, merge judge, reviewer, landing, or landing receipts;
- project-as-workspace/group overload;
- shared fleet keys, federation, node registration, or remote workers; or
- v1 database/API readers, aliases, or dual-write paths.

Old history remains an inert archive. The clean v2 instance imports only
operator configuration that maps unambiguously to v2 concepts.
