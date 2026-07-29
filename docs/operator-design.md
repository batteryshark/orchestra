# Orchestra Operator

Status: implemented v1 contract and controller

Audience: Orchestra maintainers and early operators

Primary dogfood corpus: the PIU workspace

## 1. Product thesis

Orchestra already makes worker execution durable. It can dispatch independent
runs, preserve backend sessions for follow-up, interrupt work at safe action
boundaries, wake a lead when child runs settle, record handoffs, and expose
several projects through one control plane.

The missing durable entity is the orchestrator itself.

Today, a person starts a Codex or Claude session and that session performs the
control loop:

1. understand the goal and current project state;
2. choose the next work;
3. write bounded briefs and fan them out;
4. route new evidence to affected work;
5. recover failed, stalled, or quota-limited runs;
6. verify and integrate worker results;
7. update the durable tracker and clean up execution resources;
8. repeat until the goal is actually achieved.

When the interactive session ends, the runs survive but this loop does not.
The person must return and say some version of "continue", reconstruct the
state, and restart it.

An **Operator** is a durable, policy-bounded implementation of that loop. A
person and an Operator first agree on goals and an operating contract. Once
activated, the Operator continuously reconciles actual workspace state toward
that contract. It escalates only when the contract does not grant sufficient
authority, evidence cannot resolve a consequential ambiguity, or a safety or
resource boundary has been reached.

The intended experience is:

> Discuss the desired outcome and boundaries once, approve the resulting
> contract, let the Operator keep making verified progress, and return for
> concise updates or genuinely necessary decisions.

This is not:

- an agent that may invent new goals indefinitely;
- a promise that a model process remains alive forever;
- a worker free-for-all that maximizes concurrency;
- permission to merge, publish, communicate externally, or delete data unless
  the contract grants that exact authority;
- a replacement for objective acceptance evidence;
- a background daemon that silently adopts every registered project.

## 2. Evidence from the PIU workload

The PIU workspace is not a hypothetical use case. It contains:

- 508 archived `piu-recomp` Orchestra runs with every brief, log, and the
  project database;
- 117 subsequent runs across the split repositories;
- 625 total recorded run attempts;
- hundreds of tracked work items, branches, handoffs, retries, reviews, and
  owner corrections;
- both independent fan-out and dependency-ordered work;
- cross-repository authority boundaries and shared source-of-truth artifacts;
- large build and reference trees that make resource leakage costly.

The archive is suitable for historical replay. It contains enough ordered
events to test whether a proposed Operator would have:

- selected useful next work;
- separated independent lanes from conflict-prone work;
- retried or rerouted failed attempts appropriately;
- propagated new owner evidence to affected lanes;
- distinguished worker completion from accepted work;
- preserved unique results before cleanup;
- remained within a simulated disk budget.

The current launch roster also captures substantial human routing judgment:
strong profiles with sensitive-work exclusions, quota-friendly generalists,
separate allowance pools, mechanical-only workhorses, and profiles disabled
after repeated backend trouble. Much of that policy currently lives in
free-text role descriptions, while dispatch-time usage handling is advisory.
The Operator should preserve and structure that judgment, not replace it with
a largest-headroom heuristic. Model strength is not monotonic with patch
discipline: a profile may be the best diagnostician for a hard problem while
being a poor unconstrained implementer for a narrow one.

The resource hazard is concrete. A surviving PREX3 worktree consumes about
3.8 GB, including about 3.5 GB of build/package output and 284 MB of copied
references. The owner has previously removed roughly 800 GB of accumulated
worktrees. Worktree lifecycle therefore belongs in the Operator contract and
acceptance state machine; it cannot be an optional housekeeping command.

## 3. Concepts

### 3.1 Operator

A durable control-loop identity responsible for one approved contract.

An Operator is not one immortal model process or one unbounded conversation.
It may use a sequence of bounded model attempts, different backends, and fresh
context reconstructions. Its identity, contract, event cursor, leases,
decisions, budgets, and progress survive those attempts.

### 3.2 Contract

The versioned agreement between the owner and the Operator. It defines desired
outcomes, scope, authority, evidence, resource budgets, escalation policy,
reporting, completion, and optional maintenance obligations.

The approved contract is the Operator's authority. A prompt, worker handoff,
or inferred follow-up cannot silently broaden it.

### 3.3 Goal

An outcome with observable acceptance evidence. Goals may contain milestones
and rolling work items, but a task list alone is not a goal.

Every work item and action created by the Operator must trace to a goal,
maintenance invariant, or approved contract amendment.

### 3.4 Operation

One activation of an Operator under a specific contract version. An operation
has a lifecycle, event stream, budgets, and one active controller lease.

### 3.5 Operator attempt

One bounded reasoning/execution turn by a model acting for the Operator.
Attempts are disposable execution contexts. The operation record is durable.

### 3.6 Work item and run

A work item describes a verifiable unit of project work. A run is one agent
attempt against a work item.

Run status and work status are intentionally different:

- `run done` means the agent process completed successfully;
- `work handed_off` means the result and evidence are available;
- `work accepted` means the result was independently checked, integrated when
  necessary, and satisfies the contract's gate.

### 3.7 Decision

A first-class escalation with evidence, options, a recommendation, scope, and
an explicit effect on continued work. A decision may block one lane without
blocking the whole operation.

### 3.8 Resource lease

A durable record for a worktree, integration checkout, build directory,
artifact, child-run allocation, or other bounded resource. A lease records its
owner, purpose, measured size, retention state, and cleanup eligibility.

### 3.9 Roster profile

A reusable launch profile with structured capability, quality, access, cost,
and policy metadata in addition to its backend and model. The profile is the
routing unit: two profiles using the same model may have different reasoning,
sandbox, tools, quota pools, or approved roles.

### 3.10 Capacity pool

A shared constrained resource consumed by one or more profiles. A pool may
represent a provider account, model-specific allowance, subscription window,
API balance, request/token rate limit, concurrent-session limit, or a reserved
high-quality tier.

A profile may consume more than one pool. For example, a model can share an
account-wide weekly allowance while also having a model-specific concurrency
or reset-credit constraint.

### 3.11 Routing decision

The durable record of a task's requirements, eligible profiles, exclusions,
live capacity snapshot, reservations, selected profile, and fallback chain.
This makes model choice explainable, replayable, and improvable rather than an
unrecorded orchestrator instinct.

### 3.12 Recovery council

A bounded, independent panel of the strongest profiles qualified for a stuck
task. Council members analyze the same evidence separately, then the Operator
compares their falsifiable diagnoses and recommended next actions.

A council is an internal recovery mechanism, not a new source of authority.
Its quorum may authorize an already permitted recovery action, but it cannot
approve scope expansion, weaken acceptance evidence, or turn an `ask` or
`deny` action into `auto`.

### 3.13 Change budget

A task-specific constraint on implementation surface and structural
complexity. It records the expected files or components, permitted refactoring,
and whether new dependencies, services, public APIs, schemas, migrations,
configuration, compatibility layers, or reusable abstractions are allowed.

A change budget is not an arbitrary line-count ceiling. A larger change may be
necessary, but the evidence must show why the smaller path cannot satisfy the
goal. Reasoning depth and implementation latitude are separate: a strong model
may investigate broadly while remaining authorized only to diagnose, propose
a discriminating test, or produce a narrow patch.

## 4. The collaborative contract experience

### 4.1 Design mode is read-only by default

Creating an Operator begins in design mode. The design agent may inspect
registered projects, playbooks, tracker items, Git state, tests, historical
runs, and resource usage. It may write contract drafts into Operator state,
but it does not dispatch workers, edit source, merge branches, or delete
resources.

The person should not need to restate doctrine already present in the
workspace. The design agent drafts the contract from existing evidence and
asks only questions whose answers materially change authority or outcomes.

### 4.2 Contract contents

Each contract version contains:

1. **Intent**
   - the problem or desired outcome;
   - why it matters;
   - named goals and priorities;
   - non-goals.
2. **Scope**
   - exact registered projects;
   - target and integration branches;
   - files, services, datasets, devices, or external systems in scope;
   - source-of-truth and evidence precedence.
