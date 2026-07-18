"""Checkpoint + takeover workflow tests.

Coverage targets — driven by W-0010 acceptance criteria:

  * empty state — both commands work cleanly on a freshly-initialized project
  * active runs — checkpoint captures them, takeover surfaces them
  * unread messages — the source inbox snapshot survives even when the
    source marked messages read
  * deterministic / bounded output — same input yields byte-equal output;
    snapshot caps hold
  * redaction / exclusion — bodies get credential patterns scrubbed;
    session_ref / pid / log_path / brief_path / argv / env / raw
    transcripts / run.summary are never written
  * corrupt / unsupported checkpoint — graceful errors, never a stack
    trace
  * no source-state mutation — takeover never INSERTs/UPDATEs/DELETEs
  * newest post-watermark entries kept when LIMIT clips (DESC then reverse)
  * post-watermark messages are scoped to recipient == checkpoint.source
  * real ``work list`` TSV parsing has its own boundary test
"""
from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from orchestra_cli import checkpoint, cli, db, paths


# Build credential-shaped fixtures at runtime so repository scanners do not
# mistake deliberate redaction tests for committed credentials.
FAKE_SK = "sk" + "-" + "abcdefghijklmnopqrstuvwxyz1234"
FAKE_AWS = "AK" + "IA" + "ABCDEFGHIJKLMNOP"
FAKE_API = "abcdef1234567890" + "abcdef"
FAKE_GH = "gh" + "p_" + "abcdefghijklmnopqrstuvwx"
FAKE_PRIVATE_KEY = (
    "-----BEGIN RSA " + "PRIVATE KEY-----\nAA\n-----END RSA "
    + "PRIVATE KEY-----"
)


# --- helpers ---------------------------------------------------------------


SENSITIVE_KEYS_RUN = {"session_ref", "pid", "log_path", "brief_path",
                      "workdir", "branch", "parent_run", "summary"}


def _make_project() -> tuple[tempfile.TemporaryDirectory, Path]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / ".orchestra").mkdir(parents=True, exist_ok=True)
    db.connect(root).close()
    return tmp, root


def _insert_run(root: Path, *, agent: str = "minimax",
                status: str = "spawning",
                work_item: str | None = None,
                started_at: str = "2026-07-18T12:00:00Z",
                summary: str | None = None,
                **extra) -> int:
    con = db.connect(root)
    try:
        cur = con.execute(
            "INSERT INTO runs(agent, backend, model, title, work_item, team, "
            "requested_by, workdir, branch, session_ref, pid, log_path, "
            "brief_path, slug, status, started_at, summary) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                agent, "opencode", "minimax-coding-plan/MiniMax-M3",
                extra.get("title", "test run"), work_item, None,
                extra.get("requested_by", "codex"),
                str(root), "orchestra/run-1",
                "session-secret-xyz", 4242,
                str(root / ".orchestra" / "logs" / "run-x.jsonl"),
                str(root / ".orchestra" / "briefs" / "run-x.md"),
                extra.get("slug", "test_slug"),
                status, started_at, summary,
            ),
        )
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def _insert_message(root: Path, *, sender: str, recipient: str, body: str,
                    kind: str = "", work_item: str | None = None,
                    run_id: int | None = None,
                    created_at: str = "2026-07-18T12:00:00Z",
                    read_at: str | None = None) -> int:
    con = db.connect(root)
    try:
        cur = con.execute(
            "INSERT INTO messages(sender, recipient, body, work_item, run_id, "
            "kind, created_at, read_at) VALUES(?,?,?,?,?,?,?,?)",
            (sender, recipient, body, work_item, run_id, kind, created_at, read_at),
        )
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def _insert_feed(root: Path, *, author: str, body: str, tags: str = "",
                 work_item: str | None = None,
                 run_id: int | None = None,
                 created_at: str = "2026-07-18T12:00:00Z") -> int:
    con = db.connect(root)
    try:
        cur = con.execute(
            "INSERT INTO feed(author, body, tags, work_item, run_id, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (author, body, tags, work_item, run_id, created_at),
        )
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Module-level: redaction + exclusion
# ---------------------------------------------------------------------------


class RedactionTests(unittest.TestCase):
    def test_credential_patterns_are_masked(self) -> None:
        for sample, expected_substring in [
            ("token " + FAKE_SK, "[REDACTED]"),
            ("Bearer " + FAKE_AWS, "[REDACTED]"),
            ("api" + "_key: " + FAKE_API, "[REDACTED]"),
            (FAKE_PRIVATE_KEY, "[REDACTED]"),
            (FAKE_GH, "[REDACTED]"),
        ]:
            redacted = checkpoint._redact(sample)
            self.assertIsNotNone(redacted)
            self.assertIn(expected_substring, redacted)
            self.assertNotIn(FAKE_AWS, redacted or "")
            self.assertNotIn(FAKE_SK, redacted or "")

    def test_non_secret_text_round_trips(self) -> None:
        for ok in [
            "Plain handoff text, nothing dangerous.",
            "Token budget remaining is fine.",
            "Switch to plan B because of contention.",
        ]:
            self.assertEqual(checkpoint._redact(ok), ok)

    def test_empty_and_none_safe(self) -> None:
        self.assertIsNone(checkpoint._redact(None))
        self.assertEqual(checkpoint._redact(""), "")


# ---------------------------------------------------------------------------
# Work-list parsing — real code path, not monkey-patched
# ---------------------------------------------------------------------------


class WorkListParsingTests(unittest.TestCase):
    """Boundary tests for the TSV parser that backs objective inference.

    We test the pure parser (``_parse_work_list_text``) directly so the
    tests don't depend on the ``work`` binary being on PATH. The CLI
    wrapper around it is exercised by the integration tests.
    """

    def test_parses_active_rows(self) -> None:
        text = (
            "W-0001\tin_progress\thigh\t-\tFix parser\n"
            "W-0002\treview\tmedium\tcodex\tUpdate README\n"
            "W-0003\tdone\thigh\tminimax\tDone thing\n"
        )
        items = checkpoint._parse_work_list_text(text)
        self.assertEqual([i["id"] for i in items], ["W-0001", "W-0002"])

    def test_skips_short_or_garbage_rows(self) -> None:
        text = (
            "W-0001\tin_progress\thigh\t-\tOK\n"
            "garbage line\n"
            "W-0002\tin_progress\tmedium\tcodex\talso OK\n"
        )
        items = checkpoint._parse_work_list_text(text)
        self.assertEqual([i["id"] for i in items], ["W-0001", "W-0002"])

    def test_caps_result_count(self) -> None:
        text = "\n".join(
            f"W-{i:04d}\tin_progress\thigh\t-\ttitle {i}" for i in range(50)
        )
        items = checkpoint._parse_work_list_text(text, limit=5)
        self.assertEqual(len(items), 5)

    def test_truncates_long_titles(self) -> None:
        long_title = "x" * 500
        text = f"W-0001\tin_progress\thigh\t-\t{long_title}"
        items = checkpoint._parse_work_list_text(text)
        self.assertLessEqual(len(items[0]["title"]),
                             checkpoint.TITLE_MAX_CHARS)

    def test_empty_input_yields_empty_list(self) -> None:
        self.assertEqual(checkpoint._parse_work_list_text(""), [])
        self.assertEqual(checkpoint._parse_work_list_text("\n\n\n"), [])


