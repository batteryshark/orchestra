# Orchestra

Orchestra is a self-hosted agent fleet runner for one machine. Submit an
executable request with a profile; Orchestra schedules it, runs it,
lets it delegate bounded child runs, and retains the conversation, trace,
usage, artifacts, and Git evidence. The same daemon serves the CLI, web
console, and native Apple clients.

Orchestra is not a work tracker. It has no tickets, claims, handoffs,
acceptance workflow, source-system mirror, routing policy, verification
policy, or Git landing policy. Integrations may use Orchestra to execute work,
but those concepts remain outside it.

## The model

- **Run**: one admitted execution. Every root, child, retry, or explicit
  continuation is a separately numbered run with durable lineage.
- **Group**: an organizational label for related runs. Each group has its own
  monotonic display number, such as `Research #18`. Group assignment is
  immutable after admission. `General` always exists.
- **Working directory**: a group may configure a daemon-host default for
  future runs, and a root run may supply an explicit override. Paths are
  write-only over the API and frozen at admission.
- **Profile**: a managed launch configuration: runtime, model, effort,
  timeouts, environment policy, tier, capacity, and runway source.
- **Runtime**: the harness adapter used by a profile. Codex, Claude Code,
  OpenCode, and Reasonix are polished built-ins; a configurable argv runtime
  covers other harnesses without a plugin framework.
- **Runway source**: provider/account/lane availability shared by one or more
  profiles. A fresh definitive zero can hold starts; stale or unknown runway
  never does.
- **Observer**: an optional isolated agent runtime that reviews bounded run
  evidence. It is supervision, not a worker run and not a routing engine.
  Orchestra only accepts profiles whose adapter can be launched with tools
  disabled: Claude Code, OpenCode, or Reasonix today.

One daemon and one SQLite database are authoritative. The daemon is the only
normal writer; the CLI and every device use the HTTP API. The fleet is the
configured execution capacity on that machine. There is no node membership,
distributed queue, federation, or aggregate scheduler. A harness can still
use SSH or another tool to work elsewhere.

## Run request

Every launch reduces to the same v2 request:

```json
{
  "request_id": "mail-digest:2026-08-29",
  "profile": "codex-medium",
  "context": "Review today's unread mail and write a priority digest. Do not send, archive, or modify messages.",
  "group": "Personal Ops",
  "title": "Morning mail digest",
  "cwd": "/Users/me/Automation/mail",
  "ref": "opaque-caller-value",
  "after": [{"run_id": 41, "condition": "success"}],
  "requested_by": "automation:mail-digest",
  "observer": "inherit"
}
```

`request_id`, `profile`, and `context` are required. `context` is the one
executable worker request; `title` is display metadata and never becomes the
prompt. Replaying the same `request_id` returns the original admission. `ref`
is stored and echoed but never interpreted. `after` accepts `success` or
`terminal`; `observer` is `inherit`, `off`, or an Observer profile name. `cwd`
is an optional daemon-host path, canonicalized and frozen but never returned.

The selected profile is explicit. Orchestra does not choose or substitute a
profile, including when runway is exhausted. Parents must name a child
profile, and a child may use only the same or a lower capability tier.

## Scheduling and control

Runs start FIFO subject to dependencies, global pause, global/profile
capacity, runway holds, and scheduled retry time. These holds are visible.
Profile priority is descriptive metadata only; it never reorders the queue.
The lifecycle is:

```text
queued -> starting -> running <-> waiting -> completed
                                      |----> failed | timed_out | stopped | skipped
```

A parent waiting for child results releases runtime capacity and later resumes
the same run/session. A blocking question does the same. Operators can:

- **Tell**: steer live when supported, otherwise deliver at a safe boundary.
- **Interrupt**: cancel the active turn and resume the same run with new
  direction. If no reliable session exists yet, Orchestra transparently
  restarts from the frozen brief and records replay risk.
- **Stop** or **Stop Tree**: stop one run or an explicit lineage subtree.
- **Check**: request mechanical and optional Observer inspection.
- **Retry** or **Continue**: create a new, separately numbered lineage run.

Orchestra performs at most one conservative automatic retry for a recognized
transient infrastructure failure. It is also a new run. Unknown and
authentication failures stop and raise attention.

## Attention, evidence, and artifacts

The fleet Inbox contains blocking questions, profile-change proposals, and
alerts. A blocking item has a correlation id, optional choices/deadline/
fallback, and no default expiry. The first authorized answer wins and resumes
the suspended run. Full threads also remain visible on each run.

