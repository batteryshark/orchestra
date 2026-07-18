"""Warn-only quota assessment before dispatch.

The orchestrator already reasons about which coding plan each worker targets
(`orchestra_cli.usage.inference`). This module consumes a single
`UsageService.snapshot()` and emits per-target advisories.

It is bounded and fail-open: it never reroutes, it never consumes a Codex
reset credit, and any collector failure becomes a status other than `ok`
which `assess_targets` ignores — so dispatch cannot break. When the
per-process cache is cold (first dispatch after the UI server starts, for
example), `snapshot()` does trigger one bounded fan-out to the collectors
(time-limited HTTP and subprocess calls); that is the only path on which
this hook may briefly collect, after which subsequent dispatches read the
warm cache.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from orchestra_cli.usage.service import WARN_HEADROOM_PERCENT


@dataclass(slots=True, frozen=True)
class QuotaWarning:
    provider_id: str
    provider_name: str
    agent: str
    headroom_percent: float | None
    status: str
    reason: str


def _quota_lookup(snapshot: dict, provider_id: str) -> dict | None:
    for row in snapshot.get("providers") or []:
        if isinstance(row, dict) and row.get("id") == provider_id:
            return row
    return None


def assess_targets(
    snapshot: dict,
    targets: Iterable[tuple[str, str | None]],
    *,
    warn_at_or_below_percent: float = WARN_HEADROOM_PERCENT,
) -> list[QuotaWarning]:
    """Return one warning per target whose inferred provider has headroom
    at-or-below ``warn_at_or_below_percent``. Stale, unavailable, and unknown
    providers do NOT produce a warning (fail-open). The list is empty when
    everything is healthy or when the snapshot is missing.
    """
    warnings: list[QuotaWarning] = []
    for agent, provider_id in targets:
        if not provider_id:
            continue
        provider = _quota_lookup(snapshot, provider_id)
        if not provider:
            continue
        status = provider.get("status")
        if status != "ok":
            continue
        headroom = provider.get("headroom_percent")
        if not isinstance(headroom, (int, float)):
            continue
        if headroom > warn_at_or_below_percent:
            continue
        warnings.append(
            QuotaWarning(
                provider_id=provider_id,
                provider_name=provider.get("name") or provider_id,
                agent=agent,
                headroom_percent=float(headroom),
                status=str(status),
                reason=(
                    f"{provider.get('name') or provider_id} coding headroom is "
                    f"{headroom:.0f}% (at-or-below the {warn_at_or_below_percent:.0f}% floor)"
                ),
            )
        )
    return warnings


def render_warning_lines(warnings: Iterable[QuotaWarning]) -> list[str]:
    lines = []
    for w in warnings:
        lines.append(
            f"quota warning: dispatch to '{w.agent}' targets {w.provider_name} "
            f"({w.headroom_percent:.0f}% headroom) — {w.reason}"
        )
    return lines