# ---------------------------------------------------------------------------
# build_checkpoint
# ---------------------------------------------------------------------------


class BuildCheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp, self.root = _make_project()
        # Stub the work-list wrapper so the build is deterministic and
        # doesn't require `work` on PATH for every test in this class.
        # The pure parser is tested separately in WorkListParsingTests.
        self._orig_active = checkpoint._work_active_items
        checkpoint._work_active_items = lambda root: [  # type: ignore[assignment]
            {"id": "W-0010", "status": "in_progress", "priority": "high",
             "assignee": "codex", "title": "Add checkpoint takeover"},
        ]

    def tearDown(self) -> None:
        checkpoint._work_active_items = self._orig_active  # type: ignore[assignment]
        self._tmp.cleanup()

    def test_empty_project_yields_durable_intent(self) -> None:
        data = checkpoint.build_checkpoint(self.root, source="codex", objective=None)
        self.assertEqual(data["schema"], checkpoint.SCHEMA_TAG)
        self.assertEqual(data["version"], checkpoint.SCHEMA_VERSION)
        self.assertEqual(data["source"], "codex")
        # Objective was inferred from the active work item we stubbed.
        self.assertEqual(data["objective"], "Add checkpoint takeover")
        self.assertEqual(data["active_runs"], [])
        self.assertEqual(data["source_inbox_snapshot"], [])
        self.assertEqual(data["feed_snapshot"], [])
        self.assertEqual(data["high_water"], {"max_run_id": None,
                                             "max_message_id": None,
                                             "max_feed_id": None})
        self.assertEqual(data["project"]["root"], str(self.root))
        self.assertEqual(data["project"]["name"], self.root.name)
        self.assertIsInstance(data["project"]["id"], str)
        self.assertEqual(len(data["project"]["id"]), 16)

    def test_active_runs_capture_metadata_only(self) -> None:
        # ``summary`` is the worker's last text output and counts as
        # transcript content. It MUST NOT appear in any checkpoint field.
        rid = _insert_run(self.root, work_item="W-0010",
                          started_at="2026-07-18T12:00:00Z",
                          requested_by="codex",
                          summary="worker last text that must not leak")
        data = checkpoint.build_checkpoint(self.root, source="codex",
                                           objective="explicit")
        self.assertEqual(len(data["active_runs"]), 1)
        run = data["active_runs"][0]
        self.assertEqual(run["id"], rid)
        self.assertEqual(set(run.keys()), set(checkpoint.SAFE_RUN_FIELDS))
        for forbidden in SENSITIVE_KEYS_RUN:
            self.assertNotIn(forbidden, run,
                             f"sensitive field {forbidden!r} leaked into checkpoint")
        self.assertNotIn("worker last text that must not leak", json.dumps(data))

    def test_high_water_marks_track_max_ids(self) -> None:
        rid = _insert_run(self.root)
        mid = _insert_message(self.root, sender="minimax", recipient="codex",
                              body="hi")
        fid = _insert_feed(self.root, author="codex", body="found x")
        data = checkpoint.build_checkpoint(self.root, source="codex",
                                           objective="obj")
        self.assertEqual(data["high_water"]["max_run_id"], rid)
        self.assertEqual(data["high_water"]["max_message_id"], mid)
        self.assertEqual(data["high_water"]["max_feed_id"], fid)

    def test_source_inbox_snapshot_keeps_read_messages(self) -> None:
        _insert_message(self.root, sender="minimax", recipient="codex",
                        body="HANDOFF run 1: all done",
                        read_at="2026-07-18T12:05:00Z")
        data = checkpoint.build_checkpoint(self.root, source="codex",
                                           objective="obj")
        self.assertEqual(len(data["source_inbox_snapshot"]), 1)
        snap = data["source_inbox_snapshot"][0]
        self.assertEqual(set(snap.keys()),
                         set(checkpoint.SAFE_MESSAGE_FIELDS) | {"body_preview"})
        self.assertEqual(snap["body_preview"],
                         "HANDOFF run 1: all done")

    def test_feed_snapshot_bounded(self) -> None:
        for i in range(checkpoint.FEED_SNAPSHOT_LIMIT + 5):
            _insert_feed(self.root, author="codex",
                         body=f"finding {i}",
                         created_at=f"2026-07-18T12:{i:02d}:00Z")
        data = checkpoint.build_checkpoint(self.root, source="codex",
                                           objective="obj")
        self.assertLessEqual(len(data["feed_snapshot"]),
                             checkpoint.FEED_SNAPSHOT_LIMIT)

    def test_credentials_in_message_body_are_redacted(self) -> None:
        _insert_message(self.root, sender="minimax", recipient="codex",
                        body="auth: " + FAKE_SK + " — use it carefully")
        data = checkpoint.build_checkpoint(self.root, source="codex",
                                           objective="obj")
        snap = data["source_inbox_snapshot"][0]
        self.assertIn("[REDACTED]", snap["body_preview"])
        self.assertNotIn(FAKE_SK, snap["body_preview"])

    def test_credentials_in_feed_body_are_redacted(self) -> None:
        _insert_feed(self.root, author="codex",
                     body="github token was " + FAKE_GH)
        data = checkpoint.build_checkpoint(self.root, source="codex",
                                           objective="obj")
        self.assertIn("[REDACTED]", data["feed_snapshot"][0]["body_preview"])
        self.assertNotIn(FAKE_GH, data["feed_snapshot"][0]["body_preview"])

    def test_next_steps_capped_and_truncated(self) -> None:
        # Caller passes 20 next-steps and one of them is huge; only the
        # first NEXT_STEPS_MAX_COUNT land, each capped to
        # NEXT_STEP_MAX_CHARS.
        huge = "x" * 1000
        steps = [f"step-{i}" for i in range(20)] + [huge]
        data = checkpoint.build_checkpoint(self.root, source="codex",
                                           objective="obj",
                                           next_steps=steps)
        self.assertEqual(len(data["next_steps"]), checkpoint.NEXT_STEPS_MAX_COUNT)
        for s in data["next_steps"]:
            self.assertLessEqual(len(s), checkpoint.NEXT_STEP_MAX_CHARS)

    def test_next_steps_credential_redacted(self) -> None:
        steps = [
            "deploy",
            "secret was " + FAKE_SK + " — don't share",
            "verify",
        ]
        data = checkpoint.build_checkpoint(self.root, source="codex",
                                           objective="obj",
                                           next_steps=steps)
        joined = "\n".join(data["next_steps"])
        self.assertIn("[REDACTED]", joined)
        self.assertNotIn(FAKE_SK, joined)

    def test_objective_credential_redacted(self) -> None:
        data = checkpoint.build_checkpoint(
            self.root, source="codex",
            objective="rotate the " + FAKE_AWS + " access key",
        )
        self.assertIn("[REDACTED]", data["objective"])
        self.assertNotIn(FAKE_AWS, data["objective"])

    def test_run_title_credential_redacted(self) -> None:
        # Run title is user-controlled — it must go through the same
        # filter as body text. We verify by inspecting the active_runs
        # payload directly so we don't have to read the markdown brief.
        _insert_run(self.root, title="rotate " + FAKE_AWS + " now")
        data = checkpoint.build_checkpoint(self.root, source="codex",
                                           objective="obj")
        titles = [r["title"] for r in data["active_runs"]]
        self.assertTrue(any("[REDACTED]" in t for t in titles))
        self.assertFalse(any(FAKE_AWS in t for t in titles))

    def test_feed_tags_credential_redacted(self) -> None:
        _insert_feed(self.root, author="codex", body="ok",
                     tags="api" + "_key=" + FAKE_API + ",security")
        data = checkpoint.build_checkpoint(self.root, source="codex",
                                           objective="obj")
        tags = [f["tags"] for f in data["feed_snapshot"]]
        self.assertTrue(any("[REDACTED]" in t for t in tags))
        self.assertFalse(any(FAKE_API in t for t in tags))

    def test_work_item_title_credential_redacted(self) -> None:
        # Active work items carry titles from `work list`. Those titles
        # are user-controlled and must be redacted on the way in.
        orig_active = checkpoint._work_active_items
        checkpoint._work_active_items = lambda root: [  # type: ignore[assignment]
            {"id": "W-0010", "status": "in_progress", "priority": "high",
             "assignee": "codex",
             "title": "rotate " + FAKE_AWS + " access"},
        ]
        try:
            data = checkpoint.build_checkpoint(self.root, source="codex",
                                               objective=None)
        finally:
            checkpoint._work_active_items = orig_active  # type: ignore[assignment]
        joined = json.dumps(data["active_work_items"])
        self.assertIn("[REDACTED]", joined)
        self.assertNotIn(FAKE_AWS, joined)

    def test_work_item_anchor_persisted_and_objective_used(self) -> None:
        # When --work is passed, the checkpoint persists the anchor and
        # builds the objective from `work show ITEM --json`.
        fake_show = {
            "id": "W-0010",
            "title": "Rotate the staging API key",
            "sections": {
                "requirements": ["- [ ] Persist a bounded checkpoint"],
                "acceptanceCriteria": ["- [ ] Tests cover empty state"],
            },
        }
        orig_show = checkpoint._work_show_item
        checkpoint._work_show_item = lambda root, wid: fake_show  # type: ignore[assignment]
        try:
            data = checkpoint.build_checkpoint(self.root, source="codex",
                                               objective=None,
                                               work_item="W-0010")
        finally:
            checkpoint._work_show_item = orig_show  # type: ignore[assignment]
        self.assertEqual(data["work_item"], "W-0010")
        # Objective is the work item's title (first choice).
        self.assertEqual(data["objective"], "Rotate the staging API key")
        # Audit trail says we derived it from the title field.
        self.assertEqual(data["objective_source"]["work_item"], "W-0010")
        self.assertEqual(data["objective_source"]["field"], "title")

    def test_work_item_anchor_falls_back_to_requirement(self) -> None:
        fake_show = {
            "id": "W-0010",
            "title": "",
            "sections": {
                "requirements": ["Persist a bounded checkpoint"],
            },
        }
        orig_show = checkpoint._work_show_item
        checkpoint._work_show_item = lambda root, wid: fake_show  # type: ignore[assignment]
        try:
            data = checkpoint.build_checkpoint(self.root, source="codex",
                                               objective=None,
                                               work_item="W-0010")
        finally:
            checkpoint._work_show_item = orig_show  # type: ignore[assignment]
        self.assertEqual(data["objective"], "Persist a bounded checkpoint")
        self.assertEqual(data["objective_source"]["field"], "requirement")

    def test_work_item_anchor_missing_falls_through_to_active_items(self) -> None:
        # `work show` failing (CLI missing / bad output / non-existent
        # item) must NOT break the checkpoint — we fall through to the
        # generic active-items heuristic, fail-open.
        orig_show = checkpoint._work_show_item
        checkpoint._work_show_item = lambda root, wid: None  # type: ignore[assignment]
        orig_active = checkpoint._work_active_items
        checkpoint._work_active_items = lambda root: [  # type: ignore[assignment]
            {"id": "W-0010", "status": "in_progress", "priority": "high",
             "assignee": "codex", "title": "fallback objective"},
        ]
        try:
            data = checkpoint.build_checkpoint(self.root, source="codex",
                                               objective=None,
                                               work_item="W-MISSING")
        finally:
            checkpoint._work_show_item = orig_show  # type: ignore[assignment]
            checkpoint._work_active_items = orig_active  # type: ignore[assignment]
        self.assertEqual(data["work_item"], "W-MISSING")
        self.assertEqual(data["objective"], "fallback objective")
        self.assertIsNone(data["objective_source"])

    def test_explicit_objective_beats_work_item_anchor(self) -> None:
        # ``--objective`` always wins over ``--work`` anchor and active
        # items — the source is explicitly stating their goal.
        fake_show = {"id": "W-0010", "title": "from-work"}
        orig_show = checkpoint._work_show_item
        checkpoint._work_show_item = lambda root, wid: fake_show  # type: ignore[assignment]
        try:
            data = checkpoint.build_checkpoint(self.root, source="codex",
                                               objective="explicit wins",
                                               work_item="W-0010")
        finally:
            checkpoint._work_show_item = orig_show  # type: ignore[assignment]
        self.assertEqual(data["objective"], "explicit wins")
        self.assertEqual(data["objective_source"], None)

    def test_fallback_preserves_work_list_order_within_priority(self) -> None:
        # Tied priority: the first item ``work list`` emitted must win,
        # not the one with the smallest W id (which would let an ancient
        # W-0001 beat a current W-0010).
        orig_active = checkpoint._work_active_items
        checkpoint._work_active_items = lambda root: [  # type: ignore[assignment]
            {"id": "W-0001", "status": "in_progress", "priority": "high",
             "assignee": None, "title": "ancient high-prio"},
            {"id": "W-0010", "status": "in_progress", "priority": "high",
             "assignee": None, "title": "current high-prio"},
            {"id": "W-0050", "status": "in_progress", "priority": "medium",
             "assignee": None, "title": "medium prio"},
        ]
        try:
            data = checkpoint.build_checkpoint(self.root, source="codex",
                                               objective=None)
        finally:
            checkpoint._work_active_items = orig_active  # type: ignore[assignment]
        self.assertEqual(data["objective"], "ancient high-prio",
                         "first high-priority item in work-list order should win")

    def test_active_runs_limit_keeps_newest(self) -> None:
        # Build more than ACTIVE_RUNS_LIMIT active runs. The survivor
        # slice must contain the NEWEST active run ids, not the oldest.
        total = checkpoint.ACTIVE_RUNS_LIMIT + 5
        for i in range(total):
            _insert_run(self.root, status="spawning",
                        started_at=f"2026-07-18T12:{i:02d}:00Z",
                        slug=f"active_{i}")
        data = checkpoint.build_checkpoint(self.root, source="codex",
                                           objective="obj")
        ids = [r["id"] for r in data["active_runs"]]
        self.assertEqual(len(ids), checkpoint.ACTIVE_RUNS_LIMIT)
        # Chronological inside the slice.
        self.assertEqual(ids, sorted(ids))
        # Newest active run is preserved — the contract from the review.
        self.assertEqual(ids[-1], total,
                         "newest active run id must survive the LIMIT clip")

    def test_objective_truncated_when_explicit(self) -> None:
        long_obj = "y" * 1000
        data = checkpoint.build_checkpoint(self.root, source="codex",
                                           objective=long_obj)
        self.assertIsNotNone(data["objective"])
        self.assertLessEqual(len(data["objective"]),
                             checkpoint.OBJECTIVE_MAX_CHARS)

    def test_active_runs_capped(self) -> None:
        # ACTIVE_RUNS_LIMIT bounds active_runs so a runaway wave can't
        # make the checkpoint balloon.
        for i in range(checkpoint.ACTIVE_RUNS_LIMIT + 5):
            _insert_run(self.root,
                        started_at=f"2026-07-18T13:{i:02d}:00Z",
                        slug=f"slug_{i}")
        data = checkpoint.build_checkpoint(self.root, source="codex",
                                           objective="obj")
        self.assertLessEqual(len(data["active_runs"]),
                             checkpoint.ACTIVE_RUNS_LIMIT)


