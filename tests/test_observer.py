import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from orchestra import attention, callbacks, db, messaging, observer, retry


class FleetSliceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "v2.db"
        self.con = db.connect(self.db_path)
        now = db.now()
        self.con.execute(
            "INSERT INTO runtimes(runtime_id,slug,name,adapter,created_at,updated_at) "
            "VALUES('runtime','runtime','Runtime','exec',?,?)", (now, now),
        )
        self.con.execute(
            "INSERT INTO profiles(profile_id,slug,name,runtime_id,tier,created_at,"
            "updated_at) VALUES('worker','worker','Worker','runtime',1,?,?)",
            (now, now),
        )
        self.con.execute(
            "INSERT INTO runs(request_id,profile_id,runtime_id,title,"
            "mission,requested_by,status,queued_at,started_at,workdir,"
            "cwd,cwd_source,isolation,profile_snapshot,runtime_snapshot,request_snapshot) "
            "VALUES('request-1','worker','runtime','Demo','Do the work',"
            "'operator','running',?,?,?,?,'run','auto','{}','{}','{}')",
            (now, "2026-01-01T00:00:00Z", str(self.root), str(self.root)),
        )
        self.run_id = int(self.con.execute(
            "SELECT id FROM runs WHERE request_id='request-1'").fetchone()["id"])
        self.con.commit()

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def add_events(self, start: int, stop: int):
        self.con.executemany(
            "INSERT INTO events(run_id,seq,kind,name,payload,created_at) "
            "VALUES(?,?,'tool_call','read',?,?)",
            ((self.run_id, seq, json.dumps({"path": f"file-{seq}"}), db.now())
             for seq in range(start, stop)),
        )
        self.con.commit()


class AttentionTests(FleetSliceCase):
    def test_first_authorized_answer_wins_and_losers_are_audited(self):
        request, created = attention.open_request(
            self.con, kind="question", title="Choose", body="Which path?",
            created_by="worker", run_id=self.run_id, blocking=True,
            choices=["left", "right"], correlation_id="question-1",
        )
        self.assertTrue(created)
        run = self.con.execute(
            "SELECT status,waiting_kind FROM runs WHERE id=?", (self.run_id,)
        ).fetchone()
        self.assertEqual((run["status"], run["waiting_kind"]),
                         ("waiting", "input"))
        duplicate, created = attention.open_request(
            self.con, kind="question", title="Choose", body="Which path?",
            created_by="worker", run_id=self.run_id, blocking=True,
            choices=["left", "right"], correlation_id="question-1",
        )
        self.assertFalse(created)
        self.assertEqual(duplicate["id"], request["id"])

        winner = attention.answer(
            self.con, request["id"], actor="device-a",
            response={"choice": "left", "body": "Use the safe path"},
            authorized=True,
        )
        loser = attention.answer(
            self.con, request["id"], actor="device-b",
            response={"choice": "right"}, authorized=True,
        )
        self.assertTrue(winner["accepted"])
        self.assertFalse(loser["accepted"])
        self.assertEqual(self.con.execute(
            "SELECT SUM(accepted) AS accepted,COUNT(*) AS total "
            "FROM attention_responses WHERE attention_id=?", (request["id"],)
        ).fetchone()["accepted"], 1)
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) AS n FROM attention_responses WHERE attention_id=?",
            (request["id"],)).fetchone()["n"], 2)
        thread = messaging.thread(self.con, self.run_id)
        self.assertEqual([message["kind"] for message in thread],
                         ["question", "answer"])
        self.assertEqual([message["kind"] for message in messaging.outbox(
            self.con)], ["question"])
        self.assertEqual([row["kind"] for row in messaging.claim_pending(
            self.con, self.run_id)], ["answer"])

    def test_simultaneous_authorized_answers_have_one_winner(self):
        request, _ = attention.open_request(
            self.con, kind="question", title="Race", body="Answer once",
            created_by="worker", run_id=self.run_id, blocking=True,
            correlation_id="question-race",
        )
        barrier = threading.Barrier(3)
        results, errors = [], []

        def respond(actor):
            con = db.connect(self.db_path)
            try:
                barrier.wait()
                result = attention.answer(
                    con, request["id"], actor=actor,
                    response={"body": actor}, authorized=True)
                results.append(result["accepted"])
            except BaseException as exc:  # surfaced by the parent assertion
                errors.append(exc)
            finally:
                con.close()

        threads = [threading.Thread(target=respond, args=(actor,))
                   for actor in ("device-a", "device-b")]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(5)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [False, True])

    def test_unauthorized_answer_and_blocking_proposal_are_rejected(self):
        request, _ = attention.open_request(
            self.con, kind="profile_proposal", title="Lower effort",
            body="Use low effort", created_by="worker", run_id=self.run_id,
            proposal={"effort": "low"}, correlation_id="proposal-1",
        )
        with self.assertRaises(PermissionError):
            attention.answer(self.con, request["id"], actor="reader",
                             response={"choice": "approve"}, authorized=False)
        with self.assertRaises(attention.AttentionError):
            attention.open_request(
                self.con, kind="profile_proposal", title="Bad", body="Bad",
                created_by="worker", run_id=self.run_id, blocking=True,
                proposal={"effort": "low"}, correlation_id="proposal-2")
        self.assertIsNone(request["deadline"], "attention has no default expiry")

    def test_explicit_deadline_uses_explicit_fallback(self):
        request, _ = attention.open_request(
            self.con, kind="question", title="Wait?", body="Continue?",
            created_by="worker", run_id=self.run_id, blocking=True,
            fallback={"body": "Continue conservatively"},
            deadline="2026-01-01T00:00:00Z", correlation_id="deadline-1",
        )
        self.assertEqual(attention.apply_due_fallbacks(
            self.con, at="2026-01-01T00:00:01Z"), [request["id"]])
        resolved = self.con.execute(
            "SELECT status,resolved_by FROM attention_requests WHERE id=?",
            (request["id"],)).fetchone()
        self.assertEqual((resolved["status"], resolved["resolved_by"]),
                         ("resolved", "deadline"))


