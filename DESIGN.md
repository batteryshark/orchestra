# Dromond — design v1.0

2026-08-13. Every subsystem is decided; a build session can take any item
without hitting an open design question.

This document says what Dromond is and how it behaves. It does not argue for
itself — the reasoning behind each decision, including the options rejected,
lives in the Work tickets listed at the end.

## What Dromond is

A local control plane that turns agent CLIs (Codex, Claude Code, OpenCode,
Reasonix) into a coordinated team. It consumes Work items as its source of
intent and writes results back through Work's contract verbs (CONTRACT.md).
Human-first: the person delegates, approves, and closes; Dromond executes.

## Principles

1. **Dromond dispatches; it does not own intent.** It is a dispatch service for
   harnesses: take a mission, pick a profile, run a harness in an isolated
   worktree, watch it, land the result. Work is where intent lives and
   where results are recorded — the sweeper is the front door — but the
   execution half knows nothing about it, and `dromond dispatch` from a shell
   works with no Work involved. Dromond is never the place work is born.
2. **Waiting is free.** Event-driven wakeups only — hooks, child-settled
   interrupts, dependency release, Nod callbacks. No polling turns, no timer
   check-ins.
3. **Tokens are budgeted.** Every fixed injection has a measured ceiling.
   Reducing turn count beats trimming prose.
4. **Zero runtime dependencies.** Stdlib Python, SQLite, one static HTML file.
   (Nod is a *service* dependency, not a library one, and degrades to pull.)
5. **Approval is out-of-band.** Nothing can grant itself the thing it asks for.
6. **Code coordinates; agents judge.** Everything mechanical — ferrying,
   wakeups, state assembly, sequencing, retries, filing — is deterministic code.
   Agents are invoked only for judgment, episodically, with code-curated
   context.
7. **Visibility over limits.** Where a ceiling and a clear view of what is
   happening both solve a problem, take the view. Every artificial cap
   considered for this design was deleted in favour of showing the number and
   offering a stop.

## The shape, end to end

```
Work (system of record)
  goal / task, ticked `delegated` by a human
        │  sweeper claims                              ▲
        ▼                                              │ comments, transitions,
Dromond daemon (LaunchAgent, Mac, ~/.dromond)          │ artifacts, issues,
  dispatch → worktree run → supervise → complete ──────┘ proposals
        │           │            │
        │           │            └─ findings[] + proposals[] → filed by code
        │           │
        │           ├─ trace events normalized → dashboard / iOS / SSE
        │           └─ spin observer (out-of-band, cheap model)
        │
        ├─ verified merge → base branch (scratch worktree)
        └─ escalation → Nod card → human answers → mirrored into Work
```

# Subsystems

## 1. Work integration

The contract (work-management/CONTRACT.md) is the only coupling. Dromond talks
to Work through its sanctioned agent surface and never writes Work's files: the
daemon uses `/api/agent/*` over HTTP with an `X-Work-Agent` identity, and a run
that needs Work directly uses the `work` CLI, which carries the same identity
and exposes its own capability catalog (`work agent operations`,
`work agent instructions <op>`).

Every write is made by Dromond's code: the sweeper claims and transitions, the
supervisor files findings and proposals, merge posts its comment, the conductor
logs planner turns. What remains for a run is occasional context reading.

**Five verbs**: comment, transition, attach, file-issue, and **propose
follow-on work** — create a task always parented to a delegated goal item,
always `delegated: false`, never top-level, attributed to the run. Work enforces
the last one with a gate rejecting agent-created tasks that lack a goal parent.
Agents cannot edit tasks, so everything a proposal needs — parent, tags,
project — is set at creation.

**Goals.** A goal is a Work task tagged `goal` and ticked `delegated`: the epic
pattern Work already renders, a parent task that accumulates children. No new
item kind — Work never learns a Dromond concept.

**The plan surface is hybrid.** Ephemeral steps stay Dromond runs; they die with
the run. Work that outlives one run, or that the human would want to see,
comment on, or reprioritize from the phone, becomes a **child task under the
goal**. That is what verb 5 proposes.

**Project identity.** Work owns what projects exist; Dromond owns what is
running. Projects are discovered from Work — never a second registry — and
per-project settings key on Work's immutable `projectId`, which survives folder
renames.

## 2. The daemon

**One process** runs the sweeper, the conductor, supervision, and HTTP, as a
launchd **LaunchAgent** in the user session on the Mac. The agent CLIs, their
credentials, and the project checkouts live there; a system daemon runs outside
the login session, cannot reach the keychain, and would fail at first spawn.
Work may live on another machine — Dromond reaches it over Tailscale.

**State is central**: `~/.dromond/` holds the SQLite database, briefs, logs, and
worktrees. A worktree lives at `~/.dromond/worktrees/<projectId>/run-N`, keyed
by the immutable UUID rather than the Work id, which is mutable and would
strand the directory on a rename. Projects get no state
directory of their own, so there is nothing to gitignore per repo, deleting a
project's history is one database delete plus one directory, and cross-project
questions are one query. Run history accumulating in one place is also what
makes later analysis — turning runs into knowledge — a matter of pointing at a
service rather than walking every project folder.

