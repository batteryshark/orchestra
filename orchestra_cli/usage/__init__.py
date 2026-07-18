"""Native provider-quota subsystem for Orchestra.

This package ported from the standalone ``usage-widget`` prototype keeps
credential discovery server-side, normalizes MiniMax / Claude / Z.AI / Codex
quotas into shared dataclasses, and exposes them through ``UsageService``,
which is cached per-process so callers within the same Orchestra project do
not trigger extra API calls.
"""

from orchestra_cli.usage.inference import infer_from_agent, infer_provider
from orchestra_cli.usage.models import (
    ProviderResult,
    QuotaWindow,
    RateLimitResetCredits,
    clamp_percent,
    error_result,
    utc_now_iso,
)
from orchestra_cli.usage.service import (
    DEFAULT_COLLECTORS,
    WARN_HEADROOM_PERCENT,
    UsageService,
    default_service,
)
from orchestra_cli.usage.warning import (
    QuotaWarning,
    assess_targets,
    render_warning_lines,
)


__all__ = [
    "DEFAULT_COLLECTORS",
    "ProviderResult",
    "QuotaWarning",
    "QuotaWindow",
    "RateLimitResetCredits",
    "UsageService",
    "WARN_HEADROOM_PERCENT",
    "assess_targets",
    "clamp_percent",
    "default_service",
    "error_result",
    "infer_from_agent",
    "infer_provider",
    "render_warning_lines",
    "utc_now_iso",
]
