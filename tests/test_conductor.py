"""Public orchestration contracts for the event-driven conductor."""
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from orchestra import conductor, config, db, dispatch, findings, observer
from tests.test_sweeper import PROJECT_ID, SweeperFixture


PLANNER_CONFIG = """

[profiles.planner]
backend = "opencode"
tier = 2
note = "plenty of headroom"
"""


def session_of(prompt: str) -> str:
    for line in prompt.splitlines():
        if "You are session orchestra/" in line:
            return line.split("orchestra/", 1)[1].split(".", 1)[0].strip()
    raise AssertionError("the prompt named no session")


class ConductorFixture(SweeperFixture):
    def setUp(self) -> None:
        super().setUp()
        self.global_config.write_text(self.global_config.read_text() + PLANNER_CONFIG)
        self.cfg = config.load()
        self.replies: list[dict] = []
        self.prompts: list[str] = []

    def turn(self, profile, prompt):
        self.prompts.append(prompt)
        reply = self.replies.pop(0) if self.replies else {
            "action": "wait", "rationale": "nothing to do", "await": "settled"}
        return "```json\n" + json.dumps(reply) + "\n```"

    def add_goal(self, task_id="W-0100", title="Ship the thing", **kwargs):
        kwargs.setdefault("goal", "Make the thing shippable.")
        return self.work.add_task(task_id, title, delegated=True,
                                  tags=["goal"], **kwargs)

    def conduct(self, *replies, floor=0):
        self.replies = list(replies)
        return conductor.pass_once(self.cfg, self.client, turn=self.turn,
                                   launcher=self.launcher, floor=floor)

    def turns(self, goal_id="W-0100"):
        con = db.connect()
        rows = list(con.execute(
            "SELECT * FROM conductor_turns WHERE goal_id=? ORDER BY id",
            (goal_id,)))
        con.close()
        return rows

    def make_run(self, item_id="W-0100", status="running", **columns) -> int:
        con = db.connect()
        fields = {
            "profile": "stub", "backend": "opencode", "title": "work",
            "requested_by": "work", "workdir": str(self.root),
            "project_id": PROJECT_ID, "status": status, "started_at": db.now(),
            "work_item": item_id,
        }
        if status in db.RUN_TERMINAL:
            fields["finished_at"] = db.now()
            fields.setdefault("summary", "did the thing")
        fields.update(columns)
        names = ", ".join(fields)
        run_id = int(con.execute(
            f"INSERT INTO runs({names}) VALUES({', '.join('?' * len(fields))})",
            tuple(fields.values())).lastrowid)
        con.commit()
        con.close()
        return run_id

    def finish_run(self, run_id, status="done", summary="did the thing") -> None:
        con = db.connect()
        con.execute("UPDATE runs SET status=?, summary=?, finished_at=? WHERE id=?",
                    (status, summary, db.now(), run_id))
        con.commit()
        con.close()

    def goal_log(self, goal_id="W-0100") -> str:
        return "\n".join(e["message"] for e in self.work.tasks[goal_id]["log"])


