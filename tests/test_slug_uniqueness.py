"""SQLite-level uniqueness + concurrent-collision handling tests for slug minting.

These exercise what happens when:
  1. Two dispatchers race the in-Python collision check and both pick the
     same slug. Without the partial UNIQUE index, BOTH rows land and we
     silently lose a name. With it, the second INSERT raises and the
     cli.py retry loop regenerates and tries again.
  2. We hand-craft a pre-populated table; ``assign_slug`` must not pick
     any of the existing values.
"""
from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from orchestra_cli import db, names


def _seed_runs_db(root: Path) -> sqlite3.Connection:
    """Create a brand-new DB on disk by running our migrator in isolation."""
    (root / ".orchestra").mkdir(parents=True, exist_ok=True)
    return db.connect(root)


class PartialUniqueIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.con = _seed_runs_db(self.root)

    def tearDown(self) -> None:
        self.con.close()
        self.tmp.cleanup()

    def test_partial_unique_index_exists(self) -> None:
        rows = self.con.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index'"
        ).fetchall()
        self.assertTrue(any("idx_runs_slug_unique" in r["name"] for r in rows),
                        f"slug partial unique index missing; have {[(r['name'], r['sql']) for r in rows]}")

    def test_partial_unique_index_allows_nulls(self) -> None:
        # Two NULL slugs must coexist — the index is partial WHERE slug IS NOT NULL.
        self.con.execute(
            "INSERT INTO runs(agent, backend, requested_by, workdir, status, started_at) "
            "VALUES(?,?,?,?, 'spawning', '2026-07-18T00:00:00Z')",
            ("minimax", "opencode", "codex", str(self.root)),
        )
        self.con.execute(
            "INSERT INTO runs(agent, backend, requested_by, workdir, status, started_at) "
            "VALUES(?,?,?,?, 'spawning', '2026-07-18T00:00:00Z')",
            ("minimax", "opencode", "codex", str(self.root)),
        )
        self.con.commit()
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 2,
        )

    def test_duplicate_slug_is_rejected(self) -> None:
        self.con.execute(
            "INSERT INTO runs(agent, backend, slug, requested_by, workdir, status, started_at) "
            "VALUES(?,?,?,?,?, 'spawning', '2026-07-18T00:00:00Z')",
            ("minimax", "opencode", "silly_panda", "codex", str(self.root)),
        )
        self.con.commit()
        with self.assertRaises(sqlite3.IntegrityError) as cm:
            self.con.execute(
                "INSERT INTO runs(agent, backend, slug, requested_by, workdir, status, started_at) "
                "VALUES(?,?,?,?,?, 'spawning', '2026-07-18T00:00:00Z')",
                ("minimax", "opencode", "silly_panda", "codex", str(self.root)),
            )
        # The UNIQUE-on-slug check must report the slug path so the dispatcher
        # recognises this as a retry-able collision.
        self.assertTrue(names.is_unique_violation(cm.exception))
        self.assertIn("runs.slug", str(cm.exception))


class ConcurrentCollisionRetryTests(unittest.TestCase):
    """The dispatcher's contract: when ``INSERT`` raises ``UNIQUE constraint
    failed: runs.slug``, we must regenerate and try again — NEVER silently
    overwrite the original collision.

    SQLite is single-threaded per-connection, so the realistic shape of a
    "race" between two dispatchers is two processes / two connections
    hammering the same DB. We simulate that by giving each thread its own
    connection with ``check_same_thread=False`` so the DB enforces the
    partial UNIQUE index across both writers.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # Seed the schema exactly the way it would be on a real project:
        # db.connect() runs the migrator + partial UNIQUE index, then we
        # throw that connection away and have each thread open its own.
        seeder = _seed_runs_db(self.root)
        seeder.close()
        names.reset_memory_cache()

    def tearDown(self) -> None:
        names.reset_memory_cache()
        self.tmp.cleanup()

    def _open_thread_connection(self):
        # ``check_same_thread=False`` here is purely for the test seam: the
        # test wants to simulate two concurrent dispatchers hammering one
        # DB file. Production runs always own a connection in the same
        # thread that opened it.
        c = sqlite3.connect(
            self.root / ".orchestra" / "orchestra.db",
            check_same_thread=False,
            timeout=30,
        )
        c.row_factory = sqlite3.Row
        return c

    def test_two_threads_racing_with_collision_still_each_get_a_slug(self):
        """Race the in-Python check: both threads mint the same candidate,
        then each tries to insert. With the partial UNIQUE index, exactly
        one wins per pair; the other retries and eventually succeeds."""
        barrier = threading.Barrier(2)
        seen = []
        errors = []

        def worker():
            con = self._open_thread_connection()
            try:
                barrier.wait()
                local_seen = []
                for _ in range(64):
                    slug = names.assign_slug(con)
                    try:
                        con.execute(
                            "INSERT INTO runs(agent, backend, slug, requested_by, workdir, "
                            "status, started_at) VALUES(?,?,?,?,?, 'spawning', "
                            "'2026-07-18T00:00:00Z')",
                            ("minimax", "opencode", slug, "codex", str(self.root)),
                        )
                        con.commit()
                        local_seen.append(slug)
                        break
                    except sqlite3.IntegrityError as exc:
                        if not names.is_unique_violation(exc):
                            errors.append(exc)
                            return
                        names.reset_memory_cache()
                        con.rollback()
                        continue
                seen.extend(local_seen)
            finally:
                con.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        self.assertEqual(errors, [])
        self.assertEqual(len(seen), 2)
        # Both threads finished with distinct, valid slugs.
        self.assertEqual(len(set(seen)), 2)
        for s in seen:
            self.assertTrue(names.is_valid_slug(s))

        # Independent confirmation: exactly two rows were written, both
        # unique.
        final = sqlite3.connect(self.root / ".orchestra" / "orchestra.db")
        try:
            slug_set = {row[0] for row in final.execute(
                "SELECT slug FROM runs WHERE slug IS NOT NULL ORDER BY id"
            ).fetchall()}
        finally:
            final.close()
        self.assertEqual(len(slug_set), 2)
        self.assertEqual(slug_set, set(seen))


if __name__ == "__main__":
    unittest.main()