3. **Authority**
   - actions the Operator may perform automatically;
   - actions that require approval;
   - actions that are always denied;
   - ownership boundaries for shared or conflict-prone state.
4. **Quality**
   - acceptance gates;
   - required reviewers or independent reproduction;
   - minimality and necessity gates;
   - dependency, public-interface, and compatibility policy;
   - compatibility, performance, security, and provenance constraints.
5. **Planning**
   - initial milestones and known dependencies;
   - whether the Operator may create derived work;
   - expected change surfaces and implementation latitude;
   - bounds on refactoring and opportunistic repair.
6. **Roster and routing**
   - required task capabilities and minimum quality tiers;
   - preferred, forbidden, or project-specific profiles;
   - independent-review and model-diversity rules;
   - stuck-work triggers, recovery-council membership, and quorum rules;
   - scarce-tier reserves and downgrade policy;
   - operation priority relative to other Operators.
7. **Resources**
   - worker and provider budgets;
   - wall-clock, attempt, token, and cost ceilings where applicable;
   - maximum active runs;
   - maximum worktree count and bytes;
   - minimum free disk;
   - artifact and worktree retention.
8. **Escalation**
   - conditions that require the owner;
   - permitted fallbacks;
   - retry budgets;
   - whether deadlines may apply to reversible decisions.
9. **Reporting**
   - digest cadence;
   - notification severity;
   - progress measures.
10. **Completion and maintenance**
   - observable completion conditions;
   - stop conditions;
   - optional steady-state maintenance invariants.

### 4.3 Authority matrix

The contract uses three explicit modes:

| Mode | Meaning |
|---|---|
| `auto` | The Operator may act and record the evidence. |
| `ask` | The Operator must create a decision and wait for approval. |
| `deny` | The Operator must not take the action under this contract. |

A recommended starting policy is:

| Action | Default |
|---|---|
| Read projects, tracker, history, and logs | `auto` |
| Create or refine in-scope work items | `auto` |
| Dispatch, retry, reroute, interrupt, or stop workers | `auto` |
| Reserve shared capacity and choose among contract-qualified profiles | `auto` |
| Temporarily degrade or quarantine a profile from objective health evidence | `auto` |
| Convene a bounded recovery council and run its permitted diagnostic | `auto` |
| Edit and commit within an isolated worker branch | `auto` |
| Run declared verification gates | `auto` |
| Merge a clean, reviewed branch after all declared gates | `auto` |
| Update tracker state after independently reproducing acceptance evidence | `auto` |
| Remove an eligible clean worktree after harvesting its durable state | `auto` |
| Broaden project, file, or product scope | `ask` |
| Change profile qualifications, contraindications, pool mappings, or permanent enabled state | `ask` |
| Add an unplanned dependency, service, public API, schema, migration, or compatibility layer | `ask` |
| Exceed the task's approved change surface or refactoring latitude | `ask` |
| Change source-of-truth or acceptance methodology | `ask` |
| Publish, deploy, release, message external people, or spend beyond budget | `ask` |
| Rewrite shared Git history or delete unique uncommitted work | `deny` |
| Disable a failing gate merely to make progress appear green | `deny` |
| Treat model consensus as authority to cross an `ask` or `deny` boundary | `deny` |
| Add speculative extension points, placeholder paths, or unrelated cleanup without a current goal | `deny` |

Projects may make merge automatic or approval-gated. Automatic merge should
only be offered when the target branch, merge strategy, ownership rules, and
gates are explicit.

### 4.4 Illustrative contract

This example shows the review surface, not a commitment to a storage encoding:

```text
Operator
  name: PREX3 fidelity and release readiness
  mode after goals: stop

Intent
  goal G1: close the approved PREX3 fidelity backlog
  goal G2: produce verified macOS and Windows distributables
  non-goal: redesign release behavior merely because it appears odd

Scope
  projects:
    - prex3-remaster
    - piu-evidence (evidence updates only)
  target branch: methodology-rework
  source of truth:
    running original > per-function oracle > static binary > RE notes

Authority
  dispatch/retry/reroute/interrupt: auto
  create derived in-scope work: auto
  edit/commit isolated branches: auto
  merge after declared review and gates: auto
  update tracker after acceptance: auto
  reclaim proven-eligible worktrees: auto
  change authentic behavior without binary evidence: ask
  add another project or broaden release scope: ask
  publish or send a build externally: ask
  rewrite shared history: deny
  delete dirty or uniquely uncommitted work: deny

Quality
  required gates:
    - native build and ctest
    - Python tests
    - real renderer tests
    - Godot smoke
  independent review:
    - authenticity or timing methodology changes
    - packaging before external release

Change discipline
  default: smallest coherent change that fits the existing architecture
  expected surface: recorded per work item
  new dependency/service/public API/schema/migration: ask unless goal names it
  speculative abstraction or compatibility layer without a caller: deny
  unrelated cleanup: deny
  heavy-model diagnosis does not broaden implementation authority

Resources
  max active workers: 4
  max active worktrees: 5
  max worktree/build bytes: 40 GiB
  minimum free disk: 100 GiB
  clean integrated tree: remove after harvest
  failed tree: retain through triage, then 24 hours
  dirty or pinned tree: never remove automatically

Routing
  minimum quality:
    architecture/integration: heavy
    normal feature work: generalist
    mechanical verified work: workhorse
  preserve:
    - 25% of heavy-tier weekly headroom for hard or recovery work
  reviewer:
    - do not reuse the implementer's exact profile for consequential review
  recovery council:
    trigger: repeated failure, verifier conflict, or no credible next action
    preferred: [fable, gpt-5.6-sol]
    minimum members: 2
    quorum: 2-of-2 on a specific in-scope next action
    disagreement: discriminating test, then one tie-breaker if budget permits
    max councils per work item without new evidence: 1
  downgrade below minimum quality: never

Escalate
  - authentic behavior conflicts with desired correction
  - acceptance methodology must change
  - unique state cannot be preserved automatically
  - retry or disk budget cannot be recovered automatically
  - an external release action is ready

Report
  digest: daily or on request
  notify immediately:
    - decision needed
    - goal accepted
    - hard resource pressure
    - operation achieved, stopped, or unable to recover
```

### 4.5 Derived work

The plan is rolling rather than frozen. An Operator may create derived work
automatically when all of the following hold:

- it is necessary to satisfy an approved goal or maintenance invariant;
- it remains inside project and authority scope;
- its risk and resource cost fit the contract;
- it has an objective acceptance gate;
- it records the parent goal and the evidence that caused it.

Feature expansion, changed product behavior, new external commitments, and
methodology changes require a contract amendment or decision.

"Cleaner," "more general," "future-proof," or "while we are here" is not
derived-work necessity. If an improvement can be deferred without preventing
the approved acceptance condition, the Operator leaves it out of the active
plan. It may record a bounded suggestion when useful, but does not generate a
new implementation campaign from it.

### 4.6 Approval and amendments

The owner approves a specific contract version and content hash. Activation
always names that version.

Natural-language feedback during an operation is classified as one of:

- evidence or correction inside the existing contract;
- reprioritization inside the existing goals;
- an operational instruction allowed by existing authority;
- a proposed contract amendment.

Only the last category changes the contract. The Operator presents a semantic
diff showing changed goals, scope, authority, budgets, gates, or stop
conditions. Approved amendments produce a new immutable version. In-flight
work whose assumptions changed is interrupted or invalidated explicitly.

## 5. Operation lifecycle

An operation has these top-level states:

| State | Meaning |
|---|---|
| `designing` | Contract is being discussed; no operational mutations. |
| `awaiting_approval` | A complete contract draft is ready. |
| `active` | The Operator is reconciling and may act. |
| `waiting` | No immediate action is ready; wake conditions are recorded. |
| `needs_decision` | One or more decisions are open; unaffected work may continue. |
| `paused` | No new actions may start; active actions settle or stop per policy. |
| `maintaining` | Goals are accepted and declared invariants remain active. |
| `achieved` | Goals are accepted and no maintenance obligation remains. |
| `stopped` | The owner or a terminal contract condition ended the operation. |
| `failed` | The controller cannot safely continue or reconstruct state. |

