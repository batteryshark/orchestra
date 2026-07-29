# Orchestra Operator

Status: deterministic controller architecture

Audience: maintainers and early operators

## 1. Product contract

An Operator accepts one owner-approved contract and one owner-approved roster
snapshot. It then does exactly four things:

1. waits until the approved execution lane is available;
2. dispatches one bounded, contained worker for ready work;
3. verifies and, when required, independently reviews the sealed result;
4. integrates accepted results and advances the dependency graph.

It does not redesign its contract, rewrite its roster, create recovery
committees, reinterpret congestion as technical failure, or claim to be
running before a worker exists.

The owner-facing promise is:

> The Operator works inside its lane, quietly waits for environmental
> backpressure, asks about genuine authority or evidence decisions, and reports
> verified completion.

## 2. Boundaries

The control plane is split into components with narrow authority.

### Owner

The owner may:

- create and approve immutable contract versions;
- create and approve immutable roster versions;
- start, pause, resume, or stop an operation;
- answer a genuine decision.

An operation never approves or amends those objects. Starting a replacement
operation against a newer version is an owner action, not a recovery strategy.

### Controller

The controller is deterministic code. It:

- holds the single operation lease;
- evaluates admission and runtime state;
- records explicit state and reason;
- schedules ready work;
- invokes the broker;
- applies fixed retry and verification rules.

No model decides whether a checkout is dirty, capacity is full, a process is
alive, a byte ceiling was crossed, or a dependency is complete.

### Worker

A worker receives:

- one writable project;
- one isolated standalone clone;
- repository-relative include and exclude scope;
- immutable read snapshots for declared dependencies;
- a fixed change and disk budget;
- direct verification commands.

A worker cannot spawn children, continue its session outside the controller,
change governance, or write another project.

### Broker

The trusted broker provisions clones, seals worker output into a commit,
measures the change, invokes verifiers, integrates the exact reviewed commit,
and reclaims only state proven safe to remove.

## 3. Operation states

| State | Meaning |
|---|---|
| `queued` | Approved state exists, but no contained worker is active. |
| `waiting` | Environmental backpressure currently prevents progress. |
| `running` | At least one contained implementation or review run exists. |
| `verifying` | Sealed output is being checked, reviewed, or integrated. |
| `needs_decision` | A genuine owner decision is required. |
| `paused` | The owner paused the operation. |
| `achieved` | Every goal has accepted evidence. |
| `stopped` | The owner stopped the operation. |
| `failed` | A terminal controller or contract failure occurred. |

Every state has a persisted `state_reason`. The CLI must never print “running”
merely because the controller process exists.

## 4. Admission

An operation is created in `queued`, not `running`.

At creation it pins:

- the exact immutable, hash-approved contract version;
- the exact roster version;
- the exact roster hash;
- registered project identities and target branches.

Before the first dispatch, deterministic admission proves:

1. every goal has at least one structurally qualified contained implementer;
2. every review-required goal has a distinct qualified reviewer pair;
3. quality floors and explicit contraindications are satisfied;
4. every live backend has an enforceable filesystem sandbox;
5. required verification exists for every writable project;
6. project scopes and dependency edges are valid;
7. the current clean integration heads can be pinned.

Until the first worker dispatch, a clean foreign commit may become the starting
baseline. After dispatch, the expected head advances only through verified
Operator integration; other head movement is a decision.

Capacity, quota, foreign runners, dirty checkouts, and temporary provider
unavailability are deliberately excluded from structural admission. They are
waiting conditions.

Approving a newer contract or roster does not affect an existing operation.

## 5. Waiting is not failure

The following conditions set `waiting`, persist the exact reason, and retry on
the normal controller loop:

- all qualified capacity slots are occupied;
- a provider is temporarily unavailable or quarantined;
- a checkout is dirty;
- a checkout is temporarily on another branch;
- the worktree or disk admission ceiling is reached;
- a project database is temporarily unavailable;
- a reviewer is qualified but currently lacks capacity;
- an integration checkout becomes dirty in the dispatch-to-merge race.

Waiting:

- does not increment `attempt_count`;
- does not create an action intent;
- does not create an owner decision;
- does not invoke another model;
- does not amend governance;
- does not restart the operation.

When the condition clears, the same operation resumes automatically.

## 6. Genuine owner decisions

`needs_decision` is reserved for conditions where code cannot safely preserve
the approved meaning:

