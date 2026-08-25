"""Normalized traces (DESIGN §7, W-0165).

Fixtures are hand-written JSONL samples, one per backend — never a real
transcript, and never the developer's ~/.orchestra (see tests/__init__.py).
"""
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from orchestra import db, traces

# --- fixture transcripts ----------------------------------------------------

CLAUDE = [
    {"type": "system", "subtype": "init", "session_id": "sess-claude-1",
     "tools": ["Bash"]},
    {"type": "user", "message": {"role": "user", "content": "do the thing"}},
    {"type": "stream_event", "event": {"type": "content_block_delta"}},
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "the file is probably in src/"},
        {"type": "text", "text": "Reading the file now."},
        {"type": "tool_use", "id": "tu_1", "name": "Bash",
         "input": {"command": "ls src"}}]}},
    {"type": "control_request", "request": {"subtype": "can_use_tool",
                                            "tool_name": "Write"}},
    {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tu_1", "content": "main.py"}]}},
    {"type": "result", "subtype": "success", "result": "done", "is_error": False},
]

CODEX = [
    {"type": "thread.started", "thread_id": "sess-codex-1"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "i1", "type": "reasoning",
                                        "text": "check the tests first"}},
    {"type": "item.started", "item": {"id": "i2", "type": "command_execution",
                                      "command": ["pytest", "-q"]}},
    {"type": "item.updated", "item": {"id": "i2", "type": "command_execution"}},
    {"type": "item.completed", "item": {"id": "i2", "type": "command_execution",
                                        "aggregated_output": "3 passed",
                                        "exit_code": 0}},
    {"type": "item.started", "item": {"id": "i3", "type": "file_change",
                                      "status": "awaiting_approval"}},
    {"type": "item.completed", "item": {"id": "i4", "type": "agent_message",
                                        "text": "Tests pass."}},
    {"type": "turn.completed", "usage": {"input_tokens": 10}},
]

OPENCODE = [
    {"type": "session.updated", "sessionID": "sess-opencode-1"},
    {"type": "message.part.updated", "part": {"type": "step-start"}},
    {"type": "message.part.updated", "part": {"type": "reasoning",
                                              "text": "grep for the symbol"}},
    {"type": "message.part.updated", "part": {
        "type": "tool", "tool": "grep",
        "state": {"status": "pending", "input": {"pattern": "def main"}}}},
    {"type": "message.part.updated", "part": {
        "type": "tool", "tool": "grep",
        "state": {"status": "running", "input": {"pattern": "def main"}}}},
    {"type": "message.part.updated", "part": {
        "type": "tool", "tool": "grep",
        "state": {"status": "completed", "output": "cli.py:12"}}},
    {"type": "permission.asked", "permission": {"type": "edit",
                                                "id": "perm-1"}},
    {"type": "message.part.updated", "part": {"type": "text",
                                              "text": "Found it in cli.py."}},
    {"type": "step_finish", "part": {"type": "step-finish"}},
]

