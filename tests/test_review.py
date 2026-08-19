import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dromond import db, review


def _run(con, profile, model, status, tokens=None, cost=None,
         started="2026-08-18T10:00:00Z", finished="2026-08-18T10:10:00Z"):
    con.execute(
        "INSERT INTO runs(profile, backend, model, requested_by, workdir, "
        "status, started_at, finished_at, tokens_total, cost_usd) "
        "VALUES(?,?,?,'human','/p',?,?,?,?,?)",
        (profile, "opencode", model, status, started, finished, tokens, cost))


class ReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(
            os.environ, {"DROMOND_HOME": str(Path(self.tmp.name) / "home")})
        self.env.start()
        self.con = db.connect()

    def tearDown(self) -> None:
        self.con.close()
        self.env.stop()
        self.tmp.cleanup()

    def test_outcomes_per_profile_worst_first(self) -> None:
        _run(self.con, "good", "m", "done", tokens=100, cost=1.0)
        _run(self.con, "good", "m", "done", tokens=300, cost=3.0)
        _run(self.con, "bad", "m", "done")
        _run(self.con, "bad", "m", "failed")
        _run(self.con, "bad", "m", "killed")
        rows = review.performance(self.con)
        self.assertEqual([r["profile"] for r in rows], ["bad", "good"])
        bad, good = rows
        self.assertEqual((bad["runs"], bad["done"], bad["failed"], bad["killed"]),
                         (3, 1, 1, 1))
        self.assertAlmostEqual(bad["success"], 1 / 3, places=3)
        self.assertEqual(bad["avg_seconds"], 600.0)
        self.assertEqual(bad["uncaptured"], 3)
        self.assertIsNone(bad["cost"])
        self.assertEqual((good["success"], good["tokens"], good["cost"]),
                         (1.0, 400, 4.0))

    def test_active_runs_are_not_reviewed(self) -> None:
        _run(self.con, "a", "m", "done")
        _run(self.con, "a", "m", "running", finished=None)
        rows = review.performance(self.con)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["runs"], 1)

    def test_plan_backed_runs_have_no_price(self) -> None:
        # kimi resolves to a plan provider in runway's table; api cost
        # must not be summed for it even when a transcript carried a number.
        _run(self.con, "p", "kimi-for-coding/k3", "done", cost=0.5)
        row = review.performance(self.con)[0]
        self.assertEqual(row["plan_runs"], 1)
        self.assertIsNone(row["cost"])

    def test_empty_history(self) -> None:
        self.assertEqual(review.performance(self.con), [])


if __name__ == "__main__":
    unittest.main()
