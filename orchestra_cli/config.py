import sys
import tomllib
from pathlib import Path

from orchestra_cli import paths

DEFAULT_QUESTION_WAIT_SECONDS = 1800
MIN_QUESTION_WAIT_SECONDS = 10
MAX_QUESTION_WAIT_SECONDS = 86400
DEFAULT_RUN_TIMEOUT_SECONDS = 36000
DEFAULT_STALL_TIMEOUT_SECONDS = 1800

DEFAULT_CONFIG = f"""\
# Orchestra roster + settings. Global file: ~/.config/orchestra/config.toml
# Project overrides: .orchestra/config.toml (same shape, merged over global).

[settings]
timeout = {DEFAULT_RUN_TIMEOUT_SECONDS}           # hard cap for runaway workers (10 hours)
stall_timeout = {DEFAULT_STALL_TIMEOUT_SECONDS}   # kill after no worker output (30 minutes); 0 disables
supervisor_checkin_interval = 600  # seconds between safe progress check-ins for long runs
question_wait_timeout = 1800  # opted-in blocking question fallback (30 minutes)
default_requester = "orchestrator"
# quota_warn = true (default) — print a one-shot cached headroom advisory before
# each dispatch when the target coding plan is below the runway floor; never
# blocks dispatch, never reroutes, never consumes a Codex reset credit.
# Set to false in .orchestra/config.toml to opt out.
quota_warn = true
# Native worker delegation limits. Children use isolated git worktrees by
# default and never merge their branches automatically.
child_max_depth = 1
child_max_per_run = 3
child_max_active = 3
# Optional env vars to recover from `launchctl getenv` on macOS when a worker
# starts outside the user's interactive shell. Add only names, never values.
env_passthrough = []

# Non-secret environment values applied to every worker. ``{{root}}`` expands to
# the integration checkout, even when the worker runs in an isolated worktree.
[worker_env]


# --- roster ---------------------------------------------------------------
# Each entry is a reusable launch profile, not a singleton worker. Multiple
# independent runs may use the same profile concurrently, subject to provider
# headroom and project concurrency/ownership limits. `orchestra discover`
# distinguishes configured profiles from the backends/models available here.
# backend: opencode | codex | claude
# model:   backend-specific model id (opencode: provider/model, codex: model name)
# effort:  Codex or Claude reasoning effort; OpenCode uses `variant` instead
# sandbox: Codex execution sandbox (default: workspace-write). Set a broader
#          mode only in a trusted project override when a required capability
#          has been proven unavailable in the default lane. OpenCode and Claude
#          are not wrapped in Orchestra's Codex sandbox, so capability evidence
#          is recorded per backend/profile/sandbox rather than generalized.
# tier:    optional non-negative cost/capability level; tiered children may not
#          exceed a tiered parent's level (omit either tier to leave unconstrained)
# ensemble = true opts an opencode agent into the optional OpenCode Ensemble
# integration. See the README for the plugin and roster configuration.
# opencode_native_subagents = true deliberately restores OpenCode's native
# task/team tools; ordinary supervised profiles disable them to avoid
# unattended child-session permission deadlocks.
# extra_args: appended to the backend CLI invocation

[agents.minimax]
backend = "opencode"
model = "minimax-coding-plan/MiniMax-M3"
role = "workhorse — first choice for routine implementation and grunt work (the 'Sonnet' tier)"

[agents.glm]
backend = "opencode"
model = "zhipuai-coding-plan/glm-5.2"
role = "strong generalist — standard tier for normal feature work"

[agents.glm-max]
backend = "opencode"
model = "zhipuai-coding-plan/glm-5.2"
variant = "max"
role = "heavy reasoning tier — hard design/debugging (pairs with codex xhigh)"

[agents.kimi]
backend = "opencode"
model = "kimi-for-coding/k3"
role = "flagship Kimi generalist — complex coding, long context, and visual work"

# Kimi K3 currently has one explicit thinking variant: max. Do not create
# low/medium/high Kimi thinking profiles; `kimi` above has no explicit variant.
[agents.kimi-max]
backend = "opencode"
model = "kimi-for-coding/k3"
variant = "max"
role = "Kimi K3 max-thinking tier — hard design, debugging, and integration work"

[agents.codex]
backend = "codex"
# model omitted -> uses ~/.codex/config.toml default (gpt-5.6-sol)
# effort = "high"   # override reasoning effort for workers (codex config default: xhigh)
role = "really tough thinking only — heaviest tier, use sparingly"

[agents.codex-terra]
backend = "codex"
model = "gpt-5.6-terra"
effort = "high"
role = "fast Code Mode engineer for medium implementation and review tasks"
# Bounded trial: JavaScript/V8 orchestration can collapse independent tool
# calls into one model step. Terra advertises Code Mode support; keep the
# feature profile-scoped while Codex marks it under development. Suppress the
# generic unstable-feature banner for this intentional opt-in; model-specific
# compatibility warnings remain visible.
extra_args = [
  "--enable", "code_mode",
  "-c", "suppress_unstable_features_warning=true",
]

[agents.claude]
backend = "claude"
role = "worker claude for when another orchestrator is driving"
extra_args = ["--permission-mode", "acceptEdits", "--allowedTools", "Bash Edit Write Read Glob Grep WebFetch"]

"""


