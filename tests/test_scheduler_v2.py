import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from orchestra import daemon, db, fleet_config, runs, scheduler
from orchestra.contracts import RunRequest


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"ORCHESTRA_HOME": self.temp.name})
        self.env.start()
        self.con = db.connect(":memory:")
        fleet_config.create_runtime(self.con, "Exec", "exec", slug="exec",
                                    command=["agent"])
        fleet_config.create_profile(self.con, "P", "exec", slug="p", tier=2)

    def tearDown(self):
        self.con.close()
        self.env.stop()
        self.temp.cleanup()

    def submit(self, name, **extra):
        body = {"request_id": name, "profile": "p",
                "context": name, "cwd": self.temp.name}
        body.update(extra)
        return runs.submit(self.con, RunRequest.from_mapping(body))[0]

    def test_fifo_capacity_holds_later_runs_visibly(self):
        fleet_config.set_fleet_setting(self.con, "max_active_runs", 1)
        first, second = self.submit("first"), self.submit("second")
        result = scheduler.admit(self.con)
        self.assertEqual(result["admitted"], [first["id"]])
        held = runs.find(self.con, second["id"])
        self.assertIn("global capacity", held["hold_reason"])

    def test_failed_success_dependency_skips_without_using_capacity(self):
        dependency = self.submit("dependency")
        waiting = self.submit(
            "waiting", after=[{"run_id": dependency["id"], "condition": "success"}])
        self.con.execute("UPDATE runs SET status='failed' WHERE id=?",
                         (dependency["id"],))
        self.con.commit()
        result = scheduler.admit(self.con)
        self.assertIn(waiting["id"], result["skipped"])
        self.assertEqual(runs.find(self.con, waiting["id"])["status"], "skipped")

    def test_fresh_definitive_zero_holds_but_unknown_does_not(self):
        source = fleet_config.create_runway_source(
            self.con, "Quota", "provider", adapter="command", slug="quota",
            command=["quota"])
        fleet_config.update_profile(self.con, "p", {"runway_source": "quota"})
        now = datetime.now(timezone.utc)
        self.con.execute(
            "INSERT INTO runway_readings(source_id,remaining,fresh_until,definitive,"
            "resets_at,polled_at) VALUES(?,?,?,?,?,?)",
            (source["source_id"], 0, (now + timedelta(minutes=5)).isoformat(), 1,
             (now + timedelta(hours=1)).isoformat(), now.isoformat()))
        self.con.commit()
        run = self.submit("quota-blocked")
        scheduler.admit(self.con)
        self.assertIn("runway exhausted", runs.find(self.con, run["id"])["hold_reason"])
        self.con.execute(
            "INSERT INTO runway_readings(source_id,remaining,fresh_until,definitive,"
            "reason,polled_at) VALUES(?,?,?,?,?,?)",
            (source["source_id"], None, None, 0, "offline", now.isoformat()))
        self.con.commit()
        result = scheduler.admit(self.con)
        self.assertIn(run["id"], result["admitted"])

    def test_queued_run_keeps_its_admitted_runway_source(self):
        first = fleet_config.create_runway_source(
            self.con, "First quota", "provider", adapter="command",
            slug="first", command=["quota"])
        second = fleet_config.create_runway_source(
            self.con, "Second quota", "provider", account="other",
            adapter="command", slug="second", command=["quota"])
        fleet_config.update_profile(
            self.con, "p", {"runway_source": first["source_id"]})
        run = self.submit("frozen-runway")
        fleet_config.update_profile(
            self.con, "p", {"runway_source": second["source_id"]})
        now = datetime.now(timezone.utc)
        self.con.execute(
            "INSERT INTO runway_readings(source_id,remaining,fresh_until,definitive,"
            "resets_at,polled_at) VALUES(?,?,?,?,?,?)",
            (first["source_id"], 0,
             (now + timedelta(minutes=5)).isoformat(), 1,
             (now + timedelta(hours=1)).isoformat(), now.isoformat()))
        self.con.commit()

        result = scheduler.admit(self.con)

        self.assertEqual(run["runway_source_id"], first["source_id"])
        self.assertEqual(result["admitted"], [])
        self.assertIn("runway exhausted", runs.find(
            self.con, run["id"])["hold_reason"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "UPDATE runs SET runway_source_id=? WHERE id=?",
                (second["source_id"], run["id"]))
        self.con.rollback()

    def test_waiting_run_keeps_its_admitted_runway_source(self):
        first = fleet_config.create_runway_source(
            self.con, "Wait quota", "provider", adapter="command",
            slug="wait", command=["quota"])
        second = fleet_config.create_runway_source(
            self.con, "New quota", "provider", account="new",
            adapter="command", slug="new", command=["quota"])
        fleet_config.update_profile(
            self.con, "p", {"runway_source": first["source_id"]})
        run = self.submit("waiting-frozen-runway")
        self.con.execute(
            "UPDATE runs SET status='waiting',waiting_kind='input' WHERE id=?",
            (run["id"],))
        fleet_config.update_profile(
            self.con, "p", {"runway_source": second["source_id"]})
        now = datetime.now(timezone.utc)
        self.con.execute(
            "INSERT INTO runway_readings(source_id,remaining,fresh_until,definitive,"
            "resets_at,polled_at) VALUES(?,?,?,?,?,?)",
            (first["source_id"], 0,
             (now + timedelta(minutes=5)).isoformat(), 1,
             (now + timedelta(hours=1)).isoformat(), now.isoformat()))
        self.con.commit()

        resumed = daemon._resume_waiters(self.con, lambda root, run_id: None)

        self.assertEqual(resumed, [])
        self.assertIn("runway exhausted", runs.find(
            self.con, run["id"])["hold_reason"])


if __name__ == "__main__":
    unittest.main()
