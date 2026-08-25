"""Runway adapters (DESIGN §11) against fixture data — never the user's real
files, never a live endpoint, and never a real key. Two things are asserted
throughout: the happy parse, and that every failure mode returns
unknown-with-reason instead of raising."""
import json
import os
import tempfile
import time
import pathlib
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from orchestra import db, runway

# Every adapter that needs a credential is pointed at this: an auth.json that
# is not there, so the lookup falls through to an environment variable the
# test also clears. No fixture in this file holds key-shaped material.
NO_AUTH = "/nonexistent/opencode/auth.json"
NO_KEYS = {env: "" for _, env in runway.KEY_SOURCES.values()}


def _iso_in(hours: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _epoch_in(hours: float) -> int:
    return int(time.time() + hours * 3600)


DEEPSEEK_OK = {
    "is_available": True,
    "balance_infos": [
        {"currency": "CNY", "total_balance": "72.00", "granted_balance": "0.00",
         "topped_up_balance": "72.00"},
        {"currency": "USD", "total_balance": "10.50", "granted_balance": "0.50",
         "topped_up_balance": "10.00"},
    ],
}

# Kimi answers with decimal STRINGS and describes its window rather than
# naming it. ``limits[]`` holds only the 5-hour burst window; the plan-wide
# quota lives in ``usage``, stated as USED and with its own reset — here fully
# consumed, the live case that sent W-0184 (verified 2026-08-14).
KIMI_OK = {
    "usage": {"limit": "1000", "used": "1000", "resetTime": _iso_in(44)},
    "limits": [{"window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                "detail": {"limit": "1000", "remaining": "640",
                           "resetTime": _iso_in(3)}}],
    "user": {"membership": {"level": "pro"}},
    "subType": "TYPE_PURCHASE",
}


def minimax_fixture(interval=64, weekly=88, status=0):
    now_ms = time.time() * 1000
    row = {"model_name": "general",
           "start_time": now_ms - 3600_000, "end_time": now_ms + 14400_000,
           "current_interval_remaining_percent": interval,
           "weekly_start_time": now_ms - 86400_000,
           "weekly_end_time": now_ms + 518400_000,
           "current_weekly_remaining_percent": weekly}
    return {"model_remains": [dict(row, model_name="video"), row],
            "base_resp": {"status_code": status, "status_msg": "ok"}}


def claude_fixture(five_hour_used=12, seven_day_used=40, age_min=30):
    return {
        "someOtherKey": "ignored",
        "cachedUsageUtilization": {
            "fetchedAtMs": (time.time() - age_min * 60) * 1000,
            "accountUuid": "00000000-0000-0000-0000-000000000000",
            "utilization": {
                "five_hour": {"utilization": five_hour_used,
                              "resets_at": _iso_in(2),
                              "limit_dollars": None},
                "seven_day": {"utilization": seven_day_used,
                              "resets_at": _iso_in(80)},
                "seven_day_opus": None,
                "extra_usage": {"is_enabled": False},
            },
        },
    }


def codex_lines(used=25.0, resets_in_h=6, secondary=None):
    other = json.dumps({"timestamp": "2026-08-13T10:00:00.000Z",
                        "type": "event_msg", "payload": {"type": "agent_message"}})
    event = json.dumps({
        "timestamp": "2026-08-13T10:05:00.000Z",
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {"total_token_usage": {"total_tokens": 1234},
                     "model_context_window": 258400},
            "rate_limits": {
                "limit_id": "codex", "plan_type": "prolite",
                "primary": {"used_percent": used, "window_minutes": 10080,
                            "resets_at": _epoch_in(resets_in_h)},
                "secondary": secondary,
                "credits": {"has_credits": False, "balance": "0"},
                "rate_limit_reached_type": None,
            },
        },
    })
    stale = event.replace('"used_percent": %s' % used, '"used_percent": 99.0')
    return [other, stale, event]  # the LAST token_count event wins


class ResultShapeTests(unittest.TestCase):
    def test_every_adapter_returns_the_same_record(self) -> None:
        names = []
        with mock.patch.dict(os.environ, NO_KEYS):
            for adapter in runway.ADAPTERS:
                with self.subTest(adapter.__name__):
                    got = adapter(**({"sessions_dir": "/nonexistent"}
                                     if adapter is runway.codex else
                                     {"path": "/nonexistent"}
                                     if adapter is runway.claude else
                                     {"auth_path": NO_AUTH}))
                    self.assertIsInstance(got, runway.Runway)
                    self.assertFalse(got.known)
                    self.assertTrue(got.reason)
                    # W-0182 removed ``limit``: a window's limit is always
                    # 100% of the window, so the field said nothing.
                    self.assertEqual(set(got.as_dict()), {
                        "provider", "remaining", "unit", "resets_at", "raw",
                        "as_of", "reason", "kind", "stale", "windows"})
                    names.append(got.provider)
        self.assertEqual(sorted(names),
                         ["claude", "codex", "deepseek", "kimi", "minimax", "xai"])
        self.assertEqual(len(names), len(set(names)))

    def test_provider_kind_splits_subscriptions_from_metered_apis(self) -> None:
        """W-0179: only an api provider has money at all."""
        # A run's provider is its backend, or the model's prefix when the
        # backend routes (opencode/reasonix).
        cases = {
            "claude backend": ("claude", "claude-opus-5", "claude", "plan"),
            "codex backend": ("codex", "gpt-5.6-luna", "codex", "plan"),
            "kimi prefix": ("opencode", "kimi-for-coding/k3", "kimi", "plan"),
            "minimax prefix": (
                "opencode", "minimax-coding-plan/m3", "minimax", "plan"),
            "xai prefix": ("opencode", "xai/grok-4.6", "xai", "plan"),
            "metered prefix": ("reasonix", "deepseek/deepseek-v4-flash",
                               "deepseek", "api"),
            "missing model": ("opencode", None, "opencode", "api"),
        }
        for label, (backend, model, provider, kind) in cases.items():
            with self.subTest(label):
                got = runway.provider_of(backend, model)
                self.assertEqual(got, provider)
                self.assertEqual(runway.kind_of(got), kind)


