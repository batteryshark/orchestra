"""The conductor (DESIGN §10, W-0099).

**No test here dispatches a real model.** Every planner turn is a stub
callable, and the two seam paths patch ``conductor.model_turn``, so a
regression that started a session would fail rather than spend tokens.

The load-bearing claims:

* the packet stays inside its hard cap under a goal of any size;
* each trigger fires once and only once per settle — an idle goal does not
  wake a planner forever;
* a ``wait`` turn is not re-woken by an event it did not name;
* a run merely in flight costs ZERO planner calls;
* a proposal is judged by a different session than the one that raised it,
  and a planner may not judge its own proposal at all.
"""
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from dromond import conductor, config, db, findings, observer
from tests.fake_nod import DECISIONS_CHANNEL, DECISIONS_TOKEN, FakeNod
from tests.test_sweeper import PROJECT_ID, SweeperFixture

def session_of(prompt: str) -> str:
    """The session slug a prompt announces. Each turn mints its own."""
    for line in prompt.splitlines():
        if "You are session dromond/" in line:
            return line.split("dromond/")[1].split(".")[0].strip()
    raise AssertionError("the prompt named no session")


PLANNER_CONFIG = """

[profiles.planner]
backend = "opencode"
tier = 2
note = "plenty of headroom"
"""


class ConductorFixture(SweeperFixture):
    """The sweeper's workspace + fake Work, plus a mid-tier planner profile
    and a stub turn. Reused rather than rebuilt: a conducted dispatch must
    take the same launch path a swept one does."""

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
        return "here you go\n```json\n" + json.dumps(reply) + "\n```"

    # -- helpers ------------------------------------------------------------

    def add_goal(self, task_id="W-0100", title="Ship the thing", **kw):
        kw.setdefault("goal", "Make the thing shippable.")
        return self.work.add_task(task_id, title, delegated=True,
                                  tags=["goal"], **kw)

    def conduct(self, *replies, floor=0):
        self.replies = list(replies)
        return conductor.pass_once(self.cfg, self.client, turn=self.turn,
                                   launcher=self.launcher, floor=floor)

    def turns(self, goal_id="W-0100"):
        con = db.connect()
        rows = list(con.execute("SELECT * FROM conductor_turns WHERE goal_id=? "
                                "ORDER BY id", (goal_id,)))
        con.close()
        return rows

    def make_run(self, item_id="W-0100", status="running", **cols) -> int:
        con = db.connect()
        fields = {"profile": "stub", "backend": "opencode", "title": "work",
                  "requested_by": "work", "workdir": str(self.root),
                  "project_id": PROJECT_ID, "status": status,
                  "started_at": db.now(), "work_item": item_id}
        if status in db.RUN_TERMINAL:
            fields["finished_at"] = db.now()
            fields.setdefault("summary", "did the thing")
        fields.update(cols)
        names = ", ".join(fields)
        run_id = int(con.execute(
            f"INSERT INTO runs({names}) VALUES({', '.join('?' * len(fields))})",
            tuple(fields.values())).lastrowid)
        con.commit()
        con.close()
        return run_id

    def finish_run(self, run_id, status="done", summary="did the thing"):
        con = db.connect()
        con.execute("UPDATE runs SET status=?, summary=?, finished_at=? WHERE id=?",
                    (status, summary, db.now(), run_id))
        con.commit()
        con.close()

    def goal_log(self, goal_id="W-0100") -> str:
        return "\n".join(e["message"] for e in self.work.tasks[goal_id]["log"])


# --- what a goal is ----------------------------------------------------------

class GoalTests(ConductorFixture, unittest.TestCase):

    def test_a_goal_is_tagged_goal_and_delegated(self) -> None:
        self.work.add_task("W-1", "plain delegated task", delegated=True)
        self.work.add_task("W-2", "tagged but not delegated", tags=["goal"])
        goal = self.add_goal("W-3")
        tasks = self.work.tasks.values()
        self.assertEqual([g["id"] for g in conductor.open_goals(tasks)], ["W-3"])
        self.assertTrue(conductor.is_goal(goal))

    def test_a_closed_goal_is_not_watched(self) -> None:
        self.add_goal("W-3", status="done")
        self.assertEqual(conductor.open_goals(self.work.tasks.values()), [])

    def test_the_sweeper_still_owns_ordinary_delegated_items(self) -> None:
        self.work.add_task("W-1", "plain work", delegated=True)
        self.assertEqual(self.conduct(), [])
        self.assertEqual([a["action"] for a in self.sweep()], ["dispatch"])


