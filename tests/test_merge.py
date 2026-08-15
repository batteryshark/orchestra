"""Merge behaviors against throwaway git repositories (never a real one)."""
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from unittest import mock
from pathlib import Path

from dromond import merge

BRANCH = "dromond/run-1"


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

    def _live_record_store(self) -> None:
        """A repository that tracked a service's record store, then stopped.

        This is the real shape of the recurring escalation: `.work/` held a
        live Work database, git tracked it, and every run committed whatever
        snapshot happened to be on disk.
        """
        (self.root / ".work").mkdir()
        self.write(".work/W-0171.md", "status: backlog\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "--quiet", "-m", "records, tracked by mistake")

    def test_a_run_cannot_land_a_file_the_base_branch_stopped_tracking(self):
        self._live_record_store()
        # The run edited the record store, as an agent with a file editor will
        # when the file is sitting in its worktree.
        self.run_branch({".work/W-0171.md": "status: done\n",
                         "docs.md": "the actual work\n"})
        # Meanwhile the base untracked it and Work kept writing the real copy.
        git(self.root, "rm", "-r", "--quiet", "--cached", ".work")
        self.write(".gitignore", ".work/\n")
        git(self.root, "add", ".gitignore")
        git(self.root, "commit", "--quiet", "-m", "stop tracking .work")
        (self.root / ".work" / "W-0171.md").write_text("status: in_progress\n")

        result = merge.merge_run(self.root, BRANCH, settings=self.settings)

        # Before this rule the rebase conflicted here, every time, forever.
        self.assertTrue(result["ok"], result)
        self.assertEqual([".work/W-0171.md"], result["dropped"])
        self.assertEqual(["docs.md"], result["files_changed"])
        # The service's own copy is untouched on disk; only the run's stale
        # snapshot was dropped.
        self.assertEqual("status: in_progress\n",
                         (self.root / ".work" / "W-0171.md").read_text())
        self.assertNotIn(".work", git(self.root, "ls-tree", "-r", "--name-only", "main"))

    def test_a_run_that_only_touched_untracked_state_still_lands(self):
        self._live_record_store()
        self.run_branch({".work/W-0171.md": "status: done\n"})
        git(self.root, "rm", "-r", "--quiet", "--cached", ".work")
        self.write(".gitignore", ".work/\n")
        git(self.root, "add", ".gitignore")
        git(self.root, "commit", "--quiet", "-m", "stop tracking .work")

        result = merge.merge_run(self.root, BRANCH, settings=self.settings)

        self.assertTrue(result["ok"], result)
        self.assertEqual([".work/W-0171.md"], result["dropped"])
        self.assertEqual([], result["files_changed"])

    def test_a_real_source_conflict_still_reaches_the_human(self):
        # The rule drops what the base says is not source. It must not soften
        # a genuine conflict in code, which is a judgment nobody can automate.
        self.run_branch({"app.py": "print('from the run')\n"})
        self.write("app.py", "print('from the owner')\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "--quiet", "-m", "owner edit")

        result = merge.merge_run(self.root, BRANCH, settings=self.settings)

        self.assertFalse(result["ok"])
        self.assertEqual("rebase", result["stage"])
        self.assertEqual(["app.py"], result["conflicts"])
        self.assertEqual([], result["dropped"])

    def test_a_dirty_base_checkout_refuses_the_merge(self):
        # The merge cannot commit these files -- it happens in a scratch
        # worktree -- but landing would move the ref under someone mid-edit,
        # and the refresh would then decline and leave them on an older tree
        # with no visible reason.
        self.run_branch({"feature.py": "x = 1\n"})
        self.write("app.py", "print('owner is editing this')\n")
        before = git(self.root, "rev-parse", "main")

        result = merge.merge_run(self.root, BRANCH, settings=self.settings)

        self.assertFalse(result["ok"])
        self.assertEqual("dirty", result["stage"])
        self.assertEqual(["app.py"], result["dirty"])
        self.assertIn("commit or stash", result["escalation"])
        self.assertEqual(before, git(self.root, "rev-parse", "main"))
        # The branch is untouched, so the merge is one command away once clean.
        self.assertIn(BRANCH, git(self.root, "branch", "--list", BRANCH))

    def test_an_untracked_file_is_not_dirty_enough_to_refuse(self):
        # A build directory or a scratch note is not work in flight, and
        # refusing on one would make the guard useless within a day.
        self.run_branch({"feature.py": "x = 1\n"})
        self.write("scratch.txt", "notes\n")

        result = merge.merge_run(self.root, BRANCH, settings=self.settings)

        self.assertTrue(result["ok"], result)
        self.assertEqual([], result["dirty"])
        self.assertEqual("notes\n", (self.root / "scratch.txt").read_text())

    def test_require_clean_can_be_turned_off(self):
        self.run_branch({"feature.py": "x = 1\n"})
        self.write("app.py", "print('owner is editing this')\n")
        self.config("[merge]\nrequire_clean = false\n")

        result = merge.merge_run(self.root, BRANCH, settings=self.settings)

        self.assertTrue(result["ok"], result)
        # The owner's edit is still theirs: never committed, never reverted.
        self.assertEqual("print('owner is editing this')\n",
                         (self.root / "app.py").read_text())

    def test_a_checkout_on_another_branch_is_not_this_merge_s_business(self):
        # base is pinned, so HEAD sitting elsewhere means this dirt belongs to
        # a different tree and the guard has nothing to say about it.
        self.run_branch({"feature.py": "x = 1\n"})
        self.config("[merge]\nbase = \"main\"\n")
        git(self.root, "checkout", "--quiet", "-b", "side")
        self.write("app.py", "print('editing on a side branch')\n")

        result = merge.merge_run(self.root, BRANCH, settings=self.settings)

        self.assertTrue(result["ok"], result)
        self.assertEqual([], result["dirty"])

    def test_clean_merge_lands_and_deletes_the_branch(self):
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

    def test_conflicting_rebase_aborts_cleanly(self):
        self.run_branch({"notes.txt": "run version\n"})
        self.write("notes.txt", "main version\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "--quiet", "-m", "main moves")
        before = git(self.root, "rev-parse", "main")

        result = merge.merge_run(self.root, BRANCH, settings=self.settings)

        self.assertFalse(result["ok"])
        self.assertEqual("rebase", result["stage"])
        self.assertEqual(["notes.txt"], result["conflicts"])
        self.assertIsNone(result["commit"])
        self.assertEqual(before, git(self.root, "rev-parse", "main"))
        # branch kept, no scratch worktree and no half-rebased state left
        self.assertIn(BRANCH, git(self.root, "branch", "--list", BRANCH))
        self.assertEqual(1, len(git(self.root, "worktree", "list").splitlines()))
        self.assertEqual([], list(self.root.glob(".git/worktrees/*")))
        self.assertFalse((self.root / ".git" / "rebase-merge").exists())

    def test_tripwire_blocks_the_merge(self):
        self.run_branch({"app.py": None})
        before = git(self.root, "rev-parse", "main")

        result = merge.merge_run(self.root, BRANCH, settings=self.settings)

        self.assertFalse(result["ok"])
        self.assertEqual("tripwires", result["stage"])
        self.assertIn("app.py", result["tripwires"][0])
        self.assertEqual(before, git(self.root, "rev-parse", "main"))
        self.assertIn(BRANCH, git(self.root, "branch", "--list", BRANCH))

    def test_declared_check_failure_outranks_tripwires(self):
        self.config('[merge]\nallow_deletions = true\n\n'
                    '[merge.checks]\ntest = "exit 3"\n')
        self.run_branch({"app.py": None})

        result = merge.merge_run(self.root, BRANCH, settings=self.settings)

        self.assertFalse(result["ok"])
        self.assertEqual("checks", result["stage"])
        self.assertFalse(result["checks_skipped"])
        self.assertEqual(3, result["checks"][0]["exit_code"])
        self.assertEqual([], result["tripwires"])

    def test_declared_checks_run_against_the_rebased_content(self):
        self.config('[merge.checks]\ntest = "test -f feature.py"\n')
        self.run_branch({"feature.py": "x = 1\n"})

        result = merge.merge_run(self.root, BRANCH, settings=self.settings)

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["checks"][0]["ok"])

    def test_review_runs_only_when_criteria_exist(self):
        seen = []

        def review(diff, criteria):
            seen.append((diff, criteria))
            return {"ok": False, "verdict": "fail", "notes": "criterion 2 unmet"}

        self.run_branch({"feature.py": "x = 1\n"})
        result = merge.merge_run(self.root, BRANCH, criteria="- does X",
                                 review=review, settings=self.settings)
        self.assertEqual(1, len(seen))
        self.assertIn("feature.py", seen[0][0])
        self.assertEqual("- does X", seen[0][1])
        self.assertFalse(result["ok"])
        self.assertEqual("review", result["stage"])
        self.assertEqual("criterion 2 unmet", result["escalation"])
        self.assertIn(BRANCH, git(self.root, "branch", "--list", BRANCH))

        # no acceptance criteria, no review
        result = merge.merge_run(self.root, BRANCH, criteria="  ", review=review, settings=self.settings)
        self.assertEqual(1, len(seen))
        self.assertTrue(result["ok"], result)
        self.assertIsNone(result["review"])

    def test_merge_leaves_the_dirty_working_tree_untouched(self):
        """The requirement that matters: the owner keeps their uncommitted work.

        require_clean now refuses this merge before it starts, which is the
        better answer. This still pins the layer underneath it: when the guard
        is off, a merge must STILL never touch the owner's edits. Both
        properties are real and the outer one must not be the only thing
        standing between a run and someone's work in flight.
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

    # --- refreshing the owner's checkout ------------------------------------


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

    def test_refresh_updates_a_clean_checkout_on_the_base(self):
        self.run_branch({"feature.py": "x = 1\n"})

        result = merge.merge_run(self.root, BRANCH, settings=self.settings)

        self.assertEqual("refreshed", result["refresh"]["status"])
        self.assertIsNone(result["refresh"]["command"])
        self.assertIsNone(result["note"])
        self.assertEqual("x = 1\n", (self.root / "feature.py").read_text())
        self.assertEqual("", git(self.root, "status", "--porcelain"))
        self.assertEqual(result["commit"], git(self.root, "rev-parse", "HEAD"))

    def test_refresh_refuses_rather_than_clobber_a_local_edit(self):
        # With the guard off, the refresh is the last line of defence, and it
        # declines rather than overwriting. That is why turning require_clean
        # off is safe rather than reckless.
        self.run_branch({"notes.txt": "run version\n"})
        self.config("[merge]\nrequire_clean = false\n")
        self.write("notes.txt", "MY UNCOMMITTED EDIT\n")

        result = merge.merge_run(self.root, BRANCH, settings=self.settings)

        self.assertTrue(result["ok"], result)
        self.assertEqual("refused", result["refresh"]["status"])
        self.assertEqual("MY UNCOMMITTED EDIT\n", (self.root / "notes.txt").read_text())
        self.assertIn("notes.txt", git(self.root, "status", "--porcelain"))
        self.assertIn("read-tree -m -u", result["refresh"]["command"])
        self.assertIn("read-tree -m -u", result["note"])
        self.assertIn("overwritten", result["refresh"]["why"])

    def test_refresh_skips_a_checkout_on_another_branch(self):
        self.config('[merge]\nbase = "main"\n')
        self.run_branch({"feature.py": "x = 1\n"})
        git(self.root, "checkout", "--quiet", "-b", "sidequest")
        self.write("sidequest.txt", "mine\n")

        result = merge.merge_run(self.root, BRANCH, settings=self.settings)

        self.assertTrue(result["ok"], result)
        self.assertEqual("main", result["base"])
        self.assertEqual("skipped", result["refresh"]["status"])
        self.assertIsNone(result["refresh"]["command"])
        self.assertIn("not on main", result["refresh"]["why"])
        self.assertEqual("sidequest", git(self.root, "rev-parse", "--abbrev-ref", "HEAD"))
        self.assertFalse((self.root / "feature.py").exists())
        self.assertEqual("mine\n", (self.root / "sidequest.txt").read_text())


if __name__ == "__main__":
    unittest.main()


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
        run("checkout", "-q", "-b", "dromond/run-99")
        (root / "worker.txt").write_text("the worker's own commit\n")
        run("add", "."); run("commit", "-qm", "work")
        head = subprocess.run(["git", "-C", str(root), "rev-parse", "dromond/run-99"],
                              capture_output=True, text=True).stdout.strip()
        run("checkout", "-q", "main")

        result = merge.merge_run(root, "dromond/run-99", settings={"checks": []})
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["branch_deleted"], "the branch is gone")
        self.assertEqual(result["kept_ref"], "refs/dromond/run-99")
        # The branch itself is gone. Note the bare name still resolves, because
        # git's rev-parse searches refs/<name> and finds the kept ref — which
        # is why even the old branch-name fallback keeps working after a merge.
        gone = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify",
                               "refs/heads/dromond/run-99^{commit}"],
                              capture_output=True, text=True)
        self.assertNotEqual(gone.returncode, 0, "the branch is deleted")
        kept = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify",
                               "refs/dromond/run-99^{commit}"], capture_output=True, text=True)
        self.assertEqual(kept.stdout.strip(), head)
