# W-0171 — DeepSeek Harness (dsh) as a fifth backend

Date: 2026-08-14
dsh snapshot: `47f943859bef60e4160492346772ded9b24f765a`
(`master`, 2026-08-13), npm `@deepseek-ai/dsh@0.1.0-rc.6`

Evaluated from published README, CLI reference, package contracts, and
`apps/cli/src/args.ts`. No local install and no live ACP handshake. `npx
@deepseek-ai/dsh --help` did not return within 60s.

## Recommendation

Do not add dsh as a fifth Dromond backend.

The product is a first-party DeepSeek harness with real ACP, MCP, hook, and
session-log machinery. It does not clear the same bar W-0148 applied to Codex,
Claude Code, OpenCode, and Reasonix. The two automation surfaces Dromond would
actually drive — shipped `dsh --profile headless` and the ACP demo — cannot
resume a session by id, and headless does not emit a structured event stream on
stdout.

Dromond already runs DeepSeek models through Reasonix. A fifth `build_cmd` would
buy a moving RC flag surface, not a better DeepSeek route. Hold until a tagged
non-rc release grows `--resume` plus stdout JSONL (or ACP `session/load` plus
richer `session/update`) on a published command.

## What is available now

`dsh` is a Cordis plugin tree. A profile is an ordered stack of bundle patches
under `$DSH_HOME` (else `~/.dsh`). Shipped templates: `web` and `headless`. The
TUI package and legacy entrypoints were deleted on 2026-08-04; launcher help
still shows `dsh --profile tui --resume <id>` as an example.

| Surface | What it is | Dromond-relevant limit |
|---|---|---|
| `dsh web` | Browser UI on `127.0.0.1:3080` | Human product, not a worker |
| `dsh --profile headless "job"` | One-shot: create a fresh session, wait for idle, print last assistant text, exit | No `--resume`. Stdout is final text. Stderr is empty on success. No `--output-format` |
| `pnpm run demo:acp` / `dsh-acp-demo` | Automation-only ACP server on JSON-RPC stdio | Example bin, not `dsh acp`. Fresh sessions only. Committed text only |
| `deepseek-harness-sdk` | Python subprocess SDK over JSON-RPC | Reuses `session_id`. Returns events. Adds a runtime dependency Dromond does not take |

Headless is explicit: one submitted task, no interactive follow-up, last
non-empty assistant text on stdout, exit 0 only when the owned `turn/end` is
`completed`. Session events exist inside the process. They are not the CLI
contract.

Internal persistence is real. The JSONL backend writes
`<root>/--<cwd>--/<id>/session.jsonl.zstd` by default (`compression: 'none'`
for raw `.jsonl`). `SESSION_FORMAT_VERSION` is `0` with no compatibility
promise. Compressed files are not line-readable. Dromond's supervisor tails
stdout JSONL; it cannot tail a zstd session file without a new reader.

Interrupt is supervisor-friendly: first `SIGTERM` drains plugins for five
seconds and exits 0; `SIGINT` reports 130; a second signal forces. Persistence
is incremental, so kill is unlikely to corrupt a log. That is not the same as
resume: neither headless nor ACP exposes load-by-id.

Per-spawn isolation is the strongest Dromond fit:

- `--patch <path>` overlays after profile and home layers (repeatable)
- `$DSH_HOME` relocates the whole home
- `DSH_PERMISSION_MODE`, `DSH_TOOLS_MODE`, `DSH_SESSION_ROOT`, `DSH_CORDIS_CONFIG`
- credentials from inherited env, then `$DSH_HOME/.credentials.yaml`, then `.env`

Caveat: `$DSH_HOME/cordis.patch.yml` is shared by every profile and outranks
the per-profile patch. A Dromond run must set its own `DSH_HOME` (or rely only
on `--patch`) so one run cannot rewrite another.

MCP exists as `@deepseek-ai/dsh-mcp-client`. One plugin row per server, enabled
by a patch. Tools register as `mcp__<server>__<name>`. No MCP server is on by
default. ACP `session/new` accepts only empty `mcpServers` and
`additionalDirectories`; non-empty values reject. Resources and Prompts are not
bridged.

Hooks exist as opt-in bridges, not default shell hooks. Native extension is a
Cordis plugin on typed events. `@deepseek-ai/dsh-hooks-claude-code` can point
at an existing `hooks.json` and map SessionStart, UserPromptSubmit, PreToolUse,
PostToolUse, Stop, SubagentStart, SubagentStop. Config is process-level, parsed
once. 23 of Claude Code's 30 events are ignored. Stop can force another step
via `steer()`, but `{"continue": false}` does not halt the run. The Codex
bridge is a sibling package. Neither is documented as part of the shipped
headless template; Dromond would `--patch` them on.

ACP is real and narrow. `initialize` advertises baseline prompts only. No
session, editor, terminal, filesystem, or MCP capability. `session/new` is
always fresh. `session/prompt` waits for idle and reports `end_turn` or
`cancelled`. `session/cancel` works. `session/update` emits committed assistant
text only — no reasoning, tools, plans, or usage. Known limitation: load, list,
resume, delete, and fork are unsupported. No vendor steer method is advertised.

The Python SDK is the only documented automation path with resume and events:
reuse `session_id`, read `RunResult.events`, keep JSONL under
`DSH_SESSION_ROOT`. It launches a bundled `dsh-jsonrpc-agent` binary. That is
a new runtime dependency. DESIGN §6 is exec everywhere, one `build_cmd` per
backend, no adapter classes. A Python SDK backend would be a different product
choice, not a fifth runner.