# Shapes taken from real transcripts (two Reasonix runs in this project's
# legacy state), not from documentation: Reasonix discriminates on `kind`,
# streams `text` and `reasoning` token-by-token, and closes with a single
# Claude-shaped `type: result` line.
REASONIX = [
    {"kind": "turn_started"},
    {"kind": "stream_attempt",
     "streamAttempt": {"id": "sa-1", "action": "begin", "attempt": 1, "max": 6}},
    {"kind": "reasoning", "text": "plan:"},
    {"kind": "reasoning", "text": " read, patch, verify"},
    {"kind": "message", "reasoning": "A complete block, not a fragment."},
    {"kind": "tool_dispatch",
     "tool": {"id": "t1", "name": "read", "readOnly": True, "partial": False}},
    {"kind": "tool_progress", "tool": {"id": "t1", "name": "", "output": "…"}},
    {"kind": "tool_result",
     "tool": {"id": "t1", "name": "read", "args": "{}", "output": "SCHEMA_VERSION = 6",
              "err": "", "readOnly": True, "startedAt": 1, "endedAt": 2}},
    {"kind": "text", "text": "Patched "},
    {"kind": "text", "text": "db.py."},
    {"kind": "usage", "usage": {"promptTokens": 10, "completionTokens": 4,
                                "totalTokens": 14}},
    {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
     "result": "done", "session_id": "sess-reasonix-1", "total_cost_usd": 0.002,
     "usage": {"input_tokens": 10, "output_tokens": 4,
               "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}},
]

FIXTURES = {"claude": CLAUDE, "codex": CODEX,
            "opencode": OPENCODE, "reasonix": REASONIX}


def write_jsonl(path: Path, rows) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


class TraceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ,
                                   {"ORCHESTRA_HOME": str(self.dir / "home")})
        self.env.start()
        self.con = db.connect()

    def tearDown(self) -> None:
        self.con.close()
        self.env.stop()
        self.tmp.cleanup()

    def make_run(self, backend: str, log_path: Path, status: str = "running",
                 finished_at=None) -> int:
        cur = self.con.execute(
            "INSERT INTO runs(profile, backend, requested_by, workdir, log_path, "
            "status, started_at, finished_at) VALUES(?,?,'human','/p',?,?,?,?)",
            (backend, backend, str(log_path), status, db.now(), finished_at))
        self.con.commit()
        return int(cur.lastrowid)

    def kinds(self, run_id: int) -> list[str]:
        return [r["kind"] for r in traces.events_for_run(self.con, run_id)]


class ParserTests(TraceTestCase):
    def test_every_backend_normalizes_into_the_shared_shape(self) -> None:
        # Codex and OpenCode store lifecycle only for failures (W-0303), and
        # these fixtures are happy paths; their turn/step/session markers are
        # recognized chatter, so skipped stays 0 without any stored rows.
        expected = {
            "claude": set(traces.KINDS),
            "codex": set(traces.KINDS) - {"human_injection", "lifecycle"},
            "opencode": set(traces.KINDS) - {"human_injection", "lifecycle"},
            "reasonix": set(traces.KINDS)
            - {"human_injection", "permission_request"},
        }
        for backend, rows in FIXTURES.items():
            with self.subTest(backend=backend):
                log = write_jsonl(self.dir / f"{backend}.jsonl", rows)
                run_id = self.make_run(backend, log)
                report = traces.ingest(self.con, run_id)
                self.assertGreater(report["events"], 0)
                self.assertEqual(report["skipped"], 0)
                self.assertEqual(set(self.kinds(run_id)), expected[backend])

    def test_unparseable_lines_are_counted_never_raised(self) -> None:
        """These formats are undocumented and drift; ingest must survive
        anything the backend prints."""
        log = self.dir / "junk.jsonl"
        log.write_text(
            "not json at all\n"
            "{broken json\n"
            "[1, 2, 3]\n"
            '{"type": "totally.unknown.event", "wat": 1}\n'
            + json.dumps({"type": "result", "subtype": "success",
                          "result": "ok"}) + "\n")
        run_id = self.make_run("claude", log)
        report = traces.ingest(self.con, run_id)
        self.assertEqual(report["skipped"], 4)
        self.assertEqual(self.kinds(run_id), ["lifecycle"])


class WaitAuditTests(TraceTestCase):
    def test_audit_lists_waiting_and_record_polling_tool_calls(self) -> None:
        run_id = self.make_run("codex", self.dir / "audit.jsonl")
        for command in ("sleep 30", "gh pr checks 42 --watch",
                        "work show W-0300", "pytest -q"):
            traces._record_synthetic(
                self.con, run_id,
                traces._ev("tool_call", "exec", {"command": command}))

        rows = traces.wait_audit(self.con, "2000-01-01T00:00:00+00:00")

        self.assertEqual([row["category"] for row in rows],
                         ["sleep", "ci_poll", "record_poll"])
        self.assertFalse(any("pytest" in row["command"] for row in rows))


class NoiseTests(TraceTestCase):
    """Machine chatter is dropped at ingest, not stored (W-0303).

    Measured on the live store, ~530 of ~1300 events were lifecycle chatter
    the dashboard hid client-side. The raw log keeps every line; failures
    (turn.failed, error, session.error) are never noise."""

    DROPPED = {
        "reasonix": [
            {"kind": "turn_started"},
            {"kind": "stream_attempt", "streamAttempt": {"id": "sa-1"}},
            {"kind": "usage", "usage": {"totalTokens": 14}},
            {"kind": "tool_progress", "tool": {"id": "t1", "output": "…"}},
        ],
        "codex": [
            {"type": "thread.started", "thread_id": "sess-1"},
            {"type": "turn.started"},
            {"type": "turn.completed", "usage": {"input_tokens": 10}},
            {"type": "session.created"},
        ],
        "opencode": [
            {"type": "message.part.updated", "part": {"type": "step-start"}},
            {"type": "step_finish", "part": {"type": "step-finish"}},
            {"type": "session.idle"},
            {"type": "session.updated", "sessionID": "sess-1"},
            {"type": "message.updated"},
            {"type": "message.part.updated", "part": {
                "type": "tool", "tool": "bash",
                "state": {"status": "pending", "input": {"command": "ls"}}}},
        ],
        "claude": [
            {"type": "system", "subtype": "hook_started"},
            {"type": "system", "subtype": "hook_response"},
        ],
    }
    KEPT = {
        "codex": [{"type": "turn.failed", "error": {"message": "boom"}},
                  {"type": "error", "message": "boom"}],
        "opencode": [{"type": "session.error", "error": {"name": "Unknown"}}],
    }

    def test_chatter_is_recognized_and_returns_nothing(self) -> None:
        for backend, rows in self.DROPPED.items():
            for row in rows:
                with self.subTest(backend=backend, line=row):
                    self.assertEqual(
                        traces.parse_line(backend, json.dumps(row)), [])

    def test_failures_still_land_as_lifecycle(self) -> None:
        for backend, rows in self.KEPT.items():
            for row in rows:
                with self.subTest(backend=backend, line=row):
                    events = traces.parse_line(backend, json.dumps(row))
                    self.assertEqual([e["kind"] for e in events], ["lifecycle"])
                    self.assertEqual(events[0]["name"], row["type"])

    def test_reasonix_fixture_stores_no_chatter_rows(self) -> None:
        log = write_jsonl(self.dir / "r.jsonl", REASONIX)
        run_id = self.make_run("reasonix", log)
        self.assertEqual(traces.ingest(self.con, run_id)["skipped"], 0)
        names = {r["name"] for r in traces.events_for_run(self.con, run_id)}
        self.assertEqual(names & {"tool_progress", "stream_attempt", "usage",
                                  "turn_started"}, set())

    def test_opencode_completed_tools_pair_calls_with_results(self) -> None:
        log = write_jsonl(self.dir / "o.jsonl", OPENCODE)
        run_id = self.make_run("opencode", log)
        traces.ingest(self.con, run_id)
        kinds = self.kinds(run_id)
        self.assertEqual(kinds.count("tool_call"), 1)  # pending + running once
        self.assertEqual(kinds.count("tool_call"), kinds.count("tool_result"))


class IngestTests(TraceTestCase):
    def test_ingest_is_incremental_and_idempotent(self) -> None:
        log = write_jsonl(self.dir / "o.jsonl", OPENCODE[:4])
        run_id = self.make_run("opencode", log)
        first = traces.ingest(self.con, run_id)
        self.assertEqual(traces.ingest(self.con, run_id)["events"], 0)
        with open(log, "a") as handle:
            for row in OPENCODE[4:]:
                handle.write(json.dumps(row) + "\n")
        second = traces.ingest(self.con, run_id)
        self.assertGreater(second["events"], 0)
        self.assertEqual(second["offset"], log.stat().st_size)
        self.assertEqual(len(traces.events_for_run(self.con, run_id)),
                         first["events"] + second["events"])

    def test_a_partial_trailing_line_waits_for_its_newline(self) -> None:
        log = self.dir / "p.jsonl"
        log.write_text(json.dumps(CODEX[2]) + "\n" + '{"type": "item.star')
        run_id = self.make_run("codex", log)
        report = traces.ingest(self.con, run_id)
        self.assertEqual(report["events"], 1)
        self.assertEqual(report["skipped"], 0)
        with open(log, "a") as handle:
            handle.write('ted", "item": {"type": "command_execution"}}\n')
        self.assertEqual(traces.ingest(self.con, run_id)["events"], 1)

    def test_a_missing_raw_log_is_not_an_error(self) -> None:
        run_id = self.make_run("codex", self.dir / "gone.jsonl")
        self.assertEqual(traces.ingest(self.con, run_id),
                         {"events": 0, "skipped": 0, "offset": 0})

    def test_recorded_injection_is_retained_without_moving_the_cursor(self) -> None:
        log = write_jsonl(self.dir / "o.jsonl", OPENCODE)
        run_id = self.make_run("opencode", log)
        traces.ingest(self.con, run_id)
        traces.record_injection(self.con, run_id, "human", "stop and rebase")
        event = traces.events_for_run(self.con, run_id)[-1]
        self.assertEqual(event["kind"], "human_injection")
        self.assertEqual(event["payload"], "stop and rebase")
        self.assertEqual(traces.ingest(self.con, run_id)["events"], 0)


class TruncationTests(TraceTestCase):
    def big_run(self, size: int = 6000) -> tuple[int, str]:
        blob = "x" * size
        rows = [{"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": blob}]}}]
        log = write_jsonl(self.dir / "big.jsonl", rows)
        return self.make_run("claude", log), blob

    def test_large_payload_is_stored_bounded_and_expands_in_full(self) -> None:
        run_id, blob = self.big_run()
        traces.ingest(self.con, run_id)
        event = traces.events_for_run(self.con, run_id)[0]
        self.assertTrue(event["truncated"])
        self.assertEqual(len(event["payload"]), traces.MAX_PAYLOAD)
        self.assertEqual(event["payload_len"], len(blob))
        expanded = traces.expand(self.con, event["id"])
        self.assertEqual(expanded["payload"], blob)          # full payload back
        self.assertFalse(expanded["truncated"])

    def test_expand_falls_back_to_the_stored_payload_once_raw_is_pruned(self) -> None:
        run_id, _ = self.big_run()
        traces.ingest(self.con, run_id)
        event = traces.events_for_run(self.con, run_id)[0]
        (self.dir / "big.jsonl").unlink()
        expanded = traces.expand(self.con, event["id"])
        self.assertTrue(expanded["truncated"])
        self.assertEqual(len(expanded["payload"]), traces.MAX_PAYLOAD)


