"""ACP transport (W-0104, DESIGN §6), against a stub peer — never a real
harness and never a paid model call.

``tests/fake_acp.py`` speaks the protocol shapes verified live against
``reasonix acp`` and ``opencode acp``, so the whole dispatch -> supervise ->
finalize path runs unmodified over the second transport.
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from dromond import acp, cli, db, messaging, project, supervise, traces

PROJECT_ID = "53efe3c3-6def-4797-8560-3dce073d7d63"
FAKE_ACP = Path(__file__).resolve().parent / "fake_acp.py"

CONFIG = """\
[settings]
timeout = 120
stall_timeout = 0

[profiles.acpstub]
backend = "reasonix"
transport = "acp"

[profiles.acpoc]
backend = "opencode"
transport = "acp"

[profiles.execstub]
backend = "reasonix"
"""


class AcpUnitTest(unittest.TestCase):
    """The parts with no process and no database in them."""

    def test_absent_transport_is_exec(self) -> None:
        self.assertEqual(acp.transport_for({"backend": "reasonix"}), "exec")
        self.assertEqual(acp.transport_for({"backend": "claude"}), "exec")
        self.assertEqual(acp.transport_for({"backend": "codex",
                                            "transport": "exec"}), "exec")

    def test_opt_in_is_case_insensitive(self) -> None:
        self.assertEqual(acp.transport_for({"backend": "opencode",
                                            "transport": " ACP "}), "acp")

    def test_acp_on_a_backend_without_it_is_a_configuration_error(self) -> None:
        """Never a silent downgrade: an unreasonable-about run is exactly what
        the no-fallback rule exists to prevent."""
        for backend in ("claude", "codex"):
            with self.assertRaises(SystemExit) as caught:
                acp.transport_for({"backend": backend, "transport": "acp",
                                   "name": "p"})
            self.assertIn("no ACP mode", str(caught.exception))

    def test_unknown_transport_is_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            acp.transport_for({"backend": "reasonix", "transport": "grpc"})

    def test_command_per_backend(self) -> None:
        self.assertEqual(acp.build_acp_cmd({"backend": "reasonix"}),
                         ["reasonix", "acp"])
        self.assertEqual(acp.build_acp_cmd({"backend": "opencode"}),
                         ["opencode", "acp"])

    def test_permission_answer_picks_by_option_kind(self) -> None:
        options = [{"optionId": "no", "kind": "reject_once"},
                   {"optionId": "yes", "kind": "allow_once"},
                   {"optionId": "always", "kind": "allow_always"}]
        allowed = acp.permission_answer({}, {"options": options})
        self.assertEqual(allowed,
                         {"outcome": {"outcome": "selected", "optionId": "always"}})
        denied = acp.permission_answer({"acp_permission": "deny"},
                                       {"options": options})
        self.assertEqual(denied["outcome"]["optionId"], "no")
        self.assertEqual(acp.permission_answer({}, {"options": []}),
                         {"outcome": {"outcome": "cancelled"}})

    def test_peer_close_reaps_process_and_closes_streams(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            peer = acp.Peer(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                cwd=tmp, env=dict(os.environ),
                log_path=Path(tmp) / "peer.jsonl")
            peer.start()
            proc = peer.proc
            peer.close()

        self.assertIsNotNone(proc.returncode)
        self.assertTrue(proc.stdin.closed)
        self.assertTrue(proc.stdout.closed)
        self.assertTrue(proc.stderr.closed)


class SupervisorLifecycleTest(unittest.TestCase):
    def test_detached_supervisor_has_a_waiter(self) -> None:
        waited = threading.Event()
        proc = mock.Mock()
        proc.wait.side_effect = waited.set
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(supervise.paths, "logs_dir", return_value=Path(tmp)), \
                mock.patch.object(supervise.shutil, "which", return_value="dromond"), \
                mock.patch.object(supervise.subprocess, "Popen", return_value=proc):
            supervise.spawn_supervisor(Path(tmp), 7)

        self.assertTrue(waited.wait(1), "detached supervisor was not reaped")


class AcpTraceParsingTest(unittest.TestCase):
    """ACP frames land in the SAME seven kinds exec produces — no eighth."""

    def parse(self, line):
        # The backend argument is deliberately a lie here: an ACP frame is
        # self-describing, so parsing must not depend on the run row.
        return traces.parse_line("claude", line)

    def test_streamed_messages(self) -> None:
        events = self.parse('{"jsonrpc":"2.0","method":"session/update","params":'
                            '{"update":{"sessionUpdate":"agent_message_chunk",'
                            '"content":{"type":"text","text":"hello"}}}}')
        # merge=True: ACP streams chunks, so they coalesce into one row.
        self.assertEqual(events, [{"kind": "assistant_text", "name": None,
                                   "payload": "hello", "ts": None,
                                   "merge": True}])

    def test_thoughts_and_plans_are_reasoning(self) -> None:
        thought = self.parse('{"jsonrpc":"2.0","method":"session/update","params":'
                             '{"update":{"sessionUpdate":"agent_thought_chunk",'
                             '"content":{"type":"text","text":"hmm"}}}}')
        self.assertEqual(thought[0]["kind"], "reasoning")
        plan = self.parse('{"jsonrpc":"2.0","method":"session/update","params":'
                          '{"update":{"sessionUpdate":"plan","entries":'
                          '[{"content":"step one","status":"pending"}]}}}')
        self.assertEqual(plan[0]["kind"], "reasoning")
        self.assertEqual(plan[0]["name"], "plan")
        self.assertIn("step one", plan[0]["payload"])

    def test_tool_activity(self) -> None:
        call = self.parse('{"jsonrpc":"2.0","method":"session/update","params":'
                          '{"update":{"sessionUpdate":"tool_call","title":"read x",'
                          '"status":"pending","rawInput":{"path":"x"}}}}')
        self.assertEqual((call[0]["kind"], call[0]["name"]), ("tool_call", "read x"))
        self.assertIn("path", call[0]["payload"])
        done = self.parse('{"jsonrpc":"2.0","method":"session/update","params":'
                          '{"update":{"sessionUpdate":"tool_call_update",'
                          '"title":"read x","status":"completed",'
                          '"content":[{"type":"text","text":"file body"}]}}}')
        self.assertEqual((done[0]["kind"], done[0]["payload"]),
                         ("tool_result", "file body"))

    def test_permission_request(self) -> None:
        events = self.parse('{"jsonrpc":"2.0","id":7,"method":'
                            '"session/request_permission","params":{"toolCall":'
                            '{"title":"write x","kind":"edit"},"options":[]}}')
        self.assertEqual((events[0]["kind"], events[0]["name"]),
                         ("permission_request", "write x"))

    def test_what_dromond_sends_in_is_a_human_injection(self) -> None:
        for method in ("session/prompt", "_reasonix.io/session/steer"):
            events = self.parse('{"jsonrpc":"2.0","_dir":"out","id":3,"method":"'
                                + method + '","params":{"prompt":'
                                '[{"type":"text","text":"do the thing"}]}}')
            self.assertEqual((events[0]["kind"], events[0]["payload"]),
                             ("human_injection", "do the thing"))

    def test_responses_and_errors_are_lifecycle(self) -> None:
        stop = self.parse('{"jsonrpc":"2.0","id":3,"_method":"session/prompt",'
                          '"result":{"stopReason":"end_turn"}}')
        self.assertEqual((stop[0]["kind"], stop[0]["name"]),
                         ("lifecycle", "stop:end_turn"))
        bad = self.parse('{"jsonrpc":"2.0","id":3,"_method":"session/new",'
                         '"error":{"code":-32603,"message":"boom"}}')
        self.assertEqual((bad[0]["kind"], bad[0]["name"]),
                         ("lifecycle", "session/new error"))

    def test_every_kind_is_one_of_the_existing_seven(self) -> None:
        lines = [
            '{"jsonrpc":"2.0","method":"session/update","params":{"update":'
            '{"sessionUpdate":"current_mode_update","currentModeId":"normal"}}}',
            '{"jsonrpc":"2.0","_dir":"out","id":1,"method":"initialize","params":{}}',
            '{"jsonrpc":"2.0","method":"session/update","params":{"update":'
            '{"sessionUpdate":"something_new_in_2027"}}}',
        ]
        for line in lines:
            for event in self.parse(line):
                self.assertIn(event["kind"], traces.KINDS)

    def test_exec_parsing_is_untouched(self) -> None:
        """The dispatch is on the frame, not the run: a backend line still
        goes to its own parser."""
        events = traces.parse_line(
            "claude", '{"type":"assistant","message":{"content":'
                      '[{"type":"text","text":"exec text"}]}}')
        self.assertEqual(events[0]["payload"], "exec text")


class AcpDeliveryStateTest(unittest.TestCase):
    """DESIGN §7 badge: an ACP message is not waiting on a boundary."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(
            os.environ, {"DROMOND_HOME": str(Path(self.tmp.name) / "home")})
        self.env.start()
        self.con = db.connect()
        self.con.execute(
            "INSERT INTO runs(id, profile, backend, requested_by, workdir, "
            "status, started_at) VALUES(1,'p','reasonix','human','/tmp',"
            "'running',?)", (db.now(),))
        self.con.commit()

    def tearDown(self) -> None:
        self.con.close()
        self.env.stop()
        self.tmp.cleanup()

    def test_acp_message_carries_no_boundary_offset(self) -> None:
        messaging.queue_tell(self.con, 1, "human", "use tabs", None, boundary=False)
        row = self.con.execute("SELECT delivery_offset FROM messages").fetchone()
        self.assertIsNone(row["delivery_offset"])
        badge = traces.run_messages(self.con, 1)[0]
        self.assertFalse(badge["pending_boundary"])
        self.assertEqual(badge["state"], "queued")
        # The exec boundary machinery must not see it either.
        self.assertIsNone(supervise._pending_delivery_offset(self.con, 1))

    def test_exec_message_still_pends_on_a_boundary(self) -> None:
        messaging.queue_tell(self.con, 1, "human", "use tabs", None)
        badge = traces.run_messages(self.con, 1)[0]
        self.assertTrue(badge["pending_boundary"])
        self.assertEqual(supervise._pending_delivery_offset(self.con, 1), 0)


