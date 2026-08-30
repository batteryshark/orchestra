import json
import os
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from orchestra import (daemon, db, fleet_config, groups, messaging, observer,
                       runs, scheduler)
from orchestra.contracts import RunRequest


class ObserverExecutionV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.cwd_root = self.root / "cwd"
        self.cwd_root.mkdir()
        self.env = patch.dict(os.environ, {
            "ORCHESTRA_HOME": str(self.state),
            "ORCHESTRA_RUN_TOKEN": "must-not-leak",
        })
        self.env.start()
        self.con = db.connect()

    def tearDown(self):
        self.con.close()
        self.env.stop()
        self.temp.cleanup()

    def configured_run(self, _source: str = ""):
        runtime_row = fleet_config.create_runtime(
            self.con, "Observer Claude", "claude", slug="observer-claude")
        profile = fleet_config.create_profile(
            self.con, "Observer", runtime_row["runtime_id"],
            slug="observer", tier=1, timeout_seconds=2,
            env={"ORCHESTRA_DEBUG": "must-not-leak"})
        group = groups.create(
            self.con, "Observer tests", slug="observer-tests",
            cwd=str(self.cwd_root))
        run, _ = runs.submit(self.con, RunRequest.from_mapping({
            "request_id": "observed-run", "group": group["slug"],
            "profile": profile["slug"], "context": "Stay on mission",
        }))
        self.con.execute(
            "UPDATE runs SET status='running',started_at='2020-01-01T00:00:00Z' "
            "WHERE id=?", (run["id"],))
        self.con.executemany(
            "INSERT INTO events(run_id,seq,kind,name,payload,created_at) "
            "VALUES(?,?,'progress','step','working',?)",
            ((run["id"], seq, db.now()) for seq in range(1, 6)))
        self.con.commit()
        fleet_config.configure_observer(
            self.con, enabled=True, profile=profile["profile_id"],
            first_look_seconds=1, minimum_events=5, interval_seconds=60)
        return run, profile, runtime_row

    def prepare(self, run, profile, runtime_row):
        profile_snapshot, runtime_snapshot = daemon._observer_snapshots(
            profile, runtime_row)
        return observer.prepare_check(
            self.con, run["id"], profile_id=profile["profile_id"],
            profile_snapshot=profile_snapshot,
            runtime_snapshot=runtime_snapshot)

    def additional_run(self, profile, request_id: str, *, running: bool):
        run, _ = runs.submit(self.con, RunRequest.from_mapping({
            "request_id": request_id, "group": "observer-tests",
            "profile": profile["slug"], "context": request_id,
        }))
        if running:
            self.con.execute(
                "UPDATE runs SET status='running',"
                "started_at='2020-01-01T00:00:00Z' WHERE id=?", (run["id"],))
            self.con.executemany(
                "INSERT INTO events(run_id,seq,kind,name,payload,created_at) "
                "VALUES(?,?,'progress','step','working',?)",
                ((run["id"], seq, db.now()) for seq in range(1, 6)))
            self.con.commit()
        return runs.find(self.con, run["id"])

    def wait_check(self, run_id: int, timeout: float = 5):
        deadline = time.monotonic() + timeout
        check = None
        while time.monotonic() < deadline:
            check = self.con.execute(
                "SELECT * FROM observer_checks WHERE run_id=? ORDER BY id DESC",
                (run_id,)).fetchone()
            if check is not None and check["finished_at"] is not None:
                return check
            time.sleep(0.02)
        self.fail(f"Observer check for run {run_id} did not finish")

    @staticmethod
    def fake_claude(source: str):
        def build(profile, **_kwargs):
            return [sys.executable, "-c", source,
                    *profile.get("extra_args", [])]
        return build

    def test_tick_only_persists_and_launches_one_frozen_check(self):
        run, _, _ = self.configured_run("import time; time.sleep(30)")
        launched = []
        started = time.monotonic()

        observed = daemon._run_observer(
            self.con, launcher=lambda check_id: launched.append(check_id))

        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(observed, [run["id"]])
        self.assertEqual(len(launched), 1)
        check = observer.find_check(self.con, launched[0])
        self.assertIsNone(check["finished_at"])
        self.assertEqual(json.loads(check["input_json"])["event_count"], 5)
        profile_snapshot = json.loads(check["profile_snapshot"])
        runtime_snapshot = json.loads(check["runtime_snapshot"])
        self.assertEqual(profile_snapshot["env"], {})
        self.assertEqual(runtime_snapshot["config"].get("env"), None)
        with self.assertRaises(observer.CheckNotDue):
            observer.prepare_check(self.con, run["id"])

    def test_configured_cap_fills_multiple_slots_without_using_worker_capacity(self):
        first, profile, _ = self.configured_run("import time; time.sleep(30)")
        second = self.additional_run(profile, "observed-run-2", running=True)
        queued = self.additional_run(profile, "queued-worker", running=False)
        fleet_config.configure_observer(
            self.con, enabled=True, profile=profile["profile_id"],
            max_concurrency=2, first_look_seconds=1, minimum_events=5,
            interval_seconds=60)
        launched = []

        observed = daemon._run_observer(
            self.con, launcher=lambda check_id: launched.append(check_id))

        self.assertEqual(observed, [first["id"], second["id"]])
        self.assertEqual(len(launched), 2)
        self.assertEqual(len(observer.active_checks(self.con)), 2)
        with self.assertRaisesRegex(observer.CheckNotDue, "already active"):
            observer.prepare_check(
                self.con, first["id"], max_concurrency=2)

        # Observer has its own lane: two active checks do not consume the one
        # remaining worker slot.
        fleet_config.set_fleet_setting(self.con, "max_active_runs", 3)
        admitted = scheduler.admit(self.con)
        self.assertIn(queued["id"], admitted["admitted"])

        observer.finish_check(
            self.con, launched[0],
            {"action": "ok", "reason": "healthy", "message": ""})
        self.con.execute(
            "UPDATE runs SET status='running',started_at=? WHERE id=?",
            ("2020-01-01T00:00:00Z", queued["id"]))
        self.con.executemany(
            "INSERT INTO events(run_id,seq,kind,name,payload,created_at) "
            "VALUES(?,?,'progress','step','working',?)",
            ((queued["id"], seq, db.now()) for seq in range(1, 6)))
        self.con.commit()

        resumed = daemon._run_observer(
            self.con, launcher=lambda check_id: launched.append(check_id))

        self.assertEqual(resumed, [queued["id"]])
        self.assertEqual(len(observer.active_checks(self.con)), 2)
        self.assertEqual(launched[-2], launched[1])  # recovered second check

    def test_concurrent_admission_serializes_at_the_configured_cap(self):
        first, profile, _ = self.configured_run("print('unused')")
        second = self.additional_run(profile, "observed-run-2", running=True)
        barrier = threading.Barrier(2)

        def attempt(run_id):
            con = db.connect()
            try:
                barrier.wait(timeout=2)
                try:
                    return observer.prepare_check(
                        con, run_id, max_concurrency=1)["check_id"]
                except observer.CheckNotDue:
                    return None
            finally:
                con.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            admitted = list(pool.map(attempt, (first["id"], second["id"])))

        self.assertEqual(sum(item is not None for item in admitted), 1)
        self.assertEqual(len(observer.active_checks(self.con)), 1)

    def test_supported_plans_are_tool_free_and_secret_free(self):
        self.assertEqual(set(daemon._OBSERVER_ARGS),
                         set(fleet_config.OBSERVER_ADAPTERS))
        profile = {
            "name": "Observer", "timeout_seconds": 2,
            "env": {"PROFILE_SECRET": "profile-value"},
            "config": {
                "env": {"CONFIG_SECRET": "config-value"},
                "extra_args": ["--dangerously-skip-permissions", "--yolo"],
                "add_dirs": [str(self.cwd_root)],
            },
        }
        home = self.root / "home"
        home.mkdir()
        reasonix_home = self.root / "reasonix-user"
        reasonix_home.mkdir()
        (reasonix_home / "config.toml").write_text(
            'default_model = "observer/model"\n'
            '[[providers]]\nname = "observer"\nkind = "openai"\n'
            'base_url = "https://example.invalid/v1"\n'
            'models = ["model"]\ndefault = "model"\n'
            'api_key_env = "OBSERVER_API_KEY"\nweb_search = true\n',
            encoding="utf-8")
        credential_store = reasonix_home / ".env"
        credential_store.write_text(
            "OBSERVER_API_KEY=selected-value\n"
            "OTHER_PROVIDER_KEY=must-not-leak\n", encoding="utf-8")
        os.chmod(credential_store, 0o600)
        inherited = {
            "PATH": os.environ.get("PATH", os.defpath), "HOME": str(home),
            "LANG": "en_US.UTF-8", "ORCHESTRA_TOKEN": "run-value",
            "DAEMON_SECRET": "daemon-value", "ANTHROPIC_API_KEY": "api-value",
            "HTTPS_PROXY": "https://credential@example.invalid",
            "REASONIX_HOME": str(reasonix_home),
        }
        plans = {}
        with patch.dict(os.environ, inherited, clear=True):
            for adapter in fleet_config.OBSERVER_ADAPTERS:
                runtime_row = {
                    "adapter": adapter,
                    "config": {"env": {"RUNTIME_SECRET": "runtime-value"}},
                }
                with tempfile.TemporaryDirectory(dir=self.root) as directory:
                    plan = daemon._observer_plan(
                        profile, runtime_row, "--auto", 7, directory)
                    plans[adapter] = (plan, directory)
                    self.assertEqual(plan.env["HOME"], str(home))
                    for key in ("ORCHESTRA_TOKEN", "DAEMON_SECRET",
                                "ANTHROPIC_API_KEY", "HTTPS_PROXY",
                                "PROFILE_SECRET", "CONFIG_SECRET",
                                "RUNTIME_SECRET", "OTHER_PROVIDER_KEY"):
                        self.assertNotIn(key, plan.env)
                    self.assertNotIn("--yolo", plan.argv)
                    self.assertNotIn("--dangerously-skip-permissions", plan.argv)

                    if adapter == "reasonix":
                        config_text = (Path(directory) / "reasonix.toml").read_text()
                        parsed_config = tomllib.loads(config_text)
                        self.assertIn("enabled = false", config_text)
                        self.assertIn("disable_implicit_invocation = true", config_text)
                        self.assertIn('api_key_env = "OBSERVER_API_KEY"', config_text)
                        self.assertFalse(parsed_config["providers"][0]["web_search"])

        claude = plans["claude"][0]
        self.assertIn("--safe-mode", claude.argv)
        self.assertIn("--disable-slash-commands", claude.argv)
        self.assertEqual(
            claude.argv[claude.argv.index("--tools") + 1], "")
        self.assertEqual(
            claude.argv[claude.argv.index("--mcp-config") + 1], "{}")

        reasonix = plans["reasonix"][0]
        self.assertEqual(reasonix.argv[1], "--print")
        self.assertNotIn("run", reasonix.argv[:2])
        self.assertEqual(
            reasonix.argv[reasonix.argv.index("--allowed-tools") + 1], "")
        self.assertIn("all", reasonix.argv)
        self.assertEqual(reasonix.env["OBSERVER_API_KEY"], "selected-value")
        self.assertTrue(reasonix.env["REASONIX_HOME"].startswith(
            plans["reasonix"][1]))
        self.assertNotEqual(reasonix.env["REASONIX_HOME"], str(reasonix_home))

        opencode = plans["opencode"][0]
        self.assertIn("--pure", opencode.argv)
        self.assertEqual(opencode.argv[-1], "--auto")  # prompt, not auto mode
        self.assertEqual(opencode.argv.count("--auto"), 1)
        self.assertEqual(json.loads(opencode.env["OPENCODE_PERMISSION"]),
                         {"*": "deny"})
        safe_config = json.loads(opencode.env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(safe_config["permission"], {"*": "deny"})
        self.assertEqual(safe_config["mcp"], {})
        self.assertEqual(safe_config["plugin"], [])
        self.assertEqual(safe_config["instructions"], [])
        for key in ("OPENCODE_CONFIG_DIR", "OPENCODE_DB", "XDG_CACHE_HOME"):
            self.assertTrue(opencode.env[key].startswith(plans["opencode"][1]))

    def test_uncontainable_adapters_are_rejected_before_command_building(self):
        profile = {"name": "Observer", "config": {}, "env": {}}
        for adapter in ("codex", "exec", "acp"):
            with self.subTest(adapter=adapter), tempfile.TemporaryDirectory(
                    dir=self.root) as directory, patch(
                        "orchestra.runtime.launch_plan") as launch:
                with self.assertRaisesRegex(
                        RuntimeError, "cannot provide a tool-free Observer"):
                    daemon._observer_plan(
                        profile, {"adapter": adapter}, "evidence", 1, directory)
                launch.assert_not_called()

    def test_observer_child_gets_only_bounded_environment_and_prompt(self):
        source = (
            "import json,os,sys;"
            "print(json.dumps({'type':'result','result':json.dumps({"
            "'env':dict(os.environ),'cwd':os.getcwd(),'argv':sys.argv[1:]})}))"
        )
        profile = {
            "name": "Observer", "timeout_seconds": 2,
            "env": {"PROFILE_SECRET": "profile-value"},
            "config": {"env": {"CONFIG_SECRET": "config-value"},
                       "extra_args": ["--dangerously-skip-permissions"]},
        }
        runtime_row = {
            "adapter": "claude",
            "config": {"env": {"RUNTIME_SECRET": "runtime-value"}},
        }
        secret_file = self.state / "v2" / "secrets.json"
        secret_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        secret_file.write_text('{"FILE_SECRET":"file-value"}', encoding="utf-8")
        os.chmod(secret_file, 0o600)
        log_path = self.state / "v2" / "logs" / "observer-env.jsonl"
        child_env = {
            "DAEMON_SECRET": "daemon-value", "ANTHROPIC_API_KEY": "api-value",
            "HTTPS_PROXY": "https://credential@example.invalid",
            "ORCHESTRA_TOKEN": "daemon-token",
        }

        with patch.dict(os.environ, child_env), patch(
                "orchestra.runners.build_cmd", side_effect=self.fake_claude(source)), \
                patch("orchestra.config.worker_environment",
                      side_effect=AssertionError("worker environment used")):
            output, _ = daemon._observer_turn(
                profile, runtime_row, "bounded evidence", 8, str(log_path))

        seen = json.loads(output)
        self.assertTrue(Path(seen["cwd"]).name.startswith("orchestra-observer-"))
        self.assertNotEqual(Path(seen["cwd"]), self.cwd_root)
        for key in ("DAEMON_SECRET", "FILE_SECRET", "ANTHROPIC_API_KEY",
                    "HTTPS_PROXY", "ORCHESTRA_TOKEN", "ORCHESTRA_HOME",
                    "ORCHESTRA_RUN_TOKEN", "PROFILE_SECRET", "CONFIG_SECRET",
                    "RUNTIME_SECRET"):
            self.assertNotIn(key, seen["env"])
        self.assertIn("--safe-mode", seen["argv"])
        self.assertEqual(
            seen["argv"][seen["argv"].index("--tools") + 1], "")

    def test_reasonix_child_gets_only_its_selected_provider_credential(self):
        source_home = self.root / "reasonix-source"
        source_home.mkdir()
        (source_home / "config.toml").write_text(
            'default_model = "remote/model"\n'
            '[[providers]]\nname = "remote"\nkind = "openai"\n'
            'base_url = "https://example.invalid/v1"\nmodels = ["model"]\n'
            'default = "model"\napi_key_env = "REMOTE_API_KEY"\n',
            encoding="utf-8")
        credential_store = source_home / ".env"
        credential_store.write_text(
            "REMOTE_API_KEY=selected-value\n"
            "UNRELATED_API_KEY=must-not-leak\n", encoding="utf-8")
        os.chmod(credential_store, 0o600)
        source = (
            "import json,os,sys;from pathlib import Path;"
            "print(json.dumps({'type':'result','result':json.dumps({"
            "'env':dict(os.environ),'cwd':os.getcwd(),'argv':sys.argv[1:],"
            "'config':Path('reasonix.toml').read_text()})}))"
        )

        def resolve(command):
            self.assertEqual(command[:2], ["reasonix", "--print"])
            return [sys.executable, "-c", source, *command]

        log_path = self.state / "v2" / "logs" / "observer-reasonix.jsonl"
        with patch.dict(os.environ, {
                "REASONIX_HOME": str(source_home),
                "DAEMON_SECRET": "daemon-value",
                "UNRELATED_API_KEY": "environment-value",
        }), patch("orchestra.proc.resolve_cmd", side_effect=resolve):
            output, _ = daemon._observer_turn(
                {"name": "Observer", "model": "remote/model",
                 "timeout_seconds": 2,
                 "env": {"PROFILE_SECRET": "profile-value"}, "config": {}},
                {"adapter": "reasonix",
                 "config": {"env": {"RUNTIME_SECRET": "runtime-value"}}},
                "bounded evidence", 9, str(log_path))

        seen = json.loads(output)
        self.assertEqual(seen["env"]["REMOTE_API_KEY"], "selected-value")
        for key in ("UNRELATED_API_KEY", "DAEMON_SECRET", "PROFILE_SECRET",
                    "RUNTIME_SECRET", "ORCHESTRA_HOME", "ORCHESTRA_RUN_TOKEN"):
            self.assertNotIn(key, seen["env"])
        self.assertEqual(Path(seen["env"]["REASONIX_HOME"]).resolve().parent,
                         Path(seen["cwd"]).resolve())
        self.assertNotEqual(seen["env"]["REASONIX_HOME"], str(source_home))
        self.assertEqual(seen["argv"][:2], ["reasonix", "--print"])
        self.assertEqual(
            seen["argv"][seen["argv"].index("--allowed-tools") + 1], "")
        self.assertNotIn("selected-value", seen["config"])
        self.assertNotIn("must-not-leak", seen["config"])

    def test_observe_persists_identity_with_a_contained_runtime(self):
        source = (
            "import json; print(json.dumps({'type':'result','result':"
            "json.dumps({'action':'ok','reason':'contained','message':''})}))")
        run, profile, runtime_row = self.configured_run()
        prepared = self.prepare(run, profile, runtime_row)
        log_path = self.state / "v2" / "logs" / "observer-test.jsonl"
        self.con.execute(
            "UPDATE observer_checks SET supervisor_pid=?,"
            "supervisor_pid_identity='test',log_path=? WHERE id=?",
            (os.getpid(), str(log_path), prepared["check_id"]))
        self.con.commit()

        with patch("orchestra.daemon._claim_observer", return_value=True), patch(
                "orchestra.runners.build_cmd", side_effect=self.fake_claude(source)):
            result = daemon.observe(prepared["check_id"])

        self.assertEqual(result, 0)
        check = observer.find_check(self.con, prepared["check_id"])
        self.assertEqual(check["action"], "ok")
        self.assertIsNotNone(check["worker_pid"])
        self.assertIsNotNone(check["worker_pid_identity"])
        self.assertIsNotNone(check["finished_at"])
        self.assertTrue(log_path.is_file())

    def test_real_detached_supervisor_rejects_an_unsafe_frozen_adapter(self):
        run, profile, runtime_row = self.configured_run()
        prepared = self.prepare(run, profile, runtime_row)
        self.con.execute(
            "UPDATE observer_checks SET runtime_snapshot=? WHERE id=?",
            (json.dumps({"adapter": "exec", "command": [sys.executable, "-c",
                         "print('must not launch')"], "config": {}}),
             prepared["check_id"]))
        self.con.commit()

        daemon.spawn_observer(prepared["check_id"])
        check = self.wait_check(run["id"])

        self.assertEqual(check["action"], "error")
        self.assertIn("cannot provide a tool-free Observer", check["error"])
        self.assertIsNotNone(check["supervisor_pid_identity"])
        self.assertIsNone(check["worker_pid"])
        self.assertIsNone(check["worker_pid_identity"])

    def test_observer_runtime_timeout_is_bounded_and_terminal(self):
        run, profile, runtime_row = self.configured_run()
        prepared = self.prepare(run, profile, runtime_row)
        log_path = self.state / "v2" / "logs" / "observer-timeout.jsonl"
        self.con.execute(
            "UPDATE observer_checks SET supervisor_pid=?,"
            "supervisor_pid_identity='test',log_path=? WHERE id=?",
            (os.getpid(), str(log_path), prepared["check_id"]))
        self.con.commit()

        started = time.monotonic()
        with patch("orchestra.daemon._claim_observer", return_value=True), patch(
                "orchestra.runners.build_cmd",
                side_effect=self.fake_claude("import time; time.sleep(30)")):
            result = daemon.observe(prepared["check_id"])
        check = observer.find_check(self.con, prepared["check_id"])

        self.assertEqual(result, 1)
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(check["action"], "error")
        self.assertIn("timed out after 2 seconds", check["error"])
        state, _ = daemon._observer_process_state(
            check["worker_pid"], check["worker_pid_identity"])
        self.assertEqual(state, "gone")

    def test_recovery_fails_closed_when_worker_identity_is_unreadable(self):
        run, profile, runtime_row = self.configured_run("print('unused')")
        prepared = self.prepare(run, profile, runtime_row)
        self.con.execute(
            "UPDATE observer_checks SET supervisor_pid=101,"
            "supervisor_pid_identity='supervisor',worker_pid=202,"
            "worker_pid_identity='worker' WHERE id=?", (prepared["check_id"],))
        self.con.commit()

        with patch("orchestra.daemon._observer_process_state", side_effect=[
                ("gone", "supervisor gone"),
                ("refused", "worker identity unreadable")]):
            recovered = daemon._recover_observer(self.con)

        self.assertEqual(recovered["state"], "refused")
        self.assertIsNone(observer.find_check(
            self.con, prepared["check_id"])["finished_at"])
        alert = self.con.execute(
            "SELECT body FROM attention_requests WHERE correlation_id=?",
            (f"observer-recovery-refused:{prepared['check_id']}",)).fetchone()
        self.assertEqual(alert["body"], "worker identity unreadable")

    def test_recovery_settles_check_after_owned_processes_are_gone(self):
        run, profile, runtime_row = self.configured_run("print('unused')")
        prepared = self.prepare(run, profile, runtime_row)
        self.con.execute(
            "UPDATE observer_checks SET supervisor_pid=101,"
            "supervisor_pid_identity='supervisor',worker_pid=202,"
            "worker_pid_identity='worker' WHERE id=?", (prepared["check_id"],))
        self.con.commit()

        with patch("orchestra.daemon._observer_process_state", side_effect=[
                ("gone", "supervisor gone"), ("gone", "worker gone")]):
            recovered = daemon._recover_observer(self.con)

        self.assertEqual(recovered["state"], "failed")
        check = observer.find_check(self.con, prepared["check_id"])
        self.assertEqual(check["action"], "error")
        self.assertIsNotNone(check["finished_at"])

    def test_control_outbox_recovers_without_duplicate_tell(self):
        run, profile, runtime_row = self.configured_run("print('unused')")
        prepared = self.prepare(run, profile, runtime_row)
        observer.finish_check(
            self.con, prepared["check_id"],
            {"action": "tell", "reason": "drifting", "message": "Refocus"})
        request_id = f"observer:{prepared['check_id']}"
        messaging.queue_tell(
            self.con, run["id"], "observer", "Refocus",
            correlation_id=request_id)

        delivered = daemon._deliver_pending_observer_controls(self.con)

        self.assertEqual(delivered, [prepared["check_id"]])
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM messages WHERE run_id=? AND kind='tell' "
            "AND correlation_id=?", (run["id"], request_id)).fetchone()[0], 1)
        check = observer.find_check(self.con, prepared["check_id"])
        self.assertEqual(check["delivery_status"], "delivered")
        self.assertIsNotNone(check["control_audit_id"])

    def test_correlated_message_insert_is_idempotent_but_not_an_alias(self):
        run, _, _ = self.configured_run("print('unused')")
        first = messaging.queue_tell(
            self.con, run["id"], "observer", "Refocus",
            correlation_id="same-control")
        repeated = messaging.queue_tell(
            self.con, run["id"], "observer", "Refocus",
            correlation_id="same-control")
        self.assertEqual(repeated, first)
        with self.assertRaises(messaging.CorrelationConflict):
            messaging.queue_tell(
                self.con, run["id"], "observer", "Different",
                correlation_id="same-control")


if __name__ == "__main__":
    unittest.main()