Orchestra retains run facts, messages, delivery receipts, normalized events,
raw logs, Observer checks, usage, explicit artifacts, and Git checkpoints.
Artifacts are published deliberately: Orchestra snapshots the selected file,
validates paths and symlinks, records MIME/size/SHA-256, and serves immutable
range-capable downloads. It never sweeps a workspace looking for outputs.

When the frozen working directory is inside a Git repository, Orchestra
automatically isolates execution in a per-run worktree and records
base/head/branch/checkpoint/patch/diff evidence without exposing the host path.
It does not review, rebase, merge, land, or decide whether the change is
accepted.

## Quick start

Orchestra requires Python 3.11 or newer and has no runtime Python dependencies.
The bootstrap binds to `127.0.0.1:8765` by default; it is an intentionally
ordinary high local port, not a claimed protocol port, and remains configurable
for tunnels, containers, or host collisions.

```sh
python -m venv .venv
./.venv/bin/pip install -e .
orchestra init
orchestra daemon
```

Normal configuration and operation go through the daemon:

```sh
orchestra status
orchestra groups create "Research"
orchestra groups create name="Personal Ops" cwd="$PWD"
orchestra profiles list
orchestra profiles discover --local

# init registers the built-in runtimes; create an explicit profile and runway
orchestra runway-sources create name="Codex Primary" provider=codex \
  adapter=codex account=personal
orchestra profiles create name="Codex Medium" runtime_id=codex tier=2 \
  effort=medium active_cap=4 runway_source_id=codex-primary

orchestra dispatch --profile codex-medium \
  --group Research --request-id research:local-agents:1 \
  "Research practical local-agent observability patterns"

orchestra runs --status running
orchestra runs --status waiting
orchestra statistics --group Research
orchestra show 42
orchestra thread 42
orchestra outbox --status undeliverable
orchestra artifacts 42
orchestra changes 42
orchestra tell 42 "Restrict the comparison to self-hosted systems."
orchestra interrupt 42 "Stop browsing and synthesize the evidence now."
orchestra check 42
orchestra stop-tree 42
```

Storage pruning is always review-then-apply, and evidence can be pinned:

```sh
orchestra storage report
orchestra storage plan --older-than-days 90
orchestra storage apply <plan-id>
orchestra pin 42 --reason "reference run"
```

Fleet and Observer configuration use the same API:

```sh
orchestra settings
orchestra settings set instance_name '"Studio Mac"'
orchestra observer
orchestra observer update enabled=true profile=observer-light
```

Observer launches inherit no run token, operator token, profile environment,
or runtime environment. Reasonix receives only its selected provider's
validated credential in an isolated home; no unrelated provider state crosses
the boundary. Unsupported adapters—including Codex, generic argv, and ACP—are
rejected instead of being presented as isolated.

Only bootstrap, service lifecycle, legacy archive, backup/restore, and offline
repair may write state without the daemon. `orchestra restore BACKUP` validates
only; add `--apply` to replace state, preserving the displaced v2 directory in
the archives directory and rebasing durable feed revisions so paired clients
reconcile the restored truth.

## Across devices

The dependency-free web console and SwiftUI app use the same API. Desktop,
web, and macOS expose Runs, Inbox, Outbox, Groups, Profiles, Runway, Fleet,
and Settings. iPhone emphasizes Runs, Inbox, Groups, and Runway; iPad uses a
sidebar. Run detail includes Thread, Artifacts, Changes, Raw Log, Lineage,
Facts/Usage, and Observer.

Operator devices pair with a short-lived one-time code or pairing URI and store a
revocable token in a secure cookie or Keychain. Integrations receive
fixed-authority service tokens; workers receive short-lived run tokens. Bring
your own trusted private network and TLS/reverse proxy. Orchestra does not run
a relay or APNs service.

## Callbacks and integrations

One optional argv callback command can receive low-volume JSON on stdin for
`attention.opened`, `run.terminal`, and `observer.stopped`. It is best effort.
Cursor feeds and stored evidence are the recovery truth.

Workbridge is the only integration expected to understand both Slash Work and
Orchestra. It chooses groups/profiles and optional working-directory bindings,
owns routing and work policy,
answers attention, verifies results, and lands changes through the public
Orchestra API. Orchestra remains equally useful for mail, research, browser,
document, and operations missions with Workbridge absent.

See [the design contract](DESIGN.md), [API contract](docs/API.md),
[feature ledger](docs/FEATURE_LEDGER.md), and [migration guide](docs/MIGRATION.md).

## Development

```sh
./.venv/bin/python run_tests.py -j 8
```

The Python service uses the standard library at runtime. The web client uses
plain ES modules and CSS with no framework, build step, or CDN dependency.