class KeyLookupTests(unittest.TestCase):
    """W-0182: a key comes from OpenCode's store, then the named environment
    variable. Nothing here uses or asserts a real credential."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.auth = Path(self.tmp.name) / "auth.json"
        self.addCleanup(self.tmp.cleanup)

    def _write(self, data) -> Path:
        self.auth.write_text(json.dumps(data))
        return self.auth

    def test_opencode_entry_wins_over_the_environment(self) -> None:
        self._write({"deepseek": {"type": "api", "key": "from-opencode"}})
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "from-env"}):
            value, source = runway.api_key("deepseek", self.auth)
        self.assertEqual(value, "from-opencode")
        self.assertIn("deepseek", source)
        self.assertNotIn(value, source)

    def test_an_oauth_entry_uses_its_access_token(self) -> None:
        """OpenCode stores xai as a grant, not an api key."""
        self._write({"xai": {"type": "oauth", "access": "grant",
                             "refresh": "r", "expires": 1}})
        self.assertEqual(runway.api_key("xai", self.auth)[0], "grant")

    def test_the_named_variable_is_the_fallback(self) -> None:
        cases = {
            "blank store entry": self._write({"deepseek": {"key": "  "}}),
            "missing store": Path("/nonexistent/auth.json"),
        }
        for label, auth in cases.items():
            with self.subTest(label), \
                    mock.patch.dict(os.environ,
                                    {"DEEPSEEK_API_KEY": "from-env"}):
                self.assertEqual(runway.api_key("deepseek", auth),
                                 ("from-env", "$DEEPSEEK_API_KEY"))

    def test_no_key_anywhere_names_both_places_it_looked(self) -> None:
        self.auth.write_text("{not json")
        with mock.patch.dict(os.environ, NO_KEYS):
            value, reason = runway.api_key("minimax", self.auth)
        self.assertIsNone(value)
        self.assertIn("minimax-coding-plan", reason)
        self.assertIn("MINIMAX_API_KEY", reason)

class DeepSeekTests(unittest.TestCase):
    def test_prefers_usd_balance(self) -> None:
        got = runway.parse_deepseek(DEEPSEEK_OK)
        self.assertTrue(got.known)
        self.assertEqual((got.provider, got.remaining, got.unit),
                         ("deepseek", 10.5, "USD"))
        self.assertIsNone(got.resets_at)  # a prepaid balance has no window
        self.assertEqual(got.kind, "api")
        self.assertEqual(got.windows, [])

    def test_missing_and_garbage_shapes_are_unknown(self) -> None:
        cases = {
            "missing balances": {},
            "missing total": {"balance_infos": [{"currency": "USD"}]},
            "nonnumeric total": {"balance_infos": [{"total_balance": "abc"}]},
        }
        for label, data in cases.items():
            with self.subTest(label):
                got = runway.parse_deepseek(data)
                self.assertFalse(got.known)
                self.assertTrue(got.reason)

class KimiTests(unittest.TestCase):
    def test_both_the_burst_window_and_the_plan_quota_are_reported(self) -> None:
        """W-0184: ``limits[]`` is only the 5-hour window. The plan quota in
        ``usage`` is a second window, and the one the owner runs out of."""
        got = runway.parse_kimi(KIMI_OK)
        # 640 of 1000 left over a 300-minute window; the plan quota is spent.
        self.assertEqual([(w["label"], w["remaining"]) for w in got.windows],
                         [("5h", 64.0), ("weekly", 0.0)])
        self.assertEqual((got.provider, got.remaining, got.unit),
                         ("kimi", 0.0, "percent"))
        self.assertNotIn("limit", got.windows[0])
        self.assertTrue(got.resets_at.endswith("Z"))
        self.assertEqual(got.raw["membership"], "pro")

    def test_a_weekly_in_limits_is_not_duplicated_by_usage(self) -> None:
        got = runway.parse_kimi({
            "usage": {"limit": "10", "used": "1", "resetTime": _iso_in(40)},
            "limits": [{"window": {"duration": 7, "timeUnit": "TIME_UNIT_DAY"},
                        "detail": {"limit": "10", "remaining": "5",
                                   "resetTime": _iso_in(40)}}]})
        self.assertEqual([(w["label"], w["remaining"]) for w in got.windows],
                         [("weekly", 50.0)])

    def test_the_plan_quota_alone_is_still_a_reading(self) -> None:
        got = runway.parse_kimi({"usage": {"limit": "4", "used": "3",
                                           "resetTime": _iso_in(40)}})
        self.assertEqual([(w["label"], w["remaining"]) for w in got.windows],
                         [("weekly", 25.0)])

    def test_broken_shapes_are_unknown(self) -> None:
        cases = {
            "missing windows": {},
            "nonnumeric quota": {"limits": [{"detail": {
                "remaining": "lots", "limit": "1000"}}]},
            "invalid reset": {"limits": [{"detail": {
                "remaining": "1", "limit": "2", "resetTime": "never"}}]},
        }
        for label, data in cases.items():
            with self.subTest(label):
                got = runway.parse_kimi(data)
                self.assertFalse(got.known)
                self.assertTrue(got.reason)


class MiniMaxTests(unittest.TestCase):
    def test_both_token_plan_windows_are_reported(self) -> None:
        got = runway.parse_minimax(minimax_fixture())
        self.assertEqual([(w["label"], w["remaining"]) for w in got.windows],
                         [("5h", 64.0), ("weekly", 88.0)])
        self.assertEqual((got.kind, got.unit, got.remaining),
                         ("plan", "percent", 64.0))
        self.assertEqual(got.raw["model_name"], "general")  # not the video row

    def test_a_refusal_carried_in_base_resp_is_unknown(self) -> None:
        """MiniMax answers 200 and puts the rejection in the body."""
        got = runway.parse_minimax(minimax_fixture(status=1004))
        self.assertFalse(got.known)
        self.assertIn("1004", got.reason)

    def test_broken_shapes_are_unknown(self) -> None:
        cases = {
            "missing rows": {},
            "missing percentages": {
                "model_remains": [{"model_name": "general"}]},
            "nonnumeric percentage": {"model_remains": [{
                "current_interval_remaining_percent": "lots"}]},
        }
        for label, data in cases.items():
            with self.subTest(label):
                got = runway.parse_minimax(data)
                self.assertFalse(got.known)
                self.assertTrue(got.reason)


# --- Grok / xAI --------------------------------------------------------------
# Every byte below is BUILT HERE. Nothing in this file touches the owner's
# OpenCode credentials, and no test makes a live call.

def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte, value = value & 0x7F, value >> 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _bytes_field(number: int, payload: bytes) -> bytes:
    return _varint(number << 3 | 2) + _varint(len(payload)) + payload


def _proto_time(seconds: int) -> bytes:
    return _varint(1 << 3) + _varint(seconds)  # Timestamp.seconds


def _credit(token_id: str, expires: int) -> bytes:
    """One banked reset: field 10 is the REDEEMABLE ID, field 30 the expiry.
    The id is in the fixture precisely so a test can prove it never comes back
    out."""
    return _bytes_field(10, token_id.encode()) + \
        _bytes_field(30, _proto_time(expires))


def _frame(payload: bytes, flags: int = 0) -> bytes:
    return bytes([flags]) + len(payload).to_bytes(4, "big") + payload


def _resets_body(*credits: bytes, status: bytes = b"grpc-status:0\r\n") -> bytes:
    return _frame(b"".join(_bytes_field(10, c) for c in credits)) + \
        _frame(status, 0x80)


def _billing(used=None, start=_iso_in(-24), end=_iso_in(144)) -> dict:
    config = {"currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY",
                                "start": start, "end": end}}
    if used is not None:  # proto3 JSON omits a zero, so absent means zero
        config["creditUsagePercent"] = used
    return {"config": config}


class XaiBillingTests(unittest.TestCase):
    """The SuperGrok subscription window, from the endpoints the Grok CLI
    uses (verified live 2026-08-14; parsed here from fixtures)."""

    def test_the_subscription_window_reads_as_a_weekly_window(self) -> None:
        got = runway.parse_xai(_billing(used=30.0))
        self.assertEqual([(w["label"], w["remaining"]) for w in got.windows],
                         [("weekly", 70.0)])
        self.assertEqual((got.provider, got.kind, got.unit),
                         ("xai", "plan", "percent"))
        self.assertTrue(got.resets_at.endswith("Z"))
        self.assertEqual(got.raw["period"], "USAGE_PERIOD_TYPE_WEEKLY")

    def test_an_omitted_percentage_is_proto3_for_nothing_spent(self) -> None:
        self.assertEqual(runway.parse_xai(_billing()).remaining, 100.0)

    def test_broken_shapes_are_unknown(self) -> None:
        cases = {
            "missing period": {},
            "period is not an object": {"config": {"currentPeriod": "nope"}},
            "invalid end": {"config": {"currentPeriod": {"end": "never"}}},
        }
        for label, data in cases.items():
            with self.subTest(label):
                got = runway.parse_xai(data)
                self.assertFalse(got.known)
                self.assertTrue(got.reason)

    def test_reset_credits_are_optional_window_metadata(self) -> None:
        cases = {
            "credits present": (
                {"available": 2, "soonest_expiry": "2026-09-12T18:49:00Z"},
                "2 banked reset credits · soonest expires 2026-09-12"),
            "reset rpc unavailable": (None, None),
        }
        for label, (resets, expected) in cases.items():
            with self.subTest(label):
                got = runway.parse_xai(_billing(used=10.0), resets)
                self.assertEqual(got.remaining, 90.0)
                self.assertEqual(runway.credits_text(got.raw), expected)


class XaiResetTests(unittest.TestCase):
    """The gRPC-Web reset rpc: framing and protobuf, both hand-rolled."""

    def test_unexpired_credits_are_counted_and_their_ids_never_returned(self) -> None:
        body = _resets_body(_credit("restok_live", int(time.time()) + 86400),
                            _credit("restok_also", int(time.time()) + 172800),
                            _credit("restok_gone", int(time.time()) - 60))
        got = runway.parse_xai_resets(body)
        self.assertEqual(got["available"], 2)  # the lapsed one is unspendable
        self.assertNotIn("restok", json.dumps(got))
        self.assertTrue(got["soonest_expiry"].endswith("Z"))

    def test_zero_credits_is_an_empty_message_not_an_error(self) -> None:
        self.assertEqual(runway.parse_xai_resets(_resets_body()),
                         {"available": 0, "soonest_expiry": None})

    def test_grpc_errors_are_raised_not_read_as_zero(self) -> None:
        cases = {
            "trailer": (
                _frame(b"grpc-status:16\r\ngrpc-message:no-credentials\r\n", 0x80),
                {}, "16"),
            "header": (_resets_body(status=b""), {"grpc-status": "7"}, "7"),
        }
        for label, (body, headers, status) in cases.items():
            with self.subTest(label), self.assertRaises(ValueError) as caught:
                runway.parse_xai_resets(body, headers)
            self.assertIn(f"grpc status {status}", str(caught.exception))
            self.assertNotIn("no-credentials", str(caught.exception))

    def test_malformed_frames_raise_rather_than_inventing_a_count(self) -> None:
        cases = {
            "no frame": b"",
            "truncated frame": b"\x00\x00\x00\x00\x40short",
            "truncated protobuf": _frame(b"\x0a\x7f"),
            "compressed frame": _frame(b"\x0a\x00", 0x01),
        }
        for label, body in cases.items():
            with self.subTest(label), self.assertRaises(ValueError):
                runway.parse_xai_resets(body)


class XaiAdapterTests(unittest.TestCase):
    """The three calls wired together, every failure landing on
    unknown-with-reason rather than an exception."""

    def _replies(self, *calls):
        return mock.patch.object(runway, "_fetch", side_effect=list(calls))

    OK_USER = ({}, b'{"userId":"u-1"}', None)

    def test_the_happy_path_is_a_window_plus_a_credits_phrase(self) -> None:
        with mock.patch.object(runway, "_xai_bearer",
                               return_value=("token", None)), self._replies(
                self.OK_USER,
                ({}, json.dumps(_billing(used=25.0)).encode(), None),
                ({}, _resets_body(_credit("restok", int(time.time()) + 86400)),
                 None)):
            got = runway.xai(auth_path=NO_AUTH)
        self.assertEqual(got.remaining, 75.0)
        self.assertEqual(runway.credits_text(got.raw).split(" ·")[0],
                         "1 banked reset credit")
        self.assertNotIn("restok", json.dumps(got.as_dict()))
        self.assertNotIn("token", json.dumps(got.as_dict()))

    def test_a_dead_reset_rpc_still_leaves_the_window(self) -> None:
        with mock.patch.object(runway, "_xai_bearer",
                               return_value=("token", None)), self._replies(
                self.OK_USER,
                ({}, json.dumps(_billing(used=25.0)).encode(), None),
                ({}, b"", "http 503 Service Unavailable")):
            got = runway.xai(auth_path=NO_AUTH)
        self.assertEqual(got.remaining, 75.0)
        self.assertIsNone(runway.credits_text(got.raw))

    def test_every_call_failing_is_unknown_with_a_reason(self) -> None:
        cases = {
            "user request": ("http 401 Unauthorized",
                             [({}, b"", "http 401 Unauthorized")]),
            "unrecognized user": ("did not recognise",
                                  [({}, b'{"error":"nope"}', None)]),
            "billing response": ("not JSON",
                                 [self.OK_USER, ({}, b"<html>", None)]),
        }
        for label, (expected, calls) in cases.items():
            with self.subTest(label), \
                    mock.patch.object(runway, "_xai_bearer",
                                      return_value=("token", None)), \
                    self._replies(*calls):
                got = runway.xai(auth_path=NO_AUTH)
                self.assertFalse(got.known)
                self.assertIn(expected, got.reason)

    def test_an_unusable_credential_never_reaches_the_network(self) -> None:
        with mock.patch.object(runway, "_xai_bearer",
                               return_value=(None, "no xai login")), \
                mock.patch.object(runway, "_fetch", side_effect=AssertionError):
            got = runway.xai(auth_path=NO_AUTH)
        self.assertEqual(got.reason, "no xai login")


class ClaudeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        # Removing the cache-freshness shortcut means claude() always considers
        # a screen read. These cases are about the cache FILE, so refuse the
        # screen rather than spawn Claude Code on the developer's machine.
        self._no_screen = mock.patch.object(runway, "read_claude_screen",
                                            return_value={})
        self._no_screen.start()
        self.addCleanup(self._no_screen.stop)
        runway._CLAUDE_LAST = None
        self.addCleanup(setattr, runway, "_CLAUDE_LAST", None)

        self.path = Path(self.tmp.name) / ".claude.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write(self, data) -> Path:
        self.path.write_text(json.dumps(data))
        return self.path

    def test_both_windows_are_reported_under_one_provider(self) -> None:
        """W-0179/W-0182: the 5-hour and weekly limits are two windows of ONE
        Claude plan, and neither carries a limit field."""
        got = runway.claude(self._write(claude_fixture(five_hour_used=12,
                                                       seven_day_used=40)))
        self.assertEqual(got.provider, "claude")
        self.assertEqual([(w["label"], w["remaining"]) for w in got.windows],
                         [("5h", 88.0), ("weekly", 60.0)])
        for w in got.windows:
            self.assertNotIn("limit", w)
            self.assertEqual(w["unit"], "percent")
        # the scalar fields stay the tightest live window, for dispatch
        self.assertEqual((got.remaining, got.unit), (60.0, "percent"))
        self.assertEqual(got.kind, "plan")
        self.assertFalse(got.stale)
        self.assertTrue(got.as_of.endswith("Z"))

    def test_an_expired_window_is_flagged_not_dropped(self) -> None:
        data = claude_fixture()
        data["cachedUsageUtilization"]["utilization"]["five_hour"]["resets_at"] = \
            _iso_in(-1)
        got = runway.claude(self._write(data))
        five, week = got.windows
        self.assertTrue(five["stale"])
        # The flag stays for the conductor; the caption is gone — the daemon
        # keeps readings current, so an old number is a fault, not a caption.
        self.assertIsNone(five["stale_reason"])
        self.assertFalse(week["stale"])
        self.assertEqual(got.remaining, 60.0)  # the tightest LIVE window

    def test_an_old_cache_is_reported_without_narrating_its_age(self) -> None:
        """W-0182: the age of a reading stops being a story on the card. Only
        a window that HAS ALREADY RESET changes what its number means, and
        this fixture's windows both still stand."""
        got = runway.claude(self._write(claude_fixture(age_min=60 * 27)))
        self.assertTrue(got.known)
        self.assertFalse(got.stale)
        self.assertEqual(len(got.windows), 2)
        self.assertIsNone(got.windows[1]["stale_reason"])
        # the age itself survives on the record, for the conductor
        self.assertEqual(got.raw["as_of_age_h"], 27.0)
        self.assertEqual(round(runway.age_hours(got.as_of)), 27)

    def test_shape_drift_and_bad_files_are_unknown(self) -> None:
        cases = {
            "missing cache": {"other": 1},
            "missing timestamp": {"cachedUsageUtilization": {"utilization": {}}},
            "no usable windows": {"cachedUsageUtilization": {
                "fetchedAtMs": time.time() * 1000,
                "utilization": {"five_hour": None, "seven_day": None}}},
        }
        for label, data in cases.items():
            with self.subTest(label):
                got = runway.claude(self._write(data))
                self.assertFalse(got.known)
                self.assertTrue(got.reason)
        self.path.write_text("{not json")
        self.assertFalse(runway.claude(self.path).known)
        self.assertFalse(runway.claude(self.path.parent / "absent.json").known)


