"""The HTTP surface (DESIGN §3).

Nothing here binds a public port or reads the developer's real state: every
server binds 127.0.0.1 on port 0 and DROMOND_HOME/DROMOND_CONFIG point at a
throwaway directory. No daemon is started.
"""
import gzip
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from pathlib import Path
from unittest import mock

from dromond import auth, config, db, runway
from dromond import http as mhttp

KEY = "test-secret-value"
PROJECT_ID = "53efe3c3-6def-4797-8560-3dce073d7d63"


class ServerCase(unittest.TestCase):
    """One server on 127.0.0.1:0, torn down after each test."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name).resolve()
        self.config_path = self.tmp_path / "config.toml"
        self.config_path.write_text('[profiles.probe]\nbackend = "codex"\n')
        self.env = mock.patch.dict(os.environ, {
            "DROMOND_HOME": str(self.tmp_path / "home"),
            "DROMOND_CONFIG": str(self.config_path)})
        self.env.start()
        os.environ.pop(mhttp.KEY_ENV, None)  # a developer's shell must not leak in
        self.con = db.connect()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.srv = self._serve()

    def _serve(self):
        self.restart = getattr(self, "restart", None) or threading.Event()
        srv = mhttp.serve(self.stop, wake=self.wake, addr="127.0.0.1", port=0,
                          cfg={"http": {"key": KEY}}, restart=self.restart)
        self.assertIsNotNone(srv, "the server did not start")
        return srv

    def tearDown(self) -> None:
        self.stop.set()
        self.srv.shutdown()
        self.srv.server_close()
        self.con.close()
        self.env.stop()
        self.tmp.cleanup()

    # -- helpers
    def request(self, method="GET", path="/api/snapshot", key=KEY, host=None,
                body=None, cookie=None, srv=None):
        srv = srv or self.srv
        headers = {}
        if key is not None:
            headers[mhttp.HEADER] = key
        if cookie is not None:
            headers["Cookie"] = f"{mhttp.COOKIE}={cookie}"
        if host is not None:
            headers["Host"] = host
        payload = json.dumps(body).encode() if body is not None else None
        if payload:
            headers["Content-Type"] = "application/json"
        conn = HTTPConnection("127.0.0.1", srv.server_port, timeout=10)
        try:
            conn.request(method, path, body=payload, headers=headers)
            res = conn.getresponse()
            return res.status, res.read().decode()
        finally:
            conn.close()

    def json_request(self, **kw):
        status, text = self.request(**kw)
        return status, json.loads(text) if text.strip() else {}

    def raw_request(self, encoding, path="/api/snapshot"):
        """(status, body BYTES, Content-Encoding) — undecoded, so a test can
        see the wire form rather than what a client would unwrap."""
        conn = HTTPConnection("127.0.0.1", self.srv.server_port, timeout=10)
        try:
            conn.request("GET", path, headers={mhttp.HEADER: KEY,
                                               "Accept-Encoding": encoding})
            res = conn.getresponse()
            return res.status, res.read(), res.getheader("Content-Encoding")
        finally:
            conn.close()

    def sse(self, path, key=KEY, last_event_id=None, until=None, max_lines=40):
        """Open an SSE route and read frames off it.

        A live run's stream never ends on its own, so ``until`` stops reading
        at the first line containing it and the socket is closed under the
        server — which is exactly what a closed browser tab does. Without
        ``until`` the whole body is read, which only terminates for a stream
        that ends itself (a terminal run).
        """
        headers = {}
        if key is not None:
            headers[mhttp.HEADER] = key
        if last_event_id is not None:
            headers["Last-Event-ID"] = last_event_id
        conn = HTTPConnection("127.0.0.1", self.srv.server_port, timeout=10)
        try:
            conn.request("GET", path, headers=headers)
            res = conn.getresponse()
            ctype = res.getheader("Content-Type") or ""
            if until is None or res.status != 200:
                return res.status, ctype, res.read().decode()
            lines = []
            for _ in range(max_lines):
                line = res.readline().decode()
                if not line:
                    break
                lines.append(line)
                if until in line:
                    break
            return res.status, ctype, "".join(lines)
        finally:
            conn.close()

    def make_run(self, status="running", **cols) -> int:
        fields = {"profile": "codex", "backend": "codex", "requested_by": "human",
                  "workdir": str(self.tmp_path), "status": status,
                  "project_id": PROJECT_ID, "started_at": db.now()}
        fields.update(cols)
        names = ", ".join(fields)
        marks = ", ".join("?" * len(fields))
        run_id = int(self.con.execute(
            f"INSERT INTO runs({names}) VALUES({marks})",
            tuple(fields.values())).lastrowid)
        self.con.commit()
        return run_id


class AuthTests(ServerCase):
    def test_missing_key_is_401_with_a_reason(self) -> None:
        status, text = self.request(key=None)
        self.assertEqual(status, 401)
        self.assertIn(mhttp.HEADER, text)
        self.assertEqual(len(text.strip().splitlines()), 1)

    def test_wrong_key_is_401_and_never_echoes_the_secret(self) -> None:
        status, text = self.request(key="not-the-key")
        self.assertEqual(status, 401)
        self.assertNotIn(KEY, text)
        self.assertNotIn("not-the-key", text)

    def test_the_dashboard_itself_needs_the_key(self) -> None:
        """Reads are not exempt: / is behind the same secret as /api."""
        self.assertEqual(self.request(path="/", key=None)[0], 401)
        status, text = self.request(path="/", key=KEY)
        self.assertEqual(status, 200)
        self.assertIn("<title>dromond</title>", text)

    def test_a_first_visit_may_carry_the_key_in_the_query(self) -> None:
        status, text = self.request(path="/?key=" + KEY, key=None)
        self.assertEqual(status, 200)
        self.assertIn("dromond", text)
        self.assertEqual(self.request(path="/?key=wrong", key=None)[0], 401)

    def test_a_cookie_reads_but_never_acts(self) -> None:
        """CSRF guard: POST demands the header, so another site's form on
        this browser cannot stop a run with an ambient cookie."""
        self.assertEqual(self.request(key=None, cookie=KEY)[0], 200)
        status, text = self.request(method="POST", path="/api/sweep",
                                    key=None, cookie=KEY)
        self.assertEqual(status, 401)
        self.assertIn(mhttp.HEADER, text)

    def test_a_valid_key_gets_the_snapshot(self) -> None:
        self.assertEqual(self.request()[0], 200)


class DroppedConnectionTests(unittest.TestCase):
    def _server(self):
        srv = mhttp.Server(("127.0.0.1", 0), mhttp.Handler)
        self.addCleanup(srv.server_close)
        return srv

    def test_a_reset_is_not_printed(self) -> None:
        with mock.patch.object(mhttp.ThreadingHTTPServer, "handle_error") as parent:
            with mock.patch("sys.exception", return_value=ConnectionResetError()):
                self._server().handle_error(None, ("100.1.2.3", 1))
        parent.assert_not_called()

    def test_a_real_fault_still_prints(self) -> None:
        with mock.patch.object(mhttp.ThreadingHTTPServer, "handle_error") as parent:
            with mock.patch("sys.exception", return_value=RuntimeError("boom")):
                self._server().handle_error(None, ("100.1.2.3", 1))
        parent.assert_called_once()


class HostCheckTests(ServerCase):
    def test_a_foreign_host_header_is_refused(self) -> None:
        status, text = self.request(host="evil.example")
        self.assertEqual(status, 403)
        self.assertIn("host", text.lower())

    def test_the_bound_address_and_localhost_are_accepted(self) -> None:
        port = self.srv.server_port
        for host in (f"127.0.0.1:{port}", f"localhost:{port}", "127.0.0.1"):
            with self.subTest(host=host):
                self.assertEqual(self.request(host=host)[0], 200)

    def test_a_configured_extra_host_is_accepted(self) -> None:
        hosts = mhttp.allowed_hosts("100.1.2.3",
                                    {"http": {"hosts": ["mac.tailnet.ts.net"]}})
        self.assertIn("mac.tailnet.ts.net", hosts)
        self.assertIn("100.1.2.3", hosts)
        self.assertNotIn("evil.example", hosts)

    def test_host_of_strips_the_port_and_keeps_ipv6_brackets(self) -> None:
        self.assertEqual(mhttp.host_of("Mac.local:3011"), "mac.local")
        self.assertEqual(mhttp.host_of("[::1]:3011"), "[::1]")
        self.assertEqual(mhttp.host_of(""), "")


class SnapshotTests(ServerCase):
    def test_the_snapshot_carries_an_integer_version(self) -> None:
        _, payload = self.json_request()
        self.assertIsInstance(payload["version"], int)
        self.assertEqual(payload["version"], mhttp.SNAPSHOT_VERSION)

    def test_the_snapshot_is_the_whole_control_plane(self) -> None:
        _, payload = self.json_request()
        self.assertEqual(
            set(payload),
            {"version", "generated_at", "home", "runs", "live_runs", "dispatch",
             "projects", "profiles", "runway", "statistics", "findings",
             "proposals", "daemon", "pinned_turns"})
        self.assertEqual(payload["findings"], [])   # DESIGN §9 tables absent
        self.assertEqual(payload["proposals"], [])
        self.assertEqual(payload["dispatch"], {"paused": False, "since": None})
        # There are no default profiles (DESIGN §5), so the snapshot lists
        # exactly what is configured — here, the one this case declares.
        self.assertEqual([p["name"] for p in payload["profiles"]], ["probe"])

    def test_the_run_window_holds_hundreds(self) -> None:
        """W-0187: limiting PROJECTS is the point, limiting runs is not — a
        board with a couple of hundred runs and their children on it has to
        show all of them."""
        self.assertGreaterEqual(mhttp.RECENT_RUNS, 500)

    def test_live_runs_are_never_squeezed_out_by_history(self) -> None:
        """Live runs come off their own unbounded query, so no amount of
        finished history can push a running run off the board."""
        with mock.patch.object(mhttp, "RECENT_RUNS", 2):
            live = [self.make_run() for _ in range(5)]
            for _ in range(20):
                self.make_run(status="done", finished_at=db.now())
            _, payload = self.json_request()
            shown = {r["id"] for r in payload["runs"]}
            self.assertTrue(set(live) <= shown)
            self.assertEqual(payload["live_runs"], 5)
            self.assertEqual(len(payload["runs"]), 5 + 2)

    def test_a_big_snapshot_is_gzipped_for_a_caller_that_asks(self) -> None:
        """The 4s poll is what makes the wider window affordable: the payload
        is repetitive JSON, so level-1 gzip pays for itself many times."""
        for _ in range(60):
            self.make_run(status="done", finished_at=db.now(),
                          summary="a summary line of the usual length. " * 12)
        pstatus, plain, penc = self.raw_request("identity")
        zstatus, zipped, zenc = self.raw_request("gzip")
        self.assertEqual((pstatus, zstatus), (200, 200))
        self.assertIsNone(penc)
        self.assertEqual(zenc, "gzip")
        self.assertLess(len(zipped), len(plain) / 2)
        # and it is the same payload, byte for byte, once unwrapped
        self.assertEqual(gzip.decompress(zipped), plain)

    def test_a_small_response_is_not_worth_compressing(self) -> None:
        """Below GZIP_MIN_BYTES the gzip header costs more than it saves."""
        status, body, encoding = self.raw_request("gzip", path="/api/runway")
        self.assertEqual(status, 200)
        self.assertLess(len(body), mhttp.GZIP_MIN_BYTES)
        self.assertIsNone(encoding)

    def test_runs_carry_status_profile_work_item_and_project(self) -> None:
        self.con.execute(
            "INSERT INTO projects(path, project_id, work_id, name, refreshed_at) "
            "VALUES(?,?,?,?,?)",
            (str(self.tmp_path), PROJECT_ID, "P-1", "demo", db.now()))
        live = self.make_run(work_item="W-0100", title="build the HTTP surface")
        done = self.make_run(status="done", finished_at=db.now())
        _, payload = self.json_request()
        by_id = {r["id"]: r for r in payload["runs"]}
        self.assertEqual(payload["live_runs"], 1)
        self.assertEqual(by_id[live]["status"], "running")
        self.assertEqual(by_id[live]["profile"], "codex")
        self.assertEqual(by_id[live]["work_item"], "W-0100")
        self.assertEqual(by_id[live]["project"], "P-1")
        self.assertTrue(by_id[live]["live"])
        self.assertFalse(by_id[done]["live"])
        self.assertEqual(payload["statistics"]["runs_total"], 2)
        self.assertEqual(payload["statistics"]["runs_active"], 1)
        self.assertEqual(
            [p["profile"] for p in payload["statistics"]["by_profile"]], ["codex"])

    def test_runway_reports_one_entry_per_provider_with_its_windows(self) -> None:
        """W-0182: Claude's 5-hour and weekly limits are two windows of ONE
        provider entry, and no window carries a limit or a reading age."""
        windows = json.dumps([
            {"label": "5h", "remaining": 88.0, "unit": "percent",
             "resets_at": None, "stale": False, "stale_reason": None},
            {"label": "weekly", "remaining": 40.0, "unit": "percent",
             "resets_at": None, "stale": False, "stale_reason": None}])
        for remaining in (90.0, 40.0):
            self.con.execute(
                "INSERT INTO runway_polls(provider, remaining, unit, windows, "
                "polled_at) VALUES('claude', ?, 'percent', ?, ?)",
                (remaining, windows, db.now()))
        self.con.commit()
        _, payload = self.json_request()
        entry, = payload["runway"]
        self.assertEqual(entry["provider"], "claude")
        self.assertEqual(entry["remaining"], 40.0)
        self.assertEqual(entry["kind"], "plan")
        self.assertTrue(entry["known"])
        self.assertEqual([w["label"] for w in entry["windows"]], ["5h", "weekly"])
        # `as_of` is shipped now, deliberately reversing an earlier decision to
        # omit the reading age. That decision assumed a stale reading meant the
        # daemon had failed to poll -- true for the providers Dromond calls,
        # false for Claude, whose numbers come from a cache file Claude Code
        # writes and no amount of polling can refresh. A figure that may be
        # days old is indistinguishable from a fresh one without it.
        self.assertIn("as_of", entry)
        self.assertIn("age_hours", entry)
        for key in ("limit", "polled_at", "trend", "stale"):
            self.assertNotIn(key, entry, key)
        for w in entry["windows"]:
            self.assertNotIn("limit", w)

    def test_a_windows_pace_is_measured_against_the_same_window(self) -> None:
        """W-0182 replaced the sparkline: a window compares only with an
        earlier reading of ITSELF, never across a reset."""
        def poll(label, remaining, resets_at, minutes_ago):
            stamp = (datetime.now(timezone.utc) -
                     timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.con.execute(
                "INSERT INTO runway_polls(provider, remaining, unit, resets_at, "
                "windows, polled_at) VALUES('kimi', ?, 'percent', ?, ?, ?)",
                (remaining, resets_at, json.dumps([
                    {"label": label, "remaining": remaining, "unit": "percent",
                     "resets_at": resets_at, "stale": False,
                     "stale_reason": None}]), stamp))
        later = (datetime.now(timezone.utc) +
                 timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        poll("5h", 30.0, "2026-01-01T00:00:00Z", 300)  # a window that has gone
        poll("5h", 90.0, later, 120)
        poll("5h", 60.0, later, 0)
        self.con.commit()
        _, payload = self.json_request()
        window, = payload["runway"][0]["windows"]
        self.assertEqual(window["pace"], "using 15% an hour")
        self.assertTrue(window["resets_in"].startswith("in 2h 5"))

    def test_the_runway_route_replays_stored_polls_without_refresh(self) -> None:
        self.con.execute(
            "INSERT INTO runway_polls(provider, remaining, unit, polled_at) "
            "VALUES('deepseek', 10.5, 'USD', ?)", (db.now(),))
        self.con.commit()
        _, payload = self.json_request(path="/api/runway")
        entry, = payload["runway"]
        self.assertEqual((entry["provider"], entry["unit"], entry["kind"]),
                         ("deepseek", "USD", "api"))
        self.assertTrue(payload["generated_at"])

    def test_banked_credits_reach_the_card_and_a_missing_one_is_null(self) -> None:
        """W-0184: the credits block serves a dollar balance AND banked
        resets, so the route carries the phrase its adapter wrote — and
        nothing at all for a provider that banks nothing."""
        for provider, raw in (("codex", '{"credits": "0 banked resets"}'),
                              ("kimi", '{"membership": "pro"}')):
            self.con.execute(
                "INSERT INTO runway_polls(provider, remaining, unit, raw, "
                "polled_at) VALUES(?, 40.0, 'percent', ?, ?)",
                (provider, raw, db.now()))
        self.con.commit()
        _, payload = self.json_request(path="/api/runway")
        self.assertEqual({e["provider"]: e["credits"] for e in payload["runway"]},
                         {"codex": "0 banked resets", "kimi": None})

    def test_refresh_polls_every_provider_and_stores_the_result(self) -> None:
        """The dashboard's refresh button forces a live poll; without
        ``?refresh=1`` the route only replays what is stored."""
        polled = []

        def fake_poll(cfg=None):  # poll_all takes the config now
            polled.append(True)
            return [runway.unknown("xai", "no endpoint"),
                    runway.Runway("codex", remaining=75.0, unit="percent")]

        with mock.patch.object(runway, "poll_all", fake_poll):
            _, payload = self.json_request(path="/api/runway?refresh=1")
        self.assertEqual(len(polled), 1)
        self.assertEqual([e["provider"] for e in payload["runway"]],
                         ["codex", "xai"])
        stored = self.con.execute(
            "SELECT provider FROM runway_polls ORDER BY provider").fetchall()
        self.assertEqual([r["provider"] for r in stored], ["codex", "xai"])

    def test_statistics_report_tokens_and_cost_per_profile(self) -> None:
        """DESIGN §11: the per-profile breakdown is a query over the run
        rows, and a profile whose backend recorded no usage stays null —
        never a zero that reads as free."""
        api = {"backend": "reasonix", "model": "deepseek/deepseek-v4-flash"}
        self.make_run(status="done", profile="ds", finished_at=db.now(),
                      tokens_total=1000, cost_usd=0.25, usage_source="reasonix", **api)
        self.make_run(status="done", profile="ds", finished_at=db.now(),
                      tokens_total=500, cost_usd=0.75, usage_source="reasonix", **api)
        self.make_run(status="done", profile="cx", finished_at=db.now(),
                      tokens_total=99, usage_source="codex")  # codex prices nothing
        self.make_run(profile="quiet")                        # nothing captured
        _, payload = self.json_request()
        stats = payload["statistics"]
        self.assertEqual(stats["tokens_total"], 1599)
        self.assertEqual(stats["cost_usd"], 1.0)
        by_profile = {p["profile"]: p for p in stats["by_profile"]}
        self.assertEqual(by_profile["ds"]["tokens"], 1500)
        self.assertEqual(by_profile["ds"]["cost"], 1.0)
        self.assertEqual(by_profile["ds"]["billing"], "api")
        self.assertEqual(by_profile["cx"]["tokens"], 99)
        self.assertIsNone(by_profile["cx"]["cost"])
        self.assertIsNone(by_profile["quiet"]["tokens"])
        self.assertIsNone(by_profile["quiet"]["cost"])
        self.assertEqual(by_profile["quiet"]["active"], 1)

    def test_a_plan_backed_run_has_no_price_at_all(self) -> None:
        """W-0179: a subscription reporting cost 0 (OpenCode does) is not
        free work. Plan runs contribute nothing to money and say why."""
        self.make_run(status="done", profile="kimi", backend="opencode",
                      model="kimi-for-coding/k3", finished_at=db.now(),
                      tokens_total=200, cost_usd=0.0, usage_source="opencode")
        self.make_run(status="done", profile="cc", backend="claude",
                      model="claude-opus-5", finished_at=db.now(),
                      tokens_total=50, cost_usd=3.5, usage_source="claude")
        _, payload = self.json_request()
        stats = payload["statistics"]
        self.assertIsNone(stats["cost_usd"])       # neither run has a price
        self.assertEqual(stats["plan_runs"], 2)
        by_profile = {p["profile"]: p for p in stats["by_profile"]}
        self.assertEqual(by_profile["kimi"]["billing"], "plan")
        self.assertIsNone(by_profile["kimi"]["cost"])
        self.assertEqual(by_profile["kimi"]["tokens"], 200)  # usage still counts
        self.assertEqual(by_profile["cc"]["billing"], "plan")
        self.assertIsNone(by_profile["cc"]["cost"])
        self.assertEqual({r["profile"]: r["billing"] for r in payload["runs"]},
                         {"kimi": "plan", "cc": "plan"})

    def test_the_cli_view_shows_the_same_numbers(self) -> None:
        """`dromond stats` and the snapshot read one function, so they cannot
        disagree; an uncaptured number prints as a dash, not as 0."""
        import contextlib
        import io
        from argparse import Namespace

        from dromond import cli
        self.make_run(status="done", profile="ds", backend="reasonix",
                      model="deepseek/deepseek-v4-flash", finished_at=db.now(),
                      tokens_total=1500, cost_usd=1.0, usage_source="reasonix")
        self.make_run(profile="quiet", backend="reasonix",
                      model="deepseek/deepseek-v4-flash")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.cmd_stats(Namespace(json=False))
        text = out.getvalue()
        self.assertIn("2 total, 1 active", text)
        self.assertIn("1,500", text)
        self.assertIn("$1.0000", text)
        self.assertRegex(text, r"quiet\s+1\s+1\s+\S+\s+–\s+–")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.cmd_stats(Namespace(json=True))
        _, payload = self.json_request()
        self.assertEqual(json.loads(out.getvalue()), payload["statistics"])

    def test_profiles_carry_the_headroom_note_and_its_age(self) -> None:
        self.config_path.write_text(
            '[profiles.codex]\nbackend = "codex"\n'
            'note = "10% weekly left"\nnote_at = "2020-01-01T00:00:00Z"\n')
        _, payload = self.json_request()
        codex, = [p for p in payload["profiles"] if p["name"] == "codex"]
        self.assertEqual(codex["note"], "10% weekly left")
        self.assertTrue(codex["note_age"])

    def test_daemon_health_answers_is_the_sweeper_working(self) -> None:
        mhttp.record_health({"swept": [{"action": "dispatch", "item": "W-0007"}],
                             "released": [1], "reaped": []}, con=self.con)
        _, payload = self.json_request()
        healthy = payload["daemon"]
        self.assertEqual(healthy["outcome"], "ok")
        self.assertEqual(healthy["claimed"], ["W-0007"])
        self.assertEqual(healthy["released"], 1)
        self.assertIsNone(healthy["error"])
        self.assertTrue(healthy["last_sweep_at"])

    def test_a_failed_pass_records_its_error_and_keeps_it_after(self) -> None:
        mhttp.record_health({}, error="RuntimeError('boom')", con=self.con)
        self.assertEqual(mhttp.health(self.con)["outcome"], "error")
        mhttp.record_health({}, con=self.con)
        after = mhttp.health(self.con)
        self.assertEqual(after["outcome"], "ok")
        self.assertIsNone(after["error"])
        self.assertIn("boom", after["last_error"])


OTHER_PROJECT = "9b1f0f5e-0e8e-4a2d-9d18-1f2c3d4e5f60"


class ProjectPickerTests(ServerCase):
    """W-0186: the dashboard's project switcher and what a project scopes.

    The picker is derived from the runs and nothing else, and the two things
    a project actually changes — its effective profiles and its own
    statistics — come off ``GET /api/project``.
    """

    def name_project(self, project_id: str, work_id: str, name: str) -> None:
        self.con.execute(
            "INSERT INTO projects(path, project_id, work_id, name, refreshed_at) "
            "VALUES(?,?,?,?,?)",
            (str(self.tmp_path / name), project_id, work_id, name, db.now()))
        self.con.commit()

    def test_the_project_list_is_derived_from_the_runs(self) -> None:
        self.name_project(PROJECT_ID, "P-1", "alpha")
        self.name_project(OTHER_PROJECT, "P-2", "bravo")
        self.make_run()                                   # alpha, live
        self.make_run(status="done", finished_at=db.now())  # alpha, finished
        self.make_run(project_id=OTHER_PROJECT)           # bravo, live
        _, payload = self.json_request()
        self.assertEqual(
            payload["projects"],
            [{"project_id": PROJECT_ID, "name": "P-1", "runs": 2, "live": 1},
             {"project_id": OTHER_PROJECT, "name": "P-2", "runs": 1, "live": 1}])

    def test_a_project_with_no_runs_is_not_offered(self) -> None:
        """The picker offers what you have kicked something off from. A
        project Dromond merely KNOWS about would filter the board to
        nothing, so it is not a choice."""
        self.name_project(PROJECT_ID, "P-1", "alpha")
        self.name_project(OTHER_PROJECT, "P-2", "bravo")
        self.make_run()                                   # alpha only
        _, payload = self.json_request()
        self.assertEqual([p["project_id"] for p in payload["projects"]],
                         [PROJECT_ID])

    def test_a_run_without_a_project_is_in_no_project(self) -> None:
        self.make_run(project_id=None)
        _, payload = self.json_request()
        self.assertEqual(payload["projects"], [])
        self.assertEqual(len(payload["runs"]), 1)

    def test_the_project_route_carries_the_enabled_set_not_a_profile_list(self) -> None:
        """W-0187: profiles are GLOBAL, so the snapshot's list is the only
        list. What a project changes is which of them it may staff, and the
        route answers with that and nothing else."""
        self.config_path.write_text(
            '[profiles.probe]\nbackend = "codex"\nmodel = "gpt-5"\n\n'
            '[profiles.spare]\nbackend = "codex"\nmodel = "gpt-5-mini"\n\n'
            f'[project."{PROJECT_ID}"]\nenabled_profiles = ["probe"]\n')
        _, snap = self.json_request()
        self.assertEqual(sorted(p["name"] for p in snap["profiles"]),
                         ["probe", "spare"])
        _, scoped = self.json_request(path=f"/api/project?id={PROJECT_ID}")
        self.assertEqual(scoped["project_id"], PROJECT_ID)
        self.assertEqual(scoped["enabled_profiles"], ["probe"])
        self.assertNotIn("profiles", scoped)
        # a project that has not said enables everything, and says so as null
        _, elsewhere = self.json_request(path=f"/api/project?id={OTHER_PROJECT}")
        self.assertIsNone(elsewhere["enabled_profiles"])

    def test_the_enabled_set_is_written_back_through_the_route(self) -> None:
        self.config_path.write_text(
            '[profiles.probe]\nbackend = "codex"\nmodel = "gpt-5"\n\n'
            '[profiles.spare]\nbackend = "codex"\nmodel = "gpt-5-mini"\n')
        status, result = self.json_request(
            method="POST", path=mhttp.PROJECT_ROUTE,
            body={"project_id": PROJECT_ID, "enabled_profiles": ["spare"]})
        self.assertEqual((status, result["applied"]), (200, True))
        self.assertIn(f'[project."{PROJECT_ID}"]', self.config_path.read_text())
        _, scoped = self.json_request(path=f"/api/project?id={PROJECT_ID}")
        self.assertEqual(scoped["enabled_profiles"], ["spare"])
        # the global profiles are untouched by a project's choice
        _, snap = self.json_request()
        self.assertEqual(sorted(p["name"] for p in snap["profiles"]),
                         ["probe", "spare"])

    def test_enabling_a_profile_that_does_not_exist_is_refused(self) -> None:
        self.config_path.write_text(
            '[profiles.probe]\nbackend = "codex"\nmodel = "gpt-5"\n')
        status, result = self.json_request(
            method="POST", path=mhttp.PROJECT_ROUTE,
            body={"project_id": PROJECT_ID, "enabled_profiles": ["ghost"]})
        self.assertEqual(status, 400)
        self.assertIn("ghost", result["error"])
        self.assertNotIn("project", self.config_path.read_text())

    def test_project_statistics_count_only_that_projects_runs(self) -> None:
        self.make_run()
        self.make_run(status="done", finished_at=db.now(), tokens_total=100)
        self.make_run(project_id=OTHER_PROJECT, profile="claude",
                      backend="claude")
        _, snap = self.json_request()
        self.assertEqual(snap["statistics"]["runs_total"], 3)
        _, scoped = self.json_request(path=f"/api/project?id={PROJECT_ID}")
        stats = scoped["statistics"]
        self.assertEqual(stats["runs_total"], 2)
        self.assertEqual(stats["runs_active"], 1)
        self.assertEqual(stats["tokens_total"], 100)
        self.assertEqual([p["profile"] for p in stats["by_profile"]], ["codex"])

    def test_project_statistics_see_past_the_snapshot_window(self) -> None:
        """The board is a window of finished runs; the totals are over the
        whole history, so a project's numbers must not be the window's.

        The window is patched small here: W-0187 raised it to hold hundreds
        of runs, and building 2,005 rows to prove one boundary is a slow test
        for no extra truth."""
        with mock.patch.object(mhttp, "RECENT_RUNS", 4):
            for _ in range(9):
                self.make_run(status="done", finished_at=db.now())
            _, snap = self.json_request()
            self.assertEqual(len(snap["runs"]), 4)
            _, scoped = self.json_request(path=f"/api/project?id={PROJECT_ID}")
            self.assertEqual(scoped["statistics"]["runs_total"], 9)

    def test_the_project_route_needs_an_id(self) -> None:
        status, text = self.request(path=mhttp.PROJECT_ROUTE)
        self.assertEqual(status, 400)
        self.assertIn("id=", text)

    def test_an_unknown_project_is_empty_rather_than_an_error(self) -> None:
        """A stale id in a browser tab reads as a project with no runs, not
        as a broken route — the picker will drop it on the next snapshot."""
        _, payload = self.json_request(path="/api/project?id=not-a-project")
        self.assertEqual(payload["statistics"]["runs_total"], 0)
        self.assertIsNone(payload["enabled_profiles"])

    def test_a_run_token_cannot_read_a_project(self) -> None:
        """Unlisted in auth.ROUTES, so the human's alone: the payload is the
        enabled set and every total, which is not one run's business."""
        run_id = self.make_run()
        token = auth.mint(self.con, run_id)
        status, _ = self.request(path=f"/api/project?id={PROJECT_ID}", key=token)
        self.assertEqual(status, 403)

    def test_the_config_route_reads_and_writes_the_file(self) -> None:
        """W-0190: the settings page is the file, not a form of each key."""
        status, payload = self.json_request(path=mhttp.CONFIG_ROUTE)
        self.assertEqual(status, 200)
        self.assertEqual(payload["text"], self.config_path.read_text())
        self.assertEqual(payload["path"], str(self.config_path))
        nxt = '[settings]\ntimeout = 42\n\n[profiles.probe]\nbackend = "codex"\n'
        status, result = self.json_request(
            method="POST", path=mhttp.CONFIG_ROUTE,
            body={"text": nxt, "restart": True})
        self.assertEqual((status, result["applied"], result["restarting"]),
                         (200, True, True))
        self.assertEqual(self.config_path.read_text(), nxt)
        self.assertTrue(self.restart.is_set())

    def test_a_broken_config_is_refused_and_the_file_stays(self) -> None:
        before = self.config_path.read_text()
        status, result = self.json_request(
            method="POST", path=mhttp.CONFIG_ROUTE,
            body={"text": "timeout ="})
        self.assertEqual(status, 400)
        self.assertIn("TOML", result["error"])
        self.assertEqual(self.config_path.read_text(), before)
        self.assertFalse(self.restart.is_set())

    def test_a_run_token_cannot_read_or_write_the_config(self) -> None:
        run_id = self.make_run()
        token = auth.mint(self.con, run_id)
        status, _ = self.request(path=mhttp.CONFIG_ROUTE, key=token)
        self.assertEqual(status, 403)
        status, _ = self.request(method="POST", path=mhttp.CONFIG_ROUTE,
                                 key=token, body={"text": ""})
        self.assertEqual(status, 403)

    def test_a_run_token_cannot_widen_its_projects_enabled_set(self) -> None:
        """DESIGN §5, principle 5: nothing grants itself what it asks for. A
        worker enabling a dearer profile for its own project is exactly that,
        so the write route is the human's alone."""
        run_id = self.make_run()
        token = auth.mint(self.con, run_id)
        status, _ = self.request(method="POST", path=mhttp.PROJECT_ROUTE,
                                 key=token,
                                 body={"project_id": PROJECT_ID,
                                       "enabled_profiles": ["probe"]})
        self.assertEqual(status, 403)


