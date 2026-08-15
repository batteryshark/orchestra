"""The spin observer and the retry rule (DESIGN §7, W-0166).

Nothing here dispatches a real model: every observer turn is a stub
callable, and the one end-to-end test patches ``observer.model_turn``. The
load-bearing claims are:

* a long, productive run is never stopped (length is not a fault);
* a looping run is caught mechanically, with zero tokens;
* a stop always carries its reasoning and always escalates;
* one infrastructure failure is retried once, and a second consecutive one
  on the same item escalates instead of spending a third.
"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from dromond import config, db, observer

PROJECT_ID = "53efe3c3-6def-4797-8560-3dce073d7d63"

CONFIG = """\
[settings]
timeout = 60

[profiles.worker]
backend = "opencode"

[profiles.cheap]
backend = "opencode"
tier = 1
"""


class ObserverCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name).resolve()
        self.config_path = self.tmp_path / "config.toml"
        self.config_path.write_text(CONFIG)
        self.env = mock.patch.dict(os.environ, {
            "DROMOND_HOME": str(self.tmp_path / "home"),
            "DROMOND_CONFIG": str(self.config_path)})
        self.env.start()
        self.con = db.connect()

    def tearDown(self) -> None:
        self.con.close()
        self.env.stop()
        self.tmp.cleanup()

    # -- helpers
    def cfg(self) -> dict:
        return config.load(PROJECT_ID)

    def make_run(self, status="running", **cols) -> int:
        fields = {"profile": "worker", "backend": "opencode", "title": "do it",
                  "requested_by": "human", "workdir": str(self.tmp_path),
                  "status": status, "project_id": PROJECT_ID,
                  "started_at": db.now(), "session_ref": "sess-1"}
        fields.update(cols)
        names = ", ".join(fields)
        run_id = int(self.con.execute(
            f"INSERT INTO runs({names}) VALUES({', '.join('?' * len(fields))})",
            tuple(fields.values())).lastrowid)
        self.con.commit()
        return run_id

    def add_event(self, run_id: int, kind: str, name=None, payload="") -> None:
        seq = (self.con.execute(
            "SELECT COALESCE(MAX(seq), 0) AS n FROM events WHERE run_id=?",
            (run_id,)).fetchone()["n"]) + 1
        self.con.execute(
            "INSERT INTO events(run_id, seq, kind, name, payload, created_at) "
            "VALUES(?,?,?,?,?,?)", (run_id, seq, kind, name, payload, db.now()))
        self.con.commit()

    def add_transcript(self, run_id: int, count: int = observer.MIN_EVENTS) -> None:
        """Enough trace for the observer to have something to read."""
        for n in range(count):
            self.add_event(run_id, "assistant_text", None, f"step {n}")

    def stub(self, action="ok", reason="looks fine", message=""):
        """An observer turn that answers without touching a model."""
        calls = []

        def turn(profile, prompt, **kw):
            calls.append({"profile": profile, "prompt": prompt})
            return ("Here is my read.\n"
                    + json.dumps({"action": action, "reason": reason,
                                  "message": message}))
        turn.calls = calls
        return turn


class ProfileTests(ObserverCase):
    def test_tier_one_is_the_default_observer(self) -> None:
        self.assertEqual(observer.profile_name(self.cfg()), "cheap")

    def test_a_legacy_named_tier_still_volunteers(self) -> None:
        """W-0181 numbered the tiers. A hand-written `tier = "cheap"` from
        before that keeps working — ten real profiles use it."""
        self.config_path.write_text(CONFIG.replace("tier = 1", 'tier = "cheap"'))
        self.assertEqual(observer.profile_name(self.cfg()), "cheap")

    def test_an_explicit_setting_wins(self) -> None:
        self.config_path.write_text(CONFIG + '\n[settings]\nobserver_profile = "worker"\n')
        # TOML forbids a second [settings]; write the whole file instead.
        self.config_path.write_text(CONFIG.replace(
            "[settings]\ntimeout = 60",
            '[settings]\ntimeout = 60\nobserver_profile = "worker"'))
        self.assertEqual(observer.profile_name(self.cfg()), "worker")

    def test_no_configured_observer_says_exactly_what_to_add(self) -> None:
        self.config_path.write_text('[profiles.worker]\nbackend = "opencode"\n')
        with self.assertRaises(observer.ObserverUnconfigured) as caught:
            observer.profile_name(self.cfg())
        message = str(caught.exception)
        self.assertIn("observer_profile", message)
        self.assertIn("cheap", message)
        self.assertIn("no default profiles", message)

    def test_an_unknown_named_profile_is_refused(self) -> None:
        self.config_path.write_text(CONFIG.replace(
            "[settings]\ntimeout = 60",
            '[settings]\ntimeout = 60\nobserver_profile = "ghost"'))
        with self.assertRaises(observer.ObserverUnconfigured):
            observer.profile_name(self.cfg())

    # --- the enabled set (W-0187) --------------------------------------------
    # Picking the observer is a staffing moment: it spends a model turn on the
    # project's behalf, so a profile the project disabled must not be
    # volunteered for it by the back door of the tier scan.

    def test_the_tier_scan_only_sees_profiles_the_project_enabled(self) -> None:
        self.config_path.write_text(
            CONFIG + f'\n[project."{PROJECT_ID}"]\nenabled_profiles = ["worker"]\n')
        with self.assertRaises(observer.ObserverUnconfigured) as caught:
            observer.profile_name(self.cfg())
        self.assertIn("no observer profile", str(caught.exception))
        # another project, which enabled everything, still finds it
        self.assertEqual(observer.profile_name(config.load("other-uuid")), "cheap")

    def test_a_named_observer_the_project_disabled_is_refused_by_name(self) -> None:
        self.config_path.write_text(
            CONFIG.replace("[settings]\ntimeout = 60",
                           '[settings]\ntimeout = 60\nobserver_profile = "cheap"')
            + f'\n[project."{PROJECT_ID}"]\nenabled_profiles = ["worker"]\n')
        # the NAME still resolves — it is a configured profile
        self.assertEqual(observer.profile_name(self.cfg()), "cheap")
        # staffing it does not
        with self.assertRaises(observer.ObserverUnconfigured) as caught:
            observer.observer_profile(self.cfg())
        message = str(caught.exception)
        self.assertIn(PROJECT_ID, message)
        self.assertIn("Enabled there: worker", message)


class LoopDetectionTests(ObserverCase):
    """Layer (b): zero tokens, read from the NORMALIZED events table."""

    def test_a_long_productive_run_is_never_stopped(self) -> None:
        run_id = self.make_run()
        for n in range(400):  # hours of varied, honest work
            self.add_event(run_id, "tool_call", "bash",
                           json.dumps({"command": f"pytest tests/test_{n}.py"}))
            self.add_event(run_id, "tool_result", "bash", "ok")
            self.add_event(run_id, "tool_call", "edit",
                           json.dumps({"file_path": f"/src/mod_{n}.py"}))
        self.assertIsNone(observer.loop_reason(self.con, run_id))
        self.assertIsNone(observer.mechanical(self.con, run_id, self.cfg()))
        self.assertEqual(
            self.con.execute("SELECT status FROM runs WHERE id=?",
                             (run_id,)).fetchone()["status"], "running")

    def test_the_same_call_repeated_is_caught(self) -> None:
        run_id = self.make_run()
        payload = json.dumps({"command": "npm test"})
        for _ in range(observer.TOOL_REPEATS):
            self.add_event(run_id, "tool_call", "bash", payload)
        found = observer.loop_reason(self.con, run_id)
        self.assertIsNotNone(found)
        self.assertIn("same bash call", found[0])

    def test_one_file_edited_over_and_over_is_caught(self) -> None:
        run_id = self.make_run()
        for n in range(observer.FILE_REPEATS):
            self.add_event(run_id, "tool_call", "Edit",
                           json.dumps({"file_path": "/src/a.py", "new": n}))
        found = observer.loop_reason(self.con, run_id)
        self.assertIsNotNone(found)
        self.assertIn("/src/a.py", found[0])

    def test_edits_broken_up_by_other_work_are_not_a_loop(self) -> None:
        run_id = self.make_run()
        for n in range(observer.FILE_REPEATS * 2):
            self.add_event(run_id, "tool_call", "Edit",
                           json.dumps({"file_path": "/src/a.py", "new": n}))
            self.add_event(run_id, "tool_call", "bash",
                           json.dumps({"command": f"pytest -k {n}"}))
        self.assertIsNone(observer.loop_reason(self.con, run_id))

    def test_first_trip_corrects_and_the_second_stops_with_reasoning(self) -> None:
        run_id = self.make_run(log_path=str(self.tmp_path / "run.jsonl"))
        Path(self.tmp_path / "run.jsonl").write_text("{}\n")
        payload = json.dumps({"command": "npm test"})
        for _ in range(observer.TOOL_REPEATS):
            self.add_event(run_id, "tool_call", "bash", payload)
        first = observer.mechanical(self.con, run_id, self.cfg())
        self.assertEqual(first["action"], "tell")
        queued = self.con.execute(
            "SELECT body FROM messages WHERE run_id=? AND kind='interrupt'",
            (run_id,)).fetchone()
        self.assertIn("loop", queued["body"])
        # The same tail must not produce a correction every poll.
        self.assertIsNone(observer.mechanical(self.con, run_id, self.cfg()))
        # It keeps spinning anyway: that is feral, so it stops and escalates.
        for _ in range(observer.TOOL_REPEATS):
            self.add_event(run_id, "tool_call", "bash", payload)
        second = observer.mechanical(self.con, run_id, self.cfg())
        self.assertEqual(second["action"], "stop")
        self.assertTrue(second["reason"])
        self.assertEqual(self.con.execute(
            "SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()["status"],
            "killed")
        escalation = self.con.execute(
            "SELECT body FROM messages WHERE run_id=? AND kind='escalation'",
            (run_id,)).fetchone()
        self.assertIn(second["reason"], escalation["body"])
        stops = [o for o in observer.observations(self.con, run_id)
                 if o["action"] == "stop"]
        self.assertEqual(len(stops), 1)
        self.assertTrue(stops[0]["reason"])


class VerdictTests(ObserverCase):
    def test_a_verdict_is_read_out_of_chatty_output(self) -> None:
        verdict = observer.parse_verdict(
            'Thinking about it...\n{"action": "stop", "reason": "it is stuck"}\n'
            "Hope that helps.")
        self.assertEqual(verdict["action"], "stop")
        self.assertEqual(verdict["reason"], "it is stuck")

    def test_garbled_output_never_costs_a_run_its_life(self) -> None:
        for text in ("", "I think it's fine?", "{not json", '{"reason": "x"}'):
            self.assertEqual(observer.parse_verdict(text)["action"], "ok")

    def test_an_unknown_action_reads_as_ok(self) -> None:
        self.assertEqual(
            observer.parse_verdict('{"action": "terminate"}')["action"], "ok")


class ObserverTurnTests(ObserverCase):
    def test_the_turn_is_out_of_band(self) -> None:
        """The worker's session is never touched by an 'ok' look."""
        run_id = self.make_run()
        self.add_event(run_id, "assistant_text", None, "refactoring the parser")
        turn = self.stub("ok", "steady progress on the parser")
        verdict = observer.judge(self.con, run_id, self.cfg(), turn=turn)
        self.assertEqual(verdict["action"], "ok")
        self.assertEqual(verdict["profile"], "cheap")
        self.assertIn("LENGTH IS NOT A FAULT", turn.calls[0]["prompt"])
        self.assertIn("refactoring the parser", turn.calls[0]["prompt"])
        observer.apply_verdict(self.con, run_id, verdict, self.cfg())
        # Nothing was injected into the run and nothing was stopped.
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE run_id=?",
            (run_id,)).fetchone()["n"], 0)
        self.assertEqual(self.con.execute(
            "SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()["status"],
            "running")
        self.assertEqual(observer.observations(self.con, run_id)[0]["action"], "ok")

    def test_a_tell_reaches_the_worker_as_an_interrupt(self) -> None:
        run_id = self.make_run(log_path=str(self.tmp_path / "run.jsonl"))
        Path(self.tmp_path / "run.jsonl").write_text("{}\n")
        verdict = observer.judge(self.con, run_id, self.cfg(),
                                 turn=self.stub("tell", "wrong file", "edit b.py"))
        result = observer.apply_verdict(self.con, run_id, verdict, self.cfg())
        self.assertTrue(result["tell"]["queued"])
        self.assertEqual(self.con.execute(
            "SELECT body FROM messages WHERE run_id=?", (run_id,)).fetchone()["body"],
            "edit b.py")

    def test_a_stop_always_escalates_with_its_reasoning(self) -> None:
        run_id = self.make_run()
        verdict = observer.judge(
            self.con, run_id, self.cfg(),
            turn=self.stub("stop", "it has re-read the same file for an hour"))
        result = observer.apply_verdict(self.con, run_id, verdict, self.cfg())
        self.assertTrue(result["escalation"])
        self.assertEqual(self.con.execute(
            "SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()["status"],
            "killed")
        body = self.con.execute(
            "SELECT body FROM messages WHERE run_id=? AND kind='escalation'",
            (run_id,)).fetchone()["body"]
        self.assertIn("re-read the same file", body)
        recorded = observer.observations(self.con, run_id)[-1]
        self.assertEqual(recorded["action"], "stop")
        self.assertIn("re-read the same file", recorded["reason"])

    def test_an_observer_that_cannot_run_never_hurts_the_run(self) -> None:
        self.config_path.write_text('[profiles.worker]\nbackend = "opencode"\n')
        run_id = self.make_run()
        self.add_transcript(run_id)
        watcher = observer.Watcher(run_id, PROJECT_ID)
        watcher._pass()
        self.assertEqual(self.con.execute(
            "SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()["status"],
            "running")
        note = observer.observations(self.con, run_id)[0]
        self.assertEqual(note["action"], "ok")
        self.assertIn("could not run", note["reason"])


class CadenceTests(ObserverCase):
    def test_the_first_look_is_half_an_hour_in_then_hourly(self) -> None:
        started = time.time()
        run_id = self.make_run()
        clock = [started + 60]
        watcher = observer.Watcher(run_id, PROJECT_ID, clock=lambda: clock[0])
        self.assertFalse(watcher._due(self.con, self.cfg()), "a minute in")
        clock[0] = started + observer.FIRST_LOOK + 1
        self.assertTrue(watcher._due(self.con, self.cfg()), "half an hour in")
        observer.record(self.con, run_id, "observer", "ok", "fine")
        self.con.commit()
        self.assertFalse(watcher._due(self.con, self.cfg()), "just looked")
        clock[0] += observer.INTERVAL + 1
        self.assertTrue(watcher._due(self.con, self.cfg()), "an hour later")

    def test_a_terminal_run_is_never_looked_at(self) -> None:
        run_id = self.make_run(status="done")
        watcher = observer.Watcher(run_id, PROJECT_ID,
                                   clock=lambda: time.time() + observer.FIRST_LOOK * 4)
        self.assertFalse(watcher._due(self.con, self.cfg()))

    def test_polling_is_rate_limited(self) -> None:
        run_id = self.make_run()
        clock = [time.time()]
        watcher = observer.Watcher(run_id, PROJECT_ID, clock=lambda: clock[0])
        with mock.patch.object(observer, "mechanical", return_value=None) as looked:
            watcher.poll(self.con)
            watcher.poll(self.con)
            self.assertEqual(looked.call_count, 1)
            clock[0] += observer.POLL_EVERY + 1
            watcher.poll(self.con)
            self.assertEqual(looked.call_count, 2)


class CheckOnDemandTests(ObserverCase):
    def test_check_runs_the_same_turn_now(self) -> None:
        from dromond import http
        run_id = self.make_run(log_path=str(self.tmp_path / "run.jsonl"))
        Path(self.tmp_path / "run.jsonl").write_text("{}\n")
        self.add_transcript(run_id)
        turn = self.stub("tell", "it is editing the wrong module", "look at b.py")
        with mock.patch.object(observer, "model_turn", turn):
            result = http.check_run(self.con, run_id)
        self.assertEqual(result["observer"]["action"], "tell")
        self.assertIn("wrong module", result["verdict"])
        self.assertEqual(len(turn.calls), 1)

    def test_a_run_with_no_transcript_yet_costs_nothing(self) -> None:
        from dromond import http
        run_id = self.make_run()
        with mock.patch.object(observer, "model_turn") as never:
            result = http.check_run(self.con, run_id)
        never.assert_not_called()
        self.assertIn("nothing in the trace", result["observer"]["skipped"])

    def test_mechanical_only_never_calls_a_model(self) -> None:
        from dromond import http
        run_id = self.make_run()
        with mock.patch.object(observer, "model_turn") as never:
            result = http.check_run(self.con, run_id, observe=False)
        never.assert_not_called()
        self.assertTrue(result["verdict"])
        self.assertIn("mechanical", result["observer"]["skipped"])

    def test_an_unconfigured_observer_degrades_to_the_mechanical_verdict(self) -> None:
        from dromond import http
        self.config_path.write_text('[profiles.worker]\nbackend = "opencode"\n')
        run_id = self.make_run()
        self.add_transcript(run_id)
        result = http.check_run(self.con, run_id)
        self.assertIn("observer_profile", result["observer"]["error"])
        self.assertTrue(result["verdict"])


class RetryTests(ObserverCase):
    def _brief(self, run_id: int, text: str = "the original mission") -> None:
        from dromond import paths
        path = paths.briefs_dir() / f"run-{run_id}.md"
        path.write_text(text)
        self.con.execute("UPDATE runs SET brief_path=? WHERE id=?",
                         (str(path), run_id))
        self.con.commit()

    def test_an_infrastructure_failure_is_retried_once_with_the_same_brief(self) -> None:
        run_id = self.make_run(status="failed", work_item="W-0001")
        self._brief(run_id)
        launched = []
        result = observer.after_terminal(self.con, run_id,
                                         launcher=lambda root, rid: launched.append(rid))
        self.assertEqual(result["action"], "retry")
        retry = self.con.execute("SELECT * FROM runs WHERE id=?",
                                 (result["run"],)).fetchone()
        self.assertEqual(retry["retry_of"], run_id)
        self.assertEqual(retry["work_item"], "W-0001")
        self.assertIsNone(retry["session_ref"], "a retry is a fresh run, not a resume")
        self.assertEqual(Path(retry["brief_path"]).read_text(), "the original mission")
        self.assertEqual(launched, [result["run"]])

    def test_a_second_consecutive_failure_escalates_instead(self) -> None:
        first = self.make_run(status="failed", work_item="W-0002")
        self._brief(first)
        retry_id = observer.after_terminal(
            self.con, first, launcher=lambda root, rid: None)["run"]
        self.con.execute("UPDATE runs SET status='timeout', summary='died again' "
                         "WHERE id=?", (retry_id,))
        self.con.commit()
        launched = []
        result = observer.after_terminal(self.con, retry_id,
                                         launcher=lambda root, rid: launched.append(rid))
        self.assertEqual(result["action"], "escalate")
        self.assertEqual(launched, [], "nothing spends a third attempt")
        self.assertEqual(result["streak"], 2)
        body = self.con.execute(
            "SELECT body FROM messages WHERE run_id=? AND kind='escalation'",
            (retry_id,)).fetchone()["body"]
        self.assertIn("Two infrastructure failures", body)
        self.assertIn("W-0002", body)

    def test_a_finished_run_is_never_retried(self) -> None:
        run_id = self.make_run(status="done")
        self.assertEqual(
            observer.after_terminal(self.con, run_id)["action"], "none")

    def test_a_deliberate_stop_is_never_retried(self) -> None:
        run_id = self.make_run(status="failed")
        self._brief(run_id)
        observer.record(self.con, run_id, "observer", "stop", "it went feral")
        self.con.commit()
        result = observer.after_terminal(self.con, run_id,
                                         launcher=lambda root, rid: 1 / 0)
        self.assertEqual(result["action"], "none")
        summary = self.con.execute("SELECT summary FROM runs WHERE id=?",
                                   (run_id,)).fetchone()["summary"]
        self.assertIn("it went feral", summary)

    def test_a_killed_run_is_not_infrastructure(self) -> None:
        """Only a human or the observer sets `killed` here — see the note in
        observer.INFRA_TERMINAL."""
        run_id = self.make_run(status="killed")
        self.assertEqual(
            observer.after_terminal(self.con, run_id)["action"], "none")

    def test_a_paused_dispatch_defers_the_retry(self) -> None:
        from dromond import dispatch
        run_id = self.make_run(status="failed")
        self._brief(run_id)
        dispatch.pause(self.con, "maintenance")
        result = observer.after_terminal(self.con, run_id,
                                         launcher=lambda root, rid: 1 / 0)
        self.assertEqual(result["action"], "none")
        self.assertIn("paused", result["reason"])

    def test_dependents_wait_on_the_retry_instead_of_being_declined(self) -> None:
        first = self.make_run(status="failed")
        self._brief(first)
        dependent = self.make_run(status="pending")
        self.con.execute("INSERT INTO dispatch_dependencies(run_id, depends_on_run) "
                         "VALUES(?,?)", (dependent, first))
        self.con.commit()
        retry_id = observer.after_terminal(
            self.con, first, launcher=lambda root, rid: None)["run"]
        edge = self.con.execute(
            "SELECT depends_on_run FROM dispatch_dependencies WHERE run_id=?",
            (dependent,)).fetchone()
        self.assertEqual(edge["depends_on_run"], retry_id)


    def test_a_retry_re_homes_when_the_failed_runs_worktree_is_gone(self) -> None:
        """Live failure (run 28): the automatic retry of run 27 copied the
        failed run's workdir and branch into the new row with no existence
        check. Cleanup had already released that worktree (DESIGN §2) and the
        merge had deleted the branch (§9), so `Popen` died with
        FileNotFoundError — the same death as run 9, through another door."""
        import subprocess

        from dromond import project

        checkout = self.tmp_path / "workspace" / "demo"
        checkout.mkdir(parents=True)
        for args in (["init", "-q", "."], ["config", "user.email", "t@example.com"],
                     ["config", "user.name", "t"]):
            subprocess.run(["git", "-C", str(checkout), *args], check=True,
                           capture_output=True)
        (checkout / "a.txt").write_text("x\n")
        subprocess.run(["git", "-C", str(checkout), "add", "-A"], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(checkout), "commit", "-qm", "init"],
                       check=True, capture_output=True)
        project.remember(self.con, str(self.tmp_path / "workspace"),
                         [{"projectId": PROJECT_ID, "id": "demo", "name": "Demo",
                           "path": "demo"}])

        released = checkout / "worktrees" / "run-1"  # given back at finalization
        run_id = self.make_run(status="failed", work_item="W-0028",
                               workdir=str(released), branch="dromond/run-1")
        self._brief(run_id)
        self.assertFalse(released.exists())

        launched = []
        result = observer.after_terminal(
            self.con, run_id, launcher=lambda root, rid: launched.append((root, rid)))
        self.assertEqual(result["action"], "retry")
        retry = self.con.execute("SELECT * FROM runs WHERE id=?",
                                 (result["run"],)).fetchone()
        self.assertNotEqual(retry["workdir"], str(released))
        self.assertTrue(Path(retry["workdir"]).exists(),
                        "a retry must have somewhere to stand")
        self.assertNotEqual(retry["branch"], "dromond/run-1",
                            "the deleted branch is not reused")
        self.assertEqual(launched, [(checkout, result["run"])])

    def test_a_retry_keeps_a_workdir_that_is_still_there(self) -> None:
        run_id = self.make_run(status="failed", workdir=str(self.tmp_path),
                               branch="dromond/run-1")
        self._brief(run_id)
        retry_id = observer.after_terminal(
            self.con, run_id, launcher=lambda root, rid: None)["run"]
        retry = self.con.execute("SELECT workdir, branch FROM runs WHERE id=?",
                                 (retry_id,)).fetchone()
        self.assertEqual(retry["workdir"], str(self.tmp_path))
        self.assertEqual(retry["branch"], "dromond/run-1")


class PlannerSeamTests(ObserverCase):
    def test_bad_work_is_deferred_to_a_planner_and_escalated(self) -> None:
        run_id = self.make_run(status="done")
        result = observer.planner_review(self.con, run_id,
                                         "the tests it added never assert anything")
        self.assertEqual(result["action"], "deferred")
        recorded = observer.observations(self.con, run_id, layer="planner")
        self.assertEqual(len(recorded), 1)
        body = self.con.execute(
            "SELECT body FROM messages WHERE run_id=? AND kind='escalation'",
            (run_id,)).fetchone()["body"]
        self.assertIn("never assert anything", body)
        self.assertIn("judgment failure", body)


class WatcherCadenceTests(ObserverCase):
    """W-0189: PROOF that a long run is actually looked at.

    Run 24 wandered for 21 minutes with nothing watching it, because the
    watcher's only failure surface was a swallowed exception on its own
    thread. These drive the REAL ``Watcher`` — its own ``_due`` arithmetic,
    its own thread, its own ``judge`` — against a stubbed clock and a stubbed
    turn. They fail if the watcher is never polled, never fires, or is
    silently unconfigured.
    """

    def watcher(self, run_id, turn, clock):
        return observer.Watcher(run_id, PROJECT_ID, turn=turn, clock=clock)

    def test_a_long_run_is_looked_at_and_keeps_being_looked_at(self) -> None:
        run_id = self.make_run()
        self.add_transcript(run_id, 12)
        turn = self.stub("ok", "still converging on the mission")
        # Both anchors (the run's start, then the last observation) are real
        # timestamps, so the stubbed clock walks forward from real `base`.
        base = time.time()
        now = [base]
        watch = self.watcher(run_id, turn, lambda: now[0])

        watch.poll(self.con)
        watch.wait()
        self.assertEqual(observer.observations(self.con, run_id, "observer"), [],
                         "a run that just started was judged too early")

        # Every run gets its first look in minutes: a run that is already
        # lost is lost early, and that is when stopping it is still cheap.
        now[0] = base + observer.FIRST_LOOK + 1
        self.assertLessEqual(observer.FIRST_LOOK, 300)
        self.assertLessEqual(observer.INTERVAL, 1800)
        watch.poll(self.con)
        watch.wait()
        looks = observer.observations(self.con, run_id, "observer")
        self.assertEqual(len(looks), 1, "the first look never happened")
        self.assertEqual(looks[0]["action"], "ok")
        self.assertEqual(len(turn.calls), 1, "the observer turn never ran")

        # ...and then the hourly cadence, not a second look a minute later.
        now[0] = base + observer.INTERVAL - 60
        watch.poll(self.con)
        watch.wait()
        self.assertEqual(len(observer.observations(self.con, run_id, "observer")), 1)
        now[0] = base + observer.INTERVAL + 60
        watch.poll(self.con)
        watch.wait()
        self.assertEqual(len(observer.observations(self.con, run_id, "observer")), 2,
                         "the hourly look never happened")
        self.assertEqual(len(turn.calls), 2)

    def test_the_observer_is_asked_about_progress_not_length(self) -> None:
        """Layer (c) is the only thing that can catch run 24's shape: many
        tool calls, no edits, no repeats — mechanical detection sees nothing."""
        run_id = self.make_run()
        for n in range(40):
            self.add_event(run_id, "tool_call", "read",
                           json.dumps({"file_path": f"/stale/worktree-{n}/x.py"}))
        self.assertIsNone(observer.loop_reason(self.con, run_id),
                          "reading endlessly is not a mechanical loop")
        turn = self.stub("ok")
        now = [time.time() + observer.FIRST_LOOK + 1]
        watch = self.watcher(run_id, turn, lambda: now[0])
        watch.poll(self.con)
        watch.wait()
        prompt = turn.calls[0]["prompt"]
        self.assertIn("CONVERGING", prompt)
        self.assertIn("40 tool calls so far, 0 of them file edits", prompt)
        self.assertIn("changed nothing", prompt)

    def test_an_unconfigured_observer_records_that_nothing_watched(self) -> None:
        """Two tier-1 profiles: exactly run 24's install. The watcher must not
        guess, and must not fail silently either."""
        self.config_path.write_text(CONFIG + '\n[profiles.other]\nbackend = "opencode"\ntier = 1\n')
        run_id = self.make_run()
        self.add_transcript(run_id, 12)
        turn = self.stub("ok")
        now = [time.time() + observer.FIRST_LOOK + 1]
        watch = self.watcher(run_id, turn, lambda: now[0])
        watch.poll(self.con)
        watch.wait()
        self.assertEqual(turn.calls, [])
        looks = observer.observations(self.con, run_id, "observer")
        self.assertEqual(len(looks), 1)
        self.assertIn("could not run", looks[0]["reason"])
        # ...and it is loud everywhere a human looks.
        state = observer.status(self.cfg())
        self.assertFalse(state["enabled"])
        self.assertIn("tier 1", state["problem"])
        report = "\n".join(observer.status_report(self.cfg()))
        self.assertIn("DISABLED", report)
        self.assertIn("observer_profile", report)

    def test_a_configured_observer_reports_its_cadence(self) -> None:
        state = observer.status(self.cfg())
        self.assertTrue(state["enabled"])
        self.assertEqual(state["profile"], "cheap")
        self.assertEqual(state["first_look"], observer.FIRST_LOOK)
        self.assertIn("cheap", "\n".join(observer.status_report(self.cfg())))

    def test_a_stronger_observer_keeps_the_thirty_minute_first_look(self) -> None:
        self.config_path.write_text(
            CONFIG.replace("[settings]\ntimeout = 60",
                           '[settings]\ntimeout = 60\nobserver_profile = "heavy"')
            + '\n[profiles.heavy]\nbackend = "opencode"\ntier = 3\n')
        self.assertEqual(observer.first_look(self.cfg()), observer.FIRST_LOOK)

    def test_the_first_look_stays_configurable(self) -> None:
        self.config_path.write_text(CONFIG.replace(
            "[settings]\ntimeout = 60",
            "[settings]\ntimeout = 60\nobserver_first_look = 45"))
        self.assertEqual(observer.first_look(self.cfg()), 45)


STUB = """\
#!/usr/bin/env python3
import json, os, sys
print(json.dumps({"sessionID": "stub-session-1"}), flush=True)
print(json.dumps({"type": "text", "text": "stub ran"}), flush=True)
sys.exit(int(os.environ.get("STUB_EXIT", "0")))
"""

# A run that behaves like run 24: it reads and reads, edits nothing, repeats
# nothing, and stays alive long enough for the supervisor to look at it.
WANDERING_STUB = """\
#!/usr/bin/env python3
import json, os, sys, time
print(json.dumps({"sessionID": "stub-session-1"}), flush=True)
for n in range(12):
    print(json.dumps({"part": {"type": "tool", "tool": "read",
                               "state": {"status": "running",
                                         "input": {"file_path": "/stale/w%d.py" % n}}}}),
          flush=True)
time.sleep(float(os.environ.get("STUB_SECONDS", "2")))
print(json.dumps({"type": "text", "text": "still looking"}), flush=True)
sys.exit(0)
"""


class SupervisedRunTests(unittest.TestCase):
    """The seams as the supervisor actually reaches them, against a stub
    backend binary. No model is dispatched anywhere in here."""

    def setUp(self) -> None:
        from dromond import project
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name).resolve()
        self.root = self.tmp_path / "workspace" / "demo"
        self.root.mkdir(parents=True)
        self.config_path = self.tmp_path / "config.toml"
        self.config_path.write_text(
            '[settings]\ntimeout = 60\n\n[profiles.stub]\nbackend = "opencode"\n'
            '\n[profiles.cheap]\nbackend = "opencode"\ntier = 1\n')
        bin_dir = self.tmp_path / "stub-bin"
        bin_dir.mkdir()
        stub = bin_dir / "opencode"
        stub.write_text(STUB)
        stub.chmod(0o755)
        self.env = mock.patch.dict(os.environ, {
            "DROMOND_CONFIG": str(self.config_path),
            "DROMOND_HOME": str(self.tmp_path / "home"),
            "DROMOND_ROOT": str(self.root),
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
            "STUB_EXIT": "0"})
        self.env.start()
        con = db.connect()
        project.remember(con, str(self.tmp_path / "workspace"),
                         [{"projectId": PROJECT_ID, "id": "demo", "name": "Demo",
                           "path": "demo"}])
        con.close()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def _dispatch(self):
        from argparse import Namespace
        from dromond import cli, supervise
        ns = Namespace(mission=["do the thing"], to="stub", after=None,
                       brief_file=None, context=None, title=None, worktree=False,
                       sync=False)
        with mock.patch.object(supervise, "spawn_supervisor"):
            cli.cmd_dispatch(ns)
        con = db.connect()
        run_id = int(con.execute("SELECT MAX(id) AS n FROM runs").fetchone()["n"])
        con.close()
        return run_id

    def test_a_short_successful_run_costs_the_observer_nothing(self) -> None:
        from dromond import supervise
        run_id = self._dispatch()
        with mock.patch.object(observer, "model_turn") as never:
            supervise.supervise(self.root, run_id)
        never.assert_not_called()
        con = db.connect()
        try:
            self.assertEqual(con.execute("SELECT status FROM runs WHERE id=?",
                                         (run_id,)).fetchone()["status"], "done")
            self.assertEqual(observer.observations(con, run_id), [])
        finally:
            con.close()

    def test_the_supervisor_actually_looks_at_a_long_running_run(self) -> None:
        """W-0189, the whole path: the supervisor CONSTRUCTS the watcher,
        POLLS it, and a run that crosses the first look gets an observer turn
        with its transcript in the prompt. Fails if any link is missing."""
        from dromond import supervise
        stub = self.tmp_path / "stub-bin" / "opencode"
        stub.write_text(WANDERING_STUB)
        stub.chmod(0o755)
        self.config_path.write_text(self.config_path.read_text().replace(
            "[settings]\ntimeout = 60",
            "[settings]\ntimeout = 60\nobserver_first_look = 0"))
        run_id = self._dispatch()
        seen = []

        def turn(profile, prompt, **kw):
            seen.append(prompt)
            return json.dumps({"action": "ok", "reason": "reading, not editing"})

        with mock.patch.object(observer, "model_turn", turn):
            supervise.supervise(self.root, run_id)
        con = db.connect()
        try:
            looks = observer.observations(con, run_id, layer="observer")
        finally:
            con.close()
        self.assertTrue(looks, "nothing watched a run that ran past its first look")
        self.assertTrue(seen, "the observer turn never ran")
        self.assertIn("tool calls so far", seen[0])

    def test_the_supervisor_retries_an_infrastructure_failure_once(self) -> None:
        from dromond import supervise
        os.environ["STUB_EXIT"] = "3"
        run_id = self._dispatch()
        with mock.patch.object(supervise, "spawn_supervisor") as spawned, \
                mock.patch.object(observer, "model_turn") as never:
            supervise.supervise(self.root, run_id)
        never.assert_not_called()
        con = db.connect()
        try:
            retry = con.execute("SELECT * FROM runs WHERE retry_of=?",
                                (run_id,)).fetchone()
            self.assertIsNotNone(retry, "the failed run was not retried")
            self.assertEqual(Path(retry["brief_path"]).read_text(),
                             Path(con.execute(
                                 "SELECT brief_path FROM runs WHERE id=?",
                                 (run_id,)).fetchone()["brief_path"]).read_text())
            spawned.assert_any_call(self.root, int(retry["id"]))
            action = observer.observations(con, run_id, layer="retry")[0]
            self.assertEqual(action["action"], "retry")
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