class AcpEndToEndTest(unittest.TestCase):
    """The whole path over a stub peer: handshake, turns, steer, cancel,
    permission, normalized events, dead peer."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name).resolve()
        self.root = self.tmp_path / "workspace" / "demo"
        self.root.mkdir(parents=True)
        self.global_config = self.tmp_path / "global.toml"
        self.global_config.write_text(CONFIG)
        bin_dir = self.tmp_path / "stub-bin"
        bin_dir.mkdir()
        for name in ("reasonix", "opencode"):
            shim = bin_dir / name
            shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_ACP}" "$@"\n')
            shim.chmod(0o755)
        self.env = mock.patch.dict(os.environ, {
            "DROMOND_CONFIG": str(self.global_config),
            "DROMOND_HOME": str(self.tmp_path / "home"),
            "DROMOND_ROOT": str(self.root),
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
            "STUB_ACP_TURN": "0.2",
            "STUB_ACP_PERMISSION": "0",
            "STUB_ACP_DIE": "",
            "STUB_ACP_BAD_VERSION": "0",
        })
        self.env.start()
        con = db.connect()
        project.remember(con, str(self.tmp_path / "workspace"),
                         [{"projectId": PROJECT_ID, "id": "demo", "name": "Demo",
                           "path": "demo"}])
        con.close()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    # -- helpers
    def _dispatch(self, profile: str = "acpstub", mission: str = "do the thing"):
        ns = Namespace(mission=[mission], to=profile, after=None, brief_file=None,
                       context=None, title=None, worktree=False, sync=False)
        with mock.patch.object(supervise, "spawn_supervisor"):
            cli.cmd_dispatch(ns)
        con = db.connect()
        run_id = int(con.execute("SELECT MAX(id) AS n FROM runs").fetchone()["n"])
        con.close()
        return run_id

    def _run(self, run_id: int):
        rc = supervise.supervise(self.root, run_id)
        return rc, self._row(run_id)

    def _row(self, run_id: int):
        con = db.connect()
        row = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        con.close()
        return row

    def _wait_for_session(self, run_id: int, timeout: float = 20) -> str | None:
        con = db.connect()
        deadline = time.time() + timeout
        try:
            while time.time() < deadline:
                row = con.execute("SELECT session_ref, status FROM runs WHERE id=?",
                                  (run_id,)).fetchone()
                if row["session_ref"] and row["status"] == "running":
                    return row["session_ref"]
                time.sleep(0.05)
        finally:
            con.close()
        return None

    def _sent(self, run_id: int, method: str) -> int:
        """How many times Dromond sent this method, read off the raw log."""
        path = self.tmp_path / "home" / "logs" / f"run-{run_id}.jsonl"
        count = 0
        for line in path.read_text(errors="replace").splitlines():
            if not line.startswith("{"):
                continue
            frame = json.loads(line)
            count += frame.get("_dir") == "out" and frame.get("method") == method
        return count

    def _events(self, run_id: int):
        con = db.connect()
        rows = traces.events_for_run(con, run_id, limit=2000)
        con.close()
        return rows

    # -- proofs
    def test_handshake_and_session_creation(self) -> None:
        run_id = self._dispatch()
        rc, run = self._run(run_id)
        self.assertEqual(rc, 0)
        self.assertEqual(run["status"], "done")
        self.assertEqual(run["session_ref"], "stub-acp-session-1")
        # The prompt the peer echoed back is the composed brief, so the run
        # really did go through prepare_launch and not some ACP-only path.
        self.assertIn("stub acp turn complete", run["summary"])
        self.assertIn("acpstub", run["summary"])
        names = [e["name"] for e in self._events(run_id)]
        self.assertIn("initialize", names)
        self.assertIn("session/new", names)
        self.assertIn("stop:end_turn", names)

    def test_opencode_uses_the_same_transport(self) -> None:
        run_id = self._dispatch(profile="acpoc")
        _, run = self._run(run_id)
        self.assertEqual(run["status"], "done")
        self.assertEqual(run["session_ref"], "stub-acp-session-1")

    def test_events_land_in_the_normalized_table_with_exec_shapes(self) -> None:
        run_id = self._dispatch()
        self._run(run_id)
        events = self._events(run_id)
        kinds = {e["kind"] for e in events}
        self.assertLessEqual(kinds, set(traces.KINDS))
        for expected in ("assistant_text", "reasoning", "tool_call", "tool_result",
                         "human_injection", "lifecycle"):
            self.assertIn(expected, kinds)
        # Every event points back at a real byte range in the raw log, so the
        # dashboard's expand-in-place works exactly as it does for exec.
        con = db.connect()
        text = next(e for e in events if e["kind"] == "assistant_text")
        expanded = traces.expand(con, text["id"])
        cursor = traces.cursor(con, run_id)
        con.close()
        self.assertEqual(expanded["source"], "raw")
        self.assertIn("stub acp turn complete", expanded["payload"])
        self.assertEqual(
            cursor["byte_offset"],
            (self.tmp_path / "home" / "logs" / f"run-{run_id}.jsonl").stat().st_size)

    def test_a_permission_request_surfaces(self) -> None:
        """DESIGN §6: over ACP the ask is a protocol message Dromond answers,
        not a TTY prompt nobody is watching. The stub's turn does not finish
        until the answer arrives, so completing at all proves it."""
        os.environ["STUB_ACP_PERMISSION"] = "1"
        run_id = self._dispatch()
        _, run = self._run(run_id)
        self.assertEqual(run["status"], "done")
        asks = [e for e in self._events(run_id)
                if e["kind"] == "permission_request"]
        self.assertEqual(len(asks), 1)
        self.assertEqual(asks[0]["name"], "write to README.md")
        self.assertIn("allow_once", asks[0]["payload"])

    def test_steer_delivers_a_message_mid_turn(self) -> None:
        """The whole point of ACP for Reasonix: the message lands while the
        worker is still working — no boundary, no kill, no resume."""
        os.environ["STUB_ACP_TURN"] = "8"
        run_id = self._dispatch()
        thread = threading.Thread(target=supervise.supervise,
                                  args=(self.root, run_id))
        thread.start()
        try:
            self.assertTrue(self._wait_for_session(run_id), "session never opened")
            cli.cmd_interrupt(Namespace(run_id=run_id, message=["use", "tabs"],
                                        message_file=None, now=False))
        finally:
            thread.join(timeout=60)
        self.assertFalse(thread.is_alive())
        run = self._row(run_id)
        self.assertEqual(run["status"], "done")
        # One turn only: the steer arrived inside it, so no second prompt and
        # no restarted process.
        self.assertIn("steered mid-turn", run["summary"])
        self.assertIn("use tabs", run["summary"])
        con = db.connect()
        badge = traces.run_messages(con, run_id)[0]
        con.close()
        self.assertIn(badge["state"], ("delivered", "answered"))
        self.assertIsNotNone(badge["delivered_at"])
        self.assertFalse(badge["pending_boundary"])
        stops = [e["name"] for e in self._events(run_id)
                 if (e["name"] or "").startswith("stop:")]
        self.assertEqual(stops, ["stop:end_turn"])

    def test_opencode_delivers_at_the_end_of_the_live_turn(self) -> None:
        """OpenCode has no steer, so its message goes as the next prompt on
        the SAME session — still no kill, no re-exec, no boundary badge."""
        os.environ["STUB_ACP_TURN"] = "3"
        run_id = self._dispatch(profile="acpoc")
        thread = threading.Thread(target=supervise.supervise,
                                  args=(self.root, run_id))
        thread.start()
        try:
            self.assertTrue(self._wait_for_session(run_id), "session never opened")
            os.environ["STUB_ACP_TURN"] = "0.2"
            cli.cmd_interrupt(Namespace(run_id=run_id, message=["use", "tabs"],
                                        message_file=None, now=False))
        finally:
            thread.join(timeout=60)
        run = self._row(run_id)
        self.assertEqual(run["status"], "done")
        self.assertIn("use tabs", run["summary"])
        self.assertNotIn("steered mid-turn", run["summary"])   # no steer here
        self.assertEqual(self._sent(run_id, "session/new"), 1)
        self.assertEqual(self._sent(run_id, "session/prompt"), 2)
        con = db.connect()
        badge = traces.run_messages(con, run_id)[0]
        con.close()
        self.assertFalse(badge["pending_boundary"])
        self.assertIsNotNone(badge["delivered_at"])

    def test_cancel_leaves_a_resumable_session(self) -> None:
        """`--now` over ACP is session/cancel, not a kill: the same session id
        takes the next prompt, so Reasonix's prefix cache survives."""
        os.environ["STUB_ACP_TURN"] = "8"
        run_id = self._dispatch()
        thread = threading.Thread(target=supervise.supervise,
                                  args=(self.root, run_id))
        thread.start()
        try:
            session = self._wait_for_session(run_id)
            self.assertEqual(session, "stub-acp-session-1")
            os.environ["STUB_ACP_TURN"] = "0.2"     # the resumed turn is short
            cli.cmd_interrupt(Namespace(run_id=run_id, message=["stop", "and", "pivot"],
                                        message_file=None, now=True))
        finally:
            thread.join(timeout=60)
        self.assertFalse(thread.is_alive())
        run = self._row(run_id)
        self.assertEqual(run["status"], "done")
        self.assertEqual(run["session_ref"], "stub-acp-session-1")  # never re-created
        self.assertIn("stop and pivot", run["summary"])
        names = [e["name"] for e in self._events(run_id)]
        self.assertEqual([n for n in names if (n or "").startswith("stop:")],
                         ["stop:cancelled", "stop:end_turn"])
        self.assertEqual(self._sent(run_id, "session/new"), 1)   # one session
        self.assertEqual(self._sent(run_id, "initialize"), 1)    # one peer

    def test_a_peer_that_will_not_start_fails_the_run_with_a_reason(self) -> None:
        os.environ["STUB_ACP_DIE"] = "start"
        run_id = self._dispatch()
        rc, run = self._run(run_id)
        self.assertEqual(rc, 1)
        self.assertEqual(run["status"], "failed")
        self.assertIn("ACP handshake failed", run["summary"])

    def test_a_peer_that_dies_mid_turn_fails_the_run_with_a_reason(self) -> None:
        """Never a silent fallback to exec: the run ends, and says why."""
        os.environ["STUB_ACP_DIE"] = "turn"
        run_id = self._dispatch()
        rc, run = self._run(run_id)
        self.assertEqual(rc, 1)
        self.assertEqual(run["status"], "failed")
        self.assertIn("ACP transport failed", run["summary"])
        self.assertIn("peer", run["summary"])

    def test_a_protocol_version_mismatch_fails_the_handshake(self) -> None:
        os.environ["STUB_ACP_BAD_VERSION"] = "1"
        run_id = self._dispatch()
        _, run = self._run(run_id)
        self.assertEqual(run["status"], "failed")
        self.assertIn("protocolVersion", run["summary"])

    def test_an_undelivered_message_is_still_marked_undeliverable(self) -> None:
        """DESIGN §6 holds for ACP too: marked and surfaced, never dropped."""
        run_id = self._dispatch()
        con = db.connect()
        con.execute("UPDATE runs SET session_ref='x', status='running' WHERE id=?",
                    (run_id,))
        con.commit()
        messaging.queue_tell(con, run_id, "human", "too late", None, boundary=False)
        con.execute("UPDATE runs SET status='spawning' WHERE id=?", (run_id,))
        con.commit()
        con.close()
        os.environ["STUB_ACP_DIE"] = "start"
        self._run(run_id)
        con = db.connect()
        badge = traces.run_messages(con, run_id)[0]
        con.close()
        self.assertEqual(badge["state"], "undeliverable")
        self.assertIn("run ended", badge["undeliverable_reason"])


if __name__ == "__main__":
    unittest.main()