## Evidence quality

This is a docs-and-contract review, not a live handshake. W-0148 verified the
four backends against installed CLIs plus ACP. dsh was not installed here.
Treat ACP method names and headless exit codes as documented, not probed.

Maturity is the controlling signal:

- README: developer preview; **THERE WILL BE COMPATIBILITY-BREAKING CHANGES.**
- AGENTS.md: no external consumers yet; backends reject old on-disk formats;
  `SESSION_FORMAT_VERSION` stays at 0 with no compatibility promise.
- npm: `0.1.0-rc.6`, six versions, 61 dependencies, published 2026-08-13.
- TUI removed two weeks before this review. Help text still mentions it.
- Repo traction is large (~94k stars, ~8.6k forks, 12k commits). Traction is
  not a stable worker CLI.

## Fit with Dromond (W-0148 matrix)

| Bar | Codex / Claude / OpenCode / Reasonix | dsh now | Verdict |
|---|---|---|---|
| Headless structured stream | `--json` / `stream-json` / OpenCode `--format json` on stdout | Headless prints final text. Session JSONL is internal, often zstd. OTLP is optional | **Fail** |
| Resume by id | All four resume. Kill+resume is the interrupt story | Headless always fresh. ACP fresh only. Python SDK reuses id | **Fail** on the exec/ACP paths Dromond uses |
| Safe interrupt | Plain kill is safe | SIGTERM/SIGINT drain; persistence is incremental | **Pass** (interrupt only) |
| Per-spawn config | flags / `OPENCODE_CONFIG_CONTENT` / `-c` | `DSH_HOME` + `--patch` + env | **Pass**, if each run gets its own home |
| Lifecycle hooks | Claude/Codex/Reasonix: Stop/SessionStart. OpenCode: JS plugin | Claude/Codex bridges exist; not default; partial mapping | **Partial** |
| Per-run MCP | backend flags / config content | `--patch` mcp-client rows. ACP rejects `mcpServers` | **Partial** |
| ACP | Reasonix + OpenCode; Reasonix steers mid-turn | Speaks ACP. Demo bin. No resume, no steer, committed text only | **Partial** |
| `--add-dir` | claude/codex/reasonix | ACP rejects extra directories. Headless has no flag | **Fail** |

DESIGN §6 still says four backends and exec everywhere. dsh would be a fifth
`build_cmd` that cannot resume and cannot feed the existing JSONL ingest
without a new parser. That is more code than the gain.

The plugin architecture is cleaner than wrapping foreign CLIs. Dromond does not
need that cleanliness until the worker command exists.

## When to look again

Re-open this item when all of these are true:

1. A tagged non-rc release (`0.1.0` or later), not another RC.
2. Shipped `dsh --profile headless` grows `--resume <id>` and a stdout
   stream-json/JSONL mode, **or** a published `dsh acp` (not the demo)
   implements `session/load` and emits tool/reasoning updates.
3. `SESSION_FORMAT_VERSION` documents a compatibility rule.

A smaller later spike, not a backend: pin `0.1.0-rc.6`, drive the Python SDK
against one disposable worktree, and measure resume plus event completeness.
Do not add `deepseek-harness-sdk` to Dromond for that spike. Do not add a
Switchyard-style in-process embed. Keep DeepSeek traffic on Reasonix until
the CLI bar is met.

## Sources

- [Repository README](https://github.com/deepseek-ai/deepseek-harness/blob/47f943859bef60e4160492346772ded9b24f765a/README.md)
- [AGENTS.md (layout, headless, ACP demo, format version)](https://github.com/deepseek-ai/deepseek-harness/blob/47f943859bef60e4160492346772ded9b24f765a/AGENTS.md)
- [Architecture](https://github.com/deepseek-ai/deepseek-harness/blob/47f943859bef60e4160492346772ded9b24f765a/docs/architecture.md)
- [CLI README](https://github.com/deepseek-ai/deepseek-harness/blob/47f943859bef60e4160492346772ded9b24f765a/apps/cli/README.md)
- [CLI behavior reference](https://github.com/deepseek-ai/deepseek-harness/blob/47f943859bef60e4160492346772ded9b24f765a/apps/cli/reference/README.md)
- [Launcher args](https://github.com/deepseek-ai/deepseek-harness/blob/47f943859bef60e4160492346772ded9b24f765a/apps/cli/src/args.ts)
- [Headless bundle](https://github.com/deepseek-ai/deepseek-harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/bundle/headless/README.md)
- [ACP server contract](https://github.com/deepseek-ai/deepseek-harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/acp/acp/README.md)
- [MCP client](https://github.com/deepseek-ai/deepseek-harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/mcp/mcp-client/README.md)
- [Claude Code hook bridge](https://github.com/deepseek-ai/deepseek-harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/hooks/hooks-claude-code/README.md)
- [JSONL persistence](https://github.com/deepseek-ai/deepseek-harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/session/session-persistence-jsonl/README.md)
- [App boot / `$DSH_HOME`](https://github.com/deepseek-ai/deepseek-harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/boot/app-boot/README.md)
- [Python SDK tutorial](https://github.com/deepseek-ai/deepseek-harness/blob/47f943859bef60e4160492346772ded9b24f765a/docs/user/guide/python-sdk.md)
- [Python SDK reference](https://github.com/deepseek-ai/deepseek-harness/blob/47f943859bef60e4160492346772ded9b24f765a/python/sdk/README.md)
- [npm `@deepseek-ai/dsh`](https://www.npmjs.com/package/@deepseek-ai/dsh)
