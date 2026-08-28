"""W-0309: the refine lane — a tag asks for shaping, not execution.

The human tags an item `refine`; one sweep later a refinement run is in
flight against the goal standard, whatever the item's status and whether or
not it is delegated. The lane claims nothing, ticks nothing, and lands
nothing: a refine run that finishes leaves the board column exactly where the
human left it. The tag is the receipt — the run drops it, and a tag still
present when nothing is running dispatches the pass again.
"""
import json
import unittest
import urllib.error
import urllib.request

from orchestra import brief, db
from tests.test_sweeper import SweeperFixture


class RefineLaneTests(SweeperFixture, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        # The file ends inside [work], so the bare key lands there.
        self.add_config('refine_profile = "stub"\n')
        self.reload()

    def reload(self) -> None:
        from orchestra import config
        self.cfg = config.load()

    def tag_item(self, item="W-0001", **kw):
        """A thin item exactly as the owner files one: backlog, not
        delegated, a title and a riff — plus the tag."""
        kw.setdefault("status", "backlog")
        return self.work.add_task(item, "a riffed thing", tags=("refine",),
                                  **kw)

    def refine_runs(self):
        con = db.connect()
        rows = list(con.execute(
            "SELECT * FROM runs WHERE requested_by='refine' ORDER BY id"))
        con.close()
        return rows

    def messages(self, item="W-0001"):
        return [e["message"] for e in self.work.tasks[item]["log"]]

    def patch(self, item, body, agent="orchestra/calm_otter"):
        """What the RUN does: a PATCH carrying an agent identity."""
        request = urllib.request.Request(
            f"{self.client.api_url}/api/tasks/{item}",
            data=json.dumps(body).encode(), method="PATCH",
            headers={"X-Work-Agent": agent,
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read())

    # --- dispatch -----------------------------------------------------------

    def test_a_tagged_item_is_refined_whatever_its_status(self) -> None:
        """Not ready, not delegated, not in progress — the tag is the whole
        request, because refinement happens before execution."""
        for index, status in enumerate(("backlog", "ready", "in_progress",
                                        "review", "blocked")):
            item = f"W-{index:04d}"
            self.tag_item(item, status=status)
            actions = self.sweep()
            self.assertIn({"action": "refine", "item": item,
                           "run": self.refine_runs()[-1]["id"]}, actions)
        self.assertEqual(len(self.refine_runs()), 5)

    def test_delegation_is_not_required_and_not_consumed(self) -> None:
        """The worker lane's signal is untouched: a refined item is neither
        claimed nor delegated by this pass."""
        self.tag_item()
        self.sweep()
        task = self.work.tasks["W-0001"]
        self.assertFalse(task["delegated"])
        self.assertEqual(task["status"], "backlog")
        self.assertFalse([m for m in self.messages() if "fact:" in m])

    def test_the_run_is_staffed_from_the_configured_profile(self) -> None:
        self.tag_item()
        self.sweep()
        run = self.refine_runs()[0]
        self.assertEqual(run["profile"], "stub")
        self.assertEqual(run["requested_by"], "refine")
        self.assertEqual(run["work_item"], "W-0001")
        self.assertIsNone(run["branch"])  # shaping edits Work, not the repo
        self.assertEqual(self.launched, [(self.root, run["id"])])

    def test_an_unset_profile_falls_back_to_the_one_tier1_profile(self) -> None:
        """The same volunteer rule the sign-off pass uses."""
        self.global_config.write_text(
            self.global_config.read_text()
            .replace('refine_profile = "stub"\n', "")
            .replace('[profiles.stub]\nbackend = "opencode"\n',
                     '[profiles.stub]\nbackend = "opencode"\n\n'
                     '[profiles.judge]\nbackend = "opencode"\ntier = 1\n'))
        self.reload()
        self.tag_item()
        self.sweep()
        self.assertEqual(self.refine_runs()[0]["profile"], "judge")

    def test_no_profile_at_all_skips_and_says_so(self) -> None:
        """Nothing to staff: the pass skips, and the tag stays for the human
        to see. It never guesses a profile and never crashes."""
        self.global_config.write_text(self.global_config.read_text()
                                      .replace('refine_profile = "stub"\n', ""))
        self.reload()
        self.tag_item()
        self.sweep()
        self.assertEqual(self.refine_runs(), [])
        self.assertIn("refine", self.work.tasks["W-0001"]["tags"])

    def test_one_live_run_per_item_however_many_passes(self) -> None:
        self.tag_item()
        self.sweep()
        self.sweep()
        self.sweep()
        self.assertEqual(len(self.refine_runs()), 1)

    def test_a_finished_pass_that_left_the_tag_dispatches_again(self) -> None:
        """The tag is the truth: the run failed to finish, or the human
        tagged it again. Either way the request is still on the item."""
        self.tag_item()
        self.sweep()
        self.finish_run(self.refine_runs()[0]["id"], "done", "Refined.")
        self.sweep()
        self.assertEqual(len(self.refine_runs()), 2)

    def test_dropping_the_tag_ends_the_lane(self) -> None:
        self.tag_item()
        self.sweep()
        run = self.refine_runs()[0]
        self.finish_run(run["id"], "done", "Refined.")
        self.work.tasks["W-0001"]["tags"] = []
        self.work.tasks["W-0001"]["updatedAt"] = self.work.now()
        self.sweep()
        self.assertEqual(len(self.refine_runs()), 1)

    # --- the lane is neither the worker lane nor the verify lane ------------

    def test_a_finished_refine_run_lands_nothing_and_declines_nothing(self) -> None:
        """The worker report path would decline every open criterion and
        append `fact: landed`. A refinement has nothing to land."""
        self.work.add_task("W-0001", "a riffed thing", status="backlog",
                           tags=("refine",), acceptance=("it works",))
        self.sweep()
        run = self.refine_runs()[0]
        self.finish_run(run["id"], "done", "Refined.")
        actions = self.sweep()
        self.assertNotIn("report", [a.get("action") for a in actions
                                    if isinstance(a, dict)])
        task = self.work.tasks["W-0001"]
        self.assertEqual(task["status"], "backlog")
        self.assertFalse([m for m in self.messages() if "fact: landed" in m])
        self.assertFalse(task["acceptanceCriteria"][0]["declined"])
        self.assertFalse(task["acceptanceCriteria"][0]["checked"])
        con = db.connect()
        verify_runs = con.execute(
            "SELECT 1 FROM runs WHERE requested_by='verify'").fetchall()
        con.close()
        self.assertEqual(verify_runs, [])

    def test_a_refine_run_never_blocks_the_report_queue(self) -> None:
        """Skipping the report path still settles the row, or every later
        pass would look at it again."""
        self.tag_item()
        self.sweep()
        run = self.refine_runs()[0]
        self.finish_run(run["id"], "done", "Refined.")
        self.sweep()
        self.assertIsNotNone(self.db_run(run["id"])["work_reported_at"])

    def test_the_worker_lane_still_needs_delegation(self) -> None:
        """The carve-out is scoped to this lane: a tagged item that nobody
        delegated gets no worker run."""
        self.tag_item()
        self.sweep()
        con = db.connect()
        workers = con.execute(
            "SELECT 1 FROM runs WHERE requested_by='work'").fetchall()
        con.close()
        self.assertEqual(workers, [])

    # --- the brief ----------------------------------------------------------

    def test_the_brief_carries_the_standard_and_the_refine_rules(self) -> None:
        self.tag_item()
        self.sweep()
        text = (self.tmp_path / "home" / "briefs" /
                f"run-{self.refine_runs()[0]['id']}.md").read_text()
        slug = self.refine_runs()[0]["slug"]
        self.assertIn(str(brief.GOAL_STANDARD), text)
        self.assertIn("W-0001", text)
        self.assertIn("VERBATIM", text)
        self.assertIn("`Q:` line", text)
        self.assertIn(f"[orchestra/{slug}] fact: refined", text)
        self.assertIn("minus\n   `refine`", text)
        self.assertIn(self.client.api_url, text)
        # The house style governs the summary comment; it is read from the
        # doc, never copied into the template.
        self.assertIn("## Writeback", text)
        # A refinement answers no checklist — that protocol must not reach it.
        self.assertNotIn("Before you stop, account for every requirement", text)
        self.assertNotIn("work check", text)

    def test_the_vendored_standard_is_there_and_named_by_path(self) -> None:
        self.assertTrue(brief.GOAL_STANDARD.is_file())
        self.assertIn("The four properties",
                      brief.GOAL_STANDARD.read_text(encoding="utf-8"))
        self.assertNotIn("The four properties",
                         brief.REFINE_MISSION)  # referenced, never copied

    # --- Work's tag-scoped allowance (mirrored in FakeWork) -----------------

    def test_an_agent_section_edit_without_the_tag_is_refused(self) -> None:
        self.work.add_task("W-0001", "a riffed thing", status="backlog")
        status, body = self.patch("W-0001", {"goal": "rewritten"})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "agent_task_edit_forbidden")

    def test_the_tag_buys_the_sections_and_one_tag_drop(self) -> None:
        self.tag_item()
        status, _ = self.patch("W-0001", {
            "goal": "One bounded outcome, so that shaping stops being manual.",
            "notes": "Q: what is the appetite?",
            "acceptanceCriteria": ["the tag is gone — work show W-0001"]})
        self.assertEqual(status, 200)
        task = self.work.tasks["W-0001"]
        self.assertIn("so that shaping", task["sections"]["goal"])
        self.assertEqual(task["acceptanceCriteria"][0]["text"],
                         "the tag is gone — work show W-0001")
        status, _ = self.patch("W-0001", {"tags": []})
        self.assertEqual(status, 200)
        self.assertEqual(task["tags"], [])
        # The allowance dies with the tag.
        status, body = self.patch("W-0001", {"goal": "again"})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "agent_task_edit_forbidden")

    def test_a_tags_update_may_only_drop_refine(self) -> None:
        self.work.add_task("W-0001", "a riffed thing", status="backlog",
                           tags=("refine", "ios"))
        self.assertEqual(self.patch("W-0001", {"tags": []})[0], 403)
        self.assertEqual(self.patch("W-0001", {"tags": ["ios", "new"]})[0], 403)
        self.assertEqual(self.patch("W-0001", {"tags": ["ios"]})[0], 200)

    def test_the_forbidden_fields_stay_forbidden(self) -> None:
        self.tag_item()
        for body in ({"title": "renamed"}, {"delegated": True},
                     {"status": "ready"}, {"parentId": "W-0002"}):
            self.assertEqual(self.patch("W-0001", body)[0], 403, body)

    def test_a_finished_refinement_leaves_nothing_for_the_next_pass(self) -> None:
        """End to end against FakeWork: the run rewrites the sections, posts
        its fact, drops the tag — and the lane goes quiet."""
        self.tag_item()
        self.sweep()
        run = self.refine_runs()[0]
        tag = f"[orchestra/{run['slug']}]"
        self.patch("W-0001", {"description": "the owner's riff, kept whole.",
                              "notes": "Q: what is the appetite?"})
        self.client.log_task("W-0001", f"{tag} refined W-0001. 1 open Q:")
        self.client.log_task("W-0001", f"{tag} fact: refined")
        self.patch("W-0001", {"tags": []})
        self.finish_run(run["id"], "done", "Refined.")
        self.sweep()
        self.assertEqual(len(self.refine_runs()), 1)
        task = self.work.tasks["W-0001"]
        self.assertEqual(task["tags"], [])
        self.assertEqual(task["status"], "backlog")  # `refined` moves nothing
        self.assertIn("the owner's riff, kept whole.",
                      task["sections"]["description"])

    def test_a_parked_project_gets_no_refinement_run(self) -> None:
        """DESIGN §1: shaping is unattended too, so a parked project gets
        none."""
        self.tag_item()
        self.archive_project()
        self.sweep()
        self.assertEqual([], self.refine_runs())
        self.assertIn("refine", self.work.tasks["W-0001"]["tags"],
                      "the tag is the human's request and stays put")
        # Unparking revives it: the tag is still there, and the next pass
        # that reads the item at all dispatches the shaping run. The sweep
        # cursor is incremental, so that is the item's next edit or the next
        # full board read — this lane holds no waiting row of its own.
        self.archive_project(archived=False)
        self.work.tasks["W-0001"]["updatedAt"] = self.work.now()
        self.sweep()
        self.assertEqual(1, len(self.refine_runs()))


if __name__ == "__main__":
    unittest.main()
