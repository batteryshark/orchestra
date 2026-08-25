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

from orchestra import config, db, dispatch, nod
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
                                   {"ORCHESTRA_HOME": str(self.root / "home")})
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
    def test_create_sends_the_supported_card_shape(self) -> None:
        got = self.client.create(title="Deploy?", summary="api v42",
                                 body_markdown="**ship it**",
                                 fields=[{"label": "Risk", "value": "low"}],
                                 links=nod.links_for("http://work/W-1", "http://run/7"),
                                 options=[nod.ACCEPT], priority=8,
                                 expires_at=nod.expires_in(60))
        self.assertFalse(got["deduped"])
        card = self.nod.requests[got["request_id"]]
        self.assertEqual(card["title"], "Deploy?")
        self.assertEqual(card["channel_id"], DECISIONS_CHANNEL)
        self.assertEqual(card["links"][0]["label"], "Work item")
        self.assertEqual(card["links"][1]["url"], "http://run/7")
        # The server's create body is deny_unknown_fields and has no
        # `priority`, so sending it would 422 the whole escalation.
        self.assertNotIn("priority", card)
        self.assertIn({"label": "Priority", "value": "8"}, card["fields"])
        self.assertRegex(card["expires_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


class ProxyPrefixTests(NodTestCase):
    """base_url may be proxied under a path prefix; urls join onto it."""
    prefix = "/boop"

    def test_api_paths_join_onto_the_prefix(self) -> None:
        self.assertTrue(self.url.endswith("/boop"))
        rid = self.client.create(title="behind a proxy")["request_id"]
        self.assertEqual(self.client.decision(rid)["request_id"], rid)
        self.assertEqual(nod.health(self.url, timeout=5)["status"], "ok")


class ChannelRoutingTests(NodTestCase):
    def test_invalid_channel_routes_fail_before_filing(self) -> None:
        cases = {
            "decision via alerts": (
                lambda: nod.blocked_run(self.alerts, "q?", title="t",
                                        con=self.con), "decisions"),
            "alert via decisions": (
                lambda: nod.alert(self.client, "fyi", title="t", con=self.con),
                "alerts"),
            "unknown kind": (
                lambda: nod.file_escalation(
                    self.channels, kind="invented", title="t",
                    options=[nod.DISMISS]), "unknown escalation kind"),
        }
        for label, (file_card, expected) in cases.items():
            with self.subTest(label), self.assertRaises(
                    nod.NodChannelError) as ctx:
                file_card()
            self.assertIn(expected, str(ctx.exception))
            self.assertNotIn(DECISIONS_TOKEN, str(ctx.exception))
            self.assertNotIn(ALERTS_TOKEN, str(ctx.exception))
            self.assertEqual(self.nod.requests, {})

    def test_the_wrong_token_really_is_rejected_by_the_server(self) -> None:
        """The guard above is not the only thing standing between the two
        channels — the server scopes the token too."""
        wrong = nod.NodClient(self.url, DECISIONS_CHANNEL, ALERTS_TOKEN,
                              role=nod.DECISIONS, timeout=5)
        with self.assertRaises(nod.NodError) as ctx:
            wrong.create(title="x")
        self.assertEqual(ctx.exception.status, 403)


class UnconfiguredChannelTests(NodTestCase):
    def test_one_configured_channel_works_until_the_other_is_used(self) -> None:
        only = nod.Nod({nod.DECISIONS: self.client})
        self.assertEqual(only.configured, [nod.DECISIONS])
        got = nod.blocked_run(only, "q?", title="t", con=self.con)
        self.assertEqual(self.nod.requests[got["request_id"]]["channel_id"],
                         DECISIONS_CHANNEL)
        with self.assertRaises(nod.NodChannelError) as ctx:
            nod.alert(only, "fyi", title="t")
        self.assertIn("alerts", str(ctx.exception))


class WaitTests(NodTestCase):
    def test_wait_timeouts_return_pending(self) -> None:
        with self.subTest("server timeout"):
            rid = self.client.create(title="nobody answers")["request_id"]
            got = self.client.wait(rid, timeout_seconds=1)
            self.assertTrue(got["timed_out"])
            self.assertEqual(got["status"], "pending")
        with self.subTest("socket timeout"):
            self.nod.resolve_after = 3
            client = nod.NodClient(
                self.url, DECISIONS_CHANNEL, DECISIONS_TOKEN,
                role=nod.DECISIONS, timeout=0.3)
            rid = client.create(title="slow")["request_id"]
            self.assertTrue(client.wait(rid, timeout_seconds=1)["timed_out"])

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

    def test_unknown_channels_are_refused_not_guessed(self) -> None:
        only = nod.Nod({nod.DECISIONS: self.client})
        nod.record(self.con, "req_9", kind="alert", channel=ALERTS_CHANNEL)
        cases = {
            "unrecorded request": (
                lambda: self.channels.for_request(self.con, "req_stranger"),
                "channel"),
            "unconfigured recorded channel": (
                lambda: only.for_request(self.con, "req_9"), ALERTS_CHANNEL),
        }
        for label, (read, expected) in cases.items():
            with self.subTest(label), self.assertRaises(
                    nod.NodChannelError) as ctx:
                read()
            self.assertIn(expected, str(ctx.exception))


class EscalationKindTests(NodTestCase):
    def test_escalation_helpers_offer_actionable_options_on_the_right_channel(
            self) -> None:
        cases = {
            "blocked question": (
                nod.blocked_run, "Which database?", {}, DECISIONS_CHANNEL,
                [("answer", "approve_with_text", True),
                 ("stop", "reject", False)]),
            "merge conflict": (
                nod.merge_conflict, "3 files conflict", {}, DECISIONS_CHANNEL,
                [("retry", "custom", False), ("resolver", "custom", False),
                 ("leave", "dismiss", False)]),
            "rebase conflict": (
                nod.merge_conflict, "app.py conflicts", {"stage": "rebase"},
                DECISIONS_CHANNEL,
                [("resolver", "custom", False), ("leave", "dismiss", False)]),
            "dirty worktree": (
                nod.merge_conflict, "your edits overlap", {"stage": "dirty"},
                DECISIONS_CHANNEL,
                [("retry", "custom", False), ("leave", "dismiss", False)]),
            "pivot": (
                nod.pivot_proposal, "Drop the cache", {}, DECISIONS_CHANNEL,
                [("accept", "approve", False),
                 ("reject", "reject_with_text", True)]),
            "failure": (
                nod.failure, "two failures", {}, DECISIONS_CHANNEL,
                [("retry", "custom", False), ("abandon", "reject", False)]),
            "alert": (
                nod.alert, "worktree pruned", {}, ALERTS_CHANNEL,
                [("ok", "dismiss", False)]),
        }
        for label, (helper, detail, kwargs, channel, options) in cases.items():
            with self.subTest(label):
                got = helper(self.channels, detail, title="t",
                             dedupe_key=f"test:{label}", **kwargs)
                card = self.nod.requests[got["request_id"]]
                self.assertEqual(card["channel_id"], channel)
                self.assertEqual(card["body_markdown"], detail)
                self.assertEqual([
                    (option["id"], option["kind"],
                     bool(option.get("requires_text")))
                    for option in card["options"]], options)

    def test_a_card_with_no_way_out_is_never_filed(self) -> None:
        # A notification about something the reader cannot act on is worse
        # than silence: it says "you are stuck" and hands them nothing.
        with self.assertRaises(nod.NodChannelError):
            nod.file_escalation(self.channels, kind="merge_conflict",
                                title="Stuck", options=[nod.LEAVE],
                                body_markdown="nothing you can do", run_id=9)
        self.assertEqual(self.nod.requests, {})

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
        """A forged callback body must not become the decision Orchestra acts on."""
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

    def test_no_token_reaches_the_database_at_all(self) -> None:
        nod.blocked_run(self.channels, "q?", title="t", con=self.con, run_id=7)
        nod.alert(self.channels, "fyi", title="t", con=self.con, run_id=7)
        self.con.commit()
        blob = Path(os.environ["ORCHESTRA_HOME"], "orchestra.db").read_bytes()
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

    def test_env_overrides_each_secret_independently(self) -> None:
        cases = {
            "decisions token": ("DECISIONS_TOKEN", "decisions_token", "from-env"),
            "alerts token": ("ALERTS_TOKEN", "alerts_token", "from-env"),
            "base url": ("BASE_URL", "base_url", "http://other/boop"),
        }
        for label, (suffix, key, value) in cases.items():
            with self.subTest(label), mock.patch.dict(
                    os.environ, {f"{nod.ENV_PREFIX}{suffix}": value}):
                secrets = nod.load_secrets(self.cfg())
            self.assertEqual(secrets[key], value)

    def test_secrets_file_must_be_0600(self) -> None:
        self.write(self.path.read_text(), mode=0o644)
        with self.assertRaises(SystemExit) as ctx:
            nod.load_secrets(self.cfg())
        self.assertIn("chmod 600", str(ctx.exception))

    def test_only_complete_channel_pairs_are_configured(self) -> None:
        cases = {
            "one complete channel": (
                "base_url=http://nod.invalid\n"
                "decisions_channel=chan-dec\ndecisions_token=dec-tok\n"),
            "other channel lacks token": (
                "base_url=http://nod.invalid\nalerts_channel=chan-alert\n"
                "decisions_channel=chan-dec\ndecisions_token=dec-tok\n"),
        }
        for label, contents in cases.items():
            with self.subTest(label):
                self.write(contents)
                channels = nod.from_cfg(self.cfg())
                self.assertEqual(channels.configured, [nod.DECISIONS])
                with self.assertRaises(nod.NodChannelError):
                    channels.for_role(nod.ALERTS)

    def test_disabled_or_empty_configuration_returns_none(self) -> None:
        for label, cfg in {
                "disabled": {"nod": {"enabled": False}},
                "missing nod table": {}}.items():
            with self.subTest(label):
                self.assertIsNone(nod.from_cfg(cfg))
        with self.subTest("missing secrets file"):
            self.path.unlink()
            self.assertIsNone(nod.from_cfg(self.cfg()))

    def test_config_toml_tokens_are_ignored(self) -> None:
        """config.toml is shared and gets pasted into issues; a token there
        must never be picked up."""
        self.path.unlink()
        cfg = self.cfg(decisions_token="pasted-into-a-shared-file",
                       alerts_token="also-pasted", base_url="http://nod.invalid")
        self.assertIsNone(nod.from_cfg(cfg))


class TokenLeakTests(NodTestCase):
    def test_repr_hides_the_tokens(self) -> None:
        # A default repr lands in tracebacks and log lines; these must not.
        for text in (repr(self.client), f"{self.client}", repr(self.channels)):
            self.assertNotIn(DECISIONS_TOKEN, text)
            self.assertNotIn(ALERTS_TOKEN, text)

    def test_transport_errors_hide_tokens_and_request_objects(self) -> None:
        cases = {
            "http rejection": (
                nod.NodClient(self.url, DECISIONS_CHANNEL, "wrong-token",
                              timeout=5),
                lambda client: client.create(title="x"), "wrong-token", 401),
            "unreachable server": (
                nod.NodClient("http://127.0.0.1:1", DECISIONS_CHANNEL,
                              DECISIONS_TOKEN, timeout=1),
                lambda client: client.decision("req_1"), DECISIONS_TOKEN, 0),
        }
        for label, (client, request, token, status) in cases.items():
            with self.subTest(label), self.assertRaises(nod.NodError) as ctx:
                request(client)
            self.assertNotIn(token, str(ctx.exception))
            self.assertEqual(ctx.exception.status, status)
            self.assertIsNone(ctx.exception.__cause__)

    def test_health_is_unauthenticated(self) -> None:
        self.assertEqual(nod.health(self.url, timeout=5)["status"], "ok")
        self.assertNotIn(f"Bearer {DECISIONS_TOKEN}", self.nod.auth_seen)


class ConfigTests(unittest.TestCase):
    def test_the_default_config_holds_no_token_or_url(self) -> None:
        defaults = config.load.__globals__["tomllib"].loads(
            config.DEFAULT_CONFIG)["nod"]
        self.assertFalse({"token", "decisions_token", "alerts_token", "base_url"}
                         & set(defaults))

class ActingTestCase(NodTestCase):
    """Shared plumbing for the acting half: a stub resolver module and a
    from_cfg patch, so no test depends on the sibling branch's resolver.py
    or on real Nod configuration."""

    def setUp(self) -> None:
        super().setUp()
        self.resolver = types.ModuleType("orchestra.resolver")
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
        # The lazy `from orchestra import resolver` binds whichever of these
        # exists; patch both so the stub wins before and after the merge.
        import orchestra
        for patcher in (mock.patch.dict(sys.modules,
                                        {"orchestra.resolver": stub}),
                        mock.patch.object(orchestra, "resolver", stub,
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
        dispatch.pause(self.con)
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
        dispatch.pause(self.con)
        self.assertEqual(nod.act_on_answers(self.con, {}), [])
        self.assertEqual(self._row(rid)["status"], "resolved")
        self.assertIsNone(self._row(rid)["acted_at"])
        self.assertEqual(self.resolver.calls, [])
        dispatch.resume(self.con)
        acted = nod.act_on_answers(self.con, {})
        self.assertEqual(acted[0]["action"], "resolver")
        self.assertEqual(acted[0]["outcome"], "dispatched run 99")
        self.assertEqual(self.resolver.calls,
                         [("resolver", 8, "mind the schema migration")])

    def test_non_action_answers_are_recorded_without_running_a_resolver(self) -> None:
        cases = {"leave option": (9, "leave", "resolved"),
                 "swiped dismissal": (10, None, "dismissed")}
        for label, (run_id, option, status) in cases.items():
            with self.subTest(label):
                rid = self._merge_card(run_id=run_id)
                if option:
                    self.nod.resolve(rid, option_id=option, kind="dismiss")
                else:
                    self.nod.requests[rid]["status"] = status
                acted = nod.act_on_answers(self.con, {})
                self.assertEqual(acted[0]["action"], "leave")
                self.assertEqual(self.resolver.calls, [])
                self.assertEqual(self._row(rid)["status"], status)
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

    def test_terminal_cards_never_swallow_the_next_escalation(self) -> None:
        for label, run_id in {"acted": 30, "withdrawn": 31}.items():
            with self.subTest(label):
                first = nod.merge_conflict(
                    self.channels, "conflict", con=self.con,
                    run_id=run_id, title="t")
                if label == "acted":
                    self.nod.resolve(first["request_id"], option_id="retry",
                                     kind="custom")
                    nod.act_on_answers(self.con, {})
                else:
                    nod.withdraw_merge_cards(self.con, {}, run_id, note="landed")
                second = nod.merge_conflict(
                    self.channels, "conflict again", con=self.con,
                    run_id=run_id, title="t")
                self.assertFalse(second["deduped"])
                self.assertNotEqual(first["request_id"], second["request_id"])
                self.assertIsNotNone(self._row(first["request_id"])["acted_at"])
                third = nod.merge_conflict(
                    self.channels, "conflict again", con=self.con,
                    run_id=run_id, title="t")
                self.assertTrue(third["deduped"])
                self.assertEqual(third["request_id"], second["request_id"])

    def test_non_merge_cards_are_never_acted_on(self) -> None:
        got = nod.blocked_run(self.channels, "q?", title="t", con=self.con,
                              run_id=15)
        self.nod.resolve(got["request_id"], option_id="answer", text="yes")
        self.assertEqual(nod.act_on_answers(self.con, {}), [])
        self.assertEqual(self.resolver.calls, [])


class WithdrawTests(ActingTestCase):
    def test_withdraw_cancels_pending_cards_and_survives_failed_cancels(self) -> None:
        first = self._merge_card(run_id=20, dedupe_key="a")
        second = self._merge_card(run_id=20, dedupe_key="b")
        nod.record(self.con, "req_gone", kind="merge_conflict",
                   channel="chan-gone", run_id=20)
        other = self._merge_card(run_id=21)
        self.assertEqual(
            nod.withdraw_merge_cards(self.con, {}, 20, note="merge landed"), 3)
        for rid in (first, second):
            self.assertEqual(self.nod.requests[rid]["status"], "cancelled")
            row = self._row(rid)
            self.assertEqual(row["status"], "withdrawn")
            self.assertEqual(row["decision_text"], "merge landed")
            self.assertIsNotNone(row["acted_at"])
        self.assertEqual(self._row("req_gone")["status"], "withdrawn")
        self.assertEqual(self._row(other)["status"], "pending")
        # Withdrawn cards are out of the answers pass's reach for good.
        self.assertEqual([a["request_id"]
                          for a in nod.act_on_answers(self.con, {})], [])

class StagePrefixTests(NodTestCase):
    def test_stage_prefixes_the_summary(self) -> None:
        got = nod.merge_conflict(self.channels, "held", stage="tripwires",
                                 title="t", summary="2 findings hold the door")
        card = self.nod.requests[got["request_id"]]
        self.assertEqual(card["summary"], "[tripwires] 2 findings hold the door")

if __name__ == "__main__":
    unittest.main()
