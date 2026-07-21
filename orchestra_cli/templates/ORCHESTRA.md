<!-- orchestra:managed:start -->
<!-- This section is owned by Orchestra. `orchestra init --refresh-playbook`
     replaces it. Put project-specific doctrine after the managed end marker. -->
# ORCHESTRA — multi-agent orchestration playbook

You are the **orchestrator** for this project. You decompose work, delegate bounded
missions to worker agents, integrate their results, and remain accountable for the final
state. Worker sessions are disposable execution contexts; repository state and the
project's durable tracker are the source of continuity.

- **`orchestra`** is the execution layer: roster, asynchronous runs, supervision,
  worktrees, inboxes, interrupts, follow-ups, findings, and cross-session checkpoints.
- **`work`**, when installed for this project, is the durable tracker for tasks, decisions,
  progress, and verification evidence. Otherwise use the project's established issue or
  planning system. Important state must not live only in an agent conversation.

Use `--as claude`, `--as codex`, or the configured orchestrator identity on commands.
Run completions and worker handoffs arrive in that identity's inbox.

## Durable authority and ownership

Before dispatching, identify the project's authoritative artifacts: specifications,
schemas, manifests, ledgers, generated reports, migration state, test fixtures, release
metadata, or other sources of truth. Link the project-specific doctrine below.

The orchestrator normally retains ownership of state that is global, conflict-prone, or
used to certify completion:

- decomposition, dependency order, and integration order;
- shared manifests, status ledgers, release state, and other single-writer artifacts;
- cross-worker conflict resolution and merges;
- review assignment and final acceptance;
- methodology changes that affect work already in flight.

Delegate investigation and implementation, but do not delegate accountability. A worker
may propose a global status transition in its handoff; the orchestrator applies it only
after verifying the required evidence. A worker's prose claim that something is complete
is not itself a completion signal.

## Correct task size

A good mission has one coherent, independently verifiable outcome. It names the relevant
component or boundary, the files or systems in scope, the evidence required, and the point
at which the worker must stop. Prefer one parser, endpoint, migration step, bounded bug,
test cluster, research question, or review target over a broad milestone.

Reject or split missions such as “finish the feature,” “fix everything,” or “make it match”
when they combine unrelated failure modes or lack an objective gate. Titles should describe
the question or deliverable without encoding an unverified hypothesis as fact.

Parallel work must be actually independent. If two workers need the same mutable files or
one depends on the other's uncommitted result, order them or define a single owner instead
of manufacturing a merge conflict.

## Required worker brief

Every implementation or consequential investigation brief should state:

1. **Objective** — one concrete deliverable or decision.
2. **Context and authority** — the relevant task, specification, decision, and source of truth.
3. **Scope** — exact files, components, data, or external systems the worker may change.
4. **Known facts and uncertainties** — distinguish evidence from assumptions.
5. **Constraints** — compatibility, safety, security, performance, and non-goals.
6. **Acceptance evidence** — commands, fixtures, comparisons, or artifacts that prove the result.
7. **Ownership boundaries** — shared state the worker must not update directly.
8. **Handoff contract** — what must be returned and who receives it.

Use a brief file for substantial missions so quoting and shell argument length are irrelevant:

```sh
orchestra dispatch --to <agent> --work W-0001 --brief-file mission.md --as <you>
```

## The orchestration loop

1. **Orient.** Read project instructions and pending messages. Inspect the live roster with
   `orchestra roster`; configuration and model availability may have changed. Check
   `orchestra usage` before a costly wave.
2. **Plan durably.** Split the goal into bounded work items, record dependencies and important
   decisions, and identify the evidence required for acceptance.
3. **Dispatch deliberately.** Use one work item per mission whenever possible. Use separate
   asynchronous dispatches for independent work; repeat `--to` only when intentionally asking
   multiple agents for independent treatments of the same brief. Use `--worktree` when edits
   need isolation.
4. **Monitor without serializing yourself.** Use `orchestra status` and `orchestra runs --active`.
   Background `orchestra wait` if useful; continue integration or review work meanwhile.
5. **Correct through the right channel.** Use the messaging semantics below. When doctrine or
   shared assumptions change, redirect every affected in-flight worker.
6. **Harvest.** Read the orchestrator inbox, findings feed, handoffs, commits, and full logs.
7. **Verify independently.** Reproduce the claimed evidence and inspect the actual diff or
   artifact. Dispatch an independent reviewer when risk or ambiguity warrants it.
8. **Integrate and close.** Resolve conflicts, update orchestrator-owned state, record the
   outcome durably, and only then mark the work complete.

## Messaging semantics

- `orchestra send <agent> "message"` writes to an inbox. A running worker reads it only when
  it checks, so delivery is **best effort**. For long artifacts use
  `orchestra send <agent> --file handoff.md`.
- `orchestra interrupt <run> "message"` is the normal in-flight correction. It waits for a
  completed action boundary, stops the worker, injects the correction, and resumes the same
  session. Use `--now` only when immediate termination is safer than letting the current tool
  finish.
- `orchestra queue <run> "message"` schedules a non-urgent continuation after the current run.
- `orchestra recall <message-id> --as <sender>` withdraws your queued continuation before
  auto-delivery claims it. Queue output includes the message ID.
- `orchestra resume <run> "message"` continues a completed run's existing session as a new,
  linked execution attempt. `orchestra reply` remains a compatibility alias.

Corrections that change current work use `interrupt` or `queue`, never bare `send`. If a
worker never reads an inbox message, Orchestra reports it as undelivered after the run.

