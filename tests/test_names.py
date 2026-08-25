import sqlite3
import unittest
from unittest import mock

from orchestra import names


def memory_db() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY, slug TEXT UNIQUE)")
    return con


class NamesTests(unittest.TestCase):
    def setUp(self) -> None:
        names.reset_memory_cache()

    def test_generated_and_user_supplied_slug_validation(self) -> None:
        for _ in range(10):
            self.assertTrue(names.is_valid_slug(names.generate_slug()))
        for value in (None, 42, "", "calm", "calm_otter_x", "CALM_OTTER",
                      "calm-otter", "calm_robot; DROP TABLE runs", "calm_"):
            with self.subTest(value=value):
                self.assertFalse(names.is_valid_slug(value))

    def test_assignment_retries_collisions_and_has_a_ceiling(self) -> None:
        con = memory_db()
        con.execute("INSERT INTO runs(slug) VALUES('calm_otter')")
        with mock.patch.object(
                names, "generate_slug", side_effect=["calm_otter", "bold_fox"]):
            self.assertEqual(names.assign_slug(con, max_attempts=2), "bold_fox")
        with mock.patch.object(names, "generate_slug", return_value="calm_otter"):
            with self.assertRaises(RuntimeError):
                names.assign_slug(con, max_attempts=2)

    def test_unique_constraint_detection(self) -> None:
        con = memory_db()
        con.execute("INSERT INTO runs(slug) VALUES('calm_otter')")
        with self.assertRaises(sqlite3.IntegrityError) as caught:
            con.execute("INSERT INTO runs(slug) VALUES('calm_otter')")
        self.assertTrue(names.is_unique_violation(caught.exception))


if __name__ == "__main__":
    unittest.main()