def apply_env_passthrough(cfg: dict, env: dict) -> dict:
    """Recover opted-in variables from the macOS user-session environment.

    Linux and WSL workers inherit the supplied environment directly. They do
    not have a ``launchctl`` equivalent, so missing values remain missing
    instead of spawning a macOS-only command and swallowing the failure.
    """
    if sys.platform != "darwin":
        return env
    import subprocess
    for name in cfg.get("settings", {}).get("env_passthrough", []):
        if not env.get(name):
            try:
                v = subprocess.run(["launchctl", "getenv", name], capture_output=True,
                                   text=True, timeout=5).stdout.strip()
                if v:
                    env[name] = v
            except Exception:
                pass
    return env


def apply_worker_env(cfg: dict, env: dict[str, str], root: Path) -> dict[str, str]:
    """Apply non-secret project values to every worker process."""
    values = cfg.get("worker_env", {})
    if not isinstance(values, dict):
        raise SystemExit("orchestra: [worker_env] must be a TOML table")
    updated = dict(env)
    for name, value in values.items():
        if not isinstance(name, str) or not name or "=" in name or "\0" in name:
            raise SystemExit(f"orchestra: invalid worker environment name {name!r}")
        if not isinstance(value, str) or "\0" in value:
            raise SystemExit(f"orchestra: worker environment value for {name} must be a string")
        updated[name] = value.replace("{root}", str(root))
    return updated


def codex_defaults() -> tuple[str | None, str | None]:
    """(model, reasoning_effort) from ~/.codex/config.toml, for display."""
    cfg = _load_toml(Path("~/.codex/config.toml").expanduser())
    return cfg.get("model"), cfg.get("model_reasoning_effort")


def _load_toml(p: Path) -> dict:
    if not p.is_file():
        return {}
    with open(p, "rb") as f:
        return tomllib.load(f)


def load(root: Path | None) -> dict:
    """Merged config: defaults <- global file <- project file."""
    cfg = tomllib.loads(DEFAULT_CONFIG)
    for p in [paths.global_config_path()] + ([paths.project_config_path(root)] if root else []):
        overlay = _load_toml(p)
        cfg.setdefault("settings", {}).update(overlay.get("settings", {}))
        worker_env = overlay.get("worker_env", {})
        if not isinstance(worker_env, dict):
            raise SystemExit(f"orchestra: [worker_env] in {p} must be a TOML table")
        cfg.setdefault("worker_env", {}).update(worker_env)
        for name, agent in overlay.get("agents", {}).items():
            cfg.setdefault("agents", {}).setdefault(name, {}).update(agent)
    return cfg


def agent_cfg(cfg: dict, name: str) -> dict:
    agents = cfg.get("agents", {})
    if name not in agents:
        raise SystemExit(
            f"orchestra: unknown agent '{name}'. Roster: {', '.join(sorted(agents))}\n"
            "Add it to ~/.config/orchestra/config.toml or .orchestra/config.toml"
        )
    a = dict(agents[name])
    a["name"] = name
    a.setdefault("backend", "opencode")
    a.setdefault("extra_args", [])
    a.setdefault("role", "worker agent")
    a.setdefault("ensemble", False)
    return a


def question_wait_seconds(raw) -> int:
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        raise SystemExit("orchestra: question wait timeout must be an integer number of seconds")
    if not MIN_QUESTION_WAIT_SECONDS <= seconds <= MAX_QUESTION_WAIT_SECONDS:
        raise SystemExit(
            f"orchestra: question wait timeout must be between "
            f"{MIN_QUESTION_WAIT_SECONDS} and {MAX_QUESTION_WAIT_SECONDS} seconds"
        )
    return seconds


def ensure_global_config() -> Path:
    p = paths.global_config_path()
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(DEFAULT_CONFIG)
    return p
