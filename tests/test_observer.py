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
import contextlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from orchestra import config, db, observer, supervise

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


def with_settings(extra: str) -> str:
    """CONFIG with a line added to [settings] (TOML forbids a second one)."""
    return CONFIG.replace("[settings]\ntimeout = 60",
                          f"[settings]\ntimeout = 60\n{extra}")


class ObserverCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name).resolve()
        self.config_path = self.tmp_path / "config.toml"
        self.config_path.write_text(CONFIG)
        self.env = mock.patch.dict(os.environ, {
            "ORCHESTRA_HOME": str(self.tmp_path / "home"),
            "ORCHESTRA_CONFIG": str(self.config_path)})
        self.env.start()
        self.con = db.connect()

    def tearDown(self) -> None:
        self.con.close()
        self.env.stop()
        self.tmp.cleanup()

    # -- helpers
    def cfg(self) -> dict:
        return config.load(PROJECT_ID)

    def one(self, sql: str, *args):
        return self.con.execute(sql, args).fetchone()

    def run_status(self, run_id: int) -> str:
        return self.one("SELECT status FROM runs WHERE id=?", run_id)["status"]

    def escalation_body(self, run_id: int) -> str:
        return self.one("SELECT body FROM messages WHERE run_id=? AND "
                        "kind='escalation'", run_id)["body"]

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
    def test_profile_resolution_contract(self) -> None:
        """The workhorse tier is the default observer; an explicit setting
        wins; an unknown name is refused; no configured observer says exactly
        what to add. Expected is a profile name, or the markers the
        ObserverUnconfigured message must carry."""
        cases = {
            "tier as number": (CONFIG, "cheap"),
            "tier as name": (CONFIG.replace("tier = 1", 'tier = "cheap"'),
                             "cheap"),
            "explicit setting wins": (
                with_settings('observer_profile = "worker"'), "worker"),
            "unknown named profile refused": (
                with_settings('observer_profile = "ghost"'), []),
            "nothing configured says what to add": (
                '[profiles.worker]\nbackend = "opencode"\n',
                ["observer_profile", "cheap", "no default profiles"]),
        }
        for label, (text, expected) in cases.items():
            with self.subTest(label):
                self.config_path.write_text(text)
                if isinstance(expected, str):
                    self.assertEqual(observer.profile_name(self.cfg()), expected)
                else:
                    with self.assertRaises(observer.ObserverUnconfigured) as caught:
                        observer.profile_name(self.cfg())
                    for marker in expected:
                        self.assertIn(marker, str(caught.exception))

    def test_the_enabled_set_gates_the_observer(self) -> None:
        """W-0187: picking the observer is a staffing moment — it spends a
        model turn on the project's behalf, so a profile the project disabled
        must not be volunteered for it by the back door of the tier scan."""
        self.config_path.write_text(
            CONFIG + f'\n[project."{PROJECT_ID}"]\nenabled_profiles = ["worker"]\n')
        with self.assertRaises(observer.ObserverUnconfigured) as caught:
            observer.profile_name(self.cfg())
        self.assertIn("no observer profile", str(caught.exception))
        # another project, which enabled everything, still finds it
        self.assertEqual(observer.profile_name(config.load("other-uuid")), "cheap")

        # A named observer the project disabled: the NAME still resolves — it
        # is a configured profile — but staffing it does not.
        self.config_path.write_text(
            with_settings('observer_profile = "cheap"')
            + f'\n[project."{PROJECT_ID}"]\nenabled_profiles = ["worker"]\n')
        self.assertEqual(observer.profile_name(self.cfg()), "cheap")
        with self.assertRaises(observer.ObserverUnconfigured) as caught:
            observer.observer_profile(self.cfg())
        message = str(caught.exception)
        self.assertIn(PROJECT_ID, message)
        self.assertIn("Enabled there: worker", message)