A run started from an earlier run never inherits that run's workdir or branch
unchecked: it **re-homes**. A released isolated checkout is replaced by a fresh
worktree on a fresh branch; a shared checkout falls back to the project root.
One rule, every re-dispatch path — continuation, retry, and anything added
later — because this was got wrong three separate times, each through a
different door.

A worktree is released when its run reaches a terminal state; the branch
outlives it, carrying the commits until a merge lands them. `dromond prune`
sweeps whatever a crash left behind. A checkout holding uncommitted work is
kept and reported rather than removed, since those changes die with the
directory while committed work survives on the branch.

The CLI resolves the current directory to a Work project rather than walking up
for a state directory.

**Configuration** lives at `~/.config/dromond/config.toml`: `[profiles.NAME]`
tables, `[work] api_url`, the API secret, the Nod issuer token, and
`[project."<projectId>"]` tables carrying per-project settings and that
project's enabled profile set. Profiles themselves are global, living only in
top-level `[profiles.NAME]` tables. Per-project settings
may not live in `.work/` — the contract forbids cross-boundary file writes.

**One Work server at a time**, pointed at explicitly since its port is not
guaranteed. Switching rewrites config and restarts, and is refused while runs
are in flight unless forced: in-flight items reference the old server, and
`W-####` ids are unique only within a workspace. A genuinely separate workspace
gets a **second Dromond daemon** on another port.

## 3. HTTP surface and dashboard

- **One port** (default 3011): static dashboard file at `/`, API under `/api/`.
- **One snapshot endpoint** returns the whole control plane — runs, profiles
  with headroom notes and runway, statistics, recent findings and proposals,
  and daemon health (last sweep time, outcome, items claimed, last error) —
  carrying an integer `version`. Action routes handle stop, tell, check, force
  sweep, and pause/resume dispatch.
- Any payload change **bumps `version` and updates the captured fixture** the
  iOS decoder test pins against.
- **Liveness**: the dashboard polls the snapshot; SSE carries the trace stream
  and the daemon's own log. No websockets — actions are POSTs.
- **Auth**: every route, including reads, requires a credential — the snapshot
  and traces carry source code, prompts, and transcripts. Two credentials share
  the `X-Dromond-Key` header:
  - the **human's shared secret**, generated at `dromond init` into 0600 config
    with an env override, pasted once into the iOS Keychain; and
  - a **per-run token**, minted at dispatch, stored only as a hash, injected
    into the worker's environment, and revoked the moment the run reaches a
    terminal state. Header only — never a cookie or query parameter.

  A **route authority table** decides what each may do, in one auditable place:
  reads are open to both; `stop`, `tell`, and `check` are **self-only** for a
  run, so it can act on itself and never on a sibling; sweep, pause, resume, and
  dispatch are the human's. An unlisted route is the human's by default, so
  adding one never accidentally grants a run anything.

  Tailscale binding and a Host check sit underneath.
- **Rejections are honest**: 401 with a one-line reason, logged with source IP.

## 4. Dispatch

**There are no concurrency caps** — not global, not per project, not per
profile. Ten to fifteen concurrent runs, several inside one project, is normal.
Rate limits are per provider and the harnesses handle them; the processes are
network-bound, idle awaiting model responses; every run has its own worktree, so
concurrent runs never interfere during execution; and merges are sequential,
rebasing and escalating real conflicts on their own. Many runs are read-only
research that produce no branch at all.

Run count is bounded by what creates runs: a human ticking `delegated`, planner
dispatch, and the spawn tree — which stays bounded by depth and per-run child
limits (§5).

**Order** matters only for items that must wait — those blocked by an unfinished
dependency, and anything held behind a paused dispatch switch. Ordering is
dependencies first, then ready-lane board order, then FIFO. Work has no priority
field by design, so the board's order *is* the priority signal: reordering the
lane from the phone changes what runs next. Explicit `--after` dependencies
between runs are also honored.

**Queue state stays honest**: an item transitions to `in_progress` only at
actual dispatch, never on entering the queue, and anything waiting shows its
reason. A board that claims something is running while it waits stops being
trusted.

**A pause-dispatch switch** stops new runs starting without touching those in
flight, beside a prominent live run count. That pairing replaces every ceiling
this design considered.

**Nothing predicts file overlap.** Which files a run touches is unknowable in
advance; merge handles collisions (§9).

**Isolation**: every run gets a git worktree on branch `dromond/run-<id>`,
created under `~/.dromond/worktrees/`.

## 5. Profiles and delegation

**Profiles are launch templates, not identities.** A profile names a harness, a
model, a reasoning effort, a tier, and a priority. The word is **harness**
wherever a human reads it; the config key stays `backend`, because renaming it
would break every existing profile and all four runners. Mailboxes address **run ids**, so
two concurrent runs may share one profile and neither is "the" holder of it.
There is no roster and no agent-as-person anywhere in Dromond.

