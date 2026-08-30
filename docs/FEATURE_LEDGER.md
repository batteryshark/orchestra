# Orchestra v2 feature ledger

This ledger prevents future simplification from deleting the mechanics that
make Orchestra useful while keeping work-tracking policy out of the core.

## Orchestra owns and must retain

| Capability | Contract |
|---|---|
| Runs | Durable admission, global run id, Group-local sequence, visible holds, supervision, recovery, terminal evidence. |
| Groups | Organization, filtering, stable numbering, optional private default CWD. |
| CWD | Optional write-only Group/Run path, frozen at admission; safe `cwd_configured`/`cwd_source` projections only. |
| Profiles | Runtime, model, effort, Workhorse/Core/Frontier tier, runway linkage, private launch config. |
| Runtimes | Built-in, exec, and ACP-compatible agent harnesses without core workflow assumptions. |
| Runway | Percentage windows, balances, credits/expiry, reset countdowns, per-model capacity, history, holds. |
| Scheduling | Global/profile capacity, FIFO readiness, pause/resume, dependencies, scheduled retries. |
| Delegation | Three-tier parent/child model, depth/count/active-child bounds, frozen inheritance, lineage. |
| Thread | Operator↔run communication, delivery receipts, Tell, interrupt/redirect, tail following. |
| Attention | Generic questions, proposals, alerts, Inbox/Outbox, arbitrary callback consumers. |
| Controls | Tell, Interrupt, Stop, Stop Tree, Retry, Continue, Observer check. |
| Observer | First-class configurable agent runtime with a separate lane and bounded evidence. |
| Evidence | Normalized assistant/reasoning/tool/lifecycle events plus retained raw logs. |
| Artifacts and Git | Immutable artifacts and base/head/branch/checkpoint/diff evidence; no landing policy. |
| Usage | Worker, Observer, and combined token/cost accounting. |
| Fleet access | Pairing, devices, least-authority service/run tokens, OpenAPI, durable feeds, SSE wakeups. |
| Clients | Web plus native iOS/macOS; responsive and useful without Workbridge. |
| Operations | Fleet settings, service log, storage accounting, backup/restore/prune, live migration. |

## Quality-of-life invariants

- New Run is Title, Group, Profile, required Context, and optional CWD.
- Title is metadata; Context is the executable request.
- Model gets a full-width copyable field. Effort is discovered and selectable
  for the chosen model, with a custom fallback.
- Profile cards do not expose confusing sandbox, timeout, priority, or active
  cap fields as primary UI.
- Run detail is Thread-first. Assistant text is plain; 🧠 reasoning and 🔧/🧾
  tool evidence fold; harness/lifecycle chatter is hidden by default.
- Live output follows the tail until the operator scrolls away, then offers
  Return to live. Refreshes do not tear down the panel or jump to the top.
- Live controls stay in the header; terminal Retry/Continue are contextual;
  destructive tree control lives in overflow.
- Runway never invents `0 seconds ago` for an unknown reset.
- Fleet shows operational scheduler/runtime/message state. Global limits,
  Observer, credentials, storage, and service logs live in Settings.
- The established gray three-line Orchestra mark is used across clients.

## Explicitly outside Orchestra

- tickets, backlog, claims, leases, source state, handoffs, and writeback;
- source-specific routing, project mappings, acceptance, criteria, sign-off,
  verifier/reviewer roles, and retry policy;
- control seats, control turns, hidden work layers, and Nod coupling;
- rebase/conflict resolution, merge judgment, landing, and landing receipts;
- federation or automatic failover across independent Orchestra instances; and
- assumptions about Slash Work, Workbridge, mail, research, or any other caller.

Workbridge may understand both Slash Work and Orchestra. Neither core may gain
fields, migrations, or lifecycle assumptions about the other.

## Regression gates

1. Concurrent admission cannot duplicate a Group-local number.
2. Idempotent replay returns the same run and never double-executes.
3. CWD paths never leak through list/detail/group projections.
4. Children/Retry/Continue inherit frozen CWD and preserve lineage.
5. Tier and delegation bounds reject escalation.
6. Runway exhaustion holds rather than reroutes.
7. Missed SSE/callbacks recover from durable feeds.
8. Normalized evidence never destroys raw audit evidence.
9. Device/service/run-token authority remains least privilege.
10. Web, iOS, and macOS compile against the same published v2 contract.
11. No public Scope, Mission, Isolation, handoff, control-turn, Nod, or landing
    route/field reappears.
