import json
import tempfile
import unittest
from pathlib import Path

from orchestra import db, traces


SAMPLES = {
    "claude": {"type": "assistant", "message": {
        "content": [{"type": "text", "text": "hello"}]}},
    "codex": {"type": "item.completed", "item": {
        "type": "agent_message", "text": "hello"}},
    "opencode": {"type": "message.part.updated", "part": {
        "type": "text", "text": "hello"}},
    "reasonix": {"kind": "text", "text": "hello"},
}


class TraceParserTests(unittest.TestCase):
    def test_each_builtin_normalizes_assistant_output(self):
        for adapter, value in SAMPLES.items():
            with self.subTest(adapter=adapter):
                events = traces.parse_line(adapter, json.dumps(value))
                self.assertEqual(events[0]["kind"], "assistant_text")
                self.assertEqual(events[0]["payload"], "hello")

    def test_acp_frames_join_the_same_normalized_stream(self):
        frame = {"jsonrpc": "2.0", "method": "session/update", "params": {
            "update": {"sessionUpdate": "agent_message_chunk",
                       "content": {"type": "text", "text": "hello"}}}}
        event = traces.parse_line("reasonix", json.dumps(frame))[0]
        self.assertEqual((event["kind"], event["payload"]),
                         ("assistant_text", "hello"))

    def test_malformed_and_unknown_lines_fail_soft(self):
        for line in ("", "not json", "[]", '{"unknown": true}'):
            with self.subTest(line=line):
                self.assertIsNone(traces.parse_line("codex", line))


class TraceIngestTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.con = db.connect(self.root / "v2.db")
        now = db.now()
        self.con.execute(
            "INSERT INTO runtimes(runtime_id,slug,name,adapter,created_at,updated_at) "
            "VALUES('runtime','runtime','Runtime','opencode',?,?)", (now, now))
        self.con.execute(
            "INSERT INTO profiles(profile_id,slug,name,runtime_id,tier,created_at,"
            "updated_at) VALUES('profile','profile','Profile','runtime',2,?,?)",
            (now, now))
        self.con.commit()

    def tearDown(self):
        self.con.close()
        self.directory.cleanup()

    def make_run(self, adapter="opencode"):
        log = self.root / f"{adapter}.jsonl"
        cursor = self.con.execute(
            "INSERT INTO runs(request_id,profile_id,runtime_id,mission,"
            "requested_by,status,queued_at,cwd,cwd_source,workdir,isolation,log_path,"
            "profile_snapshot,runtime_snapshot,request_snapshot) "
            "VALUES(?,?,?,?,'test','running',?,?,'run',?,'auto',?,?,?,?)",
            (f"request-{adapter}", "profile", "runtime", "test",
             db.now(), str(self.root), str(self.root), str(log), "{}",
             json.dumps({"adapter": adapter}), "{}"))
        self.con.commit()
        return int(cursor.lastrowid), log

    def test_ingest_is_incremental_and_waits_for_complete_lines(self):
        run_id, log = self.make_run()
        first = json.dumps(SAMPLES["opencode"])
        log.write_text(first + "\n", encoding="utf-8")
        result = traces.ingest(self.con, run_id)
        self.assertEqual(result["events"], 1)
        self.assertEqual(traces.ingest(self.con, run_id)["events"], 0)

        with log.open("a", encoding="utf-8") as handle:
            handle.write(first)
        offset = traces.cursor(self.con, run_id)["byte_offset"]
        self.assertEqual(traces.ingest(self.con, run_id)["offset"], offset)
        with log.open("a", encoding="utf-8") as handle:
            handle.write("\n")
        self.assertEqual(traces.ingest(self.con, run_id)["events"], 1)

    def test_large_payload_is_bounded_but_raw_coordinates_are_retained(self):
        run_id, log = self.make_run("codex")
        value = {"type": "item.completed", "item": {
            "type": "agent_message", "text": "x" * (traces.MAX_PAYLOAD + 100)}}
        log.write_text(json.dumps(value) + "\n", encoding="utf-8")
        traces.drain(self.con, run_id)
        event = self.con.execute(
            "SELECT * FROM events WHERE run_id=?", (run_id,)).fetchone()
        self.assertEqual(event["truncated"], 1)
        self.assertGreater(event["payload_len"], len(event["payload"]))
        self.assertGreaterEqual(event["byte_offset"], 0)
        self.assertGreater(event["byte_length"], 0)

    def test_synthetic_operator_delivery_joins_the_event_stream(self):
        run_id, _ = self.make_run()
        traces.record_injection(self.con, run_id, "operator", "change direction")
        event = self.con.execute(
            "SELECT kind,name,payload,byte_offset FROM events WHERE run_id=?",
            (run_id,)).fetchone()
        self.assertEqual(tuple(event),
                         ("human_injection", "operator", "change direction", -1))


if __name__ == "__main__":
    unittest.main()
