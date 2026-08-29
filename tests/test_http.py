"""The HTTP surface (DESIGN §3).

Nothing here binds a public port or reads the developer's real state: every
server binds 127.0.0.1 on port 0 and ORCHESTRA_HOME/ORCHESTRA_CONFIG point at a
throwaway directory. No daemon is started.
"""
import gzip
import json
import re
import os
import signal as signal_module
import subprocess
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest import mock

from orchestra import auth, config, db, observer, runway
from orchestra import http as mhttp

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
            "ORCHESTRA_HOME": str(self.tmp_path / "home"),
            "ORCHESTRA_CONFIG": str(self.config_path)})
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
        # create_run stamps this inside its reservation; a row inserted
        # straight into the table has to number itself the same way.
        if fields.get("layer") is None:
            self.con.execute(
                f"UPDATE runs SET project_seq = {db.NEXT_PROJECT_SEQ} "
                "WHERE id=?", (fields["project_id"], run_id))
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
        self.assertIn("<title>orchestra</title>", text)

    def test_a_first_visit_may_carry_the_key_in_the_query(self) -> None:
        status, text = self.request(path="/?key=" + KEY, key=None)
        self.assertEqual(status, 200)
        self.assertIn("orchestra", text)
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
    def test_the_snapshot_is_the_versioned_control_plane(self) -> None:
        _, payload = self.json_request()
        self.assertIsInstance(payload["version"], int)
        self.assertEqual(payload["version"], mhttp.SNAPSHOT_VERSION)
        self.assertEqual(
            set(payload),
            {"version", "generated_at", "home", "runs", "live_runs", "dispatch",
             "projects", "profiles", "runway", "statistics", "daemon",
             })
        self.assertEqual(payload["dispatch"], {"paused": False, "since": None})
        # There are no default profiles (DESIGN §5), so the snapshot lists
        # exactly what is configured — here, the one this case declares.
        self.assertEqual([p["name"] for p in payload["profiles"]], ["probe"])
        self.assertNotIn("spawn_profiles", payload["profiles"][0])

    def test_runs_report_effective_isolation_without_guessing_from_branch(self) -> None:
        isolated = self.make_run(status="done", branch="orchestra/run-1",
                                 finished_at=db.now())
        shared = self.make_run(status="done", finished_at=db.now())
        failed = self.make_run(
            status="failed", finished_at=db.now(),
            summary="Launch setup failed: cannot create worktree")
        failed_retry = self.make_run(
            status="failed", finished_at=db.now(),
            summary="Retry launch failed: process could not start")
        _, payload = self.json_request()
        modes = {r["id"]: r["isolation"] for r in payload["runs"]}
        self.assertEqual(modes[isolated], "isolated")
        self.assertEqual(modes[shared], "shared")
        self.assertEqual(modes[failed], "not_started")
        self.assertEqual(modes[failed_retry], "not_started")
        self.assertEqual(mhttp.run_diff(self.con, failed)["message"],
                         "no branch — execution never started")

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
        # ...and it is the same payload once unwrapped. The two requests are
        # two snapshots, so `generated_at` is allowed to differ by the second
        # that passed between them — comparing them raw failed whenever the
        # clock ticked mid-test, roughly one run in twenty.
        def settled(raw):
            payload = json.loads(raw)
            payload.pop("generated_at")
            return payload
        self.assertEqual(settled(gzip.decompress(zipped)), settled(plain))

    def test_a_small_response_is_not_worth_compressing(self) -> None:
        """Below GZIP_MIN_BYTES the gzip header costs more than it saves."""
        status, body, encoding = self.raw_request("gzip", path="/api/runway")
        self.assertEqual(status, 200)
        self.assertLess(len(body), mhttp.GZIP_MIN_BYTES)
        self.assertIsNone(encoding)

    def test_runs_carry_status_profile_ref_and_project(self) -> None:
        self.con.execute(
            "INSERT INTO projects(project_id, slug, name, local, refreshed_at) "
            "VALUES(?,?,?,0,?)",
            (PROJECT_ID, "P-1", "demo", db.now()))
        live = self.make_run(ref="W-0100", title="build the HTTP surface")
        done = self.make_run(status="done", finished_at=db.now())
        _, payload = self.json_request()
        by_id = {r["id"]: r for r in payload["runs"]}
        self.assertEqual(payload["live_runs"], 1)
        self.assertEqual(by_id[live]["status"], "running")
        self.assertEqual(by_id[live]["profile"], "codex")
        self.assertEqual(by_id[live]["ref"], "W-0100")
        self.assertEqual(by_id[live]["project"], "P-1")
        self.assertTrue(by_id[live]["live"])
        self.assertFalse(by_id[done]["live"])
        self.assertEqual(payload["statistics"]["runs_total"], 2)
        self.assertEqual(payload["statistics"]["runs_active"], 1)
        self.assertEqual(
            [p["profile"] for p in payload["statistics"]["by_profile"]], ["codex"])

    def _staff(self, *profiles: str) -> None:
        """The board shows the providers this workspace is STAFFED on, so a
        runway test has to say who it staffs (2026-08-26)."""
        self.config_path.write_text("\n\n".join(profiles))

    def test_the_runway_route_replays_stored_polls_without_refresh(self) -> None:
        self._staff('[profiles.ds]\nbackend = "reasonix"\n'
                    'model = "deepseek/deepseek-v4-flash"\n')
        self.con.execute(
            "INSERT INTO runway_polls(provider, remaining, unit, polled_at) "
            "VALUES('deepseek', 10.5, 'USD', ?)", (db.now(),))
        self.con.commit()
        _, payload = self.json_request(path="/api/runway")
        entry, = payload["runway"]
        self.assertEqual((entry["provider"], entry["unit"], entry["kind"]),
                         ("deepseek", "USD", "api"))
        self.assertTrue(payload["generated_at"])

    def test_refresh_polls_every_provider_and_stores_the_result(self) -> None:
        """The dashboard's refresh button forces a live poll; without
        ``?refresh=1`` the route only replays what is stored."""
        polled = []

        self._staff('[profiles.probe]\nbackend = "codex"\nmodel = "gpt-5"\n',
                    '[profiles.grok]\nbackend = "opencode"\n'
                    'model = "xai/grok-4.6"\n')

        def fake_poll(cfg=None, all_providers=False):
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

    def test_the_cli_view_shows_the_same_numbers(self) -> None:
        """`orchestra stats` and the snapshot read one function, so they cannot
        disagree; an uncaptured number prints as a dash, not as 0."""
        import contextlib
        import io
        from argparse import Namespace

        from orchestra import cli
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

    def name_project(self, project_id: str, slug: str, name: str) -> None:
        self.con.execute(
            "INSERT INTO projects(project_id, slug, name, local, refreshed_at) "
            "VALUES(?,?,?,0,?)",
            (project_id, slug, name, db.now()))
        self.con.commit()

    def test_the_project_list_is_derived_from_the_runs(self) -> None:
        self.name_project(PROJECT_ID, "P-1", "alpha")
        self.name_project(OTHER_PROJECT, "P-2", "bravo")
        self.name_project("unused-project", "P-3", "charlie")
        self.make_run()                                   # alpha, live
        self.make_run(status="done", finished_at=db.now())  # alpha, finished
        self.make_run(project_id=OTHER_PROJECT)           # bravo, live
        self.make_run(project_id=None)                    # visible, but unscoped
        _, payload = self.json_request()
        self.assertEqual(
            payload["projects"],
            [{"project_id": PROJECT_ID, "name": "P-1", "runs": 2, "live": 1},
             {"project_id": OTHER_PROJECT, "name": "P-2", "runs": 1, "live": 1}])
        self.assertEqual(len(payload["runs"]), 4)

    def test_an_archived_project_is_off_the_picker_but_keeps_its_runs(self) -> None:
        """DESIGN §1: parking hides the project, never its history. The board
        still lists its runs under "all projects", and the client drops a
        scope whose project left the list, so nothing strands."""
        self.name_project(PROJECT_ID, "P-1", "alpha")
        self.name_project(OTHER_PROJECT, "P-2", "bravo")
        self.con.execute("UPDATE projects SET archived=1 WHERE project_id=?",
                         (OTHER_PROJECT,))
        self.con.commit()
        self.make_run()
        self.make_run(project_id=OTHER_PROJECT)
        _, payload = self.json_request()
        self.assertEqual([p["project_id"] for p in payload["projects"]],
                         [PROJECT_ID])
        self.assertEqual(len(payload["runs"]), 2, "a parked project lost a run")
        # Its statistics are still addressable by id — history is untouched.
        _, scoped = self.json_request(path=f"/api/project?id={OTHER_PROJECT}")
        self.assertEqual(1, scoped["statistics"]["runs_total"])

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

    def test_run_tokens_cannot_manage_project_or_global_configuration(self) -> None:
        run_id = self.make_run()
        token = auth.mint(self.con, run_id)
        calls = (
            ("GET", f"/api/project?id={PROJECT_ID}", None),
            ("GET", mhttp.CONFIG_ROUTE, None),
            ("POST", mhttp.CONFIG_ROUTE, {"text": ""}),
            ("POST", mhttp.PROJECT_ROUTE,
             {"project_id": PROJECT_ID, "enabled_profiles": ["probe"]}),
        )
        for method, path, body in calls:
            with self.subTest(method=method, path=path):
                self.assertEqual(self.request(method, path, key=token,
                                              body=body)[0], 403)


