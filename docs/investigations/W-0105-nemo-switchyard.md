# W-0105 — NeMo Switchyard investigation

Date: 2026-08-12
Switchyard snapshot: `2bef154970d23cacf9c83b4fe9c1cd90212623e8`
(`main`, 2026-08-11), release `v0.2.0`

## Recommendation

Do not make Switchyard a Dromond dependency or replace Dromond's profile/run
routing with it. The two systems route at different levels:

- **Dromond routes work to an agent harness process.** It owns Work intake,
  profiles, worktrees, supervision, budgets, approvals, messages, resumes, and
  durable run state.
- **Switchyard routes LLM API calls made by that process.** It is a proxy and
  Rust library between Codex/Claude/OpenClaw (or another API client) and model
  providers. It owns model selection, provider-protocol translation, fallback,
  and request-level telemetry.

Switchyard is therefore complementary, not a substitute. If Dromond later
wants dynamic model selection *inside* a run, prefer an opt-in Switchyard
deployment over recreating its classifier, stage, escalation, translation, and
metrics machinery. Keep the boundary at the HTTP proxy and keep static Dromond
profiles as the default.

Do not ship that integration yet. Switchyard calls itself pre-alpha,
experimental, and not for production; `v0.2.0` was released only two days
before this review and `main` has already removed deprecated `v0.2.0` surfaces.
Run a pinned, non-production A/B spike first.

## What is available now

The current native server accepts OpenAI Chat Completions, OpenAI Responses,
and Anthropic Messages requests. It normalizes them, selects a configured
target, translates to that target's wire format, and translates buffered or
streaming responses back. It can be used as:

1. a Python-distributed launcher that starts an in-process Rust server and then
   launches Codex, Claude Code, or OpenClaw;
2. a standalone `switchyard-server` Rust proxy; or
3. the embedded `switchyard-libsy` Rust library.

The shipped route types are:

| Route | Decision and trade-off |
|---|---|
| `passthrough` | One stable route ID for one target; no decision. |
| `random` | Weighted traffic split for A/B tests and baselines. |
| `llm_classifier` / capability | A judge predicts whether the efficient model can solve the request. Optional process-local session affinity avoids judging every turn. |
| `stage_router` | Tool errors, exploration, spinning, edits, and test progress select a capable or efficient tier. An optional judge resolves low-confidence turns. |
| `llm_classifier` / escalation | Run the efficient model first, then have a judge inspect its completed turn; after repeated escalation verdicts, latch the session to the capable model. This adds weak + judge latency on unlatched turns and weak + judge + strong cost on the turn that triggers escalation. |
| `llm_classifier` / custom | Validate a judge's structured output and select among two or more named targets. |

Useful implementation details for Dromond:

- The server recognizes native Codex and Claude Code session/subagent headers,
  as well as explicit `x-switchyard-*` headers. Stateful routes therefore do
  not require Dromond to infer conversation identity.
- The launchers already know how to point Codex at an OpenAI Responses proxy
  and Claude Code at an Anthropic proxy. Cross-provider serving is a first-class
  use case.
- Targets have configured context windows. The router tries another eligible
  target when the selected one cannot fit the request.
- Observability includes Prometheus, OpenTelemetry, structured decision logs,
  per-model tokens/cost/latency, algorithm statistics, and optional durable
  per-session routing logs.
- Credentials are named by environment variable in TOML and read at startup.
  They need not appear in the deployment file.

There are also meaningful gaps between the launch article and the released
software:

- The article describes a learned **prefill router** based on residual-stream
  activations. It is research, not a route exposed by the `v0.2.0` native TOML
  schema.
- The article describes infrastructure/load/latency as useful signals, but
  `v0.2.0` removed Switchyard's latency-aware route. Its changelog directs
  multi-endpoint load- or latency-aware selection to an upstream load balancer.
- The repository contains a reproducible Harbor runner and dataset preparation
  for Terminal-Bench Lite/2.0/2.1 and SWE-Bench Pro, but no checked-in result
  artifacts supporting the headline partner numbers.

## Evidence quality

The published results are promising but not a Dromond adoption result:

- NVIDIA reports that LangChain ran 145 internal multi-turn tasks five times.
  Escalation routing between Nemotron 3.5 Lightning and Claude Opus 4.8 sent 7%
  of calls to the frontier model and reduced cost 74%, with an approximately
  six-percentage-point accuracy loss against the frontier-only baseline.
- NVIDIA reports Cognition's staged routing between Opus 5 and Kimi K2.7 at
  50.6% accuracy and $3.11 mean task cost on FrontierCode Main: 2.8 percentage
  points below Opus 5 and about 28% cheaper.
- The linked prefill-routing preprint reports that its best SharedTrunkNet
  result closed 45.58% of the gap between the best single model and an oracle
  while saving 74.31% against the most expensive model. That result requires
  workload data, labels, an open-weight encoder, hidden-state extraction, and
  training; it is not the tuning-free proxy path proposed here.

These figures are vendor/partner-reported on other agents, model pairs, tasks,
and pricing. They establish that a local experiment is worthwhile, not that a
quality/cost trade-off transfers to Dromond.

The project has real early traction (505 stars, 66 forks, and 214 commits when
checked), but maturity is the controlling signal. The inspected README says
"pre-alpha" and "not for production use." Known `v0.2.0` issues include:

