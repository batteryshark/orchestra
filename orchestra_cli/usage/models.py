"""Normalized provider quota models shared by the CLI, web UI, and dispatch warnings.

These dataclasses are the wire shape the browser sees. Headroom, windows, and
rate-limit reset credits are kept separate from raw provider IDs so the API never
accidentally leaks an account, OAuth, or credential identifier.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def clamp_percent(value: float | int) -> float:
    return round(max(0.0, min(100.0, float(value))), 1)


@dataclass(slots=True)
class QuotaWindow:
    id: str
    label: str
    scope: str
    used_percent: float
    remaining_percent: float
    resets_at: str | None = None
    burn_rate_percent_per_hour: float | None = None

    @classmethod
    def from_used(
        cls,
        *,
        id: str,
        label: str,
        scope: str,
        used_percent: float | int,
        resets_at: str | None = None,
    ) -> "QuotaWindow":
        used = clamp_percent(used_percent)
        return cls(
            id=id,
            label=label,
            scope=scope,
            used_percent=used,
            remaining_percent=clamp_percent(100 - used),
            resets_at=resets_at,
        )

    @classmethod
    def from_remaining(
        cls,
        *,
        id: str,
        label: str,
        scope: str,
        remaining_percent: float | int,
        resets_at: str | None = None,
    ) -> "QuotaWindow":
        remaining = clamp_percent(remaining_percent)
        return cls(
            id=id,
            label=label,
            scope=scope,
            used_percent=clamp_percent(100 - remaining),
            remaining_percent=remaining,
            resets_at=resets_at,
        )


@dataclass(slots=True)
class RateLimitResetCredits:
    """Codex-side only: count of remaining reset credits + earliest known expiry
    and a human title. Credit IDs and internal descriptions are deliberately
    not stored — they are account identifiers and must not leak through the API.
    """

    available_count: int
    title: str | None = None
    expires_at: str | None = None


@dataclass(slots=True)
class ProviderResult:
    id: str
    name: str
    status: str
    plan: str | None = None
    windows: list[QuotaWindow] = field(default_factory=list)
    message: str | None = None
    source: str | None = None
    rate_limit_resets: RateLimitResetCredits | None = None
    fetched_at: str = field(default_factory=utc_now_iso)

    @property
    def headroom_percent(self) -> float | None:
        coding_windows = [w for w in self.windows if w.scope != "MCP tools"]
        windows = coding_windows or self.windows
        if not windows:
            return None
        return min(w.remaining_percent for w in windows)

    @property
    def is_stale(self) -> bool:
        return self.status == "stale"

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["headroom_percent"] = self.headroom_percent
        return result


def error_result(
    provider_id: str,
    name: str,
    status: str,
    message: str,
    *,
    source: str | None = None,
) -> ProviderResult:
    return ProviderResult(
        id=provider_id,
        name=name,
        status=status,
        message=message,
        source=source,
    )
