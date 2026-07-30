# Runner token and latency optimization spike

Date: 2026-07-25

## Question

Where does Orchestra add avoidable token spend or latency when it dispatches,
redirects, and resumes runners?

This spike examines Orchestra-owned payloads and session transitions. It does
not attempt to optimize model-generated tool output or project source context.

## Method

- Inspected the dispatch, continuation, interrupt, check-in, queue, and handoff
  paths.
- Measured 17 historical local runs (11 completed OpenCode runs and 3 completed
  Codex runs, plus failed runs with no usable token event).
- Used provider-reported usage events where available.
- Used UTF-8 byte counts and `characters / 4` only as a tokenizer-independent
  prompt-size proxy. Those estimates are directional, not billing data.

OpenCode reports uncached input and cache reads per step. Codex reports a
turn-level input total that includes cached input, so its uncached input below
is `input_tokens - cached_input_tokens`.

## Baseline observations

| Cohort | n | Median duration | Median model steps | Median uncached input | Median cache-read |
|---|---:|---:|---:|---:|---:|
| OpenCode, new session | 7 | 515 s | 81 | 118,118 | 4,855,808 |
| OpenCode, continuation | 4 | 319 s | 54 | 28,584 | 11,008,924 |
| Codex, new session | 2 | 530 s | 1 reported turn | 124,582 | 2,890,496 |
| Codex, continuation | 1 | 16 s | 1 reported turn | 59,236 | 548,736 |

The sample is small and missions differ, so it cannot establish causal speed
or quality differences between backends. It does show that repeated context
processing across tool turns dwarfs the initial Orchestra wrapper. Reducing
runner turn count is therefore more valuable than shaving a few words from a
one-time brief.

The current working tree had already reduced a representative fresh-run brief
from the historical median of 6,536 bytes to 1,123 bytes (about 83%). Its full
generated `ORCHESTRA.md` is 15,317 bytes, but the generated agent pointer tells
only interactive orchestrators to read it; runners should continue receiving
their compact run brief instead of the orchestration playbook.

## Implemented prototype

### Inject interrupt and check-in bodies directly

Previously, the supervisor resumed a stopped runner with an instruction to run
`orchestra inbox --unread --mark-read`. The supervisor already had the exact
message bodies. That design added:

1. a runner subprocess/tool call;
2. tool output containing the same message text;
3. another model step to interpret that output.

The supervisor now embeds the delivered bodies, sender, and delivery kind in
the resume prompt and marks exactly those messages read. Other unread inbox
messages remain untouched. This removes one coordination round trip per
interrupt or periodic check-in.

### Keep continuation prompts incremental

A backend-session continuation retains the original mission, project
instructions, and coordination contract. The continuation wrapper no longer
repeats the full coordination section. It carries only:

- the run linkage;
- the new instructions;
- the optional blocking-question reminder;
- the mandatory handoff reminder.

The representative continuation wrapper fell from 1,240 to 321 bytes, or from
roughly 308 to 80 proxy tokens (74%).

### Trial batch-oriented execution

Fresh worker briefs now tell every backend to batch independent read-only
searches, file reads, and diagnostics while keeping dependencies and
overlapping writes sequential. The default `codex-terra` profile uses
`gpt-5.6-terra`, whose model metadata advertises Code Mode support, and enables
Codex's experimental `code_mode` behind a profile-scoped `--enable code_mode`
flag. The profile suppresses the generic unstable-feature banner for this
intentional opt-in; model-compatibility warnings remain enabled. The heavier
`codex` profile and other Codex profiles remain unchanged until the trial has
comparative evidence.

## Next experiments, in priority order

1. **Add per-run efficiency telemetry.** The existing `orchestra usage` view
   aggregates by profile. Record or derive run duration, model steps, uncached
   input, cache reads, output, interrupt count, and check-in count so changes
   can be compared by cohort rather than impression.
2. **A/B direct delivery.** Compare otherwise similar interrupted runs for
   time-to-next-model-action and total post-interrupt steps. Expected result:
   one fewer tool call and model step per delivery.
3. **Revisit periodic check-ins.** A check-in intentionally stops and resumes a
   healthy worker, which has token and latency cost. Test a longer default or a
   policy based on absence of meaningful progress rather than wall-clock time.
   Do not change this without measuring the observability tradeoff.
4. **Reduce tool-turn count in runner guidance.** Encourage bounded batching of
   related reads and proportional verification. Historical cache-read volume
   suggests this has substantially more upside than further markdown trimming.
5. **Measure project-instruction loading by backend.** Verify whether each CLI
   honors the interactive-only `ORCHESTRA.md` pointer. If a backend loads the
   full playbook for workers, split worker and orchestrator doctrine into
   separate instruction files.

## Guardrails

- Do not summarize or truncate mission requirements merely to reduce tokens.
- Do not replace exact message delivery with lossy summaries.
- Preserve sender, run, work-item, and handoff identity.
- Evaluate token savings together with correctness, completion rate, and
  elapsed time; a shorter prompt that causes rework is a regression.
