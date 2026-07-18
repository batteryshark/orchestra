import tomllib
from pathlib import Path

from orchestra_cli import paths

DEFAULT_CONFIG = """\
# Orchestra roster + settings. Global file: ~/.config/orchestra/config.toml
# Project overrides: .orchestra/config.toml (same shape, merged over global).

[settings]
timeout = 3600            # per-run seconds before the supervisor kills a worker
default_requester = "orchestrator"

# --- roster ---------------------------------------------------------------
# backend: opencode | codex | claude
# model:   backend-specific model id (opencode: provider/model, codex: model name)
# ensemble = true makes an opencode agent the LEAD of an ensemble team
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

[agents.ensemble]
backend = "opencode"
model = "zhipuai-coding-plan/glm-5.2"
ensemble = true
role = "lead of an opencode-ensemble team"
model_pool = ["zhipuai-coding-plan/glm-5.2", "minimax-coding-plan/MiniMax-M3"]
"""


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


def ensure_global_config() -> Path:
    p = paths.global_config_path()
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(DEFAULT_CONFIG)
    return p