class RetentionTests(TraceTestCase):
    def old(self, days: int) -> str:
        return (datetime.now(timezone.utc) - timedelta(days=days)) \
            .strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_retention_never_touches_a_live_run(self) -> None:
        """A running run's raw log is untouchable however old the row looks."""
        live = write_jsonl(self.dir / "live.jsonl", OPENCODE)
        run_id = self.make_run("opencode", live, status="running",
                               finished_at=self.old(400))
        for status in ("running", "spawning", "pending", "interrupt"):
            with self.subTest(status=status):
                self.con.execute("UPDATE runs SET status=? WHERE id=?",
                                 (status, run_id))
                self.con.commit()
                self.assertEqual(traces.prune_raw_logs(self.con, days=1), [])
                self.assertTrue(live.is_file())

    def test_terminal_runs_age_out_and_keep_their_events(self) -> None:
        log = write_jsonl(self.dir / "old.jsonl", CODEX)
        run_id = self.make_run("codex", log, status="done",
                               finished_at=self.old(45))
        pruned = traces.prune_raw_logs(self.con, days=30)
        self.assertEqual([p["run_id"] for p in pruned], [run_id])
        self.assertFalse(log.is_file())
        self.assertGreater(len(traces.events_for_run(self.con, run_id)), 0)
        # pruning is a one-shot; a second pass finds nothing to do
        self.assertEqual(traces.prune_raw_logs(self.con, days=30), [])

    def test_a_recent_terminal_run_is_kept(self) -> None:
        log = write_jsonl(self.dir / "fresh.jsonl", CODEX)
        self.make_run("codex", log, status="failed", finished_at=self.old(2))
        self.assertEqual(traces.prune_raw_logs(self.con, days=30), [])
        self.assertTrue(log.is_file())

    def test_dry_run_reports_without_deleting(self) -> None:
        log = write_jsonl(self.dir / "old.jsonl", CODEX)
        self.make_run("codex", log, status="killed", finished_at=self.old(90))
        pruned = traces.prune_raw_logs(self.con, days=30, dry_run=True)
        self.assertEqual(len(pruned), 1)
        self.assertTrue(log.is_file())

    def test_retention_days_default_and_override(self) -> None:
        cases = [
            (None, traces.DEFAULT_RAW_RETENTION_DAYS),
            ({"settings": {"raw_log_retention_days": 7}}, 7),
            ({"settings": {"raw_log_retention_days": "x"}},
             traces.DEFAULT_RAW_RETENTION_DAYS),
        ]
        for cfg, expected in cases:
            with self.subTest(config=cfg):
                self.assertEqual(traces.retention_days(cfg), expected)


