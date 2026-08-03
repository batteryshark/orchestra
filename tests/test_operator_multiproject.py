from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from orchestra_cli import (
    operator_broker,
    operator_contract,
    operator_runtime,
    operator_store,
)


PROJECT_A = "a1" * 8
PROJECT_B = "b2" * 8


def init_repo(root: Path, *, tracked: dict[str, str] | None = None) -> None:
    root.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-b", "main", str(root)],
        check=True,
        capture_output=True,
    )
    (root / ".gitignore").write_text(".orchestra/\n", encoding="utf-8")
    for relative, content in (tracked or {}).items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(root), "add", "."],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", "baseline"],
        check=True,
        capture_output=True,
    )


class MultiProjectContainmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.db = self.base / "operator.db"
        self.root_a = self.base / "piu-lift"
        self.root_b = self.base / "piu-live"
        init_repo(self.root_a, tracked={"rebirth.json": "{}\n"})
        init_repo(self.root_b, tracked={"config/rebirth.toml": "enabled = true\n"})
        self.projects = [
            {
                "id": PROJECT_A,
                "name": "piu-lift",
                "root": str(self.root_a),
                "available": True,
            },
            {
                "id": PROJECT_B,
                "name": "piu-live",
                "root": str(self.root_b),
                "available": True,
            },
        ]

    def approve(self, contract: dict) -> operator_store.DraftResult:
        validated = operator_contract.validate_contract(contract)
        draft = operator_store.save_draft(validated, self.projects, path=self.db)
        operator_store.approve(
            draft.operator_id,
            version=draft.version,
            sha256=draft.sha256,
            approved_by="owner",
            path=self.db,
        )
        return draft

    def multiproject_contract(self) -> dict:
        contract = operator_contract.template(
            name="Rebirth lift",
            goal="Lift the Rebirth executable",
            project_ids=[PROJECT_A, PROJECT_B],
            gates=["project verification passes"],
        )
        contract["scope"]["project_rules"] = [
            {
                "project_id": PROJECT_A,
                "include": ["rebirth.json"],
                "exclude": ["premiere2/**"],
            },
            {
                "project_id": PROJECT_B,
                "include": ["config/rebirth.toml"],
                "exclude": ["config/premiere2/**"],
            },
        ]
        contract["intent"]["goals"] = [
            {
                "id": "G1",
                "outcome": "Lift Rebirth metadata",
                "priority": 1,
                "project_id": PROJECT_A,
                "depends_on": [],
                "requires_review": False,
                "read_dependencies": [],
                "required_capabilities": ["cocoa-window"],
            },
            {
                "id": "G2",
                "outcome": "Wire the verified Rebirth runtime",
                "priority": 2,
                "project_id": PROJECT_B,
                "depends_on": ["G1"],
                "requires_review": True,
                "read_dependencies": [PROJECT_A],
                "required_capabilities": ["coreaudio-output"],
            },
        ]
        return contract

    def test_v1_live_multiproject_contract_is_rejected(self) -> None:
        contract = self.multiproject_contract()
        contract["schema"] = operator_contract.SCHEMA_TAG_V1
        contract["scope"].pop("project_rules")
        for goal in contract["intent"]["goals"]:
            for key in (
                "project_id",
                "depends_on",
                "requires_review",
                "read_dependencies",
                "required_capabilities",
            ):
                goal.pop(key)
        contract["quality"]["verification"] = [
            {
                "name": f"verify-{project_id}",
                "project_id": project_id,
                "argv": ["true"],
                "timeout_seconds": 30,
                "required": True,
                "phase": "both",
            }
            for project_id in (PROJECT_A, PROJECT_B)
        ]
        draft = self.approve(contract)
        with self.assertRaisesRegex(
            operator_runtime.RuntimeError, "require.*v2"
        ):
            operator_runtime.start_operation(
                draft.operator_id,
                mode="live",
                priority=50,
                registered_projects=self.projects,
                path=self.db,
            )

    def test_v2_binds_projects_dependencies_scopes_and_review_directly(self) -> None:
        draft = self.approve(self.multiproject_contract())
        operation = operator_runtime.start_operation(
            draft.operator_id,
            mode="shadow",
            priority=50,
            registered_projects=self.projects,
            path=self.db,
        )
        work = operator_runtime.work_items(operation["id"], path=self.db)
        self.assertEqual(
            [item["contract_project_id"] for item in work],
            [PROJECT_A, PROJECT_B],
        )
        self.assertEqual(work[0]["requirements"]["scope"]["include"], ["rebirth.json"])
        self.assertEqual(work[1]["requirements"]["scope"]["include"], ["config/rebirth.toml"])
        self.assertEqual(work[1]["dependencies"], [work[0]["id"]])
        self.assertFalse(operator_runtime.dependencies_satisfied(work[1], path=self.db))
        self.assertTrue(work[1]["requires_review"])
        self.assertEqual(work[1]["requirements"]["read_dependencies"], [PROJECT_A])
        self.assertEqual(work[0]["requirements"]["required_capabilities"], ["cocoa-window"])
        self.assertEqual(
            work[1]["requirements"]["required_capabilities"], ["coreaudio-output"]
        )

    def test_dirty_live_integration_checkout_starts_queued_for_retry(self) -> None:
        contract = self.multiproject_contract()
        contract["quality"]["verification"] = [
            {
                "name": f"verify-{project_id}",
                "project_id": project_id,
                "argv": ["true"],
                "timeout_seconds": 30,
                "required": True,
                "phase": "both",
            }
            for project_id in (PROJECT_A, PROJECT_B)
        ]
        draft = self.approve(contract)
        (self.root_b / "config" / "premiere2.toml").write_text(
            "unrelated = true\n", encoding="utf-8"
        )
        operation = operator_runtime.start_operation(
            draft.operator_id,
            mode="live",
            priority=50,
            registered_projects=self.projects,
            path=self.db,
        )
        self.assertEqual(operation["state"], "queued")
        admission = operator_runtime.inspect_live_operation(operation)
        self.assertEqual(admission["violations"], [])
        self.assertIn("dirty", admission["blockers"][0]["reason"])

    def test_clean_external_commit_is_detected_as_head_drift(self) -> None:
        contract = self.multiproject_contract()
        contract["quality"]["verification"] = [
            {
                "name": f"verify-{project_id}",
                "project_id": project_id,
                "argv": ["true"],
                "timeout_seconds": 30,
                "required": True,
                "phase": "both",
            }
            for project_id in (PROJECT_A, PROJECT_B)
        ]
        draft = self.approve(contract)
        operation = operator_runtime.start_operation(
            draft.operator_id,
            mode="live",
            priority=50,
            registered_projects=self.projects,
            path=self.db,
        )
        (self.root_b / "config" / "rebirth.toml").write_text(
            "enabled = false\n", encoding="utf-8"
        )
        subprocess.run(
            ["git", "-C", str(self.root_b), "add", "config/rebirth.toml"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root_b), "commit", "-m", "external change"],
            check=True,
            capture_output=True,
        )
        with self.assertRaisesRegex(operator_runtime.RuntimeError, "HEAD drifted"):
            operator_runtime.assert_live_operation_safe(
                operator_runtime.get_operation(operation["id"], path=self.db)
            )

    def test_sibling_project_symlink_bridge_is_rejected(self) -> None:
        namespace = self.root_a / ".orchestra" / "worktrees"
        namespace.mkdir(parents=True)
        (namespace / "piu-live").symlink_to(self.root_b, target_is_directory=True)
        with self.assertRaisesRegex(operator_broker.BrokerError, "links are forbidden"):
            operator_broker.assert_worktree_namespace(self.root_a)

    def test_read_dependency_is_commit_pinned_hashed_and_not_live(self) -> None:
        workdir = self.root_a / ".orchestra" / "worktrees" / "run-17"
        workdir.mkdir(parents=True)
        snapshots = operator_broker.materialize_read_inputs(
            workdir,
            [{"project_id": PROJECT_B, "root": str(self.root_b), "commit": "HEAD"}],
        )
        snapshot = snapshots[0]
        copied = Path(snapshot["path"]) / "config" / "rebirth.toml"
        self.assertEqual(copied.read_text(encoding="utf-8"), "enabled = true\n")
        self.assertEqual(len(snapshot["commit"]), 40)
        self.assertEqual(len(snapshot["sha256"]), 64)
        (self.root_b / "config" / "rebirth.toml").write_text(
            "enabled = false\n", encoding="utf-8"
        )
        self.assertEqual(copied.read_text(encoding="utf-8"), "enabled = true\n")

    def test_premiere_path_cannot_match_rebirth_project_scope(self) -> None:
        contract = self.multiproject_contract()
        include, exclude = operator_contract.project_scope(contract, PROJECT_A)
        violations = operator_broker.scope_violations(
            ["premiere2/analysis.json"],
            include=include,
            exclude=exclude,
        )
        self.assertTrue(violations)

    def test_dependency_cycles_are_rejected(self) -> None:
        contract = self.multiproject_contract()
        contract["intent"]["goals"][0]["depends_on"] = ["G2"]
        with self.assertRaisesRegex(operator_contract.ContractError, "dependency cycle"):
            operator_contract.validate_contract(contract)


if __name__ == "__main__":
    unittest.main()
