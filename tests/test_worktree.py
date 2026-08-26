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

from orchestra import db, supervise, worktree


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def git(*args: str, root: Path) -> str:
    r = subprocess.run(["git", "-C", str(root), *args],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


def init_repo(root: Path) -> None:
    """A throwaway repository on main whose only tracked file is README.md,
    so harness dirs stay untracked."""
    root.mkdir(parents=True, exist_ok=True)
    git("init", "--quiet", root=root)
    git("symbolic-ref", "HEAD", "refs/heads/main", root=root)
    for pair in (("user.email", "t@t"), ("user.name", "t"),
                 ("commit.gpgsign", "false")):
        git("config", *pair, root=root)
    write(root / "README.md", "r")
    git("add", "README.md", root=root)
    git("commit", "--quiet", "-m", "init", root=root)


class SyncSkillsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home = base / "home"          # stands in for ~/.orchestra
        self.root = base / "project"
        self.wt = base / "wt"
        self.wt.mkdir(parents=True)
        self.env = mock.patch.dict(os.environ, {"ORCHESTRA_HOME": str(self.home)})
        self.env.start()
        for name in (".agents/skills/shared/SKILL.md", ".claude/settings.json",
                     ".codex/config.toml", ".opencode/opencode.json"):
            write(self.root / name, "x")
        for name in ("AGENTS.md", "CLAUDE.md", "ORCHESTRA.md"):
            write(self.root / name, "x")

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def test_context_is_scoped_to_the_selected_backend(self) -> None:
        cases = {
            "codex": ((".codex", ".agents", "AGENTS.md", "ORCHESTRA.md"),
                      (".claude", ".opencode", "CLAUDE.md")),
            "claude": ((".claude", "CLAUDE.md"), (".codex", ".opencode")),
            None: ((".agents",), (".claude", ".codex", ".opencode")),
        }
        for backend, (present, absent) in cases.items():
            with self.subTest(backend=backend):
                target = Path(self.tmp.name) / f"wt-{backend or 'unknown'}"
                target.mkdir()
                worktree.sync_skills(self.root, target, backend)
                for name in present:
                    self.assertTrue((target / name).exists(), name)
                for name in absent:
                    self.assertFalse((target / name).exists(), name)

    def test_global_overlay_reaches_each_harness_discovery_directory(self) -> None:
        write(self.home / "skills" / "orchestration" / "SKILL.md", "global")
        for backend, directory in (("codex", ".agents"),
                                   ("claude", ".claude"),
                                   ("reasonix", ".reasonix")):
            with self.subTest(backend=backend):
                target = Path(self.tmp.name) / f"overlay-{backend}"
                target.mkdir()
                synced = worktree.sync_skills(self.root, target, backend)
                landed = target / directory / "skills/orchestration/SKILL.md"
                self.assertEqual(landed.read_text(), "global")
                self.assertTrue(any("skills/orchestration" in path
                                    for path in synced))

    def test_project_skill_wins_over_the_global_overlay(self) -> None:
        write(self.home / "skills" / "shared" / "SKILL.md", "global")
        for backend, directory in (("codex", ".agents"),
                                   ("claude", ".claude")):
            with self.subTest(backend=backend):
                write(self.root / directory / "skills/shared/SKILL.md", "project")
                target = Path(self.tmp.name) / f"override-{backend}"
                target.mkdir()
                worktree.sync_skills(self.root, target, backend)
                landed = target / directory / "skills/shared/SKILL.md"
                self.assertEqual(landed.read_text(), "project")

    def test_create_scopes_the_worktree_to_the_run_backend(self) -> None:
        init_repo(self.root)
        wt, branch = worktree.create(self.root, 7, "proj-uuid", backend="codex")
        self.assertEqual(branch, "orchestra/run-7")
        self.assertFalse((wt / ".claude").exists())
        self.assertTrue((wt / ".codex").exists())

        with mock.patch.object(worktree, "sync_skills",
                               side_effect=OSError("context copy failed")), \
                self.assertRaisesRegex(OSError, "context copy failed"):
            worktree.create(self.root, 8, "proj-uuid", backend="codex")
        self.assertFalse((self.home / "worktrees/proj-uuid/run-8").exists())
        branches = subprocess.run(
            ["git", "-C", str(self.root), "branch", "--list", "orchestra/run-8"],
            check=True, capture_output=True, text=True).stdout
        self.assertEqual(branches.strip(), "")


class SubmoduleTests(unittest.TestCase):
    """PREX3 runs 93, 94, and 99: `git worktree add` leaves a declared
    submodule as an EMPTY directory. Each worker needed godot-cpp to build,
    reasoned that a symlink to the main checkout was "a local workspace fix,
    not a git write", and made `git status` refuse the path — which killed
    the checkpoint of three runs whose work was finished and good.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.home = base / "home"
        self.root = base / "project"
        self.env = mock.patch.dict(os.environ, {"ORCHESTRA_HOME": str(self.home)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.inner = base / "inner"
        init_repo(self.inner)
        write(self.inner / "lib.h", "#pragma once\n")
        git("add", "lib.h", root=self.inner)
        git("commit", "--quiet", "-m", "lib", root=self.inner)
        init_repo(self.root)

    def _add_submodule(self) -> None:
        subprocess.run(
            ["git", "-C", str(self.root), "-c", "protocol.file.allow=always",
             "submodule", "add", "--quiet", str(self.inner), "vendor/lib"],
            check=True, capture_output=True, text=True)
        git("commit", "--quiet", "-m", "vendor", root=self.root)

    def test_a_worktree_gets_the_submodule_populated(self) -> None:
        self._add_submodule()
        wt, _ = worktree.create(self.root, 21, "proj-sub", backend="codex")
        landed = wt / "vendor/lib"
        self.assertFalse(landed.is_symlink(),
                         "the worker has no reason to reach for a symlink")
        self.assertTrue((landed / "lib.h").is_file(),
                        "the submodule's content is actually there")
        # And the checkpoint's first act — reading status — works, which is
        # exactly what the symlink made impossible.
        self.assertFalse(worktree.status(wt), "a clean worktree reads clean")

    def test_a_project_without_submodules_is_untouched(self) -> None:
        self.assertFalse(worktree.submodules(self.root, self.root),
                         "no .gitmodules, no submodule work")

    def test_a_submodule_that_cannot_be_checked_out_still_starts_the_run(self) -> None:
        """A worktree with empty submodules is what every run got before
        this; failing to populate them must not refuse the run."""
        self._add_submodule()
        with mock.patch.object(worktree.subprocess, "run",
                               return_value=subprocess.CompletedProcess(
                                   [], 1, "", "no network")):
            self.assertFalse(worktree.submodules(self.root, self.root))


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
            "ORCHESTRA_HOME": str(self.home),
            "ORCHESTRA_CONFIG": str(base / "absent.toml")})
        self.env.start()
        self.addCleanup(self.env.stop)
        init_repo(self.root)
        self.con = db.connect()
        self.addCleanup(self.con.close)

    def git(self, *args: str, root: Path | None = None) -> str:
        return git(*args, root=root or self.root)

    def run_row(self, run_id: int) -> dict:
        return dict(self.con.execute("SELECT * FROM runs WHERE id=?",
                                     (run_id,)).fetchone())

    def release(self, run_id: int, status: str) -> str | None:
        return supervise.release_worktree(self.con, self.run_row(run_id), status)

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

        note = self.release(1, "done")

        self.assertIsNone(note)
        self.assertFalse(wt.exists())
        self.assertIn("orchestra/run-1", self.branches())
        # the whole point: merge.py can now delete the branch
        deleted = subprocess.run(["git", "-C", str(self.root), "branch", "-D",
                                  "orchestra/run-1"], capture_output=True, text=True)
        self.assertEqual(0, deleted.returncode, deleted.stderr)

    def test_copied_context_files_do_not_block_removal(self):
        """sync_skills leaves .claude/ untracked in every run worktree."""
        wt = self.make_run(2, "failed")
        write(wt / ".claude" / "settings.json", "{}")
        self.assertIn("?? .claude/", worktree.status(wt))

        self.release(2, "failed")

        self.assertFalse(wt.exists())

    def test_a_live_run_in_the_same_checkout_keeps_it(self):
        """A follow-up run inherits its parent's workdir (create_followup)."""
        wt = self.make_run(3, "done")
        self.con.execute(
            "INSERT INTO runs(id, profile, backend, requested_by, workdir, "
            "project_id, branch, status, started_at) VALUES(4,'p','claude','t',"
            "?, ?, 'orchestra/run-3', 'running', ?)",
            (str(wt), self.PROJECT, db.now()))
        self.con.commit()

        self.release(3, "done")

        self.assertTrue(wt.exists())

    def test_checkpoint_commits_file_writes_the_run_left(self):
        """W-0259: the host owns the commit, even when the backend could."""
        wt = self.make_run(80, "done", commit=False)
        write(wt / "feature.py", "x = 1\n")
        run = self.run_row(80)
        run["base_commit"] = worktree.head(wt)

        sha = supervise._checkpoint_commit(run, "done")

        self.assertIsNotNone(sha)
        self.assertNotEqual(sha, run["base_commit"])
        self.assertEqual([], worktree.dirty_paths(wt))
        self.assertIn("checkpoint run 80", self.git("log", "-1", "--format=%s",
                                                    root=wt))
        self.assertIn("feature.py", self.git("show", "--name-only", "--format=",
                                             sha, root=wt))

        # Checkout release is an external filesystem step. If it crashes,
        # recovery must still know which commit owns the worker's changes.
        log = self.home / "logs" / "run-80.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.touch()
        self.con.execute(
            "UPDATE runs SET base_commit=?, log_path=? WHERE id=80",
            (run["base_commit"], str(log)))
        self.con.commit()
        persisted = self.run_row(80)
        with mock.patch.object(supervise, "release_worktree",
                               side_effect=RuntimeError("release crashed")), \
                self.assertRaisesRegex(RuntimeError, "release crashed"):
            supervise.finalize_run(self.con, persisted, "done", 0)
        other = db.connect()
        try:
            self.assertEqual(other.execute(
                "SELECT checkpoint_commit FROM runs WHERE id=80"
            ).fetchone()["checkpoint_commit"], sha)
        finally:
            other.close()

        # Recovery must not checkpoint anything that appeared after that
        # durable pointer. It may retry checkout release, but the result still
        # owns exactly the commit recorded before the crash.
        write(wt / "late-human-edit.py", "leave me alone\n")
        recovered = self.run_row(80)
        with mock.patch.object(supervise, "_checkpoint_commit") as checkpoint, \
                mock.patch.object(supervise, "release_worktree", return_value=None):
            result = supervise.finalize_run(self.con, recovered, "done", 0)
        checkpoint.assert_not_called()
        self.assertEqual(result["checkpoint_commit"], sha)
        self.assertEqual(worktree.head(wt), sha)
        self.assertTrue((wt / "late-human-edit.py").exists())

    def test_checkpoint_records_a_legacy_agent_commit(self):
        """Older runs committed themselves. Checkpoint still points at HEAD."""
        wt = self.make_run(81, "done", commit=True)
        run = self.run_row(81)
        run["base_commit"] = self.git("rev-parse", "main")

        sha = supervise._checkpoint_commit(run, "done")

        self.assertEqual(sha, worktree.head(wt))
        self.assertNotEqual(sha, run["base_commit"])

        clean = self.make_run(82, "done", commit=False)
        clean_run = self.run_row(82)
        clean_run["base_commit"] = worktree.head(clean)
        self.assertEqual(supervise._checkpoint_commit(clean_run, "done"),
                         clean_run["base_commit"],
                         "an unchanged HEAD is the durable checkpoint receipt")

    def test_uncommitted_work_is_kept_and_reported_at_finalization(self):
        wt = self.make_run(5, "failed")
        write(wt / "README.md", "half-finished\n")

        note = self.release(5, "failed")

        self.assertTrue(wt.exists())
        self.assertIn("Worktree kept", note)
        self.assertIn("1 uncommitted change(s)", note)
        self.assertIn("README.md", note)

    def test_a_directory_already_deleted_by_hand_is_pruned_from_git(self):
        wt = self.make_run(6, "done")
        subprocess.run(["rm", "-rf", str(wt)], check=True)

        report = worktree.remove(wt, self.root, branch="orchestra/run-6")

        self.assertTrue(report["removed"], report)
        self.assertNotIn("run-6", self.git("worktree", "list"))
        self.assertEqual(0, subprocess.run(
            ["git", "-C", str(self.root), "branch", "-D", "orchestra/run-6"],
            capture_output=True).returncode)

    # --- orchestra prune -------------------------------------------------------

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
        self.assertIn("1 commit(s) on orchestra/run-31 not on main", kept[str(unmerged)])

    def test_force_reports_what_it_discarded_but_never_touches_a_live_run(self):
        dirty = self.make_run(40, "failed", commit=False)
        write(dirty / "scratch.py", "unsaved\n")
        running = self.make_run(50, "running", commit=False)

        report = worktree.prune(self.con, force=True)

        self.assertFalse(dirty.exists())
        self.assertTrue(running.exists())
        entry = next(r for r in report["worktrees"] if r["workdir"] == str(dirty))
        self.assertTrue(entry["removed"])
        self.assertIn("1 uncommitted change(s)", "; ".join(entry["discarded"]))
        self.assertIn("scratch.py", "; ".join(entry["discarded"]))

    def test_prune_removes_the_project_directory_only_once_it_is_empty(self):
        wt = self.make_run(60, "done", commit=False)
        self.make_run(70, "running", commit=False)
        project_dir = wt.parent

        report = worktree.prune(self.con)
        self.assertTrue(project_dir.is_dir())  # the live run keeps it
        self.assertEqual([], report["dirs"])

        self.con.execute("UPDATE runs SET status='done' WHERE id=70")
        self.con.commit()
        report = worktree.prune(self.con)
        self.assertFalse(project_dir.exists())
        self.assertEqual([str(project_dir)], report["dirs"])


if __name__ == "__main__":
    unittest.main()
