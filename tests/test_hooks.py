"""Hook installation and the messaging verbs (DESIGN §6, W-0098).

Nothing here spawns a real harness, files a real Nod card, or writes into
the developer's ~/.claude, ~/.codex, ~/.reasonix or ~/.dromond: every home
is redirected at a throwaway directory, Nod is tests/fake_nod.py and Work is
tests/fake_work.py.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dromond import config, db, hooks, messaging, runners, traces
from tests.fake_nod import DECISIONS_CHANNEL, DECISIONS_TOKEN, FakeNod
from tests.fake_work import FakeWork

PROJECT_ID = "53efe3c3-6def-4797-8560-3dce073d7d63"

CONFIG = """\
[settings]
timeout = 60

[profiles.stub]
backend = "opencode"

[nod]
enabled = true
expires_after = 2
# Never the default path: that one is the developer's real Nod credentials.
secrets_file = "{secrets}"

[work]
enabled = true
agent_identity = "dromond"
profile = "stub"
"""


class HookFixture(unittest.TestCase):
    """A run row, a fake Nod, a fake Work, and every home redirected."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name).resolve()
        self.nod = FakeNod()
        self.nod_url = self.nod.start()
        self.work = FakeWork(workspace_root=self.tmp_path / "workspace")
        self.work.add_task("W-0001", "demo item")
        self.work_url = self.work.start()
        self.global_config = self.tmp_path / "global.toml"
        self.global_config.write_text(
            CONFIG.format(secrets=self.tmp_path / "nod-secrets.env")
            + f'api_url = "{self.work_url}"\n')
        self.env = mock.patch.dict(os.environ, {
            "DROMOND_CONFIG": str(self.global_config),
            "DROMOND_HOME": str(self.tmp_path / "home"),
            "CLAUDE_CONFIG_DIR": str(self.tmp_path / "claude"),
            "CODEX_HOME": str(self.tmp_path / "codex"),
            "REASONIX_HOME": str(self.tmp_path / "reasonix"),
            "DROMOND_NOD_BASE_URL": self.nod_url,
            "DROMOND_NOD_DECISIONS_CHANNEL": DECISIONS_CHANNEL,
            "DROMOND_NOD_DECISIONS_TOKEN": DECISIONS_TOKEN,
        })
        self.env.start()
        self.con = db.connect()
        self.log = self.tmp_path / "run-1.jsonl"
        self.log.touch()
        self.con.execute(
            "INSERT INTO runs(id, slug, profile, backend, requested_by, workdir, "
            "project_id, work_item, log_path, status, started_at) "
            "VALUES(1,'brave_otter','stub','claude','human',?,?, 'W-0001', ?, "
            "'running', ?)",
            (str(self.tmp_path), PROJECT_ID, str(self.log), db.now()))
        self.con.commit()
        self.cfg = config.load(PROJECT_ID)

    def tearDown(self) -> None:
        self.con.close()
        self.env.stop()
        self.work.stop()
        self.nod.stop()
        self.tmp.cleanup()

    def run_row(self):
        return self.con.execute("SELECT * FROM runs WHERE id=1").fetchone()

    def as_run(self, run_id=1):
        return mock.patch.dict(os.environ, {"DROMOND_RUN_ID": str(run_id)})


# --- installation -------------------------------------------------------------

