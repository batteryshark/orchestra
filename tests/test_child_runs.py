"""A run asks for help: bounded child runs on weaker profiles.

Ported from the original Orchestra, where this worked. The current tree kept
only the validator and W-0291 removed it as a phantom surface, because
nothing launched behind it. What is tested here is the part that has to hold
when a model is the one asking: the bounds, the direction of the handoff, and
the fact that a worker only ever WRITES a request.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestra import child_runs, config, db

CONFIG = """
[settings]
default_requester = "human"

[profiles.lead]
backend = "opencode"
tier = 3

[profiles.helper]
backend = "opencode"
tier = 1

[profiles.peer]
backend = "opencode"
tier = 3

[profiles.stronger]
backend = "opencode"
tier = 3
"""


class ChildRunCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.config_path = self.root / "config.toml"
        self.config_path.write_text(CONFIG)
        self.env = mock.patch.dict(os.environ, {
            "ORCHESTRA_HOME": str(self.root / "home"),
            "ORCHESTRA_CONFIG": str(self.config_path)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.con = db.connect()
        self.addCleanup(self.con.close)
        self.cfg = config.load()
        self.lead = self.make_run("lead", status="running")

    def make_run(self, profile="lead", **cols):
        fields = {"profile": profile, "backend": "opencode", "title": "lead work",
                  "requested_by": "human", "workdir": str(self.root),
                  "status": "running", "started_at": db.now(),
                  "session_ref": "sess-lead"}
        fields.update(cols)
        names = ", ".join(fields)
        run_id = int(self.con.execute(
            f"INSERT INTO runs({names}) VALUES({', '.join('?' * len(fields))})",
            tuple(fields.values())).lastrowid)
        self.con.commit()
        return self.con.execute("SELECT * FROM runs WHERE id=?",
                                (run_id,)).fetchone()

    def _no_worktree(self):
        """Children in the lead's workdir: the git plumbing is not what
        these cases are about."""
        return mock.patch.object(child_runs.worktree, "create",
                                 side_effect=AssertionError("shared expected"))


class BoundsTests(ChildRunCase):
    def test_help_goes_down_the_tiers_never_up(self) -> None:
        """The whole point is a cheap model taking a bounded piece. Asking
        for a STRONGER one is asking for a different decomposition, which
        belongs to the human who set the mission."""
        weak = self.make_run("helper", status="running")
        with self.assertRaises(SystemExit) as caught:
            child_runs.validate_targets(self.cfg, weak, ["stronger"])
        self.assertIn("outranks", str(caught.exception))
        self.assertIn("ask the human", str(caught.exception))
        # Down and sideways are both fine.
        child_runs.validate_targets(self.cfg, self.lead, ["helper"])
        child_runs.validate_targets(self.cfg, self.lead, ["peer"])

    def test_a_run_may_not_clone_itself(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            child_runs.validate_targets(self.cfg, self.lead, ["lead"])
        self.assertIn("handing work DOWN", str(caught.exception))

    def test_a_profile_this_project_disabled_is_refused(self) -> None:
        cfg = {**self.cfg, "enabled_profiles": ["lead"]}
        with self.assertRaises(SystemExit) as caught:
            child_runs.validate_targets(cfg, self.lead, ["helper"])
        self.assertIn("not enabled", str(caught.exception))

    def test_depth_fails_closed_so_a_child_cannot_recurse(self) -> None:
        """Default depth is 1: the lead may ask for help, the help may not."""
        child = self.make_run("helper", status="running", parent_run=self.lead["id"],
                              child_depth=1, requested_by=child_runs.REQUESTED_BY)
        with self.assertRaises(SystemExit) as caught:
            child_runs.validate_parent(self.con, self.cfg, child["id"], child["id"])
        self.assertIn("depth limit", str(caught.exception))

    def test_a_run_may_only_ask_for_itself(self) -> None:
        other = self.make_run("lead", status="running")
        with self.assertRaises(SystemExit) as caught:
            child_runs.validate_parent(self.con, self.cfg, other["id"],
                                       self.lead["id"])
        self.assertIn("for itself", str(caught.exception))

    def test_a_finished_run_asks_for_nothing(self) -> None:
        done = self.make_run("lead", status="done")
        with self.assertRaises(SystemExit) as caught:
            child_runs.validate_parent(self.con, self.cfg, done["id"], done["id"])
        self.assertIn("is done", str(caught.exception))

    def test_a_bad_limit_is_refused_not_coerced(self) -> None:
        for value in (True, -1, "two"):
            with self.assertRaises(SystemExit):
                child_runs.limits({"settings": {"child_max_depth": value}})
        self.assertEqual((1, 3, 3), child_runs.limits({}))


class CreateTests(ChildRunCase):
    def test_a_batch_records_the_edge_and_the_depth(self) -> None:
        ids = child_runs.create(self.con, self.root, self.cfg, self.lead,
                                ["helper", "peer"], "check the fixtures",
                                shared_workdir=True)
        self.assertEqual(2, len(ids))
        rows = [self.con.execute("SELECT * FROM runs WHERE id=?", (i,)).fetchone()
                for i in ids]
        for row in rows:
            self.assertEqual(self.lead["id"], row["parent_run"])
            self.assertEqual(child_runs.REQUESTED_BY, row["requested_by"])
            self.assertEqual(1, row["child_depth"])
            self.assertEqual("spawning", row["status"])
            self.assertTrue(Path(row["brief_path"]).is_file())
        self.assertEqual(["helper", "peer"], [r["profile"] for r in rows])

    def test_the_brief_says_it_is_help_not_a_second_lead(self) -> None:
        ids = child_runs.create(self.con, self.root, self.cfg, self.lead,
                                ["helper"], "read the fixtures",
                                shared_workdir=True)
        text = Path(self.con.execute(
            "SELECT brief_path FROM runs WHERE id=?",
            (ids[0],)).fetchone()["brief_path"]).read_text()
        self.assertIn("You are a child run", text)
        self.assertIn("Do not merge, land, or report to Work", text)
        self.assertIn(f"run {self.lead['id']} decides", text)

    def test_the_lifetime_and_active_caps_are_separate(self) -> None:
        """Two different questions: how many may run AT ONCE, and how many
        this lead may ever have. Settling frees the first, never the second."""
        cfg = {**self.cfg, "settings": {"child_max_per_run": 3,
                                        "child_max_active": 2}}
        child_runs.create(self.con, self.root, cfg, self.lead,
                          ["helper", "peer"], "one", shared_workdir=True)
        with self.assertRaises(SystemExit) as caught:
            child_runs.create(self.con, self.root, cfg, self.lead,
                              ["helper"], "two", shared_workdir=True)
        self.assertIn("running at once", str(caught.exception))

        self.con.execute("UPDATE runs SET status='done' WHERE parent_run=?",
                         (self.lead["id"],))
        self.con.commit()
        child_runs.create(self.con, self.root, cfg, self.lead,
                          ["helper"], "three", shared_workdir=True)
        with self.assertRaises(SystemExit) as caught:
            child_runs.create(self.con, self.root, cfg, self.lead,
                              ["peer"], "four", shared_workdir=True)
        self.assertIn("in total", str(caught.exception))


class BrokerTests(ChildRunCase):
    def test_a_worker_only_writes_a_request(self) -> None:
        """The enqueue path creates NO run and starts NO process. That is the
        whole reason the broker exists."""
        request_id = child_runs.enqueue(self.con, self.lead, ["helper"], "help")
        row = self.con.execute("SELECT * FROM spawn_requests WHERE id=?",
                               (request_id,)).fetchone()
        self.assertEqual(("pending", self.lead["id"]),
                         (row["status"], row["lead_run"]))
        self.assertEqual(0, self.con.execute(
            "SELECT COUNT(*) n FROM runs WHERE parent_run=?",
            (self.lead["id"],)).fetchone()["n"])

    def test_the_supervisor_claims_the_request_and_launches(self) -> None:
        child_runs.enqueue(self.con, self.lead, ["helper"], "help",
                           shared_workdir=True)
        launched = []
        results = child_runs.process_pending(
            self.con, self.root, self.cfg, self.lead["id"],
            lambda root, rid: launched.append(rid))
        self.assertEqual(["accepted"], [r["status"] for r in results])
        self.assertEqual(launched, results[0]["child_run_ids"])
        row = self.con.execute("SELECT * FROM spawn_requests").fetchone()
        self.assertEqual("accepted", row["status"])
        self.assertEqual(launched, json.loads(row["child_run_ids_json"]))

    def test_a_refused_request_is_recorded_and_the_lead_lives(self) -> None:
        """A bad ask is the requester's problem, not the lead's death."""
        child_runs.enqueue(self.con, self.lead, ["stronger", "nope"], "help")
        results = child_runs.process_pending(
            self.con, self.root, self.cfg, self.lead["id"],
            lambda root, rid: self.fail("nothing should launch"))
        self.assertEqual(["failed"], [r["status"] for r in results])
        row = self.con.execute("SELECT * FROM spawn_requests").fetchone()
        self.assertIn("not enabled", row["error"] or "")
        self.assertEqual("running", self.con.execute(
            "SELECT status FROM runs WHERE id=?",
            (self.lead["id"],)).fetchone()["status"])

    def test_a_claimed_request_is_not_claimed_twice(self) -> None:
        child_runs.enqueue(self.con, self.lead, ["helper"], "help",
                           shared_workdir=True)
        first = child_runs.process_pending(self.con, self.root, self.cfg,
                                           self.lead["id"], lambda r, i: None)
        again = child_runs.process_pending(self.con, self.root, self.cfg,
                                           self.lead["id"], lambda r, i: None)
        self.assertEqual(1, len(first))
        self.assertEqual([], again)

    def test_a_lead_that_ends_answers_its_unclaimed_requests(self) -> None:
        child_runs.enqueue(self.con, self.lead, ["helper"], "help")
        child_runs.fail_unprocessed(self.con, self.lead["id"], "lead ended done")
        row = self.con.execute("SELECT * FROM spawn_requests").fetchone()
        self.assertEqual("failed", row["status"])
        self.assertIn("lead ended done", row["error"])


class WakeupTests(ChildRunCase):
    def _batch(self, status="done"):
        child_runs.enqueue(self.con, self.lead, ["helper"], "help",
                           shared_workdir=True)
        results = child_runs.process_pending(self.con, self.root, self.cfg,
                                             self.lead["id"], lambda r, i: None)
        child_id = results[0]["child_run_ids"][0]
        self.con.execute("UPDATE runs SET status=?, summary=? WHERE id=?",
                         (status, "read the fixtures; found two", child_id))
        self.con.commit()
        return child_id

    def test_a_running_lead_is_told_once_by_message(self) -> None:
        child_id = self._batch()
        self.assertIsNone(child_runs.maybe_wake_lead(self.con, self.root, child_id))
        rows = list(self.con.execute(
            "SELECT * FROM messages WHERE run_id=? AND kind='interrupt'",
            (self.lead["id"],)))
        self.assertEqual(1, len(rows))
        self.assertIn("Every child run you asked for has settled", rows[0]["body"])
        self.assertIn("found two", rows[0]["body"], "the child's own words")
        # A second settling must not tell it again.
        child_runs.maybe_wake_lead(self.con, self.root, child_id)
        self.assertEqual(1, self.con.execute(
            "SELECT COUNT(*) n FROM messages WHERE run_id=? AND kind='interrupt'",
            (self.lead["id"],)).fetchone()["n"])
        self.assertTrue(self.con.execute(
            "SELECT notified_at FROM spawn_requests").fetchone()["notified_at"])

    def test_an_unsettled_batch_wakes_nobody(self) -> None:
        child_id = self._batch(status="running")
        self.assertIsNone(child_runs.maybe_wake_lead(self.con, self.root, child_id))
        self.assertEqual(0, self.con.execute(
            "SELECT COUNT(*) n FROM messages WHERE run_id=?",
            (self.lead["id"],)).fetchone()["n"])


if __name__ == "__main__":
    unittest.main()


class BriefTests(ChildRunCase):
    """A brief never teaches a verb it cannot use — the reason delegation
    stayed invisible before: the card said nothing, so no worker asked."""

    def test_a_lead_with_somewhere_weaker_is_told_it_may_ask(self) -> None:
        from orchestra import brief
        line = brief.help_protocol(self.cfg, "lead")
        self.assertIn("orchestra spawn --to", line)
        self.assertIn("helper", line)
        self.assertNotIn("peer", line, "a sideways profile is not help")

    def test_the_weakest_profile_is_told_nothing(self) -> None:
        from orchestra import brief
        self.assertEqual("", brief.help_protocol(self.cfg, "helper"))

    def test_an_untiered_profile_is_told_nothing(self) -> None:
        from orchestra import brief
        cfg = {**self.cfg, "profiles": {**self.cfg["profiles"],
                                        "lead": {"backend": "opencode"}}}
        self.assertEqual("", brief.help_protocol(cfg, "lead"))

    def test_a_child_is_never_told_it_may_spawn(self) -> None:
        ids = child_runs.create(self.con, self.root, self.cfg, self.lead,
                                ["helper"], "read the fixtures",
                                shared_workdir=True)
        text = Path(self.con.execute(
            "SELECT brief_path FROM runs WHERE id=?",
            (ids[0],)).fetchone()["brief_path"]).read_text()
        self.assertNotIn("orchestra spawn", text)
