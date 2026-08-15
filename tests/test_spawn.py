"""D11 delegation allowlist: broker check + rejection recording."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dromond import db, spawn

CFG = {"profiles": {
    "lead": {"backend": "codex", "spawn_profiles": ["worker", "phantom"]},
    "worker": {"backend": "opencode"},
    "loner": {"backend": "claude"},
}}


class CheckTargetTests(unittest.TestCase):
    def test_permitted_target_is_ok(self) -> None:
        self.assertEqual(spawn.check_target(CFG, "lead", "worker"), (True, None))

    def test_no_spawn_profiles_means_no_delegation(self) -> None:
        ok, error = spawn.check_target(CFG, "loner", "worker")
        self.assertFalse(ok)
        self.assertIn("may not delegate", error)

    def test_forbidden_target_returns_the_permitted_list(self) -> None:
        ok, error = spawn.check_target(CFG, "lead", "lead")
        self.assertFalse(ok)
        self.assertIn("worker, phantom", error)

    def test_unknown_or_unconfigured_target_is_rejected(self) -> None:
        # hallucinated name: not in the allowlist at all
        ok, error = spawn.check_target(CFG, "lead", "gpt9-mega")
        self.assertFalse(ok)
        self.assertIn("permitted", error)
        # allowlisted but not actually configured
        ok, error = spawn.check_target(CFG, "lead", "phantom")
        self.assertFalse(ok)
        self.assertIn("not a configured profile", error)


class EnabledSetTests(unittest.TestCase):
    """W-0187: delegation is the second moment the project's enabled set
    binds, so the effective allowlist is ``spawn_profiles ∩ enabled``."""

    def scoped(self, enabled):
        cfg = dict(CFG)
        cfg["project_id"] = "proj-1"
        cfg["enabled_profiles"] = enabled
        return cfg

    def test_no_enabled_set_leaves_the_allowlist_alone(self) -> None:
        cfg = self.scoped(None)
        self.assertEqual(spawn.allowed_targets(cfg, "lead"),
                         ["worker", "phantom"])
        self.assertEqual(spawn.check_target(cfg, "lead", "worker"), (True, None))

    def test_the_allowlist_is_intersected_with_the_enabled_set(self) -> None:
        cfg = self.scoped(["worker"])
        self.assertEqual(spawn.allowed_targets(cfg, "lead"), ["worker"])
        self.assertEqual(spawn.check_target(cfg, "lead", "worker"), (True, None))

    def test_delegating_to_a_disabled_profile_is_refused_by_project(self) -> None:
        """`worker` is allowlisted and configured — the project simply has
        not enabled it, and the refusal says so rather than pretending the
        allowlist never named it."""
        cfg = self.scoped(["lead", "phantom"])
        ok, error = spawn.check_target(cfg, "lead", "worker")
        self.assertFalse(ok)
        self.assertIn("proj-1", error)
        self.assertIn("has not enabled", error)
        self.assertIn("permitted here: phantom", error)

    def test_an_allowlist_the_project_enables_none_of_says_so(self) -> None:
        """Not "no spawn_profiles" — the allowlist is there, the project just
        enables none of it, and the two are different edits."""
        cfg = self.scoped([])
        ok, error = spawn.check_target(cfg, "lead", "worker")
        self.assertFalse(ok)
        self.assertIn("enables none of its spawn_profiles", error)
        self.assertIn("worker, phantom", error)


class RequestSpawnTests(unittest.TestCase):
    def test_rejection_is_recorded_as_a_finding_and_run_continues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"DROMOND_HOME": str(Path(tmp) / "home")}):
            root = Path(tmp)
            con = db.connect()
            con.execute(
                "INSERT INTO runs(profile, backend, title, requested_by, workdir, "
                "status, started_at) VALUES('lead','codex','t','human',?, "
                "'running', ?)", (str(root), db.now()))
            con.commit()
            run = con.execute("SELECT * FROM runs").fetchone()
            ok, error = spawn.request_spawn(con, CFG, run, "lead")
            self.assertFalse(ok)
            self.assertIn("worker, phantom", error)  # worker can self-correct
            msg = con.execute(
                "SELECT * FROM messages WHERE run_id=? AND kind='finding'",
                (run["id"],)).fetchone()
            self.assertIn("spawn rejected", msg["body"])
            self.assertIn("worker, phantom", msg["body"])
            # nothing was spawned; the run row is untouched
            self.assertEqual(
                con.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"], 1)

            ok, error = spawn.request_spawn(con, CFG, run, "worker")
            self.assertTrue(ok)
            self.assertIsNone(error)
            # a permitted request records nothing (phase 1: launch is a seam)
            self.assertEqual(con.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE kind='finding'"
            ).fetchone()["n"], 1)
            con.close()


class SpawnBoundsTests(unittest.TestCase):
    """DESIGN §5 depth + per-run child limits — the only bound on run count
    growing without a human, since dispatch has no concurrency cap (§4)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(
            os.environ, {"DROMOND_HOME": str(Path(self.tmp.name) / "home")})
        self.env.start()
        self.con = db.connect()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.env.stop)
        self.addCleanup(self.con.close)

    def add_run(self, profile="lead", parent=None, requested_by="human") -> int:
        cur = self.con.execute(
            "INSERT INTO runs(profile, backend, title, requested_by, workdir, "
            "parent_run, status, started_at) VALUES(?, 'codex','t',?,'/tmp',?,"
            "'running', ?)", (profile, requested_by, parent, db.now()))
        self.con.commit()
        return int(cur.lastrowid)

    def run_row(self, run_id):
        return self.con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()

    def test_depth_limit_stops_the_tree_from_growing(self) -> None:
        cfg = {**CFG, "settings": {"max_spawn_depth": 2}}
        root = self.add_run()
        child = self.add_run(parent=root, requested_by=spawn.SPAWN_REQUESTER)
        grandchild = self.add_run(parent=child, requested_by=spawn.SPAWN_REQUESTER)
        self.assertEqual(spawn.depth(self.con, root), 0)
        self.assertEqual(spawn.depth(self.con, grandchild), 2)
        self.assertEqual(spawn.request_spawn(self.con, cfg, self.run_row(child),
                                             "worker")[0], True)
        ok, error = spawn.request_spawn(self.con, cfg, self.run_row(grandchild),
                                        "worker")
        self.assertFalse(ok)
        self.assertIn("spawn depth limit", error)
        # Rejection is a finding, and nothing was launched.
        self.assertIn("spawn rejected", self.con.execute(
            "SELECT body FROM messages WHERE run_id=? AND kind='finding'",
            (grandchild,)).fetchone()["body"])

    def test_per_run_child_limit_is_enforced(self) -> None:
        cfg = {**CFG, "settings": {"max_children_per_run": 2}}
        root = self.add_run()
        for _ in range(2):
            self.add_run(parent=root, requested_by=spawn.SPAWN_REQUESTER)
        ok, error = spawn.request_spawn(self.con, cfg, self.run_row(root), "worker")
        self.assertFalse(ok)
        self.assertIn("child limit", error)

    def test_session_continuations_are_not_spawn_children(self) -> None:
        # create_followup also sets parent_run; counting it would exhaust the
        # limits on a run that never delegated anything.
        cfg = {**CFG, "settings": {"max_spawn_depth": 1, "max_children_per_run": 1}}
        root = self.add_run()
        followup = self.add_run(parent=root, requested_by="work")
        self.assertEqual(spawn.depth(self.con, followup), 0)
        self.assertEqual(spawn.child_count(self.con, root), 0)
        self.assertEqual(
            spawn.request_spawn(self.con, cfg, self.run_row(followup), "worker"),
            (True, None))

    def test_defaults_apply_when_config_says_nothing(self) -> None:
        run = self.run_row(self.add_run())
        self.assertEqual(spawn.check_bounds(self.con, CFG, run), (True, None))


if __name__ == "__main__":
    unittest.main()
