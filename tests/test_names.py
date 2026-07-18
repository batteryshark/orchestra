"""Naming + collision + validation tests for the run-slug generator.

Coverage targets:
  * format check accepts every generated pair, rejects obvious garbage.
  * assign_slug is collision-safe against a pre-populated runs table.
  * numeric run ids remain authoritative; the slug is an extra key.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from orchestra_cli import db, names


class SlugFormatTests(unittest.TestCase):
    def test_generator_returns_valid_lowercase_pair(self) -> None:
        for _ in range(200):
            slug = names.generate_slug()
            self.assertTrue(names.is_valid_slug(slug),
                            f"generated slug failed validation: {slug!r}")
            self.assertEqual(slug, slug.lower())
            adj, _, noun = slug.partition("_")
            self.assertIn(adj, names.ADJECTIVES)
            self.assertIn(noun, names.NOUNS)

    def test_is_valid_slug_rejects_garbage(self) -> None:
        for bad in ["", None, "SILLY_PANDA", "silly-panda",
                    "silly__panda", "silly_panda_", "_silly_panda",
                    "xy_panda", "silly_xy", "silly_panda!", "a b c",
                    "silly.panda", "silly_panda\n", "DROP TABLE runs;--"]:
            self.assertFalse(names.is_valid_slug(bad), f"rejected expected invalid: {bad!r}")

    def test_is_valid_slug_accepts_each_word_pair(self) -> None:
        # exhaustive sweep: every adjective*x noun must validate.
        for adj in names.ADJECTIVES:
            for noun in names.NOUNS:
                self.assertTrue(names.is_valid_slug(f"{adj}_{noun}"))


class AssignSlugTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".orchestra").mkdir(parents=True, exist_ok=True)
        # Use the project's full DB connect so we exercise the real schema
        # (including the partial UNIQUE index and Row factory), not a
        # bare-bones ad-hoc schema that would behave differently.
        names.reset_memory_cache()
        self.con = db.connect(self.root)

    def tearDown(self) -> None:
        names.reset_memory_cache()
        self.con.close()
        self.tmp.cleanup()

    def test_assigns_unique_slug_against_empty_table(self) -> None:
        slug = names.assign_slug(self.con)
        self.assertTrue(names.is_valid_slug(slug))
        # assign_slug does NOT write — the dispatcher owns the INSERT.
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)

    def test_collides_and_retry_avoids_existing_slugs(self) -> None:
        # Pre-populate with some legitimate slugs.
        for _ in range(5):
            slug = names.generate_slug()
            self.con.execute(
                "INSERT INTO runs(agent, backend, requested_by, workdir, slug, status, started_at) "
                "VALUES(?,?,?,?,?, 'spawning', '2026-07-18T00:00:00Z')",
                ("minimax", "opencode", "codex", str(self.root), slug),
            )
        self.con.commit()
        used = {r["slug"] for r in self.con.execute(
            "SELECT slug FROM runs WHERE slug IS NOT NULL",
        )}
        first = names.assign_slug(self.con)
        self.assertNotIn(first, used)

        # Insert the just-issued slug too; next call must dodge it.
        self.con.execute(
            "INSERT INTO runs(agent, backend, requested_by, workdir, slug, status, started_at) "
            "VALUES(?,?,?,?,?, 'spawning', '2026-07-18T00:00:00Z')",
            ("minimax", "opencode", "codex", str(self.root), first),
        )
        self.con.commit()
        used.add(first)
        second = names.assign_slug(self.con)
        self.assertNotIn(second, used)
        self.assertTrue(names.is_valid_slug(second))

    def test_falls_back_to_runtime_error_after_max_attempts(self) -> None:
        # Pre-fill every possible slug to make the retry impossible.
        names.reset_memory_cache()
        for adj in names.ADJECTIVES:
            for noun in names.NOUNS:
                self.con.execute(
                    "INSERT INTO runs(agent, backend, requested_by, workdir, slug, status, started_at) "
                    "VALUES(?,?,?,?,?, 'spawning', '2026-07-18T00:00:00Z')",
                    ("minimax", "opencode", "codex", str(self.root), f"{adj}_{noun}"),
                )
        self.con.commit()
        with self.assertRaises(RuntimeError) as cm:
            names.assign_slug(self.con, max_attempts=4)
        self.assertIn("unique run slug", str(cm.exception))

    def test_slug_is_optional_and_null_in_pre_w0007_rows(self) -> None:
        # Backward-compatibility: rows created before W-0007 land have slug
        # NULL; assign_slug must work even when such rows are present.
        self.con.execute(
            "INSERT INTO runs(agent, backend, requested_by, workdir, status, started_at) "
            "VALUES(?,?,?,?, 'spawning', '2026-07-18T00:00:00Z')",
            ("minimax", "opencode", "codex", str(self.root)),
        )
        self.con.commit()
        slug = names.assign_slug(self.con)
        self.assertTrue(names.is_valid_slug(slug))

    def test_reset_memory_cache_is_safe(self) -> None:
        # reset_memory_cache must not blow up on a pristine state and must
        # leave assign_slug working afterwards.
        names.reset_memory_cache()
        names.reset_memory_cache()
        slug = names.assign_slug(self.con)
        self.assertTrue(names.is_valid_slug(slug))


if __name__ == "__main__":
    unittest.main()