# ---------------------------------------------------------------------------
# write_checkpoint + load_checkpoint
# ---------------------------------------------------------------------------


class WriteLoadCheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp, self.root = _make_project()
        self._orig_active = checkpoint._work_active_items
        checkpoint._work_active_items = lambda root: []  # type: ignore[assignment]

    def tearDown(self) -> None:
        checkpoint._work_active_items = self._orig_active  # type: ignore[assignment]
        self._tmp.cleanup()

    def test_write_creates_file_under_state_dir(self) -> None:
        path = checkpoint.write_checkpoint(self.root, source="codex",
                                           objective="ship it")
        self.assertTrue(path.is_file())
        self.assertTrue(str(path).startswith(str(self.root / ".orchestra")))
        self.assertTrue(str(path).startswith(
            str(paths.checkpoints_dir(self.root, create=True))))

    def test_load_round_trips(self) -> None:
        path = checkpoint.write_checkpoint(self.root, source="codex",
                                           objective="ship it",
                                           next_steps=["merge"])
        ck = checkpoint.load_checkpoint(path)
        self.assertEqual(ck.source, "codex")
        self.assertEqual(ck.objective, "ship it")
        self.assertEqual(ck.next_steps, ["merge"])
        self.assertEqual(ck.path, path)

    def test_file_mode_is_private(self) -> None:
        path = checkpoint.write_checkpoint(self.root, source="codex",
                                           objective="obj")
        mode = path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_atomic_write_leaves_no_temp(self) -> None:
        path = checkpoint.write_checkpoint(self.root, source="codex",
                                           objective="obj")
        siblings = [p for p in path.parent.iterdir()
                    if p.name != path.name and p.name.startswith(path.name + ".")]
        self.assertEqual(siblings, [],
                         "atomic write left temp files behind: "
                         f"{[p.name for p in siblings]}")

    def test_filename_is_colon_free(self) -> None:
        path = checkpoint.write_checkpoint(self.root, source="codex",
                                           objective="obj")
        self.assertNotIn(":", path.name,
                         "filename must be colon-free for cross-platform safety")

    def test_list_checkpoints_orders_by_created_at_across_sources(self) -> None:
        # ``codex`` writes first, then ``claude`` 5ms later. ``claude``
        # must come back first even though "c" < "c" then "l" < "o" in
        # lexicographic order. Pure filename sort would put codex first.
        path_codex = checkpoint.write_checkpoint(self.root, source="codex",
                                                objective="first",
                                                next_steps=None)
        path_claude = checkpoint.write_checkpoint(self.root, source="claude",
                                                 objective="second",
                                                 next_steps=None)
        ordered = checkpoint.list_checkpoints(self.root)
        self.assertEqual(ordered[0], path_claude,
                         "list_checkpoints must order by created_at, "
                         "not lexicographic filename (claude is newer)")
        self.assertEqual(ordered[1], path_codex)

    def test_list_filtered_by_source(self) -> None:
        p1 = checkpoint.write_checkpoint(self.root, source="codex",
                                         objective="first")
        p2 = checkpoint.write_checkpoint(self.root, source="claude",
                                         objective="second")
        self.assertIn(p1, checkpoint.list_checkpoints(self.root, source="codex"))
        self.assertNotIn(p2, checkpoint.list_checkpoints(self.root, source="codex"))

    def test_unsupported_schema_raises(self) -> None:
        path = checkpoint.write_checkpoint(self.root, source="codex",
                                           objective="obj")
        payload = json.loads(path.read_text())
        payload["schema"] = "orchestra.checkpoint/v999"
        path.write_text(json.dumps(payload))
        with self.assertRaises(checkpoint.UnsupportedCheckpointError):
            checkpoint.load_checkpoint(path)

    def test_corrupt_json_raises(self) -> None:
        path = checkpoint.write_checkpoint(self.root, source="codex",
                                           objective="obj")
        path.write_text("{ this is not json")
        with self.assertRaises(checkpoint.CheckpointError):
            checkpoint.load_checkpoint(path)

    def test_missing_path_raises(self) -> None:
        # A nonexistent path is a CheckpointError, not FileNotFoundError,
        # so the CLI maps it to a clear SystemExit instead of a stack.
        ghost = self.root / ".orchestra" / "checkpoints" / "nope.json"
        with self.assertRaises(checkpoint.CheckpointError):
            checkpoint.load_checkpoint(ghost)

    def test_valid_json_but_wrong_shape_raises(self) -> None:
        # Valid JSON whose top-level is NOT an object must fail clearly
        # — not slip through to AttributeError during rendering.
        path = checkpoint.write_checkpoint(self.root, source="codex",
                                           objective="obj")
        path.write_text(json.dumps(["not", "an", "object"]))
        with self.assertRaises(checkpoint.CheckpointError):
            checkpoint.load_checkpoint(path)

    def test_missing_required_field_raises(self) -> None:
        path = checkpoint.write_checkpoint(self.root, source="codex",
                                           objective="obj")
        payload = json.loads(path.read_text())
        del payload["high_water"]  # required field
        path.write_text(json.dumps(payload))
        with self.assertRaises(checkpoint.CheckpointError) as cm:
            checkpoint.load_checkpoint(path)
        self.assertIn("high_water", str(cm.exception))

    def test_wrong_field_type_raises(self) -> None:
        path = checkpoint.write_checkpoint(self.root, source="codex",
                                           objective="obj")
        payload = json.loads(path.read_text())
        payload["active_runs"] = "should be a list"  # type: ignore[assignment]
        path.write_text(json.dumps(payload))
        with self.assertRaises(checkpoint.CheckpointError) as cm:
            checkpoint.load_checkpoint(path)
        self.assertIn("active_runs", str(cm.exception))

    def test_load_resanitizes_free_text_on_disk(self) -> None:
        # Defense in depth: hand-craft a checkpoint on disk with a
        # credential-shaped objective. load_checkpoint must scrub it
        # before it can ever reach the renderer.
        path = checkpoint.write_checkpoint(self.root, source="codex",
                                           objective="obj")
        payload = json.loads(path.read_text())
        payload["objective"] = "leak " + FAKE_SK
        payload["next_steps"] = ["step with " + FAKE_AWS]
        path.write_text(json.dumps(payload))
        ck = checkpoint.load_checkpoint(path)
        self.assertIn("[REDACTED]", ck.objective)
        self.assertNotIn(FAKE_SK, ck.objective or "")
        self.assertIn("[REDACTED]", ck.next_steps[0])
        self.assertNotIn(FAKE_AWS, ck.next_steps[0])

    def test_takeover_with_no_checkpoints_does_not_create_dir(self) -> None:
        # paths.checkpoints_dir(root) with create=False must NOT touch
        # the filesystem on read paths — a read-only takeover of a
        # never-checkpointed project must leave no trace.
        d = paths.checkpoints_dir(self.root, create=False)
        self.assertFalse(d.exists(),
                         "create=False must not materialize the dir")
        self.assertIsNone(checkpoint.latest_checkpoint(self.root))
        self.assertFalse(d.exists(),
                         "list_checkpoints must not materialize the dir either")
        self.assertEqual(checkpoint.list_checkpoints(self.root), [])