class MessageTests(TraceTestCase):
    def add(self, run_id: int, sender: str, body: str, kind: str,
            delivered=None, offset=None, undeliverable=None) -> int:
        cur = self.con.execute(
            "INSERT INTO messages(run_id, sender, body, kind, created_at, "
            "delivery_offset, delivered_at, undeliverable_at, "
            "undeliverable_reason) VALUES(?,?,?,?,?,?,?,?,?)",
            (run_id, sender, body, kind, db.now(), offset, delivered,
             undeliverable, "worker exited" if undeliverable else None))
        self.con.commit()
        return int(cur.lastrowid)

    def test_messages_are_badged_queued_delivered_answered(self) -> None:
        run_id = self.make_run("claude", self.dir / "none.jsonl")
        answered = self.add(run_id, "human", "use uv", "interrupt",
                            delivered=db.now())
        completion = self.add(run_id, "orchestra", "run 1 finished: done",
                              "completion")
        delivered = self.add(run_id, "human", "and rebase", "interrupt",
                             delivered=db.now())
        queued = self.add(run_id, "human", "later", "interrupt", offset=4096)
        failed = self.add(run_id, "human", "too late", "interrupt",
                          undeliverable=db.now())
        states = {m["id"]: m for m in traces.run_messages(self.con, run_id)}
        expected = {
            answered: ("answered", "inbound", False),
            completion: ("delivered", "outbound", False),
            delivered: ("delivered", "inbound", False),
            queued: ("queued", "inbound", True),
            failed: ("undeliverable", "inbound", False),
        }
        for message_id, state in expected.items():
            with self.subTest(message_id=message_id):
                message = states[message_id]
                self.assertEqual(
                    (message["state"], message["direction"],
                     message["pending_boundary"]), state)
        self.assertEqual(states[failed]["undeliverable_reason"], "worker exited")


