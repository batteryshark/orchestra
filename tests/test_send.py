from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestra_cli import cli, db


class SendFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".orchestra").mkdir()
        db.connect(self.root).close()

        self.original_find_root = cli.paths.find_root
        self.original_config_load = cli.config.load
        cli.paths.find_root = lambda: self.root  # type: ignore[assignment]
        cli.config.load = lambda _root: {  # type: ignore[assignment]
            "settings": {"default_requester": "orchestrator"},
            "agents": {
                "glm": {"backend": "opencode"},
                "reviewer": {"backend": "opencode"},
            },
        }

    def tearDown(self) -> None:
        cli.paths.find_root = self.original_find_root  # type: ignore[assignment]
        cli.config.load = self.original_config_load  # type: ignore[assignment]
        self.tmp.cleanup()

    def _run_main(self, argv: list[str]) -> tuple[int, str, str]:
        original_argv = sys.argv
        stdout, stderr = io.StringIO(), io.StringIO()
        code = 0
        sys.argv = ["orchestra", *argv]
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                cli.main()
        except SystemExit as exc:
            code = int(exc.code) if isinstance(exc.code, int) else 1
            if not isinstance(exc.code, int) and exc.code:
                print(exc.code, file=stderr)
        finally:
            sys.argv = original_argv
        return code, stdout.getvalue(), stderr.getvalue()

    def _messages(self) -> list[dict]:
        con = db.connect(self.root)
        try:
            return [dict(row) for row in con.execute("SELECT * FROM messages ORDER BY id")]
        finally:
            con.close()

    def _insert_active_run(
        self,
        *,
        agent: str,
        slug: str,
        session_ref: str | None = "ready",
        supervisor_protocol: int = 1,
    ) -> int:
        log_path = self.root / f"{slug}.jsonl"
        log_path.touch()
        saved_session_ref = (
            f"ses-{slug}" if session_ref == "ready" else session_ref
        )
        con = db.connect(self.root)
        try:
            cur = con.execute(
                "INSERT INTO runs(agent, backend, title, requested_by, workdir, slug, "
                "status, session_ref, supervisor_protocol, log_path, started_at) "
                "VALUES(?, 'opencode', 'task', 'codex', ?, ?, 'running', ?, ?, ?, ?)",
                (
                    agent,
                    str(self.root),
                    slug,
                    saved_session_ref,
                    supervisor_protocol,
                    str(log_path),
                    db.now(),
                ),
            )
            con.commit()
            return int(cur.lastrowid)
        finally:
            con.close()

    def test_file_sends_complete_large_utf8_message(self) -> None:
        body = ("Investigation finding: café\n" * 600) + "final conclusion\n"
        self.assertGreater(len(body.encode("utf-8")), 10_000)
        source = self.root / "investigation.md"
        source.write_text(body, encoding="utf-8")

        code, stdout, stderr = self._run_main(
            ["send", "reviewer", "--file", str(source), "--as", "researcher"]
        )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("sent researcher -> reviewer", stdout)
        messages = self._messages()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["body"], body)

    def test_inline_body_remains_supported(self) -> None:
        code, _, stderr = self._run_main(
            ["send", "reviewer", "inline handoff", "--as", "researcher"]
        )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(self._messages()[0]["body"], "inline handoff")

    def test_interrupt_reads_multiline_utf8_message_from_file(self) -> None:
        run_id = self._insert_active_run(agent="glm", slug="chilly_ferret")
        body = "Correct `support` handling.\n\nPreserve café fixtures exactly.\n"
        source = self.root / "interrupt.md"
        source.write_text(body, encoding="utf-8")

        code, stdout, stderr = self._run_main([
            "interrupt",
            str(run_id),
            "--file",
            str(source),
            "--as",
            "codex",
        ])

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("interrupt scheduled", stdout)
        message = self._messages()[0]
        self.assertEqual(message["body"], f"[INTERRUPT] {body}")

    def test_supervised_run_identity_and_run_id_are_inferred(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"ORCHESTRA_SELF": "glm", "ORCHESTRA_RUN_ID": "323"},
            clear=False,
        ):
            code, stdout, stderr = self._run_main(
                ["send", "codex", "HANDOFF run 323: done"]
            )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("sent glm -> codex", stdout)
        message = self._messages()[0]
        self.assertEqual(message["sender"], "glm")
        self.assertEqual(message["run_id"], 323)

    def test_implicit_worker_inbox_only_reads_its_run(self) -> None:
        con = db.connect(self.root)
        try:
            con.executemany(
                "INSERT INTO messages(sender, recipient, body, run_id, created_at) "
                "VALUES('codex', 'glm', ?, ?, ?)",
                [
                    ("interrupt for chilly_ferret", 323, db.now()),
                    ("interrupt for eager_badger", 324, db.now()),
                ],
            )
            con.commit()
        finally:
            con.close()

        with mock.patch.dict(
            os.environ,
            {"ORCHESTRA_SELF": "glm", "ORCHESTRA_RUN_ID": "323"},
            clear=False,
        ):
            code, stdout, stderr = self._run_main(
                ["inbox", "--unread", "--mark-read"]
            )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("interrupt for chilly_ferret", stdout)
        self.assertNotIn("interrupt for eager_badger", stdout)
        messages = self._messages()
        self.assertIsNotNone(messages[0]["read_at"])
        self.assertIsNone(messages[1]["read_at"])

    def test_explicit_profile_inbox_remains_profile_wide(self) -> None:
        con = db.connect(self.root)
        try:
            con.executemany(
                "INSERT INTO messages(sender, recipient, body, run_id, created_at) "
                "VALUES('codex', 'glm', ?, ?, ?)",
                [("run 323", 323, db.now()), ("run 324", 324, db.now())],
            )
            con.commit()
        finally:
            con.close()

        with mock.patch.dict(
            os.environ,
            {"ORCHESTRA_SELF": "glm", "ORCHESTRA_RUN_ID": "323"},
            clear=False,
        ):
            code, stdout, stderr = self._run_main(["inbox", "glm", "--unread"])

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("run 323", stdout)
        self.assertIn("run 324", stdout)

    def test_operator_send_auto_targets_only_active_profile_run(self) -> None:
        run_id = self._insert_active_run(agent="glm", slug="chilly_ferret")

        code, stdout, stderr = self._run_main(["send", "glm", "please check this"])

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn(f"scheduled for glm on run {run_id}", stdout)
        message = self._messages()[0]
        self.assertEqual(message["run_id"], run_id)
        self.assertEqual(message["kind"], "interrupt")
        self.assertIsNotNone(message["delivery_offset"])

    def test_send_can_wait_in_delivery_queue_before_session_is_identified(self) -> None:
        run_id = self._insert_active_run(
            agent="glm",
            slug="chilly_ferret",
            session_ref=None,
        )

        code, stdout, stderr = self._run_main(
            ["send", "glm", "apply this when resumable"]
        )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn(f"scheduled for glm on run {run_id}", stdout)
        message = self._messages()[0]
        self.assertEqual(message["kind"], "interrupt")
        self.assertIsNone(message["delivered_at"])
        self.assertIsNotNone(message["delivery_offset"])

    def test_send_refuses_legacy_supervisor_instead_of_claiming_delivery(self) -> None:
        run_id = self._insert_active_run(
            agent="glm",
            slug="chilly_ferret",
            supervisor_protocol=0,
        )

        code, _, stderr = self._run_main(["send", "glm", "do not lose this"])

        self.assertEqual(code, 1)
        self.assertIn(f"message was not sent: run {run_id}", stderr)
        self.assertEqual(self._messages(), [])

    def test_worker_send_targets_recipient_run_instead_of_sender_run(self) -> None:
        recipient_run = self._insert_active_run(agent="glm", slug="chilly_ferret")

        with mock.patch.dict(
            os.environ,
            {"ORCHESTRA_SELF": "reviewer", "ORCHESTRA_RUN_ID": "323"},
            clear=False,
        ):
            code, stdout, stderr = self._run_main(
                ["send", "glm", "cross-run finding"]
            )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn(f"scheduled for glm on run {recipient_run}", stdout)
        message = self._messages()[0]
        self.assertEqual(message["sender"], "reviewer")
        self.assertEqual(message["recipient"], "glm")
        self.assertEqual(message["run_id"], recipient_run)
        self.assertEqual(message["kind"], "interrupt")

    def test_inactive_profile_message_is_visible_to_its_next_run(self) -> None:
        code, _, stderr = self._run_main(
            ["send", "reviewer", "read this when you start", "--as", "codex"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIsNone(self._messages()[0]["run_id"])

        with mock.patch.dict(
            os.environ,
            {"ORCHESTRA_SELF": "reviewer", "ORCHESTRA_RUN_ID": "404"},
            clear=False,
        ):
            code, stdout, stderr = self._run_main(
                ["inbox", "--unread", "--mark-read"]
            )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("read this when you start", stdout)
        self.assertIsNotNone(self._messages()[0]["read_at"])

    def test_operator_send_rejects_ambiguous_active_profile(self) -> None:
        first = self._insert_active_run(agent="glm", slug="chilly_ferret")
        second = self._insert_active_run(agent="glm", slug="eager_badger")

        code, _, stderr = self._run_main(["send", "glm", "please check this"])

        self.assertEqual(code, 1)
        self.assertIn("ambiguous across active runs", stderr)
        self.assertIn(f"{first} (chilly_ferret)", stderr)
        self.assertIn(f"{second} (eager_badger)", stderr)
        self.assertIn("orchestra interrupt RUN", stderr)
        self.assertEqual(self._messages(), [])

    def test_worker_report_derives_route_from_supervised_run(self) -> None:
        run_id = self._insert_active_run(agent="glm", slug="chilly_ferret")

        with mock.patch.dict(
            os.environ,
            {"ORCHESTRA_SELF": "glm", "ORCHESTRA_RUN_ID": str(run_id)},
            clear=False,
        ):
            code, stdout, stderr = self._run_main(["report", "tests", "are", "passing"])

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn(f"report sent for run {run_id} -> codex", stdout)
        message = self._messages()[0]
        self.assertEqual(message["sender"], "glm")
        self.assertEqual(message["recipient"], "codex")
        self.assertEqual(message["run_id"], run_id)
        self.assertEqual(message["body"], f"REPORT run {run_id}: tests are passing")

    def test_worker_consult_is_non_blocking_and_routes_to_requester(self) -> None:
        run_id = self._insert_active_run(agent="glm", slug="chilly_ferret")

        with mock.patch.dict(
            os.environ,
            {"ORCHESTRA_SELF": "glm", "ORCHESTRA_RUN_ID": str(run_id)},
            clear=False,
        ):
            code, stdout, stderr = self._run_main(
                ["consult", "Is", "the", "wire", "format", "length-prefixed?"]
            )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn(f"sent to codex; run {run_id} keeps working", stdout)
        message = self._messages()[0]
        self.assertEqual(message["sender"], "glm")
        self.assertEqual(message["recipient"], "codex")
        self.assertEqual(message["run_id"], run_id)
        self.assertEqual(message["kind"], "consult")
        self.assertIn(f"CONSULT run {run_id}", message["body"])
        self.assertIn(f"orchestra interrupt {run_id}", message["body"])
        con = db.connect(self.root)
        try:
            status = con.execute(
                "SELECT status FROM runs WHERE id=?", (run_id,)
            ).fetchone()["status"]
        finally:
            con.close()
        self.assertEqual(status, "running")

    def test_child_consult_routes_to_exact_active_lead_at_safe_boundary(self) -> None:
        lead_id = self._insert_active_run(agent="reviewer", slug="steady_otter")
        child_id = self._insert_active_run(agent="glm", slug="chilly_ferret")
        con = db.connect(self.root)
        try:
            con.execute(
                "UPDATE runs SET requested_by='reviewer', lead_run=? WHERE id=?",
                (lead_id, child_id),
            )
            con.commit()
        finally:
            con.close()

        with mock.patch.dict(
            os.environ,
            {"ORCHESTRA_SELF": "glm", "ORCHESTRA_RUN_ID": str(child_id)},
            clear=False,
        ):
            code, stdout, stderr = self._run_main(
                ["consult", "Does", "this", "fixture", "encode", "legacy", "behavior?"]
            )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn(f"routed to requester on lead run {lead_id}", stdout)
        messages = self._messages()
        self.assertEqual(len(messages), 2)
        consultation, delivery = messages
        self.assertEqual(consultation["kind"], "consult")
        self.assertEqual(consultation["run_id"], child_id)
        self.assertIsNotNone(consultation["read_at"])
        self.assertEqual(delivery["kind"], "interrupt")
        self.assertEqual(delivery["run_id"], lead_id)
        self.assertEqual(delivery["recipient"], "reviewer")
        self.assertIsNotNone(delivery["delivery_offset"])
        self.assertIsNone(delivery["delivered_at"])
        self.assertIn(f"CONSULT run {child_id}", delivery["body"])

    def test_consult_can_pause_with_a_bounded_fallback_without_dispatch_opt_in(self) -> None:
        run_id = self._insert_active_run(agent="glm", slug="chilly_ferret")

        with mock.patch.dict(
            os.environ,
            {"ORCHESTRA_SELF": "glm", "ORCHESTRA_RUN_ID": str(run_id)},
            clear=False,
        ):
            code, stdout, stderr = self._run_main([
                "consult",
                "Which schema should I apply?",
                "--wait",
                "60",
                "--fallback",
                "Use schema v1",
            ])

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn(f"run {run_id} paused for up to 60 seconds", stdout)
        con = db.connect(self.root)
        try:
            run = con.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
            question = con.execute(
                "SELECT * FROM questions WHERE run_id=?", (run_id,)
            ).fetchone()
        finally:
            con.close()
        self.assertEqual(run["status"], "waiting_input")
        self.assertEqual(question["recommended_default"], "Use schema v1")
        self.assertEqual(question["status"], "waiting")
        self.assertEqual(self._messages()[0]["kind"], "question")

    def test_worker_consult_requires_matching_supervised_identity(self) -> None:
        run_id = self._insert_active_run(agent="glm", slug="chilly_ferret")

        with mock.patch.dict(
            os.environ,
            {"ORCHESTRA_SELF": "reviewer", "ORCHESTRA_RUN_ID": str(run_id)},
            clear=False,
        ):
            code, _, stderr = self._run_main(["consult", "Which", "schema?"])

        self.assertEqual(code, 1)
        self.assertIn("supervised identity mismatch", stderr)
        self.assertEqual(self._messages(), [])

    def test_operator_consult_does_not_suggest_bypassing_controller(self) -> None:
        run_id = self._insert_active_run(agent="glm", slug="chilly_ferret")
        con = db.connect(self.root)
        try:
            con.execute(
                "UPDATE runs SET requested_by='operator:opn-test', "
                "containment_mode='operator-write' WHERE id=?",
                (run_id,),
            )
            con.commit()
        finally:
            con.close()

        with mock.patch.dict(
            os.environ,
            {"ORCHESTRA_SELF": "glm", "ORCHESTRA_RUN_ID": str(run_id)},
            clear=False,
        ):
            code, _, stderr = self._run_main(
                ["consult", "Which", "approved", "schema", "applies?"]
            )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        message = self._messages()[0]
        self.assertEqual(message["recipient"], "operator:opn-test")
        self.assertIn("controller owns any revised instructions or retry", message["body"])
        self.assertNotIn("orchestra interrupt", message["body"])
        self.assertNotIn("orchestra resume", message["body"])

    def test_wait_returns_early_and_claims_a_target_consultation(self) -> None:
        run_id = self._insert_active_run(agent="glm", slug="chilly_ferret")
        con = db.connect(self.root)
        try:
            con.execute(
                "INSERT INTO messages(sender, recipient, body, run_id, kind, created_at) "
                "VALUES('glm','codex',?,?,'consult',?)",
                (
                    f"CONSULT run {run_id}: Which schema?",
                    run_id,
                    db.now(),
                ),
            )
            con.commit()
        finally:
            con.close()

        code, stdout, stderr = self._run_main(
            ["wait", str(run_id), "--as", "codex", "--timeout", "1"]
        )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("guidance requested; workers are still running", stdout)
        self.assertIn(f"CONSULT run {run_id}: Which schema?", stdout)
        self.assertIsNotNone(self._messages()[0]["read_at"])
        con = db.connect(self.root)
        try:
            status = con.execute(
                "SELECT status FROM runs WHERE id=?", (run_id,)
            ).fetchone()["status"]
        finally:
            con.close()
        self.assertEqual(status, "running")

    def test_worker_handoff_derives_route_and_canonical_prefix(self) -> None:
        run_id = self._insert_active_run(agent="glm", slug="chilly_ferret")

        with mock.patch.dict(
            os.environ,
            {"ORCHESTRA_SELF": "glm", "ORCHESTRA_RUN_ID": str(run_id)},
            clear=False,
        ):
            code, stdout, stderr = self._run_main(["handoff", "implemented", "and", "verified"])

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn(f"handoff sent for run {run_id} -> codex", stdout)
        message = self._messages()[0]
        self.assertEqual(message["sender"], "glm")
        self.assertEqual(message["recipient"], "codex")
        self.assertEqual(message["run_id"], run_id)
        self.assertEqual(message["body"], f"HANDOFF run {run_id}: implemented and verified")

    def test_worker_handoff_rejects_identity_mismatch(self) -> None:
        run_id = self._insert_active_run(agent="glm", slug="chilly_ferret")

        with mock.patch.dict(
            os.environ,
            {"ORCHESTRA_SELF": "minimax", "ORCHESTRA_RUN_ID": str(run_id)},
            clear=False,
        ):
            code, _, stderr = self._run_main(["handoff", "not", "my", "run"])

        self.assertEqual(code, 1)
        self.assertIn("supervised identity mismatch", stderr)
        self.assertEqual(self._messages(), [])

    def test_worker_handoff_requires_supervisor_environment(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            code, _, stderr = self._run_main(["handoff", "done"])

        self.assertEqual(code, 1)
        self.assertIn("worker-only", stderr)
        self.assertEqual(self._messages(), [])

    def test_inline_body_and_file_are_mutually_exclusive(self) -> None:
        source = self.root / "handoff.md"
        source.write_text("file handoff", encoding="utf-8")

        code, _, stderr = self._run_main(
            ["send", "reviewer", "inline handoff", "--file", str(source)]
        )

        self.assertEqual(code, 2)
        self.assertIn("not allowed with argument", stderr)
        self.assertEqual(self._messages(), [])

    def test_unreadable_file_reports_path_without_inserting(self) -> None:
        missing = self.root / "missing.md"

        code, _, stderr = self._run_main(
            ["send", "reviewer", "--file", str(missing)]
        )

        self.assertEqual(code, 1)
        self.assertIn(f"cannot read message file '{missing}'", stderr)
        self.assertEqual(self._messages(), [])


if __name__ == "__main__":
    unittest.main()
