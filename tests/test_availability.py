from __future__ import annotations

import json
import subprocess
import unittest
from unittest import mock

from orchestra_cli import availability


def _cfg(**agents) -> dict:
    return {"agents": agents}


class AvailabilityDiscoveryTests(unittest.TestCase):
    @staticmethod
    def which(name: str) -> str:
        return f"/tools/{name}"

    @staticmethod
    def healthy_run(command, **_kwargs):
        backend = command[0].rsplit("/", 1)[-1]
        if backend == "opencode":
            return subprocess.CompletedProcess(
                command, 0,
                "\x1b[32mkimi-for-coding/k3\x1b[0m\n"
                "minimax-coding-plan/MiniMax-M3\n",
                "",
            )
        if backend == "codex":
            if command[1:] == ["debug", "models"]:
                return subprocess.CompletedProcess(
                    command, 0,
                    json.dumps({"models": [
                        {"slug": "gpt-example"},
                        {"slug": "gpt-5.6-luna"},
                        {"slug": "gpt-5.3-codex-spark"},
                    ]}),
                    "",
                )
            return subprocess.CompletedProcess(command, 0, "Logged in", "")
        return subprocess.CompletedProcess(
            command, 0,
            json.dumps({"loggedIn": True, "email": "never-serialize@example.com"}),
            "",
        )

    def test_reports_backends_providers_models_and_profile_certainty(self) -> None:
        cfg = _cfg(
            kimi={"backend": "opencode", "model": "kimi-for-coding/k3"},
            stale={"backend": "opencode", "model": "missing-provider/model-x"},
            codex={"backend": "codex", "model": "gpt-example"},
            claude={"backend": "claude"},
        )

        report = availability.discover(
            cfg, which_fn=self.which, run_fn=self.healthy_run
        )

        backends = {item["backend"]: item for item in report["backends"]}
        self.assertEqual(backends["opencode"]["models"], [
            "kimi-for-coding/k3", "minimax-coding-plan/MiniMax-M3",
        ])
        self.assertEqual({item["id"] for item in report["providers"]}, {
            "kimi-for-coding", "minimax-coding-plan", "codex", "claude",
        })
        roster = {item["name"]: item for item in report["roster"]}
        self.assertEqual(roster["kimi"]["state"], "available")
        self.assertEqual(roster["stale"]["state"], "unavailable")
        self.assertEqual(roster["codex"]["state"], "available")
        self.assertEqual(roster["claude"]["state"], "available")
        self.assertNotIn("never-serialize", json.dumps(report))

    def test_missing_executable_is_proven_unavailable_without_subprocess(self) -> None:
        def must_not_run(*_args, **_kwargs):
            raise AssertionError("missing executables must not be launched")

        report = availability.discover(
            _cfg(worker={"backend": "opencode", "model": "provider/model"}),
            which_fn=lambda _name: None,
            run_fn=must_not_run,
        )

        self.assertTrue(all(item["state"] == "unavailable" for item in report["backends"]))
        self.assertEqual(report["roster"][0]["state"], "unavailable")

    def test_authentication_failures_are_unavailable(self) -> None:
        def run(command, **_kwargs):
            backend = command[0].rsplit("/", 1)[-1]
            if backend == "codex":
                return subprocess.CompletedProcess(command, 1, "Not logged in", "")
            if backend == "claude":
                return subprocess.CompletedProcess(
                    command, 0, json.dumps({"loggedIn": False}), ""
                )
            return subprocess.CompletedProcess(command, 0, "provider/model", "")

        report = availability.discover(
            _cfg(c={"backend": "codex"}, a={"backend": "claude"}),
            which_fn=self.which,
            run_fn=run,
        )

        roster = {item["name"]: item for item in report["roster"]}
        self.assertEqual(roster["c"]["state"], "unavailable")
        self.assertEqual(roster["a"]["state"], "unavailable")

    def test_probe_failure_is_unknown_and_does_not_false_block(self) -> None:
        def timeout(command, **_kwargs):
            raise subprocess.TimeoutExpired(command, 15)

        cfg = _cfg(worker={"backend": "opencode", "model": "provider/model"})
        report = availability.discover(
            cfg, only_backends={"opencode"}, which_fn=self.which, run_fn=timeout
        )
        self.assertEqual(report["roster"][0]["state"], "unknown")

        with mock.patch.object(availability, "discover", return_value=report):
            _report, issues, warnings = availability.check_profiles(
                cfg, [("worker", cfg["agents"]["worker"])]
            )
        self.assertEqual(issues, [])
        self.assertIn("model discovery timed out", warnings[0])

    def test_codex_catalog_failure_does_not_false_block_authenticated_backend(self) -> None:
        def run(command, **_kwargs):
            if command[1:] == ["login", "status"]:
                return subprocess.CompletedProcess(command, 0, "Logged in", "")
            return subprocess.CompletedProcess(command, 1, "", "catalog unavailable")

        cfg = _cfg(worker={"backend": "codex", "model": "gpt-5.6-luna"})
        report = availability.discover(
            cfg, only_backends={"codex"}, which_fn=self.which, run_fn=run
        )

        backend = report["backends"][0]
        self.assertEqual(backend["state"], "available")
        self.assertIsNone(backend["models"])
        self.assertEqual(report["roster"][0]["state"], "unknown")

    def test_codex_catalog_proves_missing_model_unavailable(self) -> None:
        cfg = _cfg(worker={"backend": "codex", "model": "gpt-does-not-exist"})
        report = availability.discover(
            cfg, only_backends={"codex"},
            which_fn=self.which, run_fn=self.healthy_run,
        )

        self.assertEqual(report["roster"][0]["state"], "unavailable")
        self.assertIn("not reported by Codex", report["roster"][0]["detail"])

    def test_unsupported_backend_is_blocking(self) -> None:
        cfg = _cfg(worker={"backend": "imaginary", "model": "x"})
        report, issues, warnings = availability.check_profiles(
            cfg, [("worker", cfg["agents"]["worker"])]
        )
        self.assertEqual(warnings, [])
        self.assertEqual(report["roster"][0]["state"], "unavailable")
        self.assertIn("unsupported backend", issues[0])

    def test_model_search_is_case_insensitive(self) -> None:
        report = availability.discover(
            _cfg(), only_backends={"opencode"},
            which_fn=self.which, run_fn=self.healthy_run,
        )
        self.assertEqual(
            availability.search_models(report, "MINIMAX"),
            ["minimax-coding-plan/MiniMax-M3"],
        )
        self.assertIn("1 model", availability.render(report))
        self.assertNotIn("1 models", availability.render(report))


if __name__ == "__main__":
    unittest.main()