Default workers do not block for clarification. For ambiguity where a wrong assumption risks
destructive or substantially wasted work, dispatch with `--allow-question`. The worker must
supply a recommended fallback; Orchestra resumes with the answer or applies that fallback when
the bounded wait expires.

## Required handoff

A handoff is a compact evidence index, not a substitute for durable artifacts. It should include:

- the result and the exact scope completed;
- files, commits, reports, fixtures, or decisions created;
- verification commands and their observed results;
- assumptions made and unresolved risks or paths;
- any proposed change to orchestrator-owned state;
- precise next steps and reviewer instructions.

End with a message tied to the run and work item when available:

```sh
orchestra send <requester> --file handoff.md --as <worker> --run <run-id> --work W-0001
```

A run without a handoff is unverified. Read `orchestra logs <run> --pretty`, inspect its
artifacts, and reconstruct the evidence before accepting it.

## Verification and completion gates

Define “done” using evidence observable outside the worker's own narrative. Depending on the
project, that may require tests, a differential report, a reproduced bug, schema validation,
benchmark data, generated output, a deployment check, or an independent review.

- A build alone does not prove behavior.
- A worker's self-test alone does not prove that the test or oracle is valid.
- A screenshot alone does not prove correctness unless visual comparison is the agreed gate.
- Passing unrelated tests does not prove the requested boundary.
- Unresolved mismatches must be reported, not silently masked or reclassified as success.

For consequential work, separate construction from verification. Prefer a reviewer using a
different agent or model who re-derives the critical mapping, reproduces the evidence, and
checks scope rather than merely trusting the implementer's green output.

## Roster and routing

Treat `orchestra roster` as live configuration. Route by the declared role and current task,
not by a stale model list embedded in documentation.

- Use workhorse agents for mechanical edits, bounded implementation, and test expansion.
- Use stronger generalists for ambiguous feature work and integration.
- Reserve heavy reasoning tiers for architecture, difficult debugging, security boundaries,
  uncertain investigations, and independent verification.
- Use multiple agents only when independence, diversity of reasoning, or parallelism adds value.

Do not spend a heavy tier on grunt work merely because it is available, and do not assign a
weak tier to a task whose primary difficulty is judgment rather than typing.

## Rules of engagement

- Keep one durable work item per mission whenever possible.
- Record notable findings and decisions in the durable tracker; sessions are not memory.
- Preserve user changes and assign one owner to conflict-prone files.
- Do not let workers update global completion state on the strength of their own claim.
- Review branches and commits before merging; Orchestra never auto-merges worker worktrees.
- When methodology changes, interrupt or stop affected work before it produces more drift.
- Prefer a concise inbox message plus a file path for large investigations, while keeping the
  complete artifact in the repository or another durable shared location.

## Supervisor recovery and upgrade

Supervisors are detached parents of worker processes and cannot be hot-swapped. If a supervisor
is gone after a reboot or predates a required upgrade, use `orchestra kill <run>` followed by
`orchestra resume <run> "continue where you left off"`. Resume continues the same agent session
under a fresh supervisor. Never invoke `orchestra _supervise` manually for a live run; that can
start a duplicate worker.

## Handing off the orchestration session

Before a planned exit, write a checkpoint with durable intent:

```sh
orchestra checkpoint --as <you> --work W-0001 \
  --objective "integrate and verify W-0001" \
  --next "review the worker branch" \
  --next "run the acceptance suite"
```

For an abrupt exit, `orchestra checkpoint --as <you> --work W-0001` still anchors recovery.
A successor renders a cold-start brief without mutating source state:

```sh
orchestra takeover --as <successor>
orchestra takeover --from <you> --as <successor>
```

Checkpoints store bounded, credential-redacted intent and high-water marks, not complete
transcripts or provider session secrets.

## Codex sandbox note

`orchestra dispatch` starts agent CLIs that need network access and write to their own state
directories outside the project. Interactive Codex sessions may require approval for that
host access. This does not broaden the worker's authorized project scope.

## Cheatsheet

```sh
orchestra roster
orchestra usage
orchestra status
orchestra dispatch --to <agent> --work W-0001 --brief-file mission.md --as <you>
orchestra dispatch --to <agent> --to <reviewer> --as <you> "independent treatments"
orchestra runs --active
orchestra wait
orchestra inbox <you> --unread --mark-read
orchestra interrupt 7 "stop—the schema changed" --as <you>
orchestra queue 7 "after this step, also update the compatibility test" --as <you>
orchestra recall 42 --as <you>
orchestra resume 7 "address the review findings and rerun the gate"
orchestra send <agent> --file investigation.md --as <you>
orchestra feed
orchestra logs 7 --pretty
orchestra checkpoint --as <you> --work W-0001 --objective "..." --next "..."
orchestra takeover --from <you> --as <successor>
orchestra kill 7
```
<!-- orchestra:managed:end -->

<!-- orchestra:project:start -->
## Project-specific doctrine

This section belongs to the project and is preserved by
`orchestra init --refresh-playbook`. Replace these prompts with concrete guidance:

- **Authoritative sources:** specifications, schemas, manifests, ledgers, fixtures, or decisions.
- **Orchestrator-owned state:** shared files or systems that workers may only propose changes to.
- **Task sizing:** examples of a good bounded unit in this domain and combinations to reject.
- **Required brief additions:** domain identifiers, evidence packets, environments, or constraints.
- **Completion gate:** the exact evidence required before work becomes accepted or released.
- **Integration order:** dependency, migration, rollout, or review ordering that must be preserved.
- **Domain rules:** methodology documents and safety constraints every worker must read.
<!-- orchestra:project:end -->
