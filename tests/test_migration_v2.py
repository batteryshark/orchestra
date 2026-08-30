import tempfile
import unittest
import sqlite3
import json
import hashlib
from pathlib import Path

from orchestra import db
from orchestra.migration import (
    archive_legacy, apply_operator_import, operator_import_plan,
    retire_legacy_hooks,
)


class ArchiveTests(unittest.TestCase):
    def test_live_v2_restart_collapses_scopes_without_losing_runs_or_numbering(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            database = root / "live-v2.db"
            con = db.connect(database)
            now = db.now()
            con.execute(
                "INSERT INTO runtimes(runtime_id,slug,name,adapter,created_at,updated_at) "
                "VALUES('runtime','runtime','Runtime','exec',?,?)", (now, now))
            con.execute(
                "INSERT INTO profiles(profile_id,slug,name,runtime_id,tier,created_at,"
                "updated_at) VALUES('profile','profile','Profile','runtime',1,?,?)",
                (now, now))
            con.execute(
                "INSERT INTO runs(request_id,profile_id,runtime_id,mission,requested_by,"
                "queued_at,cwd,cwd_source,workdir,isolation,profile_snapshot,"
                "runtime_snapshot,request_snapshot) VALUES('old-run','profile','runtime',"
                "'Keep me','test',?,?,'run',?,'auto','{}','{}','{}')",
                (now, str(root), str(root)))
            con.commit()
            run_id = con.execute(
                "SELECT id FROM runs WHERE request_id='old-run'").fetchone()[0]
            con.close()

            legacy = sqlite3.connect(database)
            legacy.execute("PRAGMA foreign_keys=OFF")
            legacy.execute("DROP TRIGGER validate_run_lineage")
            legacy.execute("DROP TRIGGER immutable_run_identity")
            legacy.execute("ALTER TABLE run_groups DROP COLUMN default_cwd")
            legacy.execute("ALTER TABLE runs DROP COLUMN cwd_source")
            legacy.execute("ALTER TABLE runs DROP COLUMN cwd")
            legacy.execute(
                "CREATE TABLE scopes(scope_id TEXT PRIMARY KEY,slug TEXT,name TEXT,"
                "root TEXT,kind TEXT,archived INTEGER,revision INTEGER,created_at TEXT,"
                "updated_at TEXT)")
            legacy.execute(
                "INSERT INTO scopes VALUES('legacy','legacy','Legacy',?,'directory',"
                "0,1,?,?)", (str(root), now, now))
            legacy.execute("CREATE TABLE scope_profiles(scope_id TEXT,profile_id TEXT)")
            legacy.execute("ALTER TABLE runs ADD COLUMN scope_id TEXT")
            legacy.execute("UPDATE runs SET scope_id='legacy'")
            legacy.execute("CREATE INDEX idx_runs_scope ON runs(scope_id,id)")
            legacy.execute(
                "CREATE TRIGGER validate_run_lineage BEFORE INSERT ON runs "
                "BEGIN SELECT 1; END")
            legacy.execute(
                "CREATE TRIGGER immutable_run_identity BEFORE UPDATE ON runs "
                "BEGIN SELECT 1; END")
            legacy.commit()
            legacy.close()

            migrated = db.connect(database)
            self.assertEqual(tuple(migrated.execute(
                "SELECT id,group_seq,cwd,cwd_source FROM runs WHERE request_id='old-run'"
            ).fetchone()), (run_id, 1, str(root), "group"))
            general = migrated.execute(
                "SELECT last_run_seq,default_cwd FROM run_groups WHERE group_id='general'"
            ).fetchone()
            self.assertEqual(tuple(general), (1, str(root)))
            self.assertFalse(migrated.execute(
                "SELECT 1 FROM sqlite_master WHERE name IN ('scopes','scope_profiles')"
            ).fetchall())
            self.assertNotIn("scope_id", {
                row[1] for row in migrated.execute("PRAGMA table_info(runs)")})
            migrated.close()

    def test_dry_run_does_not_move_legacy_state(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "orchestra.db").write_text("old")
            report = archive_legacy(base=root, stamp="fixed")
            self.assertTrue((root / "orchestra.db").exists())
            self.assertFalse(Path(report["archive"]).exists())

    def test_execute_moves_only_known_state_and_writes_manifest(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "logs").mkdir()
            (root / "logs" / "run.log").write_text("history")
            (root / "hooks").mkdir()
            (root / "hooks" / "orchestra-opencode.js").write_text("legacy")
            (root / "keep.txt").write_text("mine")
            report = archive_legacy(execute=True, base=root, stamp="fixed")
            archive = Path(report["archive"])
            self.assertEqual((archive / "logs" / "run.log").read_text(), "history")
            self.assertEqual((archive / "hooks" /
                              "orchestra-opencode.js").read_text(), "legacy")
            self.assertTrue((archive / "manifest.json").is_file())
            self.assertTrue((root / "keep.txt").is_file())

    def test_retire_hooks_removes_only_exact_orchestra_v1_entries(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            claude = root / "claude.json"
            codex = root / "hooks.json"
            reasonix = root / "reasonix.json"
            unrelated = {"type": "command", "command": "my-hook"}
            claude.write_text(json.dumps({"hooks": {"SessionStart": [
                {"hooks": [
                    {"type": "command",
                     "command": "orchestra hook --backend claude --bind"},
                    unrelated,
                ]},
            ]}}))
            reasonix.write_text(json.dumps({"hooks": {"Stop": [
                {"type": "command",
                 "command": "orchestra hook --backend reasonix"},
                unrelated,
            ]}}))
            codex_handler = {
                "type": "command",
                "command": "orchestra hook --backend codex --bind",
                "timeout": 10,
            }
            codex.write_text(json.dumps({"hooks": {"SessionStart": [
                {"hooks": [codex_handler]},
            ]}}))
            key = f"{codex.resolve()}:sessionstart:0:0"
            digest = "sha256:" + hashlib.sha256(json.dumps(
                codex_handler, sort_keys=True, separators=(",", ":")
            ).encode()).hexdigest()
            (root / "config.toml").write_text(
                f'[hooks.state."{key}"]\n'
                "enabled = true\n"
                f'trusted_hash = "{digest}"\n\n'
                "[unrelated]\nkeep = true\n")
            configs = [("claude", claude), ("codex", codex),
                       ("reasonix", reasonix)]

            planned = retire_legacy_hooks(configs=configs)
            self.assertEqual([item["found"] for item in planned], [1, 1, 1])
            self.assertEqual(planned[1]["trust_found"], 1)
            self.assertIn("orchestra hook", claude.read_text())

            applied = retire_legacy_hooks(execute=True, configs=configs)
            self.assertEqual([item["removed"] for item in applied], [1, 1, 1])
            self.assertEqual(applied[1]["trust_removed"], 1)
            for path in (claude, codex, reasonix):
                value = json.loads(path.read_text())
                encoded = json.dumps(value)
                self.assertNotIn("orchestra hook", encoded)
            self.assertIn("my-hook", claude.read_text())
            self.assertIn("my-hook", reasonix.read_text())
            trust = (root / "config.toml").read_text()
            self.assertNotIn("hooks.state", trust)
            self.assertIn("[unrelated]\nkeep = true", trust)

    def test_operator_import_excludes_secrets_and_all_execution_history(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = root / "config.toml"
            config.write_text("""
[settings]
max_active_runs = 4
observer_profile = "quick"

[profiles.quick]
backend = "codex"
model = "gpt-test"
tier = 1
env = { API_KEY = "never-copy", ENDPOINT = "http://localhost" }
""")
            old = root / "old.db"
            legacy = sqlite3.connect(old)
            legacy.execute("CREATE TABLE projects(project_id,name,root,archived)")
            legacy.execute("INSERT INTO projects VALUES('p','Repo',?,0)", (raw,))
            legacy.execute("CREATE TABLE runs(id,status)")
            legacy.execute("INSERT INTO runs VALUES(99,'done')")
            legacy.commit()
            legacy.close()
            plan = operator_import_plan(config, old)
            self.assertNotIn("never-copy", str(plan))
            self.assertTrue(any("secret-bearing" in item for item in plan["dropped"]))
            con = db.connect(":memory:")
            applied = apply_operator_import(con, plan)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)
            self.assertEqual(con.execute(
                "SELECT value_json FROM fleet_settings WHERE key='max_active_runs'"
            ).fetchone()[0], "4")
            self.assertEqual(len(applied["profiles"]), 1)
            self.assertFalse(applied["observer"])
            self.assertTrue(any(
                "codex runtime cannot provide a tool-free Observer" in item
                for item in plan["dropped"]))
            con.close()

    def test_operator_import_drops_unknown_and_acp_legacy_harnesses(self):
        with tempfile.TemporaryDirectory() as raw:
            config = Path(raw) / "config.toml"
            config.write_text("""
[profiles.good]
backend = "codex"
tier = 2

[profiles.unknown]
backend = "homegrown"

[profiles.fake_acp]
backend = "claude"
transport = "acp"
""")
            plan = operator_import_plan(config)
            self.assertEqual([item["adapter"] for item in plan["runtimes"]],
                             ["codex"])
            self.assertEqual([item["slug"] for item in plan["profiles"]],
                             ["good"])
            self.assertTrue(any(
                "unsupported legacy backend 'homegrown'" in reason
                for reason in plan["dropped"]))
            self.assertTrue(any(
                "ACP transport requires an explicit v2 argv runtime" in reason
                for reason in plan["dropped"]))

            con = db.connect(":memory:")
            applied = apply_operator_import(con, plan)
            self.assertEqual(len(applied["runtimes"]), 1)
            self.assertEqual(len(applied["profiles"]), 1)
            con.close()

    def test_operator_import_reads_roots_from_current_v1_runs(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = root / "config.toml"
            config.write_text("[profiles.quick]\nbackend = \"codex\"\n")
            repo = root / "repo"
            repo.mkdir()
            (repo / ".git").mkdir()
            archived_workspace = root / "projects" / "old" / "workspace"
            archived_workspace.mkdir(parents=True)
            old = root / "orchestra.db"
            legacy = sqlite3.connect(old)
            legacy.execute(
                "CREATE TABLE projects(project_id TEXT,name TEXT,archived INTEGER)")
            legacy.execute(
                "CREATE TABLE runs(id INTEGER,project_id TEXT,repo TEXT,started_at TEXT)")
            legacy.executemany("INSERT INTO projects VALUES(?,?,0)", [
                ("repo", "Repo"), ("managed", "Managed")])
            legacy.executemany("INSERT INTO runs VALUES(?,?,?,?)", [
                (1, "repo", str(repo), "2026-01-01"),
                (2, "managed", str(archived_workspace), "2026-01-02")])
            legacy.commit()
            legacy.close()

            plan = operator_import_plan(config, old)

            self.assertEqual([group["name"] for group in plan["groups"]], ["Repo"])
            self.assertEqual(plan["groups"][0]["cwd"], str(repo.resolve()))


if __name__ == "__main__":
    unittest.main()
