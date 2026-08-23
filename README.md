<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/orchestra-wordmark-dark.svg">
  <img src="assets/orchestra-wordmark.svg" alt="Orchestra" width="360">
</picture>

## What this is

Orchestra is a local execution plane for agent work. It accepts a mission from
a person, an agent, or Work, runs it through a configured agent CLI, and keeps
a durable record of the run, its trace, controls, and outcome.

The current release supports four harnesses: Codex, Claude Code, OpenCode, and
Reasonix. Orchestra gives their runs a common operating surface. The harnesses'
own tools, permissions, configuration, and model catalogs remain different.

Direct dispatch works without Work. Work automation and sign-off, isolated git
landing, routing and conducting, model-based observation, Nod, runway, and the
iOS client sit around the execution core. None is required for a direct run.
Work's automated sign-off verifier is disabled by default.

**Orchestra — one dispatcher, many runners, one problem.**

## Runs, isolation, and landing

A manual dispatch uses the registered project checkout unless `--worktree` is
passed. Use a worktree for a run that will edit files; shared-checkout dispatch
is best kept for research and inspection.

A successful isolated run has a branch named `orchestra/run-N`, which
Orchestra automatically attempts to land. It rebases in a scratch worktree,
runs declared checks and tripwires, then moves the base ref by compare-and-swap.
If the base moved, Orchestra rebases and retries. A conflict or repeated race
leaves the branch for intervention. After landing, Orchestra refreshes the
owner's base checkout only when Git can preserve local edits.

Landing does not review the diff against acceptance criteria. Without declared
checks, it relies on tripwires and their optional mission-alignment judge, so
configure a real test, lint, or build command before trusting automatic landing.

When Work automation is enabled, its sweeper claims delegated items and requests
isolated runs by default. It currently falls back to the shared checkout if
worktree creation fails.

## What it looks like

Captions and the full set: [`docs/screenshots/`](docs/screenshots/README.md).

![The runs board: id, slug, status, profile, harness and Work item down the left, the selected run's trace on the right](docs/screenshots/dashboard-runs.png)

![One run's trace: reasoning summaries, tool calls, and token accounting in execution order](docs/screenshots/run-detail.png)

![Statistics: worker time, tokens and cost, totalled and per profile](docs/screenshots/statistics.png)

![The optional iOS client, reading the same daemon over the tailnet](docs/screenshots/ios-runs.png)

## What you need

- Python 3.11 or later and [uv](https://docs.astral.sh/uv/). Orchestra uses the
  standard library and SQLite and has no runtime package dependencies.
- Git and a repository to work in.
- At least one signed-in agent CLI: `codex`, `claude`, `opencode`, or
  `reasonix`. Orchestra uses the CLI's existing authentication.
- Work only if you want delegated-item intake, project discovery, planning, or
  writeback. Standalone projects use `orchestra project add .`.

`orchestra service` installs a launchd agent on macOS or a per-user Scheduled
Task on Windows. The daemon can also run in the foreground.

## Install

```sh
git clone https://github.com/batteryshark/orchestra
cd orchestra
uv sync
```

## First run

Prepare the central home at `~/.orchestra/` and install harness hooks:

```sh
uv run orchestra init
uv run orchestra doctor
```

Register this checkout if it does not come from Work:

```sh
uv run orchestra project add .
```

Discover models, add a profile, and dispatch an isolated run:

```sh
uv run orchestra profiles discover
uv run orchestra profiles set fast --backend --model --effort --tier 1
uv run orchestra dispatch --to fast --worktree "Fix the failing auth test"
```

Dispatch returns a run id as soon as the run starts. It does not mean the run
succeeded. Inspect the durable outcome:

```sh
uv run orchestra runs --active
uv run orchestra show 1
```

## Run the daemon

```sh
uv run orchestra daemon                     # foreground
uv run orchestra service install --start    # launchd or Windows Scheduled Task
```

The daemon serves the dashboard on port 3011 by default and prints its address.
Browser access uses the shared key written by `orchestra init`, either in the
`X-Orchestra-Key` header or as `?key=` on the first visit. Workers receive a
revocable per-run token for restricted API access.

From the dashboard or CLI you can inspect traces, send a correction through the
run's supported delivery path, and stop a run:

```sh
uv run orchestra tell 7 "Keep the public API unchanged"
uv run orchestra check 7
uv run orchestra kill 7
```

Pausing prevents new dispatch. At present it also suspends some daemon policy
passes, including Work writeback and conductor processing, until resumed.
Running worker processes continue.

## Install it as a command

`uv run orchestra` works from a clone. To put the command on your path while
keeping it linked to the working tree:

```sh
uv tool install --editable .
```

Restart an installed service after a code change:

```sh
orchestra service restart
```

## Teach an agent to drive it

[`skills/orchestra/`](skills/orchestra/SKILL.md) explains how to register a
project, dispatch, inspect a trace, send a correction, and land an isolated
branch. Symlink it for tools that read the Claude skill format:

```sh
ln -sfn "$PWD/skills/orchestra" ~/.claude/skills/orchestra
```

## Where things live

| What | Where |
|---|---|
| State: database, briefs, logs, worktrees | `~/.orchestra/` |
| Database | `~/.orchestra/orchestra.db` |
| Config | `~/.config/orchestra/config.toml` |
| Isolated run branches | `orchestra/run-N` |
| Environment overrides | `ORCHESTRA_*` |
| Work agent identity | `orchestra` |

## The repository

| Path | What |
|---|---|
| `orchestra/` | the execution core and built-in policy modules |
| `orchestra/dashboard.html` | the dashboard, with no frontend build step |
| `ios/` | the optional iOS client |
| `assets/` | the mark and wordmark |
| `docs/screenshots/` | the images above and their captions |
| `tests/`, `run_tests.py` | the test suite and runner |
| `DESIGN.md` | the current contract, implementation boundaries, and known gaps |

`uv run orchestra --help` lists every command. Run
`uv run orchestra <command> --help` for command-specific help.

## License

MIT. See [LICENSE](LICENSE).