class ProjectRegistryTests(ServerCase):
    """``GET/POST /api/projects``: the registry panel behind the dashboard.

    Not the picker. The picker is derived from the runs (W-0186) and the
    project a human wants to park is the one with no recent run, so the panel
    reads the registry and archives through the same call the CLI makes.
    """

    def register(self, name: str, local: bool = True,
                 archived: bool = False) -> str:
        self.con.execute(
            "INSERT INTO projects(project_id, slug, name, local, "
            "refreshed_at, archived) VALUES(?,?,?,?,?,?)",
            (f"pid-{name}", name, name, int(local), db.now(), int(archived)))
        self.con.commit()
        return f"pid-{name}"

    def rows(self) -> dict:
        _, payload = self.json_request(path="/api/projects")
        return {r["name"]: r for r in payload["projects"]}

    def test_the_listing_carries_local_and_source_rows_with_their_state(self) -> None:
        self.register("alpha")
        self.register("bravo", archived=True)
        self.register("charlie", local=False)
        rows = self.rows()
        self.assertEqual(set(rows), {"alpha", "bravo", "charlie"})
        self.assertFalse(rows["alpha"]["archived"])
        self.assertTrue(rows["bravo"]["archived"], "a parked row must still list")
        self.assertTrue(rows["alpha"]["local"])
        self.assertIsNone(rows["alpha"]["archived_override"],
                          "an untouched row still follows its source")
        self.assertFalse(rows["charlie"]["local"])

    def test_archiving_through_the_post_takes_the_project_off_the_picker(self) -> None:
        self.register("alpha")
        self.make_run(project_id="pid-alpha")
        _, before = self.json_request()
        self.assertEqual([p["project_id"] for p in before["projects"]],
                         ["pid-alpha"])
        status, said = self.json_request(method="POST", path="/api/projects",
                                         body={"project": "alpha",
                                               "archived": True})
        self.assertEqual(status, 200, said)
        self.assertTrue(said["archived"])
        _, after = self.json_request()
        self.assertEqual(after["projects"], [], "the picker kept a parked project")
        self.assertEqual(len(after["runs"]), 1, "parking hid a run")
        self.assertTrue(self.rows()["alpha"]["archived"])
        # and back again, through the same route
        self.json_request(method="POST", path="/api/projects",
                          body={"project": "pid-alpha", "archived": False})
        _, back = self.json_request()
        self.assertEqual([p["project_id"] for p in back["projects"]],
                         ["pid-alpha"])

    def test_a_source_backed_project_parks_through_the_same_route(self) -> None:
        """DESIGN §1: the board's control works on every row. The override is
        what the payload carries back, so the panel can still say WHICH rows
        are parked by their source rather than by the owner."""
        self.register("charlie", local=False)
        status, said = self.json_request(method="POST", path="/api/projects",
                                         body={"project": "charlie",
                                               "archived": True})
        self.assertEqual(status, 200, said)
        row = self.rows()["charlie"]
        self.assertTrue(row["archived"])
        self.assertEqual(1, row["archived_override"])

    def test_a_row_parked_by_its_source_carries_no_override(self) -> None:
        """How the panel words "parked at the source": the derived flag is on
        and nobody decided it here."""
        self.register("delta", local=False, archived=True)
        row = self.rows()["delta"]
        self.assertTrue(row["archived"])
        self.assertIsNone(row["archived_override"])

    def test_an_unknown_project_is_a_404_not_a_silent_success(self) -> None:
        status, text = self.request(method="POST", path="/api/projects",
                                    body={"project": "nope",
                                          "archived": True})
        self.assertEqual(status, 404)
        s = None
        self.assertIn("no project matches", text)

    def test_both_routes_normalize_to_themselves_and_are_the_humans(self) -> None:
        """No catch-all swallows them — neither ends in /stream nor looks like
        a profile — so they fall to auth.DEFAULT_LEVEL, and a run token that
        parked its own project would park itself mid-run."""
        for method in ("GET", "POST"):
            key, target = auth.route_key(method, mhttp.PROJECTS_ROUTE)
            self.assertEqual(key, f"{method} /api/projects")
            self.assertIsNone(target)
            self.assertNotIn(key, auth.ROUTES)
            self.assertEqual(auth.ROUTES.get(key, auth.DEFAULT_LEVEL),
                             auth.ONLY_HUMAN)
            self.assertIsNone(auth.permit(auth.Identity(auth.HUMAN), key))
            self.assertIn("human", auth.permit(auth.Identity(auth.RUN, 1), key))