- provider work and cost continuing after a client disconnect;
- incomplete routing-tier attribution in some judge/fallback paths;
- a retry recovery metric that remains zero;
- session IDs missing from native session stats; and
- tool-bearing Codex requests failing against upstreams with incompatible tool
  names or schemas.

Those touch Dromond's cancellation, attribution, and reliable-supervision
requirements directly.

## Fit with Dromond

| Concern | Dromond | Switchyard | Conclusion |
|---|---|---|---|
| Unit routed | Work item / complete agent run | One model API request or conversation turn | Complementary layers |
| Destinations | Codex, Claude Code, OpenCode, Reasonix profiles | Model/provider targets behind a compatible API | Does not replace `runners.py` |
| Durable intent/state | Work + SQLite + briefs/logs | Process-local routing state; optional routing log | Dromond remains owner |
| Safety/control | Approval, budget, timeout, stall, worktree | Target policy, fallback, context fit | Neither subsumes the other |
| Cost control | Run/provider grants (planned D2) | Per-call tier selection and accounting | Switchyard can operate inside a Dromond grant |
| Protocol | CLI exec/JSONL or ACP | OpenAI/Anthropic HTTP APIs | HTTP proxy is the correct seam |
| Dependency posture | Stdlib Python, no runtime dependencies | Rust server or Python 3.12 + PyO3 package | Keep optional and external |

Dromond should continue recording the configured route ID as `runs.model`.
Selected-model and router-overhead detail belongs in Switchyard telemetry; it
can later be linked to `DROMOND_RUN_ID` through an explicit correlation header
if the harness/proxy path permits one. Dromond should not copy Switchyard's
per-call stats into its run schema until an actual UI or budget consumer needs
them.

## Proposed experiment (separate work item)

Pin `v0.2.0`; do not follow `main`. Operate a standalone server outside Dromond
rather than embedding Rust or nesting `switchyard launch` under Dromond's
supervisor. A standalone proxy preserves Dromond's direct ownership of the
Codex/Claude process and its JSONL output, and it makes proxy failure explicit.

1. Select one capable/efficient model pair available through the same billing
   path. Start with `stage_router`, `picker = "efficient_first"`, and
   `confidence_threshold = 0.5`, the documented calibrated starting point.
2. Build baselines from roughly 40–75 representative tasks on the capable
   model, then run about 20 stratified tasks on the efficient model, following
   Switchyard's own minimum-data guidance. Include easy/clean, easy/tricky,
   hard/structural, and hard/localized tasks.
3. Replay the same frozen Work snapshots through the routed profile. Compare
   verified completion, human acceptance, total provider cost, wall time,
   retries, cancellations, context overflow, and stuck/loop incidence. Include
   judge calls in both cost and latency.
4. Exercise cancellation, timeout, resume, streaming, tool calls, compaction,
   and concurrent runs explicitly. Confirm which native session header each
   installed harness version emits.
5. Adopt only if the routed profile reduces median cost by at least 30%, loses
   no more than two percentage points of verified task completion, adds less
   than 10% median wall time, and introduces no cancellation/accounting defect.
   These are proposed decision thresholds, not NVIDIA claims.

The smallest future Dromond change should be generic per-profile environment
overrides plus documented proxy arguments. Do not add a Switchyard backend: the
agent backend is still Codex or Claude, while Switchyard is the selected model
provider. Do not add an algorithm/plugin framework or embed `libsy` in this
stdlib Python process.

## Sources

- [NVIDIA technical article (2026-08-11)](https://developer.nvidia.com/blog/route-ai-agent-workloads-across-models-with-nvidia-nemo-switchyard/)
- [Switchyard README at the inspected commit](https://github.com/NVIDIA-NeMo/Switchyard/blob/2bef154970d23cacf9c83b4fe9c1cd90212623e8/README.md)
- [Architecture](https://github.com/NVIDIA-NeMo/Switchyard/blob/2bef154970d23cacf9c83b4fe9c1cd90212623e8/docs/architecture.md)
- [Routing overview](https://github.com/NVIDIA-NeMo/Switchyard/blob/2bef154970d23cacf9c83b4fe9c1cd90212623e8/docs/routing_algorithms/overview.md)
- [Stage-router behavior and calibration](https://github.com/NVIDIA-NeMo/Switchyard/blob/2bef154970d23cacf9c83b4fe9c1cd90212623e8/docs/routing_algorithms/stage_router_routing.md)
- [Escalation-router behavior](https://github.com/NVIDIA-NeMo/Switchyard/blob/2bef154970d23cacf9c83b4fe9c1cd90212623e8/docs/routing_algorithms/escalation_router_routing.md)
- [`v0.2.0` changelog and removals](https://github.com/NVIDIA-NeMo/Switchyard/blob/2bef154970d23cacf9c83b4fe9c1cd90212623e8/CHANGELOG.md)
- [Known issues](https://github.com/NVIDIA-NeMo/Switchyard/blob/2bef154970d23cacf9c83b4fe9c1cd90212623e8/docs/known_issues.md)
- [Harbor benchmark guide](https://github.com/NVIDIA-NeMo/Switchyard/blob/2bef154970d23cacf9c83b4fe9c1cd90212623e8/benchmark/README.md)
- [Prefill-router preprint](https://arxiv.org/abs/2603.20895)
- [Experimental NeMo Relay integration](https://docs.nvidia.com/nemo/relay/dev/configure-plugins/switchyard/about)
