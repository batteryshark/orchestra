"""The outbound feed: a cursored read, not a callback (CONTRACT §7, 0.10).

Orchestra publishes run rows with a monotonic marker and holds nothing about
who reads them — no subscriber list, no endpoint, no delivery state. So the
tests here are about exactly two things: that the marker is stamped once per
change and never on a read, and that a consumer walking ``next_cursor`` sees
every row exactly once.

The stamping half rests on one assumption about SQLite that the wave was told
not to take on faith — a trigger on ``runs`` that writes ``runs`` must not
re-enter itself — so ``test_recursive_triggers_stays_off`` asserts the pragma
and ``test_one_update_advances_the_revision_once`` asserts the consequence.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestra import auth, db
from orchestra import http as mhttp
from tests.test_http import KEY, ServerCase


def make_run(con, **fields) -> int:
    row = {"profile": "codex", "backend": "codex", "requested_by": "human",
           "workdir": "/tmp", "status": "running", "started_at": db.now()}
    row.update(fields)
    columns = ", ".join(row)
    marks = ", ".join("?" * len(row))
    run_id = int(con.execute(
        f"INSERT INTO runs({columns}) VALUES({marks})",
        tuple(row.values())).lastrowid)
    con.commit()
    return run_id


def revision_of(con, run_id: int) -> int:
    return con.execute("SELECT revision FROM runs WHERE id=?",
                       (run_id,)).fetchone()["revision"]


class StampTests(unittest.TestCase):
    """The change marker: once per change, never on a read."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {
            "ORCHESTRA_HOME": str(Path(self.tmp.name) / "home"),
            "ORCHESTRA_CONFIG": str(Path(self.tmp.name) / "config.toml")})
        self.env.start()
        self.con = db.connect()

    def tearDown(self) -> None:
        self.con.close()
        self.env.stop()
        self.tmp.cleanup()

    def test_recursive_triggers_stays_off(self) -> None:
        """The whole stamp design rests on this being 0 on a live connection.

        Turning it on would make the update trigger's own write re-enter the
        update trigger, and the counter would not terminate.
        """
        self.assertEqual(
            self.con.execute("PRAGMA recursive_triggers").fetchone()[0], 0)

    def test_an_insert_advances_once_and_stamps_the_row(self) -> None:
        before = db.board_revision(self.con)
        run_id = make_run(self.con)
        self.assertEqual(db.board_revision(self.con), before + 1)
        self.assertEqual(revision_of(self.con, run_id), before + 1)

    def test_one_update_advances_the_revision_once(self) -> None:
        """The trap: an AFTER UPDATE trigger that updates ``runs``."""
        run_id = make_run(self.con)
        before = db.board_revision(self.con)
        self.con.execute("UPDATE runs SET summary='half done' WHERE id=?",
                         (run_id,))
        self.con.commit()
        self.assertEqual(db.board_revision(self.con), before + 1)
        self.assertEqual(revision_of(self.con, run_id), before + 1)

    def test_a_read_advances_nothing(self) -> None:
        make_run(self.con)
        before = db.board_revision(self.con)
        mhttp.runs_since(0, con=self.con)
        list(self.con.execute("SELECT * FROM runs"))
        self.assertEqual(db.board_revision(self.con), before)

    def test_one_statement_marks_each_row_it_touches_apart(self) -> None:
        """Two rows changed together must not share one marker, or a page
        boundary between them would skip the second."""
        first, second = make_run(self.con), make_run(self.con)
        self.con.execute("UPDATE runs SET summary='swept'")
        self.con.commit()
        self.assertNotEqual(revision_of(self.con, first),
                            revision_of(self.con, second))

    def test_a_revoking_update_advances_twice_and_no_further(self) -> None:
        """A terminal status is TWO writes to the row: the caller's, and the
        ``revoke_run_token`` trigger's own. Both are real changes and both
        count; the point of the assertion is the ceiling — the row ends with
        one marker and the counter does not run away.
        """
        run_id = make_run(self.con)
        auth.mint(self.con, run_id)
        before = db.board_revision(self.con)
        self.con.execute("UPDATE runs SET status='done' WHERE id=?", (run_id,))
        self.con.commit()
        self.assertEqual(db.board_revision(self.con), before + 2)
        self.assertEqual(revision_of(self.con, run_id), before + 2)

    def test_an_older_database_gets_every_row_stamped(self) -> None:
        """The v22 upgrade. A run recorded before the column existed would be
        invisible to a consumer starting at cursor 0, so ``connect`` touches
        each one and lets the trigger mark it, in id order."""
        ids = [make_run(self.con) for _ in range(5)]
        for trigger in ("bump_board_revision_insert", "bump_board_revision_update"):
            self.con.execute(f"DROP TRIGGER {trigger}")
        self.con.execute("DROP INDEX idx_runs_revision")
        self.con.execute("ALTER TABLE runs DROP COLUMN revision")
        self.con.commit()
        self.con.close()

        self.con = db.connect()  # the migration runs here
        marks = [revision_of(self.con, run_id) for run_id in ids]
        self.assertEqual(marks, sorted(marks), "stamped out of id order")
        self.assertTrue(all(marks), "a row was left unmarked")
        self.assertEqual([r["id"] for r in mhttp.runs_since(0, con=self.con)["runs"]],
                         ids)


