"""Server-side cache that fans out quota collectors and shapes the JSON the UI
and `orchestra usage` both consume. Keeps one snapshot per process so the
browser, CLI, and dispatch-time warning never trigger extra API calls.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from orchestra_cli.usage.models import ProviderResult, error_result, utc_now_iso
from orchestra_cli.usage.providers import (
    collect_claude,
    collect_codex,
    collect_minimax,
    collect_zai,
)


Collector = Callable[[], ProviderResult]


@dataclass(slots=True)
class HistorySample:
    observed_at: float
    remaining_percent: float
    resets_at: str | None


DEFAULT_COLLECTORS: tuple[tuple[str, str, Collector], ...] = (
    ("minimax", "MiniMax", collect_minimax),
    ("claude", "Claude", collect_claude),
    ("zai", "Z.AI", collect_zai),
    ("codex", "Codex", collect_codex),
)


# Headroom floor for the dispatch-time warn-only assessment. Values strictly
# below trigger a warning; equal-or-above is normal. The threshold sits below
# the "switch soon" band so a true critical situation lights up without
# flapping near the watch threshold.
WARN_HEADROOM_PERCENT = 20.0


class UsageService:
    def __init__(
        self,
        *,
        collectors: tuple[tuple[str, str, Collector], ...] = DEFAULT_COLLECTORS,
        cache_ttl_seconds: float = 75,
        min_trend_interval_seconds: float = 300,
    ) -> None:
        self._collectors = collectors
        self._cache_ttl_seconds = cache_ttl_seconds
        self._min_trend_interval_seconds = min_trend_interval_seconds
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._cached_at = 0.0
        self._cached: dict[str, Any] | None = None
        self._history: dict[tuple[str, str], list[HistorySample]] = {}

    def snapshot(self, *, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            if (
                not force
                and self._cached is not None
                and now - self._cached_at < self._cache_ttl_seconds
            ):
                return self._cached

        with self._refresh_lock:
            with self._lock:
                if (
                    not force
                    and self._cached is not None
                    and time.monotonic() - self._cached_at < self._cache_ttl_seconds
                ):
                    return self._cached
            fresh = self._collect()
            with self._lock:
                self._cached = fresh
                self._cached_at = time.monotonic()
            return fresh

    def _collect(self) -> dict[str, Any]:
        results: dict[str, ProviderResult] = {}
        max_workers = max(1, min(4, len(self._collectors)))
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="quota") as pool:
            futures = {
                pool.submit(collector): (provider_id, name)
                for provider_id, name, collector in self._collectors
            }
            for future in as_completed(futures):
                provider_id, name = futures[future]
                try:
                    results[provider_id] = future.result()
                except Exception:
                    results[provider_id] = error_result(
                        provider_id,
                        name,
                        "unavailable",
                        "The provider adapter failed unexpectedly.",
                    )

        ordered = [results[provider_id] for provider_id, _, _ in self._collectors]
        self._update_trends(ordered)
        ready = [result for result in ordered if result.headroom_percent is not None]
        recommendation = max(ready, key=lambda result: result.headroom_percent or 0) if ready else None
        return {
            "generated_at": utc_now_iso(),
            "status": "ok" if all(result.status == "ok" for result in ordered) else "partial",
            "providers": [result.to_dict() for result in ordered],
            "recommendation": (
                {
                    "provider_id": recommendation.id,
                    "provider_name": recommendation.name,
                    "headroom_percent": recommendation.headroom_percent,
                    "reason": "Most remaining capacity across its coding quota windows.",
                }
                if recommendation
                else None
            ),
            "trend": {
                "sampling": "in_memory",
                "minimum_interval_seconds": self._min_trend_interval_seconds,
            },
        }

    def _update_trends(self, providers: list[ProviderResult]) -> None:
        observed_at = time.time()
        keep_after = observed_at - 48 * 60 * 60
        with self._lock:
            for provider in providers:
                if provider.status != "ok":
                    continue
                for window in provider.windows:
                    key = (provider.id, window.id)
                    samples = [
                        sample
                        for sample in self._history.get(key, [])
                        if sample.observed_at >= keep_after
                    ]
                    if samples and samples[-1].resets_at != window.resets_at:
                        samples = []
                    if samples:
                        baseline = samples[0]
                        elapsed = observed_at - baseline.observed_at
                        if elapsed >= self._min_trend_interval_seconds:
                            consumed = baseline.remaining_percent - window.remaining_percent
                            if consumed >= 0:
                                window.burn_rate_percent_per_hour = round(
                                    consumed / (elapsed / 3600), 1
                                )
                    samples.append(
                        HistorySample(
                            observed_at=observed_at,
                            remaining_percent=window.remaining_percent,
                            resets_at=window.resets_at,
                        )
                    )
                    self._history[key] = samples[-240:]


def default_service() -> UsageService:
    """Per-process singleton (NOT cross-project — each Orchestra project has
    its own UI process). Quota collectors are heavy enough that we want one
    cache per process. The CLI's `usage` subcommand, the UI's `/api/usage`,
    and the dispatch-time warning all use this within the same process.
    """
    global _SERVICE_SINGLETON
    if _SERVICE_SINGLETON is None:
        _SERVICE_SINGLETON = UsageService()
    return _SERVICE_SINGLETON


_SERVICE_SINGLETON: UsageService | None = None
