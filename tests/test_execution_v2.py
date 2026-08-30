import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestra import (attention, child_runs, daemon, db, fleet_config,
                       groups, messaging, runs, scheduler, supervise, traces,
                       worktree)
from orchestra.contracts import RunRequest


class ExecutionV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.workspace_root = self.root / "workspace"
        self.workspace_root.mkdir()
        self.env = patch.dict(os.environ, {"ORCHESTRA_HOME": str(self.state)})
        self.env.start()
        self.con = db.connect()

    def tearDown(self):
        self.con.close()
        self.env.stop()
        self.temp.cleanup()

    def configure(self, source: str, *, root=None):
        slug = f"exec-{self.con.execute('SELECT COUNT(*) FROM runtimes').fetchone()[0]}"
        runtime = fleet_config.create_runtime(
            self.con, slug, "exec", slug=slug,
            command=[sys.executable, "-c", source])
        profile = fleet_config.create_profile(
            self.con, slug, runtime["runtime_id"], slug=slug, tier=2,
            timeout_seconds=20)
        group = groups.create(
            self.con, slug, slug=slug, cwd=str(root or self.workspace_root))
        request = lambda name, **extra: RunRequest.from_mapping({
            "request_id": name, "group": group["slug"],
            "profile": profile["slug"], "context": "Do the work", **extra})
        return profile, group, request

    def run_started(self, run_id: int) -> int:
        admitted = scheduler.admit(self.con)["admitted"]
        self.assertIn(run_id, admitted)
        return supervise.supervise(self.workspace_root, run_id)

    def wait_status(self, run_id: int, wanted: str, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            row = runs.find(self.con, run_id)
            if row["status"] == wanted:
                return row
            time.sleep(0.02)
        self.fail(f"run {run_id} never became {wanted}")

    def test_custom_argv_runtime_completes_and_captures_usage_fields(self):
        _, _, request = self.configure(
            "import sys; sys.stdin.read(); print('finished', flush=True)")
        run, _ = runs.submit(self.con, request("complete"))
        self.assertEqual(self.run_started(run["id"]), 0)
        result = runs.find(self.con, run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertIsNotNone(result["finished_at"])
        self.assertIsNone(result["run_token_hash"])

    def test_worker_environment_gets_only_the_scoped_orchestra_identity(self):
        source = (
            "import json,os,sys; sys.stdin.read(); print(json.dumps({"
            "'run': bool(os.environ.get('ORCHESTRA_RUN_TOKEN'))," 
            "'id': os.environ.get('ORCHESTRA_RUN_ID'),"
            "'url': os.environ.get('ORCHESTRA_URL'),"
            "'broad': os.environ.get('ORCHESTRA_TOKEN'),"
            "'home': os.environ.get('ORCHESTRA_HOME')}))")
        _, _, request = self.configure(source)
        run, _ = runs.submit(self.con, request("scoped-environment"))
        with patch.dict(os.environ, {
            "ORCHESTRA_TOKEN": "operator-must-not-leak",
            "ORCHESTRA_RUN_TOKEN": "stale-run-token",
        }, clear=False):
            self.assertEqual(self.run_started(run["id"]), 0)
        text = Path(runs.find(self.con, run["id"])["log_path"]).read_text()
        self.assertIn('"run": true', text)
        self.assertIn(f'"id": "{run["id"]}"', text)
        self.assertIn('"broad": null', text)
        self.assertIn('"home": null', text)
        self.assertNotIn("operator-must-not-leak", text)
        self.assertNotIn("stale-run-token", text)

    def test_group_cwd_edit_does_not_move_an_admitted_run(self):
        admitted_root = self.root / "admitted-root"
        replacement_root = self.root / "replacement-root"
        admitted_root.mkdir()
        replacement_root.mkdir()
        source = "import pathlib,sys; sys.stdin.read(); pathlib.Path('ran').touch()"
        _, group, request = self.configure(source, root=admitted_root)
        run, _ = runs.submit(self.con, request("frozen-root"))
        groups.set_cwd(self.con, group["group_id"], str(replacement_root))

        self.assertEqual(scheduler.admit(self.con)["admitted"], [run["id"]])
        launch_roots = []
        self.assertTrue(daemon._launch(
            self.con, run["id"],
            lambda root, run_id: launch_roots.append((root, run_id))))
        self.assertEqual(launch_roots,
                         [(admitted_root.resolve(), run["id"])])
        self.assertEqual(supervise.supervise(self.workspace_root, run["id"]), 0)

        result = runs.find(self.con, run["id"])
        self.assertEqual(result["workdir"], str(admitted_root.resolve()))
        self.assertIsNone(result["repo"])
        self.assertTrue((admitted_root / "ran").exists())
        self.assertFalse((replacement_root / "ran").exists())

    def test_unreadable_workdir_fails_with_access_remediation(self):
        denied_root = self.root / "denied-root"
        denied_root.mkdir()
        _, _, request = self.configure(
            "import sys; sys.stdin.read()", root=denied_root)
        run, _ = runs.submit(self.con, request("denied"))
        denied_root.chmod(0o000)
        self.assertEqual(self.run_started(run["id"]), 1)
        result = runs.find(self.con, run["id"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("denied read access", result["summary"])
        self.assertIn(str(denied_root.resolve()), result["summary"])

    def test_interrupt_resumes_same_run_and_audits_replay_risk(self):
        source = (
            "import sys,time; p=sys.stdin.read(); print('started', flush=True); "
            "sys.exit(0) if 'change direction' in p else time.sleep(30)")
        _, _, request = self.configure(source)
        run, _ = runs.submit(self.con, request("interrupt"))
        scheduler.admit(self.con)
        result = []
        worker = threading.Thread(
            target=lambda: result.append(
                supervise.supervise(self.workspace_root, int(run["id"]))))
        worker.start()
        self.wait_status(run["id"], "running")
        supervise.interrupt(
            self.con, run["id"], "change direction", actor="test")
        worker.join(timeout=8)
        self.assertFalse(worker.is_alive())
        self.assertEqual(runs.find(self.con, run["id"])["status"], "completed")
        audits = self.con.execute(
            "SELECT * FROM supervision_events WHERE run_id=? AND action='replay_risk'",
            (run["id"],)).fetchall()
        self.assertEqual(len(audits), 1)
        self.assertEqual(result, [0])

    def test_blocking_attention_releases_process_and_answer_resumes(self):
        source = (
            "import sys,time; p=sys.stdin.read(); "
            "print(chr(123)+'\"session_id\":\"session-1\"'+chr(125), flush=True); "
            "sys.exit(0) if 'approved' in p else time.sleep(30)")
        _, _, request = self.configure(source)
        run, _ = runs.submit(self.con, request("question"))
        scheduler.admit(self.con)
        worker = threading.Thread(
            target=supervise.supervise, args=(self.workspace_root, int(run["id"])))
        worker.start()
        self.wait_status(run["id"], "running")
        card, _ = attention.open_request(
            self.con, kind="question", run_id=run["id"], blocking=True,
            title="Proceed?", body="May I proceed?", created_by="run")
        worker.join(timeout=8)
        waiting = self.wait_status(run["id"], "waiting")
        self.assertEqual(waiting["waiting_kind"], "input")
        self.assertIsNone(waiting["pid"])
        attention.answer(
            self.con, card["id"], actor="test",
            response={"body": "approved"}, authorized=True)
        launched = []
        daemon.tick(self.con, launcher=lambda root, run_id: launched.append(run_id))
        self.assertIn(run["id"], launched)
        self.assertEqual(supervise.supervise(self.workspace_root, run["id"]), 0)
        self.assertEqual(runs.find(self.con, run["id"])["status"], "completed")

    def test_parent_waits_for_children_then_resumes_same_number(self):
        source = (
            "import sys; p=sys.stdin.read(); "
            "print(chr(123)+'\"session_id\":\"parent-session\"'+chr(125), flush=True)")
        profile, group, request = self.configure(source)
        parent, _ = runs.submit(self.con, request("parent"))
        child, _ = runs.submit(self.con, request(
            "child", parent_run_id=parent["id"]))
        self.con.execute("UPDATE runs SET status='starting' WHERE id=?",
                         (parent["id"],))
        self.con.commit()
        self.assertEqual(supervise.supervise(self.workspace_root, parent["id"]), 0)
        waiting = runs.find(self.con, parent["id"])
        self.assertEqual((waiting["status"], waiting["waiting_kind"]),
                         ("waiting", "children"))
        supervise.finalize_run(
            self.con, child, "completed", 0, summary="Child result")
        launched = []
        daemon.tick(self.con, launcher=lambda root, run_id: launched.append(run_id))
        self.assertIn(parent["id"], launched)
        self.assertEqual(supervise.supervise(self.workspace_root, parent["id"]), 0)
        result = runs.find(self.con, parent["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["group_seq"], parent["group_seq"])

    def test_settled_children_are_delivered_before_parent_finishes(self):
        source = "import sys; sys.stdin.read()"
        _, _, request = self.configure(source)
        parent, _ = runs.submit(self.con, request("settled-parent"))
        child, _ = runs.submit(self.con, request(
            "settled-child", parent_run_id=parent["id"]))
        supervise.finalize_run(
            self.con, child, "completed", 0, summary="Already settled")
        self.con.execute("UPDATE runs SET status='starting' WHERE id=?",
                         (parent["id"],))
        self.con.commit()

        self.assertEqual(supervise.supervise(self.workspace_root, parent["id"]), 0)
        waiting = runs.find(self.con, parent["id"])
        self.assertEqual((waiting["status"], waiting["waiting_kind"]),
                         ("waiting", "children"))
        launched = []
        daemon.tick(self.con, launcher=lambda root, run_id: launched.append(run_id))
        self.assertEqual(launched, [parent["id"]])
        self.assertEqual(supervise.supervise(self.workspace_root, parent["id"]), 0)
        self.assertEqual(runs.find(self.con, parent["id"])["status"], "completed")

    def test_each_child_wave_gets_one_result_delivery(self):
        _, _, request = self.configure("import sys; sys.stdin.read()")
        parent, _ = runs.submit(self.con, request("waves-parent"))
        first, _ = runs.submit(self.con, request(
            "waves-child-1", parent_run_id=parent["id"]))
        self.con.execute("UPDATE runs SET status='running' WHERE id=?",
                         (parent["id"],))
        self.con.execute("UPDATE runs SET status='completed' WHERE id=?",
                         (first["id"],))
        self.con.commit()
        generation = child_runs.result_generation(self.con, parent["id"])
        messaging.post(
            self.con, parent["id"], direction="inbound", sender="orchestra",
            body="first results", kind="child_results", status="delivered",
            correlation_id=f"children:{parent['id']}:{generation}")
        self.assertFalse(supervise._wait_for_children(
            self.con, runs.find(self.con, parent["id"]), 0))

        second, _ = runs.submit(self.con, request(
            "waves-child-2", parent_run_id=parent["id"]))
        self.con.execute("UPDATE runs SET status='completed' WHERE id=?",
                         (second["id"],))
        self.con.commit()
        self.assertTrue(supervise._wait_for_children(
            self.con, runs.find(self.con, parent["id"]), 0))
        daemon.tick(self.con, launcher=lambda root, run_id: None)
        messages = self.con.execute(
            "SELECT correlation_id FROM messages WHERE run_id=? "
            "AND kind='child_results' ORDER BY id", (parent["id"],)
        ).fetchall()
        self.assertEqual(len(messages), 2)
        self.assertNotEqual(messages[0]["correlation_id"],
                            messages[1]["correlation_id"])

    def test_transient_failure_creates_one_separately_numbered_retry(self):
        _, _, request = self.configure(
            "import sys; sys.stdin.read(); "
            "print('worker ERROR connection reset', file=sys.stderr); sys.exit(1)")
        run, _ = runs.submit(self.con, request("transient"))
        self.assertEqual(self.run_started(run["id"]), 1)
        retry = self.con.execute(
            "SELECT * FROM runs WHERE retry_of_run_id=?", (run["id"],)
        ).fetchone()
        self.assertIsNotNone(retry)
        self.assertEqual(retry["group_seq"], run["group_seq"] + 1)
        self.assertEqual(retry["status"], "queued")

    def _merge_repo(self):
        repo = self.root / "merge-repo"
        repo.mkdir()
        git = ["git", "-C", str(repo), "-c", "user.name=Test",
               "-c", "user.email=test@example.invalid"]
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "file.txt").write_text("before\n", encoding="utf-8")
        subprocess.run([*git, "add", "file.txt"], check=True)
        subprocess.run([*git, "commit", "-qm", "initial"], check=True)
        subprocess.run([*git, "branch", "orchestra/run-9"], check=True)
        subprocess.run([*git, "worktree", "add", "-q",
                        str(self.root / "merge-wt"), "orchestra/run-9"], check=True)
        (self.root / "merge-wt" / "file.txt").write_text("after\n")
        subprocess.run(["git", "-C", str(self.root / "merge-wt"),
                        "-c", "user.name=Test", "-c",
                        "user.email=test@example.invalid",
                        "commit", "-aqm", "run work"], check=True)
        return repo, git

    def test_merge_into_owner_lands_a_clean_branch_and_refuses_conflicts(self):
        repo, git = self._merge_repo()
        (repo / "file.txt").write_text("local edit\n")
        with self.assertRaisesRegex(RuntimeError, "uncommitted"):
            worktree.merge_into_owner(repo, "orchestra/run-9")
        (repo / "file.txt").write_text("before\n")
        result = worktree.merge_into_owner(repo, "orchestra/run-9")
        self.assertTrue(result["merged"])
        self.assertEqual((repo / "file.txt").read_text(), "after\n")
        self.assertTrue(worktree.branch_merged(repo, "orchestra/run-9"))
        with self.assertRaisesRegex(RuntimeError, "already merged"):
            worktree.merge_into_owner(repo, "orchestra/run-9")
        with self.assertRaisesRegex(RuntimeError, "does not exist"):
            worktree.merge_into_owner(repo, "orchestra/run-404")

    def test_merge_into_owner_aborts_a_conflicted_merge(self):
        repo, git = self._merge_repo()
        (repo / "file.txt").write_text("diverged\n")
        subprocess.run([*git, "commit", "-aqm", "diverge"], check=True)
        with self.assertRaisesRegex(RuntimeError, "merge refused"):
            worktree.merge_into_owner(repo, "orchestra/run-9")
        self.assertEqual((repo / "file.txt").read_text(), "diverged\n")
        self.assertEqual(worktree.status(repo).strip(), "")

    def test_managed_workspace_never_rides_an_enclosing_repo(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        slug = "managed-ws"
        runtime = fleet_config.create_runtime(
            self.con, slug, "exec", slug=slug,
            command=[sys.executable, "-c", "import sys; sys.stdin.read()"])
        profile = fleet_config.create_profile(
            self.con, slug, runtime["runtime_id"], slug=slug, tier=2,
            timeout_seconds=20)
        group = groups.create(self.con, slug, slug=slug)
        run, _ = runs.submit(self.con, RunRequest.from_mapping({
            "request_id": "managed-ws", "group": group["slug"],
            "profile": profile["slug"], "context": "Do the work"}))
        self.assertEqual(self.run_started(run["id"]), 0)
        result = runs.find(self.con, run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertIsNone(result["branch"])
        listing = subprocess.run(
            ["git", "-C", str(self.root), "worktree", "list"],
            capture_output=True, text=True)
        self.assertNotIn("orchestra", listing.stdout)

    def test_worktree_branch_steps_past_a_prior_generation_leftover(self):
        repo = self.root / "legacy-repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "file.txt").write_text("before\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "file.txt"], check=True)
        subprocess.run([
            "git", "-C", str(repo), "-c", "user.name=Test",
            "-c", "user.email=test@example.invalid", "commit", "-qm", "initial"
        ], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "branch", "orchestra/run-77"], check=True)
        wt, branch = worktree.create(repo, 77, "legacy")
        self.assertEqual(branch, "orchestra/run-77-2")
        self.assertEqual(wt.name, "run-77-2")
        self.assertTrue(wt.is_dir())
        self.assertIsNotNone(worktree.RUN_DIR_RE.match(wt.name))

    def test_isolated_git_run_checkpoints_patch_and_leaves_owner_checkout(self):
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "file.txt").write_text("before\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "file.txt"], check=True)
        subprocess.run([
            "git", "-C", str(repo), "-c", "user.name=Test",
            "-c", "user.email=test@example.invalid", "commit", "-qm", "initial"
        ], check=True)
        source = (
            "import pathlib,sys; sys.stdin.read(); "
            "pathlib.Path('file.txt').write_text('after\\n')")
        _, _, request = self.configure(source, root=repo)
        run, _ = runs.submit(self.con, request("git"))
        self.assertEqual(self.run_started(run["id"]), 0)
        result = runs.find(self.con, run["id"])
        self.assertEqual((repo / "file.txt").read_text(), "before\n")
        self.assertTrue(result["branch"].startswith("orchestra/run-"))
        self.assertIsNotNone(result["checkpoint_commit"])
        self.assertIsNotNone(result["head_commit"])
        self.assertTrue(Path(result["diff_path"]).is_file())
        self.assertIn("after", Path(result["diff_path"]).read_text())
        self.assertFalse(Path(result["workdir"]).exists())
        branch = subprocess.run(
            ["git", "-C", str(repo), "show-ref", "--verify", "--quiet",
             f"refs/heads/{result['branch']}"])
        self.assertEqual(branch.returncode, 0)

    def test_stop_and_stop_tree_are_distinct(self):
        _, _, request = self.configure("import sys; sys.stdin.read()")
        parent, _ = runs.submit(self.con, request("stop-parent"))
        child, _ = runs.submit(self.con, request(
            "stop-child", parent_run_id=parent["id"]))
        supervise.stop(self.con, parent["id"], actor="test")
        self.assertEqual(runs.find(self.con, parent["id"])["status"], "stopped")
        self.assertEqual(runs.find(self.con, child["id"])["status"], "queued")
        supervise.stop(self.con, child["id"], tree=True, actor="test")
        self.assertEqual(runs.find(self.con, child["id"])["status"], "stopped")

    def test_controls_return_their_audit_receipts(self):
        _, _, request = self.configure("import sys; sys.stdin.read()")
        run, _ = runs.submit(self.con, request("controls"))
        told = supervise.tell(self.con, run["id"], "note", "test")
        interrupted = supervise.interrupt(
            self.con, run["id"], "change", "test")
        checked = supervise.check(self.con, run["id"], "test")
        stopped = supervise.stop(self.con, run["id"], "test")
        self.assertEqual(interrupted["resume_mode"], "pending_session_capture")
        for result in (told, interrupted, checked, stopped):
            self.assertIsInstance(result["control_audit_id"], int)
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM control_events WHERE target_id=? AND "
            "action IN ('run.tell','run.interrupt','run.check','run.stop')",
            (str(run["id"]),)).fetchone()[0], 4)

    def test_stop_tree_includes_retry_and_continuation_descendants(self):
        _, _, request = self.configure("import sys; sys.stdin.read()")
        parent, _ = runs.submit(self.con, request("tree-root"))
        child, _ = runs.submit(self.con, request(
            "tree-child", parent_run_id=parent["id"]))
        supervise.finalize_run(self.con, child, "failed", 1, summary="failed")
        retried, _ = runs.clone(
            self.con, child["id"], request_id="tree-retry", kind="retry",
            requested_by="test")
        supervise.stop(self.con, parent["id"], "test", tree=True)
        self.assertEqual(runs.find(self.con, retried["id"])["status"], "stopped")

    def test_trace_drain_consumes_bursts_larger_than_one_chunk(self):
        _, _, request = self.configure("import sys; sys.stdin.read()")
        run, _ = runs.submit(self.con, request("trace-burst"))
        line = b"x" * 1_000_000 + b"\n"
        Path(run["log_path"]).write_bytes(line * 5)
        result = traces.drain(
            self.con, run["id"], run["log_path"], "codex")
        self.assertGreater(result["offset"], traces.MAX_CHUNK)
        self.assertEqual(result["offset"], Path(run["log_path"]).stat().st_size)

    def test_v2_prune_releases_terminal_checkout_but_keeps_branch(self):
        repo = self.root / "prune-repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "file.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "file.txt"], check=True)
        subprocess.run([
            "git", "-C", str(repo), "-c", "user.name=Test",
            "-c", "user.email=test@example.invalid", "commit", "-qm", "base"
        ], check=True)
        _, group, request = self.configure(
            "import sys; sys.stdin.read()", root=repo)
        run, _ = runs.submit(self.con, request("prune"))
        self.con.execute(
            "UPDATE runs SET status='completed',branch=?,repo=? WHERE id=?",
            (f"orchestra/run-{run['id']}", str(repo), run["id"]))
        branch = f"orchestra/run-{run['id']}"
        subprocess.run(
            ["git", "-C", str(repo), "branch", branch], check=True)
        restored = worktree.restore(
            repo, run["id"], group["slug"], branch, "exec")
        self.con.execute("UPDATE runs SET workdir=? WHERE id=?",
                         (str(restored), run["id"]))
        self.con.commit()

        report = worktree.prune(self.con)

        self.assertFalse(restored.exists())
        self.assertTrue(any(item["removed"] for item in report["worktrees"]))
        self.assertEqual(subprocess.run(
            ["git", "-C", str(repo), "show-ref", "--verify", "--quiet",
             f"refs/heads/{branch}"]).returncode, 0)


if __name__ == "__main__":
    unittest.main()
