"""Normalized traces (DESIGN §7, W-0165).

Fixtures are hand-written JSONL samples, one per backend — never a real
transcript, and never the developer's ~/.dromond (see tests/__init__.py).
"""
import json
import os
import re
import tempfile
import threading
import unittest
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from dromond import cli, db, traces

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
                                   {"DROMOND_HOME": str(self.dir / "home")})
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


class ClaudeNoiseTests(TraceTestCase):
    def test_telemetry_subtypes_do_not_become_events(self) -> None:
        """A real Claude run carried 892 thinking_tokens counters and 186
        status pings against 94 real thinking blocks; recorded as lifecycle
        they bury the trace. Recognized, dropped, still counted as skipped."""
        for subtype in ("thinking_tokens", "status"):
            line = json.dumps({"type": "system", "subtype": subtype,
                               "tokens": 12})
            self.assertEqual(traces.parse_line("claude", line), [])

    def test_a_meaningful_system_subtype_still_lands(self) -> None:
        line = json.dumps({"type": "system", "subtype": "task_started"})
        events = traces.parse_line("claude", line)
        self.assertEqual([e["kind"] for e in events], ["lifecycle"])
        self.assertEqual(events[0]["name"], "task_started")


class ParserTests(TraceTestCase):
    def test_every_backend_normalizes_into_the_shared_shape(self) -> None:
        """One table, one shape: each backend's own JSONL lands as the same
        seven kinds (DESIGN §7)."""
        for backend, rows in FIXTURES.items():
            with self.subTest(backend=backend):
                log = write_jsonl(self.dir / f"{backend}.jsonl", rows)
                run_id = self.make_run(backend, log)
                report = traces.ingest(self.con, run_id)
                self.assertGreater(report["events"], 0)
                self.assertEqual(report["skipped"], 0)
                kinds = set(self.kinds(run_id))
                self.assertLessEqual(kinds, set(traces.KINDS))
                expected = ["reasoning", "tool_call", "tool_result",
                            "assistant_text", "lifecycle"]
                # No real Reasonix transcript on hand contains a permission
                # event — both sampled runs used an auto permission posture —
                # so requiring one here would only test an invented shape.
                # An unmapped kind is counted as skipped, never lost, and the
                # raw line stays on disk.
                if backend != "reasonix":
                    expected.append("permission_request")
                for kind in expected:
                    self.assertIn(kind, kinds, f"{backend} lost {kind}")

    def test_claude_tool_call_keeps_its_name_and_input(self) -> None:
        log = write_jsonl(self.dir / "c.jsonl", CLAUDE)
        run_id = self.make_run("claude", log)
        traces.ingest(self.con, run_id)
        call = [e for e in traces.events_for_run(self.con, run_id)
                if e["kind"] == "tool_call"][0]
        self.assertEqual(call["name"], "Bash")
        self.assertIn("ls src", call["payload"])

    def test_codex_started_is_the_call_and_completed_is_the_result(self) -> None:
        log = write_jsonl(self.dir / "x.jsonl", CODEX)
        run_id = self.make_run("codex", log)
        traces.ingest(self.con, run_id)
        events = traces.events_for_run(self.con, run_id)
        calls = [e for e in events if e["kind"] == "tool_call"]
        results = [e for e in events if e["kind"] == "tool_result"]
        self.assertEqual([c["name"] for c in calls], ["command_execution"])
        self.assertIn("3 passed", results[0]["payload"])

    def test_claude_partial_stream_events_do_not_duplicate_text(self) -> None:
        log = write_jsonl(self.dir / "c.jsonl", CLAUDE)
        run_id = self.make_run("claude", log)
        traces.ingest(self.con, run_id)
        texts = [e for e in traces.events_for_run(self.con, run_id)
                 if e["kind"] == "assistant_text"]
        self.assertEqual([t["payload"] for t in texts], ["Reading the file now."])

    def test_the_brief_lands_as_a_human_injection(self) -> None:
        log = write_jsonl(self.dir / "c.jsonl", CLAUDE)
        run_id = self.make_run("claude", log)
        traces.ingest(self.con, run_id)
        injected = [e for e in traces.events_for_run(self.con, run_id)
                    if e["kind"] == "human_injection"]
        self.assertEqual([e["payload"] for e in injected], ["do the thing"])

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

    def test_a_parser_that_explodes_is_a_skipped_line(self) -> None:
        with mock.patch.dict(traces.PARSERS,
                             {"claude": mock.Mock(side_effect=RuntimeError("drift"))}):
            self.assertIsNone(traces.parse_line("claude", '{"type": "assistant"}'))

    def test_an_unknown_backend_never_raises(self) -> None:
        self.assertIsNone(traces.parse_line("newthing", '{"type": "assistant"}'))


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
        log.write_text(json.dumps(CODEX[0]) + "\n" + '{"type": "item.star')
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

    def test_recorded_injection_has_no_raw_backing(self) -> None:
        log = write_jsonl(self.dir / "o.jsonl", OPENCODE)
        run_id = self.make_run("opencode", log)
        traces.ingest(self.con, run_id)
        traces.record_injection(self.con, run_id, "human", "stop and rebase")
        event = traces.events_for_run(self.con, run_id)[-1]
        self.assertEqual(event["kind"], "human_injection")
        self.assertEqual(event["byte_offset"], -1)
        self.assertEqual(traces.expand(self.con, event["id"])["payload"],
                         "stop and rebase")
        # the injection must not disturb the file cursor
        self.assertEqual(traces.ingest(self.con, run_id)["events"], 0)