def _no_app_server():
    """Stand in for a machine with no codex-cli, so a test that is about the
    session files does not reach the real app server."""
    raise RuntimeError("codex is not installed")


class CodexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "2026" / "08" / "13"
        self.dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _rollout(self, name: str, lines) -> Path:
        p = self.dir / name
        p.write_text("\n".join(lines) + "\n")
        return p

    def test_last_token_count_event_wins(self) -> None:
        got = runway.parse_codex(codex_lines(used=25.0))
        self.assertEqual((got.provider, got.remaining, got.unit),
                         ("codex", 75.0, "percent"))
        # window_minutes 10080 reads "weekly" rather than going unnamed
        self.assertEqual([w["label"] for w in got.windows], ["weekly"])
        self.assertEqual(got.kind, "plan")
        self.assertEqual(got.raw["plan_type"], "prolite")
        self.assertEqual(got.as_of, "2026-08-13T10:05:00Z")

    def test_zero_banked_resets_is_reported_not_hidden(self) -> None:
        """W-0184: ``credits`` on a Codex plan is banked RESETS, not money, so
        a plan provider may show it — and zero is the answer the owner wants."""
        got = runway.parse_codex(codex_lines())
        self.assertEqual(runway.credits_text(got.raw), "0 banked resets")
        self.assertEqual(got.kind, "plan")  # still never shows a price

    def test_a_second_window_renders_when_the_account_has_one(self) -> None:
        got = runway.parse_codex(codex_lines(
            secondary={"used_percent": 10.0, "window_minutes": 300,
                       "resets_at": _epoch_in(1)}))
        self.assertEqual([(w["label"], w["remaining"]) for w in got.windows],
                         [("weekly", 75.0), ("5h", 90.0)])

    def test_a_reset_window_is_flagged_not_withheld(self) -> None:
        got = runway.parse_codex(codex_lines(resets_in_h=-5))
        self.assertTrue(got.known)
        self.assertEqual(got.remaining, 75.0)
        self.assertTrue(got.stale)
        self.assertIsNone(got.windows[0]["stale_reason"])

    def test_newest_file_wins_and_empty_sessions_are_unknown(self) -> None:
        old = self._rollout("rollout-2026-08-13T09-00-00-aaa.jsonl",
                            codex_lines(used=90.0))
        new = self._rollout("rollout-2026-08-13T10-00-00-bbb.jsonl",
                            codex_lines(used=25.0))
        import os
        os.utime(old, (1_780_000_000, 1_780_000_000))
        os.utime(new, (1_790_000_000, 1_790_000_000))
        # A reader that refuses forces the session-file path, which is what
        # these cases are about. Without it the test reaches the real codex
        # binary on the developer's machine and waits out its timeout.
        self.assertEqual(
            runway.codex(self.dir.parents[2], reader=_no_app_server).remaining, 75.0)
        got = runway.codex(Path(self.tmp.name) / "nothing-here",
                           reader=_no_app_server)
        self.assertFalse(got.known)
        self.assertIn("no usable session snapshot", got.reason)

    def test_falls_back_past_a_session_with_no_snapshot_yet(self) -> None:
        started = self._rollout("rollout-2026-08-13T11-00-00-ccc.jsonl",
                                ['{"timestamp":"x","payload":{"type":"user_message"}}'])
        older = self._rollout("rollout-2026-08-13T10-00-00-ddd.jsonl",
                              codex_lines(used=25.0))
        import os
        os.utime(older, (1_780_000_000, 1_780_000_000))
        os.utime(started, (1_790_000_000, 1_790_000_000))
        self.assertEqual(runway.codex(self.dir.parents[2], reader=_no_app_server).remaining, 75.0)

    def test_corrupt_lines_and_drifted_shapes_are_unknown(self) -> None:
        cases = {
            "empty": [],
            "truncated json": ['{"payload":{"type":"token_count", "rate_l'],
            "no usable window": ['{"payload":{"type":"token_count",'
                                 '"rate_limits":{"primary":null}}}'],
            "invalid reset": ['{"payload":{"type":"token_count","rate_limits":'
                              '{"primary":{"used_percent":10,'
                              '"resets_at":"soon"}}}}'],
        }
        for label, lines in cases.items():
            with self.subTest(label):
                got = runway.parse_codex(lines)
                self.assertFalse(got.known)
                self.assertTrue(got.reason)


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        # central state (DESIGN §2): the database path comes from ORCHESTRA_HOME
        self._home = os.environ.get("ORCHESTRA_HOME")
        os.environ["ORCHESTRA_HOME"] = self.tmp.name
        self.con = db.connect()

    def tearDown(self) -> None:
        self.con.close()
        if self._home is None:
            os.environ.pop("ORCHESTRA_HOME", None)
        else:
            os.environ["ORCHESTRA_HOME"] = self._home
        self.tmp.cleanup()

    def test_polls_store_known_unknown_raw_and_window_data(self) -> None:
        runway.record(self.con, [
            runway.parse_deepseek(DEEPSEEK_OK),
            runway.parse_claude(claude_fixture()),
            runway.unknown("codex", "no rollout files"),
        ])
        rows = list(self.con.execute(
            "SELECT * FROM runway_polls ORDER BY provider"))
        by_provider = {row["provider"]: row for row in rows}
        self.assertEqual(set(by_provider), {"claude", "codex", "deepseek"})
        self.assertIsNone(by_provider["codex"]["remaining"])
        self.assertEqual(by_provider["codex"]["reason"], "no rollout files")
        self.assertEqual((by_provider["deepseek"]["remaining"],
                          by_provider["deepseek"]["unit"]), (10.5, "USD"))
        self.assertEqual(json.loads(by_provider["deepseek"]["raw"])
                         ["granted_balance"], "0.50")
        self.assertEqual([w["label"] for w in json.loads(
            by_provider["claude"]["windows"])],
                         ["5h", "weekly"])
        self.assertTrue(all(row["polled_at"] for row in rows))

    def test_a_trend_query_can_read_polls_back_in_order(self) -> None:
        for pct in (80.0, 60.0):
            runway.record(self.con, [runway.Runway("claude", remaining=pct,
                                                   unit="percent")])
        got = [r["remaining"] for r in self.con.execute(
            "SELECT remaining FROM runway_polls WHERE provider='claude' "
            "ORDER BY id")]
        self.assertEqual(got, [80.0, 60.0])


