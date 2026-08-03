from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from orchestra_cli import cli, db, incidents


def _project() -> tuple[tempfile.TemporaryDirectory, Path]:
    temporary_directory = tempfile.TemporaryDirectory()
    root = Path(temporary_directory.name)
    (root / ".orchestra").mkdir()
    return temporary_directory, root


class SystemicIncidentLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory, self.root = _project()
        self.con = db.connect(self.root)

    def tearDown(self) -> None:
        self.con.close()
        self.temporary_directory.cleanup()

    def record(self, **overrides: object) -> dict:
        fields: dict[str, object] = {
            "fingerprint": "gui-window-unavailable",
            "scope": "codex:workspace-write",
            "title": "Worker cannot create a Cocoa window",
            "evidence": "XCreateWindow failed before the smoke test.",
            "estimated_lost_seconds": 120,
        }
        fields.update(overrides)
        result = incidents.record_incident(self.con, **fields)  # type: ignore[arg-type]
        self.con.commit()
        return result

    def test_same_fingerprint_and_scope_deduplicate_and_keep_evidence(self) -> None:
        first = self.record(run_id=63, work_item="W-0050")
        second = self.record(
            evidence="A second worker failed at XCreateWindow.",
            estimated_lost_seconds=180,
            run_id=68,
            work_item="W-0053",
        )

        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["occurrence_count"], 2)
        self.assertEqual(second["estimated_lost_seconds"], 300)
        self.assertEqual(len(second["evidence"]), 2)
        self.assertEqual(second["evidence"][0]["run_id"], 68)
        self.assertEqual(second["evidence"][1]["work_item"], "W-0050")

    def test_same_fingerprint_in_different_scopes_is_a_different_incident(self) -> None:
        first = self.record()
        second = self.record(scope="opencode:k3", estimated_lost_seconds=0)

        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(len(incidents.list_incidents(self.con)), 2)
        self.assertEqual(
            [row["id"] for row in incidents.list_incidents(
                self.con, scope="codex:workspace-write"
            )],
            [first["id"]],
        )

    def test_records_accumulate_impact_and_preserve_optional_links(self) -> None:
        incident = self.record(estimated_lost_seconds=0)
        incident = self.record(
            estimated_lost_seconds=10_800,
            run_id=59,
            work_item="W-0050",
        )

        self.assertEqual(incident["estimated_lost_seconds"], 10_800)
        newest_evidence = incident["evidence"][0]
        self.assertEqual(newest_evidence["estimated_lost_seconds"], 10_800)
        self.assertEqual(newest_evidence["run_id"], 59)
        self.assertEqual(newest_evidence["work_item"], "W-0050")

    def test_state_transitions_require_resolution_proof_and_reopen_on_recurrence(self) -> None:
        incident = self.record()
        mitigated = incidents.set_incident_state(
            self.con,
            incident["id"],
            "mitigated",
            remediation="Use a profile with a successful GUI probe.",
        )
        self.con.commit()
        self.assertEqual(mitigated["state"], "mitigated")
        self.assertEqual(mitigated["remediation"], "Use a profile with a successful GUI probe.")

        with self.assertRaisesRegex(incidents.IncidentValidationError, "resolution_evidence"):
            incidents.set_incident_state(self.con, incident["id"], "resolved")
        resolved = incidents.set_incident_state(
            self.con,
            incident["id"],
            "resolved",
            resolution_evidence="A fresh GUI probe completed successfully.",
        )
        self.con.commit()
        self.assertEqual(resolved["state"], "resolved")

        reopened = self.record(evidence="The GUI probe failed again.")
        self.assertEqual(reopened["state"], "open")
        self.assertEqual(
            reopened["resolution_evidence"],
            "A fresh GUI probe completed successfully.",
        )

    def test_malformed_inputs_and_unknown_incidents_are_rejected(self) -> None:
        with self.assertRaisesRegex(incidents.IncidentValidationError, "fingerprint"):
            self.record(fingerprint="  ")
        with self.assertRaisesRegex(incidents.IncidentValidationError, "run_id"):
            self.record(run_id=0)
        with self.assertRaisesRegex(incidents.IncidentValidationError, "estimated_lost_seconds"):
            self.record(estimated_lost_seconds=-1)
        with self.assertRaisesRegex(incidents.IncidentValidationError, "state"):
            incidents.list_incidents(self.con, state="closed")
        with self.assertRaises(incidents.UnknownIncidentError):
            incidents.set_incident_state(self.con, 999, "open")

    def test_incidents_persist_after_reopening_the_project_database(self) -> None:
        incident = self.record()
        self.con.close()
        self.con = db.connect(self.root)

        persisted = incidents.get_incident(self.con, incident["id"])
        self.assertEqual(persisted["fingerprint"], "gui-window-unavailable")
        self.assertEqual(persisted["occurrence_count"], 1)
        self.assertEqual(len(persisted["evidence"]), 1)

    def test_recording_respects_the_callers_transaction_boundary(self) -> None:
        incidents.record_incident(
            self.con,
            fingerprint="transaction-boundary",
            scope="test",
            title="Ledger must not commit its caller's work",
            evidence="This observation will be rolled back.",
        )
        self.con.rollback()

        self.assertEqual(incidents.list_incidents(self.con), [])

    def test_cli_records_an_incident_visible_to_later_operator_runs(self) -> None:
        args = SimpleNamespace(
            fingerprint="gui-window-unavailable",
            scope="codex:workspace-write",
            title="Codex cannot reach WindowServer",
            evidence="Runs 63 and 68 stopped at XCreateWindow",
            run=None,
            work="W-0050",
            lost_seconds=10_800,
            remediation="Route GUI verification to a proven lane",
        )
        with mock.patch.object(cli.paths, "find_root", return_value=self.root), \
                redirect_stdout(StringIO()):
            cli.cmd_incident_record(args)

        rows = incidents.list_incidents(self.con)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["estimated_lost_seconds"], 10_800)


if __name__ == "__main__":
    unittest.main()
