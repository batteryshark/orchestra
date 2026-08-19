"""Nod client behaviours against the stub issuer API (no live Nod server).

The load-bearing fact under all of this: a Nod issuer token is scoped to
exactly ONE channel. The stub enforces it (403 for another channel's card),
so a test that used the wrong credential would fail rather than pass by
luck.
"""
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from dromond import config, db, nod
from tests.fake_nod import (ALERTS_CHANNEL, ALERTS_TOKEN, DECISIONS_CHANNEL,
                            DECISIONS_TOKEN, FakeNod)


class NodTestCase(unittest.TestCase):
    prefix = ""

    def setUp(self) -> None:
        self.nod = FakeNod(prefix=self.prefix)
        self.url = self.nod.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ,
                                   {"DROMOND_HOME": str(self.root / "home")})
        self.env.start()
        self.con = db.connect()
        self.channels = nod.Nod({
            nod.DECISIONS: nod.NodClient(self.url, DECISIONS_CHANNEL,
                                         DECISIONS_TOKEN, role=nod.DECISIONS,
                                         timeout=5),
            nod.ALERTS: nod.NodClient(self.url, ALERTS_CHANNEL, ALERTS_TOKEN,
                                      role=nod.ALERTS, timeout=5),
        })
        self.client = self.channels.for_role(nod.DECISIONS)
        self.alerts = self.channels.for_role(nod.ALERTS)

    def tearDown(self) -> None:
        self.con.close()
        self.env.stop()
        self.tmp.cleanup()
        self.nod.stop()