class SseTests(TraceTestCase):
    def test_frame_format(self) -> None:
        frame = traces.sse({"a": 1}, event="trace", event_id=7)
        self.assertEqual(frame, 'id: 7\nevent: trace\ndata: {"a": 1}\n\n')
        multi = traces.sse("one\ntwo", event="daemon")
        self.assertEqual(multi, "event: daemon\ndata: one\ndata: two\n\n")

    def test_run_trace_stream_replays_resumes_then_ends(self) -> None:
        log = write_jsonl(self.dir / "o.jsonl", OPENCODE)
        run_id = self.make_run("opencode", log, status="done",
                               finished_at=db.now())
        traces.ingest(self.con, run_id)
        events = traces.events_for_run(self.con, run_id)
        cases = [(0, len(events)), (events[-2]["id"], 1)]
        for after_id, count in cases:
            with self.subTest(after_id=after_id):
                frames = list(traces.stream_run_trace(
                    run_id, after_id=after_id, con=self.con, poll=0))
                self.assertTrue(frames[0].startswith("retry:"))
                self.assertIn("event: end", frames[-1])
                traced = [f for f in frames if "event: trace" in f]
                self.assertEqual(len(traced), count)
                if after_id:
                    self.assertIn(f"id: {events[-1]['id']}", "".join(frames))

    def test_run_trace_stream_stops_on_the_seams_stop_event(self) -> None:
        run_id = self.make_run("opencode", self.dir / "none.jsonl")  # still live
        stop = threading.Event()
        stop.set()
        self.assertEqual(list(traces.stream_run_trace(run_id, con=self.con,
                                                      stop=stop)),
                         [f"retry: {traces.SSE_RETRY_MS}\n\n"])

    def test_daemon_log_stream_is_append_only(self) -> None:
        out = self.dir / "daemon.out.log"
        out.write_text("orchestra daemon: pid 1, every 60s\n")
        stop = threading.Event()
        stream = traces.stream_daemon_log({out.name: 0}, stop=stop, files=[out],
                                          poll=0)
        self.assertTrue(next(stream).startswith("retry:"))
        first = next(stream)
        self.assertIn("event: daemon", first)
        self.assertIn("pid 1", first)
        with open(out, "a") as handle:
            handle.write("orchestra daemon: swept: [{'action': 'claim'}]\n")
        frames = []
        for frame in stream:
            frames.append(frame)
            if "swept" in frame:
                stop.set()
        self.assertTrue(any("swept" in f for f in frames))
        self.assertFalse(any("pid 1" in f for f in frames))  # never re-sent

    def test_daemon_cursor_round_trip(self) -> None:
        cases = [
            ("daemon.out.log@12,daemon.err.log@0",
             {"daemon.out.log": 12, "daemon.err.log": 0}),
            (None, {}),
            ("garbage", {}),
        ]
        for raw, expected in cases:
            with self.subTest(cursor=raw):
                self.assertEqual(traces.parse_daemon_cursor(raw), expected)


if __name__ == "__main__":
    unittest.main()