# --- zero tokens while anything is merely in flight --------------------------

class InFlightTests(ConductorFixture, unittest.TestCase):

    def test_a_run_in_flight_costs_no_planner_call(self) -> None:
        self.add_goal()
        self.make_run(status="running")
        for _ in range(3):
            self.assertEqual(self.conduct(), [])
        self.assertEqual(self.prompts, [])
        self.assertEqual(self.turns(), [])

    def test_a_child_run_in_flight_also_silences_the_goal(self) -> None:
        self.add_goal()
        self.work.add_task("W-0101", "child", parent_id="W-0100")
        self.make_run("W-0101", status="running")
        self.assertEqual(self.conduct(), [])
        self.assertEqual(self.prompts, [])


# --- triggers ----------------------------------------------------------------

class TriggerTests(ConductorFixture, unittest.TestCase):

    def test_nothing_in_flight_fires_once_per_settle(self) -> None:
        self.add_goal()
        propose = {"action": "propose", "rationale": "queue the next slice",
                   "title": "next slice"}
        # A fresh goal: one turn to start it, and then silence forever.
        self.assertEqual(len(self.conduct(propose)), 1)
        for _ in range(3):
            self.assertEqual(self.conduct(propose), [])
        # A run settles: exactly one turn for that settle, not three.
        run_id = self.make_run(status="running")
        self.finish_run(run_id)
        took = self.conduct(propose)
        self.assertEqual([t["trigger"] for t in took], ["settled"])
        for _ in range(3):
            self.assertEqual(self.conduct(propose), [])
        self.assertEqual([t["trigger_kind"] for t in self.turns()],
                         ["idle", "settled"])
        self.assertEqual([t["trigger_key"] for t in self.turns()],
                         ["idle:0", f"settle:{run_id}"])

    def test_a_blocked_run_fires_once_and_outranks_the_settle(self) -> None:
        self.add_goal()
        run_id = self.make_run(status="running")
        self.finish_run(run_id, status="failed", summary="the harness died")
        wait = {"action": "wait", "rationale": "thinking", "await": "blocked"}
        took = self.conduct(wait)
        self.assertEqual([t["trigger"] for t in took], ["blocked"])
        self.assertEqual(took[0]["key"], f"run:{run_id}")
        # The same settle does not then buy a settled turn and an idle turn.
        for _ in range(3):
            self.assertEqual(self.conduct(wait), [])
        self.assertEqual([t["trigger_kind"] for t in self.turns()], ["blocked"])

    def test_a_new_human_comment_fires_once(self) -> None:
        self.add_goal()
        self.make_run(status="running")          # in flight: nothing else fires
        self.work.human_log("W-0100", "please prioritise the API half")
        took = self.conduct({"action": "wait", "rationale": "noted",
                             "await": "comment"})
        self.assertEqual([t["trigger"] for t in took], ["comment"])
        self.assertIn("prioritise the API half", self.prompts[0])
        self.assertEqual(self.conduct(), [])
        self.work.human_log("W-0100", "and the docs")
        took = self.conduct({"action": "wait", "rationale": "noted",
                             "await": "comment"})
        self.assertEqual([t["trigger"] for t in took], ["comment"])

    def test_our_own_posts_are_not_human_comments(self) -> None:
        self.add_goal()
        self.make_run(status="running")
        self.work.tasks["W-0100"]["log"].append(
            {"at": self.work.now(), "message": "[dromond/happy_otter] dispatched"})
        self.assertEqual(self.conduct(), [])

    def test_low_runway_fires_once_per_reset_window(self) -> None:
        self.add_goal()
        con = db.connect()
        # A window still open, read just now: a stale or already-reset reading
        # is reported to humans but never triggers (W-0179).
        soon = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        con.execute("INSERT INTO runway_polls(provider, remaining, unit, "
                    "resets_at, as_of, polled_at) VALUES('claude', 4, 'percent', "
                    "?, ?, ?)", (soon, db.now(), db.now()))
        con.commit()
        con.close()
        wait = {"action": "wait", "rationale": "hold", "await": "runway_low"}
        took = self.conduct(wait)
        self.assertEqual([t["trigger"] for t in took], ["runway_low"])
        self.assertEqual(self.conduct(wait), [])

    def test_healthy_runway_fires_nothing(self) -> None:
        self.add_goal()
        con = db.connect()
        con.execute("INSERT INTO runway_polls(provider, remaining, unit, "
                    "polled_at) VALUES('claude', 80, 'percent', ?)", (db.now(),))
        con.commit()
        con.close()
        took = self.conduct({"action": "wait", "rationale": "x", "await": "idle"})
        self.assertEqual([t["trigger"] for t in took], ["idle"])

    def test_unknown_runway_is_never_low(self) -> None:
        self.assertEqual(conductor.runway_low(
            [{"provider": "codex", "remaining": None, "unit": "percent",
              "limit_value": None, "reason": "no session", "id": 1}]), [])

    def test_the_floor_holds_a_second_turn_back(self) -> None:
        self.add_goal()
        self.conduct({"action": "propose", "rationale": "go", "title": "slice"})
        run_id = self.make_run(status="running")
        self.finish_run(run_id)
        # A real settle, but inside the ~2 minute floor: no turn.
        self.replies = [{"action": "propose", "rationale": "go", "title": "s2"}]
        self.assertEqual(conductor.pass_once(self.cfg, self.client, turn=self.turn,
                                             launcher=self.launcher,
                                             floor=conductor.TURN_FLOOR_SECONDS), [])
        self.assertEqual(len(self.turns()), 1)


