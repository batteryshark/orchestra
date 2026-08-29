"""[settings] on_run_finished: the completion callback (a command, not a
policy). Orchestra fires it with the run's identity and forgets it."""
import tempfile
import time
import unittest
from pathlib import Path

from orchestra import supervise


class RunFinishedCallbackTests(unittest.TestCase):
    def test_the_command_fires_with_the_run_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "fired"
            cfg = {"settings": {"on_run_finished":
                   f'echo "$ORCHESTRA_RUN_ID $ORCHESTRA_RUN_STATUS" > {marker}'}}
            supervise.notify_run_finished(cfg, {"id": 7, "status": "done"})
            for _ in range(100):
                if marker.exists() and marker.read_text().strip():
                    break
                time.sleep(0.05)
            self.assertEqual(marker.read_text().strip(), "7 done")

    def test_no_setting_is_a_no_op(self) -> None:
        supervise.notify_run_finished({}, {"id": 1, "status": "failed"})
        supervise.notify_run_finished({"settings": {"on_run_finished": "  "}},
                                      {"id": 1, "status": "failed"})


if __name__ == "__main__":
    unittest.main()