class InstallTests(HookFixture):
    def test_claude_gets_the_matcher_group_shape(self) -> None:
        path = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "settings.json"
        hooks.install_file(path, "claude")
        data = json.loads(path.read_text())
        self.assertEqual(data["hooks"]["Stop"][0]["hooks"][0]["command"],
                         "dromond hook --backend claude")
        self.assertEqual(data["hooks"]["SessionStart"][0]["hooks"][0]["command"],
                         "dromond hook --backend claude --bind")
        # The Stop hook has to outlive a human deciding on an `ask`.
        self.assertEqual(data["hooks"]["Stop"][0]["hooks"][0]["timeout"],
                         hooks.HOOK_TIMEOUT)

    def test_reasonix_gets_the_flat_shape(self) -> None:
        # Verified against reasonix v1.22.0: the nested Claude shape reports
        # `status: invalid` in `reasonix hook list --json`; flat is `active`.
        path = Path(os.environ["REASONIX_HOME"]) / "settings.json"
        hooks.install_file(path, "reasonix")
        data = json.loads(path.read_text())
        self.assertEqual(data["hooks"]["Stop"][0]["command"],
                         "dromond hook --backend reasonix")
        self.assertNotIn("hooks", data["hooks"]["Stop"][0])

    def test_install_is_idempotent_and_keeps_foreign_hooks(self) -> None:
        path = Path(os.environ["CODEX_HOME"]) / "hooks.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"hooks": {"Stop": [
            {"hooks": [{"type": "command", "command": "somebody-elses-tool"}]}]}}))
        hooks.install_file(path, "codex")
        first = json.loads(path.read_text())
        status = hooks.install_file(path, "codex")
        self.assertIn("already present", status)
        self.assertEqual(json.loads(path.read_text()), first)
        commands = [h["command"] for g in first["hooks"]["Stop"] for h in g["hooks"]]
        self.assertIn("somebody-elses-tool", commands)
        self.assertIn("dromond hook --backend codex", commands)

    def test_unreadable_config_refuses_rather_than_clobbers(self) -> None:
        path = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json at all")
        with self.assertRaises(RuntimeError):
            hooks.install_file(path, "claude")
        self.assertEqual(path.read_text(), "{not json at all")

    def test_opencode_plugin_is_per_run_not_global(self) -> None:
        hooks.install_all()
        plugin = Path(str(self.tmp_path / "home" / "hooks" / "dromond-opencode.js"))
        self.assertTrue(plugin.exists())
        self.assertIn("session.idle", plugin.read_text())
        self.assertIn("permission.asked", plugin.read_text())
        env = runners.apply_backend_env({"backend": "opencode"}, {})
        content = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(content["plugin"], [str(plugin)])

    def test_opencode_plugin_is_skipped_until_init_wrote_it(self) -> None:
        env = runners.apply_backend_env({"backend": "opencode"}, {})
        content = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        self.assertNotIn("plugin", content)  # never point at a missing file

    def test_doctor_report_names_what_is_missing(self) -> None:
        report = "\n".join(hooks.hook_report())
        self.assertIn("NOT installed", report)
        hooks.install_all()
        report = "\n".join(hooks.hook_report())
        self.assertNotIn("NOT installed", report)
        self.assertIn("codex trust", report)


class CodexTrustTests(HookFixture):
    def test_trust_is_provisioned_at_init_not_bypassed_per_spawn(self) -> None:
        hooks.install_all()
        text = (Path(os.environ["CODEX_HOME"]) / "config.toml").read_text()
        keys = [key for key, _ in hooks.codex_trust_records()]
        self.assertEqual(len(keys), 2)
        for key in keys:
            self.assertIn(f'[hooks.state."{key}"]', text)
        self.assertIn("trusted_hash = \"sha256:", text)
        self.assertIn("provisioned for 2 hook(s)", hooks.codex_trust_status())
        # The bypass flag is never added to a spawn command.
        cmd = runners.build_cmd({"backend": "codex", "name": "stub"},
                                workdir=str(self.tmp_path), title="t", prompt="p")
        self.assertNotIn("--dangerously-bypass-hook-trust", cmd)

    def test_trust_provisioning_never_overwrites_an_existing_record(self) -> None:
        hooks.install_file(Path(os.environ["CODEX_HOME"]) / "hooks.json", "codex")
        key = hooks.codex_trust_records()[0][0]
        path = Path(os.environ["CODEX_HOME"]) / "config.toml"
        path.write_text(f'[hooks.state."{key}"]\nenabled = true\n'
                        'trusted_hash = "sha256:written-by-codex-itself"\n')
        hooks.provision_codex_trust()
        text = path.read_text()
        self.assertEqual(text.count(f'[hooks.state."{key}"]'), 1)
        self.assertIn("written-by-codex-itself", text)

    def test_status_reports_missing_trust(self) -> None:
        hooks.install_file(Path(os.environ["CODEX_HOME"]) / "hooks.json", "codex")
        self.assertIn("NOT provisioned", hooks.codex_trust_status())


