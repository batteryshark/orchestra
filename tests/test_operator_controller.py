from __future__ import annotations

import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from orchestra_cli import (
    db,
    operator_broker,
    operator_contract,
    operator_controller,
    operator_roster,
    operator_runtime,
    operator_store,
)


class OperatorControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.control = self.tmp / "operator.db"
        self.root = self.tmp / "project"
        (self.root / ".orchestra").mkdir(parents=True)
        subprocess.run(
            ["git", "init", "-b", "main", str(self.root)],
            check=True,
            capture_output=True,
        )
        (self.root / ".gitignore").write_text(".orchestra/\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.root), "add", ".gitignore"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "--allow-empty", "-m", "init"],
            check=True,
            capture_output=True,
        )
        db.connect(self.root).close()
        self.project_id = "d" * 16
        self.project = {
            "id": self.project_id,
            "name": "project",
            "root": str(self.root),
            "available": True,
        }
        self.policy = operator_roster.validate_policy({
            "schema": operator_roster.ROSTER_SCHEMA_TAG,
            "profiles": [{
                "name": "worker",
                "backend": "codex",
                "model": "gpt-5.6-sol",
                "role": "general implementation",
                "tier": "generalist",
                "capabilities": ["feature", "review"],
                "contraindications": [],
                "access": ["project", "git", "tests"],
                "actuation_modes": ["general_implementation", "review_only"],
                "enabled": True,
                "pools": ["codex-capacity"],
                "uncertainty": None,
            }],
            "pools": [{
                "id": "codex-capacity",
                "provider_id": "codex",
                "kind": "shared",
                "max_concurrency": 2,
                "reserve_percent": 0,
            }],
        })
        version, _ = operator_roster.save_policy(
            self.policy, source="test", path=self.control
        )
        operator_roster.approve_policy(
            version=version,
            sha256=self.policy.sha256,
            approved_by="owner",
            path=self.control,
        )
        self.addCleanup(self._tmp.cleanup)

    def operation(self, mode: str = "shadow") -> dict:
        contract = operator_contract.template(
            name=f"{mode} operator",
            goal="Implement the bounded feature",
            project_ids=[self.project_id],
            gates=["tests pass"],
        )
        if mode == "live":
            contract["quality"]["verification"] = [{
                "name": "tests",
                "project_id": self.project_id,
                "argv": ["true"],
                "timeout_seconds": 30,
                "required": True,
                "phase": "both",
            }]
        validated = operator_contract.validate_contract(contract)
        draft = operator_store.save_draft(
            validated, [self.project], path=self.control
        )
        operator_store.approve(
            draft.operator_id,
            version=draft.version,
            sha256=draft.sha256,
            approved_by="owner",
            path=self.control,
        )
        return operator_runtime.start_operation(
            draft.operator_id,
            mode=mode,
            priority=50,
            registered_projects=[self.project],
            path=self.control,
        )

    def report(self) -> dict:
        return {
            "roster": [{
                "name": "worker",
                "state": "available",
                "detail": "test",
            }]
        }

    def test_shadow_tick_records_route_and_action_without_dispatch(self) -> None:
        operation = self.operation()
        resources = {
            "worktree_count": 0,
            "worktree_bytes": 0,
            "free_disk_bytes": 10**15,
            "measurement_complete": True,
        }
        with (
            mock.patch(
                "orchestra_cli.operator_controller.operator_broker.resource_snapshot",
                return_value=resources,
            ),
            mock.patch(
                "orchestra_cli.operator_controller.availability.discover",
                return_value=self.report(),
            ),
            mock.patch(
                "orchestra_cli.operator_controller.operator_broker.dispatch"
            ) as dispatch,
        ):
            result = operator_controller.tick(operation["id"], path=self.control)
        self.assertEqual(result["mode"], "shadow")
        self.assertEqual(result["events"][0]["kind"], "shadow_dispatch")
        dispatch.assert_not_called()
        actions = operator_runtime.pending_actions(operation["id"], path=self.control)
        self.assertEqual(actions[0]["kind"], "dispatch isolated implementation")

    def test_paused_tick_is_side_effect_free(self) -> None:
        operation = self.operation()
        operator_runtime.set_operation_state(
            operation["id"], "paused", reason="owner", path=self.control
        )
        result = operator_controller.tick(operation["id"], path=self.control)
        self.assertEqual(result["operation_state"], "paused")
        self.assertEqual(result["events"], [])

    def test_live_work_is_accepted_only_after_verify_and_integrate(self) -> None:
        operation = self.operation(mode="live")
        resources = {
            "worktree_count": 0,
            "worktree_bytes": 0,
            "free_disk_bytes": 10**15,
            "measurement_complete": True,
        }
        dispatched = operator_broker.Dispatch(
            run_id=7,
            profile="worker",
            workdir=str(self.root / ".orchestra" / "worktrees" / "run-7"),
            branch="orchestra/run-7",
            base_head="abc123",
        )
        with (
            mock.patch(
                "orchestra_cli.operator_controller.operator_broker.resource_snapshot",
                return_value=resources,
            ),
            mock.patch(
                "orchestra_cli.operator_controller.availability.discover",
                return_value=self.report(),
            ),
            mock.patch(
                "orchestra_cli.operator_controller.operator_broker.dispatch",
                return_value=dispatched,
            ),
            mock.patch(
                "orchestra_cli.operator_controller.operator_broker.run_status",
                return_value={
                    "id": 7,
                    "status": "done",
                    "workdir": dispatched.workdir,
                    "summary": "implemented",
                },
            ),
            mock.patch(
                "orchestra_cli.operator_controller.operator_broker.seal_workspace",
                return_value="sealed123",
            ),
            mock.patch(
                "orchestra_cli.operator_controller.operator_broker.measure_change",
                return_value={"files": 1, "added_lines": 4, "deleted_lines": 1},
            ),
            mock.patch(
                "orchestra_cli.operator_controller.operator_broker.verify",
                return_value={"phase": "test", "passed": True, "commands": []},
            ) as verify,
            mock.patch(
                "orchestra_cli.operator_controller.operator_broker.integrate",
                return_value="def456",
            ) as integrate,
            mock.patch(
                "orchestra_cli.operator_controller.operator_broker.reclaim_integrated",
                return_value=True,
            ),
        ):
            first = operator_controller.tick(operation["id"], path=self.control)
            self.assertEqual(first["work_counts"], {"dispatched": 1})
            second = operator_controller.tick(operation["id"], path=self.control)
        self.assertEqual(second["operation_state"], "achieved")
        self.assertEqual(second["work_counts"], {"accepted": 1})
        self.assertEqual(verify.call_count, 2)
        integrate.assert_called_once()

    def test_integration_precondition_failure_creates_owner_decision(self) -> None:
        operation = self.operation(mode="live")
        work = operator_runtime.work_items(operation["id"], path=self.control)[0]
        operator_runtime.bind_work_run(
            work["id"],
            profile="worker",
            run_id=11,
            branch="orchestra/run-11",
            base_head="abc123",
            path=self.control,
        )
        work = operator_runtime.work_items(operation["id"], path=self.control)[0]
        contract = operator_store.get_contract(
            operation["operator_id"], path=self.control
        ).data
        events: list[dict] = []
        with mock.patch(
            "orchestra_cli.operator_controller.operator_broker.integrate",
            side_effect=operator_broker.BrokerError("integration checkout is dirty"),
        ):
            operator_controller._integrate(
                operation,
                work,
                contract,
                {"files": 1, "added_lines": 1},
                {"passed": True, "commands": []},
                events,
                path=self.control,
            )
        refreshed = operator_runtime.get_operation(operation["id"], path=self.control)
        self.assertEqual(refreshed["state"], "needs_decision")
        self.assertEqual(
            operator_runtime.work_items(operation["id"], path=self.control)[0]["state"],
            "needs_decision",
        )
        decisions = operator_runtime.decisions(
            operation["id"], state="open", path=self.control
        )
        self.assertEqual(len(decisions), 1)
        self.assertIn("cannot be integrated safely", decisions[0]["question"])

    def test_stuck_live_work_escalates_when_no_safe_quorum_exists(self) -> None:
        operation = self.operation(mode="live")
        work = operator_runtime.work_items(operation["id"], path=self.control)[0]
        contract = operator_store.get_contract(
            operation["operator_id"], path=self.control
        ).data
        events: list[dict] = []
        with mock.patch(
            "orchestra_cli.operator_controller.operator_roster.create_council",
            side_effect=operator_roster.RosterError(
                "recovery council requires at least two model families"
            ),
        ):
            operator_controller._escalate_stuck(
                operation,
                work,
                contract,
                events,
                path=self.control,
            )
        refreshed = operator_runtime.get_operation(operation["id"], path=self.control)
        self.assertEqual(refreshed["state"], "needs_decision")
        self.assertEqual(events[0]["kind"], "recovery_quorum_unavailable")
        decisions = operator_runtime.decisions(
            operation["id"], state="open", path=self.control
        )
        self.assertEqual(len(decisions), 1)
        self.assertIn("no safe recovery quorum", decisions[0]["question"])


if __name__ == "__main__":
    unittest.main()
