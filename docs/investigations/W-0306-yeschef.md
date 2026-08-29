# W-0306 — yeschef investigation

Date: 2026-08-25
yeschef snapshot: `labscommunity/yeschef` `main` at 2026-08-25 (59 commits,
3 stars, 0 forks, 0 issues, 0 PRs, no tagged release, MIT).
Announcement: [cascadia.to/blog/yes-chef-delegate-tasks-to-local-models-with-claude-and-codex](https://cascadia.to/blog/yes-chef-delegate-tasks-to-local-models-with-claude-and-codex)
(2026-08-25). Evaluated from the blog post and the repository README; no local
install.

## Recommendation

Do not integrate yeschef into Orchestra. Do not add a yeschef backend, hub, or
MCP wiring. Two reasons dominate: layer mismatch and maturity.

- **Layer mismatch.** yeschef is an MCP tool server that runs INSIDE a Claude
  Code or Codex session. The hub sits on the agent machine; cooks clock in
  from machines that run local inference; tickets are durable in hub SQLite.
  Orchestra is an execution plane ABOVE harnesses. It launches a harness
  process, supervises it, and records the run. An integration would need one
  of three shapes, and each is wrong: (a) treat the hub as a run destination,
  but the hub is a ticket queue, not an agent CLI; (b) install yeschef into
  every launched harness session, which is harness-specific configuration
  that pollutes the run surface; (c) add cooks as a profile backend, which
  W-0105 already ruled out for the same reason — mid-run model selection
  belongs at the HTTP proxy seam, not in a new backend.
- **Maturity.** The repo has 59 commits and 3 stars, with no tagged release.
  The blog and the work item landed on the same day. W-0105 applied the
  "pre-alpha, do not ship yet" bar to Switchyard at 505 stars and 214 commits.
  yeschef sits an order of magnitude below that bar. Its value also depends on
  third-party local servers (Ollama, vLLM, LM Studio, exo, Cascadia) and
  FastMCP.
- **Overlap.** Orchestra already delivers the cost-saving value yeschef
  promises, at the item level: tiered profiles (workhorse / generalist /
  heavy), the router (one cheap call staffs each item to the cheapest adequate
  profile), runway headroom, and judgment seats (per-layer profile selection).
  A user who wants local models can already define a profile whose backend and
  model name the harness lists. The missing piece is not a queue; it is
  mid-run delegation of a bounded chunk to a cheaper model, and W-0105 already
  scoped that to the proxy seam.

## What yeschef is

- Two commands. `yeschef up` runs the hub: it generates tokens, wires Claude
  Code over HTTP MCP and Codex over a stdio bridge (`yeschef mcp-proxy`), and
  prints a clock-in line. `yeschef join` clocks in a cook: it auto-detects
  Ollama / vLLM / LM Studio / exo / Cascadia, verifies the model answers, and
  registers with a token.
- Tickets are durable background jobs in hub SQLite. The chef submits, works
  on, and checks the result from any session, days later. Cooks with file
  tools write into a per-ticket jail; the `cli` backend hands the whole ticket
  to a real coding agent, e.g. `claude -p "{prompt}"
  --dangerously-skip-permissions` with `ANTHROPIC_BASE_URL` pointed at Ollama.
- Rooms are bounded multi-model dialogues. Message caps, token budgets, idle
  timeouts, and stop phrases are enforced by the hub, not hoped for.
- Cooks have a TOML persona, tier tags (`tier:fast`), and opt-in tools:
  allowlisted shell, jailed file access, and web fetch that refuses internal
  addresses. Cloud cooks work through any OpenAI- or Anthropic-compatible
  endpoint, with presets for openrouter, openai, groq, together, deepseek, and
  fireworks.
- Security posture: bearer tokens on by default, LAN or Tailscale only, no TLS
  termination, cooks cannot self-register as privileged kinds.
- Honesty ledger: local cooks are 3–30x slower per token; they win on bounded,
  verifiable jobs; cost is electricity, not zero; rooms are real model output
  with a replay pipeline.

## Fit with Orchestra

| Concern | Orchestra | yeschef | Conclusion |
|---|---|---|---|
| Unit routed | Source item / whole run | Ticket inside a live session | Different layers |
| Destination | Codex / Claude / OpenCode / Reasonix profiles | MCP tools into a running session | No new harness to add |
| Durable state | SQLite runs + board | Hub SQLite tickets | Orchestra already durable at run level |
| Cost control | Router tiers + runway + seats | Free local cooks for grunt work | Same goal, different unit |
| Multi-machine | No; runs on one machine | Hub + cooks across a network | The one capability Orchestra lacks |
| Second opinion | Judgment seats, one model per layer | Rooms: two models argue | Rooms are a genuine borrow |
| Dependency | Stdlib Python, no runtime deps | yeschef-cli + inference servers + FastMCP | Keep optional and external |

## Ideas worth borrowing, ranked

1. **Bounded multi-model dialogue as a second-opinion primitive.** Judgment
   turns today run one model on one seat. A room with hard caps (message,
   token, idle) gives the verifier a structured way to spend one more cheap
   call when it needs judgment. Reuse the seat mechanism; add the caps.
   This is the strongest borrow.
2. **Honesty ledger text on local-model profiles.** When a profile points at a
   local model, print the trade: slower per token, wins only on bounded
   verifiable jobs, cost is electricity. Profile editing already explains
   tiers (c593610); extend that text to local-vs-hosted.
3. **Auto-detection of local inference servers.** `profiles.py` discovers
   models from the harness CLIs; it does not probe Ollama / vLLM / LM Studio
   endpoints. A discovery probe would let a user add a local-model profile
   without naming a model id the CLI list omits. Small and contained, no
   yeschef dependency.
4. **Mid-run sub-delegation to a cheaper model** (yeschef's flagship). Borrow
   the capability, not the tool. W-0105 already scoped it: generic per-profile
   environment overrides plus a documented proxy argument at the HTTP
   boundary. A yeschef ticket backend would give Orchestra a second queue with
   its own auth and failure modes and no run supervision. Do not add it.
5. **Durable background tickets checkable from any session.** Orchestra has
   this at run level: SQLite runs, the board, and the daemon. No action.

## Sources

- [Blog: Yes, Chef: Delegate Tasks to Local Models with Claude and Codex (2026-08-25)](https://cascadia.to/blog/yes-chef-delegate-tasks-to-local-models-with-claude-and-codex)
- [labscommunity/yeschef README at `main` (2026-08-25)](https://github.com/labscommunity/yeschef)
