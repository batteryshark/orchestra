from __future__ import annotations

import unittest

from orchestra_cli.usage import ProviderResult, QuotaWindow, UsageService


def provider(provider_id: str, name: str, remaining: float) -> ProviderResult:
    return ProviderResult(
        id=provider_id,
        name=name,
        status="ok",
        windows=[
            QuotaWindow.from_remaining(
                id="weekly",
                label="Weekly",
                scope="Coding models",
                remaining_percent=remaining,
                resets_at="2026-07-25T00:00:00+00:00",
            )
        ],
        source="test fixture",
    )


class UsageServiceTests(unittest.TestCase):
    def test_recommends_provider_with_most_minimum_headroom(self) -> None:
        service = UsageService(
            collectors=(
                ("a", "A", lambda: provider("a", "A", 20)),
                ("b", "B", lambda: provider("b", "B", 80)),
            ),
            cache_ttl_seconds=60,
        )
        snapshot = service.snapshot(force=True)
        self.assertEqual(snapshot["recommendation"]["provider_id"], "b")
        self.assertEqual(snapshot["recommendation"]["headroom_percent"], 80.0)

    def test_caches_non_forced_snapshot(self) -> None:
        calls = 0

        def collector() -> ProviderResult:
            nonlocal calls
            calls += 1
            return provider("a", "A", 50)

        service = UsageService(
            collectors=(("a", "A", collector),), cache_ttl_seconds=60
        )
        first = service.snapshot()
        second = service.snapshot()
        self.assertIs(first, second)
        self.assertEqual(calls, 1)

    def test_adapter_exception_becomes_safe_unavailable_state(self) -> None:
        def broken() -> ProviderResult:
            raise RuntimeError("secret internal details")

        service = UsageService(collectors=(("a", "A", broken),))
        snapshot = service.snapshot(force=True)
        result = snapshot["providers"][0]
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("secret internal details", result["message"])


if __name__ == "__main__":
    unittest.main()