`needs_decision` is a presentation state, not necessarily a global execution
block. The operation remains live when independent ready work exists.

## 6. The reconciliation loop

The Operator implements a repeated observe–reconcile–act–verify loop.

### 6.1 Observe

Build a bounded snapshot of:

- current contract and amendment history;
- goal and work-item states;
- registered project availability;
- target branch heads and dirty-state indicators;
- active and recently settled runs;
- worker handoffs, questions, messages, and findings;
- recovery-council evidence packets, submissions, and discriminator results;
- gate results and integration status;
- roster policy, profile health, provider capacity, pool reservations, reset
  timing, and contract spend;
- worktree, build, artifact, and free-disk measurements;
- open decisions and their scopes;
- scheduled maintenance and wake conditions.

The snapshot contains references to full artifacts rather than embedding all
transcripts. Operator attempts receive only the evidence needed for the current
decision.

### 6.2 Reconcile

Compare actual state with contract state and identify:

- accepted goals and unmet acceptance conditions;
- ready work whose dependencies are satisfied;
- handed-off work awaiting review or integration;
- failed work within retry budget;
- repeated failure or verifier conflict that meets a recovery-council trigger;
- handed-off work that exceeds its change budget or introduces unjustified
  structure;
- work invalidated by new evidence;
- stale tracker, branch, or resource state;
- violated maintenance invariants;
- decisions or authority gaps.

### 6.3 Plan the next bounded wave

Choose a wave using capability fit, quality floors, independence, ownership,
profile health, shared-pool reservations, provider headroom, scarce-tier
reserves, change budgets, and disk budget. An active Operator does not try to
keep every model busy. It uses concurrency only when the work is independent
and the expected time saved is worth the additional integration and resource
cost.

Every wave records:

- selected work and rationale;
- dependencies and ownership locks;
- worker profiles and resource reservations;
- the routing decision and qualified fallback chain;
- the implementation latitude, expected change surface, and changes that
  require a decision;
- expected evidence;
- retry and review policy;
- the wake condition for the next Operator attempt.

### 6.4 Act

Possible actions include:

- create or refine a tracker item;
- dispatch a worker or independent reviewer;
- retry or resume a failed attempt;
- reroute work to a different profile;
- convene, collect, or synthesize a bounded recovery council;
- run a council-proposed discriminating test;
- interrupt an affected run with corrected evidence;
- harvest and inspect a handoff;
- run or delegate verification;
- integrate an accepted branch;
- update tracker and goal state;
- reclaim eligible resources;
- create a decision;
- schedule a time- or event-based wakeup.

Privileged actions are executed by a deterministic outer broker after
validating them against the active contract. A model should not gain broader
host authority merely because it is called an Operator.

### 6.5 Verify

The Operator independently checks evidence required by the contract. Worker
prose alone is never an acceptance signal.

Verification may include:

- reproducing commands and observed outputs;
- inspecting the diff and ownership scope;
- checking every new dependency, service, public interface, schema,
  migration, compatibility path, and abstraction for current necessity;
- confirming that unrelated cleanup and deferred architecture did not enter
  the patch;
- checking the branch base and integration result;
- running full or targeted gates;
- comparing against an oracle, fixture, capture, benchmark, or release;
- obtaining an independent review;
- verifying that durable artifacts exist outside an ephemeral worktree.

### 6.6 Continue, wait, escalate, or complete

Every attempt must end with one of four durable outcomes:

1. actions were scheduled or remain active;
2. the operation is waiting on named events or a named time;
3. one or more decisions are required and their blocking scope is recorded;
4. the contract's completion or stop conditions are satisfied.

An attempt cannot simply report a handoff and cause an active operation with
ready work to disappear.

## 7. Liveness and anti-churn invariants

The controller enforces these invariants independently of model judgment:

1. **Ready-work liveness**

   If the operation is active, ready work exists, authority is sufficient, and
   budgets allow it, at least one action must be active or queued.

2. **No false completion**

   A successful run cannot directly mark its work accepted.

3. **No silent scope drift**

   Every mutation maps to a goal, maintenance invariant, or approved
   amendment.

4. **Bounded retry**

   A task cannot repeat the same failure indefinitely. Failure fingerprints,
   attempt counts, and reroute history are durable.

5. **Single integration writer**

   Each project and conflict-prone resource has at most one integration owner.

6. **No invisible waiting**

   Waiting always names a wake condition, deadline, or decision.

7. **No resource leak by progress**

   Dispatch admission accounts for the worktree and build resources it is
   expected to allocate.

8. **No circular deliberation**

   A recovery council must produce a permitted action, a new evidence-gathering
   step, or a precise escalation. The same panel cannot debate the same
   unchanged evidence repeatedly.

If ready-work liveness is violated for longer than the configured threshold,
the controller schedules a self-audit attempt. If the audit cannot recover, it
creates an operational escalation rather than silently stalling.

## 8. Work acceptance and integration

### 8.1 Work-item states

Operator-managed work uses:

`proposed → ready → dispatched → running → handed_off → verifying → integrating → accepted`

Side states are:

- `blocked`
- `needs_decision`
- `failed_retryable`
- `failed_terminal`
- `needs_revision`
- `superseded`
- `cancelled`

The existing tracker may use a smaller vocabulary. The Operator retains the
more precise internal state and maps it to tracker columns without losing the
acceptance distinction.

### 8.2 Integration checkouts

The Operator should not assume the person's current checkout is clean or
available. For automatic integration, each active project gets at most one
reusable, Operator-owned integration checkout under a predictable,
resource-accounted path.

The integration checkout:

- is protected by a project integration lease;
- starts from the contract's target branch;
- receives reviewed worker commits in declared order;
- runs post-integration gates;
- advances the target branch only through the configured strategy;
- never overwrites unrelated user changes.

Cross-project changes are grouped as a change set. Their commits and
verification are prepared independently, while merge order and partial-failure
recovery are recorded because Git cannot provide an atomic transaction across
repositories.

### 8.3 Necessity and complexity gate

Before work can move from `handed_off` to integration, the Operator compares
the delivered change with its approved change budget. The gate asks:

- does every changed component trace to the current acceptance condition?
- does the patch use existing architecture where its semantics fit?
- is each new abstraction justified by present complexity rather than
  hypothetical reuse?
- is every new dependency, service, public interface, schema, migration,
  configuration surface, or compatibility path explicitly allowed and
  necessary?
- were unrelated cleanup, speculative features, placeholder branches, and
  future-proofing excluded?
- are errors, limits, and failure behavior implemented honestly?
- is focused verification present for the actual failure boundary?

The Operator records a compact complexity delta: affected components and each
new structural commitment. Raw lines changed are diagnostic context, not a
quality score; a tiny opaque patch may be worse than a clear larger one.

Exceeding the expected surface is not automatically a failure. The worker must
show why the smaller coherent alternative cannot meet the goal. If the larger
design is necessary and already within contract authority, the Operator
updates the work item's budget with recorded evidence and repeats review. If
it needs new authority, it creates one decision. Otherwise the handoff moves
to `needs_revision` with a constrained brief that removes the unnecessary
work.

High-reasoning profiles receive the same gate. The Operator may use them in
`diagnose_only` or `review_only` mode, then assign the resulting bounded patch
to the most suitable implementer. Model capability does not waive change
discipline.

## 9. Escalation model

### 9.1 What requires the owner

Escalate when:

- the requested action is `ask` or outside contract authority;
- two goals or constraints conflict;
- authoritative evidence is absent or contradictory and the choice changes
  product behavior;
- source-of-truth or acceptance methodology must change;
- the smallest implementation supported by evidence requires a structural
  commitment or change surface outside the approved budget;
- an irreversible, destructive, publishing, deployment, or external
  communication action is proposed;
- retry or resource budgets are exhausted after any contract-permitted
  recovery council;
- no eligible profile can meet the task's quality, access, and risk floor within
  the contract's wait and budget policy;
- the recovery council remains split after its bounded evidence test or
  tie-breaker, or its recommendation requires new authority;
- a roster qualification, contraindication, capacity-pool mapping, or
  permanent enabled state must change;
