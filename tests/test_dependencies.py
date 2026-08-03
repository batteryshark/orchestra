from __future__ import annotations

import sqlite3
import socket
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from orchestra_cli import cancel, capabilities, config, db, dependencies, supervise


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


def _defer(
    root: Path,
    prerequisite: int,
    *,
    use_worktree: bool = False,
    dependency_kind: str = dependencies.REQUIRES_SUCCESS,
) -> int:
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
            dependency_kind=dependency_kind,
            writes_tree=False,
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
            ) as create, mock.patch.object(
                dependencies.worktree, "head", return_value="abc123"
            ):
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

    def test_deferred_launch_uses_persisted_snapshot_and_requirements(self) -> None:
        prerequisite = _run(self.root, status="done", title="producer")
        consumer = _run(self.root, status="pending", title="consumer")
        snapshot = "**W-0141** — context captured when queued"
        capabilities.record_observation(
            self.root,
            host_identity=socket.gethostname(),
            backend="opencode",
            profile="minimax",
            sandbox_mode="orchestra-unrestricted",
            capability="cocoa-window",
            state="supported",
            evidence="launch probe passed",
        )
        con = db.connect(self.root)
        try:
            con.execute("UPDATE runs SET work_item='W-0141' WHERE id=?", (consumer,))
            dependencies.enqueue(
                con,
                consumer,
                [prerequisite],
                mission="exercise the window",
                context=None,
                use_worktree=False,
                work_snapshot=snapshot,
                required_capabilities=["cocoa-window"],
                writes_tree=False,
            )
            con.commit()
            with mock.patch.object(
                dependencies.brief, "work_snapshot",
                side_effect=AssertionError("deferred launch must use its persisted snapshot"),
            ):
                fired = dependencies.process_ready(con, self.root, self.cfg, lambda *_: None)
            row = con.execute("SELECT brief_path FROM runs WHERE id=?", (consumer,)).fetchone()
            deferred = con.execute(
                "SELECT work_snapshot, required_capabilities_json FROM deferred_dispatches "
                "WHERE run_id=?",
                (consumer,),
            ).fetchone()
        finally:
            con.close()
        self.assertEqual(fired, [{"run_id": consumer, "status": "fired"}])
        self.assertEqual(deferred["work_snapshot"], snapshot)
        self.assertEqual(deferred["required_capabilities_json"], '["cocoa-window"]')
        text = Path(row["brief_path"]).read_text()
        self.assertIn(snapshot, text)
        self.assertIn("`cocoa-window`", text)

    def test_deferred_launch_rejects_changed_or_expired_capability_evidence(self) -> None:
        changed_prerequisite = _run(self.root, status="done", title="changed producer")
        expired_prerequisite = _run(self.root, status="done", title="expired producer")
        changed = _run(self.root, status="pending", title="changed consumer")
        expired = _run(self.root, status="pending", title="expired consumer")
        lane = {
            "host_identity": socket.gethostname(),
            "backend": "opencode",
            "profile": "minimax",
            "sandbox_mode": "orchestra-unrestricted",
        }
        observed = datetime.now(UTC)
        capabilities.record_observation(
            self.root, **lane, capability="cocoa-window", state="supported",
            evidence="older pass", observed_at=observed - timedelta(minutes=1), ttl=None,
        )
        capabilities.record_observation(
            self.root, **lane, capability="cocoa-window", state="unsupported",
            evidence="policy changed", observed_at=observed, ttl=None,
        )
        capabilities.record_observation(
            self.root, **lane, capability="core-audio", state="supported",
            evidence="old pass", observed_at=observed - timedelta(days=8),
        )
        con = db.connect(self.root)
        try:
            for run_id, prerequisite, capability in (
                (changed, changed_prerequisite, "cocoa-window"),
                (expired, expired_prerequisite, "core-audio"),
            ):
                dependencies.enqueue(
                    con, run_id, [prerequisite], mission="verify environment",
                    context=None, use_worktree=False,
                    required_capabilities=[capability],
                    writes_tree=False,
                )
            con.commit()
            results = dependencies.process_ready(
                con, self.root, self.cfg,
                lambda *_: self.fail("stale capability evidence must not launch a worker"),
            )
            rows = {
                int(row["run_id"]): row
                for row in con.execute(
                    "SELECT deferred.run_id, deferred.status, deferred.error, run.brief_path "
                    "FROM deferred_dispatches deferred JOIN runs run ON run.id=deferred.run_id "
                    "WHERE deferred.run_id IN (?,?)",
                    (changed, expired),
                )
            }
        finally:
            con.close()
        self.assertEqual({entry["status"] for entry in results}, {"failed"})
        self.assertIn("unsupported=cocoa-window", rows[changed]["error"])
        self.assertIn("expired=core-audio", rows[expired]["error"])
        self.assertIsNone(rows[changed]["brief_path"])
        self.assertIsNone(rows[expired]["brief_path"])

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
            edge = con.execute(
                "SELECT kind FROM dispatch_dependencies WHERE run_id=?", (consumer,)
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
        self.assertEqual(edge["kind"], dependencies.REQUIRES_SUCCESS)
        self.assertIn("Not launched", message["body"])

    def test_required_unverified_done_prerequisite_declines_dispatch(self) -> None:
        prerequisite = _run(self.root, status="done", title="unverified producer")
        consumer = _defer(self.root, prerequisite)
        con = db.connect(self.root)
        try:
            con.execute(
                "UPDATE runs SET verification_required=1, verification_status='unverified' "
                "WHERE id=?",
                (prerequisite,),
            )
            con.commit()
            dependencies.process_ready(
                con,
                self.root,
                self.cfg,
                lambda *_: self.fail("unverified prerequisite must not launch a consumer"),
            )
            consumer_row = con.execute(
                "SELECT status, summary FROM runs WHERE id=?", (consumer,)
            ).fetchone()
        finally:
            con.close()
        self.assertEqual(consumer_row["status"], "failed")
        self.assertIn(f"{prerequisite} (done/unverified)", consumer_row["summary"])

    def test_verified_required_prerequisite_releases_dispatch(self) -> None:
        prerequisite = _run(self.root, status="done", title="verified producer")
        consumer = _defer(self.root, prerequisite)
        launched: list[int] = []
        con = db.connect(self.root)
        try:
            con.execute(
                "UPDATE runs SET verification_required=1, verification_status='verified' "
                "WHERE id=?",
                (prerequisite,),
            )
            con.commit()
            dependencies.process_ready(
                con, self.root, self.cfg, lambda _root, run_id: launched.append(run_id)
            )
        finally:
            con.close()
        self.assertEqual(launched, [consumer])

    def test_cancellation_preview_treats_unverified_done_as_unsuccessful(self) -> None:
        unverified = _run(self.root, status="done", title="unverified producer")
        active = _run(self.root, status="running", title="active producer")
        consumer = _defer(self.root, unverified)
        con = db.connect(self.root)
        try:
            con.execute(
                "UPDATE runs SET verification_required=1, verification_status='unverified' "
                "WHERE id=?",
                (unverified,),
            )
            con.execute(
                "INSERT INTO dispatch_dependencies(run_id, depends_on_run, kind) "
                "VALUES(?,?,?)",
                (consumer, active, dependencies.REQUIRES_SUCCESS),
            )
            con.commit()
            impact = dependencies.cancellation_impact(con, active)
        finally:
            con.close()
        self.assertEqual(
            impact,
            {"declined_run_ids": [consumer], "held_run_ids": [], "unblocked_run_ids": []},
        )

    def test_enqueue_rejects_unknown_kind_before_writing_any_rows(self) -> None:
        prerequisite = _run(self.root, status="running", title="producer")
        consumer = _run(self.root, status="pending", title="consumer")
        con = db.connect(self.root)
        try:
            with self.assertRaisesRegex(ValueError, "unknown dependency kind"):
                dependencies.enqueue(
                    con,
                    consumer,
                    [prerequisite],
                    mission="consume producer output",
                    context=None,
                    use_worktree=False,
                    dependency_kind="anything_goes",
                )
            edge_count = con.execute(
                "SELECT COUNT(*) FROM dispatch_dependencies WHERE run_id=?", (consumer,)
            ).fetchone()[0]
            deferred_count = con.execute(
                "SELECT COUNT(*) FROM deferred_dispatches WHERE run_id=?", (consumer,)
            ).fetchone()[0]
        finally:
            con.close()
        self.assertEqual(edge_count, 0)
        self.assertEqual(deferred_count, 0)

    def test_wait_for_releases_after_any_unsuccessful_terminal_status(self) -> None:
        launched: list[int] = []
        con = db.connect(self.root)
        try:
            for status in ("failed", "timeout", "killed"):
                prerequisite = _run(self.root, status=status, title=f"producer {status}")
                consumer = _defer(
                    self.root,
                    prerequisite,
                    dependency_kind=dependencies.WAIT_FOR,
                )
                dependencies.process_ready(
                    con,
                    self.root,
                    self.cfg,
                    lambda _root, run_id: launched.append(run_id),
                )
                row = con.execute(
                    "SELECT status FROM runs WHERE id=?", (consumer,)
                ).fetchone()
                self.assertEqual(row["status"], "spawning")
        finally:
            con.close()
        self.assertEqual(len(launched), 3)

    def test_mixed_edges_wait_for_terminal_but_requires_success_to_finish(self) -> None:
        successful = _run(self.root, status="done", title="successful producer")
        waiting = _run(self.root, status="running", title="sequencing producer")
        consumer = _defer(self.root, successful)
        con = db.connect(self.root)
        try:
            con.execute(
                "INSERT INTO dispatch_dependencies(run_id, depends_on_run, kind) "
                "VALUES(?,?,?)",
                (consumer, waiting, dependencies.WAIT_FOR),
            )
            self.assertEqual(dependencies.pending_on(con, consumer), [waiting])
            con.execute("UPDATE runs SET status='failed' WHERE id=?", (waiting,))
            con.commit()
            self.assertEqual(dependencies.pending_on(con, consumer), [])
            launched: list[int] = []
            dependencies.process_ready(
                con,
                self.root,
                self.cfg,
                lambda _root, run_id: launched.append(run_id),
            )
        finally:
            con.close()
        self.assertEqual(launched, [consumer])

    def test_mixed_graph_holds_success_chain_after_cancelled_root(self) -> None:
        root = _run(self.root, status="killed", title="root")
        first = _defer(self.root, root)
        second = _defer(self.root, first)
        released = _defer(
            self.root,
            second,
            dependency_kind=dependencies.WAIT_FOR,
        )
        launched: list[int] = []
        con = db.connect(self.root)
        try:
            dependencies.process_ready(
                con,
                self.root,
                self.cfg,
                lambda _root, run_id: launched.append(run_id),
            )
            statuses = {
                int(row["id"]): row["status"]
                for row in con.execute(
                    "SELECT id, status FROM runs WHERE id IN (?,?,?)",
                    (first, second, released),
                )
            }
        finally:
            con.close()
        self.assertEqual(statuses[first], "pending")
        self.assertEqual(statuses[second], "pending")
        self.assertEqual(statuses[released], "pending")
        self.assertEqual(launched, [])

    def test_cancelling_prerequisite_holds_pending_consumer(self) -> None:
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
        self.assertEqual(consumer_row["status"], "pending")
        self.assertIsNone(consumer_row["summary"])
        self.assertEqual(result.held_run_ids, (consumer,))

    def test_resuming_cancelled_parent_rebinds_and_releases_held_consumer(self) -> None:
        prerequisite = _run(self.root, status="running", title="producer")
        consumer = _defer(self.root, prerequisite)
        con = db.connect(self.root)
        try:
            con.execute("UPDATE runs SET session_ref='session-1' WHERE id=?", (prerequisite,))
            con.commit()
            cancel.stop_run(con, prerequisite)
            parent = dict(con.execute("SELECT * FROM runs WHERE id=?", (prerequisite,)).fetchone())
            continuation = supervise.create_followup(
                con, self.root, parent, "codex", "continue the producer"
            )
            rebound = con.execute(
                "SELECT depends_on_run FROM dispatch_dependencies WHERE run_id=?",
                (consumer,),
            ).fetchone()[0]
            con.execute("UPDATE runs SET status='done' WHERE id=?", (continuation,))
            con.commit()
            launched: list[int] = []
            dependencies.process_ready(
                con, self.root, self.cfg,
                lambda _root, run_id: launched.append(run_id),
            )
        finally:
            con.close()
        self.assertEqual(rebound, continuation)
        self.assertEqual(launched, [consumer])

    def test_legacy_dependency_rows_migrate_to_requires_success(self) -> None:
        legacy_root = self.root / "legacy"
        state_dir = legacy_root / ".orchestra"
        state_dir.mkdir(parents=True)
        legacy = sqlite3.connect(state_dir / "orchestra.db")
        try:
            legacy.executescript("""
                CREATE TABLE runs (
                  id INTEGER PRIMARY KEY,
                  agent TEXT NOT NULL,
                  backend TEXT NOT NULL,
                  requested_by TEXT NOT NULL,
                  workdir TEXT NOT NULL,
                  parent_run INTEGER,
                  status TEXT NOT NULL,
                  started_at TEXT NOT NULL
                );
                CREATE TABLE messages (
                  id INTEGER PRIMARY KEY,
                  sender TEXT NOT NULL,
                  recipient TEXT NOT NULL,
                  body TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE TABLE feed (
                  id INTEGER PRIMARY KEY,
                  author TEXT NOT NULL,
                  body TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE TABLE dispatch_dependencies (
                  run_id INTEGER NOT NULL,
                  depends_on_run INTEGER NOT NULL,
                  PRIMARY KEY(run_id, depends_on_run)
                );
                CREATE TABLE deferred_dispatches (
                  run_id INTEGER PRIMARY KEY,
                  mission TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'pending',
                  created_at TEXT NOT NULL
                );
            """)
            for run_id in (1, 2):
                legacy.execute(
                    "INSERT INTO runs(id, agent, backend, requested_by, workdir, status, started_at) "
                    "VALUES(?, 'minimax', 'opencode', 'codex', ?, 'pending', ?)",
                    (run_id, str(legacy_root), db.now()),
                )
            legacy.execute(
                "INSERT INTO dispatch_dependencies(run_id, depends_on_run) VALUES(2,1)"
            )
            legacy.commit()
        finally:
            legacy.close()

        con = db.connect(legacy_root)
        try:
            row = con.execute(
                "SELECT kind FROM dispatch_dependencies WHERE run_id=2 AND depends_on_run=1"
            ).fetchone()
            self.assertEqual(row["kind"], dependencies.REQUIRES_SUCCESS)
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