class CreateTests(NodTestCase):
    def test_create_sends_only_fields_the_server_accepts(self) -> None:
        got = self.client.create(title="Deploy?", summary="api v42",
                                 body_markdown="**ship it**",
                                 fields=[{"label": "Risk", "value": "low"}],
                                 links=nod.links_for("http://work/W-1", "http://run/7"),
                                 options=[nod.ACCEPT])
        self.assertFalse(got["deduped"])
        card = self.nod.requests[got["request_id"]]
        self.assertEqual(card["title"], "Deploy?")
        self.assertEqual(card["links"][0]["label"], "Work item")
        self.assertEqual(card["links"][1]["url"], "http://run/7")

    def test_create_files_to_the_clients_own_channel(self) -> None:
        """A client IS a channel: the channel id is not a per-call argument,
        so no caller can pair one channel's id with another's token."""
        got = self.alerts.create(title="FYI")
        self.assertEqual(self.nod.requests[got["request_id"]]["channel_id"],
                         ALERTS_CHANNEL)

    def test_priority_becomes_a_card_field(self) -> None:
        # The server's create body is deny_unknown_fields and has no
        # `priority`, so sending it would 422 the whole escalation.
        got = self.client.create(title="Urgent", priority=8)
        card = self.nod.requests[got["request_id"]]
        self.assertNotIn("priority", card)
        self.assertIn({"label": "Priority", "value": "8"}, card["fields"])

    def test_unknown_field_would_be_rejected(self) -> None:
        # Guards the stub's fidelity: the real server 422s unknown fields.
        with self.assertRaises(nod.NodError) as ctx:
            self.client._call("POST", "/api/v1/requests",
                              {"title": "x", "priority": 8})
        self.assertEqual(ctx.exception.status, 422)

    def test_expires_at_is_rfc3339(self) -> None:
        got = self.client.create(title="Stale soon", expires_at=nod.expires_in(60))
        card = self.nod.requests[got["request_id"]]
        self.assertRegex(card["expires_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


class ProxyPrefixTests(NodTestCase):
    """base_url may be proxied under a path prefix; urls join onto it."""
    prefix = "/boop"

    def test_api_paths_join_onto_the_prefix(self) -> None:
        self.assertTrue(self.url.endswith("/boop"))
        rid = self.client.create(title="behind a proxy")["request_id"]
        self.assertEqual(self.client.decision(rid)["request_id"], rid)
        self.assertEqual(nod.health(self.url, timeout=5)["status"], "ok")

    def test_join_url_keeps_the_prefix_urljoin_would_drop(self) -> None:
        self.assertEqual(nod.join_url("https://h/boop", "/api/v1/requests"),
                         "https://h/boop/api/v1/requests")
        self.assertEqual(nod.join_url("https://h/boop/", "/health"),
                         "https://h/boop/health")
        self.assertEqual(nod.join_url("https://h", "/health"), "https://h/health")


class ChannelRoutingTests(NodTestCase):
    def test_decision_kinds_go_to_the_decisions_channel(self) -> None:
        for helper, args in ((nod.blocked_run, ("q?",)),
                             (nod.merge_conflict, ("conflict",)),
                             (nod.pivot_proposal, ("pivot",)),
                             (nod.failure, ("boom",))):
            got = helper(self.channels, *args, title="t", con=self.con,
                         dedupe_key=helper.__name__)
            self.assertEqual(self.nod.requests[got["request_id"]]["channel_id"],
                             DECISIONS_CHANNEL, helper.__name__)

    def test_alerts_go_to_the_alerts_channel(self) -> None:
        got = nod.alert(self.channels, "worktree pruned", title="Housekeeping",
                        con=self.con)
        self.assertEqual(self.nod.requests[got["request_id"]]["channel_id"],
                         ALERTS_CHANNEL)

    def test_a_decision_kind_on_the_alerts_client_fails_loudly(self) -> None:
        """Never quietly file a decision with the alerts credential."""
        with self.assertRaises(nod.NodChannelError) as ctx:
            nod.blocked_run(self.alerts, "q?", title="t", con=self.con)
        self.assertIn("decisions", str(ctx.exception))
        self.assertEqual(self.nod.requests, {})  # nothing left the process

    def test_an_alert_on_the_decisions_client_fails_loudly(self) -> None:
        with self.assertRaises(nod.NodChannelError):
            nod.alert(self.client, "fyi", title="t", con=self.con)
        self.assertEqual(self.nod.requests, {})

    def test_an_unknown_kind_has_no_channel_and_says_so(self) -> None:
        with self.assertRaises(nod.NodChannelError):
            nod.file_escalation(self.channels, kind="invented", title="t",
                                options=[nod.DISMISS])

    def test_the_wrong_token_really_is_rejected_by_the_server(self) -> None:
        """The guard above is not the only thing standing between the two
        channels — the server scopes the token too."""
        wrong = nod.NodClient(self.url, DECISIONS_CHANNEL, ALERTS_TOKEN,
                              role=nod.DECISIONS, timeout=5)
        with self.assertRaises(nod.NodError) as ctx:
            wrong.create(title="x")
        self.assertEqual(ctx.exception.status, 403)


class UnconfiguredChannelTests(NodTestCase):
    def test_one_configured_channel_keeps_working(self) -> None:
        only = nod.Nod({nod.DECISIONS: self.client})
        self.assertEqual(only.configured, [nod.DECISIONS])
        got = nod.blocked_run(only, "q?", title="t", con=self.con)
        self.assertEqual(self.nod.requests[got["request_id"]]["channel_id"],
                         DECISIONS_CHANNEL)

    def test_the_other_channel_reports_unconfigured_only_when_used(self) -> None:
        only = nod.Nod({nod.DECISIONS: self.client})
        with self.assertRaises(nod.NodChannelError) as ctx:
            nod.alert(only, "fyi", title="t")
        self.assertIn("alerts", str(ctx.exception))

    def test_no_channel_at_all_is_the_human_loop_being_off(self) -> None:
        self.assertEqual(nod.Nod({}).configured, [])


class WaitTests(NodTestCase):
    def test_wait_returns_cleanly_when_the_server_times_out(self) -> None:
        rid = self.client.create(title="nobody answers")["request_id"]
        got = self.client.wait(rid, timeout_seconds=1)
        self.assertTrue(got["timed_out"])
        self.assertEqual(got["status"], "pending")

    def test_wait_returns_cleanly_when_the_socket_times_out(self) -> None:
        self.nod.resolve_after = 3
        client = nod.NodClient(self.url, DECISIONS_CHANNEL, DECISIONS_TOKEN,
                               role=nod.DECISIONS, timeout=0.3)
        rid = client.create(title="slow")["request_id"]
        got = client.wait(rid, timeout_seconds=1)
        self.assertTrue(got["timed_out"])

    def test_wait_clamps_timeout_to_the_servers_range(self) -> None:
        rid = self.client.create(title="clamp me")["request_id"]
        # The stub asserts 1 <= timeout_seconds <= 60; 900 would blow up.
        self.assertTrue(self.client.wait(rid, timeout_seconds=900)["timed_out"])

    def test_wait_still_raises_on_a_real_rejection(self) -> None:
        with self.assertRaises(nod.NodError) as ctx:
            self.client.wait("req_missing", timeout_seconds=1)
        self.assertEqual(ctx.exception.status, 404)

    def test_wait_returns_the_decision(self) -> None:
        rid = self.client.create(title="answer me")["request_id"]
        self.nod.resolve_after = 0
        got = self.client.wait(rid, timeout_seconds=1)
        self.assertFalse(got["timed_out"])
        self.assertEqual(got["decision"]["option_id"], "answer")


class ReadBackTests(NodTestCase):
    """A read addressed by request id must use that request's own channel."""

    def test_the_channel_is_persisted_with_the_request_id(self) -> None:
        got = nod.alert(self.channels, "pruned", title="Housekeeping",
                        con=self.con, run_id=3)
        row = self.con.execute("SELECT channel FROM nod_requests WHERE request_id=?",
                               (got["request_id"],)).fetchone()
        self.assertEqual(row["channel"], ALERTS_CHANNEL)

    def test_for_request_picks_the_channel_the_card_was_filed_to(self) -> None:
        alert_id = nod.alert(self.channels, "pruned", title="H", con=self.con,
                             run_id=1)["request_id"]
        blocked_id = nod.blocked_run(self.channels, "q?", title="B", con=self.con,
                                     run_id=2)["request_id"]
        self.assertIs(self.channels.for_request(self.con, alert_id), self.alerts)
        self.assertIs(self.channels.for_request(self.con, blocked_id), self.client)
        # and the read actually succeeds with that credential
        self.assertEqual(
            self.channels.for_request(self.con, alert_id).decision(alert_id)["status"],
            "pending")

    def test_wait_and_cancel_use_the_same_recorded_channel(self) -> None:
        rid = nod.alert(self.channels, "pruned", title="H", con=self.con,
                        run_id=1)["request_id"]
        client = self.channels.for_request(self.con, rid)
        self.assertTrue(client.wait(rid, timeout_seconds=1)["timed_out"])
        client.cancel(rid)
        self.assertEqual(self.nod.requests[rid]["status"], "cancelled")

    def test_an_unrecorded_request_id_is_refused_not_guessed(self) -> None:
        with self.assertRaises(nod.NodChannelError) as ctx:
            self.channels.for_request(self.con, "req_stranger")
        self.assertIn("channel", str(ctx.exception))

    def test_a_channel_no_token_covers_is_refused(self) -> None:
        only = nod.Nod({nod.DECISIONS: self.client})
        nod.record(self.con, "req_9", kind="alert", channel=ALERTS_CHANNEL)
        with self.assertRaises(nod.NodChannelError) as ctx:
            only.for_request(self.con, "req_9")
        self.assertIn(ALERTS_CHANNEL, str(ctx.exception))


class EscalationKindTests(NodTestCase):
    def test_blocked_run_offers_answer_with_text(self) -> None:
        got = nod.blocked_run(self.channels, "Which database should I target?",
                              title="Run 7 is blocked", con=self.con,
                              run_id=7, work_item="W-0168")
        card = self.nod.requests[got["request_id"]]
        self.assertEqual(card["channel_id"], DECISIONS_CHANNEL)
        answer = card["options"][0]
        self.assertEqual(answer["kind"], "approve_with_text")
        self.assertTrue(answer["requires_text"])
        self.assertEqual(card["body_markdown"], "Which database should I target?")

    def test_merge_conflict_offers_retry_resolver_leave(self) -> None:
        got = nod.merge_conflict(self.channels, "3 files conflict",
                                 title="Merge conflict on run 9", run_id=9)
        card = self.nod.requests[got["request_id"]]
        self.assertEqual([o["id"] for o in card["options"]],
                         ["retry", "resolver", "leave"])

    def test_a_content_conflict_offers_a_resolver_not_a_retry(self) -> None:
        # Retry re-runs the same rebase and hits the same conflict. Offering
        # it reads as a way out when it is not one.
        got = nod.merge_conflict(self.channels, "app.py conflicts",
                                 stage="rebase",
                                 title="Merge conflict on run 9", run_id=9)
        card = self.nod.requests[got["request_id"]]
        self.assertEqual([o["id"] for o in card["options"]],
                         ["resolver", "leave"])

    def test_a_card_with_no_way_out_is_never_filed(self) -> None:
        # A notification about something the reader cannot act on is worse
        # than silence: it says "you are stuck" and hands them nothing.
        with self.assertRaises(nod.NodChannelError):
            nod.file_escalation(self.channels, kind="merge_conflict",
                                title="Stuck", options=[nod.LEAVE],
                                body_markdown="nothing you can do", run_id=9)
        self.assertEqual(self.nod.requests, {})

    def test_an_alert_may_be_dismiss_only(self) -> None:
        # Alerts report rather than ask, so a lone Dismiss is honest there.
        got = nod.alert(self.channels, "disk is filling up", title="Disk")
        self.assertIn(got["request_id"], self.nod.requests)

    def test_pivot_proposal_offers_accept_and_reject_with_reason(self) -> None:
        got = nod.pivot_proposal(self.channels, "Drop the cache layer",
                                 title="Pivot proposed", run_id=3)
        card = self.nod.requests[got["request_id"]]
        self.assertEqual([o["kind"] for o in card["options"]],
                         ["approve", "reject_with_text"])

    def test_failure_offers_retry_and_abandon(self) -> None:
        got = nod.failure(self.channels, "two infrastructure failures",
                          title="Run 4 failed twice", run_id=4)
        card = self.nod.requests[got["request_id"]]
        self.assertEqual([o["id"] for o in card["options"]], ["retry", "abandon"])

    def test_alert_is_dismiss_only_on_the_alerts_channel(self) -> None:
        got = nod.alert(self.channels, "worktree pruned", title="Housekeeping")
        card = self.nod.requests[got["request_id"]]
        self.assertEqual(card["channel_id"], ALERTS_CHANNEL)
        self.assertEqual([o["kind"] for o in card["options"]], ["dismiss"])

    def test_dedupe_key_stops_a_retried_run_buzzing_twice(self) -> None:
        first = nod.blocked_run(self.channels, "q", title="t", run_id=7,
                                work_item="W-0168", con=self.con)
        second = nod.blocked_run(self.channels, "q", title="t", run_id=7,
                                 work_item="W-0168", con=self.con)
        self.assertEqual(first["request_id"], second["request_id"])
        self.assertTrue(second["deduped"])
        self.assertEqual(len(self.nod.requests), 1)


class CallbackTests(NodTestCase):
    def test_callback_body_is_never_trusted(self) -> None:
        """A forged callback body must not become the decision Dromond acts on."""
        rid = self.client.create(title="approve me")["request_id"]
        forged = {"request_id": rid, "status": "resolved",
                  "decision": {"option_id": "accept", "option_kind": "approve"}}
        view = nod.decision_after_callback(self.client, forged["request_id"])
        self.assertEqual(view["status"], "pending")
        self.assertIsNone(view["decision"])
        self.assertIn(("GET", f"/api/v1/requests/{rid}/decision"), self.nod.calls)


class PersistenceTests(NodTestCase):
    def test_mapping_survives_for_the_work_mirror(self) -> None:
        got = nod.blocked_run(self.channels, "q?", title="Run 7 blocked",
                              con=self.con, run_id=7, work_item="W-0168")
        row = self.con.execute("SELECT * FROM nod_requests WHERE request_id=?",
                               (got["request_id"],)).fetchone()
        self.assertEqual(row["run_id"], 7)
        self.assertEqual(row["work_item"], "W-0168")
        self.assertEqual(row["kind"], "blocked")
        self.assertEqual(row["channel"], DECISIONS_CHANNEL)
        self.assertEqual(row["status"], "pending")
        self.assertEqual([r["request_id"] for r in nod.open_requests(self.con)],
                         [got["request_id"]])

    def test_decision_is_saved_then_marked_mirrored(self) -> None:
        got = nod.blocked_run(self.channels, "q?", title="t", con=self.con,
                              run_id=7, work_item="W-0168")
        rid = got["request_id"]
        self.nod.resolve(rid, option_id="answer", text="use postgres")
        client = self.channels.for_request(self.con, rid)
        nod.save_decision(self.con, rid, client.decision(rid))
        row = self.con.execute("SELECT * FROM nod_requests WHERE request_id=?",
                               (rid,)).fetchone()
        self.assertEqual(row["status"], "resolved")
        self.assertEqual(row["decision_text"], "use postgres")
        self.assertEqual(row["option_kind"], "approve_with_text")
        self.assertEqual(nod.open_requests(self.con), [])
        self.assertEqual([r["request_id"] for r in nod.unmirrored(self.con)], [rid])
        nod.mark_mirrored(self.con, rid)
        self.assertEqual(nod.unmirrored(self.con), [])

    def test_no_column_stores_a_token(self) -> None:
        columns = {r[1] for r in self.con.execute("PRAGMA table_info(nod_requests)")}
        self.assertFalse({c for c in columns if "token" in c or "secret" in c})

    def test_no_token_reaches_the_database_at_all(self) -> None:
        nod.blocked_run(self.channels, "q?", title="t", con=self.con, run_id=7)
        nod.alert(self.channels, "fyi", title="t", con=self.con, run_id=7)
        self.con.commit()
        blob = Path(os.environ["DROMOND_HOME"], "dromond.db").read_bytes()
        for token in (DECISIONS_TOKEN, ALERTS_TOKEN):
            self.assertNotIn(token.encode(), blob)


class SecretsTests(unittest.TestCase):
    """Both tokens come from a 0600 file; env overrides either one."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "nod-secrets.env"
        self.write("base_url=http://nod.invalid/boop\n"
                   "decisions_channel=chan-dec\ndecisions_token=dec-tok\n"
                   "alerts_channel=chan-alert\nalerts_token=alert-tok\n")
        self.clean = mock.patch.dict(
            os.environ, {f"{nod.ENV_PREFIX}{k.upper()}": "" for k in nod.SECRET_KEYS})
        self.clean.start()

    def tearDown(self) -> None:
        self.clean.stop()
        self.tmp.cleanup()

    def write(self, text: str, mode: int = 0o600) -> None:
        self.path.write_text(text)
        os.chmod(self.path, mode)

    def cfg(self, **over) -> dict:
        return {"nod": {"enabled": True, "secrets_file": str(self.path), **over}}

    def test_both_tokens_load_from_the_file(self) -> None:
        channels = nod.from_cfg(self.cfg())
        self.assertEqual(channels.configured, [nod.DECISIONS, nod.ALERTS])
        self.assertEqual(channels.for_role(nod.DECISIONS).channel_id, "chan-dec")
        self.assertEqual(channels.for_role(nod.ALERTS).channel_id, "chan-alert")
        self.assertEqual(channels.base_url, "http://nod.invalid/boop")

    def test_env_overrides_either_token(self) -> None:
        for role in nod.ROLES:
            with mock.patch.dict(
                    os.environ, {f"{nod.ENV_PREFIX}{role.upper()}_TOKEN": "from-env"}):
                secrets = nod.load_secrets(self.cfg())
            self.assertEqual(secrets[f"{role}_token"], "from-env")
            other = nod.DECISIONS if role == nod.ALERTS else nod.ALERTS
            self.assertNotEqual(secrets[f"{other}_token"], "from-env")

    def test_env_overrides_the_base_url(self) -> None:
        with mock.patch.dict(os.environ,
                             {f"{nod.ENV_PREFIX}BASE_URL": "http://other/boop"}):
            self.assertEqual(nod.from_cfg(self.cfg()).base_url, "http://other/boop")

    def test_secrets_file_must_be_0600(self) -> None:
        self.write(self.path.read_text(), mode=0o644)
        with self.assertRaises(SystemExit) as ctx:
            nod.load_secrets(self.cfg())
        self.assertIn("chmod 600", str(ctx.exception))

    def test_one_channel_configured_is_a_working_setup(self) -> None:
        self.write("base_url=http://nod.invalid\n"
                   "decisions_channel=chan-dec\ndecisions_token=dec-tok\n")
        channels = nod.from_cfg(self.cfg())
        self.assertEqual(channels.configured, [nod.DECISIONS])
        with self.assertRaises(nod.NodChannelError):
            channels.for_role(nod.ALERTS)

    def test_a_channel_without_its_token_is_not_half_configured(self) -> None:
        self.write("base_url=http://nod.invalid\nalerts_channel=chan-alert\n"
                   "decisions_channel=chan-dec\ndecisions_token=dec-tok\n")
        self.assertEqual(nod.from_cfg(self.cfg()).configured, [nod.DECISIONS])

    def test_client_is_none_when_the_human_loop_is_off(self) -> None:
        self.assertIsNone(nod.from_cfg({"nod": {"enabled": False}}))
        self.assertIsNone(nod.from_cfg({}))

    def test_client_is_none_when_nothing_is_configured(self) -> None:
        self.path.unlink()
        self.assertIsNone(nod.from_cfg(self.cfg()))

    def test_config_toml_tokens_are_ignored(self) -> None:
        """config.toml is shared and gets pasted into issues; a token there
        must never be picked up."""
        self.path.unlink()
        cfg = self.cfg(decisions_token="pasted-into-a-shared-file",
                       alerts_token="also-pasted", base_url="http://nod.invalid")
        self.assertIsNone(nod.from_cfg(cfg))

    def test_comments_exports_and_quotes_parse(self) -> None:
        self.write('# nod\nexport base_url="http://nod.invalid"\n\n'
                   "alerts_channel='chan-alert'\nalerts_token=alert-tok\n")
        channels = nod.from_cfg(self.cfg())
        self.assertEqual(channels.configured, [nod.ALERTS])
        self.assertEqual(channels.for_role(nod.ALERTS).channel_id, "chan-alert")


class TokenLeakTests(NodTestCase):
    def test_repr_hides_the_tokens(self) -> None:
        # A default repr lands in tracebacks and log lines; these must not.
        for text in (repr(self.client), f"{self.client}", repr(self.channels)):
            self.assertNotIn(DECISIONS_TOKEN, text)
            self.assertNotIn(ALERTS_TOKEN, text)

    def test_errors_never_carry_the_token(self) -> None:
        bad = nod.NodClient(self.url, DECISIONS_CHANNEL, "wrong-token", timeout=5)
        with self.assertRaises(nod.NodError) as ctx:
            bad.create(title="x")
        self.assertNotIn(DECISIONS_TOKEN, str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)  # no Request object in the chain

    def test_the_wrong_channel_error_names_channels_not_tokens(self) -> None:
        with self.assertRaises(nod.NodChannelError) as ctx:
            nod.alert(self.client, "fyi", title="t")
        self.assertNotIn(DECISIONS_TOKEN, str(ctx.exception))
        self.assertNotIn(ALERTS_TOKEN, str(ctx.exception))

    def test_unreachable_server_never_carries_the_token(self) -> None:
        client = nod.NodClient("http://127.0.0.1:1", DECISIONS_CHANNEL,
                               DECISIONS_TOKEN, timeout=1)
        with self.assertRaises(nod.NodError) as ctx:
            client.decision("req_1")
        self.assertNotIn(DECISIONS_TOKEN, str(ctx.exception))
        self.assertEqual(ctx.exception.status, 0)

    def test_health_is_unauthenticated(self) -> None:
        self.assertEqual(nod.health(self.url, timeout=5)["status"], "ok")
        self.assertNotIn(f"Bearer {DECISIONS_TOKEN}", self.nod.auth_seen)


class ConfigTests(unittest.TestCase):
    def test_nod_table_merges_from_the_global_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('[nod]\nenabled = true\ntimeout = 3\n')
            with mock.patch.dict(os.environ, {"DROMOND_CONFIG": str(path)}):
                cfg = config.load()
        self.assertTrue(cfg["nod"]["enabled"])
        self.assertEqual(cfg["nod"]["timeout"], 3)
        self.assertEqual(cfg["nod"]["secrets_file"], nod.DEFAULT_SECRETS_FILE)

    def test_the_default_config_holds_no_token_or_url(self) -> None:
        defaults = config.load.__globals__["tomllib"].loads(
            config.DEFAULT_CONFIG)["nod"]
        self.assertFalse({"token", "decisions_token", "alerts_token", "base_url"}
                         & set(defaults))


class CancelTests(NodTestCase):
    def test_cancel_marks_the_request_cancelled(self) -> None:
        rid = self.client.create(title="never mind")["request_id"]
        self.client.cancel(rid)
        self.assertEqual(self.nod.requests[rid]["status"], "cancelled")

    def test_cancelling_with_the_other_channels_token_is_refused(self) -> None:
        rid = self.client.create(title="never mind")["request_id"]
        with self.assertRaises(nod.NodError) as ctx:
            self.alerts.cancel(rid)
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(self.nod.requests[rid]["status"], "pending")


class ActingTestCase(NodTestCase):
    """Shared plumbing for the acting half: a stub resolver module and a
    from_cfg patch, so no test depends on the sibling branch's resolver.py
    or on real Nod configuration."""

    def setUp(self) -> None:
        super().setUp()
        self.resolver = types.ModuleType("dromond.resolver")
        self.resolver.calls = []
        stub = self.resolver

        def retry_landing(con, cfg, run_id):
            stub.calls.append(("retry", run_id))
            return "landed on retry"

        def dispatch_resolver(con, cfg, run_id, reason):
            stub.calls.append(("resolver", run_id, reason))
            return 99

        stub.retry_landing = retry_landing
        stub.dispatch_resolver = dispatch_resolver
        # The lazy `from dromond import resolver` binds whichever of these
        # exists; patch both so the stub wins before and after the merge.
        import dromond
        for patcher in (mock.patch.dict(sys.modules,
                                        {"dromond.resolver": stub}),
                        mock.patch.object(dromond, "resolver", stub,
                                          create=True),
                        mock.patch.object(nod, "from_cfg",
                                          return_value=self.channels)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _merge_card(self, run_id=7, work_item="W-0201", dedupe_key=None) -> str:
        got = nod.merge_conflict(
            self.channels, "conflict detail", con=self.con, run_id=run_id,
            work_item=work_item, title=f"run {run_id} did not land",
            dedupe_key=dedupe_key or f"test:{run_id}")
        return got["request_id"]

    def _row(self, rid):
        return self.con.execute("SELECT * FROM nod_requests WHERE request_id=?",
                                (rid,)).fetchone()


class AnswersPassTests(ActingTestCase):
    def test_an_answered_retry_card_acts_once_and_never_again(self) -> None:
        rid = self._merge_card(run_id=7)
        self.nod.resolve(rid, option_id="retry", kind="custom")
        acted = nod.act_on_answers(self.con, {})
        self.assertEqual(acted, [{"request_id": rid, "action": "retry",
                                  "outcome": "landed on retry"}])
        self.assertEqual(self.resolver.calls, [("retry", 7)])
        row = self._row(rid)
        self.assertEqual(row["status"], "resolved")
        self.assertEqual(row["option_id"], "retry")
        self.assertIsNotNone(row["acted_at"])
        # The next tick: nothing to do, nothing re-run, no network at all.
        self.nod.calls.clear()
        self.assertEqual(nod.act_on_answers(self.con, {}), [])
        self.assertEqual(self.resolver.calls, [("retry", 7)])
        self.assertEqual(self.nod.calls, [])

    def test_a_resolver_answer_dispatches_with_the_owners_text(self) -> None:
        rid = self._merge_card(run_id=8)
        self.nod.resolve(rid, option_id="resolver", kind="custom",
                         text="mind the schema migration")
        acted = nod.act_on_answers(self.con, {})
        self.assertEqual(acted[0]["action"], "resolver")
        self.assertEqual(acted[0]["outcome"], "dispatched run 99")
        self.assertEqual(self.resolver.calls,
                         [("resolver", 8, "mind the schema migration")])

    def test_leave_acts_without_touching_the_resolver(self) -> None:
        rid = self._merge_card(run_id=9)
        self.nod.resolve(rid, option_id="leave", kind="dismiss")
        acted = nod.act_on_answers(self.con, {})
        self.assertEqual(acted[0]["action"], "leave")
        self.assertEqual(self.resolver.calls, [])
        self.assertIsNotNone(self._row(rid)["acted_at"])

    def test_a_dismissed_card_is_recorded_and_nothing_runs(self) -> None:
        rid = self._merge_card(run_id=10)
        self.nod.requests[rid]["status"] = "dismissed"  # swiped away, no option
        acted = nod.act_on_answers(self.con, {})
        self.assertEqual(acted[0]["action"], "leave")
        self.assertEqual(self.resolver.calls, [])
        self.assertEqual(self._row(rid)["status"], "dismissed")
        self.assertIsNotNone(self._row(rid)["acted_at"])

    def test_an_unanswered_card_is_untouched_and_not_marked(self) -> None:
        rid = self._merge_card(run_id=11)
        self.assertEqual(nod.act_on_answers(self.con, {}), [])
        row = self._row(rid)
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["acted_at"])
        # ...and it is still eligible next tick, once the owner answers.
        self.nod.resolve(rid, option_id="retry", kind="custom")
        self.assertEqual(nod.act_on_answers(self.con, {})[0]["action"], "retry")

    def test_no_unacted_rows_means_no_network_and_no_config_load(self) -> None:
        nod.from_cfg.reset_mock()
        self.nod.calls.clear()
        self.assertEqual(nod.act_on_answers(self.con, {}), [])
        self.assertEqual(self.nod.calls, [])
        nod.from_cfg.assert_not_called()

    def test_a_card_the_mirror_saw_first_is_still_acted_on(self) -> None:
        # Another reader (the mirror flow) saved the decision already: the
        # status is no longer 'pending', but nothing has acted. Act from the
        # stored columns, with no second decision read.
        rid = self._merge_card(run_id=12)
        self.nod.resolve(rid, option_id="retry", kind="custom")
        nod.save_decision(self.con, rid, self.client.decision(rid))
        self.nod.calls.clear()
        acted = nod.act_on_answers(self.con, {})
        self.assertEqual(acted[0]["action"], "retry")
        self.assertEqual(self.resolver.calls, [("retry", 12)])
        self.assertEqual(self.nod.calls, [])
        # ...and the mirror still sees it: acted, but not yet mirrored.
        self.assertIn(rid, [r["request_id"] for r in nod.unmirrored(self.con)])

    def test_an_unreadable_card_is_skipped_and_the_rest_still_act(self) -> None:
        # A row recorded against a channel no token covers cannot be read;
        # it is a warning, not a reason to skip the cards after it.
        nod.record(self.con, "req_gone", kind="merge_conflict", channel="chan-gone",
                   run_id=13)
        rid = self._merge_card(run_id=14)
        self.nod.resolve(rid, option_id="retry", kind="custom")
        acted = nod.act_on_answers(self.con, {})
        self.assertEqual([a["request_id"] for a in acted], [rid])
        self.assertIsNone(self._row("req_gone")["acted_at"])

    def test_an_acted_card_never_swallows_the_next_escalation(self) -> None:
        # File -> owner answers Retry -> the pass acts -> the retry escalates
        # AGAIN. The fresh card must reach the phone: the acted card's dedupe
        # key must not be reused, or the server swallows the re-file and the
        # owner never learns the retry failed.
        first = nod.merge_conflict(self.channels, "conflict", con=self.con,
                                   run_id=30, title="t")
        self.nod.resolve(first["request_id"], option_id="retry", kind="custom")
        nod.act_on_answers(self.con, {})
        second = nod.merge_conflict(self.channels, "conflict again",
                                    con=self.con, run_id=30, title="t")
        self.assertFalse(second["deduped"])
        self.assertNotEqual(first["request_id"], second["request_id"])
        self.assertNotEqual(
            self.nod.requests[first["request_id"]]["dedupe_key"],
            self.nod.requests[second["request_id"]]["dedupe_key"])
        # The acted row survived the second filing untouched: still acted.
        self.assertIsNotNone(self._row(first["request_id"])["acted_at"])
        # But an open, un-acted escalation still dedupes a re-file.
        third = nod.merge_conflict(self.channels, "conflict again",
                                   con=self.con, run_id=30, title="t")
        self.assertTrue(third["deduped"])
        self.assertEqual(third["request_id"], second["request_id"])

    def test_a_withdrawn_card_never_swallows_the_next_escalation(self) -> None:
        # The other route to the same trap: the card was withdrawn because
        # the merge landed, then a later merge for the same run escalates.
        first = nod.merge_conflict(self.channels, "conflict", con=self.con,
                                   run_id=31, title="t")
        nod.withdraw_merge_cards(self.con, {}, 31, note="landed")
        second = nod.merge_conflict(self.channels, "conflict again",
                                    con=self.con, run_id=31, title="t")
        self.assertFalse(second["deduped"])
        self.assertNotEqual(first["request_id"], second["request_id"])

    def test_non_merge_cards_are_never_acted_on(self) -> None:
        got = nod.blocked_run(self.channels, "q?", title="t", con=self.con,
                              run_id=15)
        self.nod.resolve(got["request_id"], option_id="answer", text="yes")
        self.assertEqual(nod.act_on_answers(self.con, {}), [])
        self.assertEqual(self.resolver.calls, [])


class WithdrawTests(ActingTestCase):
    def test_withdraw_cancels_every_pending_card_for_the_run(self) -> None:
        first = self._merge_card(run_id=20, dedupe_key="a")
        second = self._merge_card(run_id=20, dedupe_key="b")
        other = self._merge_card(run_id=21)
        self.assertEqual(
            nod.withdraw_merge_cards(self.con, {}, 20, note="merge landed"), 2)
        for rid in (first, second):
            self.assertEqual(self.nod.requests[rid]["status"], "cancelled")
            row = self._row(rid)
            self.assertEqual(row["status"], "withdrawn")
            self.assertEqual(row["decision_text"], "merge landed")
            self.assertIsNotNone(row["acted_at"])
        self.assertEqual(self._row(other)["status"], "pending")
        # Withdrawn cards are out of the answers pass's reach for good.
        self.assertEqual([a["request_id"]
                          for a in nod.act_on_answers(self.con, {})], [])

    def test_withdraw_survives_a_failed_cancel_and_still_marks(self) -> None:
        nod.record(self.con, "req_gone", kind="merge_conflict", channel="chan-gone",
                   run_id=22)
        good = self._merge_card(run_id=22)
        self.assertEqual(nod.withdraw_merge_cards(self.con, {}, 22), 2)
        self.assertEqual(self._row("req_gone")["status"], "withdrawn")
        self.assertEqual(self._row(good)["status"], "withdrawn")
        self.assertEqual(self.nod.requests[good]["status"], "cancelled")

    def test_withdraw_with_nothing_pending_is_a_no_op(self) -> None:
        self.assertEqual(nod.withdraw_merge_cards(self.con, {}, 404), 0)


class StagePrefixTests(NodTestCase):
    def test_stage_prefixes_the_summary(self) -> None:
        got = nod.merge_conflict(self.channels, "held", stage="tripwires",
                                 title="t", summary="2 findings hold the door")
        card = self.nod.requests[got["request_id"]]
        self.assertEqual(card["summary"], "[tripwires] 2 findings hold the door")

    def test_no_stage_leaves_existing_calls_unchanged(self) -> None:
        got = nod.merge_conflict(self.channels, "held", title="t",
                                 summary="plain")
        self.assertEqual(self.nod.requests[got["request_id"]]["summary"], "plain")

    def test_a_dirty_card_offers_no_resolver(self) -> None:
        # A resolver's only route to "landing" a dirty base is to stash the
        # owner's work in flight and hope the pop succeeds (run 62). Retry is
        # real: it lands once they have committed or stashed it themselves.
        got = nod.merge_conflict(self.channels, "your edits overlap",
                                 stage="dirty", title="t", summary="s")
        card = self.nod.requests[got["request_id"]]
        self.assertEqual([o["id"] for o in card["options"]], ["retry", "leave"])


if __name__ == "__main__":
    unittest.main()