class LoopDetectionTests(ObserverCase):
    """Layer (b): zero tokens, read from the NORMALIZED events table."""

    def test_loop_detection_contract(self) -> None:
        """A long productive run is never stopped (hours of varied, honest
        work); the same call repeated back to back is caught, as is one file
        edited over and over; edits broken up by other work are not a loop.
        Each case is (events as add_event triples, expected reason or None)."""
        def bash(cmd):
            return ("tool_call", "bash", json.dumps({"command": cmd}))

        def edit(name, path, n=0):
            return ("tool_call", name, json.dumps({"file_path": path, "new": n}))

        cases = {
            "a long productive run is never stopped": (  # hours of varied work
                [ev for n in range(400) for ev in
                 (bash(f"pytest tests/test_{n}.py"), ("tool_result", "bash", "ok"),
                  edit("edit", f"/src/mod_{n}.py", n))], None),
            "the same call repeated is caught": (
                [bash("npm test")] * observer.TOOL_REPEATS, "same bash call"),
            "one file edited over and over is caught": (
                [edit("Edit", "/src/a.py", n)
                 for n in range(observer.FILE_REPEATS)], "/src/a.py"),
            "edits broken up by other work are not a loop": (
                [ev for n in range(observer.FILE_REPEATS * 2) for ev in
                 (edit("Edit", "/src/a.py", n), bash(f"pytest -k {n}"))], None),
        }
        for label, (events, expected) in cases.items():
            with self.subTest(label):
                run_id = self.make_run()
                for event in events:
                    self.add_event(run_id, *event)
                found = observer.loop_reason(self.con, run_id)
                if expected is None:
                    self.assertIsNone(found)
                    self.assertIsNone(
                        observer.mechanical(self.con, run_id, self.cfg()))
                    self.assertEqual(self.run_status(run_id), "running")
                else:
                    self.assertIsNotNone(found)
                    self.assertIn(expected, found[0])

    def test_first_trip_corrects_and_the_second_stops_with_reasoning(self) -> None:
        run_id = self.make_run(log_path=str(self.tmp_path / "run.jsonl"))
        Path(self.tmp_path / "run.jsonl").write_text("{}\n")
        payload = json.dumps({"command": "npm test"})
        for _ in range(observer.TOOL_REPEATS):
            self.add_event(run_id, "tool_call", "bash", payload)
        first = observer.mechanical(self.con, run_id, self.cfg())
        self.assertEqual(first["action"], "tell")
        queued = self.one("SELECT body FROM messages WHERE run_id=? AND "
                          "kind='interrupt'", run_id)
        self.assertIn("loop", queued["body"])
        # The same tail must not produce a correction every poll.
        self.assertIsNone(observer.mechanical(self.con, run_id, self.cfg()))
        # It keeps spinning anyway: that is feral, so it stops and escalates.
        for _ in range(observer.TOOL_REPEATS):
            self.add_event(run_id, "tool_call", "bash", payload)
        second = observer.mechanical(self.con, run_id, self.cfg())
        self.assertEqual(second["action"], "stop")
        self.assertTrue(second["reason"])
        self.assertEqual(self.run_status(run_id), "killed")
        self.assertIn(second["reason"], self.escalation_body(run_id))
        stops = [o for o in observer.observations(self.con, run_id)
                 if o["action"] == "stop"]
        self.assertEqual(len(stops), 1)
        self.assertTrue(stops[0]["reason"])


class VerdictTests(ObserverCase):
    def test_verdict_parsing_contract(self) -> None:
        """A verdict is read out of chatty output; invalid output never costs
        a run its life."""
        verdict = observer.parse_verdict(
            'Thinking about it...\n{"action": "stop", "reason": "it is stuck"}\n'
            "Hope that helps.")
        self.assertEqual((verdict["action"], verdict["reason"]),
                         ("stop", "it is stuck"))
        for text in ("", "I think it's fine?", "{not json", '{"reason": "x"}',
                     '{"action": "terminate"}'):
            with self.subTest(text=text or "<empty>"):
                self.assertEqual(observer.parse_verdict(text)["action"], "ok")


class ObserverTurnTests(ObserverCase):
    def test_the_turn_is_out_of_band(self) -> None:
        """The worker's session is never touched by an 'ok' look."""
        run_id = self.make_run()
        self.add_event(run_id, "assistant_text", None, "refactoring the parser")
        turn = self.stub("ok", "steady progress on the parser")
        verdict = observer.judge(self.con, run_id, self.cfg(), turn=turn)
        self.assertEqual((verdict["action"], verdict["profile"]),
                         ("ok", "cheap"))
        self.assertIn("LENGTH IS NOT A FAULT", turn.calls[0]["prompt"])
        self.assertIn("refactoring the parser", turn.calls[0]["prompt"])
        observer.apply_verdict(self.con, run_id, verdict, self.cfg())
        # Nothing was injected into the run and nothing was stopped.
        self.assertEqual(self.one("SELECT COUNT(*) AS n FROM messages "
                                  "WHERE run_id=?", run_id)["n"], 0)
        self.assertEqual(self.run_status(run_id), "running")
        self.assertEqual(observer.observations(self.con, run_id)[0]["action"], "ok")

    def test_a_tell_reaches_the_worker_as_an_interrupt(self) -> None:
        run_id = self.make_run(log_path=str(self.tmp_path / "run.jsonl"))
        Path(self.tmp_path / "run.jsonl").write_text("{}\n")
        verdict = observer.judge(self.con, run_id, self.cfg(),
                                 turn=self.stub("tell", "wrong file", "edit b.py"))
        result = observer.apply_verdict(self.con, run_id, verdict, self.cfg())
        self.assertTrue(result["tell"]["queued"])
        self.assertEqual(self.one("SELECT body FROM messages WHERE run_id=?",
                                  run_id)["body"], "edit b.py")

    def test_a_stop_always_escalates_with_its_reasoning(self) -> None:
        run_id = self.make_run()
        verdict = observer.judge(
            self.con, run_id, self.cfg(),
            turn=self.stub("stop", "it has re-read the same file for an hour"))
        result = observer.apply_verdict(self.con, run_id, verdict, self.cfg())
        self.assertTrue(result["escalation"])
        self.assertEqual(self.run_status(run_id), "killed")
        self.assertIn("re-read the same file", self.escalation_body(run_id))
        recorded = observer.observations(self.con, run_id)[-1]
        self.assertEqual(recorded["action"], "stop")
        self.assertIn("re-read the same file", recorded["reason"])

    def test_an_observer_that_cannot_run_never_hurts_the_run(self) -> None:
        self.config_path.write_text('[profiles.worker]\nbackend = "opencode"\n')
        run_id = self.make_run()
        self.add_transcript(run_id)
        watcher = observer.Watcher(run_id, PROJECT_ID)
        watcher._pass()
        self.assertEqual(self.run_status(run_id), "running")
        note = observer.observations(self.con, run_id)[0]
        self.assertEqual(note["action"], "ok")
        self.assertIn("could not run", note["reason"])


