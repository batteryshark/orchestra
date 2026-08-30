import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from orchestra import (
    api, attention, auth, db, fleet_config, groups, messaging, runs, runway,
)


class APIContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {
            "ORCHESTRA_HOME": str(self.root / "state"),
        })
        self.env.start()
        self.con = db.connect(":memory:")
        _, token = auth.bootstrap_device(self.con, "Primary")
        self.identity = auth.identify(self.con, token)
        self.runtime = fleet_config.create_runtime(
            self.con, "Exec", "exec", slug="exec", command=["agent"],
            config={"endpoint": "runtime-private-marker"})
        self.profile = fleet_config.create_profile(
            self.con, "Profile", "exec", slug="profile", tier=2,
            env={"MARKER": "profile-private-marker"},
            config={"endpoint": "profile-config-marker"})
        groups.set_cwd(self.con, "general", str(self.root))
        self.api = api.API(self.con)

    def tearDown(self):
        self.con.close()
        self.env.stop()
        self.temp.cleanup()

    @staticmethod
    def data(response):
        return response.data["data"]

    def submit(self, request_id="run-one", context="Do a neutral task", **fields):
        return self.data(self.api.handle(
            "POST", "/api/v2/runs", {}, {
                "request_id": request_id,
                "profile": "profile", "context": context, **fields,
            }, self.identity))["run"]

    def resource_items(self, resource, **query):
        return self.data(self.api.handle(
            "GET", f"/api/v2/{resource}", query, None,
            self.identity))["items"]

    def test_public_projections_are_canonical_and_hide_secrets_and_host_paths(self):
        source = fleet_config.create_runway_source(
            self.con, "Usage", "provider", adapter="command", slug="usage",
            command=["private-binary", "--secretish"],
            config={"endpoint": "runway-private-marker"})
        secret_argv = (
            ["agent", "--api-key", "plain-secret-1"],
            ["agent", "--token=plain-secret-2"],
            ["agent", "OPENAI_PASSWORD=plain-secret-3"],
            ["agent", "https://user:plain-secret-4@example.test/path"],
            ["agent", "https://example.test/?access_token=plain-secret-5"],
            ["agent", "--header", "Authorization: Bearer plain-secret-6"],
        )
        for index, command in enumerate(secret_argv):
            with self.subTest(command=command), self.assertRaises(ValueError):
                fleet_config.create_runtime(
                    self.con, f"Sensitive {index}", "exec", command=command)
        with self.assertRaises(ValueError):
            fleet_config.update_runtime(
                self.con, self.runtime["runtime_id"], {
                    "command": ["agent", "--token=update-secret"],
                })
        groups_ = self.resource_items("groups")
        runtimes = self.resource_items("runtimes")
        profiles = self.resource_items("profiles")
        runway_sources = self.resource_items("runway-sources")
        safe_runtime = next(
            item for item in runtimes if item["id"] ==
            self.runtime["runtime_id"])
        safe_argv = json.dumps(safe_runtime["argv"])
        for index in range(1, 7):
            self.assertNotIn(f"plain-secret-{index}", safe_argv)
        stored_commands = " ".join(row[0] for row in self.con.execute(
            "SELECT command_json FROM runtimes"))
        for index in range(1, 7):
            self.assertNotIn(f"plain-secret-{index}", stored_commands)

        self.assertEqual(set(groups_[0]), {
            "id", "slug", "name", "archived", "cwd_configured",
            "next_number", "runs_count",
            "stats", "revision", "created_at", "updated_at",
        })
        self.assertEqual(set(runtimes[0]), {
            "id", "slug", "name", "kind", "argv", "enabled",
            "archived", "config_configured", "supports_steering",
            "supports_interrupt", "revision", "created_at", "updated_at",
        })
        self.assertEqual(set(profiles[0]), {
            "id", "slug", "name", "runtime_id", "runtime_name", "model",
            "effort", "tier", "priority", "sandbox", "timeout_seconds",
            "active_cap", "runway_source_id", "runway_source_name", "note",
            "env_configured", "config_configured", "observer_compatible",
            "observer_incompatibility", "enabled", "archived", "stats",
            "revision", "created_at", "updated_at",
        })
        self.assertTrue(safe_runtime["config_configured"])
        self.assertTrue(profiles[0]["env_configured"])
        self.assertTrue(profiles[0]["config_configured"])
        self.assertFalse(profiles[0]["observer_compatible"])
        self.assertIn("tool-free", profiles[0]["observer_incompatibility"])
        self.assertEqual(runway_sources[0]["id"], source["source_id"])
        self.assertTrue(runway_sources[0]["argv_configured"])
        self.assertTrue(runway_sources[0]["config_configured"])
        for forbidden in (
                "source_id", "latest", "linked_profiles", "command", "config",
                "config_json", "raw", "raw_json"):
            self.assertNotIn(forbidden, runway_sources[0])
        public_resources = json.dumps({
            "runtimes": runtimes, "profiles": profiles,
        })
        self.assertNotIn("runtime-private-marker", public_resources)
        self.assertNotIn("profile-private-marker", public_resources)
        self.assertNotIn("profile-config-marker", public_resources)

        run = self.submit()
        detail = self.data(self.api.handle(
            "GET", f"/api/v2/runs/{run['id']}", {}, None, self.identity))
        self.assertNotIn("repo", detail)
        self.assertNotIn("request_snapshot", detail)
        self.assertNotIn("session_ref", detail)
        self.assertNotIn("env", detail["profile_snapshot"])
        self.assertNotIn("config", detail["profile_snapshot"])
        self.assertNotIn("config", detail["runtime_snapshot"])
        encoded = json.dumps({
            "resources": [groups_, runtimes, profiles, runway_sources],
            "run": detail,
        })
        for private in (
                str(self.root), "runtime-private-marker", "profile-private-marker",
                "profile-config-marker", "runway-private-marker", "private-binary"):
            self.assertNotIn(private, encoded)
        self.assertNotIn("lifecycle", {
            row[1] for row in self.con.execute("PRAGMA table_info(runtimes)")})

    def test_client_runtime_and_profile_fields_translate_at_ingress(self):
        runtime_response = self.api.handle(
            "POST", "/api/v2/runtimes", {}, {
                "request_id": "runtime-create", "name": "Custom",
                "kind": "exec", "argv": ["custom-agent"], "enabled": True,
            }, self.identity)
        runtime = self.data(runtime_response)["runtime"]
        profile_response = self.api.handle(
            "POST", "/api/v2/profiles", {}, {
                "request_id": "profile-create", "name": "Custom profile",
                "runtime_id": runtime["id"], "tier": 1, "active_cap": 2,
            }, self.identity)
        profile = self.data(profile_response)["profile"]
        self.assertEqual(runtime_response.status, 201)
        self.assertEqual(profile_response.status, 201)
        self.assertEqual(profile["runtime_id"], runtime["id"])
        self.assertEqual(profile["active_cap"], 2)

    def test_run_cwd_override_is_validated_canonical_and_never_projected(self):
        override = self.root / "override"
        override.mkdir()
        alias = self.root / "override-link"
        alias.symlink_to(override, target_is_directory=True)
        run = self.submit("cwd-override", cwd=str(alias))
        frozen = runs.find(self.con, run["id"])
        self.assertEqual(frozen["cwd"], str(override.resolve()))
        self.assertEqual(run["cwd_source"], "run")
        self.assertNotIn("cwd", run)
        self.assertNotIn(str(override), json.dumps(run))

        not_directory = self.root / "not-a-directory"
        not_directory.write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "not a directory"):
            self.submit("cwd-file", cwd=str(not_directory))
        with self.assertRaisesRegex(ValueError, "unavailable"):
            self.submit("cwd-missing", cwd=str(self.root / "missing"))

    def test_corrupt_secret_bearing_runtime_cannot_enter_a_frozen_snapshot(self):
        self.con.execute(
            "UPDATE runtimes SET command_json=?,config_json=? WHERE runtime_id=?",
            (json.dumps(["agent", "--api-key=stored-secret"]),
             json.dumps({"api_key": "stored-config-secret"}),
             self.runtime["runtime_id"]))
        self.con.commit()
        with self.assertRaises(ValueError):
            self.submit("secret-snapshot")
        self.assertIsNone(self.con.execute(
            "SELECT 1 FROM runs WHERE request_id='secret-snapshot'"
        ).fetchone())
        snapshots = " ".join(
            (row[0] or "") + (row[1] or "") for row in self.con.execute(
                "SELECT profile_snapshot,runtime_snapshot FROM runs"))
        self.assertNotIn("stored-secret", snapshots)
        self.assertNotIn("stored-config-secret", snapshots)

    def test_runtime_adapter_and_argv_seam_is_small_and_truthful(self):
        builtin = self.data(self.api.handle(
            "POST", "/api/v2/runtimes", {}, {
                "request_id": "runtime-builtin", "name": "Codex builtin",
                "kind": "codex",
            }, self.identity))["runtime"]
        self.assertEqual(builtin["argv"], [])
        acp = self.data(self.api.handle(
            "POST", "/api/v2/runtimes", {}, {
                "request_id": "runtime-acp", "name": "ACP custom",
                "kind": "acp", "argv": ["agent-acp"],
            }, self.identity))["runtime"]
        self.assertEqual(acp["kind"], "acp")

        invalid = (
            {"name": "Unknown", "kind": "plugin"},
            {"name": "Empty exec", "kind": "exec"},
            {"name": "Misleading builtin", "kind": "claude",
             "argv": ["not-claude"]},
            {"name": "Fake resident", "kind": "codex",
             "lifecycle": "resident"},
        )
        for index, fields in enumerate(invalid):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.api.handle(
                    "POST", "/api/v2/runtimes", {}, {
                        "request_id": f"invalid-runtime-{index}", **fields,
                    }, self.identity)

        with self.assertRaises(ValueError):
            self.api.handle(
                "PATCH", f"/api/v2/runtimes/{self.runtime['runtime_id']}", {}, {
                    "request_id": "runtime-switch-with-stale-argv",
                    "kind": "codex",
                }, self.identity)
        unchanged = fleet_config.find_runtime(
            self.con, self.runtime["runtime_id"])
        self.assertEqual(unchanged["adapter"], "exec")
        switched = self.data(self.api.handle(
            "PATCH", f"/api/v2/runtimes/{self.runtime['runtime_id']}", {}, {
                "request_id": "runtime-switch", "kind": "codex", "argv": [],
            }, self.identity))["runtime"]
        self.assertEqual((switched["kind"], switched["argv"]), ("codex", []))

    def test_profile_discovery_runs_on_daemon_host_and_is_device_only(self):
        discovered = {
            "opencode": {"data": {"openai": ["gpt"]}, "error": None},
            "codex": {"data": [{"model": "gpt", "efforts": ["low"],
                                  "default_effort": "low"}], "error": None},
            "reasonix": {"data": None,
                          "error": "/Users/private/config.toml not found"},
            "claude": {"data": None,
                       "error": "no model listing command; use a profile"},
        }
        before = db.board_revision(self.con)
        with patch("orchestra.api.profiles.discover", return_value=discovered), \
                patch("orchestra.api.profiles.discover_local", return_value=[
                    {"id": "local-model", "source": "ollama"},
                ]) as local_probe:
            payload = self.data(self.api.handle(
                "GET", "/api/v2/profile-discovery", {"local": "true"}, None,
                self.identity))
        self.assertEqual(payload["local_models"], [
            {"id": "local-model", "source": "ollama"},
        ])
        self.assertTrue(payload["local_requested"])
        self.assertEqual(payload["runtimes"]["reasonix"]["error"],
                         "not_configured")
        self.assertEqual(payload["runtimes"]["claude"]["error"], "unsupported")
        self.assertNotIn("/Users/private", json.dumps(payload))
        local_probe.assert_called_once_with()
        self.assertEqual(db.board_revision(self.con), before)

        _, raw_service = auth.create_service_token(
            self.con, "Reader", ["read"])
        service = auth.identify(self.con, raw_service)
        with self.assertRaises(api.Problem) as denied:
            self.api.handle(
                "GET", "/api/v2/profile-discovery", {}, None, service)
        self.assertEqual((denied.exception.status, denied.exception.code),
                         (403, "device_required"))

        with patch("orchestra.api.profiles.discover", return_value=discovered), \
                patch("orchestra.api.profiles.discover_local") as no_probe:
            without_local = self.data(self.api.handle(
                "GET", "/api/v2/profile-discovery", {}, None, self.identity))
        no_probe.assert_not_called()
        self.assertFalse(without_local["local_requested"])
        self.assertEqual(without_local["local_models"], [])

    def test_pairing_returns_only_the_canonical_device_identity(self):
        created = self.api.handle(
            "POST", "/api/v2/devices/pairing", {},
            {"request_id": "pair-create", "label": "Phone"}, self.identity)
        pairing = self.data(created)
        redeemed = self.api.handle(
            "POST", "/api/v2/pairing/redeem", {}, {
                "request_id": "pair-redeem", "code": pairing["code"],
                "label": "Phone",
            }, None)
        device = self.data(redeemed)["device"]
        self.assertEqual(redeemed.status, 201)
        self.assertEqual(device["label"], "Phone")
        self.assertEqual(set(device), {
            "id", "label", "created_at", "last_used_at", "revoked_at",
        })
        audit = self.con.execute(
            "SELECT * FROM control_events WHERE request_id='pair-redeem'"
        ).fetchone()
        self.assertEqual(audit["actor"], f"device:{device['id']}")
        with self.assertRaises(api.Problem) as replayed:
            self.api.handle(
                "POST", "/api/v2/pairing/redeem", {}, {
                    "request_id": "pair-redeem", "code": pairing["code"],
                    "label": "Phone",
                }, None)
        self.assertEqual(replayed.exception.code, "secret_already_issued")
        with self.assertRaises(api.Problem) as conflicting:
            self.api.handle(
                "POST", "/api/v2/pairing/redeem", {}, {
                    "request_id": "pair-redeem", "code": pairing["code"],
                    "label": "Different phone",
                }, None)
        self.assertEqual(conflicting.exception.code, "request_id_conflict")

    def test_secret_mutations_commit_the_secret_and_replay_marker_atomically(self):
        pairing_body = {"request_id": "atomic-pair", "label": "Atomic"}
        pairing_count = self.con.execute(
            "SELECT COUNT(*) FROM pairing_codes").fetchone()[0]
        with patch("orchestra.api.idempotency.finish",
                   side_effect=RuntimeError("simulated crash")):
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                self.api.handle(
                    "POST", "/api/v2/devices/pairing", {}, pairing_body,
                    self.identity)
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM pairing_codes").fetchone()[0], pairing_count)
        self.assertIsNone(self.con.execute(
            "SELECT 1 FROM request_replays WHERE request_id='atomic-pair'"
        ).fetchone())
        pairing_response = self.api.handle(
            "POST", "/api/v2/devices/pairing", {}, pairing_body, self.identity)
        self.assertEqual(pairing_response.status, 201)

        service_body = {
            "request_id": "atomic-service", "name": "Bridge",
            "authorities": ["read"],
        }
        service_count = self.con.execute(
            "SELECT COUNT(*) FROM service_tokens").fetchone()[0]
        with patch("orchestra.api.idempotency.finish",
                   side_effect=RuntimeError("simulated crash")):
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                self.api.handle(
                    "POST", "/api/v2/service-tokens", {}, service_body,
                    self.identity)
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM service_tokens").fetchone()[0], service_count)
        issued = self.api.handle(
            "POST", "/api/v2/service-tokens", {}, service_body, self.identity)
        self.assertEqual(issued.status, 201)
        self.assertTrue(self.data(issued)["token"].startswith("os_"))

        redeem_body = {
            "request_id": "atomic-redeem",
            "code": self.data(pairing_response)["code"], "label": "Phone",
        }
        devices_before = self.con.execute(
            "SELECT COUNT(*) FROM devices").fetchone()[0]
        with patch("orchestra.api.idempotency.finish",
                   side_effect=RuntimeError("simulated crash")):
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                self.api.handle(
                    "POST", "/api/v2/pairing/redeem", {}, redeem_body, None)
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM devices").fetchone()[0], devices_before)
        self.assertIsNone(self.con.execute(
            "SELECT used_at FROM pairing_codes WHERE pairing_id=?",
            (self.data(pairing_response)["pairing_id"],),
        ).fetchone()["used_at"])
        redeemed = self.api.handle(
            "POST", "/api/v2/pairing/redeem", {}, redeem_body, None)
        self.assertEqual(redeemed.status, 201)

        stranded_body = {
            "request_id": "stranded-secret", "name": "Recovered",
            "authorities": ["read"],
        }
        self.con.execute(
            "INSERT INTO request_replays(request_id,method,path,body_hash,created_at) "
            "VALUES(?,?,?,?,?)",
            ("stranded-secret", "POST", "/api/v2/service-tokens",
             api.idempotency.body_hash(stranded_body), db.now()),
        )
        self.con.commit()
        recovered = self.api.handle(
            "POST", "/api/v2/service-tokens", {}, stranded_body, self.identity)
        self.assertEqual(recovered.status, 201)
        marker = self.con.execute(
            "SELECT response_json FROM request_replays WHERE request_id=?",
            ("stranded-secret",),
        ).fetchone()["response_json"]
        self.assertEqual(json.loads(marker), {"secret_response": True})

    def test_generic_mutation_rolls_back_domain_state_if_receipt_cannot_finish(self):
        body = {"request_id": "atomic-group", "name": "Atomic group"}
        before_revision = db.board_revision(self.con)
        before_audit = self.con.execute(
            "SELECT COUNT(*) FROM control_events").fetchone()[0]
        with patch("orchestra.api.idempotency.finish",
                   side_effect=RuntimeError("simulated crash")):
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                self.api.handle(
                    "POST", "/api/v2/groups", {}, body, self.identity)
        self.assertIsNone(self.con.execute(
            "SELECT 1 FROM run_groups WHERE slug='atomic-group'"
        ).fetchone())
        self.assertIsNone(self.con.execute(
            "SELECT 1 FROM request_replays WHERE request_id='atomic-group'"
        ).fetchone())
        self.assertEqual(db.board_revision(self.con), before_revision)
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM control_events").fetchone()[0], before_audit)

        created = self.api.handle(
            "POST", "/api/v2/groups", {}, body, self.identity)
        self.assertEqual(created.status, 201)
        replay = self.api.handle(
            "POST", "/api/v2/groups", {}, body, self.identity)
        self.assertEqual(replay.status, 200)

    def test_attention_projection_is_canonical_and_choices_are_always_an_array(self):
        attention.open_request(
            self.con, kind="decision", title="Choose", body="Pick a path",
            created_by="test", fallback={"choice": "safe"})
        item = self.data(self.api.handle(
            "GET", "/api/v2/inbox", {}, None, self.identity))["items"][0]
        self.assertEqual(item["state"], "open")
        self.assertEqual(item["detail"], "Pick a path")
        self.assertEqual(item["fallback"], {"choice": "safe"})
        self.assertIsInstance(item["id"], str)
        self.assertEqual(item["choices"], [])
        for alias in ("status", "body", "title", "created_at"):
            self.assertNotIn(alias, item)

    def test_attention_actions_are_kind_gated_and_empty_answers_do_not_resolve(self):
        question, _ = attention.open_request(
            self.con, kind="question", title="Question", body="Answer me",
            created_by="test")
        for index, (action, body, code) in enumerate((
                ("acknowledge", {"answer": "Acknowledged"},
                 "invalid_attention_action"),
                ("answer", {}, "empty_attention_answer"),
                ("answer", {"answer": 42}, "invalid_attention_answer"))):
            with self.subTest(action=action), self.assertRaises(api.Problem) as caught:
                self.api.handle(
                    "POST", f"/api/v2/attention/{question['id']}/{action}", {}, {
                        "request_id": f"question-invalid-{index}", **body,
                    }, self.identity)
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.con.execute(
            "SELECT status FROM attention_requests WHERE id=?",
            (question["id"],)).fetchone()[0], "open")
        answered = self.data(self.api.handle(
            "POST", f"/api/v2/attention/{question['id']}/answer", {}, {
                "request_id": "question-answer", "answer": "Use the safe path",
            }, self.identity))["attention"]
        self.assertEqual(answered["state"], "resolved")

        alert, _ = attention.open_request(
            self.con, kind="alert", title="Alert", body="Notice me",
            created_by="test")
        with self.assertRaises(api.Problem) as wrong_alert_action:
            self.api.handle(
                "POST", f"/api/v2/attention/{alert['id']}/answer", {}, {
                    "request_id": "alert-answer", "answer": "No",
                }, self.identity)
        self.assertEqual(wrong_alert_action.exception.code,
                         "invalid_attention_action")
        acknowledged = self.data(self.api.handle(
            "POST", f"/api/v2/attention/{alert['id']}/acknowledge", {}, {
                "request_id": "alert-acknowledge",
            }, self.identity))["attention"]
        self.assertEqual(acknowledged["state"], "resolved")

    def test_terminal_run_cancels_blocking_attention(self):
        run = self.submit("terminal-attention")
        self.con.execute(
            "UPDATE runs SET status='running',started_at=? WHERE id=?",
            (db.now(), run["id"]))
        self.con.commit()
        request, _ = attention.open_request(
            self.con, kind="question", title="Blocked", body="Need input",
            created_by="worker", run_id=run["id"], blocking=True,
            correlation_id="terminal-question")
        stopped = self.data(self.api.handle(
            "POST", f"/api/v2/runs/{run['id']}/stop", {}, {
                "request_id": "stop-with-attention",
            }, self.identity))["control"]
        self.assertEqual(stopped["outcome"], "ok")
        cancelled = self.data(self.api.handle(
            "GET", f"/api/v2/attention/{request['id']}", {}, None,
            self.identity))
        self.assertEqual(cancelled["state"], "cancelled")
        open_blockers = self.data(self.api.handle(
            "GET", "/api/v2/inbox", {"state": "open"}, None,
            self.identity))["items"]
        self.assertNotIn(str(request["id"]), {item["id"] for item in open_blockers})

    def test_continue_uses_one_executable_context_and_carries_prior_result_privately(self):
        source = self.data(self.api.handle(
            "POST", "/api/v2/runs", {}, {
                "request_id": "continue-source",
                "profile": "profile", "context": "Research the topic",
            }, self.identity))["run"]
        self.con.execute(
            "UPDATE runs SET status='completed',summary=?,finished_at=? WHERE id=?",
            ("Found three relevant constraints.", db.now(), source["id"]))
        self.con.commit()
        continued = self.data(self.api.handle(
            "POST", f"/api/v2/runs/{source['id']}/continue", {}, {
                "request_id": "continue-result", "context": "Synthesize them",
            }, self.identity))["run"]
        self.assertEqual(continued["context"], "Synthesize them")
        frozen = runs.find(self.con, continued["id"])
        self.assertIn("Found three relevant constraints.", frozen["context"])

    def test_outbox_is_a_filterable_cursor_paged_fleet_message_ledger(self):
        run = self.submit()
        outbound_id = messaging.post(
            self.con, run["id"], direction="outbound", sender="worker",
            body="Need a choice", kind="question", status="delivered",
            correlation_id="question:1")
        inbound_id = messaging.post(
            self.con, run["id"], direction="inbound", sender="operator",
            body="Use A", kind="answer", status="pending",
            correlation_id="question:1", reply_to=outbound_id)
        messaging.post(
            self.con, run["id"], direction="system", sender="orchestra",
            body="Recorded", kind="notice", status="delivered")

        first = self.data(self.api.handle(
            "GET", "/api/v2/outbox", {"limit": "1"}, None, self.identity))
        self.assertTrue(first["has_more"])
        self.assertIsNotNone(first["next_cursor"])
        second = self.data(self.api.handle(
            "GET", "/api/v2/outbox", {
                "limit": "1", "cursor": first["next_cursor"],
            }, None, self.identity))
        self.assertLess(second["items"][0]["id"], first["items"][0]["id"])

        pending = self.data(self.api.handle(
            "GET", "/api/v2/outbox", {"status": "pending"}, None,
            self.identity))["items"]
        outbound = self.data(self.api.handle(
            "GET", "/api/v2/outbox", {"direction": "outbound"}, None,
            self.identity))["items"]
        by_run = self.data(self.api.handle(
            "GET", "/api/v2/outbox", {"run_id": str(run["id"])}, None,
            self.identity))["items"]
        self.assertEqual([item["id"] for item in pending], [inbound_id])
        self.assertEqual([item["id"] for item in outbound], [outbound_id])
        self.assertEqual(len(by_run), 3)
        self.assertEqual(set(pending[0]), {
            "id", "run_id", "direction", "sender", "kind", "status", "body",
            "correlation_id", "reply_to", "created_at", "delivered_at",
            "undeliverable_at", "delivery_error", "display",
        })
        self.assertEqual(pending[0]["display"], "General #1")
        snapshot = self.data(self.api.handle(
            "GET", "/api/v2/snapshot", {}, None, self.identity))
        self.assertEqual(snapshot["messages"], {
            "total": 3, "pending": 1, "delivered": 2,
            "undeliverable": 0, "inbound": 1, "outbound": 1, "system": 1,
        })

    def test_thread_and_events_start_current_and_support_older_and_newer_cursors(self):
        run = self.submit()
        message_ids = [messaging.post(
            self.con, run["id"], direction="system", sender="test",
            body=f"message {index}", status="delivered")
            for index in range(1, 4)]
        for seq in range(1, 4):
            self.con.execute(
                "INSERT INTO events(run_id,seq,kind,name,payload,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (run["id"], seq, "output", None, f"event {seq}", db.now()),
            )
        self.con.commit()

        for resource in ("thread", "events"):
            with self.subTest(resource=resource):
                current = self.data(self.api.handle(
                    "GET", f"/api/v2/runs/{run['id']}/{resource}",
                    {"limit": "2"}, None, self.identity))
                ids = [item["id"] for item in current["items"]]
                self.assertEqual(ids, sorted(ids))
                self.assertEqual(len(ids), 2)
                self.assertTrue(current["has_more"])
                self.assertIsNotNone(current["resume_cursor"])

                older = self.data(self.api.handle(
                    "GET", f"/api/v2/runs/{run['id']}/{resource}", {
                        "limit": "2", "direction": "older",
                        "cursor": current["next_cursor"],
                    }, None, self.identity))
                self.assertEqual(len(older["items"]), 1)
                self.assertLess(older["items"][0]["id"], ids[0])

                first_forward = self.data(self.api.handle(
                    "GET", f"/api/v2/runs/{run['id']}/{resource}", {
                        "limit": "2", "direction": "newer",
                    }, None, self.identity))
                self.assertEqual(len(first_forward["items"]), 2)
                last_forward = self.data(self.api.handle(
                    "GET", f"/api/v2/runs/{run['id']}/{resource}", {
                        "limit": "2", "direction": "newer",
                        "cursor": first_forward["resume_cursor"],
                    }, None, self.identity))
                self.assertEqual(len(last_forward["items"]), 1)
                self.assertGreater(last_forward["items"][0]["id"],
                                   first_forward["items"][-1]["id"])

    def test_statistics_accepts_slugs_and_scales_without_run_id_placeholders(self):
        group = self.data(self.api.handle(
            "POST", "/api/v2/groups", {}, {
                "request_id": "statistics-group", "name": "Research",
            }, self.identity))["group"]
        base = self.data(self.api.handle(
            "POST", "/api/v2/runs", {}, {
                "request_id": "statistics-base", "group": group["slug"],
                "profile": "profile", "context": "Scale statistics",
            }, self.identity))["run"]
        self.con.executemany(
            "INSERT INTO runs(request_id,group_id,profile_id,runtime_id,"
            "runway_source_id,title,mission,context,requested_by,ref,status,"
            "queued_at,cwd,cwd_source,workdir,isolation,profile_snapshot,runtime_snapshot,"
            "request_snapshot) SELECT ?,group_id,profile_id,runtime_id,"
            "runway_source_id,?,mission,context,requested_by,ref,status,queued_at,"
            "cwd,cwd_source,workdir,isolation,profile_snapshot,runtime_snapshot,"
            "request_snapshot "
            "FROM runs WHERE id=?",
            [(f"statistics-{index}", f"Scale {index}", base["id"])
             for index in range(1050)],
        )
        self.con.execute(
            "UPDATE runs SET tokens_in=1,tokens_out=2,tokens_total=3,cost_usd=.5 "
            "WHERE group_id=?", (group["id"],))
        self.con.execute(
            "INSERT INTO observer_checks(run_id,profile_id,profile_snapshot,"
            "runtime_snapshot,input_json,trigger,verdict,action,tokens_in,"
            "tokens_out,tokens_total,cost_usd,started_at,finished_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (base["id"], self.profile["profile_id"], "{}", "{}", "{}",
             "interval", "healthy", "none", 10, 20, 30, .25,
             db.now(), db.now()),
        )
        self.con.commit()

        statistics = self.data(self.api.handle(
            "GET", "/api/v2/statistics", {
                "group": group["slug"],
                "profile": "profile", "status": "queued",
            }, None, self.identity))
        self.assertEqual(statistics["runs"], 1051)
        self.assertEqual(statistics["by_status"], {"queued": 1051})
        self.assertEqual(statistics["worker_usage"], {
            "input_tokens": 1051, "output_tokens": 2102,
            "total_tokens": 3153, "cost_usd": 525.5,
        })
        self.assertEqual(statistics["observer_usage"], {
            "input_tokens": 10, "output_tokens": 20,
            "total_tokens": 30, "cost_usd": .25,
        })
        with self.assertRaises(api.Problem) as invalid:
            self.api.handle(
                "GET", "/api/v2/statistics", {"status": "done"}, None,
                self.identity)
        self.assertEqual((invalid.exception.status, invalid.exception.code),
                         (422, "invalid_run_status"))

    def test_message_insert_and_delivery_receipts_advance_fleet_invalidation(self):
        run = self.submit()
        before_insert = db.board_revision(self.con)
        messaging.post(
            self.con, run["id"], direction="outbound", sender="worker",
            body="Progress", status="delivered")
        self.assertGreater(db.board_revision(self.con), before_insert)

        pending = messaging.post(
            self.con, run["id"], direction="inbound", sender="operator",
            body="Continue", kind="tell")
        before_delivery = db.board_revision(self.con)
        self.assertTrue(messaging.acknowledge(self.con, pending))
        self.assertGreater(db.board_revision(self.con), before_delivery)

        messaging.post(
            self.con, run["id"], direction="inbound", sender="operator",
            body="Too late", kind="tell")
        before_failure = db.board_revision(self.con)
        self.assertEqual(messaging.mark_undeliverable(
            self.con, run["id"], "worker exited"), 1)
        self.assertGreater(db.board_revision(self.con), before_failure)
        snapshot = self.data(self.api.handle(
            "GET", "/api/v2/snapshot", {}, None, self.identity))
        self.assertEqual(snapshot["revision"], db.board_revision(self.con))

    def test_observer_check_reports_log_availability_without_host_path(self):
        run = self.submit()
        pruned_at = db.now()
        self.con.execute(
            "INSERT INTO observer_checks("
            "run_id,profile_id,profile_snapshot,runtime_snapshot,input_json,"
            "trigger,verdict,action,log_path,log_pruned_at,started_at,finished_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (run["id"], self.profile["profile_id"], "{}", "{}", "{}",
             "interval", "healthy", "none", "/private/observer.log",
             pruned_at, db.now(), db.now()),
        )
        self.con.commit()
        check = self.data(self.api.handle(
            "GET", f"/api/v2/runs/{run['id']}/observer", {}, None,
            self.identity))["checks"][0]
        self.assertFalse(check["log_available"])
        self.assertEqual(check["log_pruned_at"], pruned_at)
        self.assertNotIn("log_path", check)
        self.assertNotIn("/private/observer.log", json.dumps(check))

    def test_prune_plan_keeps_observer_check_identity(self):
        payload = api.prune_plan_payload({
            "plan_id": "plan", "criteria": {},
            "items": [{"kind": "observer_log", "run_id": 7, "check_id": 41,
                       "size_bytes": 100}],
            "result": {
                "items": [{"kind": "observer_log", "run_id": 7,
                           "check_id": 41, "status": "pruned", "bytes": 100}],
                "pruned_items": 1, "pruned_bytes": 100, "skipped_items": 0,
            },
        })
        self.assertEqual(payload["items"][0]["check_id"], 41)
        self.assertEqual(payload["result"]["items"][0]["check_id"], 41)
        self.assertEqual(payload["result"]["pruned_items"], 1)
        self.assertEqual(payload["result"]["pruned_bytes"], 100)
        self.assertEqual(payload["result"]["skipped_items"], 0)
        self.con.execute(
            "INSERT INTO prune_plans(plan_id,criteria_json,items_json,created_by,"
            "created_at,applied_by,applied_at,result_json) VALUES(?,?,?,?,?,?,?,?)",
            ("plan", "{}", json.dumps([{**payload["items"][0],
                                         "path": "/private/hidden"}]),
             "device:test", db.now(), "device:test", db.now(), json.dumps({
                 "items": payload["result"]["items"], "pruned_items": 1,
                 "pruned_bytes": 100, "skipped_items": 0,
             })),
        )
        self.con.commit()
        projected = self.data(self.api.handle(
            "GET", "/api/v2/storage/prune-plans/plan", {}, None,
            self.identity))
        self.assertEqual(projected["result"], payload["result"])
        self.assertNotIn("/private/hidden", json.dumps(projected))

    def test_runway_burn_rate_uses_only_adjacent_comparable_windows(self):
        source = fleet_config.create_runway_source(
            self.con, "Codex runway", "codex", adapter="codex")
        start = datetime.now(timezone.utc)
        reset = (start + timedelta(days=5)).isoformat()

        def reading(at, remaining, resets_at=reset):
            return {
                "source_id": source["source_id"], "remaining": remaining,
                "limit_value": 100, "unit": "percent", "resets_at": resets_at,
                "as_of": at.isoformat(),
                "fresh_until": (at + timedelta(minutes=10)).isoformat(),
                "definitive": True, "reason": None,
                "windows": [{"id": "weekly", "name": "Weekly",
                             "remaining_percent": remaining,
                             "resets_at": resets_at, "unit": "percent"}],
                "raw": {}, "polled_at": at.isoformat(),
            }

        runway.record_source_reading(self.con, reading(start, 80))
        runway.record_source_reading(
            self.con, reading(start + timedelta(hours=2), 70))
        current = self.data(self.api.handle(
            "GET", f"/api/v2/runway-sources/{source['source_id']}", {}, None,
            self.identity))
        self.assertEqual(current["burn_rate"], 5.0)
        self.assertEqual(current["history"][0]["burn_rate"], 5.0)

        runway.record_source_reading(self.con, reading(
            start + timedelta(hours=3), 95,
            (start + timedelta(days=12)).isoformat()))
        reset_period = self.data(self.api.handle(
            "GET", f"/api/v2/runway-sources/{source['source_id']}", {}, None,
            self.identity))
        self.assertIsNone(reset_period["burn_rate"])

    def test_runway_readings_keep_balances_credits_windows_and_timestamps(self):
        start = datetime.now(timezone.utc)
        observed = (start - timedelta(seconds=30)).isoformat()
        polled = start.isoformat()
        fresh_until = (start + timedelta(minutes=10)).isoformat()
        reset = (start + timedelta(days=5)).isoformat()
        expired = (start - timedelta(minutes=5)).isoformat()
        credit_expiry = (start + timedelta(days=20)).isoformat()
        source = fleet_config.create_runway_source(
            self.con, "Claude runway", "anthropic", adapter="claude")
        reading = {
            "source_id": source["source_id"], "remaining": 39.0,
            "limit_value": 100, "unit": "percent", "resets_at": reset,
            "as_of": observed, "fresh_until": fresh_until,
            "definitive": True, "reason": None,
            "windows": [
                {"label": "weekly", "remaining": 39.0, "unit": "percent",
                 "resets_at": reset, "stale": False,
                 "stale_reason": None},
                {"label": "weekly · fable", "remaining": 77.0,
                 "unit": "percent", "resets_at": None, "stale": False,
                 "stale_reason": None, "per_model": True},
                {"label": "5h", "remaining": 88.0, "unit": "percent",
                 "resets_at": expired, "stale": True,
                 "stale_reason": None},
            ],
            "raw": {"credits": {
                "text": "2 banked resets", "count": 2,
                "expires_at": credit_expiry,
            }},
            "polled_at": polled,
        }
        runway.record_source_reading(self.con, reading)
        stored = self.con.execute(
            "SELECT windows_json,raw_json FROM runway_readings WHERE source_id=?",
            (source["source_id"],)).fetchone()
        self.assertEqual(json.loads(stored["windows_json"]), reading["windows"])
        self.assertEqual(json.loads(stored["raw_json"]), reading["raw"])

        projected = self.data(self.api.handle(
            "GET", f"/api/v2/runway-sources/{source['source_id']}", {}, None,
            self.identity))
        self.assertEqual(projected["kind"], "plan")
        self.assertEqual((projected["remaining"], projected["unit"]),
                         (39.0, "percent"))
        self.assertEqual(projected["resets_at"], reset)
        self.assertEqual(projected["observed_at"], observed)
        self.assertEqual(projected["polled_at"], polled)
        self.assertEqual(projected["fresh_until"], fresh_until)
        self.assertEqual(projected["credits"], {
            "text": "2 banked resets", "count": 2,
            "expires_at": credit_expiry,
        })
        windows = {item["name"]: item for item in projected["windows"]}
        self.assertEqual(set(windows), {"weekly", "weekly · fable", "5h"})
        self.assertTrue(windows["weekly · fable"]["per_model"])
        self.assertIsNone(windows["5h"]["remaining_percent"])
        self.assertEqual(windows["5h"]["stale_reason"],
                         "reset since this was read")
        self.assertEqual(projected["history"][0]["credits"],
                         projected["credits"])
        self.assertEqual(projected["history"][0]["polled_at"], polled)
        recorded_windows = {
            item["name"]: item for item in projected["history"][0]["windows"]}
        self.assertEqual(recorded_windows["5h"]["remaining_percent"], 88.0)

        balance_source = fleet_config.create_runway_source(
            self.con, "DeepSeek runway", "deepseek", adapter="deepseek")
        runway.record_source_reading(self.con, {
            **reading, "source_id": balance_source["source_id"],
            "remaining": 10.5, "limit_value": None, "unit": "USD",
            "resets_at": None, "windows": [], "raw": {},
        })
        balance = self.data(self.api.handle(
            "GET", f"/api/v2/runway-sources/{balance_source['source_id']}",
            {}, None, self.identity))
        self.assertEqual(balance["kind"], "api")
        self.assertEqual((balance["remaining"], balance["unit"]), (10.5, "USD"))
        self.assertIsNone(balance["resets_at"])
        self.assertEqual(balance["windows"], [])

    def test_managed_resources_archive_restore_and_group_cwd_updates_are_atomic(self):
        runtime = self.data(self.api.handle(
            "POST", "/api/v2/runtimes", {}, {
                "request_id": "runtime-extra", "name": "Extra runtime",
                "kind": "exec", "argv": ["extra"],
            }, self.identity))["runtime"]
        profile = self.data(self.api.handle(
            "POST", "/api/v2/profiles", {}, {
                "request_id": "profile-extra", "name": "Extra profile",
                "runtime_id": runtime["id"], "tier": 1,
            }, self.identity))["profile"]
        source = self.data(self.api.handle(
            "POST", "/api/v2/runway-sources", {}, {
                "request_id": "source-extra", "name": "Extra runway",
                "provider": "test", "adapter": "command", "argv": ["usage"],
            }, self.identity))["runway_source"]
        self.assertTrue(source["argv_configured"])
        self.assertFalse(source["config_configured"])
        configured = self.data(self.api.handle(
            "PATCH", f"/api/v2/runway-sources/{source['id']}", {}, {
                "request_id": "source-config", "provider": "updated-test",
                "config": {
                    "endpoint": "private-runway-endpoint"},
            }, self.identity))["runway_source"]
        self.assertEqual(configured["provider"], "updated-test")
        self.assertTrue(configured["argv_configured"])
        self.assertTrue(configured["config_configured"])
        self.assertNotIn("private-runway-endpoint", json.dumps(configured))
        with self.assertRaises(ValueError):
            self.api.handle(
                "PATCH", f"/api/v2/runway-sources/{source['id']}", {}, {
                    "request_id": "source-stale-adapter", "adapter": "codex",
                }, self.identity)
        cleared = self.data(self.api.handle(
            "PATCH", f"/api/v2/runway-sources/{source['id']}", {}, {
                "request_id": "source-clear", "adapter": "codex",
                "argv": [], "config": {},
            }, self.identity))["runway_source"]
        self.assertFalse(cleared["argv_configured"])
        self.assertFalse(cleared["config_configured"])
        with self.assertRaises(ValueError):
            self.api.handle(
                "PATCH", f"/api/v2/runway-sources/{source['id']}", {}, {
                    "request_id": "source-secret-config",
                    "config": {"api_key": "must-not-store"},
                }, self.identity)
        for index, fields in enumerate((
                {"name": "Unknown source", "provider": "test",
                 "adapter": "invented"},
                {"name": "Empty command", "provider": "test",
                 "adapter": "command"},
                {"name": "Misleading builtin", "provider": "codex",
                 "adapter": "codex", "argv": ["custom"]},
        )):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.api.handle(
                    "POST", "/api/v2/runway-sources", {}, {
                        "request_id": f"invalid-source-{index}", **fields,
                    }, self.identity)

        archived_profile = self.data(self.api.handle(
            "PATCH", f"/api/v2/profiles/{profile['id']}", {}, {
                "request_id": "profile-archive", "archived": True,
            }, self.identity))["profile"]
        self.assertTrue(archived_profile["archived"])
        self.assertNotIn(profile["id"], {
            item["id"] for item in self.resource_items("profiles")})
        self.assertIn(profile["id"], {
            item["id"] for item in self.resource_items(
                "profiles", include_archived="true")})

        archived_runtime = self.data(self.api.handle(
            "PATCH", f"/api/v2/runtimes/{runtime['id']}", {}, {
                "request_id": "runtime-archive", "archived": True,
            }, self.identity))["runtime"]
        self.assertTrue(archived_runtime["archived"])
        self.assertNotIn(runtime["id"], {
            item["id"] for item in self.resource_items("runtimes")})
        self.assertIn(runtime["id"], {
            item["id"] for item in self.resource_items(
                "runtimes", include_archived="true")})
        restored_runtime = self.data(self.api.handle(
            "PATCH", f"/api/v2/runtimes/{runtime['id']}", {}, {
                "request_id": "runtime-restore", "archived": False,
            }, self.identity))["runtime"]
        self.assertFalse(restored_runtime["archived"])
        restored_profile = self.data(self.api.handle(
            "PATCH", f"/api/v2/profiles/{profile['id']}", {}, {
                "request_id": "profile-restore", "archived": False,
            }, self.identity))["profile"]
        self.assertFalse(restored_profile["archived"])

        archived_source = self.data(self.api.handle(
            "PATCH", f"/api/v2/runway-sources/{source['id']}", {}, {
                "request_id": "source-archive", "archived": True,
            }, self.identity))["runway_source"]
        self.assertTrue(archived_source["archived"])
        self.assertNotIn(source["id"], {
            item["id"] for item in self.resource_items("runway-sources")})
        self.assertIn(source["id"], {
            item["id"] for item in self.resource_items(
                "runway-sources", include_archived="true")})

        workspace = self.data(self.api.handle(
            "POST", "/api/v2/groups", {}, {
                "request_id": "workspace-group", "name": "Workspace",
                "cwd": str(self.root),
            }, self.identity))["group"]
        self.assertTrue(workspace["cwd_configured"])
        self.assertNotIn("cwd", workspace)
        prior_run = self.submit(
            "before-group-binding", group=workspace["slug"])
        new_root = self.root / "repository"
        new_root.mkdir()
        (new_root / ".git").mkdir()
        rebound = self.data(self.api.handle(
            "PATCH", f"/api/v2/groups/{workspace['id']}", {}, {
                "request_id": "group-binding", "cwd": str(new_root),
            }, self.identity))["group"]
        self.assertTrue(rebound["cwd_configured"])
        self.assertNotIn("cwd", rebound)
        self.assertEqual(groups.find(
            self.con, workspace["id"])["default_cwd"], str(new_root.resolve()))
        self.assertEqual(runs.find(self.con, prior_run["id"])["cwd"],
                         str(self.root.resolve()))
        future_run = self.submit(
            "after-group-binding", group=workspace["slug"])
        self.assertEqual(runs.find(self.con, future_run["id"])["cwd"],
                         str(new_root.resolve()))
        archived_group = self.data(self.api.handle(
            "PATCH", f"/api/v2/groups/{workspace['id']}", {}, {
                "request_id": "workspace-archive", "archived": True,
            }, self.identity))["group"]
        self.assertTrue(archived_group["archived"])
        self.assertNotIn(workspace["id"], {
            item["id"] for item in self.resource_items("groups")})
        self.assertIn(workspace["id"], {
            item["id"] for item in self.resource_items(
                "groups", include_archived="true")})

    def test_rejected_group_updates_have_no_partial_effect(self):
        created = self.data(self.api.handle(
            "POST", "/api/v2/groups", {}, {
                "request_id": "group-create", "name": "Batch",
            }, self.identity))["group"]
        with self.assertRaisesRegex(ValueError, "exactly one"):
            self.api.handle(
                "PATCH", f"/api/v2/groups/{created['id']}", {}, {
                    "request_id": "group-bad-update", "name": "Changed",
                    "archived": True,
                }, self.identity)
        row = self.data(self.api.handle(
            "GET", f"/api/v2/groups/{created['id']}", {}, None, self.identity))
        self.assertEqual(row["name"], "Batch")
        self.assertFalse(row["archived"])

        for index, changes in enumerate((
                {"name": "Renamed General"}, {"archived": True})):
            with self.subTest(changes=changes), \
                    self.assertRaises(sqlite3.IntegrityError):
                self.api.handle(
                    "PATCH", "/api/v2/groups/general", {}, {
                        "request_id": f"general-immutable-{index}", **changes,
                    }, self.identity)
        general = self.data(self.api.handle(
            "GET", "/api/v2/groups/general", {}, None, self.identity))
        self.assertEqual((general["name"], general["archived"]),
                         ("General", False))

    def test_run_token_has_worker_authority_only_for_its_own_run(self):
        own = self.submit("run-own")
        other = self.submit("run-other")
        raw = auth.mint_run(self.con, own["id"])
        worker = auth.identify(self.con, raw)

        self.api.handle(
            "GET", f"/api/v2/runs/{own['id']}", {}, None, worker)
        opened = self.data(self.api.handle(
            "POST", f"/api/v2/runs/{own['id']}/attention", {}, {
                "request_id": "worker-attention", "kind": "question",
                "body": "Which option?", "blocking": False,
            }, worker))
        self.assertEqual(opened["attention"]["run_id"], own["id"])
        delegated = self.data(self.api.handle(
            "POST", f"/api/v2/runs/{own['id']}/children", {}, {
                "request_id": "worker-child", "profile": "profile",
                "context": "Handle the bounded subtask",
            }, worker))
        self.assertEqual(delegated["child_request"]["parent_run_id"], own["id"])
        work_file = self.root / "result.txt"
        work_file.write_text("done", encoding="utf-8")
        artifact = self.data(self.api.handle(
            "POST", f"/api/v2/runs/{own['id']}/artifacts", {}, {
                "request_id": "worker-artifact", "path": "result.txt",
            }, worker))["artifact"]
        self.assertEqual(artifact["run_id"], own["id"])

        denied = [
            ("GET", f"/api/v2/runs/{other['id']}", None),
            ("GET", "/api/v2/profiles", None),
            ("POST", "/api/v2/runs", {
                "request_id": "worker-dispatch",
                "profile": "profile", "context": "not authorized"}),
            ("POST", f"/api/v2/runs/{own['id']}/stop", {
                "request_id": "worker-stop"}),
            ("POST", f"/api/v2/runs/{own['id']}/tell", {
                "request_id": "worker-tell", "text": "redirect"}),
            ("POST", f"/api/v2/runs/{own['id']}/pin", {
                "request_id": "worker-pin"}),
            ("POST", f"/api/v2/runs/{other['id']}/attention", {
                "request_id": "worker-other-attention", "body": "no",
                "blocking": False}),
        ]
        for method, path, body in denied:
            with self.subTest(method=method, path=path):
                with self.assertRaises(api.Problem) as caught:
                    self.api.handle(method, path, {}, body, worker)
                self.assertEqual(caught.exception.status, 403)

    def test_public_dispatch_cannot_forge_parent_lineage(self):
        parent = self.submit("lineage-parent")
        _, raw = auth.create_service_token(
            self.con, "Dispatcher", ["dispatch"])
        dispatcher = auth.identify(self.con, raw)
        before = self.con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        with self.assertRaisesRegex(
                api.ContractError, "parent_run_id is internal"):
            self.api.handle(
                "POST", "/api/v2/runs", {}, {
                    "request_id": "forged-child",
                    "profile": "profile", "context": "Forge lineage",
                    "parent_run_id": parent["id"],
                }, dispatcher)
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM runs").fetchone()[0], before)

    def test_mutation_audit_has_authenticated_actor_and_request_id(self):
        response = self.api.handle(
            "POST", "/api/v2/groups", {}, {
                "request_id": "audited-group", "name": "Audited",
            }, self.identity)
        self.assertEqual(response.status, 201)
        event = self.con.execute(
            "SELECT * FROM control_events WHERE request_id='audited-group' "
            "AND actor=? ORDER BY id DESC LIMIT 1",
            (f"device:{self.identity.subject_id}",),
        ).fetchone()
        self.assertIsNotNone(event)
        self.assertEqual(event["outcome"], "ok")

    def test_snapshot_uses_the_managed_instance_name(self):
        initial = self.data(self.api.handle(
            "GET", "/api/v2/snapshot", {}, None, self.identity))
        self.assertEqual(initial["instance"]["name"], "Orchestra")
        self.assertEqual(set(initial["scheduler"]), {
            "paused", "active", "queued", "max_active",
        })
        self.assertTrue({"revision", "updated_by", "updated_at"} <=
                        set(initial["observer"]))
        self.api.handle(
            "PATCH", "/api/v2/settings", {}, {
                "request_id": "instance-name", "key": "instance_name",
                "value": "Studio Fleet",
            }, self.identity)
        updated = self.data(self.api.handle(
            "GET", "/api/v2/snapshot", {}, None, self.identity))
        self.assertEqual(updated["instance"]["name"], "Studio Fleet")

    def test_observer_concurrency_is_configurable_and_bounded(self):
        runtime = fleet_config.create_runtime(
            self.con, "Claude", "claude", slug="claude")
        observer_profile = fleet_config.create_profile(
            self.con, "Observer", runtime["runtime_id"],
            slug="observer", tier=1)
        configured = self.data(self.api.handle(
            "PATCH", "/api/v2/observer", {}, {
                "request_id": "observer-concurrency", "enabled": True,
                "profile_id": observer_profile["profile_id"], "concurrency": 3,
            }, self.identity))["observer"]
        self.assertEqual(configured["concurrency"], 3)
        snapshot = self.data(self.api.handle(
            "GET", "/api/v2/snapshot", {}, None, self.identity))
        self.assertEqual(snapshot["observer"]["concurrency"], 3)

        for index, value in enumerate((0, 9, True)):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.api.handle(
                    "PATCH", "/api/v2/observer", {}, {
                        "request_id": f"observer-invalid-{index}",
                        "concurrency": value,
                    }, self.identity)

    def test_runtime_and_profile_host_configuration_is_write_only(self):
        created_runtime = self.data(self.api.handle(
            "POST", "/api/v2/runtimes", {}, {
                "request_id": "runtime-config-create", "name": "Host runtime",
                "kind": "codex", "config": {"region": "local"},
            }, self.identity))["runtime"]
        self.assertTrue(created_runtime["config_configured"])
        created_profile = self.data(self.api.handle(
            "POST", "/api/v2/profiles", {}, {
                "request_id": "profile-config-create", "name": "Host profile",
                "runtime_id": created_runtime["id"], "tier": 2,
                "env": {"REGION": "local"}, "config": {"mode": "careful"},
            }, self.identity))["profile"]
        self.assertTrue(created_profile["env_configured"])
        self.assertTrue(created_profile["config_configured"])

        runtime = self.data(self.api.handle(
            "PATCH", f"/api/v2/runtimes/{self.runtime['runtime_id']}", {}, {
                "request_id": "runtime-config-preserve", "name": "Exec renamed",
            }, self.identity))["runtime"]
        self.assertTrue(runtime["config_configured"])
        self.assertNotIn("config", runtime)
        cleared_runtime = self.data(self.api.handle(
            "PATCH", f"/api/v2/runtimes/{self.runtime['runtime_id']}", {}, {
                "request_id": "runtime-config-clear", "config": {},
            }, self.identity))["runtime"]
        self.assertFalse(cleared_runtime["config_configured"])

        profile = self.data(self.api.handle(
            "PATCH", f"/api/v2/profiles/{self.profile['profile_id']}", {}, {
                "request_id": "profile-config-preserve", "note": "renamed",
            }, self.identity))["profile"]
        self.assertTrue(profile["env_configured"])
        self.assertTrue(profile["config_configured"])
        self.assertNotIn("env", profile)
        self.assertNotIn("config", profile)
        cleared_profile = self.data(self.api.handle(
            "PATCH", f"/api/v2/profiles/{self.profile['profile_id']}", {}, {
                "request_id": "profile-config-clear", "env": {}, "config": {},
            }, self.identity))["profile"]
        self.assertFalse(cleared_profile["env_configured"])
        self.assertFalse(cleared_profile["config_configured"])

        with self.assertRaisesRegex(ValueError, "adapter-owned"):
            self.api.handle(
                "PATCH", f"/api/v2/runtimes/{self.runtime['runtime_id']}", {}, {
                    "request_id": "runtime-hidden-capabilities",
                    "capabilities": {"steering": True},
                }, self.identity)
        with self.assertRaisesRegex(ValueError, "keys and values"):
            self.api.handle(
                "PATCH", f"/api/v2/profiles/{self.profile['profile_id']}", {}, {
                    "request_id": "profile-invalid-env", "env": {"COUNT": 3},
                }, self.identity)

    def test_observer_profiles_are_validated_before_configuration_and_admission(self):
        profiles = self.resource_items("profiles")
        incompatible = next(item for item in profiles
                            if item["id"] == self.profile["profile_id"])
        self.assertFalse(incompatible["observer_compatible"])
        self.assertIn("exec runtime", incompatible["observer_incompatibility"])

        before = fleet_config.observer(self.con)
        with self.assertRaisesRegex(ValueError, "tool-free Observer"):
            self.api.handle(
                "PATCH", "/api/v2/observer", {}, {
                    "request_id": "observer-incompatible-default",
                    "enabled": True, "profile_id": self.profile["profile_id"],
                }, self.identity)
        after = fleet_config.observer(self.con)
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["profile_id"], before["profile_id"])
        self.assertIsNone(self.con.execute(
            "SELECT 1 FROM request_replays WHERE request_id=?",
            ("observer-incompatible-default",),
        ).fetchone())

        with self.assertRaisesRegex(runs.AdmissionError, "tool-free Observer"):
            self.api.handle(
                "POST", "/api/v2/runs", {}, {
                    "request_id": "observer-incompatible-run",
                    "profile": "profile",
                    "context": "Observe safely", "observer": "profile",
                }, self.identity)

        runtime = fleet_config.create_runtime(
            self.con, "OpenCode", "opencode", slug="opencode")
        compatible = fleet_config.create_profile(
            self.con, "Safe Observer", runtime["runtime_id"],
            slug="safe-observer", tier=1)
        public = next(item for item in self.resource_items("profiles")
                      if item["id"] == compatible["profile_id"])
        self.assertTrue(public["observer_compatible"])
        self.assertIsNone(public["observer_incompatibility"])
        run = self.data(self.api.handle(
            "POST", "/api/v2/runs", {}, {
                "request_id": "observer-compatible-run",
                "profile": "profile", "context": "Observe safely",
                "observer": "safe-observer",
            }, self.identity))["run"]
        self.assertEqual(run["status"], "queued")

        fleet_config.configure_observer(
            self.con, enabled=True, profile=compatible["profile_id"])
        fleet_config.update_runtime(
            self.con, runtime["runtime_id"], {"enabled": False})
        with self.assertRaisesRegex(
                runs.AdmissionError, "configured Observer is unavailable"):
            self.api.handle(
                "POST", "/api/v2/runs", {}, {
                    "request_id": "observer-stale-inherited",
                    "profile": "profile", "context": "Do not launch late",
                }, self.identity)

    def test_fleet_settings_are_fixed_and_strictly_typed(self):
        valid = {
            "max_active_runs": 256,
            "delegation_max_depth": 0,
            "delegation_max_children": 100,
            "delegation_max_active_children": 1,
            "paused": True,
        }
        for index, (key, value) in enumerate(valid.items()):
            with self.subTest(key=key):
                setting = self.data(self.api.handle(
                    "PATCH", "/api/v2/settings", {}, {
                        "request_id": f"setting-valid-{index}", "key": key,
                        "value": value,
                    }, self.identity))["setting"]
                self.assertEqual(setting["value"], value)
        invalid = [
            ("invented", 1),
            ("instance_name", "   "),
            ("instance_name", "x" * 101),
            ("paused", "true"),
            ("max_active_runs", True),
            ("max_active_runs", 0),
            ("max_active_runs", 257),
            ("delegation_max_depth", -1),
            ("delegation_max_depth", 11),
            ("delegation_max_children", 0),
            ("delegation_max_active_children", 101),
        ]
        for index, (key, value) in enumerate(invalid):
            with self.subTest(key=key, value=value):
                with self.assertRaises(ValueError):
                    self.api.handle(
                        "PATCH", "/api/v2/settings", {}, {
                            "request_id": f"setting-invalid-{index}", "key": key,
                            "value": value,
                        }, self.identity)

    def test_openapi_is_complete_typed_and_self_consistent(self):
        document = self.api.handle(
            "GET", "/api/v2/openapi.json", {}, None, self.identity).data
        self.assertEqual(document["openapi"], "3.1.0")
        self.assertNotIn("api_version", document)
        for route in (
            "/api/v2/outbox",
            "/api/v2/profile-discovery",
            "/api/v2/runs/{run_id}/interrupt",
            "/api/v2/runs/{run_id}/children",
            "/api/v2/attention/{attention_id}/answer",
            "/api/v2/storage/prune-plans/{plan_id}/apply",
            "/api/v2/artifacts/{artifact_id}/content",
        ):
            self.assertIn(route, document["paths"])
        self.assertEqual(document["paths"]["/health"]["get"]["security"], [])
        self.assertEqual(document["paths"]["/api/v2/pairing/redeem"]
                         ["post"]["security"], [])
        self.assertEqual(set(document["components"]["securitySchemes"]), {
            "bearer", "cookie",
        })
        self.assertEqual(set(document["paths"]["/api/v2/runs"]["post"]
                             ["responses"]), {"200", "201", "default"})
        self.assertEqual(set(document["paths"]["/api/v2/groups/{resource_id}"]
                             ["patch"]["responses"]), {"200", "default"})
        self.assertEqual(set(document["paths"]["/api/v2/service-tokens"]
                             ["post"]["responses"]), {"201", "default"})
        outbox_params = {
            item["$ref"].rsplit("/", 1)[-1] for item in
            document["paths"]["/api/v2/outbox"]["get"]["parameters"]
        }
        self.assertEqual(outbox_params, {
            "Direction", "MessageStatus", "MessageKind", "RunID", "Limit",
            "Cursor",
        })
        discovery_params = document["paths"]["/api/v2/profile-discovery"][
            "get"]["parameters"]
        self.assertEqual(discovery_params, [{
            "$ref": "#/components/parameters/LocalDiscovery",
        }])
        schemas = document["components"]["schemas"]
        self.assertNotIn("/api/v2/scopes", document["paths"])
        self.assertNotIn("Scope", schemas)
        self.assertNotIn("repo", schemas["Run"]["properties"])
        self.assertNotIn("mission", schemas["Run"]["properties"])
        self.assertNotIn("scope_id", schemas["Run"]["properties"])
        self.assertNotIn("isolation", schemas["Run"]["properties"])
        self.assertTrue(schemas["RunRequest"]["properties"]["cwd"]["writeOnly"])
        self.assertEqual(set(schemas["RunRequest"]["required"]), {
            "request_id", "profile", "context"})
        self.assertNotIn("env", schemas["Profile"]["properties"])
        self.assertTrue({
            "direction", "status", "correlation_id", "reply_to",
            "delivery_error",
        } <= set(schemas["Message"]["required"]))
        self.assertNotIn("token", schemas["PairingRedemption"]["required"])
        self.assertIn("check_id", schemas["PrunePlanItem"]["properties"])
        self.assertEqual(
            schemas["PrunePlan"]["properties"]["result"]["anyOf"][0],
            {"$ref": "#/components/schemas/PruneResultSummary"})
        self.assertEqual(schemas["Runtime"]["properties"]["kind"]["enum"],
                         sorted(fleet_config.RUNTIME_ADAPTERS))
        self.assertNotIn("lifecycle", schemas["Runtime"]["properties"])
        self.assertEqual(
            document["components"]["parameters"]["Status"]["schema"]["enum"],
            list(db.RUN_ACTIVE + db.RUN_TERMINAL))
        self.assertEqual(
            schemas["ObserverSettings"]["properties"]["concurrency"],
            {"type": "integer", "minimum": 1, "maximum": 8})
        self.assertTrue({
            "env_configured", "config_configured", "observer_compatible",
            "observer_incompatibility",
        } <= set(schemas["Profile"]["required"]))
        self.assertIn("config_configured", schemas["Runtime"]["required"])
        self.assertTrue(schemas["RuntimeUpdateRequest"]["allOf"][1][
            "properties"]["config"]["writeOnly"])
        self.assertTrue(schemas["ProfileUpdateRequest"]["allOf"][1][
            "properties"]["env"]["writeOnly"])
        observer_update = schemas["ObserverUpdateRequest"]
        self.assertIn("claude, opencode, or reasonix",
                      observer_update["description"])
        runtime_create = schemas["RuntimeCreateRequest"]["allOf"]
        self.assertNotIn("argv", runtime_create[1]["required"])
        self.assertNotIn("lifecycle", runtime_create[1]["properties"])
        self.assertEqual(runtime_create[2]["then"]["required"], ["argv"])
        self.assertEqual(schemas["AttentionAnswer"]["allOf"][1]["anyOf"], [
            {"required": ["answer"]}, {"required": ["choice"]},
        ])
        self.assertEqual(
            schemas["RunwaySource"]["properties"]["adapter"]["enum"],
            sorted(fleet_config.SOURCE_ADAPTERS))
        self.assertNotIn("argv", schemas["RunwaySource"]["properties"])
        self.assertIn("argv_configured",
                      schemas["RunwaySource"]["properties"])
        self.assertTrue({
            "kind", "remaining", "unit", "resets_at", "credits",
            "observed_at", "polled_at", "fresh_until",
        } <= set(schemas["RunwaySource"]["required"]))
        self.assertTrue({
            "stale_reason", "per_model",
        } <= set(schemas["RunwayWindow"]["required"]))
        self.assertEqual(set(schemas["RunwayCredits"]["required"]),
                         {"text", "count", "expires_at"})

        unresolved, unresolved_parameters = set(), set()

        def visit(value):
            if isinstance(value, dict):
                reference = value.get("$ref")
                if isinstance(reference, str) and reference.startswith(
                        "#/components/schemas/"):
                    name = reference.rsplit("/", 1)[-1]
                    if name not in schemas:
                        unresolved.add(name)
                if isinstance(reference, str) and reference.startswith(
                        "#/components/parameters/"):
                    name = reference.rsplit("/", 1)[-1]
                    if name not in document["components"]["parameters"]:
                        unresolved_parameters.add(name)
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(document)
        self.assertEqual(unresolved, set())
        self.assertEqual(unresolved_parameters, set())
        operation_ids = [
            operation["operationId"]
            for path_item in document["paths"].values()
            for operation in path_item.values()
        ]
        self.assertEqual(len(operation_ids), len(set(operation_ids)))


if __name__ == "__main__":
    unittest.main()