class FormattingTests(unittest.TestCase):
    def test_unknown_line_carries_the_reason(self) -> None:
        line, = runway.format_lines(runway.unknown("kimi", "http 401 Unauthorized"))
        self.assertIn("unknown", line)
        self.assertIn("http 401 Unauthorized", line)

    def test_one_cli_line_per_window(self) -> None:
        lines = runway.format_lines(runway.parse_claude(
            claude_fixture(five_hour_used=12, seven_day_used=40)))
        self.assertEqual(len(lines), 2)
        self.assertIn("5h", lines[0])
        self.assertIn("88% left", lines[0])
        self.assertIn("weekly", lines[1])
        self.assertIn("60% left", lines[1])

    def test_window_minutes_are_named(self) -> None:
        cases = {
            "weekly": (10080, "weekly"),
            "five hours": (300, "5h"),
            "arbitrary minutes": (42, "42m"),
            "missing duration": (None, "window"),
            "whole hours": (240, "4h"),
            "whole days": (4320, "3d"),
            "zero": (0, "0m"),
        }
        for label, (minutes, expected) in cases.items():
            with self.subTest(label):
                self.assertEqual(runway.window_label(minutes), expected)

    def test_time_to_reset_reads_in_days_hours_and_minutes(self) -> None:
        """W-0182: "in 55h" is arithmetic the reader has to do."""
        cases = {
            "hours": (_iso_in(2), "in 1h 5"),
            "days": (_iso_in(80), "in 3d 7h"),
            "minutes": (_iso_in(0.5), "in 29m"),
            "missing": (None, "-"),
            "past": (_iso_in(-1), "now"),
        }
        for label, (resets_at, expected) in cases.items():
            with self.subTest(label):
                self.assertTrue(runway.until_text(resets_at).startswith(expected))

    def test_a_balance_prints_as_money_and_a_window_as_headroom(self) -> None:
        line, = runway.format_lines(runway.parse_deepseek(DEEPSEEK_OK))
        self.assertIn("balance", line)
        self.assertIn("10.5 USD", line)