- a security, legal, credential, or provenance boundary is encountered;
- unique uncommitted work cannot be safely preserved automatically;
- completion requires a subjective judgment explicitly reserved for the
  owner.

### 9.2 What does not require the owner

Do not escalate merely to:

- choose among suitable worker profiles;
- reserve capacity, wait for a known reset, or reroute among qualified
  profiles inside the contract's deadline and budget;
- temporarily degrade, quarantine, probe, or restore a profile under the
  approved health policy;
- convene a contract-permitted recovery council and act on an in-scope quorum;
- reject an overengineered handoff, request a smaller in-scope revision, or
  use a strong profile in diagnosis-only mode;
- retry a transient failure inside budget;
- repair an in-scope test or implementation defect;
- make a reversible documented assumption;
- split or reorder work without changing goals;
- clean a proven-eligible worktree;
- report ordinary progress;
- ask whether the Operator should continue while ready in-scope work exists.

### 9.3 Decision shape

Every decision contains:

- one concrete question;
- why it is necessary now;
- the exact affected goal, work, and projects;
- evidence and relevant artifacts;
- two or more real options when alternatives exist;
- the Operator's recommendation and reasoning;
- a safe default only when the contract permits one;
- whether a deadline applies;
- what work is blocked;
- what work will continue meanwhile.

Irreversible or product-defining choices do not receive unattended defaults
unless the approved contract explicitly names that choice and default.

## 10. Reporting and owner interaction

### 10.1 Status is derived from durable state

Asking for an update should not require reconstructing a model conversation.
The control plane can immediately provide:

- goal-level progress based on accepted outcomes;
- work accepted since the owner's last-seen cursor;
- work being verified or integrated;
- active and queued runs;
- open decisions and their blocking scope;
- failed work and recovery actions;
- active recovery councils, their evidence hash, and unresolved disagreement;
- work returned for unnecessary complexity and any pending complexity
  exception;
- provider, token, cost, and disk budgets;
- roster degradation, shared-pool reservations, and reset-aware waits;
- worktree and artifact retention;
- the next intended wave;
- confidence and known risks.

A model may explain or answer follow-up questions, but the underlying status is
deterministic.

### 10.2 Notification policy

Recommended defaults:

- routine run completion: digest only;
- recovery within contract: digest only;
- goal accepted: notify;
- decision needed: notify;
- campaign-wide stall: notify;
- loss of the only profile qualified for ready work: notify;
- ordinary quota-aware reroute or bounded profile quarantine: digest;
- successful recovery council and resulting action: digest;
- recovery council still split after its bounded discriminator: notify;
- soft resource pressure successfully reclaimed: digest;
- hard resource pressure or unsafe cleanup candidate: notify;
- contract achieved or stopped: notify.

The owner may send evidence, a correction, reprioritization, pause, or proposed
amendment at any time. The Operator records how the input changed current
work, including which runs were interrupted or invalidated.

## 11. Roster, capacity, and routing management

Model selection is part of operational judgment. The Operator must balance
task fit, quality, tool access, reliability, latency, provider headroom, rate
limits, financial cost, and the opportunity cost of consuming a scarce tier.

Capacity optimizes among qualified profiles. It never defines qualification.
A weak or unsuitable model does not become eligible merely because it has the
largest remaining quota.

Reasoning strength also does not define implementation latitude. A heavy model
can be the best diagnostician and still be a poor choice for an unconstrained
routine patch. Routing selects both a profile and an actuation mode such as
`diagnose_only`, `review_only`, `bounded_patch`, or `general_implementation`.

### 11.1 Stable policy versus live state

Roster management separates:

1. **Operator-approved profile policy**
   - backend and exact model or backend default;
   - reasoning variant and context characteristics;
   - structured capabilities and intended task classes;
   - minimum/maximum task risk;
   - tool, sandbox, platform, and external-access properties;
   - explicit contraindications;
   - quality, latency, and cost classes;
   - capacity pools consumed;
   - manual enabled/disabled policy.
2. **Observed live state**
   - executable, authentication, and model-catalog availability;
   - current quota windows and reset times;
   - API balance and observed project/operation spend;
   - request, token, and concurrency limits where exposed;
   - active reservations and runs;
   - recent infrastructure health and launch reliability;
   - measured task outcomes by comparable task class;
   - temporary degraded or quarantined state.

The Operator may learn and propose policy changes, but it does not silently
rewrite owner-declared strengths, contraindications, or quality floors from a
small number of outcomes.

### 11.2 Structured capability model

A free-text `role` remains useful to people, but routing requires structured
fields. Example dimensions include:

- task class: architecture, investigation, implementation, mechanical edit,
  review, integration, visual, documentation;
- domain: binary analysis, frontend, systems, security-sensitive, data,
  infrastructure;
- reasoning/quality tier: workhorse, generalist, heavy, specialist;
- context: short, long, repository-scale;
- tools and access: browser, image, Docker, device, network, macOS UI,
  danger-full-access;
- output constraints: code-mode support, structured tool reliability,
  patch/commit ability;
- scope behavior: change-budget adherence, unnecessary-abstraction rework,
  and suitability for diagnosis-only versus implementation roles;
- known contraindications or project exclusions.

The task or worker brief declares requirements instead of hard-coding a model:

```text
routing requirements
  task class: investigation
  capabilities: binary-analysis, long-context
  minimum quality: generalist
  access: read-only project + reference corpus
  risk: high evidence sensitivity
  actuation: diagnose_only
  implementation output: smallest discriminating test, not a redesign
  reviewer diversity: different profile and preferably different model family
```

The router resolves those requirements to a launch profile at dispatch time.
The contract may still pin a profile where reproducibility or a known
specialty matters.

### 11.3 Capacity pools and shared limits

Provider identity is not a sufficient quota model. Several profiles may share
one plan, while another model exposed through the same backend may use a
separate allowance. The control plane represents explicit capacity pools and
the edges from profiles to pools.

Pool constraints may include:

- rolling or calendar usage windows;
- model-specific or account-wide windows;
- remaining reset credits;
- prepaid currency balance;
- requests or tokens per minute;
- maximum concurrent sessions;
- owner-defined daily/operation budgets;
- a protected reserve for recovery or high-tier work.

Capacity is a vector, not one comparable percentage:

- allowance windows are stock budgets that refill or reset;
- request and token rates are flow budgets that govern earliest safe start;
- concurrent-session limits are admission slots;
- currency and owner budgets are spend ceilings;
- protected reserves are policy constraints.

The scheduler evaluates every pool a profile consumes. It does not collapse
unlike windows into one provider score. Observations refresh before a wave,
after runs settle, after rate-limit or quota errors, and on a bounded cadence.
Provider-native reset times and rate-limit headers are retained with their
observation time and certainty.

Quota evidence is often incomplete. The controller preserves the provider's
native windows and certainty instead of pretending every percentage is
fungible. Unknown quota fails open for ordinary manual dispatch today; an
autonomous Operator instead applies a contract policy such as conservative
admission, bounded probe, or wait for owner guidance when the possible spend
is consequential.

### 11.4 Reservations and global arbitration

Capacity is user-wide, not project-local. All active Operators and manual
dispatches may draw from the same accounts. A user-level scheduler therefore
tracks:

- active runs by profile and pool;
- pending capacity reservations;
- predicted token/cost or qualitative burn band;
- operation priority and budget;
- reset-aware queue timing;
- reserved emergency/heavy-tier headroom.

Before fan-out, the Operator reserves both worker concurrency and every
relevant capacity pool. Reservations have an idempotency key, bound run, and
expiry. Actual usage reconciles the estimate when logs or provider windows
advance.

For stock budgets, a reservation holds an estimated burn band. For rate
budgets, admission computes an earliest launch time and smooths starts rather
than discovering the limit through a burst of failures. Concurrency pools use
atomic slots. A dispatch is admitted only if the whole vector succeeds;
partial reservations are released.

Where provider percentages cannot be translated into tokens, the scheduler
uses conservative bands rather than false precision: small, normal, heavy,
and unknown. Historical burn may improve the estimate while preserving an
uncertainty margin.