class CadenceTests(ObserverCase):
    def test_looks_are_gated_by_status_and_rate(self) -> None:
        """A terminal run is never looked at; a live one is polled at most
        once per POLL_EVERY."""
        done = self.make_run(status="done")
        watcher = observer.Watcher(done, PROJECT_ID,
                                   clock=lambda: time.time() + observer.FIRST_LOOK * 4)
        self.assertFalse(watcher._due(self.con, self.cfg()))

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
        from orchestra import http
        run_id = self.make_run(log_path=str(self.tmp_path / "run.jsonl"))
        Path(self.tmp_path / "run.jsonl").write_text("{}\n")
        self.add_transcript(run_id)
        turn = self.stub("tell", "it is editing the wrong module", "look at b.py")
        with mock.patch.object(observer, "model_turn", turn):
            result = http.check_run(self.con, run_id)
        self.assertEqual(result["observer"]["action"], "tell")
        self.assertIn("wrong module", result["verdict"])
        self.assertEqual(len(turn.calls), 1)

    def test_check_spends_no_model_without_cause(self) -> None:
        """A run with no transcript yet costs nothing, and mechanical-only
        never calls a model; the skip says why, and the mechanical path still
        produces a verdict."""
        from orchestra import http
        cases = {
            "no transcript yet": ({}, "nothing in the trace", False),
            "mechanical only": ({"observe": False}, "mechanical", True),
        }
        for label, (kwargs, marker, has_verdict) in cases.items():
            with self.subTest(label):
                run_id = self.make_run()
                with mock.patch.object(observer, "model_turn") as never:
                    result = http.check_run(self.con, run_id, **kwargs)
                never.assert_not_called()
                self.assertIn(marker, result["observer"]["skipped"])
                if has_verdict:
                    self.assertTrue(result["verdict"])

    def test_an_unconfigured_observer_degrades_to_the_mechanical_verdict(self) -> None:
        from orchestra import http
        self.config_path.write_text('[profiles.worker]\nbackend = "opencode"\n')
        run_id = self.make_run()
        self.add_transcript(run_id)
        result = http.check_run(self.con, run_id)
        self.assertIn("observer_profile", result["observer"]["error"])
        self.assertTrue(result["verdict"])