class StopRunTests(unittest.TestCase):
    """Process ownership checks need no socket or server thread."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name).resolve()
        config_path = self.tmp_path / "config.toml"
        config_path.write_text("")
        self.env = mock.patch.dict(os.environ, {
            "ORCHESTRA_HOME": str(self.tmp_path / "home"),
            "ORCHESTRA_CONFIG": str(config_path)})
        self.env.start()
        self.con = db.connect()

    def tearDown(self) -> None:
        self.con.close()
        self.env.stop()
        self.tmp.cleanup()

    def make_run(self, **cols) -> int:
        fields = {"profile": "codex", "backend": "codex",
                  "requested_by": "human", "workdir": str(self.tmp_path),
                  "status": "running", "started_at": db.now()}
        fields.update(cols)
        names = ", ".join(fields)
        marks = ", ".join("?" * len(fields))
        run_id = int(self.con.execute(
            f"INSERT INTO runs({names}) VALUES({marks})",
            tuple(fields.values())).lastrowid)
        self.con.commit()
        return run_id

    def test_stop_marks_the_run_killed(self) -> None:
        run_id = self.make_run()
        payload = mhttp.stop_run(self.con, run_id)
        self.assertEqual(payload["status"], "killed")
        self.assertEqual(self.con.execute(
            "SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()["status"],
            "killed")
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM messages WHERE run_id=? AND kind='completion'",
            (run_id,)).fetchone()[0], 1)

        gone = self.make_run(pid=4241, pid_identity="gone-owner")
        with mock.patch.object(mhttp.proc, "signal_owned_group",
                               return_value=("gone", "process already gone")):
            payload = mhttp.stop_run(self.con, gone)
        self.assertFalse(payload["signalled"])
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM messages WHERE run_id=? AND kind='completion'",
            (gone,)).fetchone()[0], 1)

        reused = self.make_run(pid=4242, pid_identity="old-owner")
        with mock.patch.object(mhttp.proc, "signal_owned_group",
                               return_value=("refused", "identity changed")) as signal:
            payload = mhttp.stop_run(self.con, reused)
        signal.assert_called_once_with(4242, "old-owner", signal_module.SIGTERM)
        self.assertFalse(payload["signalled"])
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM messages WHERE run_id=? AND kind='completion'",
            (reused,)).fetchone()[0], 0)

        tell = self.make_run(pid=4243, pid_identity="tell-owner",
                             session_ref="session-1")
        with mock.patch.object(mhttp.proc, "signal_owned_group",
                               return_value=("refused", "identity changed")) as signal:
            payload = mhttp.tell_run(self.con, tell, "use the safe branch", now=True)
        signal.assert_called_once_with(4243, "tell-owner", signal_module.SIGTERM)
        self.assertTrue(payload["queued"])
        self.assertEqual(self.con.execute(
            "SELECT status FROM runs WHERE id=?", (tell,)).fetchone()["status"],
                         "interrupt")


class ActionTests(ServerCase):

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
        self.con.execute("UPDATE runs SET status='done' WHERE id=?", (run_id,))
        self.con.commit()
        status, payload = self.json_request(
            method="POST", path=f"/api/runs/{run_id}/tell",
            body={"text": "too late"})
        self.assertEqual(status, 400)
        self.assertIn("already done", payload["error"])
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM messages WHERE run_id=? AND kind='interrupt'",
            (run_id,)).fetchone()[0], 1)
        self.assertEqual(row["body"], "use the other branch")

    def test_tell_refuses_an_empty_message(self) -> None:
        run_id = self.make_run(session_ref="sess-1")
        self.assertEqual(self.json_request(
            method="POST", path=f"/api/runs/{run_id}/tell",
            body={"text": " "})[0], 400)

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
        git("checkout", "--quiet", "-b", "orchestra/run-diff")
        (root / "app.py").write_text("after-from-diff-pane\n")
        git("commit", "--quiet", "-am", "change")
        head = git("rev-parse", "HEAD")
        git("checkout", "--quiet", base_branch)
        git("branch", "-D", "orchestra/run-diff")
        run_id = self.make_run(
            status="done", finished_at=db.now(), workdir=str(root),
            branch="orchestra/run-diff", base_commit=base, checkpoint_commit=head)
        return run_id, head

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

    def test_a_paused_daemon_tick_still_runs_dependency_settlement(self) -> None:
        from orchestra import daemon, supervise
        mhttp.set_dispatch_paused(self.con, True)
        with mock.patch.object(supervise, "process_ready", return_value=[]) as ready:
            report = daemon.tick()
        self.assertTrue(report["paused"])
        self.assertEqual(report["released"], [])
        ready.assert_called_once()
        # Health names the admission state even though maintenance still ran.
        mhttp.record_health(report, con=self.con)
        self.assertEqual(mhttp.health(self.con)["outcome"], "paused")


class SseSeamTests(ServerCase):
    """The live streams (W-0165/W-0178): one run's trace, that run's raw
    harness output, the daemon's log, and the board's invalidation."""

    def a_run_with_a_trace(self, status="done") -> int:
        from orchestra import traces
        log = self.tmp_path / "run.jsonl"
        log.write_text(json.dumps(
            {"type": "assistant",
             "message": {"content": [{"type": "text", "text": "hello"}]}}) + "\n")
        run_id = self.make_run(status=status, backend="claude",
                               log_path=str(log), finished_at=db.now())
        traces.ingest(self.con, run_id, str(log), "claude")
        return run_id

    def daemon_log(self, *lines) -> None:
        from orchestra import paths
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

    def test_a_runs_raw_log_streams_and_resumes_on_a_file_offset(self) -> None:
        """The tail of the file itself, not the parser's reading of it."""
        log = self.tmp_path / "raw.log"
        log.write_text("harness: booting\n")
        run_id = self.make_run(status="running", backend="claude",
                               log_path=str(log))
        status, ctype, body = self.sse(f"/api/runs/{run_id}/log/stream",
                                       last_event_id=f"{log.name}@0",
                                       until="booting")
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", ctype)
        self.assertIn("event: raw", body)
        self.assertIn("harness: booting", body)
        cursor = [ln for ln in body.splitlines() if ln.startswith("id:")][0]
        cursor = cursor.split(":", 1)[1].strip()
        self.assertIn(f"{log.name}@", cursor)
        with open(log, "a") as handle:
            handle.write("harness: still going\n")
        body = self.sse(f"/api/runs/{run_id}/log/stream", last_event_id=cursor,
                        until="still going")[2]
        self.assertNotIn("booting", body)      # nothing before the cursor
        self.assertIn("still going", body)

    def test_a_terminal_runs_raw_log_ends_and_a_pruned_one_says_so(self) -> None:
        """Neither one may leave the tab on a stream that never closes."""
        log = self.tmp_path / "over.log"
        log.write_text("harness: bye\n")
        run_id = self.make_run(status="done", backend="claude",
                               log_path=str(log), finished_at=db.now())
        body = self.sse(f"/api/runs/{run_id}/log/stream",
                        last_event_id=f"{log.name}@0")[2]
        self.assertIn("harness: bye", body)
        self.assertIn("event: end", body)
        log.unlink()                            # what prune leaves behind
        body = self.sse(f"/api/runs/{run_id}/log/stream")[2]
        self.assertIn("event: pruned", body)
        self.assertNotIn("event: raw", body)

    def test_the_daemon_log_streams_and_resumes_on_a_file_offset(self) -> None:
        self.daemon_log("orchestra: swept")
        status, ctype, body = self.sse("/api/log/stream", until="orchestra: swept")
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", ctype)
        self.assertIn("orchestra: swept", body)
        cursor = [line for line in body.splitlines() if line.startswith("id:")][0]
        cursor = cursor.split(":", 1)[1].strip()
        self.assertIn("daemon.out.log@", cursor)
        # Resuming at that cursor replays nothing; the next line does arrive.
        self.daemon_log("orchestra: swept", "orchestra: swept again")
        body = self.sse("/api/log/stream", last_event_id=cursor,
                        until="swept again")[2]
        self.assertNotIn("orchestra: swept\"", body)
        self.assertIn("swept again", body)

    def test_the_board_stream_pushes_a_revision_so_the_page_stops_polling(self) -> None:
        self.make_run()  # any write to runs bumps the counter
        revision = db.board_revision(self.con)
        status, ctype, body = self.sse("/api/board/stream", until="event: board")
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", ctype)
        self.assertIn("retry: ", body)
        self.assertIn(f"id: {revision}", body)
        # An invalidation, not a state stream: the client refetches.
        self.assertNotIn("\"runs\"", body)

    def test_the_board_stream_resumes_on_last_event_id(self) -> None:
        self.make_run()
        revision = db.board_revision(self.con)
        # Caught up: the stream opens with the reconnect hint and waits.
        body = self.sse("/api/board/stream", last_event_id=str(revision),
                        until="retry:")[2]
        self.assertNotIn("event: board", body)
        # One more run, and the same cursor is stale — a frame arrives at once.
        self.make_run()
        body = self.sse("/api/board/stream", last_event_id=str(revision),
                        until="event: board")[2]
        self.assertIn(f"id: {db.board_revision(self.con)}", body)

    def test_a_board_change_no_run_write_makes_still_reaches_the_page(self) -> None:
        """Pause state, runway and health live outside runs. record_health
        bumps the counter once a tick rather than growing a trigger on meta,
        which db.connect writes on every connection."""
        before = db.board_revision(self.con)
        mhttp.record_health({"swept": []}, con=self.con)
        self.assertGreater(db.board_revision(self.con), before)

    def test_an_unknown_stream_path_is_a_named_501_not_a_silent_404(self) -> None:
        status, text = self.request(path="/api/nothing/stream")
        self.assertEqual(status, 501)
        self.assertIn("/api/nothing/stream", text)

    def test_the_seam_is_authenticated_before_it_is_called(self) -> None:
        with mock.patch.object(mhttp, "sse_stream") as seam:
            for path in ("/api/log/stream", "/api/runs/1/stream",
                         "/api/runs/1/log/stream"):
                with self.subTest(path=path):
                    self.assertEqual(self.request(path=path, key=None)[0], 401)
                    self.assertEqual(
                        self.request(path=path, key="not-the-key")[0], 401)
        seam.assert_not_called()


