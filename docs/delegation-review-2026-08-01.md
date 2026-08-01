# Delegation review — findings from a 24-run day

Written after driving Orchestra through a full reverse-engineering bring-up
(23 runs in one project, 5 in another, four models, two supervisor/implementer
loops). Everything below cost real time or is a latent version of something
that did. Ranked by what actually hurt.

Correcting one thing first, because it shaped my expectations wrongly and may
shape others': **child→parent consultation is well modelled.** `cmd_consult`
routes a spawned child's question to its lead via `_record_interrupt` when the
lead is alive, is the recorded requester, and has `supervisor_protocol >= 1`
(`cli.py:366-395`). `supervise()` sets that flag for every run it manages
(`supervise.py:843`), so the path works in practice. Delivery is not the gap.
Blocking is.

---

## 1. `spawn`'s worktree requirement is a dead end, not a fallback

**What happened.** A supervised run tried to spawn a child for an explicitly
read-only advisory ("do not edit files, return file:line evidence"). It failed:

```
spawn request 1 failed without terminating lead run 20:
orchestra: --worktree needs the project to be a git repository
```

The lead survived, but the delegation was simply lost. It did not retry.

**Why.** `worktree.create()` hard-exits when `root/.git` is absent
(`worktree.py:35-36`). `spawn` defaults to an isolated worktree and only skips
it with `--shared-workdir` (`cli.py:674`).

**Why it is worth fixing.** A worktree buys isolation from concurrent writes. A
read-only child needs none, so the strictest possible precondition is being
enforced for the case that least requires it. And the failure is terminal:
nothing tells the worker that `--shared-workdir` exists, so a capable model
reads "failed" and gives up on a recoverable condition.

**Suggested fix, in preference order:**

1. Fall back to a shared workdir with a warning when the project is not a git
   repository, rather than failing. Isolation is an optimisation here, not a
   correctness requirement.
2. Failing that, put the remedy in the message: `... is not a git repository;
   retry with 'orchestra spawn --shared-workdir'`. Orchestra is generally
   excellent at this — the interrupt and `--question-wait` errors both name
   their fix — so this one is an outlier.

---

## 2. No dependency between dispatches — and hand-rolling it is a trap

**The most expensive finding of the day.**

**What happened, twice.** A consumer mission was dispatched alongside its
producer and died on missing input (correctly, and it reported cleanly). I then
hand-rolled sequencing:

```sh
nohup sh -c 'orchestra wait 8 && orchestra dispatch ...' &
```

Later I needed to change the child's target model, so I `pkill`ed the waiter and
launched a replacement. The `pkill` matched only one of them. **Both fired**,
producing three duplicate runs of the same review (17, 18, 19) — one of which
the owner killed, after which the survivor spawned another. They competed for
the machine with a long-running lift the owner was deliberately protecting.

**Why it happened.** There is no `depends_on` / `--after` anywhere in the CLI.
Sequencing is therefore the orchestrator's problem, solved with shell, and shell
gives no deduplication, no cancellation, and no visibility — `orchestra status`
cannot show a pending chain because it does not know one exists.

**Suggested fix.** `orchestra dispatch --after <run-id> [--after <run-id>...]`,
recorded in the DB and fired by the supervisor that completes the last
dependency. Then:

* it is visible in `status` as pending-on-N;
* it is cancellable (`orchestra cancel` on a not-yet-fired dispatch);
* it cannot double-fire, because the row is claimed transactionally the way
  `spawn_requests` already is;
* it can decline to fire if the dependency failed, which is the behaviour the
  blocked run had to implement by hand.

The `spawn_requests` table is already the right shape for this.

---

## 3. Children cannot ask a blocking question

`--allow-question` (with `--question-wait` and a mandatory fallback) exists only
on `dispatch` (`cli.py:551`). `spawn` has no equivalent, and `consult` is
non-blocking by contract — "worker is continuing".

So a spawned child that hits a genuine ambiguity **cannot wait for an answer**.
It proceeds on an assumption, and that assumption surfaces at review as a silent
decision rather than as a question. In a supervisor/implementer loop this is
structural: the brief has to be complete because it is the only channel, and
"the brief must leave no decisions" stops being advice and becomes a
requirement.

**Suggested fix:** `orchestra consult --wait <seconds> --fallback "<assumption>"`
— same contract as `--allow-question`, available from inside a run. The routing
already exists; only the wait and the recorded fallback are missing. Keep
non-blocking as the default.

---

## 4. No tier model, so escalation is unconstrained

A tier-2 supervisor tried to spawn the heaviest available profile for a bounded
checking task. Nothing prevented it; only the worktree failure (finding 1)
stopped it, and that was luck.

`role` is prose (`config.py:56-86`) and several entries already say "tier" in
English. Nothing machine-readable exists.

**Suggested fix:** optional `tier = <int>` per agent. `spawn` refuses a target
above the parent's tier with a message naming `consult` as the alternative.
Absent tiers mean unconstrained, so nothing breaks for existing configs.

The rule this encodes: **a run should not be able to spend more than its parent
was authorised to.** If a child genuinely needs a stronger model, the
decomposition was wrong, and that belongs to the orchestrator rather than being
resolved in flight.

---

## 5. `interrupt` has no `--file`, and interrupts are the longest messages

`send` has `--file` (`cli.py:2124`); `interrupt` does not — it takes the message
as positional argv only.

Interrupts carry the *most* text of any command, because they exist to correct a
worker mid-flight and therefore restate context. Passing multi-paragraph text
through argv means shell quoting, and I mangled two interrupts today with
backticks that the shell expanded before Orchestra ever saw them:

```
(eval):1: command not found: support
```

Both were silently truncated corrections delivered to running workers.

**Suggested fix:** `orchestra interrupt <run> --file <path>`, mirroring `send`.
One-line change, removes a whole class of silent corruption.

---

## What is already good, and worth not regressing

* **Consult routing** (finding 0). Actively interrupting the parent rather than
  dropping a message in an inbox is the right call, and the guard conditions are
  careful — alive, is-the-requester, protocol-capable, not-an-ensemble.
* **Blocker discipline.** A worker told "if the input is absent, say so and
  stop" searched broadly, reported precisely, and invented nothing. The
  `report "BLOCKER: ..."` convention plus a run-bound handoff made that
  legible without any orchestrator polling.
* **Quota warnings at dispatch.** `dispatch to 'kimi-max' targets Moonshot AI
  (19% headroom)` was correct, timely, and shaped routing for the rest of the
  day.
* **Error messages that name their own fix.** The `--question-wait requires
  --allow-question` and detached-supervisor interrupt errors both tell you
  exactly what to do. Finding 1 is the exception that proves the pattern.
