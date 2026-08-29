"""[settings] lifecycle callbacks (a command, not a policy): on_run_finished
and on_run_blocked. Orchestra fires them with the event's identity and
forgets them."""
import tempfile
import time
import unittest
from pathlib import Path

from orchestra import callbacks, supervise


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

    def test_a_blocked_callback_carries_the_kind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "fired"
            cfg = {"settings": {"on_run_blocked":
                   f'echo "$ORCHESTRA_RUN_ID $ORCHESTRA_BLOCK_KIND" > {marker}'}}
            callbacks.fire(cfg, "on_run_blocked",
                           {"ORCHESTRA_RUN_ID": "9",
                            "ORCHESTRA_BLOCK_KIND": "ask"})
            for _ in range(100):
                if marker.exists() and marker.read_text().strip():
                    break
                time.sleep(0.05)
            self.assertEqual(marker.read_text().strip(), "9 ask")

    def test_no_setting_is_a_no_op(self) -> None:
        supervise.notify_run_finished({}, {"id": 1, "status": "failed"})
        supervise.notify_run_finished({"settings": {"on_run_finished": "  "}},
                                      {"id": 1, "status": "failed"})


if __name__ == "__main__":
    unittest.main()
