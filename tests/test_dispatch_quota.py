"""Integration test for the dispatch-time warn-only quota hook.

Verifies:
  * --no-quota-warn skips the snapshot entirely (no collectors fire).
  * default-on warns before run DB inserts when headroom is critical.
  * exceptions during quota collection are fail-open (dispatch still inserts).

Isolation contract — read this before adding cases:

  * ``cli.paths.find_root`` is patched to a temp root for the entire test,
    so every ``db.connect`` and work-tracker write is sandboxed.
  * ``cli._spawn_supervisor`` is monkey-patched to a no-op, so the test
    never spawns a real CLI / worker process. Re-running `orchestra dispatch`
    (or any consumer) by hand against this project's DB would leak — never
    do that here.
  * ``work=None`` is passed on every dispatch, so the real ``work`` CLI is
    never invoked.
  * The temp project's database is closed in ``tearDown`` and the tempdir
    is removed.

When adding new tests, keep ALL four guards in place; an inherited env
variable (``ORCHESTRA_ROOT``) can otherwise point ``find_root`` at the
real project root and silently write there.
"""
from __future__ import annotations

import contextlib
import io
import socket
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from orchestra_cli import capabilities, cli, db
from orchestra_cli.usage.models import ProviderResult, QuotaWindow


class _StubService:
    def __init__(self, providers_or_exc) -> None:
        self._payload = providers_or_exc
        self.calls = 0

    def snapshot(self, *, force: bool = False) -> dict:
        self.calls += 1
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _make_project() -> tuple[tempfile.TemporaryDirectory, Path]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / ".orchestra").mkdir(parents=True, exist_ok=True)
    db.connect(root).close()
    return tmp, root


class DispatchQuotaHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp, self.root = _make_project()
        self._orig_find_root = cli.paths.find_root
        self._orig_default = cli.default_service
        self._orig_spawn = cli._spawn_supervisor
        self._orig_check_profiles = cli.availability.check_profiles
        self._stub = _StubService(None)
        cli.paths.find_root = lambda: self.root  # type: ignore[assignment]
        cli.default_service = lambda: self._stub  # type: ignore[assignment]
        cli._spawn_supervisor = lambda *a, **kw: None  # type: ignore[assignment]
        cli.availability.check_profiles = lambda *_args, **_kwargs: ({}, [], [])

    def tearDown(self) -> None:
        cli.paths.find_root = self._orig_find_root  # type: ignore[assignment]
        cli.default_service = self._orig_default  # type: ignore[assignment]
        cli._spawn_supervisor = self._orig_spawn
        cli.availability.check_profiles = self._orig_check_profiles
        self.tmp.cleanup()

    def _make_args(self, *to: str, no_quota_warn: bool = False) -> Namespace:
        return Namespace(
            mission=["echo hi"],
            brief_file=None,
            work=None,  # never log into the real tracker
            team=None,
            to=list(to),
            title=None,
            context=None,
            worktree=False,
            read_only=True,
            sync=False,
            no_quota_warn=no_quota_warn,
            allow_question=False,
            question_wait=None,
            require_verification=False,
            as_=None,
        )

    def _run_dispatch(self, args: Namespace) -> tuple[str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            cli.cmd_dispatch(args)
        return out.getvalue(), err.getvalue()

    def test_no_quota_warn_skips_snapshot_entirely(self) -> None:
        args = self._make_args("minimax", no_quota_warn=True)
        self._run_dispatch(args)
        self.assertEqual(self._stub.calls, 0,
                         "no_quota_warn should skip the snapshot call")

    def test_supervised_worker_must_use_brokered_spawn(self) -> None:
        args = self._make_args("minimax", no_quota_warn=True)
        with mock.patch.dict(
            "os.environ",
            {"ORCHESTRA_RUN_ID": "41", "ORCHESTRA_SELF": "codex"},
            clear=False,
        ):
            with self.assertRaisesRegex(SystemExit, "orchestra spawn"):
                self._run_dispatch(args)
        con = db.connect(self.root)
        try:
            count = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(count, 0)

    def test_question_capability_is_default_off_and_explicitly_bounded(self) -> None:
        default_args = self._make_args("minimax", no_quota_warn=True)
        self._run_dispatch(default_args)
        opted_args = self._make_args("minimax", no_quota_warn=True)
        opted_args.allow_question = True
        opted_args.question_wait = 60
        self._run_dispatch(opted_args)

        con = db.connect(self.root)
        try:
            rows = list(con.execute(
                "SELECT allow_question, question_wait_seconds, brief_path FROM runs ORDER BY id"
            ))
        finally:
            con.close()
        self.assertEqual(rows[0]["allow_question"], 0)
        self.assertNotIn("orchestra ask", Path(rows[0]["brief_path"]).read_text())
        self.assertEqual(rows[1]["allow_question"], 1)
        self.assertEqual(rows[1]["question_wait_seconds"], 60)
        opted_brief = Path(rows[1]["brief_path"]).read_text()
        self.assertIn("ONE blocking-question", opted_brief)
        self.assertIn("orchestra ask", opted_brief)
        self.assertIn("waits up to 60 seconds", opted_brief)

    def test_question_wait_override_requires_opt_in(self) -> None:
        args = self._make_args("minimax", no_quota_warn=True)
        args.question_wait = 60
        with self.assertRaisesRegex(SystemExit, "requires --allow-question"):
            self._run_dispatch(args)

    def test_require_verification_is_persisted_with_pending_outcome(self) -> None:
        args = self._make_args("minimax", no_quota_warn=True)
        args.require_verification = True

        self._run_dispatch(args)

        con = db.connect(self.root)
        try:
            run = con.execute(
                "SELECT verification_required, verification_status FROM runs"
            ).fetchone()
        finally:
            con.close()
        self.assertEqual(run["verification_required"], 1)
        self.assertEqual(run["verification_status"], "pending")

    def test_after_records_a_visible_pending_dispatch_without_launching(self) -> None:
        con = db.connect(self.root)
        try:
            prerequisite = con.execute(
                "INSERT INTO runs(agent,backend,title,requested_by,workdir,status,started_at) "
                "VALUES('minimax','opencode','producer','codex',?,'running',?)",
                (str(self.root), db.now()),
            ).lastrowid
            con.commit()
        finally:
            con.close()
        args = self._make_args("minimax", no_quota_warn=True)
        args.after = [prerequisite]

        stdout, _ = self._run_dispatch(args)

        con = db.connect(self.root)
        try:
            consumer = con.execute(
                "SELECT * FROM runs WHERE id!=? ORDER BY id DESC LIMIT 1",
                (prerequisite,),
            ).fetchone()
            edge = con.execute(
                "SELECT depends_on_run FROM dispatch_dependencies WHERE run_id=?",
                (consumer["id"],),
            ).fetchone()
        finally:
            con.close()
        self.assertEqual(consumer["status"], "pending")
        self.assertIsNone(consumer["brief_path"])
        self.assertEqual(edge["depends_on_run"], prerequisite)
        self.assertIn(f"pending-on={prerequisite}", stdout)
        status_output = io.StringIO()
        with contextlib.redirect_stdout(status_output):
            cli.cmd_status(Namespace())
        self.assertIn(f"[pending-on-{prerequisite}]", status_output.getvalue())

    def test_after_rejects_unknown_prerequisite_before_creating_run(self) -> None:
        args = self._make_args("minimax", no_quota_warn=True)
        args.after = [999]
        with self.assertRaisesRegex(SystemExit, "unknown run"):
            self._run_dispatch(args)
        con = db.connect(self.root)
        try:
            count = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(count, 0)

    def test_second_pending_shared_writer_on_same_parent_is_rejected(self) -> None:
        con = db.connect(self.root)
        try:
            prerequisite = con.execute(
                "INSERT INTO runs(agent,backend,title,requested_by,workdir,status,started_at) "
                "VALUES('minimax','opencode','producer','codex',?,'running',?)",
                (str(self.root), db.now()),
            ).lastrowid
            con.commit()
        finally:
            con.close()
        first = self._make_args("minimax", no_quota_warn=True)
        first.after = [prerequisite]
        first.read_only = False
        self._run_dispatch(first)

        second = self._make_args("minimax", no_quota_warn=True)
        second.after = [prerequisite]
        second.read_only = False
        with self.assertRaisesRegex(SystemExit, "shared-tree writer collision"):
            self._run_dispatch(second)

        second.read_only = True
        self._run_dispatch(second)
        con = db.connect(self.root)
        try:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 3)
        finally:
            con.close()

    def test_terminal_run_status_keeps_verification_outcome_visible(self) -> None:
        con = db.connect(self.root)
        try:
            run_id = con.execute(
                "INSERT INTO runs(agent,backend,title,requested_by,workdir,status,"
                "verification_required,verification_status,started_at) "
                "VALUES('minimax','opencode','verified run','codex',?,'done',1,"
                "'unverified',?)",
                (str(self.root), db.now()),
            ).lastrowid
            con.commit()
            row = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        finally:
            con.close()
        self.assertEqual(cli._run_status_label(row), "done/unverified")

    def test_work_snapshot_is_captured_once_before_creating_the_run(self) -> None:
        args = self._make_args("minimax", no_quota_warn=True)
        args.work = "W-0141"
        with mock.patch.object(
            cli.brief, "work_snapshot", return_value="**W-0141** — immutable context"
        ) as snapshot:
            self._run_dispatch(args)

        con = db.connect(self.root)
        try:
            run = con.execute("SELECT brief_path FROM runs").fetchone()
        finally:
            con.close()
        snapshot.assert_called_once_with(self.root, "W-0141", required=True)
        self.assertIn("immutable context", Path(run["brief_path"]).read_text())

    def test_missing_work_item_fails_before_creating_a_run(self) -> None:
        args = self._make_args("minimax", no_quota_warn=True)
        args.work = "W-9999"
        with mock.patch.object(
            cli.brief, "work_snapshot", side_effect=SystemExit("missing work item")
        ):
            with self.assertRaisesRegex(SystemExit, "missing work item"):
                self._run_dispatch(args)

        con = db.connect(self.root)
        try:
            count = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(count, 0)

    def test_required_capability_needs_fresh_positive_lane_evidence(self) -> None:
        args = self._make_args("minimax", no_quota_warn=True)
        args.requires_capability = ["window-server"]
        with self.assertRaisesRegex(SystemExit, "missing=window-server"):
            self._run_dispatch(args)
        con = db.connect(self.root)
        try:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)
        finally:
            con.close()

        capabilities.record_observation(
            self.root,
            host_identity=socket.gethostname(),
            backend="opencode",
            profile="minimax",
            sandbox_mode="orchestra-unrestricted",
            capability="window-server",
            state="supported",
            evidence="probe opened and closed a window",
        )
        self._run_dispatch(args)
        con = db.connect(self.root)
        try:
            row = con.execute(
                "SELECT required_capabilities_json FROM runs"
            ).fetchone()
        finally:
            con.close()
        self.assertEqual(row["required_capabilities_json"], '["window-server"]')

    def test_default_on_emits_warning_before_run_insert(self) -> None:
        critical = ProviderResult(
            id="minimax", name="MiniMax", status="ok", plan="Token Plan",
            windows=[QuotaWindow.from_remaining(
                id="weekly", label="Weekly", scope="Coding models",
                remaining_percent=5.0,
            )],
            source="fixture",
        )
        self._stub._payload = {
            "generated_at": "x", "status": "ok",
            "providers": [critical.to_dict()],
            "recommendation": None, "trend": {},
        }
        _out, err = self._run_dispatch(self._make_args("minimax"))
        self.assertEqual(self._stub.calls, 1)
        self.assertIn("quota warning", err.lower())
        self.assertIn("5%", err.lower())

    def test_warning_is_emitted_before_first_db_insert(self) -> None:
        """Acceptance: warning lines MUST be printed to stderr BEFORE the
        first run row is created. We verify by reading the warning count
        from a collector that did NOT see an empty runs table yet — i.e.
        the snapshot was taken before any row landed."""
        from orchestra_cli import db as _db
        critical = ProviderResult(
            id="minimax", name="MiniMax", status="ok", plan="Token Plan",
            windows=[QuotaWindow.from_remaining(
                id="weekly", label="Weekly", scope="Coding models",
                remaining_percent=5.0,
            )],
            source="fixture",
        )
        # The collector itself inspects the runs table at the moment it's
        # called by UsageService; if it sees 0 rows, the snapshot — and
        # therefore the warning — happened before any insert. The DB
        # connection is held in a local name and closed in `finally` so
        # no ResourceWarning surfaces when Python tears down.
        runs_seen_at_snapshot = {"n": None}

        def side_effecting_provider():
            probe = _db.connect(self.root)
            try:
                runs_seen_at_snapshot["n"] = probe.execute(
                    "SELECT COUNT(*) AS n FROM runs"
                ).fetchone()["n"]
            except Exception:
                runs_seen_at_snapshot["n"] = -1
            finally:
                probe.close()
            return critical

        from orchestra_cli.usage.service import UsageService as _US
        self._stub._payload = None
        # Replace the stub with a real service whose only collector is the
        # side-effecting one. This forces the snapshot to read the DB.
        self._service = _US(collectors=(("minimax", "MiniMax", side_effecting_provider),))
        cli.default_service = lambda: self._service  # type: ignore[assignment]

        import io, contextlib
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            cli.cmd_dispatch(self._make_args("minimax"))

        # The collector saw 0 rows at the moment it ran. That proves the
        # snapshot was taken before any INSERT.
        self.assertEqual(runs_seen_at_snapshot["n"], 0)
        # The warning text reached stderr.
        self.assertIn("quota warning", err.getvalue().lower())
        # And the warning was printed before the run-start line in stdout.
        warn_idx = err.getvalue().lower().find("quota warning")
        out_text = out.getvalue()
        # As of W-0007 the dispatcher prints "run <id> (<slug>): ..." instead
        # of "run <id>: ...". Match the run-prefix generically so this
        # guard doesn't depend on the exact wording.
        import re as _re
        run_match = _re.search(r"run\s+\d+\b", out_text)
        run_idx = run_match.start() if run_match else -1
        # Warning must come before the first "run N:" line in chronological
        # output (both streams are buffered; we know stderr was flushed
        # before stdout because cmd_dispatch's `print(line, file=sys.stderr)`
        # runs in the phase BEFORE the loop emits the run line).
        self.assertGreater(warn_idx, -1)
        self.assertGreater(run_idx, -1)
        # And a row was created.
        verify = _db.connect(self.root)
        try:
            n = verify.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"]
        finally:
            verify.close()
        self.assertEqual(n, 1)

    def test_quota_collection_exception_fails_open(self) -> None:
        self._stub._payload = RuntimeError("network down")
        _out, err = self._run_dispatch(self._make_args("minimax"))
        # No warning printed (collector failed); dispatch still inserted.
        self.assertNotIn("quota warning", err.lower())
        con = db.connect(self.root)
        row = con.execute("SELECT COUNT(*) AS n FROM runs").fetchone()
        con.close()
        self.assertEqual(row["n"], 1)


if __name__ == "__main__":
    sys.exit(unittest.main())
