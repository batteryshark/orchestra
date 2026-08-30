import json
import subprocess
import unittest
from datetime import datetime, timedelta, timezone

from orchestra import runway


SOURCE = {
    "source_id": "src-1",
    "provider": "custom-provider",
    "account": "personal",
    "lane": "quota",
    "adapter": "command",
    "command_json": json.dumps(["quota-reader"]),
    "config_json": json.dumps({"fresh_seconds": 600}),
}


class SourceTests(unittest.TestCase):
    def test_custom_adapter_is_json_argv_without_a_shell(self):
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"remaining": 25, "unit": "percent"}), "")

        result = runway.poll_source(SOURCE, runner=run)
        self.assertEqual(calls[0][0], ["quota-reader"])
        self.assertNotIn("shell", calls[0][1])
        self.assertEqual(result["remaining"], 25)
        self.assertTrue(result["definitive"])

    def test_only_fresh_definitive_zero_holds(self):
        now = datetime.now(timezone.utc)
        reading = {
            "remaining": 0,
            "definitive": True,
            "fresh_until": (now + timedelta(minutes=5)).isoformat(),
            "resets_at": (now + timedelta(hours=1)).isoformat(),
        }
        self.assertIn("exhausted", runway.source_hold(reading, now=now))
        self.assertIsNone(runway.source_hold({**reading, "definitive": False}, now=now))
        self.assertIsNone(runway.source_hold({
            **reading, "fresh_until": (now - timedelta(seconds=1)).isoformat()}, now=now))

    def test_poll_does_not_make_an_old_zero_fresh_again(self):
        old = datetime.now(timezone.utc) - timedelta(hours=2)

        def run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, json.dumps({
                "remaining": 0, "unit": "percent", "as_of": old.isoformat(),
                "resets_at": (old + timedelta(hours=4)).isoformat(),
            }), "")

        result = runway.poll_source(SOURCE, runner=run)
        self.assertTrue(result["definitive"])
        self.assertIsNone(runway.source_hold(result))

    def test_expired_reset_uses_the_callers_clock(self):
        now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        reading = {
            "remaining": 0, "definitive": True,
            "fresh_until": (now + timedelta(minutes=5)).isoformat(),
            "resets_at": (now - timedelta(seconds=1)).isoformat(),
        }
        self.assertIsNone(runway.source_hold(reading, now=now))


if __name__ == "__main__":
    unittest.main()
