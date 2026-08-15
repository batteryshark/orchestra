---
name: dromond
description: Dispatch and supervise autonomous coding agents through Dromond — run work in isolated git worktrees, watch traces, send corrections, and land verified branches. Use when asked to delegate coding work, run several agents at once, check on a running agent, or merge an agent's branch.
---

# Dromond

Dromond runs coding agents in isolated git worktrees and lands their work on
your base branch after verification. You drive it with the `dromond` CLI.

Check it is alive before anything else:

```
dromond doctor
```

That prints which harnesses are installed, where state lives, and whether the
daemon is running. If a harness shows as missing, runs on that profile will
fail — say so rather than dispatching into it.

## Register the project once

Dromond can only dispatch into a directory it knows:

```
dromond project add .
dromond project list
```

`list` marks each project `work` or `local`. A `local` project is one adopted
here; Dromond owns it outright and nothing else needs to be running.

## Dispatch

```
dromond profiles                                   # what you may dispatch to
dromond dispatch --to <profile> --worktree "<mission>"
```

`--worktree` gives the run its own checkout and branch (`dromond/run-N`). Use it
whenever the run will write files. Without it the run works in the current
checkout, which is right only for read-only research and is unsafe while
anything else is editing.

Write the mission as a complete instruction. The run does not see this
conversation, cannot ask a follow-up before it starts, and gets exactly what
you type. State what to change, what "done" means, and how to verify it.

Dispatch returns immediately with a run id.

## Watch

```
dromond runs                # every run, newest first
dromond runs --active       # just what is in flight
dromond show <id>           # status, summary, tokens, branch, workdir
```

`show` is the one to read after a run finishes: it carries the run's own handoff
summary, which is where the agent says what it did and what it left undone.

## Intervene

```
dromond tell <id> "<correction>"    # queued; delivered at the run's next safe point
dromond check <id>                  # ask the observer whether it is still working
dromond kill <id>                   # stop it
```

`tell` does not interrupt mid-tool-call. `check` costs a model turn, so use it
when a run looks stalled rather than as a status poll — `runs --active` is free.

## Land the work

```
dromond merge <branch>
```

Verification runs in order and stops at the first failure: the repository's
declared checks, then mechanical tripwires (files outside the project,
deletions, oversized diffs), then a diff review against acceptance criteria when
there are any. The merge happens in a scratch worktree and the base ref moves by
compare-and-swap, so a base that moved underneath refuses rather than
overwriting.

It refuses while the base checkout is dirty. Commit or stash first — that is a
guard against landing under someone mid-edit, not a bug.

A repository that declares no checks lands on tripwires alone. If you are
dispatching into a repo with a test command, declare it in the project's
`[merge.checks]` before trusting an automatic landing.

## What to tell the person

Report the run id and what it actually did, from `show <id>`. If a run failed,
give the reason from its summary rather than only its status. If you dispatched
several, say which landed and which did not.

Do not claim a run succeeded because dispatch returned — dispatch returning
means it started.

## Boundaries

- One mission per run. A run given three unrelated jobs does the first well.
- Do not dispatch a run whose mission is to dispatch more runs unless the
  profile's `spawn_profiles` allows it; the depth limit exists so an unattended
  fleet cannot grow itself.
- Do not `kill` a run that is merely slow. Read its trace first — a run writing
  files is working, and a killed run's session is never resumed.
