"""Dispatch policy (DESIGN §4, W-0164): no caps, order, honest queue state,
the pause switch.

The headline is a negative and it gets a test of its own: fifteen delegated
items in one project dispatch in ONE pass, with no cap and no stagger. The
rest cover what only matters for items that must wait.
"""
import os
import tempfile
import unittest
from unittest import mock

from dromond import cli, db, dispatch, supervise
from tests.test_sweeper import PROJECT_ID, SweeperFixture


class NoCapsTests(SweeperFixture, unittest.TestCase):
    def test_fifteen_items_in_one_project_dispatch_in_a_single_pass(self) -> None:
        for n in range(15):
            self.work.add_task(f"W-{n:04d}", f"job {n}", delegated=True)
        actions = self.sweep()
        self.assertEqual([a["action"] for a in actions], ["dispatch"] * 15)
        # Every one of them actually launched, in one pass, same project.
        self.assertEqual(len(self.launched), 15)
        self.assertEqual({root for root, _ in self.launched}, {self.root})
        con = db.connect()
        live = dispatch.live_runs(con)
        waiting = dispatch.waiting(con)
        con.close()
        self.assertEqual(live, 15)
        self.assertEqual(waiting, [])  # nothing was made to wait for a slot
        self.assertEqual(
            {t["status"] for t in self.work.tasks.values()}, {"in_progress"})

    def test_a_second_pass_adds_more_runs_beside_the_live_ones(self) -> None:
        # No global ceiling: live runs never gate the next dispatch.
        for n in range(10):
            self.work.add_task(f"W-{n:04d}", f"job {n}", delegated=True)
        self.sweep()
        for n in range(10, 15):
            self.work.add_task(f"W-{n:04d}", f"job {n}", delegated=True)
        actions = self.sweep()
        self.assertEqual([a["action"] for a in actions], ["dispatch"] * 5)
        con = db.connect()
        self.assertEqual(dispatch.live_runs(con), 15)
        con.close()


class DependencyOrderTests(SweeperFixture, unittest.TestCase):
    def waiting(self) -> list[dict]:
        con = db.connect()
        rows = dispatch.waiting(con)
        con.close()
        return rows

    def test_dependency_blocked_item_waits_with_its_reason(self) -> None:
        self.work.add_task("W-0001", "the prerequisite", status="in_progress")
        self.work.add_task("W-0002", "the dependent", delegated=True,
                           depends_on=["W-0001"])
        actions = self.sweep()
        self.assertEqual([a["action"] for a in actions], ["hold"])
        self.assertEqual(actions[0]["reason"], "dependency")
        self.assertEqual(actions[0]["detail"], "W-0001")
        # Honest queue state: it waits, so it is NOT in_progress and has no run.
        self.assertEqual(self.work.tasks["W-0002"]["status"], "ready")
        self.assertIsNone(self.db_run())
        self.assertEqual(self.launched, [])
        row = self.waiting()[0]
        self.assertEqual((row["item_id"], row["reason"], row["detail"]),
                         ("W-0002", "dependency", "W-0001"))

    def test_blocked_by_counts_as_a_dependency_too(self) -> None:
        self.work.add_task("W-0001", "blocker", status="review")
        self.work.add_task("W-0002", "blocked", delegated=True,
                           blocked_by=["W-0001"])
        self.sweep()
        self.assertEqual(self.waiting()[0]["detail"], "W-0001")
        self.assertIsNone(self.db_run())

    def test_a_settled_dependency_releases_the_item(self) -> None:
        self.work.add_task("W-0001", "the prerequisite", status="in_progress")
        self.work.add_task("W-0002", "the dependent", delegated=True,
                           depends_on=["W-0001"])
        self.sweep()
        self.work.human_move("W-0001", "done")
        actions = self.sweep()
        self.assertEqual([a["action"] for a in actions], ["dispatch"])
        self.assertEqual(self.db_run()["work_item"], "W-0002")
        self.assertEqual(self.waiting(), [])  # queue row cleared at dispatch

    def test_holding_is_logged_once_not_once_per_pass(self) -> None:
        self.work.add_task("W-0001", "the prerequisite", status="in_progress")
        self.work.add_task("W-0002", "the dependent", delegated=True,
                           depends_on=["W-0001"])
        self.assertEqual(len(self.sweep()), 1)
        self.assertEqual(self.sweep(), [])
        self.assertEqual(len(self.waiting()), 1)

    def test_ready_lane_board_order_decides_who_goes_first(self) -> None:
        # Work has no priority field: the lane's order is the whole signal.
        con = db.connect()
        dispatch.pause(con, "hold everything")
        con.close()
        for name in ("W-0001", "W-0002", "W-0003"):
            self.work.add_task(name, f"job {name}", delegated=True)
        self.sweep()
        self.assertEqual([r["item_id"] for r in self.waiting()],
                         ["W-0001", "W-0002", "W-0003"])
        # The human reorders the lane from the phone, then lets dispatch go.
        self.work.reorder_lane("W-0003", "W-0001", "W-0002")
        con = db.connect()
        dispatch.resume(con)
        con.close()
        actions = self.sweep()
        self.assertEqual([a["item"] for a in actions],
                         ["W-0003", "W-0001", "W-0002"])

    def test_dependencies_outrank_board_order(self) -> None:
        self.work.add_task("W-0001", "prerequisite", status="in_progress")
        self.work.add_task("W-0002", "blocked, but first on the board",
                           delegated=True, depends_on=["W-0001"])
        self.work.add_task("W-0003", "ready, second on the board", delegated=True)
        actions = self.sweep()
        # The ready one goes; the blocked one waits despite being higher.
        self.assertEqual([(a["action"], a["item"]) for a in actions],
                         [("dispatch", "W-0003"), ("hold", "W-0002")])