# ---------------------------------------------------------------------------
# takeover rendering
# ---------------------------------------------------------------------------


class TakeoverRenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp, self.root = _make_project()
        self._orig_active = checkpoint._work_active_items
        checkpoint._work_active_items = lambda root: [  # type: ignore[assignment]
            {"id": "W-0010", "status": "in_progress", "priority": "high",
             "assignee": "codex", "title": "Add checkpoint takeover"},
        ]

    def tearDown(self) -> None:
        checkpoint._work_active_items = self._orig_active  # type: ignore[assignment]
        self._tmp.cleanup()

    def _seed(self) -> Path:
        _insert_run(self.root, work_item="W-0010")
        _insert_message(self.root, sender="minimax", recipient="codex",
                        body="HANDOFF run 1: all done",
                        read_at="2026-07-18T12:05:00Z")
        _insert_feed(self.root, author="codex", body="noted x")
        return checkpoint.write_checkpoint(self.root, source="codex",
                                           objective="Resume work",
                                           next_steps=["merge branch",
                                                       "run full tests"])

    def test_brief_lists_active_runs_and_saved_messages(self) -> None:
        ck_path = self._seed()
        ck = checkpoint.load_checkpoint(ck_path)
        brief = checkpoint.render_takeover_brief(self.root, ck, target="claude")
        for needle in [
            "# Orchestra takeover",
            "**codex**",            # source identity
            "**claude**",           # target identity
            "Resume work",          # objective
            "merge branch",         # next step
            "run full tests",       # next step
            "HANDOFF run 1: all done",  # saved source-inbox message
            "noted x",              # saved feed
        ]:
            self.assertIn(needle, brief, f"brief missing section: {needle!r}")

    def test_brief_surfaces_messages_landed_after_checkpoint(self) -> None:
        ck_path = self._seed()
        _insert_message(self.root, sender="minimax", recipient="codex",
                        body="late notice from a worker",
                        created_at="2026-07-18T12:30:00Z")
        ck = checkpoint.load_checkpoint(ck_path)
        brief = checkpoint.render_takeover_brief(self.root, ck, target="claude")
        self.assertIn("late notice from a worker", brief)
        # Header reflects that the source inbox (recipient == checkpoint.source)
        # is what we're surfacing — not a generic "all messages" view.
        self.assertIn("Messages received by the source after the checkpoint",
                      brief)

    def test_brief_surfaces_runs_landed_after_checkpoint(self) -> None:
        ck_path = self._seed()
        new_run = _insert_run(self.root,
                              started_at="2026-07-18T12:30:00Z",
                              status="running",
                              slug="late_panda")
        ck = checkpoint.load_checkpoint(ck_path)
        brief = checkpoint.render_takeover_brief(self.root, ck, target="claude")
        self.assertIn(f" {new_run} ", brief.replace("|", " "))

    def test_brief_redacts_credentials_in_included_bodies(self) -> None:
        _insert_message(self.root, sender="minimax", recipient="codex",
                        body="leak " + FAKE_SK + " here")
        ck_path = checkpoint.write_checkpoint(self.root, source="codex",
                                              objective="obj")
        ck = checkpoint.load_checkpoint(ck_path)
        brief = checkpoint.render_takeover_brief(self.root, ck, target="claude")
        self.assertNotIn(FAKE_SK, brief)
        self.assertIn("[REDACTED]", brief)

    def test_brief_is_deterministic_for_same_checkpoint(self) -> None:
        # Re-rendering the SAME checkpoint must produce byte-equal
        # markdown. We compare the same load twice instead of writing
        # two checkpoints (whose timestamps would differ by microseconds).
        ck_path = self._seed()
        ck = checkpoint.load_checkpoint(ck_path)
        a = checkpoint.render_takeover_brief(self.root, ck, target="claude")
        b = checkpoint.render_takeover_brief(self.root, ck, target="claude")
        self.assertEqual(a, b)

    def test_no_session_ref_or_pid_or_summary_in_brief(self) -> None:
        _insert_run(self.root, summary="worker last text leaked here")
        ck_path = checkpoint.write_checkpoint(self.root, source="codex",
                                              objective="obj")
        ck = checkpoint.load_checkpoint(ck_path)
        brief = checkpoint.render_takeover_brief(self.root, ck, target="claude")
        for forbidden in [
            "session-secret-xyz",    # session_ref
            ".orchestra/logs/",      # log_path
            ".orchestra/briefs/",    # brief_path
            "orchestra/run-1",       # branch
            "worker last text leaked here",  # summary
        ]:
            self.assertNotIn(forbidden, brief,
                             f"sensitive surface leaked into brief: {forbidden!r}")