class RetryTests(ObserverCase):
    AUTH_SUMMARY = ("Failed to authenticate: OAuth session expired and "
                    "could not be refreshed")

    def _brief(self, run_id: int, text: str = "the original mission") -> None:
        from orchestra import paths
        path = paths.briefs_dir() / f"run-{run_id}.md"
        path.write_text(text)
        self.con.execute(
            "UPDATE runs SET brief_path=?, landing_status='ok', "
            "handoff_processed_at=? WHERE id=?", (str(path), db.now(), run_id))
        self.con.execute(
            "INSERT INTO messages(run_id, sender, body, kind, created_at) "
            "SELECT ?, 'orchestra', 'finalized test result', 'completion', ? "
            "WHERE NOT EXISTS (SELECT 1 FROM messages WHERE run_id=? "
            "AND kind='completion')", (run_id, db.now(), run_id))
        self.con.commit()

    @contextlib.contextmanager
    def _nod_stubbed(self):
        """The auth escalation must page through nod.alert, never nod.failure."""
        with mock.patch.object(observer.nod, "from_cfg", return_value=object()), \
                mock.patch.object(observer.nod, "alert",
                                  return_value={"request_id": "auth-alert"}) as alert, \
                mock.patch.object(observer.nod, "failure") as failure:
            yield alert, failure

    def retries_of(self, run_id: int) -> int:
        return self.one("SELECT COUNT(*) AS n FROM runs WHERE retry_of=?",
                        run_id)["n"]

    def _add_waiter(self, depends_on: int, mission: str) -> int:
        """A pending run whose dispatch waits on ``depends_on``."""
        dependent = self.make_run(status="pending")
        self.con.execute("INSERT INTO dispatch_dependencies(run_id, depends_on_run) "
                         "VALUES(?,?)", (dependent, depends_on))
        self.con.execute(
            "INSERT INTO deferred_dispatches(run_id, mission, use_worktree, "
            "created_at) VALUES(?, ?, 0, ?)", (dependent, mission, db.now()))
        self.con.commit()
        return dependent

    def _finish_and_release(self, done_id: int, dependent: int) -> None:
        """Finishing the current owner fires the waiter; nothing replays."""
        self.con.execute("UPDATE runs SET status='done', finished_at=? WHERE id=?",
                         (db.now(), done_id))
        self.con.commit()
        released = []
        self.assertEqual(supervise.process_ready(
            self.con, launcher=lambda root, rid: released.append(rid)),
            [{"run_id": dependent, "status": "fired"}])
        self.assertEqual(released, [dependent])
        self.assertEqual(observer.resume_deferred_retries(
            self.con, self.cfg(), launcher=lambda root, rid: None), [])

    def test_an_infrastructure_failure_is_retried_once_with_the_same_brief(self) -> None:
        run_id = self.make_run(status="failed", work_item="W-0001")
        self._brief(run_id)
        launched = []
        result = observer.after_terminal(self.con, run_id,
                                         launcher=lambda root, rid: launched.append(rid))
        self.assertEqual(result["action"], "retry")
        retry = self.one("SELECT * FROM runs WHERE id=?", result["run"])
        self.assertEqual(
            (retry["retry_of"], retry["work_item"], retry["session_ref"],
             Path(retry["brief_path"]).read_text()),
            (run_id, "W-0001", None, "the original mission"),
            "a retry is a fresh run with the same brief, not a resume")
        self.assertEqual(launched, [result["run"]])
        replay = observer.after_terminal(
            self.con, run_id,
            launcher=lambda root, rid: self.fail("settled retry replayed"))
        self.assertEqual(replay["action"], "none")
        self.assertIn("already settled", replay["reason"])
        self.assertEqual(self.retries_of(run_id), 1)

    def test_expired_authentication_requires_a_changed_precondition(self) -> None:
        run_id = self.make_run(status="failed", backend="claude",
                               work_item="W-auth", summary=self.AUTH_SUMMARY)
        self._brief(run_id)
        dependent = self._add_waiter(run_id, "continue after auth")
        launched = []

        with self._nod_stubbed() as (alert, failure):
            result = observer.after_terminal(
                self.con, run_id, launcher=lambda root, rid: launched.append(rid))

        self.assertEqual(
            (result["action"], result["precondition"], result["escalation"]["nod"]),
            ("escalate", "reauthenticate", "auth-alert"))
        alert.assert_called_once()
        failure.assert_not_called()
        self.assertEqual(launched, [])
        self.assertEqual(self.retries_of(run_id), 0)
        observation = observer.observations(self.con, run_id, layer="retry")[0]
        self.assertEqual(observation["action"], "escalate")
        self.assertIn("reauthenticate Claude", observation["reason"])
        message = self.escalation_body(run_id)
        self.assertIn("Reauthenticate Claude", message)
        self.assertNotIn("choose Retry", message)
        self.assertEqual(supervise.process_ready(
            self.con, launcher=lambda root, rid: launched.append(rid)),
            [{"run_id": dependent, "status": "declined"}])
        self.assertEqual(launched, [])

    def test_transient_retries_survive_auth_noise(self) -> None:
        """Auth history on the item, or mere credential wording in the
        summary, must not spend the one transient retry."""
        def auth_history():
            auth = self.make_run(status="failed", backend="claude",
                                 work_item="W-auth-history",
                                 summary=self.AUTH_SUMMARY)
            self._brief(auth)
            observer.after_terminal(self.con, auth, launcher=lambda root, rid: None)
            later = self.make_run(status="failed", backend="claude",
                                  work_item="W-auth-history",
                                  summary="connection reset")
            self._brief(later)
            return later

        def credential_wording():
            run_id = self.make_run(status="failed", backend="claude",
                                   summary="Need credentials.")
            self._brief(run_id)
            return run_id

        cases = {"auth history does not spend a later transient": auth_history,
                 "ordinary credential language": credential_wording}
        for label, build in cases.items():
            with self.subTest(label):
                launched = []
                result = observer.after_terminal(
                    self.con, build(),
                    launcher=lambda root, rid: launched.append(rid))
                self.assertEqual(result["action"], "retry")
                self.assertEqual(launched, [result["run"]])

    def test_auth_on_a_retry_escalates_before_the_two_failure_rule(self) -> None:
        first = self.make_run(
            status="failed", backend="claude", summary="connection reset")
        self._brief(first)
        retry_id = observer.after_terminal(
            self.con, first, launcher=lambda root, rid: None)["run"]
        self.con.execute(
            "UPDATE runs SET status='failed', summary=? WHERE id=?",
            (self.AUTH_SUMMARY, retry_id))
        self.con.commit()

        with self._nod_stubbed() as (alert, failure):
            result = observer.after_terminal(
                self.con, retry_id, launcher=lambda root, rid: self.fail("launched"))

        self.assertEqual(result["precondition"], "reauthenticate")
        alert.assert_called_once()
        failure.assert_not_called()
        self.assertEqual(self.retries_of(retry_id), 0)

    def test_a_second_consecutive_failure_escalates_instead(self) -> None:
        from orchestra import dispatch
        first = self.make_run(status="failed", work_item="W-0002")
        self._brief(first)
        retry_id = observer.after_terminal(
            self.con, first, launcher=lambda root, rid: None)["run"]
        self.con.execute("UPDATE runs SET status='timeout', summary='died again' "
                         "WHERE id=?", (retry_id,))
        self.con.commit()
        dispatch.pause(self.con, "admission only")
        launched = []
        result = observer.after_terminal(self.con, retry_id,
                                         launcher=lambda root, rid: launched.append(rid))
        self.assertEqual(result["action"], "escalate")
        self.assertEqual(launched, [], "nothing spends a third attempt")
        self.assertEqual(result["streak"], 2)
        body = self.escalation_body(retry_id)
        self.assertIn("Two infrastructure failures", body)
        self.assertIn("W-0002", body)

    CAPACITY_SUMMARY = ("The model is currently at capacity due to high "
                        "demand. Please try again in a few minutes.")

    def test_a_provider_at_capacity_keeps_its_turn_past_the_two_failure_rule(self) -> None:
        """PREX3 run 64: xAI was full, the retry was full too, and the item
        was escalated after two attempts. Nothing about the brief was wrong —
        only another attempt can clear a busy provider, so the count rule
        gives way to a clock."""
        first = self.make_run(status="failed", work_item="W-cap",
                              summary=self.CAPACITY_SUMMARY)
        self._brief(first)
        second = self.make_run(status="failed", work_item="W-cap",
                               retry_of=first, summary=self.CAPACITY_SUMMARY)
        self._brief(second)
        self.assertGreaterEqual(observer.infra_streak(self.con, self.one(
            "SELECT * FROM runs WHERE id=?", second)), 2,
            "the streak alone would have escalated")
        result = observer.after_terminal(
            self.con, second,
            launcher=lambda root, rid: self.fail("a full provider was spun"))
        self.assertEqual(result["action"], "waiting")
        waiting = self.one(
            "SELECT * FROM observations WHERE run_id=? AND layer='retry' "
            "AND action='waiting' ORDER BY id DESC", second)
        self.assertIn("capacity window", waiting["reason"])
        # Scheduled, not spun: the row says when, and the wait grows.
        self.assertGreater(json.loads(waiting["detail"])["not_before"], db.now())
        self.assertEqual(60, observer.capacity_delay(1))
        self.assertEqual(120, observer.capacity_delay(2))
        self.assertEqual(observer.CAPACITY_BACKOFF_MAX_S,
                         observer.capacity_delay(9), "the wait is capped")

    def test_a_scheduled_capacity_retry_fires_when_it_is_due(self) -> None:
        """The daemon's own resume sweep is the clock: it passes over a
        waiting row until its time comes, then makes the attempt."""
        run_id = self.make_run(status="failed", work_item="W-cap3",
                               summary=self.CAPACITY_SUMMARY)
        self._brief(run_id)
        self.assertEqual("waiting", observer.after_terminal(
            self.con, run_id,
            launcher=lambda root, rid: self.fail("spun"))["action"])

        early = observer.resume_deferred_retries(
            self.con, launcher=lambda root, rid: self.fail("fired too early"))
        self.assertEqual([], early, "not due yet, so nothing happens")

        # Time passes.
        self.con.execute(
            "UPDATE observations SET detail=? WHERE run_id=? AND action='waiting'",
            (json.dumps({"not_before": "2026-01-01T00:00:00Z", "streak": 1}),
             run_id))
        self.con.commit()
        launched = []
        due = observer.resume_deferred_retries(
            self.con, launcher=lambda root, rid: launched.append(rid))
        self.assertEqual(["retry"], [d["action"] for d in due])
        self.assertEqual(launched, [due[0]["run"]])

    def test_a_provider_full_for_the_whole_window_hands_over_to_a_human(self) -> None:
        """The window is measured from the FIRST refusal, so a provider that
        stays full cannot extend its own deadline. When it runs out, a human
        decides — retry later, or staff the item somewhere else."""
        old_start = "2026-08-25T00:00:00Z"
        first = self.make_run(status="failed", work_item="W-cap2",
                              started_at=old_start, summary=self.CAPACITY_SUMMARY)
        self._brief(first)
        second = self.make_run(status="failed", work_item="W-cap2",
                               retry_of=first, started_at=old_start,
                               summary=self.CAPACITY_SUMMARY)
        self._brief(second)
        result = observer.after_terminal(
            self.con, second,
            launcher=lambda root, rid: self.fail("a spent window must not retry"))
        self.assertEqual(result["action"], "escalate")
        self.assertIn("capacity window", result["reason"])
        self.assertEqual(self.retries_of(second), 0)

    def test_ordinary_failures_still_stop_at_two(self) -> None:
        """The exception is capacity alone: a run that failed for its own
        reasons keeps the count rule exactly as it was."""
        first = self.make_run(status="failed", work_item="W-plain",
                              summary="Traceback: something broke")
        self._brief(first)
        second = self.make_run(status="failed", work_item="W-plain",
                               retry_of=first, summary="Traceback: again")
        self._brief(second)
        result = observer.after_terminal(
            self.con, second,
            launcher=lambda root, rid: self.fail("a third attempt was spent"))
        self.assertEqual(result["action"], "escalate")
        self.assertIn("nothing spends a third", result["reason"])

    def test_a_checkpoint_failure_is_not_retried_at_all(self) -> None:
        """PREX3 runs 93, 94, and 99 each finished their work, then failed
        because git would not read the checkout. The retry reran fourteen
        minutes of identical work into the identical objection, twice
        automatically and once by hand. Nothing an identical run does can
        change what git objects to, so it escalates naming the fix."""
        run_id = self.make_run(
            status="failed", work_item="W-ckpt",
            summary="Checkpoint error: cannot read worktree status: error: "
                    "expected submodule path 'vendor/lib' not to be a "
                    "symbolic link\n\nAdmitted. The cursor moves.")
        self._brief(run_id)
        result = observer.after_terminal(
            self.con, run_id,
            launcher=lambda root, rid: self.fail("a checkpoint error was retried"))
        self.assertEqual(result["action"], "escalate")
        self.assertIn("could not be checkpointed", result["reason"])
        self.assertIn("symbolic link", result["reason"])
        self.assertEqual(self.retries_of(run_id), 0)

    def test_non_infrastructure_outcomes_are_never_retried(self) -> None:
        for status in ("done", "killed", "halted"):
            with self.subTest(status=status):
                run_id = self.make_run(status=status)
                self.assertEqual(
                    observer.after_terminal(self.con, run_id)["action"], "none")

    def test_a_deliberate_stop_is_never_retried(self) -> None:
        run_id = self.make_run(status="failed")
        self._brief(run_id)
        self.assertTrue(observer.defer_retry(self.con, run_id))
        observer.record(self.con, run_id, "observer", "stop", "it went feral")
        self.con.commit()
        result = observer.after_terminal(self.con, run_id,
                                         launcher=lambda root, rid: 1 / 0)
        self.assertEqual(result["action"], "none")
        self.assertIn("it went feral", self.one(
            "SELECT summary FROM runs WHERE id=?", run_id)["summary"])
        self.assertEqual([row["action"] for row in observer.observations(
            self.con, run_id, layer="retry")], ["deferred", "cancelled"])

    def test_a_paused_dispatch_defers_the_retry(self) -> None:
        from orchestra import dispatch
        run_id = self.make_run(status="failed")
        self._brief(run_id)
        dependent = self._add_waiter(run_id, "continue after retry")
        dispatch.pause(self.con, "maintenance")
        result = observer.after_terminal(self.con, run_id,
                                         launcher=lambda root, rid: 1 / 0)
        self.assertEqual(result["action"], "none")
        self.assertIn("paused", result["reason"])
        self.assertEqual(supervise.process_ready(
            self.con, launcher=lambda root, rid: 1 / 0), [])
        waiting = self.con.execute(
            "SELECT r.status, d.status AS deferred_status, e.depends_on_run "
            "FROM runs r JOIN deferred_dispatches d ON d.run_id=r.id "
            "JOIN dispatch_dependencies e ON e.run_id=r.id WHERE r.id=?",
            (dependent,)).fetchone()
        self.assertEqual((waiting["status"], waiting["deferred_status"],
                          waiting["depends_on_run"]),
                         ("pending", "pending", run_id))
        dispatch.resume(self.con)
        launched = []
        resumed = observer.resume_deferred_retries(
            self.con, self.cfg(), launcher=lambda root, rid: launched.append(rid))
        self.assertEqual(resumed[0]["action"], "retry")
        self.assertEqual(launched, [resumed[0]["run"]])
        retry_id = resumed[0]["run"]
        edge = self.one("SELECT depends_on_run FROM dispatch_dependencies "
                        "WHERE run_id=?", dependent)
        self.assertEqual(edge["depends_on_run"], retry_id)
        self._finish_and_release(retry_id, dependent)

        # Another dispatcher can claim the item after the pause lifts but
        # before the daemon replays its deferred retry. The waiter must follow
        # that winning run; the old retry decision must not replay forever.
        launched.clear()
        failed = self.make_run(status="failed", work_item="W-COMPETING")
        self._brief(failed)
        dependent = self._add_waiter(failed, "follow the winner")
        dispatch.pause(self.con, "maintenance")
        observer.after_terminal(self.con, failed,
                                launcher=lambda root, rid: launched.append(rid))
        dispatch.resume(self.con)
        winner, blocked = supervise.create_run(
            self.con, profile="worker", backend="opencode",
            requested_by="human", workdir=str(self.tmp_path),
            project_id=PROJECT_ID, status="running", work_item="W-COMPETING")
        self.assertIsNone(blocked)
        self.con.execute("UPDATE runs SET status='done', finished_at=? WHERE id=?",
                         (db.now(), winner["id"]))
        self.con.commit()

        resumed = observer.resume_deferred_retries(
            self.con, self.cfg(), launcher=lambda root, rid: launched.append(rid))
        self.assertEqual(len(resumed), 1)
        self.assertEqual(resumed[0]["action"], "none")
        self.assertIn(f"work_item:{winner['id']}", resumed[0]["reason"])
        self.assertEqual(launched, [])
        self.assertEqual(self.one(
            "SELECT depends_on_run FROM dispatch_dependencies WHERE run_id=?",
            dependent)["depends_on_run"], winner["id"])
        retry_notes = observer.observations(self.con, failed, layer="retry")
        self.assertEqual([note["action"] for note in retry_notes],
                         ["deferred", "superseded"])
        self.assertEqual(json.loads(retry_notes[-1]["detail"])["winning_run"],
                         winner["id"])
        self.assertEqual(self.retries_of(failed), 0)
        self._finish_and_release(winner["id"], dependent)

    def test_dependents_wait_on_the_retry_instead_of_being_declined(self) -> None:
        first = self.make_run(status="running")
        self._brief(first)
        dependent = self.make_run(status="pending")
        self.con.execute("INSERT INTO dispatch_dependencies(run_id, depends_on_run) "
                         "VALUES(?,?)", (dependent, first))
        self.con.execute("UPDATE runs SET status='failed', finished_at=? WHERE id=?",
                         (db.now(), first))
        self.assertTrue(observer.defer_retry(self.con, first))
        self.con.commit()
        other = db.connect()
        try:
            self.assertEqual(supervise.process_ready(
                other, launcher=lambda root, rid: self.fail("released early")), [])
        finally:
            other.close()
        retry_id = observer.after_terminal(
            self.con, first, launcher=lambda root, rid: None)["run"]
        edge = self.one("SELECT depends_on_run FROM dispatch_dependencies "
                        "WHERE run_id=?", dependent)
        self.assertEqual(edge["depends_on_run"], retry_id)
        self.assertEqual([note["action"] for note in observer.observations(
            self.con, first, layer="retry")], ["deferred", "retry"])

    def test_blocked_retry_chases_the_winners_current_owner(self) -> None:
        failed = self.make_run(status="failed", work_item="W-RACE")
        self._brief(failed)
        dependent = self._add_waiter(failed, "follow the final owner")
        self.assertTrue(observer.defer_retry(self.con, failed))
        self.con.commit()
        winner = self.make_run(status="failed", work_item="W-RACE")
        real_create = supervise.create_run
        replacements = []

        def settle_winner_before_repoint(con, **kwargs):
            blocked = real_create(con, **kwargs)
            self.assertEqual(blocked[1], f"work_item:{winner}")
            retry = self.make_run(
                status="failed", work_item="W-RACE", retry_of=winner)
            current = self.make_run(status="running", work_item="W-RACE")
            # Both ownership passes finish before the old failed run adds its
            # waiter to the winner: the ordering that exposed the race.
            observer._repoint_dependents(con, winner, retry)
            observer.record(con, winner, "retry", "retry", detail={
                "retry_run": retry})
            observer._repoint_dependents(con, retry, current)
            observer.record(con, retry, "retry", "superseded", detail={
                "winning_run": current})
            con.commit()
            replacements.extend((retry, current))
            return blocked

        with mock.patch.object(supervise, "create_run",
                               side_effect=settle_winner_before_repoint):
            result = observer.after_terminal(
                self.con, failed,
                launcher=lambda root, rid: self.fail(f"launched run {rid}"))

        current = replacements[-1]
        self.assertEqual(result["action"], "none")
        self.assertIn(f"work_item:{current}", result["reason"])
        edges = list(self.con.execute(
            "SELECT depends_on_run FROM dispatch_dependencies WHERE run_id=?",
            (dependent,)))
        self.assertEqual([row["depends_on_run"] for row in edges], [current])
        decision = observer.observations(self.con, failed, layer="retry")[-1]
        self.assertEqual(decision["action"], "superseded")
        self.assertEqual(json.loads(decision["detail"])["winning_run"], current)

    def test_a_retry_re_homes_when_the_failed_runs_worktree_is_gone(self) -> None:
        """Live failure (run 28): the automatic retry of run 27 copied the
        failed run's workdir and branch into the new row with no existence
        check. Cleanup had already released that worktree (DESIGN §2) and the
        merge had deleted the branch (§9), so `Popen` died with
        FileNotFoundError — the same death as run 9, through another door."""
        import subprocess

        from orchestra import project

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
                               workdir=str(released), branch="orchestra/run-1")
        self._brief(run_id)
        self.assertFalse(released.exists())

        launched = []
        result = observer.after_terminal(
            self.con, run_id, launcher=lambda root, rid: launched.append((root, rid)))
        self.assertEqual(result["action"], "retry")
        retry = self.one("SELECT * FROM runs WHERE id=?", result["run"])
        self.assertNotEqual(retry["workdir"], str(released))
        self.assertTrue(Path(retry["workdir"]).exists(),
                        "a retry must have somewhere to stand")
        self.assertEqual(retry["branch"], f"orchestra/run-{result['run']}",
                         "the actual retry id owns the replacement branch")
        self.assertEqual(launched, [(checkout, result["run"])])

    def test_a_retry_keeps_a_workdir_that_is_still_there(self) -> None:
        run_id = self.make_run(status="failed", workdir=str(self.tmp_path),
                               branch="orchestra/run-1")
        self._brief(run_id)
        retry_id = observer.after_terminal(
            self.con, run_id, launcher=lambda root, rid: None)["run"]
        retry = self.one("SELECT workdir, branch FROM runs WHERE id=?", retry_id)
        self.assertEqual((retry["workdir"], retry["branch"]),
                         (str(self.tmp_path), "orchestra/run-1"))