One operation must not silently drain capacity needed by another. Contracts
therefore include operation priority, maximum concurrent reservations, and
optional per-pool budgets. Global arbitration may delay low-priority routine
work until a reset while admitting a critical task that requires the same
pool.

Manual launches made through Orchestra use the same reservation path, with an
explicit owner override when desired. Sessions started outside Orchestra
cannot be controlled; observed provider usage is reconciled as external burn
and reduces later admissions rather than being misattributed to an Operator.

### 11.5 Eligibility and selection

Routing is a two-stage process.

First, apply hard eligibility filters:

- profile is enabled and not quarantined;
- backend/model availability is not proven unavailable;
- required capabilities, platform, tools, and access are present;
- task quality and risk floors are met;
- task context fits the profile;
- project and contract exclusions are satisfied;
- required reviewer independence is satisfied;
- hard quota, concurrency, spend, and reserve constraints allow admission.

If no profile remains, wait, reroute the plan, or escalate according to the
contract. Never silently downgrade below the quality floor.

Second, rank eligible profiles using:

- task and domain fit;
- expected probability of accepted work, including likely rework;
- observed change-budget adherence for comparable implementation tasks;
- current health and recent infrastructure reliability;
- marginal quota/cost burn;
- scarcity and opportunity cost;
- latency and current load;
- reset timing;
- diversity value for independent review.

The goal is expected accepted-work efficiency, not cheapest tokens, highest
headline intelligence, most remaining capacity, or equal roster utilization.

### 11.6 Tier preservation and downgrade behavior

The router should preserve scarce high-quality capacity for work that needs
it. A contract may state:

- reserve 25% of a heavy pool for hard debugging, integration, or recovery;
- route mechanical, oracle-checkable work to qualified workhorses;
- allow a stronger tier to take routine work only when its reserve remains
  healthy and latency matters;
- queue low-priority work until reset rather than exhaust the only qualified
  heavy profile;
- prohibit downgrade for architecture, security, destructive migration, or
  acceptance review.

Fallback chains are computed from eligible profiles, not from provider
headroom alone. When quota is exhausted mid-run, the Operator preserves the
session and work state, then resumes on the same profile after reset or
reroutes only when the backend/session and task semantics make that safe.

### 11.7 Recovery councils and quorum

Ordinary retry should not be the final step before involving the owner. A
contract may reserve strong-model capacity for a recovery council when:

- the same failure fingerprint survives a changed strategy or qualified
  reroute;
- an implementer and verifier disagree about the cause or adequacy of a fix;
- the Operator has ready work but cannot produce a credible next action;
- several plausible diagnoses imply materially different implementations;
- the task crosses a contract-defined consequence or uncertainty threshold.

The council is deliberately composed from the best qualified profiles
available, not the cheapest remaining profiles. For a difficult coding or
architecture problem, that could currently mean independent reviews from
Fable and GPT-5.6-sol. The roster policy names capability and quality
requirements plus preferred profiles so the mechanism survives model and
profile changes.

Council procedure:

1. Freeze one evidence packet containing the question, relevant artifacts,
   failed approaches, constraints, and actions still permitted.
2. Give it independently to at least two qualified, preferably
   different-family profiles. Each first-pass reviewer is blind to the other
   conclusions to reduce anchoring.
3. Require structured output: diagnosis, cited evidence, falsifiable
   hypotheses, proposed discriminating tests, recommended next action,
   smallest viable implementation surface, explicitly deferred ideas,
   confidence, and known risks.
4. Compare the outputs. Agreement counts only when the members independently
   support the same specific action for compatible reasons, not when their
   prose merely sounds similar.
5. If they disagree, prefer a cheap discriminating test. If evidence cannot
   distinguish them and budget permits, ask one independent tie-breaker who
   sees the evidence and both anonymized positions.
6. Execute the agreed action only if it is already within contract authority,
   then apply the normal verification and acceptance gates.
7. Otherwise create a decision for the owner containing the competing
   diagnoses, evidence, attempted discriminator, and recommendation.

For two members, the default technical quorum is `2-of-2`. A configured third
member permits `2-of-3`, but a vote alone never establishes factual
correctness. Objective tests and source-of-truth evidence outrank model
agreement. Consequential review still occurs after the resulting
implementation; the council's diagnosis is not acceptance of the work. The
Operator process that compares or summarizes the submissions does not count
as another vote.

Council members default to `diagnose_only`: they may recommend a narrow patch,
but a quorum for "redesign the subsystem" is not a usable action unless the
approved task already grants that latitude. When their diagnosis is valuable
but their proposed implementation is too broad, the Operator extracts the
evidence-supported minimal step and assigns it under a fresh bounded brief.

Councils are bounded by member count, token/cost, wall-clock time, and a
maximum number per failure fingerprint. A new council requires materially new
evidence. Its heavy-tier reservations are acquired through the normal global
scheduler, and unrelated lanes continue while it runs.

The durable council record includes the frozen evidence hash, member profiles,
blind first-pass outputs, comparison, tests, vote or abstention, synthesis,
action, cost, and eventual outcome. This supports later calibration and makes
"the models agreed" auditable rather than rhetorical.

### 11.8 Health, degradation, and quarantine

Availability, capacity, and health are separate:

| State | Meaning |
|---|---|
| `available` | Launch evidence and policy permit routing. |
| `unknown` | Evidence is incomplete; contract determines admission. |
| `degraded` | Usable with a known reliability, latency, or tool problem. |
| `quarantined` | Temporarily excluded after repeated infrastructure failures. |
| `disabled` | Owner policy excludes the profile. |
| `unavailable` | Backend, authentication, or model evidence proves it cannot run. |

Health signals include:

- launch and authentication failures;
- zero-output stalls;
- malformed backend event streams;
- provider/rate-limit errors;
- missing required tools;
- repeated failure to produce the handoff contract;
- gate and rework outcomes, interpreted in task context.

A difficult task failing is not automatically evidence that a model is
unhealthy. Outcome statistics must be stratified by task class, risk, and
review standard. The historical PIU corpus can establish baselines, but it
contains selection bias because the human orchestrator already routed tasks
deliberately.

Overengineering is also a task-fit and actuation-mode signal, not an
infrastructure-health failure. Repeated unnecessary scope on routine patches
may make a profile less attractive for `general_implementation` while it
remains excellent for `diagnose_only` or difficult architecture review.

Quarantine is bounded and explainable. It records the trigger, cooldown,
probe condition, and owner override. Successful probes can restore service;
policy-disabled profiles never self-enable.

### 11.9 Routing record and explainability

Every dispatch records:

- task routing requirements;
- actuation mode and approved change budget;
- profiles considered;
- hard exclusions and reasons;
- capacity snapshot and certainty;
- reservations made;
- selected profile and why;
- fallback chain;
- expected review policy.

The normal digest need not narrate every choice. The owner can ask "why this
model?" and receive the exact contemporaneous decision rather than a
post-hoc explanation.

Notable routing events belong in the digest:

- a preferred profile was unavailable or quarantined;
- work waited for a reset;
- a reserve protected scarce capacity;
- a task was rerouted after quota or health failure;
- no model met the quality floor;
- a broad handoff was constrained or rerouted for unnecessary complexity;
- actual burn materially exceeded the reservation.

### 11.10 Roster management surface

The roster UI should show, per profile:

- backend, exact model/variant, and intended roles;
- capabilities, contraindications, and access;
- enabled and observed health state;
- capacity pools and the tightest live window;
- reset time, active runs, and reservations;
- recent comparable-task acceptance, rework, latency, and burn;
- change-budget adherence and preferred actuation modes by task class;
- current operation assignments;
- the last routing or quarantine explanation.

Owner actions include:

- add or edit a launch profile;
- qualify it for task classes;
- pin or forbid it for a project/operation;
- disable, quarantine, probe, or restore it;
- map it to capacity pools;
- set reserves and per-operation budgets;
- approve a proposed learned policy change.

Secrets, account identifiers, and raw credential material remain server-side
and do not enter the roster wire format.

### 11.11 Bootstrap and calibration

The first roster migration should not ask the owner to reconstruct years of
model judgment in a blank form:

