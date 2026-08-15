"""DESIGN §12: what a run can see -- backend-scoped skills + global overlay.

Plus W-0172: giving a worktree back. Every git command here runs against a
throwaway repository built under tempfile, never a real checkout.
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dromond import db, supervise, worktree


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class SyncSkillsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home = base / "home"          # stands in for ~/.dromond
        self.root = base / "project"
        self.wt = base / "wt"
        self.wt.mkdir(parents=True)
        self.env = mock.patch.dict(os.environ, {"DROMOND_HOME": str(self.home)})
        self.env.start()
        for name in (".agents/skills/shared/SKILL.md", ".claude/settings.json",
                     ".codex/config.toml", ".opencode/opencode.json"):
            write(self.root / name, "x")
        for name in ("AGENTS.md", "CLAUDE.md", "DROMOND.md"):
            write(self.root / name, "x")

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def test_codex_run_gets_no_other_harness_directory(self) -> None:
        worktree.sync_skills(self.root, self.wt, "codex")
        self.assertTrue((self.wt / ".codex").is_dir())
        self.assertTrue((self.wt / ".agents").is_dir())
        self.assertTrue((self.wt / "AGENTS.md").is_file())
        self.assertTrue((self.wt / "DROMOND.md").is_file())
        for absent in (".claude", ".opencode", "CLAUDE.md"):
            self.assertFalse((self.wt / absent).exists(), absent)

    def test_claude_run_gets_claude_dir_and_doc_only(self) -> None:
        worktree.sync_skills(self.root, self.wt, "claude")
        self.assertTrue((self.wt / ".claude").is_dir())
        self.assertTrue((self.wt / "CLAUDE.md").is_file())
        for absent in (".codex", ".opencode"):
            self.assertFalse((self.wt / absent).exists(), absent)

    def test_a_project_still_carrying_MAESTRO_md_keeps_getting_it(self) -> None:
        """W-0188 renamed the product, not the user's repositories. A project
        whose context file is still called MAESTRO.md must keep receiving it:
        dropping the old name would silently starve the run of its brief."""
        write(self.root / "MAESTRO.md", "the project's own instructions")
        worktree.sync_skills(self.root, self.wt, "codex")
        self.assertEqual((self.wt / "MAESTRO.md").read_text(),
                         "the project's own instructions")
        # And it is excluded from checkpoints like every other context file,
        # so it never lands in the run's diff as if the run had written it.
        self.assertIn("MAESTRO.md", worktree.DOC_FILES)

    def test_unknown_backend_gets_the_shared_set_only(self) -> None:
        worktree.sync_skills(self.root, self.wt, None)
        self.assertTrue((self.wt / ".agents").is_dir())
        for absent in (".claude", ".codex", ".opencode"):
            self.assertFalse((self.wt / absent).exists(), absent)

    def test_global_overlay_reaches_a_project_that_lacks_the_skill(self) -> None:
        write(self.home / "skills" / "orchestration" / "SKILL.md", "global")
        synced = worktree.sync_skills(self.root, self.wt, "codex")
        landed = self.wt / ".agents/skills" / "orchestration" / "SKILL.md"
        self.assertEqual(landed.read_text(), "global")
        self.assertIn(".agents/skills/orchestration", synced)

    def test_a_claude_run_finds_the_overlay_under_claude_skills(self) -> None:
        """Claude Code discovers skills at .claude/skills, never .agents."""
        write(self.home / "skills" / "orchestration" / "SKILL.md", "global")
        worktree.sync_skills(self.root, self.wt, "claude")
        landed = self.wt / ".claude" / "skills" / "orchestration" / "SKILL.md"
        self.assertEqual(landed.read_text(), "global")
        self.assertFalse((self.wt / ".agents/skills/orchestration").exists())

    def test_a_reasonix_run_finds_the_overlay_under_reasonix_skills(self) -> None:
        write(self.home / "skills" / "orchestration" / "SKILL.md", "global")
        worktree.sync_skills(self.root, self.wt, "reasonix")
        landed = self.wt / ".reasonix" / "skills" / "orchestration" / "SKILL.md"
        self.assertEqual(landed.read_text(), "global")

    def test_project_skill_of_the_same_name_wins_over_the_overlay(self) -> None:
        write(self.root / ".agents" / "skills" / "shared" / "SKILL.md", "project")
        write(self.home / "skills" / "shared" / "SKILL.md", "global")
        worktree.sync_skills(self.root, self.wt, "codex")
        landed = self.wt / ".agents/skills" / "shared" / "SKILL.md"
        self.assertEqual(landed.read_text(), "project")

    def test_project_claude_skill_wins_over_the_overlay(self) -> None:
        write(self.root / ".claude" / "skills" / "shared" / "SKILL.md", "project")
        write(self.home / "skills" / "shared" / "SKILL.md", "global")
        worktree.sync_skills(self.root, self.wt, "claude")
        landed = self.wt / ".claude" / "skills" / "shared" / "SKILL.md"
        self.assertEqual(landed.read_text(), "project")

    def test_create_scopes_the_worktree_to_the_run_backend(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        write(self.root / "README.md", "r")  # harness dirs stay untracked
        for cmd in (["config", "user.email", "t@t"], ["config", "user.name", "t"],
                    ["add", "README.md"], ["commit", "-qm", "init"]):
            subprocess.run(["git", "-C", str(self.root), *cmd], check=True,
                           capture_output=True)
        wt, branch = worktree.create(self.root, 7, "proj-uuid", backend="codex")
        self.assertEqual(branch, "dromond/run-7")
        self.assertFalse((wt / ".claude").exists())
        self.assertTrue((wt / ".codex").exists())


class WorktreeRemovalTests(unittest.TestCase):
    """W-0172: a terminal run's checkout goes; a live run's checkout stays."""

    PROJECT = "proj-uuid"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.home = base / "home"
        self.root = base / "project"
        self.env = mock.patch.dict(os.environ, {
            "DROMOND_HOME": str(self.home),
            "DROMOND_CONFIG": str(base / "absent.toml")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.root.mkdir(parents=True)
        self.git("init", "--quiet")
        self.git("symbolic-ref", "HEAD", "refs/heads/main")
        for pair in (("user.email", "t@t"), ("user.name", "t"),
                     ("commit.gpgsign", "false")):
            self.git("config", *pair)
        write(self.root / "README.md", "r")
        self.git("add", "README.md")
        self.git("commit", "--quiet", "-m", "init")
        self.con = db.connect()
        self.addCleanup(self.con.close)

    def git(self, *args: str, root: Path | None = None) -> str:
        r = subprocess.run(["git", "-C", str(root or self.root), *args],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise AssertionError(f"git {' '.join(args)}: {r.stderr.strip()}")
        return r.stdout.strip()

    def make_run(self, run_id: int, status: str, commit: bool = True) -> Path:
        """A run row plus its worktree, as dispatch would leave them."""
        wt, branch = worktree.create(self.root, run_id, self.PROJECT,
                                     backend="claude")
        if commit:
            write(wt / f"feature-{run_id}.py", "x = 1\n")
            self.git("add", f"feature-{run_id}.py", root=wt)
            self.git("commit", "--quiet", "-m", f"run {run_id}", root=wt)
        self.con.execute(
            "INSERT INTO runs(id, profile, backend, requested_by, workdir, "
            "project_id, branch, status, started_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (run_id, "p", "claude", "t", str(wt), self.PROJECT, branch, status,
             db.now()))
        self.con.commit()
        return wt

    def branches(self) -> str:
        return self.git("branch", "--list")

    # --- terminal removal ----------------------------------------------------

    def test_terminal_run_loses_its_worktree_and_the_branch_is_deletable(self):
        wt = self.make_run(1, "done")
        run = self.con.execute("SELECT * FROM runs WHERE id=1").fetchone()

        note = supervise.release_worktree(self.con, dict(run), "done")

        self.assertIsNone(note)
        self.assertFalse(wt.exists())
        self.assertIn("dromond/run-1", self.branches())
        # the whole point: merge.py can now delete the branch
        deleted = subprocess.run(["git", "-C", str(self.root), "branch", "-D",
                                  "dromond/run-1"], capture_output=True, text=True)
        self.assertEqual(0, deleted.returncode, deleted.stderr)

    def test_copied_context_files_do_not_block_removal(self):
        """sync_skills leaves .claude/ untracked in every run worktree."""
        wt = self.make_run(2, "failed")
        write(wt / ".claude" / "settings.json", "{}")
        self.assertIn("?? .claude/", worktree.status(wt))

        supervise.release_worktree(
            self.con, dict(self.con.execute("SELECT * FROM runs WHERE id=2"
                                            ).fetchone()), "failed")

        self.assertFalse(wt.exists())

    def test_a_live_run_in_the_same_checkout_keeps_it(self):
        """A follow-up run inherits its parent's workdir (create_followup)."""
        wt = self.make_run(3, "done")
        self.con.execute(
            "INSERT INTO runs(id, profile, backend, requested_by, workdir, "
            "project_id, branch, status, started_at) VALUES(4,'p','claude','t',"
            "?, ?, 'dromond/run-3', 'running', ?)",
            (str(wt), self.PROJECT, db.now()))
        self.con.commit()

        supervise.release_worktree(
            self.con, dict(self.con.execute("SELECT * FROM runs WHERE id=3"
                                            ).fetchone()), "done")

        self.assertTrue(wt.exists())

    def test_uncommitted_work_is_kept_and_reported_at_finalization(self):
        wt = self.make_run(5, "failed")
        write(wt / "README.md", "half-finished\n")

        note = supervise.release_worktree(
            self.con, dict(self.con.execute("SELECT * FROM runs WHERE id=5"
                                            ).fetchone()), "failed")

        self.assertTrue(wt.exists())
        self.assertIn("Worktree kept", note)
        self.assertIn("1 uncommitted change(s)", note)
        self.assertIn("README.md", note)

    def test_a_directory_already_deleted_by_hand_is_pruned_from_git(self):
        wt = self.make_run(6, "done")
        subprocess.run(["rm", "-rf", str(wt)], check=True)

        report = worktree.remove(wt, self.root, branch="dromond/run-6")

        self.assertTrue(report["removed"], report)
        self.assertNotIn("run-6", self.git("worktree", "list"))
        self.assertEqual(0, subprocess.run(
            ["git", "-C", str(self.root), "branch", "-D", "dromond/run-6"],
            capture_output=True).returncode)

    # --- dromond prune -------------------------------------------------------

    def test_prune_removes_a_terminal_worktree_and_keeps_a_live_one(self):
        done = self.make_run(10, "done", commit=False)
        running = self.make_run(11, "running")
        spawning = self.make_run(12, "spawning", commit=False)
        interrupt = self.make_run(13, "interrupt", commit=False)

        report = worktree.prune(self.con)

        self.assertFalse(done.exists())
        for live in (running, spawning, interrupt):
            self.assertTrue(live.exists(), live)
        kept = {r["workdir"]: r["kept"] for r in report["worktrees"] if r["kept"]}
        self.assertEqual({str(running): "run 11 is running",
                          str(spawning): "run 12 is spawning",
                          str(interrupt): "run 13 is interrupt"}, kept)

    def test_prune_removes_a_worktree_whose_run_row_is_gone(self):
        wt = self.make_run(20, "done", commit=False)
        self.con.execute("DELETE FROM runs WHERE id=20")
        self.con.commit()

        report = worktree.prune(self.con)

        self.assertFalse(wt.exists())
        self.assertEqual([str(wt)], [r["workdir"] for r in report["worktrees"]
                                     if r["removed"]])

    def test_prune_keeps_unmerged_work_and_says_so(self):
        dirty = self.make_run(30, "failed", commit=False)
        write(dirty / "scratch.py", "unsaved\n")
        self.git("add", "scratch.py", root=dirty)
        unmerged = self.make_run(31, "done")  # a commit not on main

        report = worktree.prune(self.con)

        self.assertTrue(dirty.exists())
        self.assertTrue(unmerged.exists())
        kept = {r["workdir"]: r["kept"] for r in report["worktrees"]}
        self.assertIn("1 uncommitted change(s)", kept[str(dirty)])
        self.assertIn("scratch.py", kept[str(dirty)])
        self.assertIn("1 commit(s) on dromond/run-31 not on main", kept[str(unmerged)])

    def test_force_removes_them_and_reports_what_it_discarded(self):
        dirty = self.make_run(40, "failed", commit=False)
        write(dirty / "scratch.py", "unsaved\n")

        report = worktree.prune(self.con, force=True)

        self.assertFalse(dirty.exists())
        entry = next(r for r in report["worktrees"] if r["workdir"] == str(dirty))
        self.assertTrue(entry["removed"])
        self.assertIn("1 uncommitted change(s)", "; ".join(entry["discarded"]))
        self.assertIn("scratch.py", "; ".join(entry["discarded"]))

    def test_force_still_refuses_to_touch_a_live_run(self):
        running = self.make_run(50, "running", commit=False)

        worktree.prune(self.con, force=True)

        self.assertTrue(running.exists())

    def test_prune_removes_the_empty_project_directory_it_leaves(self):
        wt = self.make_run(60, "done", commit=False)
        project_dir = wt.parent

        report = worktree.prune(self.con)

        self.assertFalse(project_dir.exists())
        self.assertEqual([str(project_dir)], report["dirs"])

    def test_prune_keeps_the_project_directory_of_a_live_run(self):
        wt = self.make_run(70, "running", commit=False)

        report = worktree.prune(self.con)

        self.assertTrue(wt.parent.is_dir())
        self.assertEqual([], report["dirs"])


if __name__ == "__main__":
    unittest.main()