class MessagingTests(FleetSliceCase):
    def test_tell_is_claimed_once_and_rendered(self):
        message_id = messaging.queue_tell(
            self.con, self.run_id, "operator", "Change direction")
        claimed = messaging.claim_pending(self.con, self.run_id)
        self.assertEqual([row["id"] for row in claimed], [message_id])
        self.assertIn("Change direction", messaging.render_delivery(claimed))
        self.assertEqual(messaging.claim_pending(self.con, self.run_id), [])
        status = self.con.execute(
            "SELECT status FROM messages WHERE id=?", (message_id,)).fetchone()
        self.assertEqual(status["status"], "delivered")

    def test_pending_inbound_message_cannot_target_a_terminal_run(self):
        self.con.execute("UPDATE runs SET status='completed' WHERE id=?",
                         (self.run_id,))
        self.con.commit()
        with self.assertRaises(messaging.RunClosed):
            messaging.queue_tell(self.con, self.run_id, "operator", "Too late")


class CallbackTests(unittest.TestCase):
    def test_callback_is_argv_json_stdin_without_a_shell(self):
        seen = {}

        def launch(command, **kwargs):
            seen["command"] = command
            seen["payload"] = json.loads(kwargs["stdin"].read())
            seen["kwargs"] = kwargs
            return object()

        with mock.patch("orchestra.callbacks.subprocess.Popen", side_effect=launch):
            self.assertTrue(callbacks.emit(
                ["notify", "--quiet"], "run.terminal", {"run_id": 8}))
        self.assertEqual(seen["command"], ("notify", "--quiet"))
        self.assertEqual(seen["payload"]["event"], "run.terminal")
        self.assertEqual(seen["payload"]["data"], {"run_id": 8})
        self.assertNotIn("shell", seen["kwargs"])

    def test_callback_rejects_unbounded_event_vocabulary(self):
        with self.assertRaises(ValueError):
            callbacks.envelope("run.started", {})


class ObserverTests(FleetSliceCase):
    def test_check_is_bounded_redacted_and_corrects_before_stopping(self):
        self.add_events(1, 7)
        is_due, reason = observer.due(
            self.con, self.run_id, at="2026-01-01T00:10:00Z")
        self.assertTrue(is_due, reason)
        prepared = observer.prepare_check(
            self.con, self.run_id, profile_id="worker",
            profile_snapshot={"model": "small", "api_key": "secret"})
        self.assertEqual(prepared["input"]["event_count"], 6)
        self.assertNotIn(str(self.root), prepared["prompt"])
        stored = self.con.execute(
            "SELECT profile_snapshot FROM observer_checks WHERE id=?",
            (prepared["check_id"],)).fetchone()["profile_snapshot"]
        self.assertEqual(json.loads(stored)["api_key"], "[redacted]")

        first = observer.finish_check(
            self.con, prepared["check_id"],
            '{"action":"stop","reason":"wandering","message":"refocus"}')
        self.assertEqual(first["action"], "tell")

        self.add_events(7, 9)
        second = observer.prepare_check(
            self.con, self.run_id, profile_id="worker", trigger="manual")
        stopped = observer.finish_check(
            self.con, second["check_id"],
            {"action": "stop", "reason": "ignored correction", "message": ""})
        self.assertEqual(stopped["action"], "stop")

    def test_no_new_evidence_means_no_scheduled_check(self):
        self.add_events(1, 7)
        prepared = observer.prepare_check(
            self.con, self.run_id, profile_id="worker", trigger="manual")
        observer.finish_check(self.con, prepared["check_id"],
                              {"action": "ok", "reason": "working"})
        due, reason = observer.due(
            self.con, self.run_id, at="2030-01-01T00:00:00Z")
        self.assertFalse(due)
        self.assertEqual(reason, "no new evidence")

    def test_malformed_observer_output_is_safe(self):
        self.assertEqual(observer.parse_verdict("not json")["action"], "ok")

    def test_stop_publishes_inbox_alert_and_callback(self):
        with mock.patch("orchestra.observer.callbacks.emit") as emit:
            request = observer.publish_stop(
                self.con, run_id=self.run_id, check_id=44,
                reason="ignored a correction", callback_command=["notify"])
        self.assertEqual(request["kind"], "alert")
        emit.assert_called_once()
        self.assertEqual(emit.call_args.args[1], "observer.stopped")


class RetryTests(unittest.TestCase):
    def test_only_one_known_transient_failure_retries(self):
        self.assertEqual(retry.decide(
            "failed", "service unavailable")["action"], "retry")
        self.assertEqual(retry.decide(
            "failed", "service unavailable", automatic_retries=1)["action"],
            "alert")
        self.assertEqual(retry.decide(
            "failed", "invalid API key")["classification"], "auth")
        self.assertEqual(retry.decide("failed", "tests failed")["action"],
                         "alert")
        self.assertEqual(retry.decide("stopped")["action"], "none")


if __name__ == "__main__":
    unittest.main()