# --- the wait gate -----------------------------------------------------------

class WaitTests(ConductorFixture, unittest.TestCase):

    def test_a_wait_is_not_woken_by_an_unrelated_event(self) -> None:
        self.add_goal()
        self.conduct({"action": "wait", "rationale": "the human owes me an answer",
                      "await": "comment"})
        self.assertEqual([t["wait_event"] for t in self.turns()], ["comment"])
        # A settle is a real event, and it is not the one named.
        run_id = self.make_run(status="running")
        self.finish_run(run_id)
        self.assertEqual(self.conduct({"action": "done", "rationale": "no"}), [])
        # Even a blocked run does not jump the gate.
        blocked = self.make_run(status="running")
        self.finish_run(blocked, status="failed")
        self.assertEqual(self.conduct({"action": "done", "rationale": "no"}), [])
        self.assertEqual(len(self.turns()), 1)
        # The named event does wake it.
        self.work.human_log("W-0100", "here is your answer")
        took = self.conduct({"action": "done", "rationale": "acceptance is met"})
        self.assertEqual([t["trigger"] for t in took], ["comment"])

    def test_a_wait_turn_posts_nothing_to_work(self) -> None:
        self.add_goal()
        before = self.work.mutation_count()
        self.conduct({"action": "wait", "rationale": "still running", "await": "settled"})
        self.assertEqual(self.work.mutation_count(), before)
        self.assertEqual(self.turns()[0]["action"], "wait")
        self.assertEqual(self.turns()[0]["rationale"], "still running")

    def test_an_unreadable_reply_changes_nothing(self) -> None:
        self.add_goal()
        before = self.work.mutation_count()
        actions = conductor.pass_once(
            self.cfg, self.client, launcher=self.launcher, floor=0,
            turn=lambda profile, prompt: "I could not decide, sorry.")
        self.assertEqual([a["action"] for a in actions], ["wait"])
        self.assertEqual(self.work.mutation_count(), before)
        self.assertIsNone(self.turns()[0]["wait_event"])  # any event may wake it

    def test_an_invented_action_changes_nothing(self) -> None:
        decision = conductor.parse_decision('{"action": "delete_everything"}')
        self.assertEqual(decision["action"], "wait")
        self.assertIsNone(decision["await"])

    def test_an_unknown_await_is_not_a_gate(self) -> None:
        decision = conductor.parse_decision(
            '{"action": "wait", "rationale": "x", "await": "tuesday"}')
        self.assertEqual(decision["action"], "wait")
        self.assertIsNone(decision["await"])


# --- the packet --------------------------------------------------------------