# ---------------------------------------------------------------------------
# Overflow + scoping tests — newest post-watermark entries kept; only the
# source recipient's mail is exposed.
# ---------------------------------------------------------------------------


class TakeoverOverflowTests(unittest.TestCase):
    """Pin the two post-watermark contracts:

      1. DESC LIMIT then reverse — when more than ``NEW_MESSAGES_LIMIT``
         new messages land after the checkpoint, the NEWEST ones (which
         include the most recent HANDOFF) survive, not the oldest.
      2. Post-watermark messages are scoped to ``recipient ==
         checkpoint.source`` so unrelated worker/other-orchestrator mail
         is never surfaced.

    Same shape for runs and feed.
    """

    def setUp(self) -> None:
        self._tmp, self.root = _make_project()
        self._orig_active = checkpoint._work_active_items
        checkpoint._work_active_items = lambda root: []  # type: ignore[assignment]

    def tearDown(self) -> None:
        checkpoint._work_active_items = self._orig_active  # type: ignore[assignment]
        self._tmp.cleanup()

    def test_post_watermark_messages_keeps_newest(self) -> None:
        ck_path = checkpoint.write_checkpoint(self.root, source="codex",
                                              objective="obj")
        ck = checkpoint.load_checkpoint(ck_path)
        # Insert MORE than NEW_MESSAGES_LIMIT new messages addressed to
        # the source AFTER the checkpoint was written.
        total = checkpoint.NEW_MESSAGES_LIMIT + 10
        for i in range(total):
            _insert_message(self.root, sender="minimax", recipient="codex",
                            body=f"new-msg-{i:03d}",
                            created_at=f"2026-07-18T13:{i:02d}:00Z")
        state = checkpoint._collect_takeover_state(self.root, ck)
        ids = [m["id"] for m in state["new_messages"]]
        # Count: NEW_MESSAGES_LIMIT kept.
        self.assertEqual(len(ids), checkpoint.NEW_MESSAGES_LIMIT)
        # Chronological (oldest -> newest) within the slice.
        self.assertEqual(ids, sorted(ids))
        # Newest available id is present — that's the key invariant.
        self.assertEqual(ids[-1], total,
                         "newest post-watermark message must survive the LIMIT clip")

    def test_post_watermark_messages_excludes_other_recipients(self) -> None:
        ck_path = checkpoint.write_checkpoint(self.root, source="codex",
                                              objective="obj")
        ck = checkpoint.load_checkpoint(ck_path)
        # One message to a different orchestrator, one to codex.
        _insert_message(self.root, sender="minimax", recipient="claude",
                        body="for claude, must not appear",
                        created_at="2026-07-18T13:00:00Z")
        target_id = _insert_message(self.root, sender="minimax",
                                    recipient="codex",
                                    body="for codex, must appear",
                                    created_at="2026-07-18T13:01:00Z")
        state = checkpoint._collect_takeover_state(self.root, ck)
        ids = [m["id"] for m in state["new_messages"]]
        recipients = {m["recipient"] for m in state["new_messages"]}
        self.assertEqual(recipients, {"codex"},
                         "post-watermark messages must scope to checkpoint.source")
        self.assertIn(target_id, ids)
        # Defense in depth: the other-orchestrator body string is
        # nowhere in the takeover brief (this is the user-visible
        # assertion that motivates the scoping).
        brief = checkpoint.render_takeover_brief(self.root, ck, target="glm")
        self.assertNotIn("for claude, must not appear", brief)
        self.assertIn("for codex, must appear", brief)

    def test_post_watermark_runs_keeps_newest(self) -> None:
        ck_path = checkpoint.write_checkpoint(self.root, source="codex",
                                              objective="obj")
        ck = checkpoint.load_checkpoint(ck_path)
        total = checkpoint.NEW_RUNS_LIMIT + 5
        for i in range(total):
            _insert_run(self.root, started_at=f"2026-07-18T13:{i:02d}:00Z",
                        status="running", slug=f"late_{i}")
        state = checkpoint._collect_takeover_state(self.root, ck)
        ids = [r["id"] for r in state["new_runs"]]
        self.assertEqual(len(ids), checkpoint.NEW_RUNS_LIMIT)
        self.assertEqual(ids[-1], total,
                         "newest post-watermark run id must be in the slice")

    def test_post_watermark_feed_keeps_newest(self) -> None:
        ck_path = checkpoint.write_checkpoint(self.root, source="codex",
                                              objective="obj")
        ck = checkpoint.load_checkpoint(ck_path)
        total = checkpoint.NEW_FEED_LIMIT + 5
        for i in range(total):
            _insert_feed(self.root, author="codex",
                         body=f"feed-{i:03d}",
                         created_at=f"2026-07-18T13:{i:02d}:00Z")
        state = checkpoint._collect_takeover_state(self.root, ck)
        ids = [f["id"] for f in state["new_feed"]]
        self.assertEqual(len(ids), checkpoint.NEW_FEED_LIMIT)
        self.assertEqual(ids[-1], total,
                         "newest post-watermark feed id must be in the slice")


