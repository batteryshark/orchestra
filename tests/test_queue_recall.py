from __future__ import annotations

import contextlib
import io
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestra_cli import cli, db, supervise


class QueueRecallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".orchestra").mkdir()
        con = db.connect(self.root)
        con.execute(
            "INSERT INTO runs(agent, backend, requested_by, workdir, status, started_at) "
            "VALUES('codex', 'codex', 'orchestrator', ?, 'running', ?)",
            (str(self.root), db.now()),
        )
        con.commit()
        con.close()

        self.original_find_root = cli.paths.find_root
        self.original_config_load = cli.config.load
        cli.paths.find_root = lambda: self.root  # type: ignore[assignment]
        cli.config.load = lambda _root: {  # type: ignore[assignment]
            "settings": {"default_requester": "orchestrator"}
        }

    def tearDown(self) -> None:
        cli.paths.find_root = self.original_find_root  # type: ignore[assignment]
        cli.config.load = self.original_config_load  # type: ignore[assignment]
        self.tmp.cleanup()

    def _run_main(self, argv: list[str]) -> tuple[int, str, str]:
        original_argv = sys.argv
        stdout, stderr = io.StringIO(), io.StringIO()
        code = 0
        sys.argv = ["orchestra", *argv]
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                cli.main()
        except SystemExit as exc:
            code = int(exc.code) if isinstance(exc.code, int) else 1
            if not isinstance(exc.code, int) and exc.code:
                print(exc.code, file=stderr)
        finally:
            sys.argv = original_argv
        return code, stdout.getvalue(), stderr.getvalue()

    def _queue(self, *, sender: str = "orchestrator") -> int:
        code, stdout, stderr = self._run_main(
            ["queue", "1", "run the obsolete follow-up", "--as", sender]
        )
        self.assertEqual((code, stderr), (0, ""))
        match = re.search(r"queued message (\d+)", stdout)
        self.assertIsNotNone(match)
        return int(match.group(1))

    def _message(self, message_id: int) -> dict:
        con = db.connect(self.root)
        try:
            return dict(con.execute(
                "SELECT * FROM messages WHERE id=?", (message_id,)
            ).fetchone())
        finally:
            con.close()

    def test_queue_prints_recallable_message_id(self) -> None:
        message_id = self._queue()

        message = self._message(message_id)
        self.assertEqual(message["kind"], "queued")
        self.assertEqual(message["sender"], "orchestrator")
        self.assertIsNone(message["recalled_at"])

    def test_finished_run_dispatches_immediately_without_recallable_message(self) -> None:
        con = db.connect(self.root)
        con.execute(
            "UPDATE runs SET status='done', session_ref='session-1' WHERE id=1"
        )
        con.commit()
        con.close()

        with mock.patch.object(supervise, "spawn_supervisor") as spawn:
            code, stdout, stderr = self._run_main(
                ["queue", "1", "continue immediately", "--as", "orchestrator"]
            )

        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("follow-up dispatched now as run 2", stdout)
        spawn.assert_called_once_with(self.root, 2)
        con = db.connect(self.root)
        try:
            self.assertEqual(con.execute(
                "SELECT COUNT(*) FROM messages WHERE kind='queued'"
            ).fetchone()[0], 0)
            followup = con.execute("SELECT * FROM runs WHERE id=2").fetchone()
            self.assertEqual(followup["parent_run"], 1)
            self.assertEqual(followup["session_ref"], "session-1")
        finally:
            con.close()

    def test_sender_can_atomically_recall_pending_queue(self) -> None:
        message_id = self._queue()

        code, stdout, stderr = self._run_main(
            ["recall", str(message_id), "--as", "orchestrator"]
        )

        self.assertEqual((code, stderr), (0, ""))
        self.assertIn(f"recalled queued message {message_id} for run 1", stdout)
        message = self._message(message_id)
        self.assertEqual(message["recalled_by"], "orchestrator")
        self.assertIsNotNone(message["recalled_at"])
        self.assertEqual(message["read_at"], message["recalled_at"])
        con = db.connect(self.root)
        try:
            self.assertEqual(supervise._pending_queued_followups(con, 1), [])
        finally:
            con.close()

    def test_other_identity_cannot_recall_queue(self) -> None:
        message_id = self._queue(sender="codex")

        code, _, stderr = self._run_main(
            ["recall", str(message_id), "--as", "orchestrator"]
        )

        self.assertEqual(code, 1)
        self.assertIn("belongs to codex", stderr)
        self.assertIsNone(self._message(message_id)["recalled_at"])

    def test_delivered_queue_cannot_be_recalled(self) -> None:
        message_id = self._queue()
        con = db.connect(self.root)
        con.execute("UPDATE messages SET read_at=? WHERE id=?", (db.now(), message_id))
        con.commit()
        con.close()

        code, _, stderr = self._run_main(["recall", str(message_id)])

        self.assertEqual(code, 1)
        self.assertIn("already delivered", stderr)
        self.assertIsNone(self._message(message_id)["recalled_at"])

    def test_repeated_recall_reports_existing_state(self) -> None:
        message_id = self._queue()
        self.assertEqual(self._run_main(["recall", str(message_id)])[0], 0)

        code, _, stderr = self._run_main(["recall", str(message_id)])

        self.assertEqual(code, 1)
        self.assertIn("already recalled", stderr)


if __name__ == "__main__":
    unittest.main()
