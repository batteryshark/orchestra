# Orchestra API v2

The authoritative machine-readable contract is `GET /api/v2/openapi.json`.
This guide explains the stable public model and its privacy boundaries.

## Envelope, identity, and errors

Every JSON API response except OpenAPI and `/health` is wrapped with:

```json
{"api_version":2,"instance_id":"…","data":{}}
```

Clients must pin `instance_id`; a changed id is a different/reset fleet. JSON
errors use `application/problem+json` semantics and stable error codes.

Authenticate with a paired-device credential or a least-authority service
token. Run tokens are restricted to their own bounded worker routes.

## Core discovery

```http
GET /health
GET /api/v2/openapi.json
GET /api/v2/snapshot
GET /api/v2/statistics
GET /api/v2/groups
GET /api/v2/runtimes
GET /api/v2/profiles
GET /api/v2/runway-sources
```

Groups organize and number runs. They may hold a private host-local default
CWD. There is no Scope resource or profile allowlist attached to a directory.
Profiles select runtime/model/effort/tier/runway; callers own routing policy.
Statistics may be filtered by Group/Profile/status and return run counts,
status breakdown, input/output/total tokens, metered API cost, and cumulative
agent wall time in `agent_seconds`.

Private runtime/profile/runway configuration uses write-only replacement
fields. Public projections expose only `*_configured`. Omission preserves an
unknown value; an explicit empty object/list clears it where documented.

## Groups and CWD

Create a group with an optional write-only default:

```json
POST /api/v2/groups
{"request_id":"group:research","name":"Research","cwd":"/host/path"}
```

Group projections expose `cwd_configured`, never the path. Group PATCH accepts
exactly one mutable field per request (`name`, `archived`, or `cwd`). `cwd` is a
string replacement or `null` to clear; omission means no change.

## Admit a run

```json
POST /api/v2/runs
{
  "request_id":"mail:thread-42:attempt:1",
  "group":"research",
  "profile":"codex-medium",
  "title":"Passkey landscape",
  "context":"Research current passkey adoption and return a sourced brief.",
  "cwd":"/optional/write-only/override",
  "requested_by":"mail-bridge"
}
```

Required: `request_id`, `profile`, `context`.

Optional: `group` (defaults to General), `title`, write-only `cwd`, opaque
`ref`, dependencies in `after`, `requested_by`, and `observer`.

Context is the executable request. Title is metadata only and never becomes the
prompt. There are no `mission`, `scope`, or `isolation` request fields.
Repository detection and worktree handling are automatic internal behavior.

Admission returns immediately:

```json
{"created":true,"run":{"id":42,"display":"Research #7","status":"queued"}}
```

Only `request_id` deduplicates. Replay the identical request after uncertain
delivery; Orchestra returns the same run with `created: false`. `ref` is opaque
correlation and never deduplicates or routes.

The run freezes group number, profile/runtime snapshots, executable Context,
and resolved CWD. Public projections expose `cwd_source` (`run`, `group`, or
`managed`), never the path.

## Runs, evidence, and feeds

```http
GET /api/v2/runs
GET /api/v2/runs/{id}
GET /api/v2/runs/{id}/thread
GET /api/v2/runs/{id}/events
GET /api/v2/runs/{id}/lineage
GET /api/v2/runs/{id}/observer
GET /api/v2/runs/{id}/artifacts
GET /api/v2/runs/{id}/changes
GET /api/v2/runs/{id}/log
GET /api/v2/run-feed?after=<revision>&limit=200
GET /api/v2/attention-feed?after=<revision>&limit=200
```

Normalized event kinds include assistant text, reasoning, tool calls, tool
results, lifecycle, and progress. Raw logs remain separately downloadable.
Cursor feeds are durable delivery truth; SSE streams are low-latency hints.

The lifecycle is `queued`, `starting`, `running`, `waiting`, `completed`,
`failed`, `timed_out`, `stopped`, or `skipped`.

## Control and lineage

All mutations require an idempotent `request_id`.

```http
POST /api/v2/runs/{id}/tell
POST /api/v2/runs/{id}/interrupt
POST /api/v2/runs/{id}/stop
POST /api/v2/runs/{id}/stop-tree
POST /api/v2/runs/{id}/retry
POST /api/v2/runs/{id}/continue
POST /api/v2/runs/{id}/children
```

Tell and Interrupt accept `text`. Continue requires a new `context`. Retry may
omit `context` to repeat the frozen request or provide a replacement. Retry,
Continue, and children create distinct run ids in lineage. Children inherit the
frozen CWD and obey tier/depth/count/active-child limits.

## Attention, Inbox, and Outbox

```http
GET /api/v2/inbox
GET /api/v2/outbox
POST /api/v2/attention/{id}/answer
POST /api/v2/attention/{id}/approve
POST /api/v2/attention/{id}/reject
POST /api/v2/attention/{id}/acknowledge
```

Questions, proposals, and alerts are generic. Orchestra does not require Nod;
humans, Workbridge, or another callback consumer may answer.

## Runway

Runway sources expose capacity without leaking adapter credentials or raw
configuration. A source may project percentage windows, monetary balance and
currency unit, typed credits/count/expiry, reset timestamps, per-model windows,
staleness, and bounded history. Missing timestamps stay unknown; clients must
not invent “0 seconds ago.” Exhaustion holds admission and never silently
substitutes a profile.

## Settings and service log

Fleet-wide capacity, delegation limits, Observer configuration, devices,
tokens, and storage are administrative resources. `GET /api/v2/service-log`
returns a bounded stdout/stderr tail for paired operators; run harness output
remains in each run's retained log.

## Integration boundary

Integrations own source records, routing, claims, acceptance, retries,
writeback, review policy, and Git landing. They use v2 HTTP only and must not
import Orchestra internals, open its database, inspect private paths, or depend
on removed work-tracking/control-turn concepts.
