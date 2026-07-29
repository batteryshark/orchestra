from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestra_cli import (
    containment,
    db,
    operator_broker,
    supervise,
)


def init_repo(root: Path) -> None:
    root.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-b", "main", str(root)],
        check=True,
        capture_output=True,
    )
    (root / ".gitignore").write_text(
        ".orchestra/\n.work/\n", encoding="utf-8"
    )
    (root / "baseline.txt").write_text("baseline\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(root), "add", "."],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", "baseline"],
        check=True,
        capture_output=True,
    )


class ContainmentPolicyTests(unittest.TestCase):
    def test_unrestricted_backends_fail_closed(self) -> None:
        with self.assertRaisesRegex(
            containment.ContainmentPolicyError, "only Codex"
        ):
            containment.apply_profile(
                {"name": "kimi", "backend": "opencode"},
                "operator-write",
            )

    def test_sandbox_broadening_arguments_fail_closed(self) -> None:
        with self.assertRaisesRegex(
            containment.ContainmentPolicyError, "sandbox-broadening"
        ):
            containment.apply_profile(
                {
                    "name": "codex",
                    "backend": "codex",
                    "extra_args": ["--add-dir", "/Users/example"],
                },
                "operator-write",
            )

    def test_contained_worker_never_receives_project_root_write_access(self) -> None:
        root = Path("/workspace/project")
        workdir = root / ".orchestra" / "worktrees" / "run-7"
        self.assertEqual(
            containment.additional_write_dirs(root, workdir, "operator-write"),
            [],
        )
        self.assertEqual(
            containment.additional_write_dirs(root, workdir, None),
            [str(root)],
        )

    def test_contained_run_cannot_be_resumed_outside_controller(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "cannot be resumed directly"):
            supervise.create_followup(
                mock.Mock(),
                Path("/workspace/project"),
                {
                    "containment_mode": "operator-write",
                    "id": 7,
                },
                "owner",
                "continue",
            )


class StandaloneCloneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        init_repo(self.root)

    def dispatch(self, *, requester: str = "operator:test-op"):
        with mock.patch.object(
            operator_broker.availability,
            "check_profiles",
            return_value=({}, [], []),
        ):
            return operator_broker.dispatch(
                root=self.root,
                profile_name="codex",
                mission="Implement one bounded change",
                work_item_id="ow_test",
                requester=requester,
                start_supervisor=False,
                containment_mode="operator-write",
            )

    def test_operator_workspace_has_local_git_metadata_and_integrates_by_fetch(self) -> None:
        dispatched = self.dispatch()
        workspace = Path(dispatched.workdir)
        self.assertTrue((workspace / ".git").is_dir())
        self.assertFalse((workspace / ".git").is_symlink())
        run = operator_broker.run_status(self.root, dispatched.run_id)
        self.assertEqual(run["containment_mode"], "operator-write")

        (workspace / "bounded.txt").write_text("contained\n", encoding="utf-8")
        sealed = operator_broker.seal_workspace(
            workspace,
            branch=dispatched.branch or "",
            base_head=dispatched.base_head or "",
        )
        complexity = operator_broker.measure_change(
            workspace,
            base_head=dispatched.base_head or "",
            branch=dispatched.branch or "",
        )
        self.assertEqual(sealed, complexity["head"])
        self.assertEqual(complexity["changed_paths"], ["bounded.txt"])

        head = operator_broker.integrate(
            self.root,
            branch=dispatched.branch or "",
            target_branch="main",
            source_repo=workspace,
        )
        self.assertEqual(
            head,
            subprocess.run(
                ["git", "-C", str(self.root), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
        )
        self.assertEqual(
            (self.root / "bounded.txt").read_text(encoding="utf-8"),
            "contained\n",
        )
        self.assertTrue(
            operator_broker.reclaim_integrated(
                self.root,
                run_id=dispatched.run_id,
                branch=dispatched.branch or "",
                target_branch="main",
            )
        )
        self.assertFalse(workspace.exists())

    def test_seal_caps_changes_already_committed_by_worker(self) -> None:
        dispatched = self.dispatch()
        workspace = Path(dispatched.workdir)
        (workspace / "large.txt").write_text("too large\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(workspace), "add", "large.txt"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(workspace), "commit", "-m", "worker commit"],
            check=True,
            capture_output=True,
        )
        with self.assertRaisesRegex(
            operator_broker.ContainmentError, "exceeds 1 bytes"
        ):
            operator_broker.seal_workspace(
                workspace,
                branch=dispatched.branch or "",
                base_head=dispatched.base_head or "",
                max_uncommitted_bytes=1,
            )

    def test_readonly_workspace_modification_is_a_runtime_violation(self) -> None:
        with mock.patch.object(
            operator_broker.availability,
            "check_profiles",
            return_value=({}, [], []),
        ):
            dispatched = operator_broker.dispatch(
                root=self.root,
                profile_name="codex",
                mission="Review only",
                work_item_id="ow_test",
                requester="operator:op-read",
                start_supervisor=False,
                containment_mode="operator-read",
            )
        (Path(dispatched.workdir) / "forbidden.txt").write_text(
            "modified\n", encoding="utf-8"
        )
        violations = operator_broker.operation_containment_violations(
            self.root, "op-read"
        )
        self.assertIn(
            "readonly_workspace_modified",
            {row["kind"] for row in violations},
        )

    def test_undeclared_child_is_detected_and_stopped(self) -> None:
        dispatched = self.dispatch(requester="operator:op-test")
        con = db.connect(self.root)
        try:
            con.execute(
                "UPDATE runs SET status='running' WHERE id=?",
                (dispatched.run_id,),
            )
            child = con.execute(
                "INSERT INTO runs(agent,backend,title,requested_by,workdir,status,"
                "lead_run,started_at) VALUES('codex','codex','child','codex',?,"
                "'running',?,?)",
                (dispatched.workdir, dispatched.run_id, db.now()),
            ).lastrowid
            con.commit()
        finally:
            con.close()

        violations = operator_broker.operation_containment_violations(
            self.root, "op-test"
        )
        self.assertIn("undeclared_child_run", {row["kind"] for row in violations})
        stopped = operator_broker.stop_operation_run_tree(self.root, "op-test")
        self.assertEqual(set(stopped), {dispatched.run_id, child})
        con = db.connect(self.root)
        try:
            states = {
                row["id"]: row["status"]
                for row in con.execute(
                    "SELECT id,status FROM runs WHERE id IN (?,?)",
                    (dispatched.run_id, child),
                )
            }
        finally:
            con.close()
        self.assertEqual(set(states.values()), {"interrupt"})

    def test_escape_link_created_during_run_is_detected(self) -> None:
        dispatched = self.dispatch(requester="operator:op-links")
        workspace = Path(dispatched.workdir)
        (workspace / "outside").symlink_to(self.root, target_is_directory=True)
        violations = operator_broker.operation_containment_violations(
            self.root, "op-links"
        )
        self.assertIn("worktree_escape", {row["kind"] for row in violations})

    def test_retry_clone_preserves_predecessor_before_reclaim(self) -> None:
        first = self.dispatch()
        first_workspace = Path(first.workdir)
        (first_workspace / "attempt.txt").write_text(
            "first\n", encoding="utf-8"
        )
        subprocess.run(
            ["git", "-C", str(first_workspace), "add", "attempt.txt"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(first_workspace), "commit", "-m", "attempt"],
            check=True,
            capture_output=True,
        )
        with mock.patch.object(
            operator_broker.availability,
            "check_profiles",
            return_value=({}, [], []),
        ):
            second = operator_broker.dispatch(
                root=self.root,
                profile_name="codex",
                mission="Continue the bounded attempt",
                work_item_id="ow_test",
                requester="operator:test-op",
                start_supervisor=False,
                start_point=first.branch,
                start_repo=first_workspace,
                comparison_base=first.base_head,
                containment_mode="operator-write",
            )
        self.assertEqual(
            (Path(second.workdir) / "attempt.txt").read_text(encoding="utf-8"),
            "first\n",
        )
        self.assertTrue(
            operator_broker.reclaim_transferred_worktree(
                self.root,
                run_id=first.run_id,
                branch=first.branch or "",
                successor_branch=second.branch or "",
                successor_repo=Path(second.workdir),
            )
        )
        self.assertFalse(first_workspace.exists())

    def test_integration_uses_verified_commit_not_later_branch_head(self) -> None:
        dispatched = self.dispatch()
        workspace = Path(dispatched.workdir)
        (workspace / "reviewed.txt").write_text("reviewed\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(workspace), "add", "reviewed.txt"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(workspace), "commit", "-m", "reviewed"],
            check=True,
            capture_output=True,
        )
        verified = operator_broker.measure_change(
            workspace,
            base_head=dispatched.base_head or "",
            branch=dispatched.branch or "",
        )
        (workspace / "unreviewed.txt").write_text(
            "unreviewed\n", encoding="utf-8"
        )
        subprocess.run(
            ["git", "-C", str(workspace), "add", "unreviewed.txt"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(workspace), "commit", "-m", "unreviewed"],
            check=True,
            capture_output=True,
        )
        operator_broker.integrate(
            self.root,
            branch=verified["head"],
            target_branch="main",
            source_repo=workspace,
        )
        self.assertTrue((self.root / "reviewed.txt").is_file())
        self.assertFalse((self.root / "unreviewed.txt").exists())
        with self.assertRaisesRegex(
            operator_broker.BrokerError, "unique, unintegrated"
        ):
            operator_broker.reclaim_integrated(
                self.root,
                run_id=dispatched.run_id,
                branch=dispatched.branch or "",
                target_branch="main",
            )


class ResidualProcessTests(unittest.TestCase):
    def test_workspace_size_measurement_counts_ignored_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory)
            (workdir / "ignored.bin").write_bytes(b"x" * 8192)
            measured = supervise._workspace_size_bytes(str(workdir))
        self.assertIsNotNone(measured)
        self.assertGreaterEqual(measured or 0, 8192)

    def test_contained_run_terminates_background_process_group(self) -> None:
        calls: list[tuple[int, int]] = []
        alive = iter([None, None, ProcessLookupError()])

        def killpg(group: int, signal_number: int) -> None:
            calls.append((group, signal_number))
            outcome = next(alive)
            if isinstance(outcome, BaseException):
                raise outcome

        with mock.patch.object(supervise.os, "killpg", side_effect=killpg):
            self.assertTrue(
                supervise._terminate_residual_process_group(
                    8123, grace_seconds=0.1
                )
            )
        self.assertIn((8123, 0), calls)
        self.assertIn((8123, supervise.signal.SIGTERM), calls)


if __name__ == "__main__":
    unittest.main()