class ExpiredWindowTests(unittest.TestCase):
    """A window whose reset has passed describes a window that no longer
    exists. Claude's five-hour window read 88% for two days after it reset,
    on both the dashboard and the phone, because the reading was merely
    flagged stale and every surface drew the number anyway."""

    def _window(self, resets_at):
        return runway.make_window("5h", 88.0, resets_at)

    def test_an_expired_window_reports_unknown_not_the_old_number(self) -> None:
        past = "2020-01-01T00:00:00Z"
        current = runway.as_of_now(self._window(past))
        self.assertIsNone(current["remaining"])
        self.assertTrue(current["stale"])
        self.assertIn("reset", current["stale_reason"])
        self.assertEqual("5h", current["label"])

    def test_current_windows_keep_their_reading(self) -> None:
        for label, resets_at in {
                "future reset": "2999-01-01T00:00:00Z",
                "unknown reset": None}.items():
            with self.subTest(label):
                window = self._window(resets_at)
                self.assertEqual(window, runway.as_of_now(window))
                self.assertEqual(88.0, runway.as_of_now(window)["remaining"])


class SkipTests(unittest.TestCase):
    """A lapsed subscription refuses forever, and an adapter cannot tell that
    apart from an outage -- so it showed an orange 'Not reported' that read as
    a fault every five minutes."""

    def _fake_adapters(self):
        """Stand-ins for the real six. poll_all must never reach a network or
        spawn a subprocess in a test -- two adapters shell out now, and one of
        them opens a pseudo-terminal."""
        def make(name):
            def adapter():
                return runway.from_windows(
                    name, [runway.make_window("weekly", 50.0, None)])
            adapter.__name__ = name
            return adapter
        return tuple(make(n) for n in ("claude", "codex", "minimax"))

    def test_a_skipped_provider_is_reported_not_hidden(self) -> None:
        with mock.patch.object(runway, "ADAPTERS", self._fake_adapters()):
            results = runway.poll_all({"runway": {"skip": ["minimax"]}})
        by_name = {r.provider: r for r in results}
        self.assertIn("minimax", by_name, "a skipped provider still gets a row")
        self.assertFalse(by_name["minimax"].known)
        self.assertIn("no plan configured", by_name["minimax"].reason)
        self.assertTrue(by_name["claude"].known, "the rest still poll")

    def test_a_skipped_provider_is_not_polled(self) -> None:
        called = []

        def spy():
            called.append("minimax")
            return runway.unknown("minimax", "should not run")

        spy.__name__ = "minimax"
        with mock.patch.object(runway, "ADAPTERS", (spy,)):
            runway.poll_all({"runway": {"skip": ["minimax"]}})
            self.assertEqual([], called)
            runway.poll_all({})
            self.assertEqual(["minimax"], called)

    def test_the_skip_list_is_case_and_space_tolerant(self) -> None:
        self.assertEqual({"minimax"}, runway.skipped({"runway": {"skip": [" MiniMax "]}}))
        self.assertEqual(set(), runway.skipped({}))
        self.assertEqual(set(), runway.skipped(None))