1. import existing launch profiles and role text;
2. infer a draft capability, contraindication, and pool map;
3. show uncertainty and conflicting evidence explicitly;
4. have the owner approve or correct the structured policy once;
5. run the new router in shadow mode beside ordinary human selection;
6. record owner overrides as evidence, with an optional short reason;
7. propose batched policy changes only after enough comparable outcomes.

An override affects the current dispatch immediately but does not silently
become permanent policy. The owner can promote it into a versioned roster
amendment. This lets the system acquire the operator's selection instincts
without turning historical correlation or one successful run into authority.

## 12. Worktree and disk stewardship

### 12.1 Admission control

Before dispatching work that needs an isolated checkout, the controller checks:

- current free disk;
- contract minimum free disk;
- current active and retained worktree count;
- measured worktree and build bytes;
- expected project-specific provisioning cost;
- whether a reusable integration or analysis checkout exists.

At the soft limit, the controller first reclaims eligible resources and
reduces concurrency. At the hard limit, it stops new disk-allocating work. It
only escalates if safe automatic reclamation cannot restore the contract
margin.

### 12.2 Worktree lease states

`reserved → provisioning → active → harvesting → retained_or_eligible → removing → removed`

Exceptional states:

- `blocked_dirty`
- `pinned`
- `orphaned`
- `cleanup_failed`

Each lease records:

- project and run;
- canonical worktree path;
- branch and HEAD;
- owner and active descendants;
- creation and last-use times;
- measured logical bytes;
- clean/dirty/untracked status;
- handoff and artifact harvest status;
- verification and integration disposition;
- retention deadline and pin reason.

### 12.3 Removal proof

Automatic worktree removal requires positive proof that:

1. no active run or descendant owns the tree;
2. the canonical path is the exact registered worktree path;
3. branch and HEAD are durably recorded;
4. the tree is clean, or every unique change has been archived as a
   checksummed patch/bundle under an explicitly permitted policy;
5. handoff, logs, and declared artifacts have been harvested;
6. the work is accepted, rejected, superseded, or past its allowed
   post-triage retention;
7. the tree is not pinned;
8. the target branch or retained worker branch preserves committed work.

Removal uses `git worktree remove` followed by `git worktree prune`. Raw
recursive deletion is not an Operator cleanup mechanism.

Branch deletion is a separate, later policy. Removing a clean worktree does
not require deleting its branch.

### 12.4 Retention defaults

Recommended starting policy:

| Resource | Default |
|---|---|
| Active worker tree | retain |
| Handed-off tree awaiting verification | retain |
| Accepted and integrated clean tree | remove immediately after harvest |
| Clean committed branch awaiting merge | tree may be removed; branch retained |
| Failed/killed tree | retain through Operator triage, then 24 hours |
| Dirty or untracked unique state | never auto-delete |
| Pinned tree | retain until explicitly unpinned |
| Build directory after accepted gate | remove unless declared as an artifact |
| Durable logs, briefs, handoffs, manifests | retain according to operation policy |

### 12.5 Provisioning and build reuse

Projects may declare a verified post-create provisioning hook. The hook is
part of project configuration, runs from the outer supervisor rather than a
worker prompt, and must be idempotent.

Large immutable inputs should use safe copy-on-write clones or a verified
content-addressed cache when project path-containment rules permit it.
Symlinks must not be substituted where the project deliberately requires a
real in-tree path.

Build caches may be shared only when the project declares them safe for
concurrent use and cache identity includes every input that affects
correctness. Large per-run build products are ephemeral unless the handoff
declares specific artifacts for harvest.

## 13. Persistence and controller architecture

### 13.1 User-level control plane

Operator state belongs in a user-level control-plane database, not in a
pretend project at a non-repository workspace root.

The control plane stores:

- operators and operations;
- immutable contract versions;
- goals and internal work state;
- event cursors across registered project databases;
- operator attempts and controller leases;
- decisions and answers;
- budgets and resource samples;
- roster policy versions and profile qualifications;
- capacity pools, live windows, reservations, and health/quarantine records;
- routing requirements, decisions, and observed outcomes;
- recovery-council evidence packets, independent opinions, synthesis, and
  outcomes;
- work-item change budgets, complexity deltas, revisions, and approved
  exceptions;
- worktree and artifact leases;
- scheduled wakeups;
- idempotency records.

Project databases remain authoritative for their runs, messages, briefs, logs,
and local teams. Operator references use `(project_id, run_id)` rather than a
bare run number.

The Operator control plane needs an immutable logical project key plus a
current binding to the registered project root. Orchestra's present
path-derived project ID is suitable for routing to a current root but changes
when a repository moves. Rebinding a moved project must preserve operation,
goal, work, decision, and resource history without accidentally adopting a
different repository at the old path.

Work-item references are similarly qualified by their workspace or project
authority. A bare identifier such as `W-0001` is not globally unique.

The human-readable contract and status can be exported to Markdown. Runtime
state does not have to be committed into every participating repository.

The database and exports are private operator state: owner-only file
permissions, no persisted credentials or provider session secrets, bounded
text fields, and credential redaction equivalent to checkpoint/takeover are
required.

Capacity and profile health are shared across operations. A central scheduler
must see reservations from every Operator and, where practical, ordinary
manual dispatches. Project-local usage views remain useful, but they cannot be
the authority for a user-wide provider account.

### 13.2 Eventual consistency across project databases

There is no cross-database transaction. The controller therefore:

- assigns every requested mutation an idempotency key;
- writes intent before executing an external action;
- observes project state after execution;
- marks the action applied only when the expected state is visible;
- safely retries incomplete actions;
- records compensating action where atomicity is impossible.

### 13.3 Controller lease and recovery

At most one controller generation holds an active lease for an operation.
Heartbeat expiry permits recovery by a new process or machine. Recovery reads
the contract, unapplied intents, project event cursors, active supervisors,
and resource leases before taking new action.

Provider session references may improve continuity but are never the sole
source of Operator state.

### 13.4 Bounded model attempts

Operator attempts receive:

- the active contract version;
- a bounded reconciliation snapshot;
- relevant evidence references;
- the actions permitted in this attempt;
- the previous attempt's durable outcome.

The outer controller validates structured action requests. This prevents a
model from broadening its own authority and makes replay possible.

## 14. Mapping to current Orchestra

The design reuses current primitives:

| Existing primitive | Operator use |
|---|---|
| Project registry | Explicit project allowlist |
| Project SQLite databases | Run/message/feed source |
| Configured launch profiles and role text | Stable roster-policy seed |
| Backend/model discovery | Live launch availability evidence |
| Provider runway collectors | Quota-window and balance evidence |
| Run logs and usage summaries | Actual burn, reliability, and outcome evidence |
| Dispatch and native child runs | Bounded worker execution |
| Child-settle wakeups | Event-driven lead continuation |
| Resume, queue, and interrupt | Recovery and evidence routing |
| Opt-in questions with fallback | Worker-level ambiguity handling |
| Checkpoint/takeover | Attempt context recovery pattern |
| Usage runway | Provider budget input |
| Dashboard and iOS app | Status and decision surfaces |
| Slash-work integration | Durable user-facing work tracker |

The Operator implementation turns these inputs into a scheduler. Ordinary
manual dispatch remains warn-only for compatibility, while Operator dispatch
uses an approved structured roster, hard eligibility filters, live launch
evidence, durable shared-pool reservations, heavy-tier reserves, manual-run
load, health state, and a recorded fallback chain. Unknown capacity is treated
conservatively rather than promoted to headroom.

Implemented core components are:

- Operator control database and schema;
- contract designer, validator, and versioner;
- event collector and reconciliation scheduler;
- structured capability profiles and qualification policy;
- shared capacity-pool graph, reservations, and global arbitration;
- eligibility/ranking router with durable explanations and fallbacks;
- profile health, degradation, quarantine, and probe lifecycle;
- independent recovery-council coordinator and quorum evaluator;
- change-budget validator and necessity-review gate;
- structured action broker and authority checker;
- work acceptance and integration state;
- project/integration locks;
- resource measurement, leases, and worktree garbage collection;
- decision and digest APIs;
- historical replay harness.

### 14.1 Implemented control surfaces