class GoalAndTriggerTests(ConductorFixture, unittest.TestCase):
    def test_only_open_delegated_goal_items_are_selected(self) -> None:
        self.work.add_task("W-1", "plain", delegated=True)
        self.work.add_task("W-2", "tag only", tags=["goal"])
        self.add_goal("W-3", status="done")
        goal = self.add_goal("W-4")
        self.assertEqual(
            [item["id"] for item in conductor.open_goals(self.work.tasks.values())],
            ["W-4"])
        self.assertTrue(conductor.is_goal(goal))

    def test_any_goal_run_in_flight_costs_no_planner_turn(self) -> None:
        self.add_goal()
        self.work.add_task("W-0101", "child", parent_id="W-0100")
        self.make_run("W-0101", status="running")
        self.assertEqual(self.conduct(), [])
        self.assertEqual(self.conduct(), [])
        self.assertEqual(self.prompts, [])
        self.assertEqual(self.turns(), [])

    def test_idle_and_each_new_settle_fire_once(self) -> None:
        self.add_goal()
        propose = {"action": "propose", "rationale": "next slice",
                   "title": "next slice"}
        self.assertEqual([a["trigger"] for a in self.conduct(propose)], ["idle"])
        self.assertEqual(self.conduct(propose), [])

        run_id = self.make_run(status="running")
        self.finish_run(run_id)
        self.assertEqual([a["trigger"] for a in self.conduct(propose)], ["settled"])
        self.assertEqual(self.conduct(propose), [])
        self.assertEqual(
            [(row["trigger_kind"], row["trigger_key"]) for row in self.turns()],
            [("idle", "idle:0"), ("settled", f"settle:{run_id}")])

    def test_blocked_outranks_the_same_settle_and_fires_once(self) -> None:
        self.add_goal()
        run_id = self.make_run(status="running")
        self.finish_run(run_id, status="failed", summary="harness died")
        wait = {"action": "wait", "rationale": "hold", "await": "blocked"}
        took = self.conduct(wait)
        self.assertEqual((took[0]["trigger"], took[0]["key"]),
                         ("blocked", f"run:{run_id}"))
        self.assertEqual(self.conduct(wait), [])
        self.assertEqual([row["trigger_kind"] for row in self.turns()], ["blocked"])

    def test_each_new_human_comment_fires_once(self) -> None:
        self.add_goal()
        self.make_run(status="running")
        for text in ("prioritise the API", "and the docs"):
            self.work.human_log("W-0100", text)
            took = self.conduct({"action": "wait", "rationale": "noted",
                                 "await": "comment"})
            self.assertEqual([a["trigger"] for a in took], ["comment"])
            self.assertIn(text, self.prompts[-1])
            self.assertEqual(self.conduct(), [])

    def test_low_runway_fires_once_and_thresholds_are_conservative(self) -> None:
        self.add_goal()
        con = db.connect()
        reset = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        con.execute(
            "INSERT INTO runway_polls(provider, remaining, unit, resets_at, "
            "as_of, polled_at) VALUES('claude', 4, 'percent', ?, ?, ?)",
            (reset, db.now(), db.now()))
        con.commit()
        con.close()
        wait = {"action": "wait", "rationale": "hold", "await": "runway_low"}
        self.assertEqual([a["trigger"] for a in self.conduct(wait)], ["runway_low"])
        self.assertEqual(self.conduct(wait), [])

        for remaining, expected in ((4, True), (80, False), (None, False)):
            with self.subTest(remaining=remaining):
                entries = [{"provider": "codex", "remaining": remaining,
                            "unit": "percent", "limit_value": None,
                            "resets_at": reset, "id": 1}]
                self.assertEqual(bool(conductor.runway_low(entries)), expected)

    def test_turn_floor_holds_back_a_new_event(self) -> None:
        self.add_goal()
        self.conduct({"action": "propose", "rationale": "go", "title": "slice"})
        run_id = self.make_run(status="running")
        self.finish_run(run_id)
        self.replies = [{"action": "done", "rationale": "done"}]
        self.assertEqual(conductor.pass_once(
            self.cfg, self.client, turn=self.turn, launcher=self.launcher,
            floor=conductor.TURN_FLOOR_SECONDS), [])
        self.assertEqual(len(self.turns()), 1)


class WaitTests(ConductorFixture, unittest.TestCase):
    def test_named_wait_ignores_other_events_until_its_event_arrives(self) -> None:
        self.add_goal()
        self.conduct({"action": "wait", "rationale": "need an answer",
                      "await": "comment"})
        run_id = self.make_run(status="running")
        self.finish_run(run_id, status="failed")
        self.assertEqual(self.conduct({"action": "done", "rationale": "no"}), [])
        self.work.human_log("W-0100", "here is the answer")
        took = self.conduct({"action": "done", "rationale": "accepted"})
        self.assertEqual([a["trigger"] for a in took], ["comment"])

    def test_wait_and_invalid_replies_never_mutate_work(self) -> None:
        self.add_goal()
        before = self.work.mutation_count()
        self.conduct({"action": "wait", "rationale": "still running",
                      "await": "settled"})
        self.assertEqual(self.work.mutation_count(), before)
        self.assertEqual(self.turns()[0]["wait_event"], "settled")

        replies = (
            "not JSON",
            '{"action": "delete_everything"}',
            '{"action": "wait", "await": "tuesday"}',
        )
        for reply in replies:
            with self.subTest(reply=reply):
                decision = conductor.parse_decision(reply)
                self.assertEqual(decision["action"], "wait")
                self.assertIsNone(decision["await"])