class PacketTests(ConductorFixture, unittest.TestCase):

    def big_packet(self):
        goal = {"id": "W-0100", "title": "T" * 4000, "status": "in_progress",
                "sections": {"goal": "G" * 60000,
                             "acceptanceCriteria": "A" * 60000}}
        delta = conductor.delta_entries([], [
            {"at": f"2026-08-13T00:{n:02d}:00Z", "text": f"comment {n} " + "x" * 500}
            for n in range(60)])
        children = conductor.child_entries([
            {"id": f"W-9{n:03d}", "status": "ready", "title": "child " + "y" * 400,
             "updatedAt": f"2026-08-12T00:{n:02d}:00Z"} for n in range(60)])
        issues = conductor.issue_entries([
            {"id": f"issue_{n}", "state": "queued", "title": "finding " + "z" * 400,
             "updatedAt": f"2026-08-11T00:{n:02d}:00Z"} for n in range(60)])
        return conductor.build_packet(
            goal, delta=delta, children=children, issues=issues,
            profiles=conductor.profile_entries(self.cfg),
            runway_entries=conductor.runway_entries_for(
                [{"provider": "claude", "remaining": 40.0, "unit": "percent",
                  "resets_at": None, "limit_value": None, "id": 1}]),
            flight=[])

    def test_a_huge_goal_still_respects_the_hard_cap(self) -> None:
        packet = self.big_packet()
        self.assertLessEqual(len(packet), conductor.PACKET_CHAR_CAP)
        self.assertLessEqual(conductor.est_tokens(packet), conductor.PACKET_TOKEN_CAP)

    def test_the_oldest_detail_goes_first(self) -> None:
        packet = self.big_packet()
        self.assertIn("W-0100", packet)                 # the goal survives
        self.assertIn("older entries truncated", packet)
        self.assertIn("comment 59", packet)             # newest detail survives
        self.assertNotIn("comment 00", packet)          # oldest went first
        self.assertIn("runway claude", packet)          # state is not old detail

    def test_the_six_blocks_are_all_there(self) -> None:
        goal = self.add_goal(notes="")
        packet = conductor.build_packet(
            goal, delta=[], children=[], issues=[], profiles=[],
            runway_entries=[], flight=[])
        for title in ("goal and acceptance", "delta since the last turn",
                      "open child items", "open findings",
                      "profiles and runway", "in flight now"):
            self.assertIn(f"## {title}", packet)

    def test_the_live_packet_is_capped_and_recorded(self) -> None:
        self.add_goal(goal="G" * 80000)
        self.work.add_task("W-0101", "child " + "y" * 300, parent_id="W-0100")
        self.conduct({"action": "wait", "rationale": "reading", "await": "comment"})
        row = self.turns()[0]
        self.assertLessEqual(row["packet_tokens"], conductor.PACKET_TOKEN_CAP)
        # The instruction preamble is a separate, measured fixed cost.
        self.assertLessEqual(len(self.prompts[0]),
                             conductor.PACKET_CHAR_CAP + len(conductor.INSTRUCTIONS) + 200)

    def test_the_packet_carries_profiles_notes_and_runway(self) -> None:
        self.add_goal()
        con = db.connect()
        con.execute("INSERT INTO runway_polls(provider, remaining, unit, "
                    "polled_at) VALUES('claude', 55, 'percent', ?)", (db.now(),))
        con.commit()
        con.close()
        self.conduct({"action": "wait", "rationale": "x", "await": "comment"})
        prompt = self.prompts[0]
        self.assertIn("plenty of headroom", prompt)
        self.assertIn("tier 2 (generalist)", prompt)
        self.assertIn("runway claude: 55% left", prompt)


# --- the five actions --------------------------------------------------------