class PauseSwitchTests(SweeperFixture, unittest.TestCase):
    def restart(self) -> bool:
        """A daemon restart holds nothing in memory, so the switch has to
        come back out of the database on a fresh connection."""
        con = db.connect()
        try:
            return dispatch.paused(con)
        finally:
            con.close()

    def test_pause_persists_across_a_restart_and_blocks_new_dispatch(self) -> None:
        con = db.connect()
        dispatch.pause(con, "provider is flaky")
        con.close()
        self.assertTrue(self.restart())
        self.work.add_task("W-0001", "would have run", delegated=True)
        actions = self.sweep()
        self.assertEqual([(a["action"], a["reason"]) for a in actions],
                         [("hold", "paused")])
        self.assertEqual(actions[0]["detail"], "provider is flaky")
        # Not started, and not claiming to be started.
        self.assertIsNone(self.db_run())
        self.assertEqual(self.work.tasks["W-0001"]["status"], "ready")
        self.assertEqual(self.launched, [])

    def test_pause_leaves_in_flight_runs_completely_alone(self) -> None:
        self.work.add_task("W-0001", "already running", delegated=True)
        self.sweep()
        run = self.db_run()
        con = db.connect()
        dispatch.pause(con)
        self.assertEqual(dispatch.live_runs(con), 1)  # counted, not touched
        con.close()
        self.work.add_task("W-0002", "arrives during the pause", delegated=True)
        self.sweep()
        after = self.db_run(run["id"])
        self.assertEqual(after["status"], run["status"])
        self.assertIsNone(after["finished_at"])
        # The live run keeps being serviced: a human comment still ferries.
        self.finish_run(run["id"], status="running", summary=None,
                        session_ref="sess-1")
        self.work.human_log("W-0001", "one more thing")
        self.assertEqual([a["action"] for a in self.sweep()], ["ferry"])

    def test_resume_lets_the_held_item_go(self) -> None:
        con = db.connect()
        dispatch.pause(con)
        con.close()
        self.work.add_task("W-0001", "held", delegated=True)
        self.sweep()
        con = db.connect()
        was = dispatch.resume(con)
        con.close()
        self.assertIsNotNone(was)
        actions = self.sweep()
        self.assertEqual([a["action"] for a in actions], ["dispatch"])
        self.assertEqual(self.work.tasks["W-0001"]["status"], "in_progress")

    def test_resume_when_not_paused_is_a_no_op(self) -> None:
        con = db.connect()
        self.assertIsNone(dispatch.resume(con))
        self.assertFalse(dispatch.paused(con))
        con.close()

    def test_manual_dispatch_refuses_while_paused(self) -> None:
        con = db.connect()
        dispatch.pause(con, "quota")
        with self.assertRaises(SystemExit) as caught:
            cli._gate_dispatch(con, self.cfg, "human")
        con.close()
        self.assertIn("paused", str(caught.exception))
        self.assertIn("dromond resume", str(caught.exception))

    def test_deferred_dependency_release_is_held_by_the_pause(self) -> None:
        con = db.connect()
        con.execute(
            "INSERT INTO runs(id, profile, backend, title, requested_by, workdir, "
            "project_id, status, started_at) VALUES(1,'stub','opencode','done one',"
            "'human',?,?, 'done', ?)", (str(self.root), PROJECT_ID, db.now()))
        con.execute(
            "INSERT INTO runs(id, profile, backend, title, requested_by, workdir, "
            "project_id, status, started_at) VALUES(2,'stub','opencode','waiting',"
            "'human',?,?, 'pending', ?)", (str(self.root), PROJECT_ID, db.now()))
        con.execute("INSERT INTO dispatch_dependencies(run_id, depends_on_run) "
                    "VALUES(2, 1)")
        con.execute("INSERT INTO deferred_dispatches(run_id, mission, created_at) "
                    "VALUES(2, 'go', ?)", (db.now(),))
        con.commit()
        dispatch.pause(con, "hold the line")
        launched: list = []
        self.assertEqual(
            supervise.process_ready(con, lambda root, rid: launched.append(rid)), [])
        self.assertEqual(launched, [])
        self.assertEqual(
            con.execute("SELECT status FROM runs WHERE id=2").fetchone()["status"],
            "pending")  # still pending, still honest
        dispatch.resume(con)
        released = supervise.process_ready(con, lambda root, rid: launched.append(rid))
        con.close()
        self.assertEqual([r["status"] for r in released], ["fired"])
        self.assertEqual(launched, [2])


