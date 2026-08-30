# Post-v2 ideas

The hard reset already includes groups, scopes, profiles/runtimes, runway,
delegation, attention, Observer, artifacts, evidence, per-device auth, and
universal clients. This file is for ideas that remain after that contract—not
a back door for restoring deleted architecture.

## Useful extensions

- Client-side saved launch templates that still expand into ordinary explicit
  `RunRequest` values.
- Better local runway adapters backed by captured provider fixtures, while
  retaining the rule that only fresh definitive zero blocks starts.
- Encrypted off-machine backup rotation and automated restore verification.
- More artifact preview types implemented entirely in clients.
- macOS menu-bar status, keyboard control, and local notifications.
- Client-side switching among independent Orchestra endpoints. Each endpoint
  remains a separate instance with separate queue/state; no cross-node
  scheduler or capacity abstraction.
- A resident-runtime implementation for a proven PydanticAI or pi-agent use
  case, using the existing runtime/session contract without adding a generic
  plugin framework or speculative pool.
- A direct completion adapter for cheaper Observer profiles, but only with an
  equally enforceable no-tools, no-workspace, minimal-environment boundary.
- Evidence export bundles with checksums and redaction controls.
- Empirically derived transient-failure classifiers, still bounded to one
  automatic retry and never profile substitution.

## Ideas requiring evidence first

- Queue reordering: add only if real FIFO usage produces an unsolved problem;
  profile priority remains non-scheduling metadata.
- Object storage: add only when node-local immutable artifacts are measurably
  insufficient, as a narrow storage backend rather than a distribution layer.
- Additional callback transports: keep outside Orchestra unless the single
  argv JSON command cannot support a concrete integration.
- More delegation knobs: preserve small depth/count/concurrency defaults and
  require observed workloads before expanding them.

## Out of scope permanently

Do not propose backlog, task, project-planning, claim, lease, handoff,
acceptance, sign-off, source writeback, automatic verification, Git landing,
Nod coupling, generic control turns, users/organizations/RBAC, public relay,
built-in APNs, runtime plugin SDK, registered remote workers, federation,
leader election, or a distributed queue. Those belong to integrations,
clients, infrastructure, or a different product.
