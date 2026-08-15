import sqlite3
import unittest

from dromond import names


def _memory_con() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY, slug TEXT UNIQUE)")
    return con


class NamesTests(unittest.TestCase):
    def setUp(self) -> None:
        names.reset_memory_cache()

    def test_generate_slug_format(self) -> None:
        for _ in range(50):
            self.assertTrue(names.is_valid_slug(names.generate_slug()))

    def test_is_valid_slug_rejects_junk(self) -> None:
        for bad in (None, 42, "", "calm", "calm_otter_x", "CALM_OTTER",
                    "calm-otter", "calm_robot; DROP TABLE runs", "calm_"):
            self.assertFalse(names.is_valid_slug(bad))

    def test_assign_slug_avoids_existing(self) -> None:
        con = _memory_con()
        taken = {f"{a}_{n}" for a in names.ADJECTIVES for n in names.NOUNS[:63]}
        con.executemany("INSERT INTO runs(slug) VALUES(?)", [(s,) for s in taken])
        slug = names.assign_slug(con, max_attempts=10_000)
        self.assertTrue(names.is_valid_slug(slug))
        self.assertNotIn(slug, taken)

    def test_assign_slug_exhaustion_raises(self) -> None:
        con = _memory_con()
        every = {f"{a}_{n}" for a in names.ADJECTIVES for n in names.NOUNS}
        con.executemany("INSERT INTO runs(slug) VALUES(?)", [(s,) for s in every])
        with self.assertRaises(RuntimeError):
            names.assign_slug(con, max_attempts=64)

    def test_is_unique_violation(self) -> None:
        con = _memory_con()
        con.execute("INSERT INTO runs(slug) VALUES('calm_otter')")
        try:
            con.execute("INSERT INTO runs(slug) VALUES('calm_otter')")
            self.fail("expected IntegrityError")
        except sqlite3.IntegrityError as exc:
            self.assertTrue(names.is_unique_violation(exc))


if __name__ == "__main__":
    unittest.main()