class TruncationTests(TraceTestCase):
    def big_run(self, size: int = 6000) -> tuple[int, str]:
        blob = "x" * size
        rows = [{"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": blob}]}}]
        log = write_jsonl(self.dir / "big.jsonl", rows)
        return self.make_run("claude", log), blob

    def test_payload_truncates_at_2kb_and_expands_from_the_byte_offset(self) -> None:
        """The stored row stays small; the raw file stays the source of truth."""
        run_id, blob = self.big_run()
        traces.ingest(self.con, run_id)
        event = traces.events_for_run(self.con, run_id)[0]
        self.assertTrue(event["truncated"])
        self.assertEqual(len(event["payload"]), traces.MAX_PAYLOAD)
        self.assertEqual(event["payload_len"], len(blob))

        expanded = traces.expand(self.con, event["id"])
        self.assertEqual(expanded["payload"], blob)          # full payload back
        self.assertFalse(expanded["truncated"])
        self.assertEqual(expanded["source"], "raw")
        # the offset/length really are that line's slice of the file
        raw = Path(self.con.execute("SELECT log_path FROM runs WHERE id=?",
                                    (run_id,)).fetchone()["log_path"]).read_bytes()
        line = raw[event["byte_offset"]:event["byte_offset"] + event["byte_length"]]
        self.assertEqual(json.loads(line)["message"]["content"][0]["text"], blob)
        self.assertEqual(expanded["raw"], line.decode())

    def test_offsets_are_right_for_every_line_not_just_the_first(self) -> None:
        run_id = self.make_run("codex", write_jsonl(self.dir / "x.jsonl", CODEX))
        traces.ingest(self.con, run_id)
        raw = (self.dir / "x.jsonl").read_bytes()
        for event in traces.events_for_run(self.con, run_id):
            slice_ = raw[event["byte_offset"]:
                         event["byte_offset"] + event["byte_length"]]
            self.assertIsInstance(json.loads(slice_), dict)

    def test_two_same_kind_blocks_on_one_line_expand_to_their_own_payload(self) -> None:
        first, second = "a" * 3000, "b" * 3000
        rows = [{"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": first},
            {"type": "tool_use", "id": "t", "name": "Bash", "input": {}},
            {"type": "text", "text": second}]}}]
        run_id = self.make_run("claude", write_jsonl(self.dir / "two.jsonl", rows))
        traces.ingest(self.con, run_id)
        texts = [e for e in traces.events_for_run(self.con, run_id)
                 if e["kind"] == "assistant_text"]
        self.assertEqual([traces.expand(self.con, t["id"])["payload"] for t in texts],
                         [first, second])

    def test_expand_falls_back_to_the_stored_payload_once_raw_is_pruned(self) -> None:
        run_id, _ = self.big_run()
        traces.ingest(self.con, run_id)
        event = traces.events_for_run(self.con, run_id)[0]
        (self.dir / "big.jsonl").unlink()
        expanded = traces.expand(self.con, event["id"])
        self.assertEqual(expanded["source"], "stored")
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
        self.assertEqual(traces.prune_raw_logs(self.con, days=1), [])
        self.assertTrue(live.is_file())
        for status in ("spawning", "pending", "interrupt"):
            self.con.execute("UPDATE runs SET status=? WHERE id=?", (status, run_id))
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

    def test_pruning_ingests_the_tail_before_deleting(self) -> None:
        """Nothing unread is thrown away."""
        log = write_jsonl(self.dir / "unread.jsonl", CLAUDE)
        run_id = self.make_run("claude", log, status="done",
                               finished_at=self.old(90))
        traces.prune_raw_logs(self.con, days=30)
        self.assertIn("assistant_text", self.kinds(run_id))

    def test_dry_run_reports_without_deleting(self) -> None:
        log = write_jsonl(self.dir / "old.jsonl", CODEX)
        self.make_run("codex", log, status="killed", finished_at=self.old(90))
        pruned = traces.prune_raw_logs(self.con, days=30, dry_run=True)
        self.assertEqual(len(pruned), 1)
        self.assertTrue(log.is_file())

    def test_retention_days_default_and_override(self) -> None:
        self.assertEqual(traces.retention_days(None),
                         traces.DEFAULT_RAW_RETENTION_DAYS)
        self.assertEqual(traces.retention_days({"settings":
                                                {"raw_log_retention_days": 7}}), 7)
        self.assertEqual(traces.retention_days({"settings":
                                                {"raw_log_retention_days": "x"}}),
                         traces.DEFAULT_RAW_RETENTION_DAYS)

    def test_cli_prune_entry(self) -> None:
        log = write_jsonl(self.dir / "old.jsonl", CODEX)
        self.make_run("codex", log, status="done", finished_at=self.old(90))
        cli.cmd_traces(Namespace(action="prune", days=30, dry_run=False,
                                 run_id=None))
        self.assertFalse(log.is_file())


