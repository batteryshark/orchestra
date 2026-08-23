# Screenshots

Every image is a real screen with real data. Nothing is staged or mocked.

The dashboard shots come from Chrome at 1440–1600 CSS px.

## Web dashboard

| File | Shows | Caption |
| --- | --- | --- |
| `statistics.png` | RUNS view, statistics popup | What the fleet has cost: worker time, tokens and money, totalled and broken out per profile. |
| `runway.png` | RUNWAY view | How much quota is left at each provider, per rolling window, so the dispatcher knows what it can still afford to run. |

## What is deliberately absent

- **No SETTINGS screenshot.** That view renders the absolute path of
  `config.toml`, which is a home directory.
- **No HEALTH screenshot.** The daemon log prints the URL the service binds
  to, which is a tailnet address, and it echoes the shared key.
- **No live runs.** Nothing was in flight when these were taken, so every
  dashboard header reads `0 live`. Staging a run to make the number larger
  would have been a lie.
