from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from orchestra_cli import operator_contract, operator_runtime, operator_store


PROJECT_ID = "b" * 16


class OperatorRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db_path = self.tmp / "state" / "operator.db"
        self.project_root = self.tmp / "project"
        (self.project_root / ".orchestra").mkdir(parents=True)
        self.project = {
            "id": PROJECT_ID,
            "name": "runtime-project",
            "root": str(self.project_root),
            "available": True,
        }
        self.addCleanup(self._tmp.cleanup)

    def approve_contract(
        self,
        *,
        name: str = "Runtime operator",
        live_verification: bool = False,
        authority: dict[str, str] | None = None,
    ) -> operator_store.DraftResult:
        data = operator_contract.template(
            name=name,
            goal="Implement and verify the bounded change",
            project_ids=[PROJECT_ID],
            gates=["Full tests pass"],
        )
        if live_verification:
            data["quality"]["verification"] = [{
                "name": "full tests",
                "project_id": PROJECT_ID,
                "argv": ["python3", "-m", "unittest"],
                "timeout_seconds": 600,
                "required": True,
                "phase": "both",
            }]
        if authority:
            data["authority"].update(authority)
        validated = operator_contract.validate_contract(data)
        draft = operator_store.save_draft(
            validated,
            [self.project],
            path=self.db_path,
        )
        operator_store.approve(
            draft.operator_id,
            version=draft.version,
            sha256=draft.sha256,
            approved_by="owner",
            path=self.db_path,
        )
        return draft

    def start_shadow(self, **kwargs):
        draft = self.approve_contract(**kwargs)
        operation = operator_runtime.start_operation(
            draft.operator_id,
            mode="shadow",
            priority=50,
            registered_projects=[self.project],
            path=self.db_path,
        )
        return draft, operation

    def test_shadow_start_materializes_goals_projects_and_ready_work(self) -> None:
        draft, operation = self.start_shadow()
        self.assertEqual(operation["operator_id"], draft.operator_id)
        self.assertEqual(operation["state"], "active")
        self.assertEqual(operation["mode"], "shadow")
        self.assertEqual(operation["goals"][0]["state"], "active")
        self.assertEqual(len(operation["projects"]), 1)
        work = operator_runtime.work_items(operation["id"], path=self.db_path)
        self.assertEqual(len(work), 1)
        self.assertEqual(work[0]["state"], "ready")
        self.assertEqual(work[0]["minimum_tier"], "generalist")
        self.assertEqual(work[0]["contract_project_id"], PROJECT_ID)

    def test_only_one_nonterminal_operation_per_operator(self) -> None:
        draft, operation = self.start_shadow()
        with self.assertRaisesRegex(
            operator_runtime.RuntimeError,
            "already has live operation",
        ):
            operator_runtime.start_operation(
                draft.operator_id,
                mode="shadow",
                priority=50,
                registered_projects=[self.project],
                path=self.db_path,
            )
        operator_runtime.set_operation_state(
            operation["id"],
            "stopped",
            reason="test complete",
            path=self.db_path,
        )
        restarted = operator_runtime.start_operation(
            draft.operator_id,
            mode="shadow",
            priority=50,
            registered_projects=[self.project],
            path=self.db_path,
        )
        self.assertNotEqual(restarted["id"], operation["id"])

    def test_live_start_requires_available_project_and_executable_gate(self) -> None:
        draft = self.approve_contract()
        with self.assertRaisesRegex(
            operator_runtime.RuntimeError,
            "required verification command",
        ):
            operator_runtime.start_operation(
                draft.operator_id,
                mode="live",
                priority=50,
                registered_projects=[self.project],
                path=self.db_path,
            )

        verified = self.approve_contract(
            name="Live runtime",
            live_verification=True,
        )
        unavailable = {**self.project, "available": False}
        with self.assertRaisesRegex(operator_runtime.RuntimeError, "unavailable"):
            operator_runtime.start_operation(
                verified.operator_id,
                mode="live",
                priority=50,
                registered_projects=[unavailable],
                path=self.db_path,
            )
        operation = operator_runtime.start_operation(
            verified.operator_id,
            mode="live",
            priority=50,
            registered_projects=[self.project],
            path=self.db_path,
        )
        self.assertEqual(operation["mode"], "live")

    def test_verification_command_must_be_bounded_and_in_scope(self) -> None:
        data = operator_contract.template(
            name="Bad verification",
            goal="Verify safely",
            project_ids=[PROJECT_ID],
            gates=["Tests pass"],
        )
        data["quality"]["verification"] = [{
            "name": "unsafe",
            "project_id": "c" * 16,
            "argv": ["/bin/sh", "-c", "anything"],
            "timeout_seconds": 100_000,
            "required": True,
            "phase": "both",
        }]
        with self.assertRaises(operator_contract.ContractError) as caught:
            operator_contract.validate_contract(data)
        message = str(caught.exception)
        self.assertIn("must reference a project", message)
        self.assertIn("not an absolute executable", message)
        self.assertIn("between 1 and 86400", message)

    def test_verification_rejects_shell_launchers(self) -> None:
        data = operator_contract.template(
            name="No shell verification",
            goal="Verify safely",
            project_ids=[PROJECT_ID],
            gates=["Tests pass"],
        )
        data["quality"]["verification"] = [{
            "name": "unsafe shell",
            "project_id": PROJECT_ID,
            "argv": ["sh", "-c", "tests && anything"],
            "timeout_seconds": 60,
            "required": True,
            "phase": "both",
        }]
        with self.assertRaisesRegex(
            operator_contract.ContractError,
            "shell and env launchers are forbidden",
        ):
            operator_contract.validate_contract(data)

    def test_controller_lease_is_exclusive_and_recoverable_after_expiry(self) -> None:
        _, operation = self.start_shadow()
        first = operator_runtime.acquire_lease(
            operation["id"],
            holder="controller-a",
            lease_seconds=10,
            now_epoch=100,
            path=self.db_path,
        )
        with self.assertRaises(operator_runtime.LeaseBusyError):
            operator_runtime.acquire_lease(
                operation["id"],
                holder="controller-b",
                lease_seconds=10,
                now_epoch=105,
                path=self.db_path,
            )
        second = operator_runtime.acquire_lease(
            operation["id"],
            holder="controller-b",
            lease_seconds=10,
            now_epoch=111,
            path=self.db_path,
        )
        self.assertGreater(second.generation, first.generation)
        with self.assertRaises(operator_runtime.LeaseLostError):
            operator_runtime.heartbeat_lease(
                first,
                lease_seconds=10,
                now_epoch=112,
                path=self.db_path,
            )
        refreshed = operator_runtime.heartbeat_lease(
            second,
            lease_seconds=10,
            now_epoch=112,
            path=self.db_path,
        )
        self.assertEqual(refreshed.expires_at_epoch, 122)

    def test_resource_lease_requires_positive_proof_before_release(self) -> None:
        _, operation = self.start_shadow()
        work = operator_runtime.work_items(operation["id"], path=self.db_path)[0]
        resource_id = operator_runtime.register_resource_lease(
            operation["id"],
            work_item_id=work["id"],
            project_key=work["project_key"],
            kind="git_worktree",
            resource_path=str(self.project_root / ".orchestra" / "worktrees" / "run-1"),
            project_run_id=1,
            unique_state=True,
            details={"branch": "orchestra/run-1"},
            path=self.db_path,
        )
        with self.assertRaisesRegex(operator_runtime.RuntimeError, "unique state"):
            operator_runtime.release_resource_lease(
                resource_id,
                state="released",
                unique_state=True,
                details={},
                path=self.db_path,
            )
        operator_runtime.release_resource_lease(
            resource_id,
            state="released",
            unique_state=False,
            details={"integrated_head": "abc"},
            path=self.db_path,
        )
        resource = operator_runtime.resource_leases(
            operation["id"], path=self.db_path
        )[0]
        self.assertEqual(resource["state"], "released")
        self.assertFalse(resource["unique_state"])

    def test_project_mutation_lease_serializes_integration(self) -> None:
        _, operation = self.start_shadow()
        project_key = operation["projects"][0]["project_key"]
        first = operator_runtime.acquire_project_lease(
            operation["id"],
            project_key,
            holder="controller-a",
            purpose="integrate",
            now_epoch=100,
            path=self.db_path,
        )
        with self.assertRaises(operator_runtime.LeaseBusyError):
            operator_runtime.acquire_project_lease(
                operation["id"],
                project_key,
                holder="controller-b",
                purpose="integrate",
                now_epoch=101,
                path=self.db_path,
            )
        operator_runtime.release_project_lease(
            project_key, first, path=self.db_path
        )
        second = operator_runtime.acquire_project_lease(
            operation["id"],
            project_key,
            holder="controller-b",
            purpose="integrate",
            now_epoch=102,
            path=self.db_path,
        )
        self.assertEqual(second.holder, "controller-b")

    def test_attempt_snapshot_is_bounded_and_bound_to_current_lease(self) -> None:
        _, operation = self.start_shadow()
        lease = operator_runtime.acquire_lease(
            operation["id"],
            holder="controller",
            path=self.db_path,
        )
        attempt_id = operator_runtime.begin_attempt(
            lease,
            {"work": ["one"], "state": "active"},
            path=self.db_path,
        )
        operator_runtime.finish_attempt(
            attempt_id,
            outcome="actions_scheduled",
            summary="one action",
            path=self.db_path,
        )
        con = operator_runtime.connect(self.db_path)
        try:
            row = con.execute(
                "SELECT snapshot_sha256, snapshot_json, outcome "
                "FROM operator_attempts WHERE id=?",
                (attempt_id,),
            ).fetchone()
        finally:
            con.close()
        self.assertEqual(len(row["snapshot_sha256"]), 64)
        self.assertEqual(json.loads(row["snapshot_json"])["state"], "active")
        self.assertEqual(row["outcome"], "actions_scheduled")

    def test_authority_modes_gate_action_intents_and_owner_answer(self) -> None:
        _, operation = self.start_shadow()
        work = operator_runtime.work_items(operation["id"], path=self.db_path)[0]
        automatic = operator_runtime.propose_action(
            operation["id"],
            attempt_id=None,
            idempotency_key="dispatch:" + work["id"],
            kind="dispatch",
            authority_action="manage_workers",
            target={"work_item_id": work["id"]},
            evidence={"reason": "ready work"},
            work_item_id=work["id"],
            path=self.db_path,
        )
        self.assertEqual(automatic["state"], "authorized")
        repeated = operator_runtime.propose_action(
            operation["id"],
            attempt_id=None,
            idempotency_key="dispatch:" + work["id"],
            kind="dispatch",
            authority_action="manage_workers",
            target={"different": True},
            evidence={},
            work_item_id=work["id"],
            path=self.db_path,
        )
        self.assertEqual(repeated["id"], automatic["id"])

        waiting = operator_runtime.propose_action(
            operation["id"],
            attempt_id=None,
            idempotency_key="publish:" + work["id"],
            kind="publish",
            authority_action="publish_external",
            target={"artifact": "build.zip"},
            evidence={"gate": "passed"},
            work_item_id=work["id"],
            path=self.db_path,
        )
        self.assertEqual(waiting["state"], "waiting")
        decision = operator_runtime.decisions(
            operation["id"],
            state="open",
            path=self.db_path,
        )[0]
        self.assertEqual(
            operator_runtime.work_items(operation["id"], path=self.db_path)[0]["state"],
            "needs_decision",
        )
        operator_runtime.answer_decision(
            decision["id"],
            answer="approve",
            answered_by="owner",
            path=self.db_path,
        )
        con = operator_runtime.connect(self.db_path)
        try:
            action_state = con.execute(
                "SELECT state FROM operator_action_intents WHERE id=?",
                (waiting["id"],),
            ).fetchone()["state"]
        finally:
            con.close()
        self.assertEqual(action_state, "authorized")

        denied = operator_runtime.propose_action(
            operation["id"],
            attempt_id=None,
            idempotency_key="rewrite:" + work["id"],
            kind="rewrite_history",
            authority_action="rewrite_history",
            target={},
            evidence={},
            path=self.db_path,
        )
        self.assertEqual(denied["state"], "denied")

    def test_decision_is_idempotent_and_unblocks_work(self) -> None:
        _, operation = self.start_shadow()
        work = operator_runtime.work_items(operation["id"], path=self.db_path)[0]
        kwargs = {
            "operation_id": operation["id"],
            "idempotency_key": "decision:scope",
            "question": "Which source is authoritative?",
            "why_now": "The sources conflict.",
            "options": [
                {"id": "a", "label": "Use A"},
                {"id": "b", "label": "Use B"},
            ],
            "recommendation": "Use A because it is executable.",
            "evidence": {"a": "capture", "b": "notes"},
            "blocking_scope": {"goal": "G1"},
            "work_item_id": work["id"],
            "path": self.db_path,
        }
        first = operator_runtime.create_decision(**kwargs)
        second = operator_runtime.create_decision(**kwargs)
        self.assertEqual(first, second)
        operator_runtime.answer_decision(
            first,
            answer="Use A",
            answered_by="owner",
            path=self.db_path,
        )
        updated = operator_runtime.work_items(operation["id"], path=self.db_path)[0]
        self.assertEqual(updated["state"], "ready")

    def test_acceptance_is_independent_from_successful_run_state(self) -> None:
        _, operation = self.start_shadow()
        work = operator_runtime.work_items(operation["id"], path=self.db_path)[0]
        operator_runtime.bind_work_run(
            work["id"],
            profile="kimi",
            run_id=17,
            branch="orchestra/run-17",
            base_head="abc",
            path=self.db_path,
        )
        dispatched = operator_runtime.work_items(operation["id"], path=self.db_path)[0]
        self.assertEqual(dispatched["state"], "dispatched")
        self.assertNotEqual(operation["goals"][0]["state"], "accepted")
        operator_runtime.record_work_result(
            work["id"],
            state="handed_off",
            handoff={"summary": "implemented"},
            path=self.db_path,
        )
        self.assertEqual(
            operator_runtime.get_operation(operation["id"], path=self.db_path)[
                "goals"
            ][0]["state"],
            "active",
        )
        operator_runtime.record_work_result(
            work["id"],
            state="accepted",
            verification={"commands": [{"exit_code": 0}]},
            path=self.db_path,
        )
        self.assertEqual(
            operator_runtime.get_operation(operation["id"], path=self.db_path)[
                "goals"
            ][0]["state"],
            "accepted",
        )

    def test_time_and_event_wakeups_are_named_and_deterministic(self) -> None:
        _, operation = self.start_shadow()
        timed = operator_runtime.schedule_wakeup(
            operation["id"],
            kind="quota_reset",
            reason="Codex weekly window resets",
            due_at_epoch=200,
            path=self.db_path,
        )
        evented = operator_runtime.schedule_wakeup(
            operation["id"],
            kind="run_settled",
            reason="Wait for run 7",
            event_key="run:b:7",
            path=self.db_path,
        )
        self.assertEqual(
            operator_runtime.fire_wakeups(
                operation["id"],
                now_epoch=199,
                path=self.db_path,
            ),
            [],
        )
        fired = operator_runtime.fire_wakeups(
            operation["id"],
            event_keys=["run:b:7"],
            now_epoch=201,
            path=self.db_path,
        )
        self.assertEqual(set(fired), {timed, evented})

    def test_runtime_text_is_redacted_before_persistence(self) -> None:
        _, operation = self.start_shadow()
        operator_runtime.record_observation(
            operation["id"],
            kind="test",
            subject="credential",
            payload={"message": "api_key=abcdefghijklmnop123456"},
            path=self.db_path,
        )
        con = operator_runtime.connect(self.db_path)
        try:
            payload = con.execute(
                "SELECT payload_json FROM operator_observations"
            ).fetchone()["payload_json"]
        finally:
            con.close()
        self.assertNotIn("abcdefghijklmnop123456", payload)
        self.assertIn("[REDACTED]", payload)

    def test_runtime_event_log_is_immutable(self) -> None:
        _, operation = self.start_shadow()
        con = sqlite3.connect(self.db_path)
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                con.execute(
                    "DELETE FROM operator_runtime_events WHERE operation_id=?",
                    (operation["id"],),
                )
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
