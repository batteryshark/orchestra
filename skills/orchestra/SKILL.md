---
name: orchestra
description: Use Orchestra to dispatch and supervise an already-formed coding or research mission through Codex, Claude Code, OpenCode, or Reasonix. Use when asked to delegate work, inspect a run, send a correction, or land an isolated branch.
---

# Orchestra

Orchestra runs missions through Codex, Claude Code, OpenCode, or Reasonix behind
one lifecycle, trace, control, and result surface. Work is an optional intent
and ledger adapter. A locally registered project needs no Work server.

Check the installation before dispatching:

```sh
orchestra doctor
```

This reports installed harnesses, state paths, configuration problems, and
whether a managed daemon service is installed. Do not send a run to a missing
harness.

## Register the project

Orchestra dispatches only into registered directories:

```sh
orchestra project add .
orchestra project list
```

`project list` labels each entry as local or Work-backed. Local registration
is enough for direct dispatch.

## Dispatch

```sh
orchestra profiles
orchestra dispatch --to <profile> --worktree "<mission>"
```

Use `--worktree` whenever the run will edit files. Without it, the run uses the
registered checkout. Shared execution is appropriate for read-only research but
unsafe while another process is editing the same files.

Write a self-contained mission. The run does not see the conversation that led
to it. State what to change, what done means, and how to verify it.

Dispatch returns a run id when the run starts, not when it succeeds.

## Watch

```sh
orchestra runs
orchestra runs --active
orchestra show <id>
```

`show` carries the run's final status, handoff summary, token usage, branch,
and working directory.

## Intervene

```sh
orchestra tell <id> "<correction>"
orchestra check <id>
orchestra kill <id>
```

`tell` uses the live ACP channel when one exists. Exec runs receive it at the
next safe action boundary. `check` may use an out-of-band model turn, so
inspect status and trace first.

## Land an isolated branch

A successful isolated run automatically attempts landing. If its branch remains
after a pause or landing failure, retry it with:

```sh
orchestra merge <branch>
```

Landing rebases in a scratch worktree, runs the repository's declared checks
and mechanical tripwires, then updates the base ref by compare-and-swap. It does
not review the diff against acceptance criteria. A tripped limit may use an
available observer profile to judge whether the change matches the mission.

Without declared checks, landing relies on that tripwire policy. Configure test,
lint, or build commands in `[merge.checks]` before trusting it.

After landing, Orchestra refreshes an owner checkout sitting on the base branch
only when Git can preserve local edits. If refresh would overwrite an edit, the
checkout keeps its pre-merge tree and the result reports what happened.
`require_clean = true` instead refuses landing when edits overlap it.

## Report the outcome

Report the run id and the outcome shown by `orchestra show <id>`. If the run
failed, include its recorded reason. For several runs, say which completed,
landed, failed, or remain active.

Never claim success because dispatch returned.

## Boundaries

- Give one coherent mission to each run.
- Use an isolated worktree for mutation.
- Read the trace before killing a run that may simply be slow.
- Do not instruct a run to create child runs. Orchestra does not implement child
  launch yet.
