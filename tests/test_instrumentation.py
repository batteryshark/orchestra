import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestra import db, instrumentation


class InstrumentationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(
            os.environ, {"ORCHESTRA_HOME": str(Path(self.tmp.name) / "home")})
        self.env.start()
        self.con = db.connect()

    def tearDown(self):
        self.con.close()
        self.env.stop()
        self.tmp.cleanup()

    def make_run(self, profile="p", model="m", status="done", landing="ok"):
        return int(self.con.execute(
            "INSERT INTO runs(profile, backend, model, requested_by, workdir, "
            "status, started_at, finished_at, tokens_total, landing_status) "
            "VALUES(?, 'codex', ?, 'human', '/p', ?, "
            "'2026-08-25T10:00:00Z', '2026-08-25T10:10:00Z', 100, ?)",
            (profile, model, status, landing)).lastrowid)

    def event(self, run_id, seq, kind, name, payload, ts):
        self.con.execute(
            "INSERT INTO events(run_id, seq, kind, name, payload, payload_len, "
            "ts, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (run_id, seq, kind, name, json.dumps(payload), len(json.dumps(payload)),
             ts, ts))

    def test_report_classifies_times_and_flags_repeats(self):
        run_id = self.make_run()
        rows = [
            ("tool_call", "command_execution", {"command": "pytest tests/test_x.py"}, "10:00:01Z"),
            ("tool_result", "command_execution", {"exit_code": 1, "error": "failed"}, "10:00:04Z"),
            ("tool_call", "command_execution", {"command": "pytest tests/test_x.py"}, "10:00:05Z"),
            ("tool_result", "command_execution", {"exit_code": 1}, "10:00:07Z"),
            ("tool_call", "command_execution", {"command": "sed -n 1,20p app.py"}, "10:00:08Z"),
            ("tool_result", "command_execution", {"exit_code": 0}, "10:00:09Z"),
            ("tool_call", "command_execution", {"command": "sed -n 1,20p app.py"}, "10:00:10Z"),
            ("tool_result", "command_execution", {"exit_code": 0}, "10:00:11Z"),
            ("tool_call", "command_execution", {"command": "rg test orchestra"}, "10:00:12Z"),
            ("tool_result", "command_execution",
             {"exit_code": 0, "output": "0 failures"}, "10:00:13Z"),
            ("assistant_text", None, "done", "10:00:14Z"),
            ("lifecycle", "context_compacted", {}, "10:00:15Z"),
        ]
        for seq, (kind, name, payload, clock) in enumerate(rows, 1):
            self.event(run_id, seq, kind, name, payload, "2026-08-25T" + clock)
        report = instrumentation.report(self.con)
        self.assertEqual(report["tools"]["test"]["seconds"], 5.0)
        self.assertEqual(report["tools"]["test"]["errors"], 2)
        self.assertEqual(report["tools"]["read"]["calls"], 2)
        self.assertEqual(report["tools"]["search"]["errors"], 0)
        self.assertEqual((report["turns"], report["compactions"]), (1, 1))
        self.assertEqual(report["classification_rate"], 1.0)
        self.assertEqual([g["kind"] for g in report["gap_candidates"]],
                         ["failing command", "re-read file"])

    def test_profile_model_rates_use_the_selected_window(self):
        self.make_run("good", "a", "done", "ok")
        self.make_run("bad", "b", "failed", None)
        report = instrumentation.report(self.con, limit=1)
        self.assertEqual(report["runs_count"], 1)
        self.assertEqual(report["failure_rate"], 1.0)
        self.assertEqual(report["comparisons"][0]["profile"], "bad")
        self.assertEqual(report["comparisons"][0]["landings_per_hour"], 0.0)
        self.assertIn("unclassified", report["tools"])


if __name__ == "__main__":
    unittest.main()
