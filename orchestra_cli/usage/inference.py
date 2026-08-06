"""Map Orchestra agents to a provider ID for the dispatch-time quota check.

Inference uses the actual coding-plan prefixes recorded by OpenCode:
``minimax-coding-plan`` / ``minimax-cn-coding-plan`` map to ``minimax``;
``kimi-for-coding`` maps to ``kimi``; ``zhipuai-coding-plan`` /
``zai-coding-plan`` map to ``zai``; ``deepseek`` maps to ``deepseek``;
``togetherai`` maps to ``together``. Backend matches
for Claude (``claude``) and Codex (``codex``). Anything else returns ``None``
(unknown), which the dispatcher treats as fail-open.
"""
from __future__ import annotations

from typing import Mapping


def infer_provider(backend: str | None, model: str | None) -> str | None:
    """Return the provider id used by the usage collectors, or ``None`` when
    the backend/model pair is not one of the supported quota or prepaid providers.

    Only explicit provider prefixes are matched. Bare ``glm-*`` or ``claude-*``
    model ids from unrelated providers are intentionally not inferred.
    """
    backend = (backend or "").lower()
    model = (model or "").lower()

    if backend == "codex":
        return "codex"
    if backend == "claude":
        return "claude"
    if backend != "opencode":
        return None
    if model.startswith(("minimax-coding-plan/", "minimax-cn-coding-plan/")):
        return "minimax"
    if model.startswith("kimi-for-coding/"):
        return "kimi"
    if model.startswith(("zhipuai-coding-plan/", "zai-coding-plan/")):
        return "zai"
    if model.startswith("deepseek/"):
        return "deepseek"
    if model.startswith("togetherai/"):
        return "together"
    return None


def infer_from_agent(agent: Mapping[str, object]) -> str | None:
    """Infer the provider for a roster entry. Accepts the dict returned by
    ``orchestra_cli.config.agent_cfg``."""
    backend = agent.get("backend") if isinstance(agent, Mapping) else None
    model = agent.get("model") if isinstance(agent, Mapping) else None
    return infer_provider(backend if isinstance(backend, str) else None,
                          model if isinstance(model, str) else None)
