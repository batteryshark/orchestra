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


# --- roster ---------------------------------------------------------------
# backend: opencode | codex | claude
# model:   backend-specific model id (opencode: provider/model, codex: model name)
# ensemble = true opts an opencode agent into the optional OpenCode Ensemble
# integration. See the README for the plugin and roster configuration.
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

[agents.codex-55]
backend = "codex"
model = "gpt-5.5"
effort = "high"
role = "fast engineer for medium tasks"

[agents.claude]
backend = "claude"
role = "worker claude for when another orchestrator is driving"
extra_args = ["--permission-mode", "acceptEdits", "--allowedTools", "Bash Edit Write Read Glob Grep WebFetch"]

"""


def apply_env_passthrough(cfg: dict, env: dict) -> dict:
    """Fill missing env vars from launchctl (macOS user-session env), so workers
    spawned from scrubbed environments still see keys the user set globally."""
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