class PacketTests(ConductorFixture, unittest.TestCase):
    def big_packet(self) -> str:
        goal = {"id": "W-0100", "title": "T" * 4000,
                "status": "in_progress", "sections": {
                    "goal": "G" * 60000, "acceptanceCriteria": "A" * 60000}}
        delta = conductor.delta_entries([], [
            {"at": f"2026-08-13T00:{n:02d}:00Z",
             "text": f"comment {n:02d} " + "x" * 500} for n in range(30)])
        children = conductor.child_entries([
            {"id": f"W-9{n:03d}", "status": "ready",
             "title": "child " + "y" * 400,
             "updatedAt": f"2026-08-12T00:{n:02d}:00Z"} for n in range(30)])
        issues = conductor.issue_entries([
            {"id": f"issue_{n}", "state": "queued",
             "title": "finding " + "z" * 400,
             "updatedAt": f"2026-08-11T00:{n:02d}:00Z"} for n in range(30)])
        return conductor.build_packet(
            goal, delta=delta, children=children, issues=issues,
            profiles=conductor.profile_entries(self.cfg),
            runway_entries=conductor.runway_entries_for([{
                "provider": "claude", "remaining": 40.0, "unit": "percent",
                "resets_at": None, "limit_value": None, "id": 1}]), flight=[])

    def test_packet_is_hard_bounded_and_evicts_old_detail_first(self) -> None:
        packet = self.big_packet()
        self.assertLessEqual(len(packet), conductor.PACKET_CHAR_CAP)
        self.assertLessEqual(conductor.est_tokens(packet), conductor.PACKET_TOKEN_CAP)
        self.assertNotIn("comment 00", packet)
        self.assertIn("comment 29", packet)
        self.assertIn("runway claude", packet)

    def test_live_packet_records_its_bound_and_current_routing_state(self) -> None:
        self.add_goal(goal="G" * 80000)
        self.work.add_task("W-0101", "child", parent_id="W-0100")
        con = db.connect()
        con.execute("INSERT INTO runway_polls(provider, remaining, unit, polled_at) "
                    "VALUES('claude', 55, 'percent', ?)", (db.now(),))
        con.commit()
        con.close()
        self.conduct({"action": "wait", "rationale": "reading",
                      "await": "comment"})
        self.assertLessEqual(self.turns()[0]["packet_tokens"],
                             conductor.PACKET_TOKEN_CAP)
        self.assertLessEqual(len(self.prompts[0]),
                             conductor.PACKET_CHAR_CAP + len(conductor.INSTRUCTIONS) + 200)
        self.assertIn("plenty of headroom", self.prompts[0])
        self.assertIn("tier 2 (generalist)", self.prompts[0])
        self.assertIn("runway claude: 55% left", self.prompts[0])


