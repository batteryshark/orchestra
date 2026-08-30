import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestra import attention, db, fleet_config, groups, maintenance, runs
from orchestra.contracts import RunRequest


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(
            os.environ, {"ORCHESTRA_HOME": str(self.root / "state")})
        self.env.start()
        con = db.connect()
        self.instance_id = db.instance_id(con)
        con.close()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_backup_is_verified_and_restore_preserves_displaced_state(self):
        archive = self.root / "fleet.tar.gz"
        result = maintenance.backup(archive)
        self.assertEqual(result["instance_id"], self.instance_id)
        self.assertEqual(archive.stat().st_mode & 0o077, 0)
        self.assertTrue(maintenance.inspect_backup(archive)["valid"])

        con = db.connect()
        groups.create(con, "Added later", actor="test")
        con.close()
        dry_run = maintenance.restore(archive)
        self.assertFalse(dry_run["applied"])
        con = db.connect()
        self.assertIsNotNone(groups.find(con, "added-later"))
        con.close()

        restored = maintenance.restore(archive, apply=True)
        self.assertTrue(restored["applied"])
        self.assertTrue(Path(restored["previous_state"]).is_dir())
        con = db.connect()
        self.assertEqual(db.instance_id(con), self.instance_id)
        self.assertIsNone(groups.find(con, "added-later"))
        con.close()

    def test_backup_refuses_a_live_daemon_marker(self):
        con = db.connect()
        db.meta_set(con, "daemon_pid", str(os.getpid()))
        db.meta_set(con, "daemon_pid_identity", "")
        con.commit()
        con.close()
        with self.assertRaisesRegex(maintenance.MaintenanceError, "stop.*daemon"):
            maintenance.backup(self.root / "blocked.tar.gz")

    def test_restore_rebases_durable_feeds_above_displaced_client_cursors(self):
        con = db.connect()
        fleet_config.create_runtime(
            con, "Exec", "exec", slug="exec", command=["/usr/bin/true"])
        fleet_config.create_profile(con, "Fast", "exec", slug="fast", tier=1)
        run, _ = runs.submit(con, RunRequest.from_mapping({
            "request_id": "restore-feed-run", "profile": "fast",
            "context": "Retained result", "cwd": str(self.root),
        }))
        con.execute(
            "UPDATE runs SET status='completed',finished_at=? WHERE id=?",
            (db.now(), run["id"]),
        )
        con.commit()
        request, _ = attention.open_request(
            con, kind="alert", title="Retained alert", body="Review this",
            created_by="test", run_id=run["id"], correlation_id="restore-alert",
        )
        con.close()

        archive = self.root / "feeds.tar.gz"
        maintenance.backup(archive)
        con = db.connect()
        db.meta_set(con, "board_revision", "500")
        con.commit()
        con.close()

        result = maintenance.restore(archive, apply=True)
        con = db.connect()
        restored_run = con.execute(
            "SELECT revision FROM runs WHERE id=?", (run["id"],)
        ).fetchone()[0]
        restored_attention = con.execute(
            "SELECT revision FROM attention_requests WHERE id=?", (request["id"],)
        ).fetchone()[0]
        self.assertGreater(restored_run, 500)
        self.assertGreater(restored_attention, restored_run)
        self.assertGreater(result["revision"], restored_attention)
        self.assertEqual(db.board_revision(con), result["revision"])
        con.close()

    def test_restore_rejects_path_traversal_before_writing_state(self):
        archive = self.root / "hostile.tar"
        payload = b"bad"
        with tarfile.open(archive, "w") as handle:
            member = tarfile.TarInfo("orchestra-v2/../escaped")
            member.size = len(payload)
            handle.addfile(member, io.BytesIO(payload))
        with self.assertRaisesRegex(maintenance.MaintenanceError, "unsafe"):
            maintenance.restore(archive)
        self.assertFalse((self.root / "escaped").exists())

    def test_restore_rejects_a_tampered_inventory(self):
        archive = self.root / "bad-manifest.tar"
        manifest = json.dumps({"format": maintenance.FORMAT,
                               "schema_version": db.SCHEMA_VERSION,
                               "files": []}).encode()
        with tarfile.open(archive, "w") as handle:
            directory = tarfile.TarInfo("orchestra-v2")
            directory.type = tarfile.DIRTYPE
            handle.addfile(directory)
            member = tarfile.TarInfo("orchestra-v2/manifest.json")
            member.size = len(manifest)
            handle.addfile(member, io.BytesIO(manifest))
        with self.assertRaises(maintenance.MaintenanceError):
            maintenance.restore(archive)


if __name__ == "__main__":
    unittest.main()