class MessageTests(TraceTestCase):
    def add(self, run_id: int, sender: str, body: str, kind: str,
            delivered=None, offset=None) -> int:
        cur = self.con.execute(
            "INSERT INTO messages(run_id, sender, body, kind, created_at, "
            "delivery_offset, delivered_at) VALUES(?,?,?,?,?,?,?)",
            (run_id, sender, body, kind, db.now(), offset, delivered))
        self.con.commit()
        return int(cur.lastrowid)

    def test_messages_are_badged_queued_delivered_answered(self) -> None:
        run_id = self.make_run("claude", self.dir / "none.jsonl")
        answered = self.add(run_id, "human", "use uv", "interrupt",
                            delivered=db.now())
        self.add(run_id, "dromond", "run 1 finished: done", "completion")
        delivered = self.add(run_id, "human", "and rebase", "interrupt",
                             delivered=db.now())
        queued = self.add(run_id, "human", "later", "interrupt", offset=4096)
        states = {m["id"]: m for m in traces.run_messages(self.con, run_id)}
        self.assertEqual(states[answered]["state"], "answered")
        self.assertEqual(states[delivered]["state"], "delivered")
        self.assertEqual(states[queued]["state"], "queued")
        self.assertTrue(states[queued]["pending_boundary"])
        self.assertFalse(states[delivered]["pending_boundary"])
        self.assertEqual(states[answered]["direction"], "inbound")
        self.assertEqual([m["direction"] for m in
                          traces.run_messages(self.con, run_id)].count("outbound"), 1)