class CodexLiveTests(unittest.TestCase):
    """Codex headroom was read from the newest session file, which is a
    RECORD and not a reading: it showed 100% while the account was at 11%,
    because the last token_count event was 23 hours old. The app server
    answers for now."""

    LIVE = {"rateLimits": {"limitId": "codex", "planType": "prolite",
                           "primary": {"usedPercent": 89,
                                       "windowDurationMins": 10080,
                                       "resetsAt": 1787196960},
                           "secondary": None,
                           "credits": {"hasCredits": False, "balance": "0"}}}

    def test_used_percent_becomes_remaining(self) -> None:
        r = runway.parse_codex_live(self.LIVE)
        self.assertTrue(r.known)
        self.assertEqual(11.0, r.remaining)
        self.assertEqual(["weekly"], [w["label"] for w in r.windows])
        self.assertEqual("0 banked resets", runway.credits_text(r.raw))

    def test_a_dead_app_server_falls_back_to_the_session_record(self) -> None:
        def boom():
            raise RuntimeError("codex is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp) / "2026" / "08"
            d.mkdir(parents=True)
            (d / "rollout-x.jsonl").write_text(json.dumps({
                "timestamp": "2026-08-15T00:00:00Z",
                "payload": {"type": "token_count", "rate_limits": {
                    "primary": {"used_percent": 20, "window_minutes": 10080,
                                "resets_in_seconds": 3600}}}}) + "\n")
            r = runway.codex(sessions_dir=tmp, reader=boom)
        self.assertTrue(r.known, r.reason)
        self.assertEqual(80.0, r.remaining)

    def test_no_app_server_and_no_record_says_both(self) -> None:
        def boom():
            raise RuntimeError("codex is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            r = runway.codex(sessions_dir=tmp, reader=boom)
        self.assertFalse(r.known)
        self.assertIn("codex is not installed", r.reason)
        self.assertIn("no usable session snapshot", r.reason)


SCREEN = """\
 Current session
   2% used · resets in 2h
 Current week (all models)
   53% used · resets Tue
 Account
   99% something irrelevant
"""


class ClaudeLiveTests(unittest.TestCase):
    """cachedUsageUtilization is written when Claude Code feels like it. The
    owner's sat 86 hours old reading 83% weekly while /usage said 47%, which
    is the number that decides whether to dispatch."""

    STATE = {"cachedUsageUtilization": {
        "fetchedAtMs": 1_786_000_000_000,
        "utilization": {
            "five_hour": {"utilization": 12,
                          "resets_at": "2020-01-01T00:00:00Z"},
            "seven_day": {"utilization": 17,
                          "resets_at": "2999-01-01T00:00:00Z"}}}}

    def setUp(self) -> None:
        runway._CLAUDE_LAST = None
        self.addCleanup(setattr, runway, "_CLAUDE_LAST", None)

    def _file(self, tmp, state=None):
        path = pathlib.Path(tmp) / "claude.json"
        path.write_text(json.dumps(state if state is not None else self.STATE))
        return path

    def test_the_screen_beats_a_stale_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            r = runway.claude(self._file(tmp), now_ms=1_786_000_000_000 + 86 * 3600000,
                              screen=lambda state: runway.parse_claude_screen(SCREEN))
        by = {w["label"]: w for w in r.windows}
        self.assertEqual(47.0, by["weekly"]["remaining"])   # not the cache's 83
        self.assertEqual(98.0, by["5h"]["remaining"])       # not the cache's 88
        # The screen gives percentages, not reset times. Pairing a live number
        # with a reset that has passed would make as_of_now blank a reading
        # just taken.
        self.assertIsNone(by["5h"]["resets_at"])
        self.assertEqual(98.0, runway.as_of_now(by["5h"])["remaining"])
        self.assertEqual("2999-01-01T00:00:00Z", by["weekly"]["resets_at"])

    def test_a_fresh_cache_does_not_skip_the_screen(self) -> None:
        # It used to. The per-model rows live only on the screen and are never
        # written to the cache, and reading /usage refreshes the cache as a
        # side effect -- so a fresh cache hid the very thing that refreshed it,
        # and Fable never appeared again.
        called = []
        with tempfile.TemporaryDirectory() as tmp:
            r = runway.claude(self._file(tmp), now_ms=1_786_000_000_000 + 60_000,
                              screen=lambda state: called.append(1) or
                              {"five_hour": 5.0, "seven_day": 61.0, "week:fable": 53.0})
        self.assertEqual([1], called)
        by = {w["label"]: w for w in r.windows}
        self.assertEqual(39.0, by["weekly"]["remaining"])
        self.assertIn("weekly \u00b7 fable", by)

    def test_a_screen_that_says_nothing_keeps_the_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            r = runway.claude(self._file(tmp), now_ms=1_786_000_000_000 + 86 * 3600000,
                              screen=lambda state: {})
        self.assertTrue(r.known)
        self.assertEqual(83.0, {w["label"]: w for w in r.windows}["weekly"]["remaining"])

    def test_the_screen_read_is_throttled_between_polls(self) -> None:
        # The daemon polls every five minutes and the cache is stale most of
        # the time. Spawning Claude Code that often rate-limits the owner's own
        # /usage view -- the per-model breakdown goes first.
        calls = []

        def once(state):
            calls.append(1)
            return {"five_hour": 5.0, "seven_day": 61.0}

        with tempfile.TemporaryDirectory() as tmp:
            path = self._file(tmp)
            stale = 1_786_000_000_000 + 86 * 3600000
            with mock.patch.object(runway, "read_claude_screen", once):
                first = runway.claude(path, now_ms=stale)
                second = runway.claude(path, now_ms=stale)
        self.assertEqual(1, len(calls), "the second poll reused the first answer")
        self.assertEqual(first.remaining, second.remaining)

    def test_a_partial_read_is_retried_sooner_than_a_complete_one(self) -> None:
        # A read missing the per-model rows was rate limited, not finished.
        # Caching it for the full interval locked Fable out for twenty minutes.
        answers = [{"five_hour": 5.0, "seven_day": 61.0},
                   {"five_hour": 5.0, "seven_day": 61.0, "week:fable": 53.0}]
        calls = []

        def reader(state):
            calls.append(1)
            return answers[min(len(calls) - 1, len(answers) - 1)]

        with tempfile.TemporaryDirectory() as tmp:
            path = self._file(tmp)
            stale = 1_786_000_000_000 + 86 * 3600000
            with mock.patch.object(runway, "read_claude_screen", reader):
                runway.claude(path, now_ms=stale)              # partial
                self.assertEqual(1, len(calls))
                # A partial answer is held only briefly, so the next poll asks.
                runway._CLAUDE_LAST = (runway.time.monotonic()
                                       - runway.CLAUDE_PARTIAL_EVERY_S - 1,
                                       runway._CLAUDE_LAST[1])
                second = runway.claude(path, now_ms=stale)
                self.assertEqual(2, len(calls))
                # Now it is complete, so the long interval applies.
                runway._CLAUDE_LAST = (runway.time.monotonic()
                                       - runway.CLAUDE_PARTIAL_EVERY_S - 1,
                                       runway._CLAUDE_LAST[1])
                runway.claude(path, now_ms=stale)
                self.assertEqual(2, len(calls), "a complete read is not re-asked")
        self.assertIn("weekly \u00b7 fable", [w["label"] for w in second.windows])

    def test_a_per_model_week_is_reported_but_never_the_scalar(self) -> None:
        # Anthropic meters some models separately. Fable's weekly running out
        # says nothing about Opus, so it must not become "how much Claude is
        # left" -- which is the number dispatch reads.
        screen = ("Current session\n 5% used\n"
                  "Current week (all models)\n 61% used\n"
                  "Current week (Fable only)\n 23% used\n")
        with tempfile.TemporaryDirectory() as tmp:
            r = runway.claude(self._file(tmp), now_ms=1_786_000_000_000 + 86 * 3600000,
                              screen=lambda state: runway.parse_claude_screen(screen))
        by = {w["label"]: w for w in r.windows}
        self.assertEqual(77.0, by["weekly \u00b7 fable"]["remaining"])
        self.assertTrue(by["weekly \u00b7 fable"]["per_model"])
        self.assertEqual(39.0, r.remaining)  # the account week, not Fable's

    def test_screen_parser_recognizes_quota_rows(self) -> None:
        cases = {
            "account rows only": (
                SCREEN, {"five_hour": 2.0, "seven_day": 53.0}),
            "new per-model row": (
                "Current week (Something New only)\n 10% used\n",
                {"week:something new": 10.0}),
            "all models is account-wide": (
                "Current week (all models)\n 61% used\n", {"seven_day": 61.0}),
            "terminal escapes": (
                "\x1b[1m Current session\x1b[0m\r\n"
                "\x1b[32m   2% used\x1b[0m\r\n", {"five_hour": 2.0}),
        }
        for label, (screen, expected) in cases.items():
            with self.subTest(label):
                self.assertEqual(expected, runway.parse_claude_screen(screen))


class ExhaustionTests(unittest.TestCase):
    """Per-profile burn (W-0249): a live zero is a wall; unknown and stale are not."""

    def test_a_live_zero_is_exhausted(self) -> None:
        reason = runway.exhaustion(
            {"remaining": 0, "unit": "percent", "resets_at": None, "as_of": None})
        self.assertIsNotNone(reason)
        self.assertIn("0% left", reason)

    def test_non_walls_are_available(self) -> None:
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        cases = {
            "no poll": None,
            "unknown": {"remaining": None, "reason": "down"},
            "headroom": {"remaining": 12, "unit": "percent"},
            "stale zero": {"remaining": 0, "unit": "percent", "as_of": old},
            "expired zero": {"remaining": 0, "unit": "percent",
                             "resets_at": past},
        }
        for label, entry in cases.items():
            with self.subTest(label):
                self.assertIsNone(runway.exhaustion(entry))

    def test_profile_burns_records_the_provider_wall(self) -> None:
        burns = runway.profile_burns(
            {"big": {"backend": "opencode", "model": "anthropic/opus"},
             "stub": {"backend": "opencode"}},
            {"anthropic": {"remaining": 0.0, "unit": "percent"}})
        self.assertEqual(list(burns), ["big"])
        self.assertIn("0% left", burns["big"])


if __name__ == "__main__":
    unittest.main()
