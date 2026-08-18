"""The merge card's two verbs (DESIGN §9): retry the landing, dispatch a resolver.

Throwaway git repositories and a throwaway DROMOND_HOME. Nod's future surface
(``withdraw_merge_cards``) is STUBBED, never depended on; the one test that
needs a real card uses tests/fake_nod.py. No real harness is ever launched —
the ``resolver.launcher`` seam is replaced everywhere a dispatch could fire.
"""
import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dromond import db, nod, resolver
from tests.fake_nod import DECISIONS_CHANNEL, DECISIONS_TOKEN, FakeNod

BRANCH = "dromond/run-1"

PROFILES = {
    "cheap": {"backend": "opencode", "tier": 1, "priority": 10},
    "cheaper": {"backend": "opencode", "tier": 1, "priority": 5},
    "big": {"backend": "opencode", "tier": 3, "priority": 10},
}


def git(root: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


class ResolverFixture(unittest.TestCase):
    """A git project, a run row on a kept branch, central state in a tempdir."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name).resolve()
        self.root = self.tmp_path / "demo"
        self.root.mkdir()
        git(self.root, "init", "--quiet")
        git(self.root, "symbolic-ref", "HEAD", "refs/heads/main")
        git(self.root, "config", "user.email", "test@example.com")
        git(self.root, "config", "user.name", "test")
        git(self.root, "config", "commit.gpgsign", "false")
        (self.root / "app.py").write_text("print('hello')\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "--quiet", "-m", "initial")
        # Never the developer's real home, config, or Nod credentials.
        self.env = mock.patch.dict(os.environ, {
            "DROMOND_HOME": str(self.tmp_path / "home"),
            "DROMOND_CONFIG": str(self.tmp_path / "global.toml"),
            "DROMOND_NOD_SECRETS_FILE": str(self.tmp_path / "no-secrets.env"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.con = db.connect()
        self.addCleanup(self.con.close)

    def run_branch(self, changes: dict[str, str], branch: str = BRANCH) -> None:
        git(self.root, "checkout", "--quiet", "-b", branch)
        for name, text in changes.items():
            (self.root / name).write_text(text)
        git(self.root, "add", "-A")
        git(self.root, "commit", "--quiet", "-m", "run work")
        git(self.root, "checkout", "--quiet", "main")

    def add_run(self, status: str = "done", branch: str | None = BRANCH,
                work_item: str | None = None) -> None:
        self.con.execute(
            "INSERT INTO runs(id, slug, profile, backend, requested_by, workdir, "
            "work_item, branch, status, started_at) "
            "VALUES(1,'brave_otter','stub','opencode','human',?,?,?,?,?)",
            (str(self.root), work_item, branch, status, db.now()))
        self.con.commit()

    def db_run(self, run_id: int):
        return self.con.execute("SELECT * FROM runs WHERE id=?",
                                (run_id,)).fetchone()


class RetryTestCase(ResolverFixture):

    def test_a_retry_lands_and_withdraws_the_stale_card(self) -> None:
        self.run_branch({"feature.py": "x = 1\n"})
        self.add_run()
        withdrawn = []
        stub = lambda con, cfg, run_id, note="": withdrawn.append((run_id, note))
        with mock.patch.object(nod, "withdraw_merge_cards", stub, create=True):
            note = resolver.retry_landing(self.con, {}, 1)
        self.assertIn(f"Merged {BRANCH} into main", note)
        self.assertIn("feature.py", git(self.root, "ls-tree", "--name-only", "main"))
        self.assertEqual(1, len(withdrawn))
        self.assertEqual(1, withdrawn[0][0])
        self.assertIn("landed as", withdrawn[0][1])

    def test_a_broken_withdrawal_never_breaks_the_landing(self) -> None:
        self.run_branch({"feature.py": "x = 1\n"})
        self.add_run()
        boom = mock.Mock(side_effect=RuntimeError("nod went away"))
        with mock.patch.object(nod, "withdraw_merge_cards", boom, create=True), \
                contextlib.redirect_stderr(io.StringIO()):
            note = resolver.retry_landing(self.con, {}, 1)
        self.assertIn(f"Merged {BRANCH} into main", note)

    def test_a_still_live_run_refuses(self) -> None:
        self.run_branch({"feature.py": "x = 1\n"})
        self.add_run(status="running")
        before = git(self.root, "rev-parse", "main")
        note = resolver.retry_landing(self.con, {}, 1)
        self.assertIn("still running", note)
        self.assertEqual(before, git(self.root, "rev-parse", "main"))

    def test_a_missing_run_refuses(self) -> None:
        self.assertIn("does not exist", resolver.retry_landing(self.con, {}, 99))

    def test_a_gone_branch_refuses_with_the_sentence_saying_so(self) -> None:
        self.add_run()  # the row names a branch git never had
        note = resolver.retry_landing(self.con, {}, 1)
        self.assertIn(f"branch {BRANCH} is gone", note)

    def test_a_shared_checkout_run_has_no_branch_to_retry(self) -> None:
        self.add_run(branch=None)
        self.assertIn("no branch to land", resolver.retry_landing(self.con, {}, 1))

    def test_a_retry_that_escalates_again_files_a_fresh_card(self) -> None:
        # merge_run's existing behaviour, proven still alive through the
        # retry entry: the conflict escalates and a card reaches the channel.
        fake = FakeNod()
        url = fake.start()
        self.addCleanup(fake.stop)
        self.run_branch({"app.py": "print('branch')\n"})
        (self.root / "app.py").write_text("print('main moved')\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "--quiet", "-m", "main moves")
        self.add_run()
        with mock.patch.dict(os.environ, {
                "DROMOND_NOD_BASE_URL": url,
                "DROMOND_NOD_DECISIONS_CHANNEL": DECISIONS_CHANNEL,
                "DROMOND_NOD_DECISIONS_TOKEN": DECISIONS_TOKEN}):
            note = resolver.retry_landing(self.con, {"nod": {"enabled": True}}, 1)
        self.assertIn("escalated at rebase", note)
        card = fake.requests["req_1"]
        self.assertEqual(["retry", "resolver", "leave"],
                         [o["id"] for o in card["options"]])
        self.assertIn(BRANCH, card["title"])


class DispatchTestCase(ResolverFixture):

    def setUp(self) -> None:
        super().setUp()
        self.launched: list[tuple[Path, int]] = []
        seam = mock.patch.object(resolver, "launcher",
                                 lambda root, rid: self.launched.append((root, rid)))
        seam.start()
        self.addCleanup(seam.stop)

    def dispatch(self, cfg: dict, run_id: int = 1,
                 reason: str = "Stage `rebase`: rebase onto main conflicted"):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            new_id = resolver.dispatch_resolver(self.con, cfg, run_id, reason)
        return new_id, err.getvalue()

    def test_the_resolver_run_carries_lineage_and_a_real_brief(self) -> None:
        self.run_branch({"app.py": "print('branch')\n"})
        self.add_run(work_item="W-0042")
        reason = "Stage `rebase`: rebase onto main conflicted; resolve by hand"
        new_id, _ = self.dispatch({"profiles": PROFILES}, reason=reason)
        self.assertIsNotNone(new_id)
        row = self.db_run(new_id)
        self.assertEqual(1, row["parent_run"])
        self.assertEqual("W-0042", row["work_item"])
        self.assertEqual("spawning", row["status"])
        # Its OWN fresh worktree off the current base, never the failed branch.
        self.assertEqual(f"dromond/run-{new_id}", row["branch"])
        self.assertNotEqual(str(self.root), row["workdir"])
        self.assertEqual([(self.root, new_id)], self.launched)
        brief = Path(row["brief_path"]).read_text()
        self.assertIn(BRANCH, brief)
        self.assertIn("`main`", brief)
        self.assertIn(reason, brief)
        self.assertIn("Do not force-push", brief)
        self.assertIn(f"Do not delete `{BRANCH}`", brief)
        self.assertIn(f"dromond merge {BRANCH}", brief)

    def test_a_configured_resolver_profile_wins(self) -> None:
        self.run_branch({"app.py": "print('branch')\n"})
        self.add_run()
        cfg = {"profiles": PROFILES, "merge": {"resolver_profile": "cheap"}}
        new_id, _ = self.dispatch(cfg)
        self.assertEqual("cheap", self.db_run(new_id)["profile"])

    def test_without_one_the_top_tier_1_profile_is_staffed(self) -> None:
        # Tier 1, not tier 3 (owner, 2026-08-18): most escalations are a
        # mechanical rebase a workhorse handles, and staffing every one heavy
        # is wasteful. Anything bigger is one config line away.
        self.run_branch({"app.py": "print('branch')\n"})
        self.add_run()
        new_id, _ = self.dispatch({"profiles": PROFILES})
        self.assertEqual("cheaper", self.db_run(new_id)["profile"])  # priority 5 < 10

    def test_no_profile_at_all_refuses_on_stderr(self) -> None:
        self.run_branch({"app.py": "print('branch')\n"})
        self.add_run()
        new_id, err = self.dispatch({"profiles": {"big": PROFILES["big"]}})
        self.assertIsNone(new_id)
        self.assertIn("tier = 1", err)
        self.assertIsNone(self.db_run(2), "no run row may be left behind")

    def test_a_missing_run_or_gone_branch_refuses_on_stderr(self) -> None:
        new_id, err = self.dispatch({"profiles": PROFILES}, run_id=99)
        self.assertIsNone(new_id)
        self.assertIn("does not exist", err)
        self.add_run()  # the row names a branch git never had
        new_id, err = self.dispatch({"profiles": PROFILES})
        self.assertIsNone(new_id)
        self.assertIn("gone", err)


if __name__ == "__main__":
    unittest.main()
