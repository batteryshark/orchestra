"""Merge behaviors against throwaway git repositories (never a real one)."""
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from unittest import mock
from pathlib import Path

from orchestra import db, dispatch, merge

BRANCH = "orchestra/run-1"


def git(root: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


class MergeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        git(self.root, "init", "--quiet")
        git(self.root, "symbolic-ref", "HEAD", "refs/heads/main")
        git(self.root, "config", "user.email", "test@example.com")
        git(self.root, "config", "user.name", "test")
        git(self.root, "config", "commit.gpgsign", "false")
        self.settings = {}
        self.write("notes.txt", "base\n")
        self.write("app.py", "print('hello')\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "--quiet", "-m", "initial")

    def write(self, name: str, text: str) -> None:
        (self.root / name).write_text(text)

    def config(self, text: str) -> None:
        """Per-project settings live in the central config now (DESIGN §2), so
        the table is passed to merge_run instead of written beside the repo."""
        self.settings = tomllib.loads(text)

    def run_branch(self, changes: dict[str, str | None], branch: str = BRANCH) -> None:
        """Create a run branch off main carrying ``changes`` (None deletes)."""
        git(self.root, "checkout", "--quiet", "-b", branch)
        for name, text in changes.items():
            if text is None:
                git(self.root, "rm", "--quiet", name)
            else:
                self.write(name, text)
        git(self.root, "add", "-A")
        git(self.root, "commit", "--quiet", "-m", "run work")
        git(self.root, "checkout", "--quiet", "main")

    # --- cases --------------------------------------------------------------

    def test_a_run_cannot_land_a_file_the_base_branch_stopped_tracking(self):
        """The drop rule, in the real shape of the recurring escalation:
        `.work/` held a live service's database, git tracked it, and every run
        committed whatever snapshot happened to be on disk. Two runs branched
        while it was still tracked — one edited the snapshot alongside real
        work (as an agent with a file editor will when the file is sitting in
        its worktree), one touched ONLY the snapshot. Then the base untracked
        it while the service kept writing the real copy. Both runs still land; only
        their stale snapshots are dropped."""
        (self.root / ".work").mkdir()
        self.write(".work/W-0171.md", "status: backlog\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "--quiet", "-m", "records, tracked by mistake")
        self.run_branch({".work/W-0171.md": "status: done\n",
                         "docs.md": "the actual work\n"})
        self.run_branch({".work/W-0171.md": "status: done\n"},
                        branch="orchestra/run-2")
        # Meanwhile the base untracked it and the service kept writing the real copy.
        git(self.root, "rm", "-r", "--quiet", "--cached", ".work")
        self.write(".gitignore", ".work/\n")
        git(self.root, "add", ".gitignore")
        git(self.root, "commit", "--quiet", "-m", "stop tracking .work")
        (self.root / ".work" / "W-0171.md").write_text("status: in_progress\n")

        with self.subTest("real work lands without the snapshot"):
            result = merge.merge_run(self.root, BRANCH, settings=self.settings)
            # Before this rule the rebase conflicted here, every time, forever.
            self.assertTrue(result["ok"], result)
            self.assertEqual([".work/W-0171.md"], result["dropped"])
            self.assertEqual(["docs.md"], result["files_changed"])

        with self.subTest("a run that only touched untracked state still lands"):
            result = merge.merge_run(self.root, "orchestra/run-2",
                                     settings=self.settings)
            self.assertTrue(result["ok"], result)
            self.assertEqual([".work/W-0171.md"], result["dropped"])
            self.assertEqual([], result["files_changed"])

        # The service's own copy is untouched on disk; only the runs' stale
        # snapshots were dropped, and none reached the tree.
        self.assertEqual("status: in_progress\n",
                         (self.root / ".work" / "W-0171.md").read_text())
        self.assertNotIn(".work", git(self.root, "ls-tree", "-r", "--name-only", "main"))

    def test_a_legacy_branch_with_agent_commits_still_lands(self):
        """W-0259: history exists. Merge takes the agent's own commits too."""
        git(self.root, "checkout", "--quiet", "-b", BRANCH)
        self.write("one.py", "a = 1\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "--quiet", "-m", "agent: first")
        self.write("two.py", "b = 2\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "--quiet", "-m", "agent: second")
        git(self.root, "checkout", "--quiet", "main")

        result = merge.merge_run(self.root, BRANCH, settings=self.settings)

        self.assertTrue(result["ok"], result)
        self.assertEqual(["one.py", "two.py"], result["files_changed"])

    def test_a_real_source_conflict_still_reaches_the_human(self):
        """The drop rule drops what the base says is not source. It must not
        soften a genuine conflict in code, which is a judgment nobody can
        automate — and the conflicting rebase aborts cleanly: base unmoved,
        branch kept, no scratch worktree, no half-rebased state left."""
        self.run_branch({"app.py": "print('from the run')\n"})
        self.write("app.py", "print('from the owner')\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "--quiet", "-m", "owner edit")
        before = git(self.root, "rev-parse", "main")

        result = merge.merge_run(self.root, BRANCH, settings=self.settings)

        self.assertFalse(result["ok"])
        self.assertEqual("rebase", result["stage"])
        self.assertEqual(["app.py"], result["conflicts"])
        self.assertEqual([], result["dropped"])
        self.assertIsNone(result["commit"])
        self.assertEqual(before, git(self.root, "rev-parse", "main"))
        self.assertIn(BRANCH, git(self.root, "branch", "--list", BRANCH))
        self.assertEqual(1, len(git(self.root, "worktree", "list").splitlines()))
        self.assertEqual([], list(self.root.glob(".git/worktrees/*")))
        self.assertFalse((self.root / ".git" / "rebase-merge").exists())

    def test_a_dirty_base_checkout_that_the_merge_does_not_touch_still_lands(self):
        """THE recurrence (runs 60/61/62): a repo whose owner works in it is
        dirty nearly always, and refusing on that escalated every single run
        — including the resolver dispatched to clear the escalation. Edits
        the merge does not rewrite are none of its business.

        And I-0012: a run merge once published the owner's work in flight
        under the run's message (bb3eb6f, 2026-08-14). The merge is built in
        a scratch worktree, so the dirty file must appear in no commit it
        creates."""
        self.run_branch({"feature.py": "x = 1\n"})
        self.write("app.py", "print('half-finished, mine, not for main')\n")

        result = merge.merge_run(self.root, BRANCH, settings=self.settings)

        self.assertTrue(result["ok"], result)
        self.assertEqual(["app.py"], result["dirty"])
        self.assertEqual("refreshed", result["refresh"]["status"])
        merge_sha = git(self.root, "rev-parse", "HEAD")
        landed = git(self.root, "show", "--name-only", "--format=", merge_sha)
        self.assertNotIn("app.py", landed,
                         "the owner's uncommitted file rode into the merge")
        # And the content itself reached no commit anywhere in the repo —
        # the pickaxe is the assertion that would have caught bb3eb6f.
        self.assertEqual("", git(self.root, "log", "--all", "--oneline",
                                 "-S", "half-finished, mine, not for main"))
        # Their work is untouched: never committed, never stashed, never
        # reverted — still an unstaged local modification.
        self.assertIn("M app.py", git(self.root, "status", "--porcelain"))
        self.assertEqual("print('half-finished, mine, not for main')\n",
                         (self.root / "app.py").read_text())

    def test_an_edit_that_overlaps_the_merge_still_lands(self):
        # It used to escalate, and the card it filed could not be resolved by
        # either option it offered. The merge is safe anyway: it happens in a
        # scratch worktree and the ref moves by update-ref, so the owner keeps
        # both their edit and their pre-merge tree.
        self.run_branch({"app.py": "x = 1\n"})
        self.write("app.py", "print('owner is editing this')\n")
        before = git(self.root, "rev-parse", "main")

        result = merge.merge_run(self.root, BRANCH, settings=self.settings)

        self.assertTrue(result["ok"], result)
        self.assertEqual(["app.py"], result["overlap"])
        self.assertNotEqual(before, git(self.root, "rev-parse", "main"),
                            "the merge landed")
        # The owner's work in flight is never touched, committed, or stashed.
        self.assertEqual("print('owner is editing this')\n",
                         (self.root / "app.py").read_text())
        # Their checkout keeps the pre-merge tree, and the note says so with
        # the one command that fixes it.
        self.assertEqual("refused", result["refresh"]["status"])
        self.assertIn("read-tree", result["note"])
        self.assertIn("app.py", result["note"])

    def test_require_clean_gates_the_merge_but_never_the_owners_edit(self):
        """require_clean = true restores the old refusal — the escape hatch
        for anyone who wants the merge to wait on an overlapping edit. Turned
        off, the merge lands and the refresh becomes the last line of
        defence: it declines rather than overwriting the owner's edit, which
        is why turning require_clean off is safe rather than reckless."""
        self.run_branch({"app.py": "x = 1\n"})
        self.write("app.py", "print('owner is editing this')\n")
        before = git(self.root, "rev-parse", "main")

        with self.subTest("require_clean = true refuses"):
            self.config("[merge]\nrequire_clean = true\n")
            result = merge.merge_run(self.root, BRANCH, settings=self.settings)
            self.assertFalse(result["ok"])
            self.assertEqual("dirty", result["stage"])
            self.assertEqual(before, git(self.root, "rev-parse", "main"))
            self.assertIn(BRANCH, git(self.root, "branch", "--list", BRANCH))

        with self.subTest("turned off, the merge lands and the refresh refuses"):
            self.config("[merge]\nrequire_clean = false\n")
            result = merge.merge_run(self.root, BRANCH, settings=self.settings)
            self.assertTrue(result["ok"], result)
            # The owner's edit is still theirs: never committed, never reverted.
            self.assertEqual("print('owner is editing this')\n",
                             (self.root / "app.py").read_text())
            self.assertIn("app.py", git(self.root, "status", "--porcelain"))
            self.assertEqual("refused", result["refresh"]["status"])
            self.assertIn("read-tree -m -u", result["refresh"]["command"])
            self.assertIn("read-tree -m -u", result["note"])
            self.assertIn("overwritten", result["refresh"]["why"])

    def test_an_untracked_file_is_not_dirty_enough_to_refuse(self):
        # A build directory or a scratch note is not work in flight, and
        # refusing on one would make the guard useless within a day.
        self.run_branch({"feature.py": "x = 1\n"})
        self.write("scratch.txt", "notes\n")

        result = merge.merge_run(self.root, BRANCH, settings=self.settings)

        self.assertTrue(result["ok"], result)
        self.assertEqual([], result["dirty"])
        self.assertEqual("notes\n", (self.root / "scratch.txt").read_text())

    def test_a_checkout_on_another_branch_is_not_this_merge_s_business(self):
        """base is pinned, so HEAD sitting elsewhere means this dirt belongs
        to a different tree and the guard has nothing to say about it — and
        the refresh skips rather than yanking the owner off their branch."""
        self.config('[merge]\nbase = "main"\n')
        self.run_branch({"feature.py": "x = 1\n"})
        git(self.root, "checkout", "--quiet", "-b", "sidequest")
        self.write("app.py", "print('editing on a side branch')\n")
        self.write("sidequest.txt", "mine\n")

        result = merge.merge_run(self.root, BRANCH, settings=self.settings)

        self.assertTrue(result["ok"], result)
        self.assertEqual("main", result["base"])
        self.assertEqual([], result["dirty"])
        self.assertEqual("skipped", result["refresh"]["status"])
        self.assertIsNone(result["refresh"]["command"])
        self.assertIn("not on main", result["refresh"]["why"])
        self.assertEqual("sidequest", git(self.root, "rev-parse", "--abbrev-ref", "HEAD"))
        self.assertFalse((self.root / "feature.py").exists())
        self.assertEqual("print('editing on a side branch')\n",
                         (self.root / "app.py").read_text())
        self.assertEqual("mine\n", (self.root / "sidequest.txt").read_text())

    def test_a_mission_that_ordered_the_deletions_lands_them(self):
        # Run 35 was dispatched to delete dead code, deleted six files, and the
        # deletion tripwire escalated the exact work it was sent to do. The
        # judge exists so that never reaches a phone again.
        self.run_branch({"notes.txt": None, "feature.py": "x = 1\n"})
        asked = []

        def judge(cfg, mission, fired, diff):
            asked.append((mission, tuple(fired)))
            return {"verdict": "mission_work",
                    "rationale": "the mission is dead-code deletion"}

        result = merge.merge_run(self.root, BRANCH, settings=self.settings,
                                 mission="Delete the dead notes file.",
                                 judge=judge)
        self.assertTrue(result["ok"], result)
        self.assertEqual(1, len(asked))
        self.assertIn("Delete the dead notes file.", asked[0][0])
        self.assertTrue(any("delete" in f for f in asked[0][1]))
        # The facts stay on the record even though nobody was asked.
        self.assertTrue(result["tripwires"])
        self.assertEqual("mission_work", result["tripwire_verdict"]["verdict"])
        self.assertNotIn("notes.txt", git(self.root, "ls-tree", "--name-only", "main"))

    def test_every_escalation_leaves_the_base_unmoved_and_the_branch_kept(self):
        """Four ways a deletion-bearing branch fails to land, chained on one
        repo — safe because an escalation moves nothing.

        - No mission: the real judge refuses an empty mission before any
          model call, and the pipeline escalates on that refusal exactly as
          before the judge existed. The tripwire names the deleted file.
        - A judge that says escalate still escalates.
        - judge_tripwires = false: the judge must not even be consulted.
        - A declared check failure outranks tripwires: the stage is checks,
          and allow_deletions keeps the tripwire list empty."""
        self.run_branch({"notes.txt": None})
        before = git(self.root, "rev-parse", "main")

        with self.subTest("no mission means no judgment means escalate"):
            result = merge.merge_run(self.root, BRANCH, settings=self.settings)
            self.assertFalse(result["ok"])
            self.assertEqual("tripwires", result["stage"])
            self.assertIn("notes.txt", result["tripwires"][0])

        with self.subTest("a judge that says escalate still escalates"):
            result = merge.merge_run(
                self.root, BRANCH, settings=self.settings,
                mission="Refactor one function in feature.py.",
                judge=lambda cfg, m, f, d: {
                    "verdict": "escalate",
                    "rationale": "nothing here asks for a deletion"})
            self.assertFalse(result["ok"])
            self.assertEqual("tripwires", result["stage"])
            self.assertIn("nothing here asks for a deletion", result["escalation"])

        with self.subTest("judging can be turned off"):
            self.config("[merge]\njudge_tripwires = false\n")
            called = []
            result = merge.merge_run(self.root, BRANCH, settings=self.settings,
                                     mission="Delete everything.",
                                     judge=lambda *a: called.append(1))
            self.assertFalse(result["ok"])
            self.assertEqual([], called, "the judge must not even be consulted")

        with self.subTest("a declared check failure outranks tripwires"):
            self.config('[merge]\nallow_deletions = true\n\n'
                        '[merge.checks]\ntest = "exit 3"\n')
            result = merge.merge_run(self.root, BRANCH, settings=self.settings)
            self.assertFalse(result["ok"])
            self.assertEqual("checks", result["stage"])
            self.assertFalse(result["checks_skipped"])
            self.assertEqual(3, result["checks"][0]["exit_code"])
            self.assertEqual([], result["tripwires"])

        # None of the four moved the base or consumed the branch.
        self.assertEqual(before, git(self.root, "rev-parse", "main"))
        self.assertIn(BRANCH, git(self.root, "branch", "--list", BRANCH))

    def test_the_judge_itself_fails_toward_escalation(self):
        # No nameable profile, a dead turn, an unparsable reply: each is an
        # escalate, never a landing.
        v = merge.judge_tripwires({}, "", ["deletes 1 file(s)"], "diff")
        self.assertEqual("escalate", v["verdict"])
        # A cfg whose judge profile RESOLVES, so the turn actually runs and
        # the reply-parsing paths are the thing under test. With an empty cfg
        # the profile lookup fails first and the turn is never consulted.
        cfg = {"settings": {"observer_profile": "j"},
               "profiles": {"j": {"backend": "opencode", "model": "m"}}}
        ran = []
        v = merge.judge_tripwires(cfg, "a mission", ["deletes 1 file(s)"], "diff",
                                  turn=lambda p, t: ran.append(1) or "not json at all")
        self.assertEqual([1], ran, "the turn must actually have run")
        self.assertEqual("escalate", v["verdict"])
        v = merge.judge_tripwires(cfg, "a mission", ["deletes 1 file(s)"], "diff",
                                  turn=lambda p, t: (_ for _ in ()).throw(RuntimeError("dead")))
        self.assertEqual("escalate", v["verdict"])
        self.assertIn("dead", v["rationale"])
        # And the accepting path, same resolved profile.
        v = merge.judge_tripwires(cfg, "a mission", ["deletes 1 file(s)"], "diff",
                                  turn=lambda p, t: '{"verdict": "mission_work", "rationale": "asked for"}')
        self.assertEqual("mission_work", v["verdict"])

    def test_clean_merge_lands_deletes_the_branch_and_refreshes_the_checkout(self):
        self.run_branch({"feature.py": "x = 1\n"})
        before = git(self.root, "rev-parse", "main")

        result = merge.merge_run(self.root, BRANCH, item_id="W-0167", settings=self.settings)

        self.assertTrue(result["ok"], result)
        self.assertEqual("merged", result["stage"])
        self.assertEqual("main", result["base"])
        self.assertEqual(["feature.py"], result["files_changed"])
        self.assertTrue(result["checks_skipped"])
        self.assertEqual([], result["checks"])
        self.assertEqual(result["commit"], git(self.root, "rev-parse", "main"))
        self.assertNotEqual(before, result["commit"])
        self.assertIn(result["commit"], result["revert_command"])
        self.assertIn("revert -m 1", result["revert_command"])
        self.assertIn("feature.py", git(self.root, "ls-tree", "--name-only", "main"))
        self.assertTrue(result["branch_deleted"])
        self.assertNotIn(BRANCH, git(self.root, "branch", "--list", BRANCH))
        # merge commit, not a fast-forward
        self.assertEqual(2, len(git(self.root, "rev-list", "--parents", "-n", "1",
                                    result["commit"]).split()) - 1)
        # and the owner's clean checkout on the base is refreshed to it
        self.assertEqual("refreshed", result["refresh"]["status"])
        self.assertIsNone(result["refresh"]["command"])
        self.assertIsNone(result["note"])
        self.assertEqual("x = 1\n", (self.root / "feature.py").read_text())
        self.assertEqual("", git(self.root, "status", "--porcelain"))
        self.assertEqual(result["commit"], git(self.root, "rev-parse", "HEAD"))

    def test_declared_checks_run_against_the_rebased_content(self):
        self.config('[merge.checks]\ntest = "test -f feature.py"\n')
        self.run_branch({"feature.py": "x = 1\n"})

        result = merge.merge_run(self.root, BRANCH, settings=self.settings)

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["checks"][0]["ok"])

    def test_merge_leaves_the_dirty_working_tree_untouched(self):
        """The requirement that matters: the owner keeps their uncommitted work.

        require_clean only refuses when the edit OVERLAPS the merge. This
        pins the layer underneath it: with the guard off entirely, a merge
        must STILL never touch the owner's edits. Both properties are real
        and the outer one must not be the only thing standing between a run
        and someone's work in flight.
        """
        self.run_branch({"feature.py": "x = 1\n"})
        self.config("[merge]\nrequire_clean = false\n")
        self.write("notes.txt", "UNCOMMITTED EDIT\n")
        self.write("scratch.txt", "untracked\n")

        result = merge.merge_run(self.root, BRANCH, settings=self.settings)

        self.assertTrue(result["ok"], result)
        self.assertEqual("UNCOMMITTED EDIT\n", (self.root / "notes.txt").read_text())
        self.assertEqual("untracked\n", (self.root / "scratch.txt").read_text())
        # still an unstaged local modification (git() strips the leading column)
        self.assertIn("M notes.txt", git(self.root, "status", "--porcelain"))
        self.assertEqual("main", git(self.root, "rev-parse", "--abbrev-ref", "HEAD"))
        self.assertEqual(1, len(git(self.root, "worktree", "list").splitlines()))
        # the untouched edit does not block the refresh
        self.assertEqual("refreshed", result["refresh"]["status"])

    def test_a_base_that_moved_is_retried_not_escalated(self) -> None:
        """Two runs landing at once is a race, not a conflict. The
        compare-and-swap refuses the stale write; the merge rebases onto the
        new base and lands. The owner hears nothing (owner, 2026-08-14)."""
        self.run_branch({"notes.txt": "from the run\n"})
        landed = []

        real = merge._git

        def racer(args, cwd, check=True):
            # Land an unrelated commit on main the first time the swap runs,
            # exactly as a sibling run finishing a moment earlier would.
            if args[:1] == ["update-ref"] and not landed:
                landed.append(True)
                git(self.root, "checkout", "--quiet", "main")
                self.write("other.txt", "from a sibling run\n")
                git(self.root, "add", "-A")
                git(self.root, "commit", "--quiet", "-m", "sibling")
                git(self.root, "checkout", "--quiet", "--detach", "HEAD")
            return real(args, cwd, check=check)

        with mock.patch.object(merge, "_git", side_effect=racer):
            result = merge.merge_run(self.root, BRANCH, settings=self.settings)

        self.assertTrue(result["ok"], result.get("escalation"))
        self.assertEqual(result["stage"], "merged")
        self.assertTrue(result.get("races"), "the race should be recorded")
        # Both commits survive: the sibling's and this run's.
        log = subprocess.run(["git", "-C", str(self.root), "log", "--oneline", "main"],
                             capture_output=True, text=True).stdout
        self.assertIn("sibling", log)

    # --- a merge that had nothing to do (I-0077) ----------------------------

    def test_a_branch_already_on_the_base_reports_no_merge_commit(self):
        """Run 65's shape: the branch's work is already on main, and somebody
        else committed afterwards. The run created nothing, so nothing is
        attributed to it — least of all the owner's commit that HEAD happened
        to be sitting on. And `git revert -m 1 <not a merge>` either errors
        or, without -m 1, destroys a bystander's change, so the report and
        the note must not offer it at all."""
        self.run_branch({"feature.py": "x = 1\n"})
        git(self.root, "merge", "--quiet", "--no-ff", "-m", "run 62 landed it", BRANCH)
        self.write("ota.sh", "the owner's own commit\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "--quiet", "-m", "OTA install, nothing to do with the run")
        unrelated = git(self.root, "rev-parse", "main")

        result = merge.merge_run(self.root, BRANCH, settings=self.settings)

        self.assertTrue(result["ok"], result)
        self.assertEqual("merged", result["stage"])
        self.assertEqual([], result["files_changed"])
        self.assertIsNone(result["commit"])
        self.assertIsNone(result["revert_command"])
        self.assertIn("nothing to merge", result["note"])
        self.assertEqual(unrelated, git(self.root, "rev-parse", "main"))
        self.assertTrue(result["branch_deleted"])

        report = merge._report_text({"id": 65}, result, None)
        note = merge._note({"id": 65}, result, None)
        self.assertNotIn("revert", report)
        self.assertNotIn(unrelated, report)
        self.assertNotIn(unrelated, note)
        self.assertIn("nothing to merge", report)
        self.assertIn("nothing to merge", note)
        self.assertIn("merge commit: none", report)

    def test_the_report_names_only_checks_that_ran(self):
        not_run = merge.blank_result("main", BRANCH, checks_skipped=False)
        self.assertIn("- checks: not run",
                      merge._report_text({"id": 64}, not_run, None))
        undeclared = merge.blank_result("main", BRANCH, checks_skipped=True)
        self.assertIn("- checks: none declared",
                      merge._report_text({"id": 64}, undeclared, None))
        result = merge.blank_result("main", BRANCH, checks_skipped=False)
        result["checks"] = [
            {"name": "test", "ok": True, "exit_code": 0},
            {"name": "lint", "ok": False, "exit_code": 2},
        ]
        report = merge._report_text({"id": 65}, result, None)
        self.assertIn("- checks: test ok; lint FAILED (exit 2)", report)
        self.assertNotIn("review", report)

    def test_pause_does_not_block_completion_landing(self):
        result = merge.blank_result("main", BRANCH)
        result.update(ok=True, stage="merged", commit="abc123", files_changed=[],
                      branch_deleted=True, refresh={"why": "already current"},
                      revert_command="git revert -m 1 abc123")
        con = db.connect()
        dispatch.pause(con, "hold admissions")
        try:
            run = {"id": 7, "branch": BRANCH, "requested_by": "human",
                   "title": "done", "ref": None, "brief_path": None}
            with mock.patch.object(merge, "merge_run", return_value=result) as land, \
                    mock.patch.object(merge.project, "root_for", return_value=self.root), \
                    mock.patch.object(merge.nod, "withdraw_merge_cards"), \
                    mock.patch.object(merge, "_thread"):
                note = merge._land(con, {}, run, "done")
            land.assert_called_once()
            self.assertIn("Merged", note)
        finally:
            dispatch.resume(con)
            con.close()


class FileCardStageTestCase(unittest.TestCase):
    """The stage pass-through: a tripwire card and a conflict card stop
    looking identical once nod.merge_conflict grows ``stage=`` — and the
    guard lets this branch run against a nod.py that has not grown it yet."""

    def test_stage_is_passed_exactly_when_merge_conflict_accepts_it(self) -> None:
        result = merge._escalate(merge.blank_result("main", "orchestra/run-7"),
                                 "tripwires", "touches 99 files")
        seen = {}

        def grown(target, detail, *, stage=None, **ctx):
            seen["stage"] = stage
            return {"request_id": "req_9"}

        def today(target, detail, **ctx):
            seen.update(ctx)
            return {"request_id": "req_9"}

        for label, handler, expect_stage in (
                ("grown merge_conflict is passed the stage", grown, "tripwires"),
                ("today's merge_conflict is not", today, None)):
            with self.subTest(label):
                seen.clear()
                with mock.patch.object(merge.nod, "from_cfg",
                                       return_value=object()), \
                        mock.patch.object(merge.nod, "merge_conflict", handler):
                    rid = merge._file_card(None, {}, {"id": 7}, result)
                self.assertEqual("req_9", rid)
                self.assertEqual(expect_stage, seen.get("stage"))


class KeptRefTestCase(unittest.TestCase):
    """A merged run's diff must outlive its branch."""

    def test_merge_anchors_the_head_before_deleting_the_branch(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        run = lambda *a: subprocess.run(["git", "-C", str(root), *a], check=True,
                                        capture_output=True, text=True)
        run("init", "-q", "-b", "main")
        run("config", "user.email", "t@example.com")
        run("config", "user.name", "t")
        (root / "seed.txt").write_text("seed\n")
        run("add", "."); run("commit", "-qm", "seed")
        run("checkout", "-q", "-b", "orchestra/run-99")
        (root / "worker.txt").write_text("the worker's own commit\n")
        run("add", "."); run("commit", "-qm", "work")
        head = subprocess.run(["git", "-C", str(root), "rev-parse", "orchestra/run-99"],
                              capture_output=True, text=True).stdout.strip()
        run("checkout", "-q", "main")

        result = merge.merge_run(root, "orchestra/run-99", settings={"checks": []})
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["branch_deleted"], "the branch is gone")
        self.assertEqual(result["kept_ref"], "refs/orchestra/run-99")
        # The branch itself is gone. Note the bare name still resolves, because
        # git's rev-parse searches refs/<name> and finds the kept ref — which
        # is why even the old branch-name fallback keeps working after a merge.
        gone = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify",
                               "refs/heads/orchestra/run-99^{commit}"],
                              capture_output=True, text=True)
        self.assertNotEqual(gone.returncode, 0, "the branch is deleted")
        kept = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify",
                               "refs/orchestra/run-99^{commit}"], capture_output=True, text=True)
        self.assertEqual(kept.stdout.strip(), head)


if __name__ == "__main__":
    unittest.main()