The `orchestra operator` CLI is the canonical owner surface:

- `template`, `validate`, `draft`, `approve`, and `export` manage immutable,
  hash-bound contracts;
- `roster bootstrap|draft|approve|show` manages model qualifications,
  contraindications, shared quota pools, and owner approval;
- `start --mode shadow|live`, `tick`, `run`, `pause`, `resume`, and `stop`
  manage durable operation lifecycles;
- `operations`, `status`, `decisions`, and `answer` provide concise progress
  and escalation handling;
- `replay import-archive|import-live|list|show` imports metadata-only evidence
  and reconstructs deterministic historical state at a UTC clock bound.

The shared dashboard exposes the same owner data through
`GET /api/operators`, `GET /api/operators/{id}`, and a JSON-only
`POST /api/operator-decisions/{id}/answer`. These endpoints intentionally do
not expose provider credentials, raw historical transcripts, or arbitrary
filesystem selection.

Live activation requires at least one required, direct, bounded verification
command for every project. Shell and environment launchers are rejected.
Verification runs in its own process group, output is bounded and redacted,
and timeouts terminate the group. Integration is serialized by a renewable
project lease. Worktree cleanup is a separate authority-checked action and
requires a clean tree plus Git ancestry proof that no unique state remains.

The PIU evidence corpus is a standing replay fixture: the preservation ZIP
imports as 508 runs, 2,075 messages, and 743 feed events, while consistent
read-only snapshots of the six current project databases import 117 runs.

## 15. Failure behavior

The Operator must remain honest under:

- controller or host restart;
- worker supervisor death;
- provider exhaustion or model disappearance;
- stale, unavailable, or contradictory quota evidence;
- several operations reserving one shared provider pool;
- degraded or flaky profile behavior;
- unavailable council members, correlated model errors, or unsupported
  apparent consensus;
- technically correct but unnecessarily broad implementations;
- speculative dependencies, abstractions, services, compatibility paths, or
  cleanup entering a handoff;
- project move or unavailable registered root;
- dirty target checkout;
- branch drift and merge conflict;
- failing or flaky acceptance gates;
- worker completion without a handoff;
- corrupt or incomplete log;
- disk pressure during provisioning or build;
- cleanup failure;
- contract amendment during active work.

Failure should reduce authority and concurrency rather than silently weaken
evidence. When state cannot be reconstructed safely, the operation pauses the
affected scope and creates a precise decision or operational alert.

### 15.1 Representative PIU cases

| Observed case | Required Operator behavior |
|---|---|
| Owner reports that 144 Hz behavior differs from the release | Record higher-priority evidence, interrupt only affected timing lanes, and invalidate superseded verification. |
| Worker finishes but its card remains backlog or its branch is unreviewed | Move only to `handed_off`; independently verify, integrate, and then update the tracker. |
| Several workers need the same untracked references and submodule setup | Run the project provisioning hook once per admitted tree and verify it before dispatch. |
| Worker times out after producing partial useful work | Harvest the state, classify retryability, and resume or reroute within budget without asking "continue." |
| A provider or model becomes unavailable | Recompute the wave from task qualification, live health, and capacity; preserve task/session state and reroute when compatible. |
| Several Codex or Claude profiles share a plan while another model has a separate allowance | Reserve explicit capacity pools rather than treating backend or provider name as the quota boundary. |
| The provider with most headroom is a poor fit for the task | Exclude it on capability or quality grounds; capacity only ranks profiles that already qualify. |
| A normally useful model repeatedly stalls or emits malformed events | Quarantine the launch profile with a recorded trigger and cooldown; continue with qualified fallbacks. |
| Only a scarce heavy tier can safely perform the task | Preserve or wait for its reserve rather than silently downgrading to a workhorse. |
| Repeated implementation and review attempts remain stuck | Freeze the evidence and convene independent strong-model reviews, such as Fable and GPT-5.6-sol; act on an evidence-supported in-scope quorum or escalate the exact disagreement. |
| A strong model solves a narrow defect by introducing a framework, generalized API, and unrelated cleanup | Return the handoff for a bounded revision; require evidence that each structural addition is necessary before integration. |
| Two lanes would modify a shared manifest or methodology | Grant one ownership lease, order the work, or reserve the shared change for integration. |
| Runtime evidence requires unavailable display, cabinet, or hardware | Continue independent static work, record the unmet gate honestly, and escalate only if the contract requires that evidence now. |
| Owner feedback changes a product or authenticity decision | Present a contract amendment or scoped decision rather than treating it as casual prompt text. |
| Workspace contains several Git repositories under one non-repository root | Coordinate by registered project IDs and a user-level operation; never initialize the workspace as a fake project. |
| Hundreds of multi-gigabyte worktrees accumulate | Enforce admission budgets, harvest durable state, and remove only proof-eligible trees while retaining required branches. |
| Operator process or host dies | Recover through the controller lease, event cursors, unapplied intents, and resource records without duplicate dispatch. |

## 16. Historical replay and live dogfood

### 16.1 Replay corpus

Use the 508-run archive plus the 117 post-split project databases. Replay
events in timestamp order without exposing future events to the policy under
test.

Supplement database events with Git and tracker history where the original
interactive orchestrator conversation was not persisted.

### 16.2 Metrics

Measure:

- next-action agreement with successful historical behavior;
- unnecessary escalation rate;
- missed consequential decision rate;
- hard-eligibility violations, which must remain zero;
- routing outcome by comparable task class, risk, and acceptance standard;
- unnecessary quality-tier upgrades or forbidden downgrades;
- accepted work per unit of each scarce capacity pool;
- quota-exhaustion incidents avoided and caused;
- reservation error, overcommit, expiry leakage, and reset-time prediction
  error;
- wait time attributable to quota, health, reserve, and concurrency policy;
- profile-health false quarantine and missed-quarantine rates;
- reviewer model-family diversity where the contract requires it;
- stuck-work recovery rate after a council;
- councils avoided, owner escalations avoided, and accepted outcomes per
  council budget;
- false consensus, unresolved split, and redundant-council rates;
- change-budget exception and `needs_revision` rates by profile and task class;
- unnecessary dependency, service, public-interface, schema, migration,
  compatibility-layer, and speculative-abstraction escapes, which should be
  zero;
- accepted outcomes after a broad proposal was reduced to a smaller coherent
  change;
- time from worker handoff to accepted work;
- successful recovery after failed/killed/timed-out attempts;
- ownership or merge-conflict violations;
- false acceptance and stale tracker state;
- redundant provisioning or repeated briefing;
- peak simulated worktree bytes;
- unique work lost by cleanup, which must remain zero;
- progress achieved per worker and capacity-pool budget.

Raw diff size is retained for investigation but is not optimized as a target.
The useful measure is unnecessary structural commitment and rework relative to
the accepted behavior, not the fewest lines.

Replay must compare the router with the human's actual choice without assuming
that the historical choice was either optimal or interchangeable. Where the
archive lacks contemporaneous quota or roster state, mark the routing result
`indeterminate` instead of manufacturing headroom. Live shadow mode is needed
to evaluate reservations, reset-aware scheduling, and cross-operation
contention accurately.

### 16.3 Rollout

1. **Replay only**

   No live mutations. Establish policy and cleanup safety on the corpus.

2. **Shadow operation**

   Observe live PIU projects, capture contemporaneous capacity, and propose
   actions, routing decisions, and cleanup candidates. A human orchestrator
   remains authoritative; overrides calibrate policy.

3. **Bounded execution**

   Allow dispatch, recovery, and reporting; require approval for integration
   and deletion.

4. **Gated autonomy**

   Enable verified integration, tracker transitions, and proof-based cleanup
   under the approved contract.

5. **Maintenance mode**

   Enable declared recurring invariants after goal acceptance.

## 17. Initial implementation slices

### Slice A: contract and replay foundation

- user-level Operator database;
- contract draft, validation, approval, and versioning;
- change-discipline defaults and task change-budget schema;
- project references by stable project ID;
- read-only event import from archived and live databases;
- import of current launch profiles, role text, availability, and quota
  snapshots as explicitly uncertain seed evidence;