- no profile structurally satisfies an approved task;
- no independent implementer/reviewer pair exists;
- scope or acceptance methodology must change;
- the admitted integration baseline changed after work began;
- a containment violation or undeclared child was detected;
- a sealed change exceeds approved scope or change budget;
- post-integration verification failed and requires fix-forward judgment;
- all actual worker attempts were exhausted;
- unique work cannot be preserved automatically;
- an explicitly approval-gated external action is ready.

A decision contains one concrete question, bounded options, evidence, and the
smallest blocking scope.

## 7. Capacity

Profiles are qualifications, not singleton workers. Pools model shared
provider concurrency and quota.

The scheduler:

- counts manual and Operator runs against their shared pool;
- reserves capacity atomically immediately before dispatch;
- selects only among structurally qualified profiles;
- never lowers a quality floor for headroom;
- waits when a qualified pool is full;
- treats a reservation race as waiting.

The number of goals does not consume capacity. Only active runs and active
reservations do.

## 8. Containment

Live workers currently require Codex filesystem sandboxing. Ordinary Claude
and OpenCode profiles remain available outside live Operator execution.

Each implementation run uses a standalone clone under the owning project's
`.orchestra/worktrees/` namespace. The worker receives no additional writable
project root. Hardening disables hooks, multi-agent fan-out, network access,
user configuration, and repository rules for that contained process.

The supervisor:

- denies child-run requests and ad-hoc continuation;
- audits the run graph and all workspace links;
- terminates residual background processes;
- measures allocated workspace bytes, including ignored output;
- terminates at the contract ceiling;
- preserves rejected output for inspection.

After the process exits, the broker rejects links and oversized output, seals
the delta into a local commit, and uses that exact commit for review and
integration.

## 9. Retry and recovery

A dispatch increments `attempt_count`; waiting never does.

Failed attempts may be retried or rerouted only within the approved attempt
budget. The controller preserves predecessor state before replacement.

Live recovery councils are not part of the controller. They created correlated
work, consumed capacity, and turned operational congestion into governance.
Legacy council fields and records remain readable for compatibility, but the
live reconciliation loop does not create or execute them.

When actual attempts are exhausted, the Operator opens one owner decision with
the preserved fingerprints and evidence.

## 10. Review and integration

Review-required work cannot dispatch until admission proves that an independent
reviewer pair exists. At runtime, temporary reviewer capacity becomes waiting.

Review runs use a contained read-only clone at the exact sealed commit.
Approval is recognized only from the declared review protocol.

The integration broker:

1. holds the project integration lease;
2. confirms the checkout is clean and on the admitted branch;
3. fetches the exact reviewed commit;
4. merges without rewriting history;
5. runs post-integration verification;
6. advances the expected head only after the merge;
7. records acceptance evidence;
8. reclaims the worker clone only when unique state is integrated.

Temporary checkout dirtiness defers the automatic merge. Baseline drift after
work starts is a decision because silently accepting a different base could
change the meaning of reviewed work.

## 11. Multi-project work

Each goal names exactly one writable project. Dependency edges determine
dispatch and integration order. Cross-project input is materialized from an
exact Git commit into a bounded, hashed snapshot; workers never read live
sibling worktrees.

One project stream cannot write another stream's clone or integration
checkout. Shared provider capacity may delay it, but does not alter its
contract, attempts, or state.

Current limitation: the final integration endpoint is the registered checkout.
Foreign work may therefore delay integration by keeping that checkout dirty.
The Operator waits; it does not interfere with or reinterpret the foreign
work.

## 12. Cleanup

Cleanup is a controller responsibility.

- Integrated, clean, unpinned state may be removed according to contract.
- Dirty or unique state is never removed automatically.
- Retry cleanup requires proof that the successor preserves the predecessor.
- Read-only review clones are removed after terminal status and a clean check.
- Disk limits are enforced during execution, not merely checked before the
  next dispatch.

## 13. Required adversarial tests

No live release is complete without deterministic tests for:

- a full shared capacity pool at zero attempts;
- unrelated manual runners in another project;
- dirty integration checkout before and during work;
- provider availability changing between route and reservation;
- impossible implementation qualification;
- impossible independent review;
- newly approved roster while an operation is active;
- controller crash and lease takeover;
- undeclared worker child;
- escaping and broken symlinks;
- background process survival;
- ignored output crossing the disk ceiling;
- worker commit followed by unreviewed branch movement;
- verifier failure before and after integration;
- cleanup with dirty, unique, and transferred state.

The expected outcome must always be one of: deterministic waiting, one bounded
owner decision, verified progress, or terminal failure. No test may require an
interactive orchestrator to “keep it going.”