class ActionTests(ServerCase):
    def test_stop_marks_the_run_killed(self) -> None:
        run_id = self.make_run()
        status, payload = self.json_request(
            method="POST", path=f"/api/runs/{run_id}/stop")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "killed")
        self.assertEqual(self.con.execute(
            "SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()["status"],
            "killed")

    def test_stop_on_an_unknown_run_is_400_not_500(self) -> None:
        status, payload = self.json_request(method="POST", path="/api/runs/999/stop")
        self.assertEqual(status, 400)
        self.assertIn("no run 999", payload["error"])

    def test_tell_queues_an_interrupt_message(self) -> None:
        run_id = self.make_run(session_ref="sess-1")
        status, payload = self.json_request(
            method="POST", path=f"/api/runs/{run_id}/tell",
            body={"text": "use the other branch"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["queued"])
        row = self.con.execute(
            "SELECT * FROM messages WHERE run_id=?", (run_id,)).fetchone()
        self.assertEqual(row["kind"], "interrupt")
        self.assertEqual(row["body"], "use the other branch")

    def test_tell_refuses_an_empty_message_and_a_finished_run(self) -> None:
        run_id = self.make_run(status="done", session_ref="sess-1")
        self.assertEqual(self.json_request(
            method="POST", path=f"/api/runs/{run_id}/tell",
            body={"text": " "})[0], 400)
        self.assertEqual(self.json_request(
            method="POST", path=f"/api/runs/{run_id}/tell",
            body={"text": "hi"})[0], 400)

    def test_check_reports_a_mechanical_verdict(self) -> None:
        run_id = self.make_run()
        status, payload = self.json_request(
            method="POST", path=f"/api/runs/{run_id}/check")
        self.assertEqual(status, 200)
        self.assertEqual(payload["run"], run_id)
        self.assertTrue(payload["verdict"])

    def test_sweep_wakes_the_daemon_loop(self) -> None:
        self.assertFalse(self.wake.is_set())
        status, payload = self.json_request(method="POST", path="/api/sweep")
        self.assertEqual(status, 200)
        self.assertTrue(payload["queued"])
        self.assertTrue(self.wake.is_set())

    def test_sweep_without_a_loop_says_so(self) -> None:
        self.srv.wake = None
        self.assertEqual(self.request(method="POST", path="/api/sweep")[0], 503)

    def test_an_unknown_route_is_404_with_a_reason(self) -> None:
        status, text = self.request(path="/api/nope")
        self.assertEqual(status, 404)
        self.assertIn("/api/nope", text)


class BriefRouteTests(ServerCase):
    """The brief tab's own read (W-0183): one run's brief file, on demand.

    The point of the route is that the brief is NOT in the snapshot, which
    carries every run on a 4s poll — so the first test here is that one.
    """

    def a_run_with_a_brief(self, text="# Run 1\n\n## Mission\n\nship it\n") -> int:
        path = self.tmp_path / "run-brief.md"
        path.write_text(text)
        return self.make_run(brief_path=str(path)), path

    def test_the_route_returns_the_whole_brief_text(self) -> None:
        run_id, path = self.a_run_with_a_brief()
        status, payload = self.json_request(path=f"/api/runs/{run_id}/brief")
        self.assertEqual(status, 200)
        self.assertEqual(payload["run"], run_id)
        self.assertEqual(payload["text"], path.read_text())
        self.assertEqual(payload["path"], str(path))

    def test_the_snapshot_still_carries_no_brief_text(self) -> None:
        """~35 runs every 4 seconds is why this is a route at all."""
        self.a_run_with_a_brief(text="secret-mission-body")
        _, snap = self.json_request(path="/api/snapshot")
        self.assertNotIn("secret-mission-body", json.dumps(snap))
        self.assertIn("brief_path", snap["runs"][0])

    def test_a_missing_file_is_a_message_not_an_error(self) -> None:
        run_id, path = self.a_run_with_a_brief()
        path.unlink()
        status, payload = self.json_request(path=f"/api/runs/{run_id}/brief")
        self.assertEqual(status, 200)
        self.assertIsNone(payload["text"])
        self.assertNotIn("error", payload)
        self.assertIn("gone", payload["message"])

    def test_a_run_that_never_had_a_brief_file_says_so(self) -> None:
        run_id = self.make_run()
        status, payload = self.json_request(path=f"/api/runs/{run_id}/brief")
        self.assertEqual(status, 200)
        self.assertIsNone(payload["path"])
        self.assertIn("title", payload["message"])

    def test_an_unknown_run_is_400_not_500(self) -> None:
        status, payload = self.json_request(path="/api/runs/9999/brief")
        self.assertEqual(status, 400)
        self.assertIn("9999", payload["error"])

    def test_the_handler_is_never_reached_without_the_key(self) -> None:
        run_id, _ = self.a_run_with_a_brief()
        with mock.patch.object(mhttp, "run_brief") as handler:
            self.assertEqual(
                self.request(path=f"/api/runs/{run_id}/brief", key=None)[0], 401)
            self.assertEqual(self.request(path=f"/api/runs/{run_id}/brief",
                                          key="not-the-key")[0], 401)
        handler.assert_not_called()


class DiffRouteTests(ServerCase):
    """The Merge tab reads an immutable run diff without bloating snapshots."""

    def committed_run(self) -> tuple[int, str]:
        root = self.tmp_path / "repo"
        root.mkdir()

        def git(*args):
            result = subprocess.run(["git", *args], cwd=root, capture_output=True,
                                    text=True, check=True)
            return result.stdout.strip()

        git("init", "--quiet")
        git("config", "user.email", "test@example.com")
        git("config", "user.name", "test")
        (root / "app.py").write_text("before\n")
        git("add", "-A")
        git("commit", "--quiet", "-m", "base")
        base = git("rev-parse", "HEAD")
        base_branch = git("branch", "--show-current")
        git("checkout", "--quiet", "-b", "dromond/run-diff")
        (root / "app.py").write_text("after-from-diff-pane\n")
        git("commit", "--quiet", "-am", "change")
        head = git("rev-parse", "HEAD")
        git("checkout", "--quiet", base_branch)
        git("branch", "-D", "dromond/run-diff")
        run_id = self.make_run(
            status="done", finished_at=db.now(), workdir=str(root),
            branch="dromond/run-diff", base_commit=base, checkpoint_commit=head)
        return run_id, head

    def test_diff_survives_the_rename_for_a_run_merged_before_it(self) -> None:
        """W-0188. A run merged while the product was still Maestro has its
        only surviving pointer in refs/maestro/: no checkpoint (it committed
        its own work) and no branch (merging deleted it). Reading only the new
        namespace would lose that run's diff forever."""
        root = self.tmp_path / "legacy-repo"
        root.mkdir()

        def git(*args):
            result = subprocess.run(["git", *args], cwd=root, capture_output=True,
                                    text=True, check=True)
            return result.stdout.strip()

        git("init", "--quiet")
        git("config", "user.email", "test@example.com")
        git("config", "user.name", "test")
        (root / "app.py").write_text("before\n")
        git("add", "-A")
        git("commit", "--quiet", "-m", "base")
        base = git("rev-parse", "HEAD")
        base_branch = git("branch", "--show-current")
        git("checkout", "--quiet", "-b", "maestro/run-77")
        (root / "app.py").write_text("after-from-a-maestro-era-run\n")
        git("commit", "--quiet", "-am", "change")
        head = git("rev-parse", "HEAD")
        git("checkout", "--quiet", base_branch)
        # Exactly what the old merge_run left behind: the anchor, then no branch.
        git("update-ref", "refs/maestro/run-77", head)
        git("branch", "-D", "maestro/run-77")

        run_id = self.make_run(status="done", finished_at=db.now(),
                               workdir=str(root), branch="maestro/run-77",
                               base_commit=base, checkpoint_commit=None)
        status, payload = self.json_request(path=f"/api/runs/{run_id}/diff")
        self.assertEqual(status, 200)
        self.assertEqual(payload["head"], head)
        self.assertIn("+after-from-a-maestro-era-run", payload["text"])

    def test_diff_survives_the_run_branch_being_deleted(self) -> None:
        run_id, head = self.committed_run()
        status, payload = self.json_request(path=f"/api/runs/{run_id}/diff")
        self.assertEqual(status, 200)
        self.assertEqual(payload["head"], head)
        self.assertIn("+after-from-diff-pane", payload["text"])
        self.assertIn("-before", payload["text"])

    def test_diff_is_capped_and_stays_out_of_the_snapshot(self) -> None:
        run_id, _ = self.committed_run()
        with mock.patch.object(mhttp, "DIFF_BYTES", 20):
            _, payload = self.json_request(path=f"/api/runs/{run_id}/diff")
        self.assertTrue(payload["truncated"])
        _, snap = self.json_request()
        self.assertNotIn("after-from-diff-pane", json.dumps(snap))

    def test_a_run_token_reads_only_its_own_diff(self) -> None:
        run_id = self.make_run()
        sibling = self.make_run()
        token = auth.mint(self.con, run_id)
        self.assertEqual(self.request(path=f"/api/runs/{run_id}/diff",
                                      key=token)[0], 200)
        self.assertEqual(self.request(path=f"/api/runs/{sibling}/diff",
                                      key=token)[0], 403)


class PauseTests(ServerCase):
    def test_pause_and_resume_move_the_switch(self) -> None:
        _, payload = self.json_request(method="POST", path="/api/dispatch/pause")
        self.assertTrue(payload["paused"])
        self.assertTrue(payload["since"])
        _, payload = self.json_request(method="POST", path="/api/dispatch/resume")
        self.assertFalse(payload["paused"])
        self.assertIsNone(payload["since"])

    def test_pause_survives_a_restart(self) -> None:
        """The switch lives in meta, not in memory: a daemon that forgets it
        is paused resumes dispatch silently, which is the whole failure."""
        self.json_request(method="POST", path="/api/dispatch/pause")
        # Restart: drop the server and every open connection, then reopen.
        self.stop.set()
        self.srv.shutdown()
        self.srv.server_close()
        self.con.close()
        self.stop = threading.Event()
        self.srv = self._serve()
        self.con = db.connect()
        _, payload = self.json_request()
        self.assertTrue(payload["dispatch"]["paused"])
        self.assertTrue(mhttp.dispatch_paused(self.con))

    def test_a_paused_daemon_tick_starts_nothing(self) -> None:
        from dromond import daemon, supervise
        mhttp.set_dispatch_paused(self.con, True)
        with mock.patch.object(supervise, "process_ready") as ready:
            report = daemon.tick()
        self.assertTrue(report["paused"])
        self.assertEqual(report["released"], [])
        ready.assert_not_called()
        # …and health says so, rather than reporting a sweep that never ran.
        mhttp.record_health(report, con=self.con)
        self.assertEqual(mhttp.health(self.con)["outcome"], "paused")


class SseSeamTests(ServerCase):
    """The two live streams (W-0165/W-0178): one run's trace, and the log."""

    def a_run_with_a_trace(self, status="done") -> int:
        from dromond import traces
        log = self.tmp_path / "run.jsonl"
        log.write_text(json.dumps(
            {"type": "assistant",
             "message": {"content": [{"type": "text", "text": "hello"}]}}) + "\n")
        run_id = self.make_run(status=status, backend="claude",
                               log_path=str(log), finished_at=db.now())
        traces.ingest(self.con, run_id, str(log), "claude")
        return run_id

    def daemon_log(self, *lines) -> None:
        from dromond import paths
        path = paths.logs_dir() / "daemon.out.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(line + "\n" for line in lines))

    def test_a_run_trace_streams_event_stream_frames(self) -> None:
        run_id = self.a_run_with_a_trace()
        status, ctype, body = self.sse(f"/api/runs/{run_id}/stream")
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", ctype)
        self.assertIn("event: trace", body)
        self.assertIn("hello", body)
        # A terminal run's stream closes itself rather than polling forever.
        self.assertIn("event: end", body)

    def test_a_trace_resumes_from_the_event_id_it_last_sent(self) -> None:
        run_id = self.a_run_with_a_trace()
        first = self.sse(f"/api/runs/{run_id}/stream")[2]
        last = max(int(line.split(":", 1)[1]) for line in first.splitlines()
                   if line.startswith("id:"))
        body = self.sse(f"/api/runs/{run_id}/stream", last_event_id=str(last))[2]
        self.assertNotIn("event: trace", body)  # nothing after the cursor
        self.assertIn("event: end", body)

    def test_a_live_run_keeps_its_stream_open(self) -> None:
        run_id = self.a_run_with_a_trace(status="running")
        status, ctype, body = self.sse(f"/api/runs/{run_id}/stream",
                                       until="hello")
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", ctype)
        self.assertIn("retry: ", body)  # the reconnect hint leads every stream
        self.assertNotIn("event: end", body)

    def test_the_daemon_log_streams_and_resumes_on_a_file_offset(self) -> None:
        self.daemon_log("dromond: swept")
        status, ctype, body = self.sse("/api/log/stream", until="dromond: swept")
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", ctype)
        self.assertIn("dromond: swept", body)
        cursor = [line for line in body.splitlines() if line.startswith("id:")][0]
        cursor = cursor.split(":", 1)[1].strip()
        self.assertIn("daemon.out.log@", cursor)
        # Resuming at that cursor replays nothing; the next line does arrive.
        self.daemon_log("dromond: swept", "dromond: swept again")
        body = self.sse("/api/log/stream", last_event_id=cursor,
                        until="swept again")[2]
        self.assertNotIn("dromond: swept\"", body)
        self.assertIn("swept again", body)

    def test_an_unknown_stream_path_is_a_named_501_not_a_silent_404(self) -> None:
        status, text = self.request(path="/api/nothing/stream")
        self.assertEqual(status, 501)
        self.assertIn("/api/nothing/stream", text)

    def test_the_seam_is_authenticated_before_it_is_called(self) -> None:
        with mock.patch.object(mhttp, "sse_stream") as seam:
            for path in ("/api/log/stream", "/api/runs/1/stream"):
                with self.subTest(path=path):
                    self.assertEqual(self.request(path=path, key=None)[0], 401)
                    self.assertEqual(
                        self.request(path=path, key="not-the-key")[0], 401)
        seam.assert_not_called()


class KeyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.toml"
        self.env = mock.patch.dict(os.environ, {"DROMOND_CONFIG": str(self.path)})
        self.env.start()
        os.environ.pop(mhttp.KEY_ENV, None)

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def test_init_mints_one_key_into_a_0600_config(self) -> None:
        key, minted = mhttp.ensure_key()
        self.assertTrue(minted)
        self.assertGreaterEqual(len(key), 32)
        if sys.platform != "win32":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        again, minted_again = mhttp.ensure_key()
        self.assertEqual(again, key)
        self.assertFalse(minted_again)
        self.assertEqual(config.load()["http"]["key"], key)

    def test_the_environment_overrides_the_file(self) -> None:
        mhttp.ensure_key()
        with mock.patch.dict(os.environ, {mhttp.KEY_ENV: "from-the-env"}):
            self.assertEqual(mhttp.load_key(), "from-the-env")

    def test_no_key_means_no_port(self) -> None:
        self.path.write_text("")
        self.assertIsNone(mhttp.serve(None, addr="127.0.0.1", port=0, cfg={}))


class BindTests(unittest.TestCase):
    def test_bind_prefers_the_tailscale_address(self) -> None:
        with mock.patch.object(mhttp, "tailscale_address", return_value="100.1.2.3"):
            self.assertEqual(mhttp.bind_address({}), "100.1.2.3")

    def test_bind_falls_back_to_loopback_not_every_interface(self) -> None:
        with mock.patch.object(mhttp, "tailscale_address", return_value=None):
            self.assertEqual(mhttp.bind_address({}), "127.0.0.1")

    def test_an_explicit_bind_wins(self) -> None:
        self.assertEqual(mhttp.bind_address({"http": {"bind": "127.0.0.1"}}),
                         "127.0.0.1")


class DashboardFileTests(unittest.TestCase):
    """The page is one hand-written file with no build step, so nothing type
    checks it. These are the two things that would rot silently (W-0180)."""

    def test_the_settings_view_edits_the_config_file(self) -> None:
        """A tab with no textarea is a dead control, same class of miss as
        the restart route shipping without its button."""
        page = (Path(__file__).resolve().parents[1]
                 / "dromond" / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn('data-view="settings"', page)
        self.assertIn('id="cfgtext"', page)
        self.assertIn("/api/config", page)
        self.assertIn("save and restart", page)

    def test_the_health_view_has_a_restart_control(self) -> None:
        """The route shipped without the button once already: the
        backend answered /api/restart while the dashboard had no way to
        reach it, so the feature was invisible and got re-delegated.
        """
        page = (Path(__file__).resolve().parents[1]
                 / "dromond" / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn('id="restart"', page)
        self.assertIn("/api/restart", page)

    def setUp(self) -> None:
        self.text = mhttp.DASHBOARD.read_text(encoding="utf-8")

    def test_every_run_detail_tab_has_a_pane_and_trace_is_first(self) -> None:
        """A tab with no builder in PANES is a dead control on the strip."""
        strip = re.search(r"const TABS = \[(.*?)\n\];", self.text, re.S)
        panes = re.search(r"const PANES = \{(.*?)\n\};", self.text, re.S)
        self.assertTrue(strip and panes, "the run detail tab strip is gone")
        keys = re.findall(r'\["(\w+)",', strip.group(1))
        self.assertEqual(keys[0], "trace", "the trace tab is the default")
        for key in keys:
            self.assertRegex(panes.group(1), rf"\n  {key}:",
                             f"the {key} tab has no pane builder")

    def test_the_health_view_tells_nobody_to_run_a_command(self) -> None:
        """W-0183: 'sweep now' and 'pause dispatch' are right there, and the
        page is served BY the daemon — telling the reader to start one was
        both noise and impossible advice."""
        body = re.search(r"function renderDaemon\(d\) \{(.*?)\n\}", self.text, re.S)
        self.assertTrue(body, "renderDaemon is gone")
        self.assertNotIn("dromond ", body.group(1))

    def test_the_tab_strip_sits_outside_the_scrolling_body(self) -> None:
        """Header and tabs above #pbody is what keeps the run actions from
        scrolling away — the whole point of the ticket."""
        order = [self.text.index(f'id="{i}"') for i in ("phead", "ptabs", "pbody")]
        self.assertEqual(order, sorted(order))
        self.assertNotIn("innerHTML", self.text)

    def test_live_run_actions_are_useful_and_separated(self) -> None:
        """W-0185: instruction gets an on-demand tab; stop is separated from
        its composer at the right edge of the detail tabs."""
        body = re.search(r"function instructionPane\(r\) \{(.*?)\n\}",
                         self.text, re.S)
        self.assertTrue(body, "instructionPane is gone")
        actions = body.group(1)
        self.assertIn('"send instruction"', actions)
        self.assertNotIn('"stop run"', actions)
        self.assertIn('el("textarea")', actions)
        self.assertIn("INSTRUCTION_DRAFTS", actions)
        self.assertIn("send.disabled = !r.session_ref", actions)
        self.assertIn("body.contains(document.activeElement)", self.text)
        self.assertNotIn("prompt(", actions)
        self.assertNotIn("check progress", actions)
        self.assertNotIn('"/check"', actions)
        self.assertNotRegex(actions, r'el\("button", [^\n]+, "(?:tell|check|stop)"\)')
        tabs = re.search(r"function renderTabs\(run\) \{(.*?)\n\}", self.text, re.S)
        self.assertTrue(tabs, "renderTabs is gone")
        self.assertIn('["instruction", "send instruction"]', self.text)
        self.assertIn('run.live || key !== "instruction"', tabs.group(1))
        self.assertIn('"danger stop-run", "stop run"', tabs.group(1))
        self.assertIn("if (run.live)", tabs.group(1))
        self.assertIn("#ptabs .stop-run { margin-left: auto; }", self.text)

    def section(self, view: str) -> str:
        start = self.text.index(f'id="{view}"')
        return self.text[start:self.text.index("</section>", start)]

    def test_statistics_is_a_runs_page_control_not_a_health_card(self) -> None:
        """W-0186: statistics belong beside the runs they count. Health keeps
        the daemon's own pulse, its two controls and its log — and nothing
        else, or the move was for nothing."""
        runs, health = self.section("view-runs"), self.section("view-health")
        self.assertIn('id="statsbtn"', runs)
        self.assertNotIn('id="stats"', health)
        self.assertIn('id="stats"', self.text[self.text.index('id="statsdlg"'):])
        for control in ('id="daemon"', 'id="sweep"', 'id="pause"', 'id="logtail"'):
            self.assertIn(control, health)

    def test_the_project_picker_replaced_the_snapshot_stamp(self) -> None:
        """The header carries a control, not a version number: the stamp
        moved into the statistics popup (W-0186)."""
        header = self.text[self.text.index("<header>"):self.text.index("</header>")]
        self.assertIn('id="projbtn"', header)
        self.assertIn('id="projmenu"', header)
        self.assertNotIn('id="stamp"', self.text)

    def test_no_scrolling_pane_is_pinned_to_a_slice_of_the_viewport(self) -> None:
        """W-0186: a short runs list left dead space under it because every
        pane was capped at a fixed `vh`. Each one now takes the height its
        view is given, and only the narrow-screen fallback (where the columns
        stack and the page scrolls) may still cap anything."""
        style = self.text[self.text.index("<style>"):self.text.index("</style>")]
        desktop = re.sub(r"@media[^{]*\{(?:[^{}]|\{[^{}]*\})*\}", "", style)
        for pane in ("#runlist", "#pbody", "#logtail"):
            rule = re.search(re.escape(pane) + r"\s*\{([^}]*)\}", desktop)
            self.assertTrue(rule, f"{pane} lost its rule")
            self.assertIn("flex: 1", rule.group(1),
                          f"{pane} does not fill the height it is given")
            self.assertNotIn("vh", rule.group(1),
                             f"{pane} is pinned to a slice of the viewport")


if __name__ == "__main__":
    unittest.main()
