from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from orchestra_cli import capabilities, cli


LANE = {
    "host_identity": "mac-studio-01",
    "backend": "codex",
    "profile": "codex-gui",
    "sandbox_mode": "danger-full-access",
}
BASE_TIME = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)


class CapabilityRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".orchestra").mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_non_codex_profiles_ignore_irrelevant_sandbox_config(self) -> None:
        cfg = {
            "settings": {},
            "agents": {
                "glm": {
                    "backend": "opencode",
                    "model": "example/glm",
                    "sandbox": "danger-full-access",
                }
            },
        }
        self.assertEqual(
            cli._capability_lane(cfg, "glm"),
            ("opencode", "orchestra-unrestricted"),
        )

    def record(self, capability: str, state: capabilities.CapabilityState, **kwargs):
        options = {"observed_at": BASE_TIME, **kwargs}
        return capabilities.record_observation(
            self.root,
            **LANE,
            capability=capability,
            state=state,
            evidence="glcheck exited successfully",
            probe="make glcheck",
            **options,
        )

    def test_effective_observation_expires_and_check_reports_expired(self) -> None:
        self.record("cocoa-window", "supported", ttl=timedelta(minutes=5))

        fresh = capabilities.get_effective_observation(
            self.root, **LANE, capability="cocoa-window", at=BASE_TIME + timedelta(minutes=4)
        )
        self.assertIsNotNone(fresh)
        self.assertEqual(fresh.state, "supported")

        stale = capabilities.get_effective_observation(
            self.root, **LANE, capability="cocoa-window", at=BASE_TIME + timedelta(minutes=5)
        )
        self.assertIsNone(stale)
        result = capabilities.check_requirements(
            self.root,
            **LANE,
            capabilities=["cocoa-window"],
            at=BASE_TIME + timedelta(minutes=5),
        )
        self.assertFalse(result.satisfied)
        self.assertEqual(result.expired, frozenset({"cocoa-window"}))

    def test_absolute_expiry_is_an_alternative_to_ttl(self) -> None:
        self.record(
            "coreaudio-output",
            "supported",
            expires_at=BASE_TIME + timedelta(minutes=5),
        )
        self.assertIsNotNone(
            capabilities.get_effective_observation(
                self.root,
                **LANE,
                capability="coreaudio-output",
                at=BASE_TIME + timedelta(minutes=4),
            )
        )

    def test_observations_are_isolated_by_host_backend_profile_and_sandbox(self) -> None:
        self.record("legacy-opengl", "supported", ttl=None)

        for changed in (
            {"host_identity": "mac-mini-02"},
            {"backend": "claude"},
            {"profile": "claude-gui"},
            {"sandbox_mode": "workspace-write"},
        ):
            lane = {**LANE, **changed}
            self.assertIsNone(
                capabilities.get_effective_observation(
                    self.root, **lane, capability="legacy-opengl", at=BASE_TIME
                )
            )
            result = capabilities.check_requirements(
                self.root, **lane, capabilities=["legacy-opengl"], at=BASE_TIME
            )
            self.assertEqual(result.missing, frozenset({"legacy-opengl"}))

    def test_check_distinguishes_unsupported_unknown_and_missing(self) -> None:
        self.record("legacy-opengl", "unsupported", ttl=None)
        self.record("coreaudio-output", "unknown", ttl=None)

        result = capabilities.check_requirements(
            self.root,
            **LANE,
            capabilities=["legacy-opengl", "coreaudio-output", "cocoa-window"],
            at=BASE_TIME,
        )

        self.assertFalse(result.satisfied)
        self.assertEqual(result.unsupported, frozenset({"legacy-opengl"}))
        self.assertEqual(result.unknown, frozenset({"coreaudio-output"}))
        self.assertEqual(result.missing, frozenset({"cocoa-window"}))
        self.assertEqual(result.expired, frozenset())

    def test_list_filters_newest_first_and_can_omit_stale_observations(self) -> None:
        self.record(
            "legacy-opengl",
            "unsupported",
            observed_at=BASE_TIME - timedelta(minutes=1),
            ttl=timedelta(minutes=1),
        )
        self.record(
            "coreaudio-output",
            "unknown",
            observed_at=BASE_TIME,
            ttl=None,
        )
        self.record(
            "cocoa-window",
            "supported",
            observed_at=BASE_TIME + timedelta(minutes=1),
            ttl=None,
        )
        capabilities.record_observation(
            self.root,
            **{**LANE, "profile": "codex-sandbox"},
            capability="cocoa-window",
            state="unsupported",
            evidence="sandbox denied WindowServer",
            observed_at=BASE_TIME + timedelta(minutes=2),
            ttl=None,
        )

        all_observations = capabilities.list_observations(
            self.root, at=BASE_TIME
        )
        self.assertEqual(
            [(item.profile, item.capability) for item in all_observations],
            [
                ("codex-sandbox", "cocoa-window"),
                ("codex-gui", "cocoa-window"),
                ("codex-gui", "coreaudio-output"),
                ("codex-gui", "legacy-opengl"),
            ],
        )
        self.assertEqual(
            [item.capability for item in capabilities.list_observations(
                self.root, profile="codex-gui", include_expired=False, at=BASE_TIME
            )],
            ["cocoa-window", "coreaudio-output"],
        )
        self.assertEqual(
            [item.profile for item in capabilities.list_observations(
                self.root, capability="cocoa-window", at=BASE_TIME
            )],
            ["codex-sandbox", "codex-gui"],
        )

    def test_newer_observation_replaces_older_but_old_late_arrival_cannot_regress_it(self) -> None:
        self.record("cocoa-window", "unsupported", ttl=None)
        newer = capabilities.record_observation(
            self.root,
            **LANE,
            capability="cocoa-window",
            state="supported",
            evidence="Cocoa probe created a window",
            probe="make glcheck",
            observed_at=BASE_TIME + timedelta(minutes=1),
            ttl=None,
        )
        self.assertEqual(newer.state, "supported")

        retained = capabilities.record_observation(
            self.root,
            **LANE,
            capability="cocoa-window",
            state="unsupported",
            evidence="delayed old report",
            probe="make glcheck",
            observed_at=BASE_TIME - timedelta(minutes=1),
            ttl=None,
        )
        self.assertEqual(retained.state, "supported")
        self.assertEqual(retained.evidence, "Cocoa probe created a window")

    def test_invalid_values_fail_before_writing(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid capability state"):
            self.record("cocoa-window", "maybe")  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "invalid capability state"):
            self.record("cocoa-window", ["supported"])  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "host_identity"):
            capabilities.record_observation(
                self.root,
                **{**LANE, "host_identity": " "},
                capability="cocoa-window",
                state="supported",
                evidence="probe passed",
            )
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            self.record("cocoa-window", "supported", observed_at=datetime(2026, 8, 2, 12, 0))
        with self.assertRaisesRegex(ValueError, "positive timedelta"):
            self.record("cocoa-window", "supported", ttl=timedelta())
        with self.assertRaisesRegex(ValueError, "either ttl or expires_at"):
            self.record(
                "cocoa-window",
                "supported",
                ttl=timedelta(minutes=1),
                expires_at=BASE_TIME + timedelta(minutes=2),
            )
        with self.assertRaisesRegex(ValueError, "evidence"):
            capabilities.record_observation(
                self.root,
                **LANE,
                capability="cocoa-window",
                state="supported",
                evidence=" ",
            )

    def test_cli_records_and_checks_the_profile_launch_lane(self) -> None:
        config = {
            "settings": {},
            "agents": {"codex-gui": {"backend": "codex"}},
        }
        record = SimpleNamespace(
            profile="codex-gui",
            host="mac-studio-01",
            capability="cocoa-window",
            state="supported",
            evidence="glcheck passed",
            probe="make glcheck",
            permanent=False,
            ttl_seconds=60,
        )
        check = SimpleNamespace(
            profile="codex-gui",
            host="mac-studio-01",
            capabilities=["cocoa-window"],
        )
        with mock.patch.object(cli.paths, "find_root", return_value=self.root), \
                mock.patch.object(cli.config, "load", return_value=config), \
                redirect_stdout(StringIO()):
            cli.cmd_capability_record(record)
            cli.cmd_capability_check(check)

        observation = capabilities.list_observations(self.root)[0]
        self.assertEqual(observation.sandbox_mode, "workspace-write")
        self.assertEqual(observation.state, "supported")
        with self.assertRaisesRegex(ValueError, "profile"):
            capabilities.list_observations(self.root, profile=" ")
        with self.assertRaisesRegex(ValueError, "include_expired"):
            capabilities.list_observations(self.root, include_expired=1)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
