import sqlite3
import tempfile
import unittest
from pathlib import Path

from orchestra import db, idempotency


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.con = db.connect(":memory:")

    def tearDown(self):
        self.con.close()

    def test_replay_returns_frozen_response_and_mismatch_conflicts(self):
        body = {"request_id": "one", "name": "Research"}
        with db.api_mutation(self.con):
            self.assertIsNone(idempotency.reserve(
                self.con, "one", "POST", "/groups", body))
            idempotency.finish(
                self.con, "one", {"group_id": "g"}, commit=False)
        with db.api_mutation(self.con):
            replay = idempotency.reserve(
                self.con, "one", "POST", "/groups", body)
        self.assertEqual(
            replay, {"group_id": "g"})
        with self.assertRaises(idempotency.Conflict), \
                db.api_mutation(self.con):
            idempotency.reserve(
                self.con, "one", "POST", "/groups",
                {**body, "name": "Other"})

    def test_api_transaction_defers_helper_commits_and_with_blocks(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "fleet.db"
            con = db.connect(path)
            observer = sqlite3.connect(path)
            try:
                con.execute("CREATE TABLE transaction_probe(value TEXT)")
                con.commit()
                with db.api_mutation(con):
                    con.execute("INSERT INTO transaction_probe VALUES('explicit')")
                    con.commit()
                    with con:
                        con.execute("INSERT INTO transaction_probe VALUES('context')")
                    self.assertEqual(observer.execute(
                        "SELECT COUNT(*) FROM transaction_probe").fetchone()[0], 0)
                    self.assertTrue(db.in_api_mutation(con))
                self.assertEqual(observer.execute(
                    "SELECT COUNT(*) FROM transaction_probe").fetchone()[0], 2)
            finally:
                observer.close()
                con.close()

    def test_api_transaction_rollback_is_immediate_and_state_is_restored(self):
        self.con.execute("CREATE TABLE transaction_probe(value TEXT)")
        self.con.commit()
        with self.assertRaisesRegex(RuntimeError, "nested failure"):
            with db.api_mutation(self.con):
                with self.con:
                    with self.con:
                        self.con.execute(
                            "INSERT INTO transaction_probe VALUES('rolled back')")
                        raise RuntimeError("nested failure")
        self.assertFalse(db.in_api_mutation(self.con))
        self.assertFalse(self.con.in_transaction)
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM transaction_probe").fetchone()[0], 0)

        with self.assertRaisesRegex(RuntimeError, "rolled back before completion"):
            with db.api_mutation(self.con):
                self.con.execute(
                    "INSERT INTO transaction_probe VALUES('also rolled back')")
                self.con.rollback()
                self.assertFalse(self.con.in_transaction)
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM transaction_probe").fetchone()[0], 0)
        with self.con:
            self.con.execute("INSERT INTO transaction_probe VALUES('normal')")
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM transaction_probe").fetchone()[0], 1)

    def test_swallowed_nested_rollback_poisons_the_outer_transaction(self):
        self.con.execute("CREATE TABLE transaction_probe(value TEXT)")
        self.con.commit()
        with self.assertRaisesRegex(RuntimeError, "rolled back before completion"):
            with db.api_mutation(self.con):
                try:
                    with self.con:
                        self.con.execute(
                            "INSERT INTO transaction_probe VALUES('before')")
                        raise ValueError("helper failure")
                except ValueError:
                    pass
                self.con.execute(
                    "INSERT INTO transaction_probe VALUES('after')")
        self.assertFalse(db.in_api_mutation(self.con))
        self.assertFalse(self.con.in_transaction)
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM transaction_probe").fetchone()[0], 0)

    def test_helper_cannot_swallow_an_explicit_rollback(self):
        self.con.execute("CREATE TABLE transaction_probe(value TEXT)")
        self.con.commit()

        def helper_that_recovers_locally():
            try:
                self.con.execute(
                    "INSERT INTO transaction_probe VALUES('helper')")
                raise ValueError("local failure")
            except ValueError:
                self.con.rollback()

        with self.assertRaisesRegex(RuntimeError, "rolled back before completion"):
            with db.api_mutation(self.con):
                helper_that_recovers_locally()
                self.con.execute(
                    "INSERT INTO transaction_probe VALUES('caller')")
        self.assertFalse(db.in_api_mutation(self.con))
        self.assertFalse(self.con.in_transaction)
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM transaction_probe").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