# ---------------------------------------------------------------------------
# End-to-end CLI integration: cmd_checkpoint / cmd_takeover
# ---------------------------------------------------------------------------


class CmdCheckpointTakeoverTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp, self.root = _make_project()
        self._orig_find_root = cli.paths.find_root
        cli.paths.find_root = lambda: self.root  # type: ignore[assignment]
        self._orig_active = checkpoint._work_active_items
        checkpoint._work_active_items = lambda root: [  # type: ignore[assignment]
            {"id": "W-0010", "status": "in_progress", "priority": "high",
             "assignee": "codex", "title": "Add checkpoint takeover"},
        ]

    def tearDown(self) -> None:
        cli.paths.find_root = self._orig_find_root  # type: ignore[assignment]
        checkpoint._work_active_items = self._orig_active  # type: ignore[assignment]
        self._tmp.cleanup()

    def _run(self, args: Namespace) -> tuple[str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if args.fn_name == "checkpoint":
                cli.cmd_checkpoint(args)
            else:
                cli.cmd_takeover(args)
        return out.getvalue(), err.getvalue()

    def test_cmd_checkpoint_writes_file(self) -> None:
        args = Namespace(as_="codex", objective="obj", next=["a", "b"],
                         work=None, fn_name="checkpoint")
        out, _ = self._run(args)
        self.assertIn("checkpoint:", out)
        files = list(paths.checkpoints_dir(self.root, create=True).glob("*.json"))
        self.assertEqual(len(files), 1)
        payload = json.loads(files[0].read_text())
        self.assertEqual(payload["source"], "codex")
        self.assertEqual(payload["next_steps"], ["a", "b"])
        self.assertEqual(payload["objective"], "obj")

    def test_cmd_checkpoint_preserves_explicit_identity(self) -> None:
        # ``--as custom`` lands as ``source`` in the JSON.
        args = Namespace(as_="custom-orchestrator", objective="obj", next=[],
                         work=None, fn_name="checkpoint")
        self._run(args)
        files = list(paths.checkpoints_dir(self.root, create=True).glob("*.json"))
        payload = json.loads(files[0].read_text())
        self.assertEqual(payload["source"], "custom-orchestrator")

    def test_cmd_takeover_prints_brief(self) -> None:
        self._run(Namespace(as_="codex", objective="resume",
                            next=["verify"], work=None, fn_name="checkpoint"))
        out, _ = self._run(Namespace(as_="claude", from_=None,
                                     checkpoint=None, json=False,
                                     fn_name="takeover"))
        self.assertIn("# Orchestra takeover", out)
        self.assertIn("resume", out)

    def test_cmd_takeover_json_emits_structured(self) -> None:
        self._run(Namespace(as_="codex", objective="resume",
                            next=[], work=None, fn_name="checkpoint"))
        out, _ = self._run(Namespace(as_="claude", from_=None,
                                     checkpoint=None, json=True,
                                     fn_name="takeover"))
        payload = json.loads(out)
        self.assertEqual(payload["source"], "codex")
        self.assertEqual(payload["target"], "claude")
        self.assertIn("brief", payload)
        self.assertIn("# Orchestra takeover", payload["brief"])

    def test_cmd_takeover_from_source_picks_matching(self) -> None:
        self._run(Namespace(as_="codex", objective="from-codex",
                            next=[], work=None, fn_name="checkpoint"))
        self._run(Namespace(as_="claude", objective="from-claude",
                            next=[], work=None, fn_name="checkpoint"))
        out, _ = self._run(Namespace(as_="glm", from_="codex",
                                     checkpoint=None, json=True,
                                     fn_name="takeover"))
        payload = json.loads(out)
        self.assertEqual(payload["source"], "codex")
        self.assertEqual(payload["objective"], "from-codex")

    def test_cmd_takeover_no_checkpoint_errors(self) -> None:
        args = Namespace(as_="claude", from_=None, checkpoint=None,
                         json=False, fn_name="takeover")
        with self.assertRaises(SystemExit):
            self._run(args)

    def test_cmd_takeover_never_mutates_source_state(self) -> None:
        self._run(Namespace(as_="codex", objective="obj", next=[],
                            work=None, fn_name="checkpoint"))
        con = db.connect(self.root)
        try:
            runs_before = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            feed_before = con.execute("SELECT COUNT(*) FROM feed").fetchone()[0]
            unread_before = con.execute(
                "SELECT COUNT(*) FROM messages WHERE read_at IS NULL"
            ).fetchone()[0]
            msgs_before = con.execute(
                "SELECT COUNT(*) FROM messages"
            ).fetchone()[0]
        finally:
            con.close()
        self._run(Namespace(as_="claude", from_=None,
                            checkpoint=None, json=False, fn_name="takeover"))
        con = db.connect(self.root)
        try:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
                             runs_before)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM feed").fetchone()[0],
                             feed_before)
            self.assertEqual(con.execute(
                "SELECT COUNT(*) FROM messages WHERE read_at IS NULL"
            ).fetchone()[0], unread_before)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                             msgs_before)
        finally:
            con.close()
        # And the checkpoints dir still contains exactly one file (the
        # one we wrote), proving takeover did not add its own artifact.
        files = list(paths.checkpoints_dir(self.root, create=True).glob("*.json"))
        self.assertEqual(len(files), 1)

    def test_cmd_takeover_unsupported_checkpoint_errors(self) -> None:
        self._run(Namespace(as_="codex", objective="obj", next=[],
                            work=None, fn_name="checkpoint"))
        files = list(paths.checkpoints_dir(self.root, create=True).glob("*.json"))
        self.assertEqual(len(files), 1)
        payload = json.loads(files[0].read_text())
        payload["schema"] = "orchestra.checkpoint/v999"
        files[0].write_text(json.dumps(payload))
        with self.assertRaises(SystemExit):
            self._run(Namespace(as_="claude", from_=None,
                                checkpoint=str(files[0]), json=False,
                                fn_name="takeover"))