class RunListNumberingTests(ServerCase):
    """W-0304: a control turn shares the runs id space. The board densifies
    the worker list in the dashboard; the wire still carries the real id."""

    def test_a_turn_between_workers_leaves_real_ids_on_the_wire(self) -> None:
        first = self.make_run(status="done", finished_at=db.now())
        turn = self.make_run(status="done", finished_at=db.now(),
                             layer="observer", title="observer turn")
        second = self.make_run(status="done", finished_at=db.now())
        _, snap = self.json_request()
        self.assertEqual([r["id"] for r in snap["runs"]], [second, first])
        self.assertEqual(second, first + 2)
        # The gap is counted, not renumbered: one turn ran between them.
        self.assertEqual([r["turns_before"] for r in snap["runs"]], [1, 0])
        _, page = self.json_request(path="/api/turns")
        self.assertEqual([t["id"] for t in page["turns"]], [turn])
        self.assertIsNone(page["turns"][0]["turns_before"])
        self.assertNotIn("pinned_turns", snap,
                         "a turn is no longer a headline over the runs list")

    def test_the_runs_list_shows_the_project_s_own_run_number(self) -> None:
        """The number a human reads counts THIS PROJECT's runs. A single
        global sequence could not: control turns took 146 of the first 300
        numbers and five projects shared the rest, so PREX3's 105 runs were
        spread across ids 1 to 299 (2026-08-27)."""
        src = mhttp.DASHBOARD.read_text(encoding="utf-8")
        start = src.index("function renderRunList(s) {")
        body = src[start:src.index("\nfunction ", start + 1)]
        self.assertIn("runNo(r)", body)
        self.assertNotIn("machine_note", body,
                         "control-turn content lives on health, not per row")

    def test_the_number_is_dense_per_project_and_starts_at_one(self) -> None:
        mine = self.make_run(status="done", finished_at=db.now())
        self.con.execute("UPDATE runs SET project_id='other' WHERE id=?",
                         (self.make_run(status="done", finished_at=db.now()),))
        self.con.commit()
        second = self.make_run(status="done", finished_at=db.now())
        _, snap = self.json_request()
        numbered = {r["id"]: r["no"] for r in snap["runs"]}
        self.assertEqual(1, numbered[mine], "a project counts from one")
        self.assertEqual(2, numbered[second],
                         "another project's run does not take a number here")

    def test_a_run_carries_the_machines_latest_word_about_it(self) -> None:
        """The observer's note belongs ON the run it judged. A control turn's
        own row carries no link back to what it looked at; the observation
        does (2026-08-27)."""
        run_id = self.make_run(status="running")
        for reason in ("reading the files its mission names", "still on task"):
            self.con.execute(
                "INSERT INTO observations(run_id, layer, action, reason, "
                "created_at) VALUES(?,'observer','ok',?,?)",
                (run_id, reason, db.now()))
        self.con.commit()
        _, snap = self.json_request()
        note = [r for r in snap["runs"] if r["id"] == run_id][0]["machine_note"]
        self.assertEqual("ok: still on task", note, "the LATEST word, not the first")

    def test_the_runs_list_puts_children_under_their_parent_in_spawn_order(self) -> None:
        """2026-08-27: a retry sorted ABOVE the run it retried, and a
        mission's attempts scattered among unrelated ones, so the order of
        operations had to be reconstructed from ids. Families group; inside
        one the order is the order things happened."""
        src = mhttp.DASHBOARD.read_text(encoding="utf-8")
        start = src.index("function lineages(runs) {")
        body = src[start:src.index("\nfunction ", start + 1)]
        self.assertIn("a.id - b.id", body, "children ascend: spawn order")
        self.assertIn("newest(b) - newest(a)", body,
                      "families ordered by their newest member")
        self.assertIn("seen.has(r.id)", body, "a cycle must not hang the board")
        listing = src[src.index("function renderRunList(s) {"):]
        listing = listing[:listing.index("\nfunction ")]
        self.assertIn("lineages(runs)", listing)
        self.assertNotIn("turnItem(pinned)", listing,
                         "a turn is no longer pinned above the runs")
        self.assertNotIn("machine_note", listing,
                         "control-turn content lives on health, not per row")

    def test_no_surface_shows_the_row_id_to_a_human(self) -> None:
        """The row id is the internal key every foreign key points at, and
        nothing renders it. Mixing the two is what made "run 37" and "run
        64" the same run (2026-08-26); the cure is one number in front of a
        human, and this one now counts the project's runs."""
        src = mhttp.DASHBOARD.read_text(encoding="utf-8")
        shown = set(re.findall(r'"#"\s*\+\s*([A-Za-z_][\w.]*)', src))
        self.assertEqual({"hit.no"}, shown,
                         f"a run number came from something else: {sorted(shown)}")
        self.assertNotIn('"#" + r.id', src)
        self.assertNotIn('"#" + run.id', src)
        # The wire still carries the id — routes are addressed by it — but
        # every run also carries the number that is shown instead.
        _, snap = self.json_request()
        for run in snap["runs"]:
            self.assertIn("no", run)
            self.assertIsNotNone(run["no"])