class ActionTests(ConductorFixture, unittest.TestCase):
    def test_dispatch_launches_one_run_with_the_mission_and_attribution(self) -> None:
        self.add_goal()
        took = self.conduct({"action": "dispatch", "rationale": "start the API",
                             "item": "W-0100", "mission": "Build the API."})
        run = self.db_run()
        self.assertEqual(took[0]["run"], run["id"])
        self.assertEqual(run["work_item"], "W-0100")
        self.assertEqual(self.launched, [(self.root, run["id"])])
        self.assertIn("Build the API.", Path(run["brief_path"]).read_text())
        self.assertRegex(self.goal_log(), r"\[orchestra/\w+_\w+\] dispatched run \d+")

    def test_pause_preserves_the_trigger_without_spending_a_turn(self) -> None:
        self.add_goal()
        con = db.connect()
        dispatch.pause(con, "hold launches")
        con.close()
        decision = {"action": "dispatch", "rationale": "start",
                    "item": "W-0100", "mission": "Build it."}
        self.assertEqual(self.conduct(decision), [])
        self.assertEqual((self.turns(), self.prompts, self.launched), ([], [], []))

        con = db.connect()
        dispatch.resume(con)
        con.close()
        took = self.conduct(decision)
        self.assertEqual((took[0]["trigger"], took[0]["key"]), ("idle", "idle:0"))
        self.assertEqual(len(self.launched), 1)

    def test_dispatch_scope_and_live_run_deduplication(self) -> None:
        self.add_goal()
        self.work.add_task("W-0101", "child", parent_id="W-0100")
        self.work.add_task("W-0200", "someone else's", delegated=True)

        self.conduct({"action": "dispatch", "rationale": "outside",
                      "item": "W-0200", "mission": "nope"})
        self.assertEqual(self.db_run()["work_item"], "W-0100")

        self.work.human_log("W-0100", "start the child")
        self.conduct({"action": "dispatch", "rationale": "child",
                      "item": "W-0101", "mission": "Do the child."})
        self.assertEqual(self.db_run()["work_item"], "W-0101")

        self.work.human_log("W-0100", "start the child again")
        took = self.conduct({"action": "dispatch", "rationale": "again",
                             "item": "W-0101"})
        self.assertEqual(took[0]["action"], "skipped")
        self.assertEqual(len(self.launched), 2)

    def test_dispatch_respects_the_projects_enabled_profiles(self) -> None:
        self.global_config.write_text(
            self.global_config.read_text()
            + f'\n[project."{PROJECT_ID}"]\nenabled_profiles = ["planner"]\n')
        self.cfg = config.load()
        self.add_goal()
        took = self.conduct({"action": "dispatch", "rationale": "start",
                             "item": "W-0100", "mission": "Build it."})
        self.assertEqual(took[0]["action"], "skipped")
        self.assertIn(PROJECT_ID, took[0]["reason"])
        self.assertIn("'stub'", took[0]["reason"])
        self.assertEqual(self.launched, [])
        self.assertIsNone(self.db_run())

    def test_non_dispatch_action_effects(self) -> None:
        cases = (
            ("W-0100", {"action": "propose", "rationale": "needs docs",
                         "title": "Write docs"}),
            ("W-0200", {"action": "done", "rationale": "criteria met"}),
            ("W-0300", {"action": "ask_human", "rationale": "need a ruling",
                         "question": "Postgres or SQLite?"}),
        )
        con = db.connect()
        try:
            for goal_id, decision in cases:
                with self.subTest(action=decision["action"]):
                    goal = self.add_goal(goal_id)
                    result = conductor.apply_decision(
                        con, self.cfg, self.client, goal, {goal_id: goal},
                        decision, "calm_otter", self.launcher)
                    self.assertEqual(result["action"], decision["action"])
                    if decision["action"] == "propose":
                        children = [item for item in self.work.tasks.values()
                                    if item["parentId"] == goal_id]
                        self.assertEqual(children[0]["title"], "Write docs")
                        self.assertFalse(children[0]["delegated"])
                    elif decision["action"] == "done":
                        self.assertEqual(self.work.tasks[goal_id]["status"], "ready")
                        self.assertIn("criteria met", self.goal_log(goal_id))
                    else:
                        self.assertIsNone(result["nod"])
                        self.assertIn("Postgres or SQLite?", self.goal_log(goal_id))
                    self.assertEqual(conductor.wait_event_for(decision),
                                     "comment" if decision["action"] in
                                     ("done", "ask_human") else None)
        finally:
            con.close()


class ProfileTests(ConductorFixture, unittest.TestCase):
    def test_profile_selection_errors_name_the_fix(self) -> None:
        cases = (
            ({"profiles": {"worker": {}}}, conductor.profile_name,
             "planner_profile"),
            ({"profiles": {"worker": {}},
              "settings": {"planner_profile": "ghost"}}, conductor.profile_name,
             "ghost"),
            ({"profiles": {"a": {"tier": 2}, "b": {"tier": "mid"}}},
             conductor.profile_name, "several profiles"),
            ({"profiles": {"planner": {"tier": 2}}, "enabled_profiles": [],
              "project_id": "project-1"}, conductor.planner_profile, "project-1"),
        )
        for cfg, select, marker in cases:
            with self.subTest(marker=marker), \
                    self.assertRaises(conductor.PlannerUnconfigured) as caught:
                select(cfg)
            self.assertIn(marker, str(caught.exception))

    def test_project_settings_pick_the_planner(self) -> None:
        self.global_config.write_text(
            self.global_config.read_text()
            + f'\n[profiles.other]\nbackend = "opencode"\ntier = 2\n'
              f'[project."{PROJECT_ID}".settings]\nplanner_profile = "other"\n')
        self.assertEqual(conductor.profile_name(config.load(PROJECT_ID)), "other")

    def test_unconfigured_goal_is_reported_without_starting_a_session(self) -> None:
        self.global_config.write_text(
            self.global_config.read_text().replace("tier = 2", ""))
        self.cfg = config.load()
        self.add_goal()
        took = self.conduct({"action": "done", "rationale": "never asked"})
        self.assertEqual([a["action"] for a in took], ["unconfigured"])
        self.assertIn("planner_profile", took[0]["error"])
        self.assertEqual(self.prompts, [])