**Discovery, not typing.** Model lists come from the harnesses themselves
(`opencode models`, `codex debug models`, Reasonix's config with its declared
`supported_efforts`). A profile is assembled by picking backend → model →
effort from real lists.

**Profiles are global presets; a project enables a subset.** A profile is
never overridden per project — that was tried and removed. A project's
`[project."<projectId>"] enabled_profiles` lists which profiles it may staff a
run with; absent means all of them. Enablement binds at exactly two moments:
when a run is **staffed** (the sweeper, `dromond dispatch`, the conductor's
dispatch action, and the observer's and planner's own profile picks) and when a
running agent **delegates**. A run already in flight is never revalidated — it
keeps the preset it launched with, stale or not, which is the accepted cost of
never letting a config change reach into running work. Staffing a profile a
project has not enabled is a refusal naming the project and the enabled set,
never a fallback to another profile.

**Tier and priority are routing metadata.** Tier is capability — 1 workhorse
(well-defined bounded tasks), 2 generalist, 3 heavy (the hardest thinking) —
numbered so they sort. Priority is preference *within* a tier: 0–99, `nice`
semantics, lower is more preferred, default 50. Both ride in the snapshot
because a planner routes on them, and they pick the internal profiles too: the
spin observer takes tier 1, the conductor's planner tier 2.