class SeatsAndOutageTests(ServerCase):
    """The seats picker and the auth-outage feed (2026-08-25): an expired
    Claude OAuth ran the router and observer blind for hours — the judgment
    layers' profiles were invisible and nothing on the dashboard said so."""

    def test_seats_round_trip_preserves_the_config_comments(self) -> None:
        self.config_path.write_text(
            '# the probe fleet\n[profiles.probe]\nbackend = "codex"\n')
        status, data = self.json_request(path="/api/seats")
        self.assertEqual(status, 200)
        self.assertEqual(data["profiles"], ["probe"])
        self.assertEqual(data["seats"], {seat: None for seat in mhttp.SEATS})

        status, data = self.json_request(
            method="POST", path="/api/seats",
            body={"seat": "observer", "profile": "probe"})
        self.assertEqual(status, 200)
        self.assertEqual(data["seats"]["observer"], "probe")
        text = self.config_path.read_text()
        self.assertIn("# the probe fleet", text)
        self.assertIn('observer_profile = "probe"', text)

        status, data = self.json_request(
            method="POST", path="/api/seats",
            body={"seat": "observer", "profile": None})
        self.assertEqual(status, 200)
        self.assertIsNone(data["seats"]["observer"])
        self.assertNotIn('observer_profile = "probe"',
                         self.config_path.read_text())

    def test_a_seat_refuses_what_the_config_does_not_hold(self) -> None:
        # `verify` was a seat until the source automation left for the
        # bridge; an evicted seat refuses like any unknown one.
        for body in ({"seat": "conductor", "profile": "probe"},
                     {"seat": "verify", "profile": "probe"},
                     {"seat": "observer", "profile": "ghost"}):
            with self.subTest(body=body):
                status, _ = self.request(
                    method="POST", path="/api/seats", body=body)
                self.assertEqual(status, 400)

    def test_an_auth_outage_rides_the_snapshot_until_cleared(self) -> None:
        observer.note_auth_outage(
            self.con, "claude", "Failed to authenticate: OAuth expired")
        status, snap = self.json_request()
        self.assertEqual(status, 200)
        outages = snap["daemon"]["outages"]
        self.assertEqual([o["backend"] for o in outages], ["claude"])
        self.assertIn("at", outages[0])
        observer.clear_auth_outage(self.con, "claude")
        status, snap = self.json_request()
        self.assertEqual(snap["daemon"]["outages"], [])


class KeyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.toml"
        self.env = mock.patch.dict(os.environ, {"ORCHESTRA_CONFIG": str(self.path)})
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
    def test_bind_precedence_never_falls_back_to_every_interface(self) -> None:
        cases = (({"http": {"bind": "10.0.0.2"}}, "100.1.2.3", "10.0.0.2"),
                 ({}, "100.1.2.3", "100.1.2.3"),
                 ({}, None, "127.0.0.1"))
        for cfg, tailscale, expected in cases:
            with self.subTest(cfg=cfg, tailscale=tailscale), \
                    mock.patch.object(mhttp, "tailscale_address",
                                      return_value=tailscale):
                self.assertEqual(mhttp.bind_address(cfg), expected)


if __name__ == "__main__":
    unittest.main()


class TailnetAuthTests(unittest.TestCase):
    """DESIGN §3: a device on the owner's tailnet is the owner — the phone
    needs no key. Loopback and this machine's own tailscale address never
    count, because workers run on this machine."""

    def test_a_tailnet_peer_is_the_human_with_exclusions(self) -> None:
        cfg = {"http": {"key": "k"}}
        lookup = {"100.64.0.7": "me@github"}.get
        with mock.patch.object(mhttp, "tailscale_address",
                               return_value="100.64.0.1"):
            self.assertEqual(
                mhttp.tailnet_login_for("100.64.0.7", cfg, lookup),
                "me@github")
            self.assertIsNone(mhttp.tailnet_login_for("127.0.0.1", cfg, lookup))
            self.assertIsNone(mhttp.tailnet_login_for("100.64.0.1", cfg, lookup))
            self.assertIsNone(mhttp.tailnet_login_for("10.0.0.9", cfg, lookup))
            off = {"http": {"tailnet_auth": False}}
            self.assertIsNone(mhttp.tailnet_login_for("100.64.0.7", off, lookup))
            narrowed = {"http": {"tailnet_logins": ["other@x"]}}
            self.assertIsNone(
                mhttp.tailnet_login_for("100.64.0.7", narrowed, lookup))
            listed = {"http": {"tailnet_logins": ["me@github"]}}
            self.assertEqual(
                mhttp.tailnet_login_for("100.64.0.7", listed, lookup),
                "me@github")