class AlignmentTests(ConductorFixture, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(setattr, findings, "PLANNER", findings.PLANNER)
        self.addCleanup(setattr, observer, "planner_review", observer.planner_review)
        conductor.attach()
        self.calls: list[str] = []

    def model(self, profile, prompt, **kwargs):
        self.calls.append(prompt)
        return '{"verdict": "aligned", "rationale": "it serves the goal"}'

    def worker_run(self, session_ref="worker-session-1"):
        run_id = self.make_run(status="done", session_ref=session_ref)
        con = db.connect()
        run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        con.close()
        return run

    def test_proposals_use_fresh_sessions_and_cannot_self_approve(self) -> None:
        goal = self.add_goal()
        run = self.worker_run()
        with mock.patch.object(conductor, "model_turn", self.model):
            verdicts = [findings.evaluate_alignment(
                goal, {"title": f"metric {n}", "why": "proves the goal"}, run)
                for n in range(2)]
        self.assertEqual([v["verdict"] for v in verdicts], ["aligned", "aligned"])
        sessions = [session_of(prompt) for prompt in self.calls]
        self.assertEqual(len(set(sessions)), 2)
        self.assertNotIn(run["session_ref"], sessions)
        self.assertNotIn(run["slug"], sessions)

        con = db.connect()
        conductor.log_turn(con, goal["id"], trigger="idle", key="idle:0",
                           action="propose", slug="clever_otter")
        con.close()
        own_run = self.worker_run(session_ref="clever_otter")
        self.calls.clear()
        with mock.patch.object(conductor, "model_turn", self.model):
            self.assertIsNone(findings.evaluate_alignment(
                goal, {"title": "my proposal"}, own_run))
        self.assertEqual(self.calls, [])


class JudgmentTests(ConductorFixture, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(setattr, observer, "planner_review", observer.planner_review)
        self.addCleanup(setattr, findings, "PLANNER", findings.PLANNER)
        conductor.attach()

    def test_deferred_judgment_reuses_one_paid_decision_after_resume(self) -> None:
        self.add_goal()
        run_id = self.make_run(status="done", summary="wrong shape",
                               session_ref="worker-session")
        self.replies = [{"action": "dispatch", "rationale": "re-brief",
                         "item": "W-0100", "mission": "Fix the tests properly."}]
        con = db.connect()
        dispatch.pause(con, "hold admissions")
        queued = observer.planner_review(
            con, run_id, "tests were deleted", cfg=self.cfg,
            turn=self.turn, launcher=self.launcher)
        self.assertEqual(queued["action"], "deferred")
        self.assertEqual(self.prompts, [])

        dispatch.resume(con)
        with mock.patch.object(conductor.work_client, "from_cfg",
                               return_value=self.client), \
                mock.patch.object(self.client, "log_task", return_value=None):
            held = conductor.resume_deferred_judgments(
                con, turn=self.turn, launcher=self.launcher)[0]
        self.assertEqual(held["action"], "deferred")
        self.assertEqual(len(self.prompts), 1)

        with mock.patch.object(conductor.work_client, "from_cfg",
                               return_value=self.client):
            result = conductor.resume_deferred_judgments(
                con, turn=self.turn, launcher=self.launcher)[0]
        self.assertEqual(conductor.resume_deferred_judgments(
            con, turn=self.turn, launcher=self.launcher), [])
        con.close()
        self.assertEqual(result["action"], "dispatch")
        self.assertIsNotNone(result["run"])
        self.assertEqual(len(self.prompts), 1)
        self.assertNotEqual(session_of(self.prompts[0]), "worker-session")
        self.assertIn("Fix the tests properly.",
                      Path(self.db_run()["brief_path"]).read_text())

    def test_unactionable_judgments_fall_back_to_deferred_review(self) -> None:
        self.add_goal()
        con = db.connect()
        try:
            with self.subTest(case="wait"):
                run_id = self.make_run(status="done")
                self.replies = [{"action": "wait", "rationale": "dodging",
                                 "await": "idle"}]
                result = observer.planner_review(
                    con, run_id, "bad work", cfg=self.cfg, turn=self.turn)
                self.assertEqual(result["action"], "deferred")

            with self.subTest(case="no goal"):
                prompts = len(self.prompts)
                run_id = self.make_run(item_id=None, status="done")
                result = observer.planner_review(
                    con, run_id, "bad work", cfg=self.cfg, turn=self.turn)
                self.assertEqual(result["action"], "deferred")
                self.assertEqual(len(self.prompts), prompts)

            with self.subTest(case="unconfigured planner"):
                self.global_config.write_text(
                    self.global_config.read_text().replace("tier = 2", ""))
                cfg = config.load()
                prompts = len(self.prompts)
                run_id = self.make_run(status="done")
                result = observer.planner_review(
                    con, run_id, "bad work", cfg=cfg, turn=self.turn)
                self.assertEqual(result["action"], "deferred")
                self.assertEqual(len(self.prompts), prompts)
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
