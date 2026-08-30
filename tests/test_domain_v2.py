import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from orchestra import db, fleet_config, groups


class DomainV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_file = self.root / "orchestra.db"
        self.con = db.connect(self.db_file)
        self.runtime = fleet_config.create_runtime(
            self.con, "Codex", "codex", capabilities={"interrupt": True})
        self.source = fleet_config.create_runway_source(
            self.con, "OpenAI main", "openai", account="main",
            lane="codex", adapter="codex")
        self.profile = fleet_config.create_profile(
            self.con, "Fast", self.runtime["runtime_id"], tier=1,
            runway_source=self.source["source_id"])
        self.group = groups.create(self.con, "Research", cwd=str(self.root))

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def insert_run(self, con, request_id: str, *, group_id=None,
                   cwd=None, profile_snapshot="{}", runtime_snapshot="{}",
                   **lineage) -> int:
        chosen = str(cwd or self.root)
        cur = con.execute(
            "INSERT INTO runs(request_id,group_id,profile_id,runtime_id," 
            "mission,requested_by,queued_at,cwd,cwd_source,workdir,isolation,profile_snapshot," 
            "runtime_snapshot,request_snapshot,parent_run_id,retry_of_run_id," 
            "continuation_of_run_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (request_id, group_id or self.group["group_id"],
             self.profile["profile_id"], self.runtime["runtime_id"],
             "Do the thing", "test", db.now(), chosen, "run", chosen, "auto",
             profile_snapshot, runtime_snapshot, "{}", lineage.get("parent_run_id"),
             lineage.get("retry_of_run_id"),
             lineage.get("continuation_of_run_id")),
        )
        con.commit()
        return int(cur.lastrowid)

    def test_fresh_schema_is_v2_and_has_no_work_tracking_compatibility(self):
        self.assertEqual(db.meta_get(self.con, "schema_version"), "v2")
        tables = {row[0] for row in self.con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        self.assertTrue({
            "run_groups", "runtimes", "profiles", "runway_sources",
            "runs", "messages", "attention_requests", "observer_checks",
            "artifacts", "devices", "service_tokens", "pairing_codes",
        } <= tables)
        self.assertFalse({"projects", "nod_requests", "spawn_requests"} & tables)
        self.assertNotIn("scopes", tables)
        run_columns = {row[1] for row in self.con.execute("PRAGMA table_info(runs)")}
        self.assertFalse({"project_id", "scope_id", "layer", "landing_status",
                          "landing_commit"} & run_columns)
        self.assertTrue({"cwd", "cwd_source"} <= run_columns)

    def test_general_group_and_operator_defaults_are_seeded(self):
        general = groups.find(self.con, "general")
        self.assertEqual((general["group_id"], general["name"], general["archived"]),
                         ("general", "General", 0))
        self.assertEqual(fleet_config.fleet_setting(
            self.con, "max_active_runs"), 8)
        self.assertFalse(fleet_config.fleet_setting(self.con, "paused"))
        observer = fleet_config.observer(self.con)
        self.assertEqual(
            (observer["enabled"], observer["profile_id"],
             observer["max_concurrency"]),
            (0, None, 1))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "permanent"):
            groups.set_archived(self.con, "general", True)

    def test_connect_clears_historical_subscription_cost_estimates(self):
        run_id = self.insert_run(
            self.con, "subscription-cost",
            profile_snapshot=json.dumps({"model": "xai/grok-4.6"}),
            runtime_snapshot=json.dumps({"adapter": "opencode"}))
        self.con.execute(
            "UPDATE runs SET cost_usd=? WHERE id=?", (1.25, run_id),
        )
        self.con.commit()
        self.con.close()
        self.con = db.connect(self.db_file)
        self.assertIsNone(self.con.execute(
            "SELECT cost_usd FROM runs WHERE id=?", (run_id,)).fetchone()[0])

    def test_group_sequence_is_atomic_dense_and_immutable(self):
        group_id = self.group["group_id"]

        def admit(index):
            con = db.connect(self.db_file)
            try:
                self.insert_run(con, f"concurrent-{index}")
            finally:
                con.close()

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(admit, range(24)))
        rows = self.con.execute(
            "SELECT id,group_seq FROM runs WHERE group_id=? ORDER BY group_seq",
            (group_id,),
        ).fetchall()
        self.assertEqual([row["group_seq"] for row in rows], list(range(1, 25)))
        self.assertEqual(groups.find(self.con, group_id)["last_run_seq"], 24)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self.con.execute(
                "UPDATE runs SET group_seq=99 WHERE id=?", (rows[0]["id"],))

    def test_lineage_derives_root_and_cannot_cross_group_or_cwd(self):
        root = self.insert_run(self.con, "root")
        child = self.insert_run(self.con, "child", parent_run_id=root)
        row = self.con.execute("SELECT * FROM runs WHERE id=?", (child,)).fetchone()
        self.assertEqual((row["root_run_id"], row["group_seq"]), (root, 2))
        other = groups.create(self.con, "Other")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "lineage"):
            self.insert_run(self.con, "cross-group", group_id=other["group_id"],
                            parent_run_id=root)
        self.con.rollback()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "lineage"):
            other_cwd = self.root / "other"
            other_cwd.mkdir()
            self.insert_run(self.con, "cross-cwd", cwd=other_cwd,
                            parent_run_id=root)
        self.con.rollback()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "lineage"):
            self.con.execute(
                "INSERT INTO runs(request_id,group_id,profile_id,runtime_id," 
                "mission,requested_by,queued_at,cwd,cwd_source,workdir,isolation,profile_snapshot," 
                "runtime_snapshot,request_snapshot,parent_run_id,retry_of_run_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("two-predecessors", self.group["group_id"],
                 self.profile["profile_id"], self.runtime["runtime_id"], "Do it",
                 "test", db.now(), str(self.root), "run", str(self.root), "auto",
                 "{}", "{}", "{}", root, root),
            )

    def test_archived_group_blocks_roots_but_not_existing_lineage(self):
        root = self.insert_run(self.con, "root")
        groups.set_archived(self.con, self.group["group_id"], True)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "archived group"):
            self.insert_run(self.con, "new-root")
        self.con.rollback()
        child = self.insert_run(self.con, "existing-child", parent_run_id=root)
        self.assertEqual(self.con.execute(
            "SELECT group_seq FROM runs WHERE id=?", (child,)
        ).fetchone()[0], 2)

    def test_group_cwd_is_canonical_write_only_configuration(self):
        linked = self.root / "linked"
        linked.symlink_to(self.root, target_is_directory=True)
        changed = groups.set_cwd(self.con, self.group["group_id"], str(linked))
        self.assertEqual(changed["default_cwd"], str(self.root.resolve()))
        cleared = groups.set_cwd(self.con, self.group["group_id"], None)
        self.assertIsNone(cleared["default_cwd"])
        with self.assertRaisesRegex(ValueError, "directory"):
            groups.set_cwd(self.con, self.group["group_id"], str(self.root / "missing"))

    def test_profile_runtime_runway_and_observer_are_managed(self):
        first_revision = self.profile["revision"]
        changed = fleet_config.update_profile(
            self.con, self.profile["profile_id"], {"effort": "low", "note": "cheap"},
            expected_revision=first_revision)
        self.assertEqual((changed["effort"], changed["revision"]),
                         ("low", first_revision + 1))
        with self.assertRaisesRegex(RuntimeError, "changed since"):
            fleet_config.update_profile(
                self.con, self.profile["profile_id"], {"note": "stale"},
                expected_revision=first_revision)
        observer_runtime = fleet_config.create_runtime(
            self.con, "Claude Observer", "claude")
        observer_profile = fleet_config.create_profile(
            self.con, "Observer", observer_runtime["runtime_id"], tier=1)
        with self.assertRaisesRegex(ValueError, "tool-free Observer"):
            fleet_config.configure_observer(
                self.con, enabled=True, profile=self.profile["profile_id"])
        configured = fleet_config.configure_observer(
            self.con, enabled=True, profile=observer_profile["profile_id"],
            max_concurrency=3)
        self.assertEqual(
            (configured["enabled"], configured["profile_id"],
             configured["max_concurrency"]),
            (1, observer_profile["profile_id"], 3))
        for invalid in (0, 9, True, 1.5, "2"):
            with self.subTest(observer_concurrency=invalid), self.assertRaises(
                    ValueError):
                fleet_config.configure_observer(
                    self.con, enabled=True,
                    profile=observer_profile["profile_id"],
                    max_concurrency=invalid)
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "UPDATE observer_settings SET max_concurrency=9 WHERE singleton=1")
        self.con.rollback()

    def test_managed_config_rejects_secret_bearing_fields(self):
        with self.assertRaisesRegex(ValueError, "secret-bearing"):
            fleet_config.update_profile(
                self.con, self.profile["profile_id"],
                {"config": {"nested": {"api_key": "plaintext"}}})
        with self.assertRaisesRegex(ValueError, "secret-bearing"):
            fleet_config.create_runtime(
                self.con, "Unsafe", "exec", command=["unsafe"],
                config={"password": "plaintext"})
        with self.assertRaisesRegex(ValueError, "raw JSON"):
            fleet_config.update_profile(
                self.con, self.profile["profile_id"],
                {"config_json": '{"api_key":"plaintext"}'})

    def test_runway_identity_and_settings_revisions_are_constrained(self):
        with self.assertRaises(sqlite3.IntegrityError):
            fleet_config.create_runway_source(
                self.con, "Duplicate lane", "openai", account="main",
                lane="codex", adapter="codex")
        setting = fleet_config.set_fleet_setting(
            self.con, "max_active_runs", 12, expected_revision=1)
        self.assertEqual((json.loads(setting["value_json"]), setting["revision"]),
                         (12, 2))
        with self.assertRaisesRegex(RuntimeError, "changed since"):
            fleet_config.set_fleet_setting(
                self.con, "max_active_runs", 16, expected_revision=1)


class CleanBreakTests(unittest.TestCase):
    def test_non_v2_database_is_refused_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.db"
            con = sqlite3.connect(path)
            con.execute("CREATE TABLE projects(id TEXT)")
            con.commit()
            con.close()
            with self.assertRaisesRegex(RuntimeError, "not a v2 database"):
                db.connect(path)
            probe = sqlite3.connect(path)
            self.assertEqual(probe.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall(), [("projects",)])
            probe.close()


if __name__ == "__main__":
    unittest.main()