class ActionTests(ConductorFixture, unittest.TestCase):

    def test_dispatch_starts_a_run_and_posts_to_the_goal(self) -> None:
        self.add_goal()
        took = self.conduct({"action": "dispatch", "rationale": "start the API half",
                             "item": "W-0100", "mission": "Build the API half."})
        run = self.db_run()
        self.assertEqual(took[0]["run"], run["id"])
        self.assertEqual(run["work_item"], "W-0100")
        self.assertEqual(self.launched, [(self.root, run["id"])])
        self.assertIn("Build the API half.", Path(run["brief_path"]).read_text())
        self.assertIn("## Work item snapshot", Path(run["brief_path"]).read_text())
        self.assertRegex(self.goal_log(),
                         r"\[dromond/\w+_\w+\] dispatched run \d+ on W-0100")

    def test_dispatch_can_target_an_open_child(self) -> None:
        self.add_goal()
        self.work.add_task("W-0101", "the API half", parent_id="W-0100")
        self.conduct({"action": "dispatch", "rationale": "child first",
                      "item": "W-0101", "mission": "Do the child."})
        self.assertEqual(self.db_run()["work_item"], "W-0101")

    def test_dispatch_cannot_target_someone_elses_item(self) -> None:
        self.add_goal()
        self.work.add_task("W-0200", "another goal's work", delegated=True)
        self.conduct({"action": "dispatch", "rationale": "not yours",
                      "item": "W-0200", "mission": "nope"})
        self.assertEqual(self.db_run()["work_item"], "W-0100")

    def test_dispatch_never_doubles_a_live_run(self) -> None:
        self.add_goal()
        self.work.add_task("W-0101", "child", parent_id="W-0100")
        self.make_run("W-0101", status="running")
        self.work.human_log("W-0100", "kick the child again")
        took = self.conduct({"action": "dispatch", "rationale": "again",
                             "item": "W-0101"})
        self.assertEqual(took[0]["action"], "skipped")
        self.assertEqual(self.launched, [])

    def test_dispatch_refuses_a_profile_the_project_has_not_enabled(self) -> None:
        """W-0187: the conductor is another caller of the one dispatcher, so
        it staffs through the same gate — and reports the refusal as a skip
        with its reason, never a quiet swap to some other profile."""
        self.global_config.write_text(
            self.global_config.read_text()
            + f'\n[project."{PROJECT_ID}"]\nenabled_profiles = ["planner"]\n')
        self.cfg = config.load()
        self.add_goal()
        took = self.conduct({"action": "dispatch", "rationale": "start it",
                             "item": "W-0100", "mission": "Build it."})
        self.assertEqual(took[0]["action"], "skipped")
        self.assertIn(PROJECT_ID, took[0]["reason"])
        self.assertIn("'stub'", took[0]["reason"])
        self.assertEqual(self.launched, [])
        self.assertIsNone(self.db_run())

    def test_propose_files_a_child_under_the_goal(self) -> None:
        self.add_goal()
        self.conduct({"action": "propose", "rationale": "the docs need their own item",
                      "title": "Write the docs"})
        child = [t for t in self.work.tasks.values() if t["parentId"] == "W-0100"]
        self.assertEqual(len(child), 1)
        self.assertEqual(child[0]["title"], "Write the docs")
        self.assertFalse(child[0]["delegated"])   # never delegated by an agent
        self.assertIn("proposed child", self.goal_log())

    def test_done_says_so_and_never_closes(self) -> None:
        self.add_goal()
        self.conduct({"action": "done", "rationale": "every criterion is met"})
        self.assertEqual(self.work.tasks["W-0100"]["status"], "ready")
        self.assertIn("every criterion is met", self.goal_log())
        self.assertEqual(self.turns()[0]["wait_event"], "comment")

    def test_ask_human_without_nod_still_reaches_the_thread(self) -> None:
        self.add_goal()
        took = self.conduct({"action": "ask_human", "rationale": "two ways to do it",
                             "question": "Postgres or SQLite?"})
        self.assertIsNone(took[0]["nod"])
        self.assertIn("the human loop is off", took[0]["nod_error"])
        self.assertIn("Postgres or SQLite?", self.goal_log())
        self.assertEqual(self.turns()[0]["wait_event"], "comment")


class AskHumanNodTests(ConductorFixture, unittest.TestCase):
    """``ask_human`` files a Nod card and mirrors it into the Work thread."""

    def setUp(self) -> None:
        super().setUp()
        self.nod = FakeNod()
        url = self.nod.start()
        secrets = self.tmp_path / "nod-secrets.env"
        secrets.write_text(f"base_url={url}\n"
                           f"decisions_channel={DECISIONS_CHANNEL}\n"
                           f"decisions_token={DECISIONS_TOKEN}\n")
        os.chmod(secrets, 0o600)
        self.global_config.write_text(
            self.global_config.read_text()
            + f'\n[nod]\nenabled = true\nsecrets_file = "{secrets}"\n')
        self.cfg = config.load()

    def tearDown(self) -> None:
        self.nod.stop()
        super().tearDown()

    def test_a_card_is_filed_and_mirrored(self) -> None:
        self.add_goal()
        took = self.conduct({"action": "ask_human", "rationale": "I need a ruling",
                             "question": "Ship behind a flag?"})
        request_id = took[0]["nod"]
        self.assertIsNotNone(request_id)
        card = self.nod.requests[request_id]
        self.assertEqual(card["body_markdown"], "Ship behind a flag?")
        self.assertIn("Ship behind a flag?", self.goal_log())
        con = db.connect()
        row = con.execute("SELECT * FROM nod_requests WHERE request_id=?",
                          (request_id,)).fetchone()
        con.close()
        self.assertEqual(row["work_item"], "W-0100")


