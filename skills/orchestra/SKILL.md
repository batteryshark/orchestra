---
name: orchestra
description: Use Orchestra v2 as a standalone fleet runner to dispatch, supervise, inspect, control, and correlate durable agent runs across configured runtimes and profiles.
---

# Orchestra v2

Orchestra runs self-contained agent requests behind one durable queue,
lifecycle, thread, evidence model, and bounded delegation system. It has no
tickets, handoffs, source lifecycle, routing rules, acceptance gates, control
turns, or landing requirements.

## Inspect the fleet

```bash
orchestra status
orchestra groups
orchestra profiles
orchestra runway-sources
orchestra runs
```

Groups organize and number runs. Profiles choose runtime, model, effort, tier,
and runway. Profile routing belongs to the caller or integration.

## Groups and working directories

```bash
orchestra groups create 'name="Research"'
orchestra groups create 'name="Orchestra"' 'cwd="/absolute/path/to/orchestra"'
orchestra groups update orchestra 'cwd="/new/default/path"'
orchestra groups update orchestra 'cwd=null'
```

A group may own a private default CWD. A run may override it. Actual paths are
write-only over HTTP; clients see only `cwd_configured` and `cwd_source`.

## Dispatch

```bash
orchestra run \
  --group research \
  --profile codex-medium \
  --title "Passkey landscape" \
  --cwd /optional/host/path \
  --request-id integration:passkeys:attempt:1 \
  "Research current passkey adoption and return a sourced brief."
```

`request_id`, Profile, and Context are required. Context is the executable
request. Title is optional metadata and is never used as the prompt. CWD is
optional and overrides the group default. There is no Scope, Mission,
Isolation, or per-run Observer setup.

Dispatch returns after admission. `queued` is valid and may show a dependency,
pause, capacity, runway, or scheduled-retry hold. Do not report completion from
an admission response.

## Watch and inspect

```bash
orchestra show <run-id>
orchestra thread <run-id>
orchestra events <run-id>
orchestra lineage <run-id>
orchestra changes <run-id>
orchestra artifacts <run-id>
```

Use normalized events for the readable execution trace. Raw logs remain audit
evidence. Repository detection and worktree handling are automatic internal
execution behavior; Orchestra never lands or merges Git changes.

## Control

```bash
orchestra tell <run-id> "Focus on primary sources."
orchestra interrupt <run-id> "Stop the current approach and use the API."
orchestra stop <run-id>
orchestra stop-tree <run-id>
orchestra retry <run-id>
orchestra continue <run-id> "Turn the result into an executive summary."
```

Tell steers the current run when supported. Interrupt accepts replay risk.
Retry creates a distinct run and may repeat the frozen request. Continue
requires new Context and creates a distinct lineage node.

## Delegation and attention

Runs may delegate bounded child runs through their run token. Children inherit
the frozen CWD and obey the three-tier model, depth, child-count, and active
child limits.

```bash
orchestra inbox
orchestra outbox
orchestra answer <attention-id> "answer"
```

Questions and proposals use generic Attention. Nod is not required; a human,
Workbridge, or another callback consumer may answer.

## Integration boundary

Integrations use `/api/v2` with least-authority service tokens. They own source
correlation, routing, claims, acceptance, writeback, and landing. Use stable
idempotency keys and durable run/attention feeds; SSE and callbacks are wake-up
hints, not delivery truth. Never import Orchestra internals or open its DB.
