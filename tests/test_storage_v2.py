import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestra import artifacts, db, fleet_config, paths, runs, storage
from orchestra.contracts import RunRequest


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"ORCHESTRA_HOME": str(self.root / "state")})
        self.env.start()
        self.con = db.connect(":memory:")
        (self.root / "work").mkdir()
        fleet_config.create_runtime(
            self.con, "Exec", "exec", slug="exec", command=["agent"])
        fleet_config.create_profile(
            self.con, "Profile", "exec", slug="profile", tier=2)
        self.run, _ = runs.submit(self.con, RunRequest.from_mapping({
            "request_id": "run-1", "profile": "profile",
            "context": "Produce evidence", "cwd": str(self.root / "work"),
        }))
        self.run_id = int(self.run["id"])
        self.log = Path(self.run["log_path"])
        self.log.write_text('{"type":"result","result":"ok"}\n', encoding="utf-8")
        work = self.root / "work"
        (work / "result.txt").write_text("retained artifact", encoding="utf-8")
        self.artifact = artifacts.publish(self.con, self.run_id, "result.txt")
        self.con.execute(
            "UPDATE runs SET status='completed',finished_at='2020-01-01T00:00:00Z' "
            "WHERE id=?", (self.run_id,))
        self.con.commit()

    def tearDown(self):
        self.con.close()
        self.env.stop()
        self.temp.cleanup()

    def _observer_log(self) -> tuple[int, Path]:
        observer_log = paths.logs_dir() / "observer-7.jsonl"
        observer_log.write_text('{"action":"ok"}\n', encoding="utf-8")
        check_id = self.con.execute(
            "INSERT INTO observer_checks(run_id,profile_snapshot,runtime_snapshot,"
            "input_json,trigger,log_path,started_at,finished_at) "
            "VALUES(?,'{}','{}','{}','manual',?,'2020-01-01T00:00:00Z',"
            "'2020-01-01T00:00:01Z')",
            (self.run_id, str(observer_log))).lastrowid
        self.con.commit()
        return check_id, observer_log

    def test_pins_exclude_evidence_and_apply_requires_the_saved_plan(self):
        storage.pin(self.con, self.run_id, actor="device:test", reason="keep")
        empty = storage.create_plan(
            self.con, actor="device:test", older_than_days=0)
        self.assertEqual(empty["items"], [])
        storage.unpin(self.con, self.run_id, actor="device:test")

        plan = storage.create_plan(
            self.con, actor="device:test", older_than_days=0)
        self.assertEqual({item["kind"] for item in plan["items"]},
                         {"raw_log", "artifact"})
        applied = storage.apply_plan(
            self.con, plan["plan_id"], actor="device:test")
        self.assertEqual(applied["result"]["pruned_items"], 2)
        self.assertFalse(self.log.exists())
        self.assertFalse(artifacts.get(
            self.con, self.artifact["artifact_id"])["available"])
        cursor = self.con.execute(
            "SELECT raw_pruned_at FROM trace_cursors WHERE run_id=?",
            (self.run_id,)).fetchone()
        self.assertIsNotNone(cursor["raw_pruned_at"])
        self.assertEqual(
            storage.apply_plan(self.con, plan["plan_id"], actor="device:test")
            ["result"], applied["result"])

    def test_changed_file_is_skipped_instead_of_deleting_unreviewed_bytes(self):
        plan = storage.create_plan(
            self.con, actor="device:test", older_than_days=0,
            kinds=["raw_logs"])
        self.log.write_text("changed after review\n", encoding="utf-8")
        applied = storage.apply_plan(
            self.con, plan["plan_id"], actor="device:test")
        self.assertEqual(applied["result"]["pruned_items"], 0)
        self.assertEqual(applied["result"]["skipped_items"], 1)
        self.assertTrue(self.log.exists())

    def test_finished_observer_logs_share_reviewable_pinned_retention(self):
        check_id, observer_log = self._observer_log()

        plan = storage.create_plan(
            self.con, actor="device:test", older_than_days=0,
            kinds=["raw_logs"])
        self.assertEqual({item["kind"] for item in plan["items"]},
                         {"raw_log", "observer_log"})
        observer_item = next(item for item in plan["items"]
                             if item["kind"] == "observer_log")
        self.assertEqual(observer_item["check_id"], check_id)

        applied = storage.apply_plan(
            self.con, plan["plan_id"], actor="device:test")
        self.assertEqual(applied["result"]["pruned_items"], 2)
        self.assertFalse(observer_log.exists())
        check = self.con.execute(
            "SELECT verdict,log_pruned_at FROM observer_checks WHERE id=?",
            (check_id,)).fetchone()
        self.assertIsNone(check["verdict"])
        self.assertIsNotNone(check["log_pruned_at"])

    def test_crash_after_rename_restores_uncommitted_artifact(self):
        plan = storage.create_plan(
            self.con, actor="device:test", older_than_days=0,
            kinds=["artifacts"])
        item = plan["items"][0]
        original = Path(item["path"])
        real_stage = storage._stage_removal

        def crash_after_rename(path, staged):
            real_stage(path, staged)
            raise SystemExit("injected crash")

        with self.assertRaises(SystemExit), db.api_mutation(self.con), \
                patch.object(storage, "_stage_removal",
                             side_effect=crash_after_rename):
            storage.apply_plan(self.con, plan["plan_id"], actor="device:test")

        self.assertFalse(original.exists())
        self.assertTrue(artifacts.get(
            self.con, self.artifact["artifact_id"])["available"])
        self.assertEqual(storage.reconcile(self.con, plan["plan_id"]),
                         {"restored": 1, "discarded": 0})
        self.assertEqual(original.read_text(), "retained artifact")

    def test_crash_before_commit_restores_every_evidence_kind(self):
        check_id, observer_log = self._observer_log()
        plan = storage.create_plan(
            self.con, actor="device:test", older_than_days=0)
        originals = [Path(item["path"]) for item in plan["items"]]

        with self.assertRaises(SystemExit):
            with db.api_mutation(self.con):
                storage.apply_plan(
                    self.con, plan["plan_id"], actor="device:test")
                self.assertFalse(any(path.exists() for path in originals))
                raise SystemExit("injected crash")

        self.assertFalse(any(path.exists() for path in originals))
        self.assertEqual(storage.reconcile(self.con, plan["plan_id"]),
                         {"restored": 3, "discarded": 0})
        self.assertTrue(all(path.exists() for path in originals))
        self.assertTrue(artifacts.get(
            self.con, self.artifact["artifact_id"])["available"])
        check = self.con.execute(
            "SELECT log_pruned_at FROM observer_checks WHERE id=?", (check_id,)
        ).fetchone()
        self.assertIsNone(check["log_pruned_at"])
        self.assertTrue(observer_log.exists())

    def test_committed_prune_and_audit_discard_staged_files_on_recovery(self):
        plan = storage.create_plan(
            self.con, actor="device:test", older_than_days=0,
            kinds=["artifacts"])
        original = Path(plan["items"][0]["path"])
        with db.api_mutation(self.con):
            storage.apply_plan(
                self.con, plan["plan_id"], actor="device:test")

        self.assertFalse(original.exists())
        self.assertFalse(artifacts.get(
            self.con, self.artifact["artifact_id"])["available"])
        applied = storage.get_plan(self.con, plan["plan_id"])
        self.assertIsNotNone(applied["applied_at"])
        audit = self.con.execute(
            "SELECT 1 FROM control_events WHERE action='storage.prune_apply' "
            "AND target_id=?", (plan["plan_id"],)).fetchone()
        self.assertIsNotNone(audit)
        self.assertEqual(storage.reconcile(self.con, plan["plan_id"]),
                         {"restored": 0, "discarded": 1})


if __name__ == "__main__":
    unittest.main()