# --- the hook at runtime --------------------------------------------------------

class HookRuntimeTests(HookFixture):
    def test_silent_outside_a_dromond_run(self) -> None:
        # Installed user-wide: the human's own sessions must be untouched.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DROMOND_RUN_ID", None)
            self.assertIsNone(hooks.run_hook("claude", {"session_id": "abc"}))

    def test_bind_records_the_session_ref(self) -> None:
        with self.as_run():
            self.assertIsNone(hooks.run_hook("claude", {"session_id": "sess-9"},
                                             bind=True))
        self.assertEqual(self.run_row()["session_ref"], "sess-9")

    def test_bind_ignores_a_junk_session_id(self) -> None:
        with self.as_run():
            hooks.run_hook("claude", {"session_id": "; rm -rf /"}, bind=True)
        self.assertIsNone(self.run_row()["session_ref"])

    def test_stop_delivers_queued_messages_once(self) -> None:
        messaging.queue_tell(self.con, 1, "human", "use the other API", str(self.log))
        with self.as_run():
            text = hooks.run_hook("claude", {"hook_event_name": "Stop"})
        self.assertIn("use the other API", text)
        rows = traces.run_messages(self.con, 1)
        self.assertEqual(rows[0]["state"], "delivered")
        # The injection is in the trace, and the message is not handed over twice.
        kinds = [e["kind"] for e in traces.events_for_run(self.con, 1)]
        self.assertIn("human_injection", kinds)
        with self.as_run():
            self.assertIsNone(hooks.run_hook("claude", {"hook_event_name": "Stop"}))

    def test_stop_leaves_a_terminal_run_alone(self) -> None:
        messaging.queue_tell(self.con, 1, "human", "too late", str(self.log))
        self.con.execute("UPDATE runs SET status='killed' WHERE id=1")
        self.con.commit()
        with self.as_run():
            self.assertIsNone(hooks.run_hook("claude", {"hook_event_name": "Stop"}))

    def test_opencode_permission_ask_is_recorded_not_answered(self) -> None:
        with self.as_run():
            text = hooks.run_hook("opencode", {}, event="permission.asked",
                                  session="ses_1")
        self.assertIsNone(text)
        names = [e["name"] for e in traces.events_for_run(self.con, 1)]
        self.assertIn("permission.asked", names)

    def test_render_matches_each_harness(self) -> None:
        self.assertEqual(hooks.render("opencode", "go on"), "go on")
        self.assertEqual(json.loads(hooks.render("claude", "go on")),
                         {"decision": "block", "reason": "go on"})
        self.assertEqual(hooks.render("claude", None), "{}")
        self.assertEqual(hooks.render("opencode", None), "")


# --- ask ------------------------------------------------------------------------