**Headroom is a note** — one freeform field plus the time it was written ("10%
weekly left, resets Sunday 18:00, lean heavy on it"), displayed with its age.
The note is **authoritative over measured runway** for routing: a number cannot
know that an allowance is about to expire unused, and a note exists because the
human wants that thing to happen.

**Claude is the one hole in discovery**: it has no model-listing command, so a
Claude profile's model and effort are typed rather than picked, and the UI says
so rather than pretending otherwise.

**Profiles are managed, not hand-edited.** The dashboard adds, edits, and
removes them, with the discovery pickers above feeding the model and effort
fields, and the CLI keeps parity (`dromond profiles`). There are no default
profiles: a profile that names no model would launch whatever the backend
happens to default to, which is the guessing discovery exists to end. A fresh
install has none and says so.

**Agent write authority is split by cost, and keyed on the credential** — not on
a caller declaring what it is. An agent may update a profile's note
freely, and may **lower** its effort. Tier and priority are not its to change,
nor is a project's enabled set: promoting itself, or enabling a dearer profile
for its own project, is the self-grant principle 5 forbids. **Raising** effort files a Work
decision, as does adding a provider or model entry: a worker moving itself from
`low` to `ultra` is granting itself the thing it asks for, which principle 5
forbids regardless of whether a new model entry appears.

### Child runs

**A run can delegate to child runs, and this is central to how work gets done.**
A worker that needs parallel help — or a cheaper model for a bounded subtask —
requests a child run; the supervisor brokers it, launches it, and wakes the
parent when the child batch settles. A parent blocked on children costs nothing
while it waits.

Delegation is **brokered by code, not granted wholesale to the model**, because
harnesses that grant it wholesale leave the *choice* of delegate to
unconstrained prompt-level behavior — which in practice means delegating down to
weaker models, up to expensive ones, and to names that do not exist.

- **`spawn_profiles` on a profile** names exactly which profiles it may delegate
  to, intersected with the project's enabled set. Absent or empty means it
  cannot delegate at all.
- **The brief mentions spawning only when `spawn_profiles` is non-empty**, and
  then lists the permitted names. A worker is never taught a verb it is
  forbidden to use.
- **Reject, list, continue**: a request for a forbidden or unknown profile is
  refused with the valid list returned, so the worker corrects itself and the
  run continues. The attempt is recorded as a finding, so a profile that keeps
  trying becomes visible rather than folklore.
- **Depth, per-run, and concurrent child limits** are config-bounded.
  `spawn_profiles` answers *who*; those answer *how many*.

There is no grouping layer above profiles — no teams, no squads. Coordination
happens through run ids, the goal item, and the parent/child relationship, which
covers what a named group was ever used for.

## 6. Execution: backends, hooks, messaging

**Four backends**: Codex, Claude Code, OpenCode, Reasonix. One `build_cmd`
function per backend, no adapter classes.

**v1 transport is exec everywhere**, with messages delivered at turn boundaries
(Claude `--resume <id> -p`, Codex `exec resume <id>`, OpenCode exec plus its JS
plugin, Reasonix `--resume`). ACP is the first post-parity upgrade, and it has two
tiers rather than one: Reasonix alone injects mid-turn, through the
vendor-prefixed `_reasonix.io/session/steer` advertised in `initialize._meta`;
OpenCode's gain is a live session needing no kill and re-exec. `session/cancel`
is a **notification, not a request** — both agents answer `-32601` to a
request-shaped one, so getting this wrong silently breaks cancel. A profile
opts in with `transport = "acp"`, and a dead peer fails the run with a reason
rather than falling back to exec mid-run, which would leave a run whose
transport nobody can reason about. The pending-delivery badge (§7) keeps
boundary delivery honest for the backends still on exec.

**Hooks are mandatory and install at `dromond init`.** One shared `dromond hook`
binary serves Claude, Codex, and Reasonix — but their *file formats* are not
identical: Claude and Codex take matcher groups, while Reasonix takes a flat
handler list and reports the nested form as invalid. Codex's hook trust lives
in `$CODEX_HOME/config.toml` as `[hooks.state."<path>:<event>:<group>:<index>"]`
records, written at init and reported by `dromond doctor`.
OpenCode has no shell hooks and gets a small JS plugin listening for
`session.idle` and `permission.asked`. Codex's one-time hook trust is
provisioned at init and verified by `dromond doctor`; spawning with a
bypass-trust flag every time would silently disable a deliberate safety feature,
so that flag stays a documented escape hatch.

Liveness comes from process-level stall detection and status from transcript
parsing — never from asking a worker to report.

**Two messaging verbs**: `tell <run> <msg>` (non-blocking, delivered at a safe
boundary) and `ask human <question>` (blocking with a declared fallback). Only
`human` is a meaningful ask target: the answer arrives through a Nod card and
the Stop hook, and a parent-to-child ask has no such channel — it would need
its own design.
Broadcast is `tell` × N; notes and reports go to Work item threads; handoff is a
checkpoint.

**`ask`**: the Stop hook holds the session open, Dromond files a Nod decision
request (§8), and the answer is injected back through the hook. Question and
answer are both mirrored into the Work thread. Nod's `expires_at` is the
declared fallback.

**An open `ask` suspends stall detection.** A run held by its Stop hook emits
no output for as long as the human takes, which the stall timer would read as a
hang and kill, discarding the answer. The hard timeout still caps it.

**Undeliverable messages are marked and surfaced**, never dropped — and never
auto-redelivered to a later run, which would hand a correction to a run that
never saw the context it referred to.

**Peer-to-peer scope**: human-to-run and parent-to-child. Arbitrary run-to-run
messaging waits for a use case and a discovery story.

**Brief budget: ≤300 fixed tokens per dispatch** — header, mission, and a
~10-line protocol card, with the Work snapshot capped at 2,000 characters and
frozen at dispatch. Per-verb detail loads on demand through `dromond <verb>
--help`. The continuation wrapper stays ~130 tokens.

## 7. Supervision: traces, the observer, retry

**Traces normalize at ingest.** The supervisor already tails each backend's
JSONL to detect completion, so it writes one normalized events table —
assistant text, reasoning, tool call, tool result, permission request, human
injection, lifecycle — while keeping the raw file as source of truth against
format drift. One schema means one renderer, one SSE stream, one iOS decoder.

Each event stores a truncated payload (~2KB) plus a byte offset; the collapsible
view expands in place from the raw file. **Retention**: normalized events are
kept indefinitely; raw logs age out after N days for terminal runs only.

**Inbox / outbox**: one panel per run, both directions, every message badged
**queued / delivered / answered**. Knowing what happened to a message is the
feature.

**Direct intervention** works from the dashboard and from iOS. Seeing a stuck
run and having to route a correction through an orchestrator wastes tokens and
time. Record editing and board management stay off the phone.

**The daemon's own log** streams to the dashboard over the same SSE plumbing,
answering "is the service healthy and doing its job" — separate from what any
run is doing.

**There are no budgets or run ceilings.** Runs go three hours and do good work.
What bounds the system: the `spawn_profiles` allowlist (who), the pause switch
(whether), retry-and-escalate (when things break), and the spin observer (when
things go feral).

**The spin observer** has three layers: process stall detection (zero tokens),
mechanical loop detection (same tool call or same file edited N times running),
and a cheap **out-of-band observer turn** for long runs — first look at five minutes, then every half hour. A run that is already lost is lost inside five minutes, which is when stopping it is still cheap; after that a half-hourly glance catches one that wanders later without pestering one that is simply working. The cadence is the same for every run — keying it to the observer's own tier measured the wrong thing, since naming a stronger judge silently made the watch later. It reads the transcript from outside and judges with a
cheap model, so the worker never knows it happened and a productive long run is
undisturbed. Exactly three outcomes: do nothing, `tell` a correction, or stop
and escalate. **It may never silently kill a run** — long is not wrong. Also
available on demand as `dromond check <run>`. The observer profile is named explicitly and is deliberately NOT the cheapest: deciding whether a run is converging or merely busy is a judgement, and it is asked rarely — first look, then hourly — so the model matters more than the tokens. The tier scan cannot choose while two profiles share a tier, and an unnameable observer must be loud rather than silently off.

**A resume that cannot resume is not a failure.** When a backend reports the
session missing, the run restarts fresh in a new session, exactly once, and the
row records that the session was gone. A `killed` run's session is never
resumed at all — the sweeper continues only a run that ended on its own, which
is why `killed` is also outside the retry set.

**Retry**: a retry is a fresh run in a re-homed checkout, never the failed
run's released one. Code retries **once**, automatically, for infrastructure-shaped
terminal states — `failed` and `timeout` — reusing the same brief. `killed` is
**not** among them: nothing sets it except a human's stop, the dashboard button,
or the observer's own stop verdict, and retrying any of those would fight the
person who asked for the stop. A run that finished but produced bad work is a
judgment failure and goes to a planner turn, never to retry — the same brief
through the same model reproduces the same bad work. Two consecutive
infrastructure failures on the same item stop and escalate.

A retry re-points the waiting dependents at the new run, or the dependency
release would decline the whole chain while the retry was still starting.

The observer's cadence is driven by the supervisor, which is already awake per
run; putting it on the daemon tick would block sweeps behind a model call. Its
profile is configurable per project rather than per goal — a Work item carries
no settings.

## 8. The human loop

**Escalations are delivered through Nod** (github.com/batteryshark/nod), a
self-hosted decision service that already owns Apple push, a TestFlight iOS app,
macOS/Windows/TUI clients, channels, signed on-device decisions, and audit
records. Dromond builds no push plumbing of its own.

Issuer API: `POST /api/v1/requests`, `GET /api/v1/requests/{id}/decision`,
`GET /api/v1/requests/{id}/wait` (long-poll, `timeout_seconds` 1–60, returns
`timed_out`), `POST /api/v1/requests/{id}/cancel`. Bearer issuer token in 0600
config.

- **Two channels**: `dromond-decisions` (needs an answer) and `dromond-alerts`
  (informational, dismiss-only), so alerts can be muted without muting
  decisions.
- **Answers happen in the card** via Nod's options, with `links` carrying "open
  the Work item" and "open the run trace". `options` give merge conflicts real
  buttons, `approve_with_text` answers a blocked run, `dedupe_key` stops a
  retried run buzzing twice, `expires_at` times out a stale ask.
- **Nod is the input device; Work stays the ledger.** Every decision is mirrored
  into the Work thread as an attributed comment or decision resolution. Nothing
  is answerable only in Nod, so an outage degrades to Work's pull queue.
- **Callback-driven** (`callback_url` wakes the daemon) with a `wait` long-poll
  as a startup backstop. Nod's callback is unsigned and best-effort by design,
  so Dromond always re-reads the decision through the API before acting.

**What escalates**: anything entering the needs-you queue — blocked runs, pivot
proposals, merge conflicts, failure escalations — plus goal completion. Never
run start or finish: a feed that buzzes trains you to ignore it.

Work itself does not issue Nod requests. Work is a place the human goes,
deliberately; push is reserved for genuine interrupts.

## 9. Completion: findings, proposals, merge

**Findings and proposals are structural** — two sibling **required** handoff
fields, `findings: []` and `proposals: []`, where empty is a valid answer and
absent is not. A finding is "I noticed something wrong"; a proposal is "this
goal needs this next step". **Code files them**, so a worker cannot forget.

- Findings carry `claim`, `where`, `confidence` (`observed` | `suspected`), and
  `why_not_fixed`, and become Work issues attributed to the run, left unclaimed
  so they land in triage and never become work without a human. Not-delegated
  is Work's default for an agent-filed issue; nothing sends a flag.
- **Fingerprint dedup** on `(project, where, normalized claim)`: a repeat
  increments an occurrence count rather than filing a duplicate. It also tries
  to note the recurrence on the existing issue, which Work currently refuses on
  an unclaimed issue — so until that changes, the count is visible in Dromond
  and the rejection is recorded.
- **A proposal needs a goal to parent to.** The sweeper dispatches any delegated
  item, so a run serving a plain task or an issue has no goal; its proposals are
  dropped with a reason rather than inventing a parent. Goals arrive with the
  conductor.
- The acceptance-criteria tripwire is **declared by the worker**, not detected:
  agents cannot edit tasks, so nothing can literally change criteria. It is the
  weakest of the three tripwires and depends on the worker being truthful.
- **Optional confirm pass**: a `suspected` finding may be dispatched to a cheap
  short-lived run whose only job is confirm-or-deny with evidence. Per-goal
  opt-in.
- **Handoffs carry findings forward**: a takeover brief lists the project's open
  findings.

**Proposal evaluation keeps approval out-of-band**: a **worker** proposes and a
**planner turn** evaluates alignment — a separate episodic session holding the
goal, so nothing approves itself. A planner's own next step is not a proposal;
it is dispatch. The planner returns a forced binary (aligned | pivot) plus
rationale, and **mechanical tripwires force human review regardless**: the
proposal touches another project, changes the goal's acceptance criteria, or
exceeds a child-count ceiling. An aligned proposal lands as a child task under
the goal plus one thread comment; a **pivot** becomes a Work decision in the
needs-you queue.

**Landing the work.** Each run branch merges to the base branch on verified
success; a per-goal integration branch is available when a goal needs reviewing
as a unit.

Verification runs in authority order:

1. the repo's **declared checks** (test/lint/build) — deterministic, first;
2. mechanical **tripwires** — files touched outside the project, deletions,
   oversized diffs;
3. a cheap **agent review** of the diff against the item's acceptance criteria,
   only when checks pass and criteria exist.

This is a verification step, not a review council.

**A project that declares no checks lands on tripwires alone** — "verified" then
means only that nothing tripped, which is weaker than it sounds. Declaring a
check is what makes automatic landing trustworthy, and a project that wants
review rather than landing declares a check that fails.

Verification runs **inside the supervisor at finalization, synchronously**, so
that process stays alive through a full test suite, bounded by `check_timeout`.

Two ownership rules the escalation path needs: a merge escalation **outranks the
sweeper's completion report**, which would otherwise move the item to `review`
and quietly undo the escalation; and a git refusal — the compare-and-swap losing
to a base that moved — **escalates rather than passing as a note**, since that
is precisely the case the compare-and-swap exists to make loud.

An item that is an **issue** has no `review` state: the merge report goes to its
thread and the sweeper owns its state. Conflicted files exist only for a rebase
escalation; a checks or tripwire escalation names the stage instead.

**Deconflict**: rebase onto latest base; a clean rebase merges, a conflict
escalates with the conflicted files listed and a resolution run offered as an
explicit human choice, never automatic.

**Ordering**: a run's own worktree must be released before its branch can be
deleted — git refuses to delete a branch that a worktree still has checked out.
Release happens at terminal state (§2), so by merge time the branch stands
alone.

**Merge mechanics**: the supervisor merges, never the planner, and it does so in
a **scratch worktree** — updating the base branch ref without checking anything
out in the owner's working tree, which routinely holds uncommitted work. The
merge is `--no-ff`, so `git revert -m 1 <sha>` undoes it in one command, and the
base ref moves by compare-and-swap, so a base that moved underneath fails loudly
rather than silently.

**Refreshing the owner's checkout**: when that checkout sits on the base branch —
the ordinary case — a moved ref would leave the tree at pre-merge content, with
`git status` reporting every merged file as deleted. So after the ref moves,
Dromond runs `git read-tree -m -u <old> <new>` there. That plumbing **refuses
rather than clobbers** when local edits are in the way, which is the entire
reason it is safe to run unattended; it is never forced, and no variant that
discards work is ever used as a fallback. If it refuses, or the checkout is on
another branch or mid-operation, the refresh is skipped and the ready command
lands in the result instead. The outcome — refreshed, skipped, or refused, and
why — is always reported.

**A merge refuses only when the owner's edits overlap it.** A dirty base
checkout is the normal state of a repo somebody works in, so refusing on any
dirt at all escalated every run forever, and the resolver dispatched to clear
one escalation hit the same guard and filed another. The merge cannot touch
those files anyway — it happens in a scratch worktree and the ref moves by
`update-ref`. Only files the owner is editing that the merge also rewrites are
a real problem: that is precisely the case the refresh must refuse, leaving
them on a stale index. So the guard waits until the merged file list is known
and compares. Untracked files do not count, or a build directory would block
every merge. `require_clean = false` turns even the overlap check off, and the
refresh still declines rather than clobbers underneath it — the guard is the
outer of two, not the only one. Dromond never resolves the overlap itself:
committing or stashing work in flight is the owner's to do, so the card offers
retry or leave it, and no resolver.

**A run may not land what the base branch does not track.** A run commits with
`git add -A`, so it sweeps up whatever sits in its worktree — including a live
record store a service rewrites while the run holds its branch. Both sides then
edit the same append-only file and the rebase conflicts every time; nothing
raced, two processes simply own one file, and no retry can fix it. So the merge
asks the base checkout's own `.gitignore` what is not source, and drops those
paths from the run branch — at its tip, and again from each replayed commit as
it conflicts. The file stays on disk and its owner keeps writing; only the
run's stale snapshot goes. A conflict in anything the base *does* track is
untouched and still reaches the human, which is the only kind of conflict worth
their attention.

The Work item transitions to `review`, and the thread comment carries the merge
commit, files changed, check results, and **the revert command**.

**A brief says what recently landed.** A run starts in a fresh worktree with no
memory of the project, so it cannot tell work that is waiting from work that
landed an hour ago — which is how two runs came to build the same thing at
once. Every dispatch brief carries the last twelve commits of its checkout,
frozen at dispatch like the Work snapshot. A **continuation** carries a
sharper version: the commits that landed on the base branch *since its parent
started*, which its own worktree branched before and cannot see.

**Every acceptance criterion is answered before the item moves.** Work refuses
a move to `review` or to `blocked` while any requirement or acceptance
criterion is neither ticked nor declined-with-a-reason. Both states are gated,
because both are a handoff back to the human, and an unanswered criterion makes
that human re-derive from a diff what the run already knew. Declining is always
available and is never a failure — "not attempted, blocked on X" is a complete
answer. What is refused is silence.

The worker owns the answer: its brief names the item and the exact commands.
A run that dies, is killed, or ignores the protocol gives no answer, and the
item would then have no state left to move to — so the sweeper declines what
remains, recording *which run failed to account for it*. It never ticks: a tick
claims verification, and the sweeper verified nothing. The one case it does not
answer for is a run that reports **success** with criteria still open; that
claim is unverified, so the item is parked in `blocked` for the human to judge. The run branch
is deleted on success and kept whenever the merge escalated — which makes the
branch recorded on a merged run a dead reference, so anything re-dispatching
from it mints a new one rather than reusing the name.

## 10. The conductor

Delegate a project to a goal and let agents keep aligning to it, without the
human acting as the persistence layer for that goal.

- **The goal lives in Work** — durable, editable from the phone, with a thread
  as its audit trail.
- **The conductor is deterministic code** extending the sweeper: it watches
  runs, ferries messages, wakes on events, sequences dependencies, and assembles
  state. Zero tokens while anything is merely in flight.
- **Planner turns are episodic judgment.** On a triggering event the conductor
  invokes a fresh stateless session with a code-curated packet and receives one
  structured decision, which is logged, attributed, and then discarded with the
  session. Cost scales with events, not elapsed time.

**The packet** (~1,500 tokens, hard cap, oldest detail truncated first) carries
six blocks: goal text and acceptance; delta since the last turn (runs finished
with outcomes, new human comments); open child items; open findings; available
profiles with headroom notes and measured runway; the current in-flight set.
It is deliberately larger than the 300-token worker brief — a worker needs
little, a planner deciding what to spend needs the state.

**Triggers** (five): batch settled, run blocked, nothing in flight, budget low,
new human comment on the goal. A floor of ~2 minutes between turns for one goal.
"Nothing in flight" fires **once per settle**, or an idle goal wakes a planner
forever. A turn returning `wait` must name the event it waits for, and only that
event re-wakes it.

**A turn returns** one structured object `{action, rationale}` where action is
`dispatch` | `propose` | `ask_human` | `wait` | `done`. Only state-changing turns
post to the goal's Work thread; `wait` turns go to Dromond's log, or an
overnight goal leaves fifty "still waiting" comments. Attribution is
`dromond/<run-slug>`.

The planner profile is configurable **per project**, default tier 2 (generalist) — a Work
item carries no Dromond settings, so per-project is the finest key that exists;
the observer's profile has the same limit.

"Budget low" is a **runway** reading (§11), never a grant: there are no budgets.
It informs a turn and does not gate dispatch.

Blocked, settled, and idle describe one moment, so the first to take a turn
silences the other two for that batch — otherwise a badly-ending batch buys
three turns over identical state. `done` and `ask_human` gate on a human
comment, so neither strands a goal nor re-wakes it forever, and a `wait` naming
no valid event is simply not a gate.

## 11. Statistics and runway

**Statistics come from Dromond's own data.** At completion a run records token
counts and cost alongside wall-clock on the run row, taken from whatever each
backend actually emits — which is not a uniform "result event": Claude and
Reasonix carry it in a final result, Codex in `turn.completed`, and OpenCode
per step, so its run total is a sum. Cache-read tokens are folded into
`tokens_in` for every backend so the four are comparable, though their own
inclusion rules differ: Reasonix already counts cache reads inside its input
total, Claude does not — so the dashboard is a query rather than a re-parse. Surfaced: runs
total and currently active, combined worker time, and a per-profile breakdown
(runs, time, tokens, cost) so a profile's real cost per unit of work is visible
rather than assumed. Capture is best-effort per backend and degrades to null,
never to a crash or a wrong number.

**Runway comes from outside, through code adapters.** One adapter per provider,
returning **every window that provider reports** — each with its own remaining,
reset, and staleness — plus scalar fields holding the tightest window that has
not itself reset, which is what dispatch and the trend read. Claude reports a
5-hour and a weekly limit: two facts, two rows. Codex reports `primary` and an
optional `secondary`, named by `window_minutes`. Keys are read from named environment variables inside the adapter and
never written to config, database, log, or payload — which is also why runway is
not declarative configuration.

- **DeepSeek** — balance endpoint.
- **Moonshot / Kimi** — coding-plan quota.
- **Claude** — `~/.claude.json` key `cachedUsageUtilization`: percent used and
  `resets_at` for the 5-hour and 7-day windows. Refreshes opportunistically and
  can lag hours, so the adapter surfaces `fetchedAtMs` as "as of".
- **Codex** — newest `~/.codex/sessions/**/rollout-*.jsonl`; `token_count`
  events carry `rate_limits` (`used_percent`, `resets_at`, `window_minutes`)
  from response headers, fresh per turn while a session runs.

**Every provider is `plan` or `api`.** A plan provider (Claude, Codex, the Kimi
coding plan) shows consumption against its windows and **never** a cost or a
currency balance; only an api provider (DeepSeek, anything billed per token)
shows money. The same split governs run statistics: a plan-backed run reads
"plan", never a cost of 0 or a bare null — OpenCode reports `cost: 0` on
subscription data, and a zero reads as free. Plan runs contribute nothing to a
cost total; their tokens still count.

**Staleness is a flag, never a silence.** A number labelled with its age is not
a number presented as current, so an old reading is always reported, carrying
its `as_of` and a `stale` marker the surfaces tone down. Withholding is the
worse failure: the owner then sees nothing at all. Only shape drift, an
unreachable endpoint, or an unauthorized key yields unknown — and unknown *and*
stale both mean treat the provider as available, so neither ever fires the §10
low-runway trigger. Every adapter fails soft: never an exception. Local-state readers carry a `ponytail:` comment, since the CLIs own
those files and may change them.

**Unknown runway means treat the provider as available and mark it unknown.**
Dispatch never blocks on a failed scraper: a scraper outage silently halting
automation is worse than the overspend it would prevent.

Each poll is stored, so the view shows the number now, a short trend, and
time-to-reset.

## 12. Run environment: what a run can see

Skills and directory exposure are one surface — what a dispatched run has in its
hands.

**A run inherits its harness's own tool configuration**, exactly as it would
when launched by hand. Should a run ever need a specific MCP server, a profile
declares it and Dromond passes it through the backend's own flag
(`--mcp-config`, `-c` overrides, config content).

**Skills are scoped to the backend.** A run receives the skill directory for its
own backend plus the shared set (`.agents`, `AGENTS.md`, `DROMOND.md`). Copying
another harness's directory in is not merely wasteful: its settings can carry
hooks that fire inside a run Dromond is already hooking.

**Global skills exist as an overlay.** `~/.dromond/skills/` syncs into every
run, with project-level skills winning on conflict. Harness-side orchestration
skills need somewhere every run can see them.

**Extra directories are declared, never discovered.** A run may be granted
read-only access outside its worktree — a reference repository, for instance —
via `--add-dir`, declared in central config keyed by `projectId`, per project or
per goal.

**A run carries its own API credential** — the per-run token of §3, in its
environment. Anything the worker spawns inherits it, and it dies with the run.

**Tool denials stay minimal.** One denial exists: OpenCode's native delegation
tools, which can leave a task child blocked forever on its own permission ask.
Dromond brokers delegation itself (§5), so nothing is lost. A general
`deny_tools` list waits for a second real case.

A containment profile bounds what a run may reach at the process level.

## Non-goals

- **Code-encoded judgment**: contract schemas, admission checks, review
  councils. Judgment belongs to agents, invoked episodically. The conductor
  loop itself is in scope.
- **A grouping layer above profiles** (teams, squads). Run ids, the goal item,
  and parent/child cover it.
- **Windows native.** WSL only.
- **Multi-machine federation**, and multiple simultaneous Work connections. A
  separate workspace gets a separate daemon.
- **Arbitrary run-to-run messaging**, pending a use case.
- **iOS write surface** beyond stop, answer, and `tell`. Record editing and
  board management stay off the phone.
- **Provider quota advisories that change nothing.** Runway exists to inform
  routing, not to print warnings.
- **Checkpoint and takeover.** They existed so a dying orchestrator session
  could hand off; the conductor is stateless and episodic, so there is nothing
  to hand over.

## Decision history

Every section above is built. Full reasoning for each decision, including the
options rejected, lives in the Work item's log, which is not public. This
document is the settled result; the rows below say which item settled what.

| Ticket | Decision |
|---|---|
| W-0145 | Goal shape and the planner packet (§1, §10) |
| W-0146 | Contract verb 5: propose follow-on work (§1, §9) |
| W-0147 | No budgets; retry policy; the spin observer (§7) |
| W-0148 | Backend hook / interrupt / ACP matrix — research (§6) |
| W-0149 | Claude and Codex local quota formats — research (§11) |
| W-0150 | Dashboard and the one API (§3) |
| W-0151 | Traces and direct intervention (§7) |
| W-0152 | Service topology, central state, project identity (§2) |
| W-0153 | Landing the work: merge policy (§9) |
| W-0154 | Escalation delivery via Nod (§8) |
| W-0155 | Dispatch: no caps, ordering, queue state (§4) |
| W-0157 | Messaging delivery tiers per backend (§6) |
| W-0169 | Run environment: skills and directories (§12) |
| W-0188 | Renamed Maestro to Dromond, 2026-08-14. "Maestro" collides with at least three active projects, one of them a near-identical AI-agent orchestration product. |

Standing decisions carried in: the planner profile is per-project configurable,
default tier 2 (generalist); orchestration and scoping skills stay harness-side, named by
the protocol card for discovery, with no coupling into Dromond.