class FeedTests(unittest.TestCase):
    """``runs_since``: only what is past the cursor, exactly once."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {
            "ORCHESTRA_HOME": str(Path(self.tmp.name) / "home"),
            "ORCHESTRA_CONFIG": str(Path(self.tmp.name) / "config.toml")})
        self.env.start()
        self.con = db.connect()
        self.ids = [make_run(self.con, ref=f"W-{n:04d}") for n in range(10)]

    def tearDown(self) -> None:
        self.con.close()
        self.env.stop()
        self.tmp.cleanup()

    def read(self, since=0, limit=mhttp.FEED_PAGE) -> dict:
        return mhttp.runs_since(since, limit, con=self.con)

    def test_only_rows_past_the_cursor_oldest_first(self) -> None:
        page = self.read()
        self.assertEqual([r["id"] for r in page["runs"]], self.ids)
        marks = [r["revision"] for r in page["runs"]]
        self.assertEqual(marks, sorted(marks))
        cut = marks[4]
        self.assertEqual([r["id"] for r in self.read(cut)["runs"]], self.ids[5:])

    def test_the_next_cursor_resumes_with_nothing_lost_or_repeated(self) -> None:
        seen, cursor, pages = [], 0, 0
        while True:
            page = self.read(cursor, limit=3)
            pages += 1
            seen += [r["id"] for r in page["runs"]]
            if len(page["runs"]) < page["limit"]:
                break
            cursor = page["next_cursor"]
        self.assertEqual(seen, self.ids)
        self.assertEqual(pages, 4)

    def test_an_exhausted_feed_hands_the_cursor_back(self) -> None:
        end = self.read()["next_cursor"]
        page = self.read(end)
        self.assertEqual(page["runs"], [])
        self.assertEqual(page["next_cursor"], end)

    def test_a_changed_run_reappears_past_the_cursor(self) -> None:
        cursor = self.read()["next_cursor"]
        self.con.execute("UPDATE runs SET status='done', summary='landed' "
                         "WHERE id=?", (self.ids[0],))
        self.con.commit()
        page = self.read(cursor)
        self.assertEqual([r["id"] for r in page["runs"]], [self.ids[0]])
        self.assertEqual(page["runs"][0]["summary"], "landed")

    def test_a_new_run_appears_and_a_deleted_one_stops(self) -> None:
        cursor = self.read()["next_cursor"]
        fresh = make_run(self.con, ref="W-9999")
        self.assertEqual([r["id"] for r in self.read(cursor)["runs"]], [fresh])
        self.con.execute("DELETE FROM runs WHERE id=?", (fresh,))
        self.con.commit()
        self.assertEqual(self.read(cursor)["runs"], [])
        self.assertNotIn(fresh, [r["id"] for r in self.read(0)["runs"]])

    def test_the_page_bound_is_a_ceiling_and_is_visible(self) -> None:
        self.assertEqual(self.read(limit=99999)["limit"], mhttp.FEED_PAGE)
        page = self.read(limit=4)
        self.assertEqual((page["limit"], len(page["runs"])), (4, 4))
        self.assertEqual(self.read(limit="rubbish")["limit"], mhttp.FEED_PAGE)
        self.assertEqual(self.read("rubbish")["cursor"], 0)

    def test_the_payload_carries_what_a_consumer_acts_on(self) -> None:
        """The fields ``sweeper``'s reporting path reads off a run row, plus
        the usage that prices the outcome."""
        run_id = make_run(self.con, ref="W-0301", slug="calm_otter",
                          status="done", summary="all four criteria answered",
                          branch="orch/w-0301", landing_status="ok",
                          handoff_processed_at=db.now(), project_id="p-1",
                          project_seq=7, tokens_total=1200, cost_usd=0.42,
                          usage_source="codex")
        row = next(r for r in self.read()["runs"] if r["id"] == run_id)
        self.assertEqual(
            {k: row[k] for k in ("ref", "slug", "no", "status", "summary",
                                 "branch", "landing_status", "cost_usd")},
            {"ref": "W-0301", "slug": "calm_otter", "no": 7, "status": "done",
             "summary": "all four criteria answered", "branch": "orch/w-0301",
             "landing_status": "ok", "cost_usd": 0.42})
        self.assertTrue(row["handoff_processed_at"])
        self.assertEqual((row["project_id"], row["tokens_total"],
                          row["usage_source"], row["requested_by"]),
                         ("p-1", 1200, "codex", "human"))
        self.assertNotIn("run_token_hash", row)

    def test_a_summary_is_not_truncated(self) -> None:
        """The consumer carries it onward; the length limit belongs on the
        comment it posts, not on the read."""
        long = "x" * (mhttp.SUMMARY_CHARS + 500)
        run_id = make_run(self.con, summary=long)
        row = next(r for r in self.read()["runs"] if r["id"] == run_id)
        self.assertEqual(row["summary"], long)


class FeedAuthTests(ServerCase):
    """The feed is the human's alone.

    It returns every run's outcome across every project — refs, summaries,
    branches, cost — which is wider than ``/api/snapshot``'s window and has
    no per-run scope to fall back on. No run needs to read its siblings'
    outcomes, so nothing argues for lowering it from the unlisted default.
    """

    def test_the_route_key_is_not_swallowed_by_a_catch_all(self) -> None:
        key, target = auth.route_key("GET", mhttp.RUNS_ROUTE)
        self.assertEqual((key, target), ("GET /api/runs", None))
        self.assertNotIn(key, auth.ROUTES)
        self.assertEqual(auth.ROUTES.get(key, auth.DEFAULT_LEVEL),
                         auth.ONLY_HUMAN)

    def test_a_run_token_is_refused_and_the_human_key_is_not(self) -> None:
        run_id = make_run(self.con, ref="W-0400")
        token = auth.mint(self.con, run_id)
        status, _ = self.request(path="/api/runs?since=0", key=token)
        self.assertEqual(status, 403)
        status, payload = self.json_request(path="/api/runs?since=0", key=KEY)
        self.assertEqual(status, 200)
        self.assertEqual([r["ref"] for r in payload["runs"]], ["W-0400"])
        self.assertEqual(payload["limit"], mhttp.FEED_PAGE)
        self.assertEqual(payload["next_cursor"], payload["runs"][0]["revision"])


if __name__ == "__main__":
    unittest.main()