class AskTests(HookFixture):
    def file_one(self, question="which database?"):
        return messaging.file_question(self.con, self.cfg, self.run_row(), question)

    def test_question_is_filed_and_mirrored_into_the_work_thread(self) -> None:
        request_id, seconds = self.file_one()
        self.assertLessEqual(seconds, messaging.MAX_ASK_SECONDS)
        card = self.nod.requests[request_id]
        self.assertEqual(card["channel_id"], DECISIONS_CHANNEL)
        self.assertIn("which database?", card["body_markdown"])
        self.assertTrue(card["expires_at"])          # the declared fallback
        row = self.con.execute("SELECT * FROM nod_requests WHERE request_id=?",
                               (request_id,)).fetchone()
        self.assertEqual(row["kind"], "blocked")
        self.assertEqual(row["work_item"], "W-0001")
        self.assertIn("which database?",
                      self.work.tasks["W-0001"]["log"][-1]["message"])

    def test_the_stop_hook_holds_the_session_and_injects_the_answer(self) -> None:
        request_id, _ = self.file_one()
        self.nod.resolve(request_id, text="postgres, not sqlite")  # the device
        with self.as_run():
            text = hooks.run_hook("claude", {"hook_event_name": "Stop"})
        self.assertIn("postgres, not sqlite", text)
        row = self.con.execute("SELECT * FROM nod_requests WHERE request_id=?",
                               (request_id,)).fetchone()
        self.assertEqual(row["status"], "resolved")
        self.assertIsNotNone(row["mirrored_at"])
        kinds = [m["kind"] for m in traces.run_messages(self.con, 1)]
        self.assertEqual(kinds, ["ask", "answer"])
        # Both sides reached the Work thread.
        thread = " ".join(e["message"] for e in self.work.tasks["W-0001"]["log"])
        self.assertIn("which database?", thread)
        self.assertIn("postgres, not sqlite", thread)

    def test_expiry_is_the_declared_fallback_not_a_hang(self) -> None:
        self.file_one()
        self.nod.resolve_after = None     # nobody ever answers
        with self.as_run():
            text = hooks.run_hook("claude", {"hook_event_name": "Stop"})
        self.assertIn("No answer arrived", text)
        self.assertIn("own best judgement", text)

    def test_ask_refuses_when_the_human_loop_is_off(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DROMOND_NOD_BASE_URL")
            with self.assertRaises(SystemExit):
                messaging.file_question(self.con, config.load(PROJECT_ID),
                                        self.run_row(), "anyone there?")


# --- undeliverable ----------------------------------------------------------------

class UndeliverableTests(HookFixture):
    def test_marked_and_surfaced_never_dropped_or_re_aimed(self) -> None:
        messaging.queue_tell(self.con, 1, "human", "stop touching auth.py",
                             str(self.log))
        marked = messaging.mark_undeliverable(self.con, 1, "run ended (killed)")
        self.assertEqual(marked, 1)
        # Still there, with its reason, badged for the dashboard.
        row = messaging.undeliverable(self.con)[0]
        self.assertEqual(row["body"], "stop touching auth.py")
        self.assertIn("killed", row["undeliverable_reason"])
        self.assertEqual(traces.run_messages(self.con, 1)[0]["state"],
                         "undeliverable")
        # And never handed to anything afterwards — not this run's next
        # resume, and not a later run.
        self.assertEqual(messaging.claim_pending(self.con, 1), [])
        self.con.execute(
            "INSERT INTO runs(id, profile, backend, requested_by, workdir, "
            "status, started_at) VALUES(2,'stub','claude','human','/tmp',"
            "'running',?)", (db.now(),))
        self.con.commit()
        self.assertEqual(messaging.claim_pending(self.con, 2), [])

    def test_marking_is_idempotent(self) -> None:
        messaging.queue_tell(self.con, 1, "human", "x", str(self.log))
        messaging.mark_undeliverable(self.con, 1, "first")
        self.assertEqual(messaging.mark_undeliverable(self.con, 1, "second"), 0)
        self.assertEqual(messaging.undeliverable(self.con)[0]["undeliverable_reason"],
                         "first")

    def test_a_delivered_message_is_never_marked(self) -> None:
        messaging.queue_tell(self.con, 1, "human", "landed", str(self.log))
        messaging.claim_pending(self.con, 1)
        self.assertEqual(messaging.mark_undeliverable(self.con, 1, "run ended"), 0)


if __name__ == "__main__":
    unittest.main()