- replay clock and structured proposed actions;
- deterministic status/digest renderer.

Acceptance:

- an approved PIU contract can be reconstructed byte-for-byte;
- replay never mutates source databases or repositories;
- every proposed action cites contract authority and evidence;
- status is useful without model transcript access.

### Slice B: roster, capacity, and routing control plane

- structured profile capabilities, qualifications, contraindications, and
  owner-policy versions;
- explicit many-to-many profile-to-capacity-pool mappings;
- native quota windows, certainty, reset timing, balances, and concurrency
  constraints;
- user-wide idempotent reservations and reconciliation against actual usage;
- hard eligibility filters followed by task-fit and opportunity-cost ranking;
- diagnosis/review/patch actuation modes and comparable-task scope-discipline
  signals;
- heavy-tier reserves, operation budgets, and reset-aware queueing;
- protected recovery-council capacity and model-diversity constraints;
- durable routing explanations and fallback chains;
- degradation, quarantine, probe, and owner-override lifecycle.

Acceptance:

- profiles sharing an account-wide plan consume one pool while profiles with
  genuinely separate allowances do not block one another;
- two concurrent Operators cannot over-admit the same known capacity;
- no amount of spare quota makes a profile eligible below the task's quality,
  access, risk, or project-policy floor;
- mechanical work uses a qualified workhorse while scarce heavy capacity is
  preserved for an eligible hard task in the same replay;
- a malformed-event or launch-failure pattern quarantines only the affected
  profile and records an inspectable recovery condition;
- every dispatch can answer why the selected profile beat each practical
  alternative using the contemporaneous snapshot;
- a `diagnose_only` assignment cannot mutate project source, regardless of
  profile strength;
- owner pin, forbid, reserve, and manual override policies are deterministic
  and survive restart.

### Slice C: durable single-project control loop

- controller lease and heartbeat;
- event/time wakeups;
- bounded Operator attempts;
- dispatch, retry, reroute, interrupt, and stop broker actions;
- broker enforcement of actuation mode and approved change surface;
- frozen-evidence recovery councils, independent submissions, quorum
  evaluation, and bounded tie-breaking;
- liveness and retry invariants;
- first-class decisions.

Acceptance:

- one approved operation completes at least three waves and ten work items
  without a human "continue" instruction;
- controller restart resumes without duplicate dispatch;
- unrelated work continues while one lane awaits a decision;
- a repeated hard failure convenes two qualified strong profiles without a
  human prod, executes an agreed permitted discriminator, and records the
  outcome;
- a split council runs at most the configured discriminator and tie-breaker
  before producing a precise owner decision;
- repeated failure exhausts a declared retry budget rather than looping.

The schema should support multiple projects from the beginning even if the
first execution slice activates one project at a time.

### Slice D: acceptance, integration, and resource lifecycle

- handed-off/verifying/integrating/accepted states;
- independent evidence reproduction;
- change-budget comparison, complexity-delta records, and necessity review;
- project integration leases and checkouts;
- artifact harvesting;
- worktree/build measurement;
- cleanup dry-run and proof records;
- guarded worktree removal.

Acceptance:

- worker `done` never directly produces accepted work;
- an otherwise correct broad patch with an unnecessary dependency and
  abstraction moves to `needs_revision`, while its bounded replacement can
  pass;
- unapproved structural commitments cannot reach integration;
- target branches advance only after declared gates;
- clean integrated trees are reclaimed automatically;
- dirty, pinned, or unique uncommitted state is never deleted;
- simulated PIU peak disk remains inside the contract budget;
- every removal has an inspectable proof record.

### Slice E: multi-project operation and owner surfaces

- change sets spanning registered projects;
- cross-project dependency and integration ordering;
- dashboard and iOS operation overview;
- roster, pool, reservation, and routing-explanation views;
- decision answer and contract-amendment flows;
- last-seen digests and notifications;
- explicit maintenance mode.

Acceptance:

- one operation coordinates at least two PIU repositories without treating
  the workspace root as a Git repository;
- status and decisions remain actionable from the remote UI;
- contract changes interrupt only affected work;
- achieved goals do not cause unapproved feature generation.

## 18. Recommended product decisions

These defaults best match the stated goal of high autonomy with meaningful
owner control:

1. **Use a durable operation, not one long agent session.**
2. **Draft contracts from existing project evidence and ask only material
   questions.**
3. **Allow derived work automatically when it is traceable and in scope.**
4. **Treat merge as automatic only behind explicit project gates and a
   single integration lease.**
5. **Keep publishing, external communication, destructive history changes,
   and scope expansion approval-gated.**
6. **Make proof-based worktree cleanup automatic and part of normal
   acceptance.**
7. **Never automatically delete dirty, pinned, or uniquely uncommitted
   state.**
8. **Architect for multi-project operations immediately, then activate the
   first implementation incrementally.**
9. **Make task qualification a hard gate; optimize quota, cost, and latency
   only among models that are suitable for the work.**
10. **Represent actual shared capacity pools and user-wide reservations
    explicitly; backend names and remaining percentages are not schedulers.**
11. **Preserve scarce strong-model capacity for work that needs it, and never
    silently downgrade below a contract quality floor.**
12. **Keep owner-declared model strengths, contraindications, and permanent
    roster state as versioned policy; health automation may quarantine but
    must not silently rewrite that policy.**
13. **Record each routing choice at dispatch time so "why this model?" has a
    factual answer.**
14. **Before escalating difficult technical work, convene a bounded,
    independent council of the strongest qualified diverse models; require
    evidence and normal gates, not consensus alone.**
15. **Treat reasoning depth and implementation latitude as separate controls;
    use strong models for diagnosis without granting them permission to
    redesign.**
16. **Default to the smallest coherent change that satisfies the evidence;
    require explicit necessity for every new structural commitment and keep
    unrelated cleanup out.**
17. **Use deterministic status and budgets; use models for judgment rather
   than bookkeeping.**
18. **After goals are accepted, continue only under explicitly approved
    maintenance invariants.**

## 19. North-star acceptance scenario

The owner discusses a PREX3 outcome with the design agent. Orchestra reads the
existing binary-as-specification doctrine, project gates, worktree
provisioner, tracker, and current branches. It drafts a contract saying that
in-scope implementation, verified merge, tracker updates, worker recovery,
and clean-worktree cleanup are automatic; authenticity choices, scope
expansion, publishing, and unique-data deletion require approval.

The owner approves it once.

The Operator:

1. reconciles stale completed work and current branches;
2. classifies each ready task by capability, quality, access, risk, and review
   requirements;
3. reads live model health and explicit shared-capacity pools, then reserves
   enough capacity without consuming the protected recovery tier;
4. creates a dependency-aware next wave;
5. provisions only the worktrees admitted by the disk budget;
6. routes mechanical oracle-checkable work to qualified workhorses, hard
   investigation to a suitable heavy profile, and consequential review to an
   independent profile;
7. dispatches the wave with a recorded reason and fallback for each choice;
8. routes new owner evidence to the affected lane without stopping others;
9. quarantines a flaky profile and reroutes compatible work, while waiting
   through a known quota reset when downgrade would violate the quality floor;
10. retries a transient provider failure within budget;
11. when changed strategies still fail on a difficult lane, freezes the
    evidence and asks Fable and GPT-5.6-sol for blind, diagnosis-only reviews;
12. extracts their smallest agreed discriminating test or bounded patch,
    rejects speculative redesign and unrelated cleanup, and presents only a
    still-unresolved evidence or authority conflict to the owner;
13. verifies and integrates accepted branches through its integration
   checkout;
14. updates the tracker from evidence;
15. harvests artifacts and removes eligible multi-gigabyte worktrees;
16. reconciles reservations with actual usage and schedules the next wave
    without waiting for "continue";
17. sends a compact digest including material routing, reserve, or council
    events;
18. asks the owner only when an authentic-release behavior conflicts with a
    desired correction not resolved by the contract.

If the host restarts, a new controller generation reconstructs the operation
from durable state and resumes without duplicating runs or leaking ownership.
When the goals are accepted, the Operator either enters the explicitly
approved maintenance mode or stops as achieved.

That is the behavior this design must make ordinary.
