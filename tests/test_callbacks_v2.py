import sys
import tempfile
import time
import unittest
from pathlib import Path

from orchestra import callbacks, db


class CallbackAuditTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "orchestra.sqlite3"
        self.con = db.connect(self.database)

    def tearDown(self):
        self.con.close()
        self.directory.cleanup()

    def test_successful_callback_records_admission_and_exit(self):
        admitted = callbacks.emit(
            [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
            "run.terminal", {"run_id": 17, "status": "completed"},
            audit_db=self.con, timeout=2)
        self.assertTrue(admitted)
        deadline = time.monotonic() + 3
        outcomes = []
        while time.monotonic() < deadline:
            outcomes = [row[0] for row in self.con.execute(
                "SELECT outcome FROM control_events "
                "WHERE action='callback.run.terminal' ORDER BY id")]
            if outcomes == ["started", "delivered"]:
                break
            time.sleep(0.02)
        self.assertEqual(outcomes, ["started", "delivered"])

    def test_no_command_is_not_a_delivery_attempt(self):
        self.assertFalse(callbacks.emit(
            [], "attention.opened", {"attention_id": 2}, audit_db=self.con))
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM control_events").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
