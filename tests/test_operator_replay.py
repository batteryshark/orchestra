from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from orchestra_cli import (
    db,
    operator_contract,
    operator_replay,
    operator_runtime,
    operator_store,
)


class OperatorReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.control = self.tmp / "control" / "operator.db"
        self.root = self.tmp / "project"
        (self.root / ".orchestra").mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)

    def seed(self) -> Path:
        con = db.connect(self.root)
        con.execute(
            "INSERT INTO runs(agent, backend, requested_by, workdir, status, "
            "started_at, finished_at) "
            "VALUES('codex','codex','owner',?,'done','2026-01-01T00:00:00Z',"
            "'2026-01-01T00:01:00Z')",
            (str(self.root),),
        )
        con.execute(
            "INSERT INTO runs(agent, backend, requested_by, workdir, status, started_at) "
            "VALUES('fable','claude','owner',?,'running','2026-01-02T00:00:00Z')",
            (str(self.root),),
        )
        con.execute(
            "INSERT INTO messages(sender, recipient, body, created_at) "
            "VALUES('a','b','secret sk-test-key','2026-01-02T00:00:01Z')"
        )
        con.execute(
            "INSERT INTO feed(author, body, created_at) "
            "VALUES('a','finding','2026-01-02T00:00:02Z')"
        )
        con.commit()
        con.close()
        return self.root / ".orchestra" / "orchestra.db"

    def test_live_import_is_metadata_only_and_idempotent(self) -> None:
        source = self.seed()
        before = source.read_bytes()
        first = operator_replay.import_live_database(source, path=self.control)
        second = operator_replay.import_live_database(source, path=self.control)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["run_count"], 2)
        self.assertEqual(first["message_count"], 1)
        self.assertEqual(source.read_bytes(), before)
        con = operator_replay.connect(self.control)
        self.assertNotIn(
            "body",
            {row["name"] for row in con.execute("PRAGMA table_info(replay_messages)")},
        )
        con.close()

    def test_archive_import_and_clocked_replay(self) -> None:
        source = self.seed()
        archive = self.tmp / "runs.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.write(source, ".orchestra/orchestra.db")
        imported = operator_replay.import_archive(archive, path=self.control)
        early = operator_replay.replay_state(
            imported["id"],
            at="2026-01-01T12:00:00Z",
            path=self.control,
        )
        final = operator_replay.replay_state(imported["id"], path=self.control)
        during = operator_replay.replay_state(
            imported["id"],
            at="2026-01-01T00:00:30Z",
            path=self.control,
        )
        self.assertEqual(early["observed_runs"], 1)
        self.assertEqual(during["status_counts"], {"running": 1})
        self.assertEqual(final["observed_runs"], 2)
        self.assertEqual(final["active_run_ids"], [2])

    def test_archive_requires_the_database_member(self) -> None:
        archive = self.tmp / "empty.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("readme.txt", "nothing")
        with self.assertRaisesRegex(operator_replay.ReplayError, "does not contain"):
            operator_replay.import_archive(archive, path=self.control)

    def test_snapshot_cursor_drains_bounded_batches_without_skipping(self) -> None:
        self.seed()
        con = db.connect(self.root)
        for index in range(28):
            con.execute(
                "INSERT INTO runs(agent, backend, requested_by, workdir, status, "
                "started_at) VALUES('codex','codex','owner',?,'done',?)",
                (str(self.root), f"2026-02-01T00:00:{index:02d}Z"),
            )
        con.commit()
        con.close()
        project_id = "e" * 16
        project = {
            "id": project_id,
            "name": "project",
            "root": str(self.root),
            "available": True,
        }
        contract = operator_contract.validate_contract(
            operator_contract.template(
                name="snapshot",
                goal="observe",
                project_ids=[project_id],
                gates=["evidence observed"],
            )
        )
        draft = operator_store.save_draft(contract, [project], path=self.control)
        operator_store.approve(
            draft.operator_id,
            version=draft.version,
            sha256=draft.sha256,
            approved_by="owner",
            path=self.control,
        )
        operation = operator_runtime.start_operation(
            draft.operator_id,
            mode="shadow",
            priority=50,
            registered_projects=[project],
            path=self.control,
        )
        first = operator_replay.operation_snapshot(
            operation["id"], advance_cursors=True, path=self.control
        )
        second = operator_replay.operation_snapshot(
            operation["id"], advance_cursors=True, path=self.control
        )
        self.assertEqual(len(first["projects"][0]["runs"]), 25)
        self.assertEqual(len(second["projects"][0]["runs"]), 5)
        self.assertEqual(second["projects"][0]["runs"][0]["id"], 26)


if __name__ == "__main__":
    unittest.main()
