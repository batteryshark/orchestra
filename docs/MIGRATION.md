# Orchestra v2 hard-reset migration

V2 burns the work-tracker architecture and keeps the fleet runner: durable
runs, groups and numbering, profiles/tiers/runway, three-tier child delegation,
threads, Inbox/Outbox, controls, Observer, evidence, usage, and device access.

Removed concepts include tickets, claims, leases, handoffs, writeback,
acceptance/review policy, routing, control turns, Nod coupling, and Git landing.
Workbridge is the only component allowed to translate Slash Work into
Orchestra's generic public contract.

## State preservation

Before the reset, legacy state is copied under:

```text
~/.orchestra/archives/<timestamp>/
```

The live-v2 restart migration preserves run ids, group ids and sequence
numbers, run history/log references, profiles, runway readings, credentials,
and configuration. It removes Scope tables/columns after freezing each retained
run's CWD. A group receives a default CWD only when its historical bindings are
unambiguous. Original archived data remains available for forensic recovery.

The old v1 import is deliberately conservative: it imports explicit runtimes,
profiles, runway, and safe fleet settings. It does not import source workflow,
Nod state, control turns, landing receipts, or private path aliases.

## New model

| Before | V2 |
|---|---|
| project mixed organization and execution | Group organizes/numbers and may provide a default CWD |
| Scope selected a path and profile allowlist | Optional write-only Group or Run CWD; all enabled worker profiles remain selectable |
| mission plus context | one required executable `context` |
| explicit isolation choice | automatic internal repository/worktree handling |
| title could leak into execution | title is metadata only |
| per-run Observer/Nod assumptions | fleet Observer plus generic Attention/callbacks |
| internal routing/review/landing | integration-owned policy over public v2 HTTP |

## Recreate deliberate configuration

Inspect the reset:

```bash
orchestra status
orchestra groups
orchestra runtimes
orchestra profiles
orchestra runway-sources
orchestra settings
```

Create groups with optional private defaults:

```bash
orchestra groups create 'name="Research"'
orchestra groups create 'name="Orchestra"' 'cwd="/Users/me/Projects/orchestra"'
```

The API never returns those paths. Group projections expose
`cwd_configured`; runs expose only `cwd_source`. A run-level CWD override is
also write-only and frozen at admission.

Dispatch using the neutral contract:

```bash
orchestra run \
  --group orchestra \
  --profile codex-medium \
  --title "API audit" \
  --request-id manual:api-audit:1 \
  "Audit the public API and report concrete issues."
```

## Client migration

Clients must:

1. require API v2 and pin `instance_id`;
2. remove Scope, Mission, Isolation, reference, and per-run Observer UI;
3. send required `context`, optional metadata `title`, Group, Profile, and CWD;
4. never round-trip private host configuration or paths;
5. consume durable run/attention feeds and treat SSE/callbacks as wakeups;
6. preserve stable `request_id` across uncertain delivery; and
7. keep source routing, acceptance, writeback, review, and landing outside
   Orchestra.

## Verification

```bash
python3 run_tests.py
orchestra service restart
curl -s http://127.0.0.1:8765/health
```

Then submit one minimal CWD-backed run and confirm the terminal projection has
the expected Group number and safe `cwd_source`, while no host path is returned.