class SseTests(TraceTestCase):
    def test_frame_format(self) -> None:
        frame = traces.sse({"a": 1}, event="trace", event_id=7)
        self.assertEqual(frame, 'id: 7\nevent: trace\ndata: {"a": 1}\n\n')
        multi = traces.sse("one\ntwo", event="daemon")
        self.assertEqual(multi, "event: daemon\ndata: one\ndata: two\n\n")

    def test_run_trace_stream_replays_then_ends_on_a_terminal_run(self) -> None:
        log = write_jsonl(self.dir / "o.jsonl", OPENCODE)
        run_id = self.make_run("opencode", log, status="done",
                               finished_at=db.now())
        traces.ingest(self.con, run_id)
        frames = list(traces.stream_run_trace(run_id, con=self.con, poll=0))
        self.assertTrue(frames[0].startswith("retry:"))
        self.assertIn("event: end", frames[-1])
        traced = [f for f in frames if "event: trace" in f]
        self.assertEqual(len(traced), len(traces.events_for_run(self.con, run_id)))

    def test_run_trace_stream_resumes_after_last_event_id(self) -> None:
        log = write_jsonl(self.dir / "o.jsonl", OPENCODE)
        run_id = self.make_run("opencode", log, status="done",
                               finished_at=db.now())
        traces.ingest(self.con, run_id)
        events = traces.events_for_run(self.con, run_id)
        frames = list(traces.stream_run_trace(run_id, after_id=events[-2]["id"],
                                              con=self.con, poll=0))
        self.assertEqual(len([f for f in frames if "event: trace" in f]), 1)
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
        out.write_text("dromond daemon: pid 1, every 60s\n")
        stop = threading.Event()
        stream = traces.stream_daemon_log({out.name: 0}, stop=stop, files=[out],
                                          poll=0)
        self.assertTrue(next(stream).startswith("retry:"))
        first = next(stream)
        self.assertIn("event: daemon", first)
        self.assertIn("pid 1", first)
        with open(out, "a") as handle:
            handle.write("dromond daemon: swept: [{'action': 'claim'}]\n")
        frames = []
        for frame in stream:
            frames.append(frame)
            if "swept" in frame:
                stop.set()
        self.assertTrue(any("swept" in f for f in frames))
        self.assertFalse(any("pid 1" in f for f in frames))  # never re-sent

    def test_daemon_cursor_round_trip(self) -> None:
        self.assertEqual(traces.parse_daemon_cursor("daemon.out.log@12,daemon.err.log@0"),
                         {"daemon.out.log": 12, "daemon.err.log": 0})
        self.assertEqual(traces.parse_daemon_cursor(None), {})
        self.assertEqual(traces.parse_daemon_cursor("garbage"), {})


class GlyphCoverageTests(unittest.TestCase):
    """The dashboard renders each event kind with a glyph. A kind added to the
    parser without a glyph would render as an unlabelled line, so the two lists
    are pinned to each other here rather than drifting apart silently."""

    def test_every_event_kind_has_a_glyph(self) -> None:
        from dromond.http import DASHBOARD
        source = DASHBOARD.read_text(encoding="utf-8")
        block = source.split("const GLYPH = {", 1)[1].split("};", 1)[0]
        mapped = set(re.findall(r"^\s*(\w+):", block, re.MULTILINE))
        self.assertEqual(mapped, set(traces.KINDS))
        # and an unknown kind still gets one, so nothing renders bare
        self.assertIn("const UNKNOWN_GLYPH =", source)

    def test_short_reasoning_starts_expanded(self) -> None:
        from dromond.http import DASHBOARD
        source = DASHBOARD.read_text(encoding="utf-8")
        self.assertIn("const SHORT_REASONING_CHARS = 512;", source)
        self.assertIn("d.open = open;", source)
        self.assertIn("row.payload_len <= SHORT_REASONING_CHARS", source)


if __name__ == "__main__":
    unittest.main()