# --- the planner profile -----------------------------------------------------

class ProfileTests(ConductorFixture, unittest.TestCase):

    def test_an_unconfigured_planner_names_what_to_configure(self) -> None:
        cfg = {"profiles": {"worker": {"backend": "opencode"}}}
        with self.assertRaises(conductor.PlannerUnconfigured) as caught:
            conductor.profile_name(cfg)
        message = str(caught.exception)
        self.assertIn("planner_profile", message)
        self.assertIn("tier = 2", message)
        self.assertIn(str(self.global_config), message)   # names the file too

    def test_a_named_profile_that_does_not_exist_is_named(self) -> None:
        cfg = {"profiles": {"worker": {}}, "settings": {"planner_profile": "ghost"}}
        with self.assertRaises(conductor.PlannerUnconfigured) as caught:
            conductor.profile_name(cfg)
        self.assertIn("ghost", str(caught.exception))

    def test_two_mid_profiles_ask_which(self) -> None:
        # 'mid' is the legacy spelling of tier 2; both still count.
        cfg = {"profiles": {"a": {"tier": 2}, "b": {"tier": "mid"}}}
        with self.assertRaises(conductor.PlannerUnconfigured) as caught:
            conductor.profile_name(cfg)
        self.assertIn("several profiles", str(caught.exception))

    def test_the_project_table_picks_the_planner(self) -> None:
        self.global_config.write_text(
            self.global_config.read_text()
            + f'\n[profiles.other]\nbackend = "opencode"\ntier = 2\n'
              f'[project."{PROJECT_ID}".settings]\nplanner_profile = "other"\n')
        self.assertEqual(conductor.profile_name(config.load(PROJECT_ID)), "other")

    def test_a_goal_without_a_planner_is_reported_not_crashed(self) -> None:
        self.global_config.write_text(
            self.global_config.read_text().replace("tier = 2", ""))
        self.cfg = config.load()
        self.add_goal()
        took = self.conduct({"action": "done", "rationale": "never asked"})
        self.assertEqual([a["action"] for a in took], ["unconfigured"])
        self.assertIn("planner_profile", took[0]["error"])
        self.assertEqual(self.prompts, [])


# --- seam: findings.PLANNER (nothing approves itself) ------------------------