class StateReportTests(SweeperFixture, unittest.TestCase):
    def test_state_reports_pause_live_count_and_reasons(self) -> None:
        self.work.add_task("W-0001", "running one", delegated=True)
        self.sweep()
        self.work.add_task("W-0002", "prerequisite", status="ready")
        self.work.add_task("W-0003", "dependent", delegated=True,
                           depends_on=["W-0002"])
        self.sweep()
        con = db.connect()
        state = dispatch.state(con)
        con.close()
        self.assertEqual(state["live_runs"], 1)
        self.assertFalse(state["paused"])
        self.assertEqual([(w["item_id"], w["reason"]) for w in state["waiting"]],
                         [("W-0003", "dependency")])


class PauseSwitchTests(unittest.TestCase):
    """One switch, one format. There used to be two implementations writing
    the same meta key -- dispatch.py a JSON object, http.py a bare "1"/"0" --
    and the moment anyone pressed Resume in the dashboard or the phone, the
    "0" it left parsed as the int 0 and dispatch.pause_state called .get on
    it. That raised on EVERY daemon tick, so the daemon quietly stopped
    sweeping, dispatching and observing until someone read stderr."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {"DROMOND_HOME": self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.con = db.connect()
        self.addCleanup(self.con.close)

    def test_the_http_route_and_the_module_agree(self) -> None:
        from dromond import http
        self.assertFalse(dispatch.paused(self.con))

        http.set_dispatch_paused(self.con, True)
        self.assertTrue(dispatch.paused(self.con))
        self.assertTrue(http.dispatch_paused(self.con))
        self.assertIsNotNone(dispatch.pause_state(self.con)["at"])
        self.assertIsNotNone(http.pause_state(self.con)["since"])

        http.set_dispatch_paused(self.con, False)
        self.assertFalse(dispatch.paused(self.con))
        self.assertFalse(http.dispatch_paused(self.con))
        # The bug: this call is what raised, on every tick, forever.
        self.assertIsNone(dispatch.pause_state(self.con))

    def test_a_legacy_flag_left_in_the_key_is_read_not_fatal(self) -> None:
        for raw, expected in (("0", False), ("1", True), ("", False),
                              ("false", False), ("garbage", True)):
            db.meta_set(self.con, dispatch.PAUSE_KEY, raw)
            self.con.commit()
            self.assertEqual(dispatch.paused(self.con), expected, raw)

    def test_pausing_still_carries_its_note_and_time(self) -> None:
        dispatch.pause(self.con, note="landing a merge")
        state = dispatch.pause_state(self.con)
        self.assertEqual("landing a merge", state["note"])
        self.assertIsNotNone(state["at"])


if __name__ == "__main__":
    unittest.main()
