from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestra_cli import (
    cli,
    operator_contract,
    operator_store,
    projects,
)


PROJECT_ID = "a" * 16


def _contract(**overrides):
    data = operator_contract.template(
        name="PIU fidelity",
        goal="Close the verified fidelity backlog",
        project_ids=[PROJECT_ID],
        gates=["Python tests pass", "Native build passes"],
        non_goals=["Redesign authentic behavior without evidence"],
    )
    data.update(overrides)
    return data


def _project_rows():
    return [{
        "id": PROJECT_ID,
        "name": "piu",
        "root": "/private/tmp/piu",
        "available": True,
    }]


class ContractValidationTests(unittest.TestCase):
    def test_template_is_complete_and_canonical_hash_is_stable(self) -> None:
        first = operator_contract.validate_contract(_contract())
        reordered = json.loads(
            json.dumps(_contract(), sort_keys=True, separators=(",", ":"))
        )
        second = operator_contract.validate_contract(reordered)
        self.assertEqual(first.canonical_bytes, second.canonical_bytes)
        self.assertEqual(first.sha256, second.sha256)
        self.assertEqual(len(first.sha256), 64)

    def test_duplicate_json_keys_are_rejected(self) -> None:
        with self.assertRaisesRegex(operator_contract.ContractError, "duplicate key"):
            operator_contract.parse_contract('{"schema":"x","schema":"y"}')

    def test_unknown_and_missing_fields_are_reported_together(self) -> None:
        data = _contract()
        del data["reporting"]
        data["surprise"] = {}
        with self.assertRaises(operator_contract.ContractError) as caught:
            operator_contract.validate_contract(data)
        message = str(caught.exception)
        self.assertIn("missing required field 'reporting'", message)
        self.assertIn("unknown field 'surprise'", message)

    def test_programmatic_non_string_field_name_is_a_validation_error(self) -> None:
        data = _contract()
        data[7] = "not JSON-shaped"
        with self.assertRaisesRegex(
            operator_contract.ContractError,
            "field names must be strings",
        ):
            operator_contract.validate_contract(data)

    def test_non_delegable_authority_cannot_be_relaxed(self) -> None:
        data = _contract()
        data["authority"]["rewrite_history"] = "ask"
        with self.assertRaisesRegex(
            operator_contract.ContractError,
            "non-delegable invariant",
        ):
            operator_contract.validate_contract(data)

    def test_quality_floor_cannot_be_downgraded_for_spare_quota(self) -> None:
        data = _contract()
        data["routing"]["downgrade_below_minimum"] = True
        with self.assertRaisesRegex(
            operator_contract.ContractError,
            "spare quota cannot override",
        ):
            operator_contract.validate_contract(data)

    def test_credential_bearing_fields_are_rejected(self) -> None:
        data = _contract()
        data["resources"]["api_key"] = "should-never-be-stored"
        with self.assertRaises(operator_contract.ContractError) as caught:
            operator_contract.validate_contract(data)
        self.assertIn("credential-bearing fields are forbidden", str(caught.exception))

    def test_boolean_is_not_accepted_as_an_integer_budget(self) -> None:
        data = _contract()
        data["resources"]["max_active_runs"] = True
        with self.assertRaisesRegex(operator_contract.ContractError, "must be an integer"):
            operator_contract.validate_contract(data)

    def test_project_reference_must_be_a_registry_id(self) -> None:
        data = _contract()
        data["scope"]["projects"] = ["/Users/me/secret/project"]
        with self.assertRaisesRegex(
            operator_contract.ContractError,
            "registered project id",
        ):
            operator_contract.validate_contract(data)

    def test_oversized_input_is_rejected_before_json_parse(self) -> None:
        raw = " " * (operator_contract.MAX_CONTRACT_BYTES + 1)
        with self.assertRaisesRegex(operator_contract.ContractError, "limit is"):
            operator_contract.parse_contract(raw)


class OperatorStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db_path = self.tmp / "private" / "operator.db"
        self.validated = operator_contract.validate_contract(_contract())
        self.addCleanup(self._tmp.cleanup)

    def draft(self):
        return operator_store.save_draft(
            self.validated,
            _project_rows(),
            path=self.db_path,
        )

    def test_store_is_owner_private_and_contract_reconstructs_byte_for_byte(self) -> None:
        result = self.draft()
        self.assertTrue(result.created)
        self.assertEqual(self.db_path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.db_path.stat().st_mode & 0o777, 0o600)
        loaded = operator_store.get_contract(result.operator_id, path=self.db_path)
        self.assertEqual(loaded.canonical_bytes, self.validated.canonical_bytes)
        self.assertEqual(loaded.sha256, self.validated.sha256)

    def test_saving_identical_latest_contract_is_idempotent(self) -> None:
        first = self.draft()
        second = self.draft()
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(second.version, 1)
        self.assertEqual(
            operator_store.get_status(first.operator_id, path=self.db_path)[
                "contract_version"
            ],
            1,
        )

    def test_amendment_creates_immutable_version_and_semantic_diff(self) -> None:
        first = self.draft()
        amended_data = _contract()
        amended_data["resources"]["max_active_runs"] = 2
        amended = operator_contract.validate_contract(amended_data)
        second = operator_store.save_draft(
            amended,
            _project_rows(),
            path=self.db_path,
        )
        self.assertEqual(second.version, 2)
        self.assertIn("$.resources.max_active_runs", second.changed_paths)
        old = operator_store.get_contract(
            first.operator_id,
            version=1,
            path=self.db_path,
        )
        self.assertEqual(old.sha256, first.sha256)
        self.assertEqual(
            operator_store.get_contract(first.operator_id, path=self.db_path).sha256,
            amended.sha256,
        )

    def test_approval_requires_exact_latest_version_and_hash(self) -> None:
        first = self.draft()
        with self.assertRaisesRegex(operator_store.ApprovalError, "does not match"):
            operator_store.approve(
                first.operator_id,
                version=1,
                sha256="0" * 64,
                approved_by="owner",
                path=self.db_path,
            )
        approval = operator_store.approve(
            first.operator_id,
            version=1,
            sha256=first.sha256,
            approved_by="owner",
            path=self.db_path,
        )
        self.assertTrue(approval.created)
        repeated = operator_store.approve(
            first.operator_id,
            version=1,
            sha256=first.sha256,
            approved_by="someone-else",
            path=self.db_path,
        )
        self.assertFalse(repeated.created)
        self.assertEqual(repeated.approved_by, "owner")
        status = operator_store.get_status(first.operator_id, path=self.db_path)
        self.assertEqual(status["state"], "approved")
        self.assertEqual(
            status["next_action"],
            "approved contract is ready; no operation is active",
        )

    def test_new_draft_after_approval_returns_to_awaiting_approval(self) -> None:
        first = self.draft()
        operator_store.approve(
            first.operator_id,
            version=1,
            sha256=first.sha256,
            approved_by="owner",
            path=self.db_path,
        )
        amended_data = _contract()
        amended_data["reporting"]["digest"] = "daily"
        operator_store.save_draft(
            operator_contract.validate_contract(amended_data),
            _project_rows(),
            path=self.db_path,
        )
        status = operator_store.get_status(first.operator_id, path=self.db_path)
        self.assertEqual(status["state"], "awaiting_approval")
        self.assertEqual(status["approved_versions"], [1])
        with self.assertRaisesRegex(operator_store.ApprovalError, "not the latest"):
            operator_store.approve(
                first.operator_id,
                version=1,
                sha256=first.sha256,
                approved_by="owner",
                path=self.db_path,
            )

    def test_unregistered_project_is_rejected_by_store_boundary(self) -> None:
        with self.assertRaisesRegex(
            operator_store.OperatorStoreError,
            "unregistered project ids",
        ):
            operator_store.save_draft(
                self.validated,
                [],
                path=self.db_path,
            )

    def test_store_revalidates_canonical_bytes_after_caller_mutation(self) -> None:
        original_name = self.validated.data["name"]
        self.validated.data["name"] = "mutated after validation"
        result = operator_store.save_draft(
            self.validated,
            _project_rows(),
            path=self.db_path,
        )
        self.assertEqual(result.name, original_name)
        stored = operator_store.get_contract(result.operator_id, path=self.db_path)
        self.assertEqual(stored.data["name"], original_name)

    def test_contract_tables_are_immutable_at_database_layer(self) -> None:
        result = self.draft()
        con = sqlite3.connect(self.db_path)
        try:
            con.execute("PRAGMA foreign_keys = ON")
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                con.execute(
                    "UPDATE contract_versions SET content_json='{}' "
                    "WHERE operator_id=? AND version=1",
                    (result.operator_id,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute(
                    "INSERT INTO contract_approvals("
                    "operator_id, contract_version, content_sha256, approved_at, approved_by"
                    ") VALUES(?,?,?,?,?)",
                    (result.operator_id, 1, "0" * 64, "2026-01-01T00:00:00Z", "owner"),
                )
        finally:
            con.close()

    def test_status_rendering_is_deterministic_and_transcript_free(self) -> None:
        result = self.draft()
        first = operator_store.get_status(result.operator_id, path=self.db_path)
        second = operator_store.get_status(result.operator_id, path=self.db_path)
        self.assertEqual(first, second)
        rendered = operator_store.render_status(first)
        self.assertIn("state: awaiting_approval", rendered)
        self.assertIn("4 active runs", rendered)
        self.assertNotIn("transcript", json.dumps(first))


class OperatorCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.project = self.tmp / "project"
        (self.project / ".orchestra").mkdir(parents=True)
        self.registry = self.tmp / "config" / "projects.json"
        self.operator_db = self.tmp / "config" / "operator.db"
        self.saved_registry = os.environ.get("ORCHESTRA_PROJECTS_FILE")
        self.saved_operator_db = os.environ.get("ORCHESTRA_OPERATOR_DB")
        os.environ["ORCHESTRA_PROJECTS_FILE"] = str(self.registry)
        os.environ["ORCHESTRA_OPERATOR_DB"] = str(self.operator_db)
        self.entry = projects.register(self.project)
        self.addCleanup(self._restore_environment)
        self.addCleanup(self._tmp.cleanup)

    def _restore_environment(self) -> None:
        if self.saved_registry is None:
            os.environ.pop("ORCHESTRA_PROJECTS_FILE", None)
        else:
            os.environ["ORCHESTRA_PROJECTS_FILE"] = self.saved_registry
        if self.saved_operator_db is None:
            os.environ.pop("ORCHESTRA_OPERATOR_DB", None)
        else:
            os.environ["ORCHESTRA_OPERATOR_DB"] = self.saved_operator_db

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = 0
        with (
            mock.patch.object(sys, "argv", ["orchestra", *argv]),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            try:
                cli.main()
            except SystemExit as exc:
                code = int(exc.code or 0) if isinstance(exc.code, int) else 1
                if not isinstance(exc.code, int) and exc.code:
                    stderr.write(str(exc.code))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_template_draft_approve_show_and_exact_export(self) -> None:
        contract_file = self.tmp / "contract.json"
        code, stdout, stderr = self.run_cli(
            "operator",
            "template",
            "Autonomous PIU",
            "--goal",
            "Finish the verified backlog",
            "--project",
            self.entry["id"],
            "--gate",
            "Full tests pass",
            "--output",
            str(contract_file),
        )
        self.assertEqual((code, stderr), (0, ""))
        self.assertTrue(contract_file.is_file())
        self.assertEqual(contract_file.stat().st_mode & 0o777, 0o600)
        self.assertIn("sha256:", stdout)

        code, stdout, stderr = self.run_cli(
            "operator", "draft", str(contract_file)
        )
        self.assertEqual((code, stderr), (0, ""))
        first_line = stdout.splitlines()[0]
        operator_id = first_line.split()[1]
        digest = next(
            line.strip().removeprefix("sha256:")
            for line in stdout.splitlines()
            if line.strip().startswith("sha256:")
        )

        code, stdout, stderr = self.run_cli(
            "operator",
            "approve",
            operator_id,
            "--version",
            "1",
            "--hash",
            digest,
        )
        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("no operation is active", stdout)

        code, stdout, stderr = self.run_cli(
            "operator", "show", operator_id, "--json"
        )
        self.assertEqual((code, stderr), (0, ""))
        status = json.loads(stdout)
        self.assertEqual(status["state"], "approved")

        exported = self.tmp / "canonical.json"
        code, stdout, stderr = self.run_cli(
            "operator",
            "export",
            operator_id,
            "--output",
            str(exported),
        )
        self.assertEqual((code, stderr), (0, ""))
        validated = operator_contract.load_contract(contract_file)
        self.assertEqual(exported.read_bytes(), validated.canonical_bytes)

    def test_template_rejects_unregistered_project(self) -> None:
        code, _, stderr = self.run_cli(
            "operator",
            "template",
            "Bad scope",
            "--goal",
            "Do something",
            "--project",
            "b" * 16,
            "--gate",
            "Tests pass",
        )
        self.assertEqual(code, 1)
        self.assertIn("unregistered project ids", stderr)

    def test_export_refuses_to_overwrite(self) -> None:
        validated = operator_contract.validate_contract(
            operator_contract.template(
                name="No overwrite",
                goal="Keep exports safe",
                project_ids=[self.entry["id"]],
                gates=["Tests pass"],
            )
        )
        result = operator_store.save_draft(
            validated,
            [{
                **self.entry,
                "available": True,
            }],
        )
        output = self.tmp / "exists.json"
        output.write_text("keep", encoding="utf-8")
        code, _, stderr = self.run_cli(
            "operator",
            "export",
            result.operator_id,
            "--output",
            str(output),
        )
        self.assertEqual(code, 1)
        self.assertIn("refusing to overwrite", stderr)
        self.assertEqual(output.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
