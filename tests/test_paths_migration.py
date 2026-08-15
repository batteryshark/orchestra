"""W-0188: Maestro's state becomes Dromond's, once, and reversibly.

Every test here sandboxes ``HOME`` itself, because that is what ``~/.maestro``
and ``~/.dromond`` expand through — and because the whole point of the
adoption guard is that it fires when NOTHING is overridden. The rest of the
suite pins ``DROMOND_HOME`` (see tests/__init__.py) and therefore never
adopts anything; this module is the one that turns the overrides off, so it
has to put a temporary directory where the real home would be.
"""
import contextlib
import io
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dromond import paths

# The environment a real first start has: no Dromond variables, no Maestro
# ones, and a home directory of our own.
BARE = ("DROMOND_HOME", "DROMOND_CONFIG", "MAESTRO_HOME", "MAESTRO_CONFIG")


class HomeAdoptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve()
        env = {k: v for k, v in os.environ.items() if k not in BARE}
        env["HOME"] = str(self.home)
        self.env = mock.patch.dict(os.environ, env, clear=True)
        self.env.start()
        # A warning is printed once per process, so the cache outlives a
        # test and is reset here. Adoption itself keeps no such state: the
        # filesystem is its only record.
        paths._DEPRECATED_SEEN.clear()
        self.addCleanup(paths._DEPRECATED_SEEN.clear)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.env.stop)

    # --- helpers ------------------------------------------------------------

    def adopt(self) -> str:
        """Run the adoption the way a command does, returning what it said."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            paths.adopt_legacy_home()
        return err.getvalue()

    def old(self) -> Path:
        return self.home / ".maestro"

    def new(self) -> Path:
        return self.home / ".dromond"

    def make_old_state(self) -> None:
        (self.old() / "logs").mkdir(parents=True)
        (self.old() / "maestro.db").write_text("the-old-database")
        (self.old() / "logs" / "daemon.out.log").write_text("old-log")

    # --- a machine that never ran Maestro -----------------------------------

    def test_fresh_home_is_created_when_neither_exists(self) -> None:
        """Nothing to adopt: the new directory is simply made, and no
        migration line is printed at a first start that migrated nothing."""
        said = self.adopt()
        self.assertEqual(said, "")
        self.assertFalse(self.old().exists())
        self.assertEqual(paths.home(), self.new())
        # home() alone does not create the directory; the first real use does.
        self.assertEqual(paths.db_path(), self.new() / "dromond.db")
        self.assertTrue(self.new().is_dir())

    # --- a machine upgrading from Maestro -----------------------------------

    def test_old_home_is_moved_and_the_move_is_announced(self) -> None:
        self.make_old_state()
        said = self.adopt()
        self.assertFalse(self.old().exists(), "the old directory must be MOVED, not copied")
        # The database is renamed with the directory (see the sidecar test).
        self.assertEqual((self.new() / "dromond.db").read_text(), "the-old-database")
        self.assertEqual((self.new() / "logs" / "daemon.out.log").read_text(), "old-log")
        self.assertIn(str(self.old()), said)
        self.assertIn(str(self.new()), said)
        moved = [ln for ln in said.strip().splitlines() if "moved your" in ln]
        self.assertEqual(len(moved), 1, f"exactly one move line, got: {said}")

    def test_the_database_is_renamed_so_the_run_history_survives(self) -> None:
        """The failure this exists to stop: the directory moves, the database
        inside it keeps the name ``maestro.db``, ``db_path`` asks for
        ``dromond.db``, and Dromond starts empty with every run record sitting
        beside it unread. Nothing errors — the history just vanishes."""
        self.make_old_state()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(paths.db_path(), self.new() / "dromond.db")
        self.assertEqual((self.new() / "dromond.db").read_text(), "the-old-database",
                         "the live database must BE the one Dromond opens")
        self.assertFalse((self.new() / "maestro.db").exists())

    def test_the_wal_and_shm_sidecars_travel_with_the_database(self) -> None:
        """SQLite finds the WAL by name. A dromond.db beside a maestro.db-wal
        silently loses every transaction since the last checkpoint, so the
        sidecars are renamed in step or the move is not safe."""
        self.make_old_state()
        (self.old() / "maestro.db-wal").write_text("uncheckpointed")
        (self.old() / "maestro.db-shm").write_text("index")
        with contextlib.redirect_stderr(io.StringIO()):
            paths.home()
        self.assertEqual((self.new() / "dromond.db-wal").read_text(), "uncheckpointed")
        self.assertEqual((self.new() / "dromond.db-shm").read_text(), "index")
        self.assertEqual(sorted(p.name for p in self.new().glob("*.db*")),
                         ["dromond.db", "dromond.db-shm", "dromond.db-wal"])

    def test_a_real_database_survives_the_move_and_still_opens(self) -> None:
        """The same thing again with an actual SQLite file, because the point
        is not that bytes moved but that the rows are still queryable."""
        import sqlite3
        self.old().mkdir(parents=True)
        con = sqlite3.connect(self.old() / "maestro.db")
        con.execute("CREATE TABLE runs(id INTEGER PRIMARY KEY, slug TEXT)")
        con.execute("INSERT INTO runs(slug) VALUES('calm_otter')")
        con.commit()
        con.close()

        with contextlib.redirect_stderr(io.StringIO()):
            moved = paths.db_path()
        with sqlite3.connect(moved) as after:
            self.assertEqual(after.execute("SELECT slug FROM runs").fetchall(),
                             [("calm_otter",)])

    def test_the_move_happens_through_the_ordinary_path_getter(self) -> None:
        """No command has to remember to call the migration: reaching for
        state is what triggers it."""
        self.make_old_state()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(paths.home(), self.new())
        self.assertFalse(self.old().exists())
        self.assertTrue((self.new() / "dromond.db").is_file())

    def test_the_old_home_is_moved_once_and_only_once(self) -> None:
        """A second start must not move anything again — and in particular a
        directory the user recreates under the old name is THEIRS, not a
        second migration."""
        self.make_old_state()
        first = self.adopt()
        self.assertIn("moved", first)

        # A later start with the old name back on disk (a restored backup, a
        # downgraded binary that ran once) must leave it exactly where it is.
        # Nothing is reset here on purpose: the guard has to come from the
        # filesystem, not from a flag this process happens to remember.
        paths._DEPRECATED_SEEN.clear()
        self.old().mkdir()
        (self.old() / "maestro.db").write_text("a-second-database")
        second = self.adopt()
        self.assertNotIn("moved", second)
        # The refusal has to come from the destination check, not from the
        # operating system happening to reject a rename onto a full directory.
        self.assertIn("left alone", second)
        self.assertEqual((self.old() / "maestro.db").read_text(), "a-second-database")
        self.assertEqual((self.new() / "dromond.db").read_text(), "the-old-database")

    def test_both_present_uses_the_new_one_and_leaves_the_old_alone(self) -> None:
        """Two databases is a question only the human can answer. Nothing is
        merged, and the refusal says which directory was used."""
        self.make_old_state()
        self.new().mkdir()
        (self.new() / "dromond.db").write_text("the-live-database")

        said = self.adopt()
        self.assertTrue(self.old().is_dir(), "the old directory must survive untouched")
        self.assertEqual((self.old() / "maestro.db").read_text(), "the-old-database")
        self.assertEqual((self.new() / "dromond.db").read_text(), "the-live-database")
        self.assertFalse((self.new() / "maestro.db").exists(), "nothing may be merged in")
        self.assertIn(str(self.old()), said)
        self.assertIn("left alone", said)

    def test_a_failed_move_is_reported_and_never_fatal(self) -> None:
        """A cross-device rename cannot stop the command: Dromond says so and
        starts fresh, because refusing to run is worse than starting empty."""
        self.make_old_state()
        with mock.patch("dromond.paths.os.rename",
                        side_effect=OSError("Invalid cross-device link")):
            said = self.adopt()
        self.assertIn("could not move", said)
        self.assertIn("cross-device", said)
        self.assertTrue(self.old().is_dir(), "a failed move must not destroy the old state")

    # --- the config directory travels the same way --------------------------

    def test_the_config_directory_moves_with_its_neighbours(self) -> None:
        """config.toml is not alone: the 0600 Nod secrets and the legacy
        profile-notes sidecar live beside it and must arrive with it."""
        old_cfg = self.home / ".config" / "maestro"
        old_cfg.mkdir(parents=True)
        (old_cfg / "config.toml").write_text("[settings]\ntimeout = 99\n")
        (old_cfg / "nod-secrets.env").write_text("alerts_token=abc\n")

        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(paths.global_config_path(),
                             self.home / ".config" / "dromond" / "config.toml")
        new_cfg = self.home / ".config" / "dromond"
        self.assertFalse(old_cfg.exists())
        self.assertIn("timeout = 99", (new_cfg / "config.toml").read_text())
        self.assertEqual((new_cfg / "nod-secrets.env").read_text(), "alerts_token=abc\n")

    # --- the guard that protects the developer's live state -----------------

    def test_an_explicit_dromond_home_never_adopts_anything(self) -> None:
        """The guard the whole test suite depends on. A process pointed at a
        scratch directory must not swallow the real ~/.maestro — which is what
        every other test module does, all day, on a live machine."""
        self.make_old_state()
        elsewhere = self.home / "scratch"
        with mock.patch.dict(os.environ, {"DROMOND_HOME": str(elsewhere)}):
            said = self.adopt()
            self.assertEqual(paths.home(), elsewhere)
        self.assertEqual(said, "")
        self.assertTrue(self.old().is_dir())
        self.assertFalse(self.new().exists())

    def test_an_explicit_maestro_home_is_used_in_place_not_moved(self) -> None:
        """Someone who already pinned MAESTRO_HOME has said where their state
        is. The shim reads it; there is nothing to migrate."""
        self.make_old_state()
        pinned = self.home / "pinned"
        pinned.mkdir()
        with mock.patch.dict(os.environ, {"MAESTRO_HOME": str(pinned)}):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(paths.home(), pinned)
            self.assertIn("MAESTRO_HOME is deprecated", err.getvalue())
        self.assertTrue(self.old().is_dir(), "an explicit pin must not move ~/.maestro")


class EnvironmentShimTests(unittest.TestCase):
    """MAESTRO_* keeps working for one release, and says so."""

    def setUp(self) -> None:
        paths._DEPRECATED_SEEN.clear()
        self.addCleanup(paths._DEPRECATED_SEEN.clear)

    def read(self, name: str, default: str = "") -> tuple[str, str]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            value = paths.env(name, default)
        return value, err.getvalue()

    def test_a_legacy_variable_is_honoured_and_deprecated_by_name(self) -> None:
        with mock.patch.dict(os.environ, {"MAESTRO_RUN_ID": "42"}, clear=False):
            os.environ.pop("DROMOND_RUN_ID", None)
            value, said = self.read("DROMOND_RUN_ID")
        self.assertEqual(value, "42")
        # The line has to name BOTH: what is going away and what replaces it.
        self.assertIn("MAESTRO_RUN_ID", said)
        self.assertIn("DROMOND_RUN_ID", said)
        self.assertIn("deprecated", said)

    def test_the_new_name_wins_when_both_are_set(self) -> None:
        with mock.patch.dict(os.environ, {"MAESTRO_RUN_ID": "old",
                                          "DROMOND_RUN_ID": "new"}, clear=False):
            value, said = self.read("DROMOND_RUN_ID")
        self.assertEqual(value, "new")
        self.assertEqual(said, "", "nothing is deprecated when the new name was used")

    def test_an_explicitly_empty_new_name_still_wins(self) -> None:
        """Setting DROMOND_X= is how you switch the old value OFF. Falling
        through to MAESTRO_X there would make that impossible."""
        with mock.patch.dict(os.environ, {"MAESTRO_KEY": "leaked",
                                          "DROMOND_KEY": ""}, clear=False):
            value, said = self.read("DROMOND_KEY")
        self.assertEqual(value, "")
        self.assertEqual(said, "")

    def test_the_deprecation_line_is_printed_once_per_process(self) -> None:
        """A worker reads its run id on every hook invocation; one warning is
        guidance, one per call is noise that buries the run's own output."""
        with mock.patch.dict(os.environ, {"MAESTRO_RUN_ID": "42"}, clear=False):
            os.environ.pop("DROMOND_RUN_ID", None)
            _, first = self.read("DROMOND_RUN_ID")
            _, second = self.read("DROMOND_RUN_ID")
        self.assertIn("deprecated", first)
        self.assertEqual(second, "")

    def test_the_default_is_returned_when_neither_is_set(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DROMOND_RUN_ID", None)
            os.environ.pop("MAESTRO_RUN_ID", None)
            value, said = self.read("DROMOND_RUN_ID", "fallback")
        self.assertEqual(value, "fallback")
        self.assertEqual(said, "")

    def test_the_shim_reaches_the_real_call_sites(self) -> None:
        """Not a unit test of ``env`` but of the WIRING: each of these calls
        the function a command really uses, so a reader still going straight
        to ``os.environ`` fails here rather than in a user's shell."""
        from dromond import http, project, service
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with mock.patch.dict(os.environ, {"MAESTRO_KEY": "shared-secret",
                                              "MAESTRO_ROOT": str(root),
                                              "MAESTRO_HOME": str(root / "state")},
                                 clear=False):
                for new in ("DROMOND_KEY", "DROMOND_ROOT", "DROMOND_HOME"):
                    os.environ.pop(new, None)
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(http.load_key({}), "shared-secret")
                    self.assertEqual(project.start_dir(), root)
                    self.assertEqual(paths.home(), root / "state")
                    # A plist written today must carry the NEW name.
                    self.assertEqual(
                        service.build_plist()["EnvironmentVariables"]["DROMOND_HOME"],
                        str(root / "state"))


class LegacyBranchNameTests(unittest.TestCase):
    """Rule 3: a ``maestro/run-N`` branch keeps its name forever.

    The guarantee is structural — every path reads ``runs.branch`` from the
    database and nothing rebuilds a branch name from a prefix — so these
    tests pin the structure, not just one code path's behaviour.
    """

    def test_only_one_place_in_the_package_builds_a_branch_name(self) -> None:
        """If a second one ever appears, a legacy branch breaks silently.
        This is the check that makes rule 3 hold for code not yet written."""
        # A branch name being BUILT, not merely mentioned: the prefix followed
        # by an interpolation or a concatenation. A literal like the `dromond
        # merge` help text's "e.g. dromond/run-7" names no particular run and
        # reaches no repository, so it is not a construction site.
        building = re.compile(r"""dromond/(run-)?\{|["']\s*\+|\.format\(""")
        package = Path(paths.__file__).parent
        builders = []
        for source in sorted(package.glob("*.py")):
            for number, line in enumerate(source.read_text().splitlines(), 1):
                if "dromond/run-" not in line or "refs/" in line:
                    continue
                if line.lstrip().startswith("#") or not building.search(line):
                    continue
                builders.append(f"{source.name}:{number}: {line.strip()}")
        self.assertEqual(
            len(builders), 1,
            "exactly one site may construct a run branch name (worktree.create); "
            "everything else must read runs.branch from the database. Found:\n"
            + "\n".join(builders))
        self.assertIn("worktree.py", builders[0])

    def test_a_legacy_branch_merges_under_its_own_name(self) -> None:
        """End to end on a real repository: a branch created before the rename
        is merged, anchored and deleted by the name the database carries."""
        from dromond import merge
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()

            def git(*args, **kw):
                return subprocess.run(["git", "-C", str(root), *args],
                                      capture_output=True, text=True, **kw)

            subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
            git("config", "user.email", "t@example.com")
            git("config", "user.name", "T")
            (root / "seed.txt").write_text("seed\n")
            git("add", "-A")
            git("commit", "-qm", "seed")

            # The branch a pre-rename run left behind.
            git("checkout", "-q", "-b", "maestro/run-12")
            (root / "work.txt").write_text("done\n")
            git("add", "-A")
            git("commit", "-qm", "the run's work")
            head = git("rev-parse", "maestro/run-12").stdout.strip()
            git("checkout", "-q", "main")

            result = merge.merge_run(root, "maestro/run-12", settings={"checks": []})
            self.assertTrue(result["ok"], result)
            self.assertTrue(result["branch_deleted"])
            # The anchor lands in the NEW namespace, keyed on the run segment
            # of the OLD branch name — which is why run_diff reads both.
            self.assertEqual(result["kept_ref"], "refs/dromond/run-12")
            kept = git("rev-parse", "refs/dromond/run-12^{commit}").stdout.strip()
            self.assertEqual(kept, head)
            self.assertIn("done", (root / "work.txt").read_text())


if __name__ == "__main__":
    unittest.main()