class PlannerSeamTests(ObserverCase):
    def test_bad_work_is_deferred_to_a_planner_and_escalated(self) -> None:
        run_id = self.make_run(status="done")
        result = observer.planner_review(self.con, run_id,
                                         "the tests it added never assert anything")
        self.assertEqual(result["action"], "deferred")
        recorded = observer.observations(self.con, run_id, layer="planner")
        self.assertEqual(len(recorded), 1)
        body = self.escalation_body(run_id)
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
        self.assertEqual((state["enabled"], state["profile"], state["first_look"]),
                         (True, "cheap", observer.FIRST_LOOK))
        self.assertIn("cheap", "\n".join(observer.status_report(self.cfg())))
        # ...and the first look stays configurable.
        self.config_path.write_text(with_settings("observer_first_look = 45"))
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


class AuthOutageTurnTests(ObserverCase):
    """A turn whose reply is the harness's own auth error is not a judgment
    (2026-08-25): recording it as a success ran the router and observer
    blind for hours. The turn records as failed, the backend's outage flag
    is set for the banner, and the next clean turn clears it."""

    AUTH_TEXT = ("Failed to authenticate: OAuth session expired and could "
                 "not be refreshed")

    def _turn(self, reply: str):
        proc = mock.Mock(returncode=0, stdout="{}", stderr="")
        with mock.patch.object(observer.subprocess, "run",
                               return_value=proc), \
             mock.patch.object(observer.runners, "parse_log",
                               return_value=(None, reply)):
            return observer.model_turn(
                {"backend": "claude", "model": "opus"}, "judge this",
                layer="router", con=self.con)

    def test_an_auth_reply_records_a_failed_turn_and_flags_the_backend(self):
        with self.assertRaises(observer.ObserverTurnError) as caught:
            self._turn(self.AUTH_TEXT)
        self.assertIn("reauthenticate", str(caught.exception))
        turn = self.one("SELECT * FROM runs WHERE layer='router'")
        self.assertEqual(turn["status"], "failed")
        self.assertIn("cannot authenticate", turn["summary"])
        self.assertTrue(db.meta_get(self.con, "auth_outage:claude"))

        self.assertEqual(self._turn('{"profile": "a"}'), '{"profile": "a"}')
        self.assertFalse(db.meta_get(self.con, "auth_outage:claude"))

    def test_a_control_turn_inherits_profile_env(self) -> None:
        proc = mock.Mock(returncode=0, stdout="{}", stderr="")
        profile = {
            "name": "proxy", "backend": "claude",
            "env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8080"},
        }
        with mock.patch.object(observer.subprocess, "run",
                               return_value=proc) as run, \
             mock.patch.object(observer.runners, "parse_log",
                               return_value=(None, "ok")):
            observer.model_turn(profile, "judge this",
                                layer="observer", con=self.con)
        self.assertEqual(
            run.call_args.kwargs["env"]["ANTHROPIC_BASE_URL"],
            "http://127.0.0.1:8080")


class SupervisedRunTests(unittest.TestCase):
    """The seams as the supervisor actually reaches them, against a stub
    backend binary. No model is dispatched anywhere in here."""

    def setUp(self) -> None:
        from orchestra import project
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
            "ORCHESTRA_CONFIG": str(self.config_path),
            "ORCHESTRA_HOME": str(self.tmp_path / "home"),
            "ORCHESTRA_ROOT": str(self.root),
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
        from orchestra import cli, supervise
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
        from orchestra import supervise
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
        from orchestra import supervise
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
        from orchestra import supervise
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
            actions = [note["action"] for note in observer.observations(
                con, run_id, layer="retry")]
            self.assertEqual(actions, ["deferred", "retry"])
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
