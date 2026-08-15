# Screenshots

Every image is a real screen with real data: 31 runs against the `maestro`
project, 11 profiles, six providers. Nothing is staged or mocked.

The web dashboard shots come from Chrome at 1440–1600 CSS px. The iOS shots
come from an iPhone 17 Pro Max simulator, downscaled to 660 px wide.

## Web dashboard

| File | Shows | Caption |
| --- | --- | --- |
| `dashboard-runs.png` | RUNS view | Every run the daemon has dispatched, with its id, slug, status, profile, harness and Work item — and the selected run's transcript beside it. |
| `run-detail.png` | RUNS view, trace tab | One run's trace: the model's own reasoning, the tool calls it made, and the token accounting, in the order they happened. |
| `profiles.png` | PROFILES view | Eleven profiles across four harnesses. Each names a model, an effort, a priority, a tier, and which profiles it is allowed to spawn. |
| `statistics.png` | RUNS view, statistics popup | What the fleet has cost: worker time, tokens and money, totalled and broken out per profile. |
| `runway.png` | RUNWAY view | How much quota is left at each provider, per rolling window, so the dispatcher knows what it can still afford to run. |

## iOS client

| File | Shows | Caption |
| --- | --- | --- |
| `ios-runs.png` | Runs tab | The same daemon from a phone: every run, searchable, with a live badge on the tab. |
| `ios-run-detail.png` | Run detail, trace tab | A run's trace on the phone, with the same tab strip and the same three-state disclosure — collapsed, previewed, or whole. |

## What is deliberately absent

- **No SETTINGS screenshot.** That view renders the absolute path of
  `config.toml`, which is a home directory.
- **No HEALTH screenshot.** The daemon log prints the URL the service binds
  to, which is a tailnet address, and it echoes the shared key.
- **No live runs.** Nothing was in flight when these were taken, so every
  dashboard header reads `0 live`. Staging a run to make the number larger
  would have been a lie.
- **Some tool-call payloads are left collapsed.** A worker runs inside a git
  worktree under the operator's home directory, so an expanded tool call
  often prints an absolute path. The disclosures that carry one stay shut;
  the ones that do not are open.
