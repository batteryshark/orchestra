from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestra_cli import cancel, config, db, dependencies


def _project() -> tuple[tempfile.TemporaryDirectory, Path]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / ".orchestra").mkdir()
    db.connect(root).close()
    return tmp, root


def _run(root: Path, *, status: str, title: str) -> int:
    con = db.connect(root)
    try:
        cur = con.execute(
            "INSERT INTO runs(agent,backend,title,requested_by,workdir,status,started_at) "
            "VALUES('minimax','opencode',?,'codex',?,?,?)",
            (title, str(root), status, db.now()),
        )
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def _defer(root: Path, prerequisite: int, *, use_worktree: bool = False) -> int:
    run_id = _run(root, status="pending", title="consumer")
    con = db.connect(root)
    try:
        dependencies.enqueue(
            con,
            run_id,
            [prerequisite],
            mission="consume producer output",
            context=None,
            use_worktree=use_worktree,
        )
        con.commit()
    finally:
        con.close()
    return run_id


class DeferredDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp, self.root = _project()
        self.cfg = config.load(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_last_successful_prerequisite_fires_dispatch_exactly_once(self) -> None:
        prerequisite = _run(self.root, status="running", title="producer")
        consumer = _defer(self.root, prerequisite)
        launched: list[int] = []
        con = db.connect(self.root)
        try:
            self.assertEqual(
                dependencies.process_ready(
                    con, self.root, self.cfg,
                    lambda _root, run_id: launched.append(run_id),
                ),
                [],
            )
            self.assertEqual(dependencies.pending_on(con, consumer), [prerequisite])
            con.execute(
                "UPDATE runs SET started_at='2000-01-01T00:00:00Z' WHERE id=?",
                (consumer,),
            )
            con.execute("UPDATE runs SET status='done' WHERE id=?", (prerequisite,))
            con.commit()

            dependencies.process_ready(
                con, self.root, self.cfg,
                lambda _root, run_id: launched.append(run_id),
            )
            dependencies.process_ready(
                con, self.root, self.cfg,
                lambda _root, run_id: launched.append(run_id),
            )
            run = con.execute("SELECT * FROM runs WHERE id=?", (consumer,)).fetchone()
            deferred = con.execute(
                "SELECT status FROM deferred_dispatches WHERE run_id=?", (consumer,)
            ).fetchone()
        finally:
            con.close()
        self.assertEqual(launched, [consumer])
        self.assertEqual(run["status"], "spawning")
        self.assertNotEqual(run["started_at"], "2000-01-01T00:00:00Z")
        self.assertTrue(Path(run["brief_path"]).is_file())
        self.assertTrue(Path(run["log_path"]).is_file())
        self.assertEqual(deferred["status"], "fired")

    def test_worktree_setup_waits_until_dependency_is_ready(self) -> None:
        prerequisite = _run(self.root, status="running", title="producer")
        consumer = _defer(self.root, prerequisite, use_worktree=True)
        isolated = self.root / "isolated"
        isolated.mkdir()
        con = db.connect(self.root)
        try:
            with mock.patch.object(
                dependencies.worktree,
                "create",
                return_value=(isolated, "orchestra/run-consumer"),
            ) as create:
                dependencies.process_ready(con, self.root, self.cfg, lambda *_: None)
                create.assert_not_called()
                con.execute("UPDATE runs SET status='done' WHERE id=?", (prerequisite,))
                con.commit()
                dependencies.process_ready(con, self.root, self.cfg, lambda *_: None)
                create.assert_called_once_with(self.root, consumer)
            run = con.execute("SELECT workdir,branch FROM runs WHERE id=?", (consumer,)).fetchone()
        finally:
            con.close()
        self.assertEqual(run["workdir"], str(isolated))
        self.assertEqual(run["branch"], "orchestra/run-consumer")

    def test_failed_prerequisite_declines_dispatch_without_launching(self) -> None:
        prerequisite = _run(self.root, status="failed", title="producer")
        consumer = _defer(self.root, prerequisite)
        con = db.connect(self.root)
        try:
            result = dependencies.process_ready(
                con,
                self.root,
                self.cfg,
                lambda *_: self.fail("declined dispatch must not launch"),
            )
            run = con.execute("SELECT status,summary FROM runs WHERE id=?", (consumer,)).fetchone()
            deferred = con.execute(
                "SELECT status FROM deferred_dispatches WHERE run_id=?", (consumer,)
            ).fetchone()
            message = con.execute(
                "SELECT body FROM messages WHERE run_id=?", (consumer,)
            ).fetchone()
        finally:
            con.close()
        self.assertEqual(result, [])
        self.assertEqual(run["status"], "failed")
        self.assertIn(f"{prerequisite} (failed)", run["summary"])
        self.assertEqual(deferred["status"], "declined")
        self.assertIn("Not launched", message["body"])

    def test_cancelling_prerequisite_transitively_declines_pending_consumer(self) -> None:
        prerequisite = _run(self.root, status="running", title="producer")
        consumer = _defer(self.root, prerequisite)
        con = db.connect(self.root)
        try:
            result = cancel.stop_run(con, prerequisite)
            consumer_row = con.execute(
                "SELECT status,summary FROM runs WHERE id=?", (consumer,)
            ).fetchone()
        finally:
            con.close()
        self.assertTrue(result.stopped)
        self.assertEqual(consumer_row["status"], "failed")
        self.assertIn(f"{prerequisite} (killed)", consumer_row["summary"])


if __name__ == "__main__":
    unittest.main()
