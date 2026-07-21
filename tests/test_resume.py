from __future__ import annotations

import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from orchestra_cli import cli, db, supervise


class ResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".orchestra").mkdir(parents=True)
        db.connect(self.root).close()
        self.cfg = {"settings": {"default_requester": "orchestrator"}}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, *, status: str = "done", session_ref: str = "session-1",
             parent_run: int | None = None) -> int:
        con = db.connect(self.root)
        try:
            cur = con.execute(
                "INSERT INTO runs(agent,backend,model,title,requested_by,workdir,"
                "parent_run,session_ref,status,started_at,finished_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("codex", "codex", "gpt-5.6-sol", "investigate", "orchestrator",
                 str(self.root), parent_run, session_ref, status, db.now(),
                 db.now() if status in db.RUN_TERMINAL else None),
            )
            con.commit()
            return int(cur.lastrowid)
        finally:
            con.close()

    def _resume(self, run_id: int, *message: str):
        args = Namespace(run_id=run_id, message=list(message), as_="orchestrator", sync=False)
        patches = (
            mock.patch.object(cli.paths, "find_root", return_value=self.root),
            mock.patch.object(cli.config, "load", return_value=self.cfg),
            mock.patch.object(cli, "_spawn_supervisor"),
        )
        with patches[0], patches[1], patches[2] as spawn, mock.patch("builtins.print") as output:
            cli.cmd_reply(args)
        return spawn, output

    def test_resume_creates_new_immutable_attempt_in_same_session(self) -> None:
        original_id = self._run()

        spawn, output = self._resume(original_id, "Continue", "the", "investigation")

        con = db.connect(self.root)
        try:
            original = con.execute("SELECT * FROM runs WHERE id=?", (original_id,)).fetchone()
            resumed = con.execute("SELECT * FROM runs WHERE parent_run=?", (original_id,)).fetchone()
        finally:
            con.close()
        self.assertEqual(original["status"], "done")
        self.assertEqual(original["title"], "investigate")
        self.assertEqual(resumed["session_ref"], "session-1")
        self.assertEqual(resumed["title"], f"continuation of run {original_id}")
        self.assertEqual(resumed["status"], "spawning")
        prompt = Path(resumed["brief_path"]).read_text()
        self.assertIn("Continue the investigation", prompt)
        self.assertIn(f"session continuation from run {original_id}", prompt)
        spawn.assert_called_once_with(self.root, resumed["id"])
        self.assertIn(f"continuing run {original_id}'s session", output.call_args.args[0])

    def test_resuming_earlier_attempt_continues_latest_terminal_tip(self) -> None:
        root_id = self._run()
        tip_id = self._run(parent_run=root_id)

        spawn, output = self._resume(root_id, "One", "more", "pass")

        con = db.connect(self.root)
        try:
            latest = con.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            con.close()
        self.assertEqual(latest["parent_run"], tip_id)
        self.assertEqual(latest["session_ref"], "session-1")
        spawn.assert_called_once_with(self.root, latest["id"])
        rendered = output.call_args.args[0]
        self.assertIn(f"continuing run {tip_id}'s session", rendered)
        self.assertIn(f"requested from run {root_id}", rendered)

    def test_active_descendant_rejects_concurrent_resume(self) -> None:
        root_id = self._run()
        active_id = self._run(status="running", parent_run=root_id)
        args = Namespace(run_id=root_id, message=["Do", "more"], as_="orchestrator", sync=False)

        with mock.patch.object(cli.paths, "find_root", return_value=self.root), \
                mock.patch.object(cli.config, "load", return_value=self.cfg), \
                mock.patch.object(cli, "_spawn_supervisor") as spawn, \
                self.assertRaises(SystemExit) as raised:
            cli.cmd_reply(args)

        self.assertIn(f"already active as run {active_id}", str(raised.exception))
        spawn.assert_not_called()
        con = db.connect(self.root)
        try:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 2)
        finally:
            con.close()

    def test_active_sibling_session_rejects_resume_from_historical_branch(self) -> None:
        root_id = self._run()
        active_id = self._run(status="running", parent_run=root_id)
        historical_tip = self._run(parent_run=root_id)
        args = Namespace(
            run_id=historical_tip, message=["Branch", "again"],
            as_="orchestrator", sync=False,
        )

        with mock.patch.object(cli.paths, "find_root", return_value=self.root), \
                mock.patch.object(cli.config, "load", return_value=self.cfg), \
                mock.patch.object(cli, "_spawn_supervisor") as spawn, \
                self.assertRaises(SystemExit) as raised:
            cli.cmd_reply(args)

        self.assertIn(f"already active as run {active_id}", str(raised.exception))
        spawn.assert_not_called()
        con = db.connect(self.root)
        try:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 3)
        finally:
            con.close()

    def test_resume_and_reply_parse_as_the_same_operation(self) -> None:
        for command in ("resume", "reply"):
            with self.subTest(command=command), \
                    mock.patch.object(cli, "cmd_reply") as handler, \
                    mock.patch("sys.argv", ["orchestra", command, "7", "keep", "going"]):
                cli.main()
                parsed = handler.call_args.args[0]
                self.assertEqual(parsed.run_id, 7)
                self.assertEqual(parsed.message, ["keep", "going"])
