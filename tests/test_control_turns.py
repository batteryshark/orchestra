"""Control turns in the Runs tab (W-0214).

A control turn — router staffing, merge judge, observer, conductor — is
recorded as a terminal runs row with ``layer`` set, its transcript retained
and normalized through ``traces.ingest``. These tests patch
``subprocess.run``: no test here starts a real backend process.
``ORCHESTRA_HOME`` is sandboxed, so the real database and logs are untouched.
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from orchestra import conductor, db, http, merge, observer, review, router, traces

PROFILE = {"name": "stub", "backend": "opencode"}
LAYERS = ("router", "merge", "observer", "conductor")

# Two enabled profiles plus a router profile, so the staffing turn has a
# real choice to make.
CFG = {
    "work": {"router": "cheap", "profile": "stub"},
    "profiles": {
        "stub": {"backend": "opencode"},
        "cheap": {"backend": "opencode"},
        "big": {"backend": "opencode"},
    },
}

# An opencode-shaped turn transcript: thinking, a tool call with its result,
# and the reply. The acceptance criterion reads `reasoning` events off the
# ingested trace, so the fixture carries one.
TURN_LINES = [
    {"type": "session.updated", "sessionID": "sess-turn-1"},
    {"type": "message.part.updated", "part": {"type": "reasoning",
                                              "text": "tier 2 fits this item"}},
    {"type": "message.part.updated", "part": {
        "type": "tool", "tool": "read",
        "state": {"status": "running", "input": {"file": "b.py"}}}},
    {"type": "message.part.updated", "part": {
        "type": "tool", "tool": "read",
        "state": {"status": "completed", "output": "the file"}}},
    {"type": "message.part.updated", "part": {
        "type": "text", "text": '{"profile": "big", "reason": "cross-cutting"}'}},
]
STDOUT = "".join(json.dumps(line) + "\n" for line in TURN_LINES)


def fake_proc(returncode=0, stdout=STDOUT, stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class ControlTurnTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ,
                                   {"ORCHESTRA_HOME": str(Path(self.tmp.name) / "home")})
        self.env.start()
        self.con = db.connect()

    def tearDown(self) -> None:
        self.con.close()
        self.env.stop()
        self.tmp.cleanup()

    def turn_row(self, run_id: int):
        return self.con.execute("SELECT * FROM runs WHERE id=?",
                                (run_id,)).fetchone()

    # --- the turn is recorded ----------------------------------------------

    def test_each_layer_persists_a_visible_traced_turn_but_no_usage(self) -> None:
        for layer in LAYERS:
            project_id = f"proj-{layer}"
            stdout = _judge_stdout() if layer == "merge" else STDOUT
            with self.subTest(layer=layer), mock.patch.object(
                    observer.subprocess, "run", return_value=fake_proc(stdout=stdout)):
                if layer == "router":
                    cfg = dict(CFG, project_id=project_id)
                    name, _, _ = router.choose(
                        self.con, cfg, "W-1 · rewrite the scheduler", "stub",
                        dict(cfg["profiles"]["stub"]))
                    self.assertEqual(name, "big")
                elif layer == "merge":
                    cfg = {"project_id": project_id,
                           "settings": {"observer_profile": "stub"},
                           "profiles": dict(CFG["profiles"])}
                    self.assertEqual(
                        merge.judge_tripwires(
                            cfg, "delete dead code", ["deletes 6 file(s)"],
                            "the diff")["verdict"],
                        "mission_work")
                elif layer == "observer":
                    observer.model_turn(PROFILE, "pick one", con=self.con,
                                        layer=layer, project_id=project_id)
                else:
                    conductor.model_turn(PROFILE, "pick one", con=self.con,
                                         project_id=project_id)

                row = self.con.execute(
                    "SELECT * FROM runs WHERE layer=? ORDER BY id DESC LIMIT 1",
                    (layer,)).fetchone()
                self.assertEqual((row["status"], row["project_id"]),
                                 ("done", project_id))
                self.assertTrue(Path(row["log_path"]).is_file())
                kinds = {e["kind"] for e in
                         traces.events_for_run(self.con, row["id"])}
                self.assertTrue({"reasoning", "tool_call"} <= kinds)
                page = http.control_turns(project_id, layer, con=self.con)
                self.assertEqual([turn["id"] for turn in page["turns"]],
                                 [row["id"]])
                pinned = {turn["project_id"]: turn for turn in
                          http.snapshot(self.con)["pinned_turns"]}
                self.assertEqual(pinned[project_id]["id"], row["id"])

        self.con.execute(
            "UPDATE runs SET tokens_total=100, cost_usd=1 WHERE layer IS NOT NULL")
        self.con.commit()
        snapshot = http.snapshot(self.con)
        self.assertEqual((snapshot["runs"], snapshot["live_runs"]), ([], 0))
        self.assertEqual(snapshot["statistics"]["runs_total"], 0)
        self.assertIsNone(snapshot["statistics"]["tokens_total"])
        self.assertEqual(review.performance(self.con), [])

    def test_a_turn_without_a_layer_behaves_as_before(self) -> None:
        with mock.patch.object(observer.subprocess, "run",
                               return_value=fake_proc()):
            text = observer.model_turn(PROFILE, "pick one")
        self.assertIn("cross-cutting", text)
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) AS n FROM runs").fetchone()["n"], 0)
        self.assertEqual(list(Path(observer.paths.logs_dir()).glob("turn-*")),
                         [], "no transcript is kept for an unrecorded turn")

    # --- a failed turn is still viewable ------------------------------------

    def test_failed_turns_remain_visible(self) -> None:
        cases = [
            ("nonzero", fake_proc(returncode=3, stdout="", stderr="boom"),
             "exited 3"),
            ("empty", fake_proc(stdout=""), "no text"),
        ]
        for name, proc, reason in cases:
            project_id = f"failed-{name}"
            with self.subTest(case=name), mock.patch.object(
                    observer.subprocess, "run", return_value=proc):
                with self.assertRaises(observer.ObserverTurnError):
                    observer.model_turn(PROFILE, "pick one", con=self.con,
                                        layer="observer", project_id=project_id)
                row = self.con.execute(
                    "SELECT * FROM runs WHERE project_id=?", (project_id,)
                ).fetchone()
                self.assertEqual(row["status"], "failed")
                self.assertIn(reason, row["summary"])
                self.assertTrue(Path(row["log_path"]).is_file())
                self.assertEqual(
                    [turn["id"] for turn in http.control_turns(
                        project_id, con=self.con)["turns"]],
                    [row["id"]])

    def test_a_turn_is_pinned_only_on_the_project_it_acted_on(self) -> None:
        """One decision per project. A staffing turn for another project
        pinned above this board reads as if it happened here."""
        observer.record_turn(self.con, "router", PROFILE,
                             _write(self.tmp.name), True, "staffed for A",
                             project_id="proj-a")
        observer.record_turn(self.con, "merge", PROFILE,
                             _write(self.tmp.name), True, "escalated for B",
                             project_id="proj-b")
        # An older turn for A must lose to A's newest, not to B's.
        observer.record_turn(self.con, "observer", PROFILE,
                             _write(self.tmp.name), True, "watched A again",
                             project_id="proj-a")
        pinned = http.snapshot(self.con)["pinned_turns"]
        by_project = {t["project_id"]: t for t in pinned}
        self.assertEqual(set(by_project), {"proj-a", "proj-b"})
        self.assertEqual(by_project["proj-a"]["summary"], "watched A again")
        self.assertEqual(by_project["proj-b"]["summary"], "escalated for B")

    def test_a_turn_with_no_project_is_pinned_nowhere(self) -> None:
        """It names no project, so there is no board it belongs above."""
        observer.record_turn(self.con, "router", PROFILE,
                             _write(self.tmp.name), True, "no project")
        self.assertEqual(http.snapshot(self.con)["pinned_turns"], [])

    # --- the log: the series behind the pinned line (I-0081) -----------------

    def test_the_log_is_the_series_not_only_the_newest(self) -> None:
        """The pinned entry is one decision. The owner reads the reasoning
        over time, so the log goes back."""
        for note in ("watched once", "watched twice", "watched again"):
            observer.record_turn(self.con, "observer", PROFILE,
                                 _write(self.tmp.name), True, note,
                                 project_id="proj-a")
        observer.record_turn(self.con, "router", PROFILE,
                             _write(self.tmp.name), True, "staffed for B",
                             project_id="proj-b")
        page = http.control_turns("proj-a", con=self.con)
        self.assertEqual([t["summary"] for t in page["turns"]],
                         ["watched again", "watched twice", "watched once"],
                         "newest first, and every turn of this project")
        self.assertEqual({t["project_id"] for t in page["turns"]}, {"proj-a"})
        every = http.control_turns(con=self.con)["turns"]
        self.assertEqual(len(every), 4, "no project named means every project")

    def test_the_log_filters_by_layer(self) -> None:
        observer.record_turn(self.con, "observer", PROFILE,
                             _write(self.tmp.name), True, "watched",
                             project_id="proj-a")
        observer.record_turn(self.con, "router", PROFILE,
                             _write(self.tmp.name), True, "staffed",
                             project_id="proj-a")
        page = http.control_turns("proj-a", "observer", con=self.con)
        self.assertEqual([t["layer"] for t in page["turns"]], ["observer"])
        self.assertEqual(page["layer"], "observer")
        self.assertEqual(http.control_turns("proj-a", "merge",
                                            con=self.con)["turns"], [],
                         "a layer that has not decided anything is empty, "
                         "not everything")

    def test_the_log_is_turns_alone_and_carries_the_trace_link(self) -> None:
        """A worker run in the log would be the fleet again — and the row has
        to be a run payload, because the client opens it in the run detail
        screen and reads its transcript there."""
        self.con.execute(
            "INSERT INTO runs(profile, backend, requested_by, workdir, status, "
            "project_id, started_at) VALUES('w','opencode','human','/p',"
            "'running','proj-a',?)", (db.now(),))
        self.con.commit()
        turn_id = observer.record_turn(self.con, "observer", PROFILE,
                                       _write(self.tmp.name), True, "watched",
                                       project_id="proj-a")
        turns = http.control_turns("proj-a", con=self.con)["turns"]
        self.assertEqual([t["id"] for t in turns], [turn_id])
        self.assertEqual(turns[0]["layer"], "observer")
        self.assertIn("reasoning",
                      {e["kind"] for e in traces.events_for_run(self.con, turn_id)})

    def test_the_log_is_bounded(self) -> None:
        for _ in range(4):
            observer.record_turn(self.con, "observer", PROFILE,
                                 _write(self.tmp.name), True, "watched",
                                 project_id="proj-a")
        # A query string arrives as text, and a hostile one arrives as junk.
        self.assertEqual(len(http.control_turns("proj-a", limit="2",
                                                con=self.con)["turns"]), 2)
        self.assertEqual(http.control_turns("proj-a", limit="lots",
                                            con=self.con)["limit"],
                         http.RECENT_TURNS)
        self.assertEqual(http.control_turns("proj-a", limit=10 ** 6,
                                            con=self.con)["limit"],
                         http.RECENT_TURNS, "the cap is the daemon's, not the "
                         "caller's")
        self.assertEqual(http.control_turns("proj-a", limit=0,
                                            con=self.con)["limit"], 1)

    def test_statistics_and_the_performance_review_skip_turns(self) -> None:
        from orchestra import review
        observer.record_turn(self.con, "merge", PROFILE,
                             _write(self.tmp.name), True, "escalate")
        stats = http.snapshot(self.con)["statistics"]
        self.assertEqual(stats["runs_total"], 0)
        self.assertEqual(review.performance(self.con), [])

    # --- the decision links both ways ----------------------------------------

    def test_the_verdict_names_the_turn_and_the_turn_the_decision(self) -> None:
        turn_id = observer.record_turn(self.con, "observer", PROFILE,
                                       _write(self.tmp.name), True, "")
        run_id = int(self.con.execute(
            "INSERT INTO runs(profile, backend, requested_by, workdir, status, "
            "started_at) VALUES('w','opencode','human','/p','running',?)",
            (db.now(),)).lastrowid)
        self.con.commit()
        verdict = {"action": "ok", "reason": "steady progress",
                   "message": "", "turn_id": turn_id}
        observer.apply_verdict(self.con, run_id, verdict, cfg={})
        row = self.turn_row(turn_id)
        self.assertEqual(row["summary"], "ok: steady progress")

    # --- retention ------------------------------------------------------------

    def test_turn_logs_age_out_like_run_logs(self) -> None:
        log_path = _write(self.tmp.name)
        old = (datetime.now(timezone.utc) - timedelta(days=40)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        turn_id = observer.record_turn(self.con, "observer", PROFILE,
                                       log_path, True, "")
        self.con.execute("UPDATE runs SET finished_at=? WHERE id=?",
                         (old, turn_id))
        self.con.commit()
        pruned = traces.prune_raw_logs(self.con, days=30)
        self.assertEqual([p["run_id"] for p in pruned], [turn_id])
        self.assertFalse(Path(log_path).exists(),
                         "the transcript pruned under the run-log rule")


def _judge_stdout() -> str:
    lines = TURN_LINES[:-1] + [
        {"type": "message.part.updated", "part": {
            "type": "text",
            "text": '{"verdict": "mission_work", "rationale": "the mission '
                    'asked for the deletion"}'}},
    ]
    return "".join(json.dumps(line) + "\n" for line in lines)


def _write(tmp: str) -> str:
    path = Path(tmp) / f"transcript-{os.getpid()}-{len(list(Path(tmp).glob('transcript-*')))}.jsonl"
    path.write_text(STDOUT)
    return str(path)


if __name__ == "__main__":
    unittest.main()