# ---------------------------------------------------------------------------
# Strict read-only takeover — takeover must use db.connect_readonly and
# never touch the source DB. We prove this by patching db.connect to
# raise during render/takeover; if takeover still works, it didn't use
# the writable opener. We then snapshot the on-disk state before/after
# to prove no sidecar files were created.
# ---------------------------------------------------------------------------


class StrictReadOnlyTakeoverTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp, self.root = _make_project()
        self._orig_find_root = cli.paths.find_root
        cli.paths.find_root = lambda: self.root  # type: ignore[assignment]
        self._orig_active = checkpoint._work_active_items
        self._orig_connect = db.connect
        self._orig_connect_ro = db.connect_readonly
        checkpoint._work_active_items = lambda root: []  # type: ignore[assignment]

    def tearDown(self) -> None:
        cli.paths.find_root = self._orig_find_root  # type: ignore[assignment]
        checkpoint._work_active_items = self._orig_active  # type: ignore[assignment]
        db.connect = self._orig_connect  # type: ignore[assignment]
        db.connect_readonly = self._orig_connect_ro  # type: ignore[assignment]
        self._tmp.cleanup()

    def _seed_checkpoint(self) -> None:
        self._run(Namespace(as_="codex", objective="obj", next=[],
                            work=None, fn_name="checkpoint"))

    def _run(self, args: Namespace) -> tuple[str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if args.fn_name == "checkpoint":
                cli.cmd_checkpoint(args)
            else:
                cli.cmd_takeover(args)
        return out.getvalue(), err.getvalue()

    def test_takeover_uses_readonly_opener(self) -> None:
        # If takeover accidentally fell back to the writable opener,
        # this raises and the test fails loudly. We seed BEFORE the
        # patch — seeding itself uses the writable opener by design
        # (writing a checkpoint needs INSERT/MAX/atomic-rename), but
        # the takeover under test must use only the read-only opener.
        self._seed_checkpoint()
        def fail_if_called(*a, **kw):
            raise AssertionError(
                "takeover must use db.connect_readonly, not db.connect"
            )
        db.connect = fail_if_called  # type: ignore[assignment]
        db.connect_readonly = self._orig_connect_ro  # type: ignore[assignment]
        out, _ = self._run(Namespace(as_="claude", from_=None,
                                     checkpoint=None, json=False,
                                     fn_name="takeover"))
        self.assertIn("# Orchestra takeover", out)

    def test_takeover_does_not_modify_db_file(self) -> None:
        # On-disk evidence of strict read-only: the DB file's mtime and
        # size are unchanged after takeover. SQLite's WAL-mode DBs
        # create ``-wal`` / ``-shm`` sidecars even when opened in
        # ``mode=ro`` (it's a documented SQLite quirk), but the DB
        # file itself is never written to.
        self._seed_checkpoint()
        db_file = self.root / ".orchestra" / "orchestra.db"
        before_mtime = db_file.stat().st_mtime_ns
        before_size = db_file.stat().st_size
        # Open and immediately close a writable connection so the WAL
        # checkpoint is flushed — establishes a clean baseline.
        con = self._orig_connect(self.root)
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            con.close()
        before_mtime = db_file.stat().st_mtime_ns
        before_size = db_file.stat().st_size
        self._run(Namespace(as_="claude", from_=None, checkpoint=None,
                            json=False, fn_name="takeover"))
        self.assertEqual(db_file.stat().st_size, before_size,
                         "takeover wrote to the DB file (size changed)")
        self.assertEqual(db_file.stat().st_mtime_ns, before_mtime,
                         "takeover wrote to the DB file (mtime changed)")

    def test_render_brief_uses_readonly_opener(self) -> None:
        # Lower-level test: the brief renderer itself must not invoke
        # the writable opener. Catch accidental regression by patching
        # both openers to count calls.
        self._seed_checkpoint()
        calls = {"rw": 0, "ro": 0}

        def counting_connect(*a, **kw):
            calls["rw"] += 1
            return self._orig_connect(*a, **kw)

        def counting_connect_ro(*a, **kw):
            calls["ro"] += 1
            return self._orig_connect_ro(*a, **kw)

        db.connect = counting_connect  # type: ignore[assignment]
        db.connect_readonly = counting_connect_ro  # type: ignore[assignment]
        ck = checkpoint.latest_checkpoint(self.root)
        self.assertIsNotNone(ck)
        checkpoint.render_takeover_brief(self.root, ck, target="claude")
        self.assertEqual(calls["rw"], 0,
                         "render_takeover_brief must not open the DB "
                         "with the writable opener")
        self.assertEqual(calls["ro"], 1,
                         "render_takeover_brief must use the read-only opener")


# ---------------------------------------------------------------------------
# CLI argument wiring — make sure --work threads through.
# ---------------------------------------------------------------------------


class CheckpointWorkItemArgTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp, self.root = _make_project()
        self._orig_find_root = cli.paths.find_root
        cli.paths.find_root = lambda: self.root  # type: ignore[assignment]
        self._orig_active = checkpoint._work_active_items
        checkpoint._work_active_items = lambda root: []  # type: ignore[assignment]

    def tearDown(self) -> None:
        cli.paths.find_root = self._orig_find_root  # type: ignore[assignment]
        checkpoint._work_active_items = self._orig_active  # type: ignore[assignment]
        self._tmp.cleanup()

    def _run(self, args: Namespace) -> tuple[str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            cli.cmd_checkpoint(args)
        return out.getvalue(), err.getvalue()

    def test_cmd_checkpoint_persists_work_item(self) -> None:
        # No real `work show` is available in the test sandbox; stub it
        # to a deterministic payload so we exercise the full code path
        # including the work-item anchor lookup.
        fake_show = {"id": "W-0099", "title": "anchored objective"}
        orig_show = checkpoint._work_show_item
        checkpoint._work_show_item = lambda root, wid: fake_show  # type: ignore[assignment]
        try:
            self._run(Namespace(as_="codex", objective=None, next=[],
                                work="W-0099", fn_name="checkpoint"))
        finally:
            checkpoint._work_show_item = orig_show  # type: ignore[assignment]
        files = list(paths.checkpoints_dir(self.root, create=True).glob("*.json"))
        self.assertEqual(len(files), 1)
        payload = json.loads(files[0].read_text())
        self.assertEqual(payload["work_item"], "W-0099")
        self.assertEqual(payload["objective"], "anchored objective")
        self.assertEqual(payload["objective_source"]["field"], "title")


class LoadedCheckpointIntegrityTests(unittest.TestCase):
    """Exercise the real write -> load -> render boundary.

    Unit-level sanitizer assertions are not enough: nested row fields must
    survive loading while unknown keys and malformed rows are rejected.
    """

    def setUp(self) -> None:
        self._tmp, self.root = _make_project()
        self._orig_active = checkpoint._work_active_items
        checkpoint._work_active_items = lambda root: []  # type: ignore[assignment]

    def tearDown(self) -> None:
        checkpoint._work_active_items = self._orig_active  # type: ignore[assignment]
        self._tmp.cleanup()

    def test_nested_state_survives_write_load_and_render(self) -> None:
        run_id = _insert_run(self.root, status="running", title="active recovery run")
        message_id = _insert_message(
            self.root, sender="minimax", recipient="codex",
            body="HANDOFF: nested state survived loading",
        )
        path = checkpoint.write_checkpoint(
            self.root, source="codex", objective="resume safely",
        )
        loaded = checkpoint.load_checkpoint(path)

        self.assertEqual(loaded.data["project"]["id"],
                         checkpoint.projects.project_id(self.root))
        self.assertEqual(loaded.data["active_runs"][0]["id"], run_id)
        saved = loaded.data["source_inbox_snapshot"][0]
        self.assertEqual(saved["id"], message_id)
        self.assertIn("nested state survived", saved["body_preview"])

        brief = checkpoint.render_takeover_brief(
            self.root, loaded, target="claude",
        )
        self.assertIn("active recovery run", brief)
        self.assertIn("HANDOFF: nested state survived loading", brief)

    def test_malformed_nested_row_is_rejected(self) -> None:
        path = checkpoint.write_checkpoint(
            self.root, source="codex", objective="resume safely",
        )
        payload = json.loads(path.read_text())
        payload["active_runs"] = ["not a row"]
        path.write_text(json.dumps(payload))
        with self.assertRaises(checkpoint.CheckpointError):
            checkpoint.load_checkpoint(path)

    def test_loaded_lists_are_bounded_to_newest_slice(self) -> None:
        path = checkpoint.write_checkpoint(
            self.root, source="codex", objective="resume safely",
        )
        payload = json.loads(path.read_text())
        payload["source_inbox_snapshot"] = [
            {"id": i, "sender": "worker", "recipient": "codex",
             "body_preview": f"message {i}"}
            for i in range(checkpoint.INBOX_SNAPSHOT_LIMIT + 5)
        ]
        path.write_text(json.dumps(payload))
        loaded = checkpoint.load_checkpoint(path)
        rows = loaded.data["source_inbox_snapshot"]
        self.assertEqual(len(rows), checkpoint.INBOX_SNAPSHOT_LIMIT)
        self.assertEqual(rows[-1]["id"], checkpoint.INBOX_SNAPSHOT_LIMIT + 4)


class CheckpointProjectBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp, self.root = _make_project()
        self._orig_find_root = cli.paths.find_root
        self._orig_active = checkpoint._work_active_items
        cli.paths.find_root = lambda: self.root  # type: ignore[assignment]
        checkpoint._work_active_items = lambda root: []  # type: ignore[assignment]

    def tearDown(self) -> None:
        cli.paths.find_root = self._orig_find_root  # type: ignore[assignment]
        checkpoint._work_active_items = self._orig_active  # type: ignore[assignment]
        self._tmp.cleanup()

    def test_explicit_checkpoint_from_another_project_is_rejected(self) -> None:
        path = checkpoint.write_checkpoint(
            self.root, source="codex", objective="resume safely",
        )
        payload = json.loads(path.read_text())
        payload["project"]["id"] = "different-project"
        path.write_text(json.dumps(payload))
        args = Namespace(
            as_="claude", from_=None, checkpoint=str(path), json=False,
        )
        with self.assertRaisesRegex(SystemExit, "different project"):
            cli.cmd_takeover(args)


class ReadonlyUriPathTests(unittest.TestCase):
    def test_readonly_connection_quotes_uri_path_characters(self) -> None:
        with tempfile.TemporaryDirectory(prefix="orchestra?readonly-") as tmp:
            root = Path(tmp)
            (root / ".orchestra").mkdir()
            db.connect(root).close()
            con = db.connect_readonly(root)
            try:
                self.assertEqual(con.execute("SELECT 1").fetchone()[0], 1)
            finally:
                con.close()


if __name__ == "__main__":
    sys.exit(unittest.main())