class AlignmentSeamTests(ConductorFixture, unittest.TestCase):

    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(setattr, findings, "PLANNER", findings.PLANNER)
        self.addCleanup(setattr, observer, "planner_review", observer.planner_review)
        conductor.attach()
        self.calls: list[str] = []

    def model(self, profile, prompt, **kw):
        self.calls.append(prompt)
        return '{"verdict": "aligned", "rationale": "it serves the goal"}'

    def worker_run(self, session_ref="worker-session-1"):
        run_id = self.make_run(status="done", session_ref=session_ref)
        con = db.connect()
        run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        con.close()
        return run

    def test_attach_fills_both_seams(self) -> None:
        self.assertIs(findings.PLANNER, conductor.alignment_planner)
        self.assertIs(observer.planner_review, conductor.judgment_turn)

    def test_a_proposal_is_judged_by_a_different_session(self) -> None:
        goal = self.add_goal()
        run = self.worker_run()
        with mock.patch.object(conductor, "model_turn", self.model):
            first = findings.evaluate_alignment(
                goal, {"title": "add a metric", "why": "it proves the goal"}, run)
            second = findings.evaluate_alignment(
                goal, {"title": "add another", "why": "same"}, run)
        self.assertEqual(first["verdict"], "aligned")
        sessions = [session_of(prompt) for prompt in self.calls]
        self.assertEqual(len(sessions), 2)
        self.assertNotIn(run["session_ref"], sessions)   # never the worker's
        self.assertNotIn(run["slug"], sessions)
        self.assertNotEqual(sessions[0], sessions[1])    # fresh each time
        self.assertEqual(second["verdict"], "aligned")
        rows = self.turns()
        self.assertEqual([r["action"] for r in rows], ["align:aligned"] * 2)

    def test_a_planner_may_not_judge_its_own_proposal(self) -> None:
        goal = self.add_goal()
        con = db.connect()
        conductor.log_turn(con, "W-0100", trigger="idle", key="idle:0",
                           action="propose", slug="clever_otter")
        con.close()
        run = self.worker_run(session_ref="clever_otter")
        with mock.patch.object(conductor, "model_turn", self.model):
            verdict = findings.evaluate_alignment(goal, {"title": "mine"}, run)
        self.assertIsNone(verdict)          # unevaluated: the human rules
        self.assertEqual(self.calls, [])    # and no session was started

    def test_an_unconfigured_planner_leaves_a_proposal_unevaluated(self) -> None:
        self.global_config.write_text(
            self.global_config.read_text().replace("tier = 2", ""))
        goal = self.add_goal()
        run = self.worker_run()
        with mock.patch.object(conductor, "model_turn", self.model):
            self.assertIsNone(findings.evaluate_alignment(goal, {"title": "x"}, run))
        self.assertEqual(self.calls, [])

    def test_a_hedged_verdict_is_unevaluated(self) -> None:
        goal = self.add_goal()
        run = self.worker_run()
        with mock.patch.object(conductor, "model_turn",
                               lambda p, prompt, **kw: '{"verdict": "maybe"}'):
            self.assertIsNone(findings.evaluate_alignment(goal, {"title": "x"}, run))


# --- seam: observer.planner_review (judgment failures) -----------------------

class JudgmentSeamTests(ConductorFixture, unittest.TestCase):

    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(setattr, observer, "planner_review", observer.planner_review)
        self.addCleanup(setattr, findings, "PLANNER", findings.PLANNER)
        conductor.attach()

    def test_a_judgment_failure_is_re_briefed_by_a_planner(self) -> None:
        self.add_goal()
        run_id = self.make_run(status="done", summary="wrong shape")
        self.replies = [{"action": "dispatch", "rationale": "re-brief it",
                         "item": "W-0100", "mission": "Fix the tests properly."}]
        con = db.connect()
        result = observer.planner_review(
            con, run_id, "the tests were deleted, not fixed",
            cfg=self.cfg, turn=self.turn, launcher=self.launcher)
        con.close()
        self.assertEqual(result["action"], "dispatch")
        self.assertIsNotNone(result["run"])
        self.assertIn("the tests were deleted", self.prompts[0])
        self.assertEqual(self.db_run()["id"], result["run"])
        self.assertIn("Fix the tests properly.",
                      Path(self.db_run()["brief_path"]).read_text())

    def test_a_wait_from_a_judgment_turn_falls_back(self) -> None:
        self.add_goal()
        run_id = self.make_run(status="done")
        self.replies = [{"action": "wait", "rationale": "dodging", "await": "idle"}]
        con = db.connect()
        result = observer.planner_review(con, run_id, "bad work", cfg=self.cfg,
                                         turn=self.turn)
        con.close()
        self.assertEqual(result["action"], "deferred")

    def test_no_goal_falls_back_to_the_recorded_escalation(self) -> None:
        run_id = self.make_run(item_id=None, status="done")
        con = db.connect()
        result = observer.planner_review(con, run_id, "bad work", cfg=self.cfg,
                                         turn=self.turn)
        con.close()
        self.assertEqual(result["action"], "deferred")
        self.assertEqual(self.prompts, [])

    def test_an_unconfigured_planner_falls_back(self) -> None:
        self.global_config.write_text(
            self.global_config.read_text().replace("tier = 2", ""))
        self.cfg = config.load()
        self.add_goal()
        run_id = self.make_run(status="done")
        con = db.connect()
        result = observer.planner_review(con, run_id, "bad work", cfg=self.cfg,
                                         turn=self.turn)
        con.close()
        self.assertEqual(result["action"], "deferred")
        self.assertEqual(self.prompts, [])


def _main():  # pragma: no cover
    unittest.main()


if __name__ == "__main__":  # pragma: no cover
    _main()
