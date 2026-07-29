from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from orchestra_cli import (
    operator_contract,
    operator_roster,
    operator_runtime,
    operator_store,
)


PROJECT_ID = "d" * 16


def _config() -> dict:
    return {
        "agents": {
            "fable": {
                "backend": "claude",
                "model": "claude-fable-5",
                "role": (
                    "Mythos-class hardest reasoning and integration; "
                    "do not route security work here"
                ),
            },
            "codex": {
                "backend": "codex",
                "model": "gpt-5.6-sol",
                "role": "really tough thinking only — heaviest tier",
            },
            "codex-55": {
                "backend": "codex",
                "model": "gpt-5.5",
                "role": "fast engineer for medium tasks",
            },
            "codex-spark": {
                "backend": "codex",
                "model": "gpt-5.3-codex-spark",
                "role": "cheap basic sweep work; flaky, not critical",
            },
            "kimi": {
                "backend": "opencode",
                "model": "kimi-for-coding/k3",
                "role": "frontier-tier generalist for real feature work",
            },
            "minimax": {
                "backend": "opencode",
                "model": "minimax-coding-plan/MiniMax-M3",
                "role": "workhorse for routine mechanical tasks",
            },
            "broken": {
                "backend": "disabled-broken",
                "model": "DO-NOT-USE",
                "role": "broken do-not-use",
            },
        }
    }


class OperatorRosterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db_path = self.tmp / "state" / "operator.db"
        self.project_root = self.tmp / "project"
        (self.project_root / ".orchestra").mkdir(parents=True)
        self.project = {
            "id": PROJECT_ID,
            "name": "roster-project",
            "root": str(self.project_root),
            "available": True,
        }
        self.policy = operator_roster.bootstrap_policy(_config())
        self.addCleanup(self._tmp.cleanup)

    def start_operation(self):
        contract_data = operator_contract.template(
            name="Roster operation",
            goal="Implement the bounded feature",
            project_ids=[PROJECT_ID],
            gates=["Tests pass"],
        )
        validated = operator_contract.validate_contract(contract_data)
        draft = operator_store.save_draft(
            validated,
            [self.project],
            path=self.db_path,
        )
        operator_store.approve(
            draft.operator_id,
            version=1,
            sha256=draft.sha256,
            approved_by="owner",
            path=self.db_path,
        )
        operation = operator_runtime.start_operation(
            draft.operator_id,
            mode="shadow",
            priority=50,
            registered_projects=[self.project],
            path=self.db_path,
        )
        work = operator_runtime.work_items(operation["id"], path=self.db_path)[0]
        return contract_data, operation, work

    def save_and_approve_policy(self):
        version, created = operator_roster.save_policy(
            self.policy,
            source="test bootstrap",
            path=self.db_path,
        )
        self.assertTrue(created)
        operator_roster.approve_policy(
            version=version,
            sha256=self.policy.sha256,
            approved_by="owner",
            path=self.db_path,
        )
        return version

    def test_bootstrap_separates_tiers_modes_disabled_profiles_and_pools(self) -> None:
        profiles = {row["name"]: row for row in self.policy.data["profiles"]}
        self.assertEqual(profiles["fable"]["tier"], "heavy")
        self.assertEqual(profiles["codex"]["tier"], "heavy")
        self.assertNotIn(
            "general_implementation",
            profiles["codex"]["actuation_modes"],
        )
        self.assertEqual(profiles["codex-55"]["tier"], "generalist")
        self.assertEqual(profiles["minimax"]["tier"], "workhorse")
        self.assertFalse(profiles["broken"]["enabled"])
        self.assertIn("security", profiles["fable"]["contraindications"])
        self.assertNotEqual(
            profiles["codex-spark"]["pools"],
            profiles["codex"]["pools"],
        )

    def test_policy_version_and_approval_are_hash_bound_and_immutable(self) -> None:
        version = self.save_and_approve_policy()
        loaded_version, loaded = operator_roster.latest_policy(path=self.db_path)
        self.assertEqual(loaded_version, version)
        self.assertEqual(loaded.canonical_json, self.policy.canonical_json)
        with self.assertRaises(operator_roster.RosterError):
            operator_roster.approve_policy(
                version=version,
                sha256="0" * 64,
                approved_by="owner",
                path=self.db_path,
            )
        con = sqlite3.connect(self.db_path)
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                con.execute(
                    "UPDATE roster_policy_versions SET source='changed' WHERE version=?",
                    (version,),
                )
        finally:
            con.close()

    def test_latest_policy_requires_owner_approval(self) -> None:
        operator_roster.save_policy(
            self.policy,
            source="bootstrap",
            path=self.db_path,
        )
        with self.assertRaisesRegex(operator_roster.RosterError, "approved"):
            operator_roster.latest_policy(path=self.db_path)
        version, loaded = operator_roster.latest_policy(
            require_approved=False,
            path=self.db_path,
        )
        self.assertEqual(version, 1)
        self.assertEqual(loaded.sha256, self.policy.sha256)

    def test_router_applies_quality_and_actuation_filters_before_headroom(self) -> None:
        contract, operation, work = self.start_operation()
        availability_report = {
            "roster": [
                {"name": name, "state": "available"}
                for name in ("fable", "codex", "codex-55", "kimi", "minimax")
            ]
        }
        capacity = {
            pool["id"]: {
                "headroom_percent": 99 if pool["provider_id"] == "minimax" else 50,
                "certainty": "observed",
            }
            for pool in self.policy.data["pools"]
        }
        route = operator_roster.route(
            operation,
            work,
            contract,
            self.policy,
            availability_report=availability_report,
            capacity=capacity,
            path=self.db_path,
        )
        self.assertEqual(route.profile, "kimi")
        considered = {row["profile"]: row for row in route.considered}
        self.assertFalse(considered["minimax"]["eligible"])
        self.assertIn("below generalist", " ".join(considered["minimax"]["reasons"]))
        self.assertFalse(considered["codex"]["eligible"])
        self.assertIn(
            "not qualified for general_implementation",
            considered["codex"]["reasons"],
        )

    def test_live_router_rejects_backend_without_filesystem_sandbox(self) -> None:
        contract, operation, work = self.start_operation()
        route = operator_roster.route(
            {**operation, "mode": "live"},
            work,
            contract,
            self.policy,
            availability_report={
                "roster": [{"name": "kimi", "state": "available"}]
            },
            capacity={},
            path=self.db_path,
        )
        kimi = next(
            row for row in route.considered if row["profile"] == "kimi"
        )
        self.assertFalse(kimi["eligible"])
        self.assertIn("filesystem sandbox", " ".join(kimi["reasons"]))

    def test_router_never_uses_security_contraindicated_fable(self) -> None:
        contract, operation, work = self.start_operation()
        work = {
            **work,
            "title": "Security-sensitive key validation",
            "description": "Review cryptographic authentication behavior",
            "task_class": "architecture",
            "minimum_tier": "heavy",
            "actuation_mode": "diagnose_only",
        }
        report = {
            "roster": [
                {"name": "fable", "state": "available"},
                {"name": "codex", "state": "available"},
            ]
        }
        route = operator_roster.route(
            operation,
            work,
            contract,
            self.policy,
            availability_report=report,
            capacity={},
            path=self.db_path,
        )
        self.assertEqual(route.profile, "codex")
        fable = next(row for row in route.considered if row["profile"] == "fable")
        self.assertIn("explicit contraindication", " ".join(fable["reasons"]))

    def test_capacity_reservations_are_atomic_and_preserve_heavy_reserve(self) -> None:
        _, operation, work = self.start_operation()
        version, _ = operator_roster.save_policy(
            self.policy,
            source="bootstrap",
            path=self.db_path,
        )
        # Kimi's pool gets a 25% protected reserve for this test.
        kimi = next(row for row in self.policy.data["profiles"] if row["name"] == "kimi")
        pool_id = kimi["pools"][0]
        con = operator_roster.connect(self.db_path)
        try:
            con.execute(
                "UPDATE capacity_pools SET reserve_percent=25 "
                "WHERE policy_version=? AND id=?",
                (version, pool_id),
            )
            con.execute(
                "INSERT INTO capacity_observations("
                "pool_id, provider_id, status, headroom_percent, windows_json, "
                "certainty, observed_at"
                ") VALUES(?,?,?,?,?,?,?)",
                (
                    pool_id,
                    "kimi",
                    "ok",
                    30,
                    "[]",
                    "observed",
                    operator_store.now(),
                ),
            )
            con.commit()
        finally:
            con.close()
        adjusted = copy.deepcopy(self.policy.data)
        next(pool for pool in adjusted["pools"] if pool["id"] == pool_id)[
            "reserve_percent"
        ] = 25
        policy = operator_roster.validate_policy(adjusted)
        first = operator_roster.reserve(
            operation["id"],
            work["id"],
            profile_name="kimi",
            policy=policy,
            minimum_tier="generalist",
            burn_band="normal",
            now_epoch=100,
            path=self.db_path,
        )
        self.assertEqual(first.pools, (pool_id,))
        with self.assertRaisesRegex(operator_roster.RosterError, "reserve protects"):
            operator_roster.reserve(
                operation["id"],
                work["id"],
                profile_name="kimi",
                policy=policy,
                minimum_tier="generalist",
                burn_band="normal",
                now_epoch=101,
                path=self.db_path,
            )
        heavy = operator_roster.reserve(
            operation["id"],
            work["id"],
            profile_name="kimi",
            policy=policy,
            minimum_tier="heavy",
            burn_band="normal",
            now_epoch=101,
            path=self.db_path,
        )
        operator_roster.bind_reservation(
            heavy.group,
            project_run_id=44,
            path=self.db_path,
        )
        operator_roster.release_reservation(
            heavy.group,
            state="consumed",
            path=self.db_path,
        )

    def test_unknown_capacity_allows_only_one_conservative_reservation(self) -> None:
        _, operation, work = self.start_operation()
        first = operator_roster.reserve(
            operation["id"],
            work["id"],
            profile_name="kimi",
            policy=self.policy,
            minimum_tier="generalist",
            now_epoch=100,
            path=self.db_path,
        )
        self.assertTrue(first.group.startswith("res_"))
        with self.assertRaisesRegex(operator_roster.RosterError, "uncertain"):
            operator_roster.reserve(
                operation["id"],
                work["id"],
                profile_name="kimi",
                policy=self.policy,
                minimum_tier="generalist",
                now_epoch=101,
                path=self.db_path,
            )

    def test_health_quarantines_infrastructure_not_difficult_task_failure(self) -> None:
        state = operator_roster.record_health_signal(
            "kimi",
            signal="task_failure",
            context={"task": "hard reverse engineering"},
            now_epoch=100,
            path=self.db_path,
        )
        self.assertEqual(state, "unknown")
        for index in range(3):
            state = operator_roster.record_health_signal(
                "kimi",
                signal="zero_output_stall",
                context={"attempt": index},
                now_epoch=101 + index,
                path=self.db_path,
            )
        self.assertEqual(state, "quarantined")
        health = operator_roster.health_states(path=self.db_path)["kimi"]
        self.assertEqual(health["consecutive_infra_failures"], 3)
        restored = operator_roster.record_health_signal(
            "kimi",
            signal="probe_success",
            context={},
            now_epoch=2000,
            path=self.db_path,
        )
        self.assertEqual(restored, "available")

    def test_capacity_snapshot_preserves_native_windows_and_certainty(self) -> None:
        snapshot = {
            "providers": [{
                "id": "kimi",
                "status": "ok",
                "headroom_percent": 42.0,
                "windows": [{
                    "id": "weekly",
                    "remaining_percent": 42.0,
                    "resets_at": "2026-08-01T00:00:00Z",
                }],
                "account_balance": None,
            }]
        }
        recorded = operator_roster.record_capacity_snapshot(
            self.policy,
            snapshot,
            path=self.db_path,
        )
        kimi_pool = next(
            pool["id"]
            for pool in self.policy.data["pools"]
            if pool["provider_id"] == "kimi"
        )
        self.assertEqual(recorded[kimi_pool]["headroom_percent"], 42.0)
        self.assertEqual(recorded[kimi_pool]["certainty"], "observed")

    def test_recovery_council_requires_diverse_blind_quorum(self) -> None:
        contract, operation, work = self.start_operation()
        council = operator_roster.create_council(
            operation["id"],
            work["id"],
            failure_fingerprint="same-failure",
            evidence={"failure": "three attempts disagree"},
            contract=contract,
            policy=self.policy,
            path=self.db_path,
        )
        members = {row["profile_name"]: row for row in council["members"]}
        self.assertEqual(set(members), {"fable", "codex"})
        self.assertEqual(
            {row["model_family"] for row in members.values()},
            {"anthropic", "openai"},
        )
        first = operator_roster.submit_council_opinion(
            council["id"],
            "fable",
            _opinion("add_discriminator"),
            path=self.db_path,
        )
        self.assertEqual(first["state"], "collecting")
        final = operator_roster.submit_council_opinion(
            council["id"],
            "codex",
            _opinion("add_discriminator"),
            path=self.db_path,
        )
        self.assertEqual(final["state"], "quorum")
        self.assertEqual(final["synthesis"]["action_key"], "add_discriminator")
        self.assertEqual(final["synthesis"]["votes"], 2)

    def test_recovery_council_split_is_not_misrepresented_as_quorum(self) -> None:
        contract, operation, work = self.start_operation()
        council = operator_roster.create_council(
            operation["id"],
            work["id"],
            failure_fingerprint="split-failure",
            evidence={"failure": "ambiguous"},
            contract=contract,
            policy=self.policy,
            path=self.db_path,
        )
        operator_roster.submit_council_opinion(
            council["id"],
            "fable",
            _opinion("test_parser"),
            path=self.db_path,
        )
        final = operator_roster.submit_council_opinion(
            council["id"],
            "codex",
            _opinion("inspect_binary"),
            path=self.db_path,
        )
        self.assertEqual(final["state"], "split")
        self.assertEqual(final["synthesis"]["status"], "split")


def _opinion(action_key: str) -> dict:
    return {
        "diagnosis": "The current evidence does not isolate the boundary.",
        "evidence": ["failure log", "current diff"],
        "hypotheses": ["parser offset", "fixture mismatch"],
        "action_key": action_key,
        "next_action": {
            "kind": "diagnostic",
            "description": "Add the smallest discriminating test",
        },
        "smallest_surface": ["one focused test", "one implementation function"],
        "deferred": ["general parser rewrite"],
        "confidence": 0.8,
        "risks": ["fixture may be incomplete"],
    }


if __name__ == "__main__":
    unittest.main()
