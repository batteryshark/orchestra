"""The supervisor's landing seam (W-0174, DESIGN §9).

``merge.merge_run`` is covered by tests/test_merge.py; this file covers what
happens around it when a run finishes: the Work thread comment, the
review/blocked transition, the Nod decision card a merge escalation is, the
pause switch, and the rule that a merge failure never breaks finalization.

Throwaway git repositories, tests/fake_work.py and tests/fake_nod.py only —
no live Work server, no live Nod server, no real credentials.
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dromond import config, db, dispatch, merge, project
from tests.fake_nod import DECISIONS_CHANNEL, DECISIONS_TOKEN, FakeNod
from tests.fake_work import FakeWork

PROJECT_ID = "53efe3c3-6def-4797-8560-3dce073d7d63"
BRANCH = "dromond/run-1"

CONFIG = """\
[settings]
timeout = 60

[profiles.stub]
backend = "opencode"

[nod]
enabled = true
# Never the default path: that one is the developer's real Nod credentials.
secrets_file = "{secrets}"

[work]
enabled = true
agent_identity = "dromond"
profile = "stub"
"""


def git(root: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


class LandingTestCase(unittest.TestCase):
    """A git project, a run row on a branch, a fake Work item, a fake Nod."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name).resolve()
        self.workspace = self.tmp_path / "workspace"
        self.root = self.workspace / "demo"
        self.root.mkdir(parents=True)
        git(self.root, "init", "--quiet")
        git(self.root, "symbolic-ref", "HEAD", "refs/heads/main")
        git(self.root, "config", "user.email", "test@example.com")
        git(self.root, "config", "user.name", "test")
        git(self.root, "config", "commit.gpgsign", "false")
        (self.root / "app.py").write_text("print('hello')\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "--quiet", "-m", "initial")

        self.nod = FakeNod()
        self.nod_url = self.nod.start()
        self.addCleanup(self.nod.stop)
        self.work = FakeWork(workspace_root=self.workspace)
        self.work.add_project("demo", PROJECT_ID, path="demo", name="Demo")
        self.work.add_task("W-0001", "demo item", status="in_progress",
                           delegated=True)
        self.work_url = self.work.start()
        self.addCleanup(self.work.stop)

        self.global_config = self.tmp_path / "global.toml"
        self.global_config.write_text(
            CONFIG.format(secrets=self.tmp_path / "nod-secrets.env")
            + f'api_url = "{self.work_url}"\n')
        self.env = mock.patch.dict(os.environ, {
            "DROMOND_CONFIG": str(self.global_config),
            "DROMOND_HOME": str(self.tmp_path / "home"),
            "DROMOND_NOD_BASE_URL": self.nod_url,
            "DROMOND_NOD_DECISIONS_CHANNEL": DECISIONS_CHANNEL,
            "DROMOND_NOD_DECISIONS_TOKEN": DECISIONS_TOKEN,
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.con = db.connect()
        self.addCleanup(self.con.close)
        project.remember(self.con, str(self.workspace),
                         [{"projectId": PROJECT_ID, "id": "demo", "name": "Demo",
                           "path": "demo"}])
        self.cfg = config.load(PROJECT_ID)

    # --- helpers -------------------------------------------------------------

    def run_branch(self, changes: dict[str, str], branch: str = BRANCH) -> None:
        git(self.root, "checkout", "--quiet", "-b", branch)
        for name, text in changes.items():
            (self.root / name).write_text(text)
        git(self.root, "add", "-A")
        git(self.root, "commit", "--quiet", "-m", "run work")
        git(self.root, "checkout", "--quiet", "main")

    def commit_on_main(self, name: str, text: str) -> None:
        (self.root / name).write_text(text)
        git(self.root, "add", "-A")
        git(self.root, "commit", "--quiet", "-m", "human work")

    def add_run(self, *, work_item: str | None = "W-0001",
                branch: str | None = BRANCH) -> dict:
        self.con.execute(
            "INSERT INTO runs(id, slug, profile, backend, requested_by, workdir, "
            "project_id, work_item, branch, status, started_at) "
            "VALUES(1,'brave_otter','stub','opencode','work',?,?,?,?,'running',?)",
            (str(self.root), PROJECT_ID, work_item, branch, db.now()))
        self.con.commit()
        return dict(self.con.execute("SELECT * FROM runs WHERE id=1").fetchone())

    def thread(self) -> str:
        rows = self.con.execute(
            "SELECT body FROM messages WHERE run_id=1 AND kind='merge'").fetchall()
        return "\n".join(r["body"] for r in rows)

    def item_log(self) -> str:
        return "\n".join(e["message"] for e in self.work.tasks["W-0001"]["log"])

    def branches(self) -> str:
        return git(self.root, "branch", "--list")

    # --- a successful run lands ---------------------------------------------

    def test_verified_run_lands_and_its_item_reaches_review(self) -> None:
        self.run_branch({"feature.py": "x = 1\n"})
        run = self.add_run()

        note = merge.at_completion(self.con, self.cfg, run, "done")

        head = git(self.root, "rev-parse", "main")
        self.assertIn(f"Merged {BRANCH} into main", note)
        self.assertIn(head[:12], note)
        self.assertIn("feature.py",
                      git(self.root, "ls-tree", "--name-only", "main"))
        self.assertNotIn(BRANCH, self.branches())
        # The item carries the commit, the files, the checks and the revert.
        self.assertEqual("review", self.work.tasks["W-0001"]["status"])
        log = self.item_log()
        self.assertIn(head, log)
        self.assertIn("feature.py", log)
        self.assertIn("checks: none declared", log)
        self.assertIn(f"revert -m 1 {head}", log)
        # and so does the run's own thread
        self.assertIn(f"revert -m 1 {head}", self.thread())
        self.assertEqual({}, self.nod.requests)  # nothing buzzed the phone

    def test_declared_checks_run_before_the_branch_lands(self) -> None:
        self.global_config.write_text(
            self.global_config.read_text()
            + '\n[merge.checks]\ntest = "test -f feature.py"\n')
        self.cfg = config.load(PROJECT_ID)
        self.run_branch({"feature.py": "x = 1\n"})

        merge.at_completion(self.con, self.cfg, self.add_run(), "done")

        self.assertIn("checks: test ok", self.item_log())
        self.assertEqual("review", self.work.tasks["W-0001"]["status"])

    # --- an escalation stops, reports, and reaches the phone -----------------

    def test_conflicting_run_keeps_its_branch_and_reaches_blocked(self) -> None:
        self.run_branch({"app.py": "print('branch')\n"})
        self.commit_on_main("app.py", "print('main')\n")
        run = self.add_run()

        note = merge.at_completion(self.con, self.cfg, run, "done")

        self.assertIn("Merge escalated at rebase", note)
        self.assertIn(BRANCH, self.branches())          # branch left intact
        self.assertEqual("print('main')\n", (self.root / "app.py").read_text())
        self.assertEqual("blocked", self.work.tasks["W-0001"]["status"])
        log = self.item_log()
        self.assertIn("conflicted files: `app.py`", log)
        self.assertIn(f"dromond merge {BRANCH}", log)
        # A needs-you event: one decision card, offering the three §9 options.
        card = self.nod.requests["req_1"]
        self.assertEqual(DECISIONS_CHANNEL, card["channel_id"])
        self.assertEqual(["retry", "resolver", "leave"],
                         [o["id"] for o in card["options"]])
        self.assertIn("app.py", card["body_markdown"])
        # and the request id is recorded, so the answer can be mirrored later
        row = self.con.execute(
            "SELECT * FROM nod_requests WHERE request_id='req_1'").fetchone()
        self.assertEqual(("merge_conflict", 1, "W-0001"),
                         (row["kind"], row["run_id"], row["work_item"]))
        self.assertIn("req_1", self.item_log())
        # The sweeper must not later move this blocked item to review.
        self.assertIsNotNone(
            self.con.execute("SELECT work_reported_at FROM runs "
                             "WHERE id=1").fetchone()["work_reported_at"])

    def test_failing_check_escalates_and_never_touches_the_base(self) -> None:
        self.global_config.write_text(
            self.global_config.read_text()
            + '\n[merge.checks]\ntest = "exit 3"\n')
        self.cfg = config.load(PROJECT_ID)
        self.run_branch({"feature.py": "x = 1\n"})
        before = git(self.root, "rev-parse", "main")

        note = merge.at_completion(self.con, self.cfg, self.add_run(), "done")

        self.assertIn("Merge escalated at checks", note)
        self.assertEqual(before, git(self.root, "rev-parse", "main"))
        self.assertIn(BRANCH, self.branches())
        self.assertEqual("blocked", self.work.tasks["W-0001"]["status"])
        self.assertIn("test FAILED (exit 3)", self.item_log())
        self.assertEqual(1, len(self.nod.requests))

    # --- the pause switch -----------------------------------------------------

    def test_a_paused_daemon_lands_nothing(self) -> None:
        self.run_branch({"feature.py": "x = 1\n"})
        before = git(self.root, "rev-parse", "main")
        dispatch.pause(self.con, note="hands off")

        note = merge.at_completion(self.con, self.cfg, self.add_run(), "done")

        self.assertIn("dispatch is paused", note)
        self.assertEqual(before, git(self.root, "rev-parse", "main"))
        self.assertIn(BRANCH, self.branches())
        self.assertEqual("in_progress", self.work.tasks["W-0001"]["status"])
        self.assertEqual({}, self.nod.requests)
        self.assertIn("dispatch is paused", self.thread())

    # --- what never merges ----------------------------------------------------

    def test_an_unsuccessful_run_never_lands(self) -> None:
        self.run_branch({"feature.py": "x = 1\n"})
        before = git(self.root, "rev-parse", "main")
        for status in ("failed", "timeout", "killed"):
            self.assertIsNone(
                merge.at_completion(self.con, self.cfg, self.add_run(), status))
            self.con.execute("DELETE FROM runs WHERE id=1")
            self.con.commit()
        self.assertEqual(before, git(self.root, "rev-parse", "main"))

    def test_a_shared_tree_run_has_no_branch_to_land(self) -> None:
        run = self.add_run(branch=None)
        self.assertIsNone(merge.at_completion(self.con, self.cfg, run, "done"))
        self.assertEqual(0, self.work.mutation_count())

    # --- no Work item: report to the run instead ------------------------------

    def test_a_hand_dispatched_run_lands_and_reports_to_its_own_thread(self) -> None:
        self.run_branch({"feature.py": "x = 1\n"})
        run = self.add_run(work_item=None)

        note = merge.at_completion(self.con, self.cfg, run, "done")

        head = git(self.root, "rev-parse", "main")
        self.assertIn(f"Merged {BRANCH} into main", note)
        report = self.thread()
        self.assertIn(head, report)
        self.assertIn(f"revert -m 1 {head}", report)
        self.assertEqual(0, self.work.mutation_count())  # nothing posted anywhere

    # --- finalization is never broken ----------------------------------------

    def test_a_merge_exception_is_recorded_not_raised(self) -> None:
        self.run_branch({"feature.py": "x = 1\n"})
        run = self.add_run()
        with mock.patch.object(merge, "merge_run",
                               side_effect=OSError("the disk went away")):
            note = merge.at_completion(self.con, self.cfg, run, "done")
        self.assertEqual("Merge failed: the disk went away", note)
        self.assertIn("the disk went away", self.thread())
        self.assertEqual("in_progress", self.work.tasks["W-0001"]["status"])

    def test_git_refusing_the_ref_update_escalates_like_a_conflict(self) -> None:
        """The compare-and-swap is meant to fail loudly when the base moved.
        Loudly means blocked plus a card, not a note under a reviewed item."""
        self.run_branch({"feature.py": "x = 1\n"})
        run = self.add_run()
        with mock.patch.object(
                merge, "merge_run",
                side_effect=RuntimeError("git update-ref failed: stale ref")):
            note = merge.at_completion(self.con, self.cfg, run, "done")
        self.assertIn("Merge escalated at merge", note)
        self.assertIn(BRANCH, self.branches())
        self.assertEqual("blocked", self.work.tasks["W-0001"]["status"])
        self.assertIn("stale ref", self.nod.requests["req_1"]["body_markdown"])

    def test_work_being_down_does_not_stop_the_merge(self) -> None:
        self.run_branch({"feature.py": "x = 1\n"})
        self.work.stop()

        note = merge.at_completion(self.con, self.cfg, self.add_run(), "done")

        self.assertIn(f"Merged {BRANCH} into main", note)
        self.assertIn("feature.py",
                      git(self.root, "ls-tree", "--name-only", "main"))


if __name__ == "__main__":
    unittest.main()
