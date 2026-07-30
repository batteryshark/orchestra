import http.client
import json
import tempfile
import threading
import unittest
from argparse import Namespace
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from orchestra_cli import cli, db, ui


class DashboardHTTPServerErrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = object.__new__(ui._DashboardHTTPServer)
        self.request = object()
        self.address = ("100.103.106.14", 51465)

    def test_expected_client_disconnect_is_quiet(self) -> None:
        with mock.patch.object(ThreadingHTTPServer, "handle_error") as fallback:
            try:
                raise BrokenPipeError(32, "Broken pipe")
            except BrokenPipeError:
                self.server.handle_error(self.request, self.address)

        fallback.assert_not_called()

    def test_unexpected_handler_error_uses_default_reporting(self) -> None:
        with mock.patch.object(ThreadingHTTPServer, "handle_error") as fallback:
            try:
                raise RuntimeError("database failed")
            except RuntimeError:
                self.server.handle_error(self.request, self.address)

        fallback.assert_called_once_with(self.request, self.address)


class UIStartupTests(unittest.TestCase):
    def test_cli_starts_without_a_project_root(self) -> None:
        args = Namespace(
            port=None, no_open=True, host=None, tailscale=False,
        )
        with mock.patch.object(cli, "_maybe_root", return_value=None), \
             mock.patch.object(ui, "serve") as serve:
            cli.cmd_ui(args)

        serve.assert_called_once_with(
            None,
            port=None,
            open_browser=False,
            host=None,
            tailscale_mode=False,
        )


class DashboardServeLifecycleTests(unittest.TestCase):
    def test_restart_execs_same_port_and_closes_listener_first(self) -> None:
        class FakeServer:
            created = []

            def __init__(self, address, _handler) -> None:
                self.server_address = address
                self.restart_requested = threading.Event()
                self.closed = False
                self.created.append(self)

            def serve_forever(self) -> None:
                self.restart_requested.set()

            def server_close(self) -> None:
                self.closed = True

        with mock.patch.object(ui, "_DashboardHTTPServer", FakeServer), \
                mock.patch.object(ui, "_exec_dashboard_restart") as restart:
            port = ui.serve(None, port=4764, open_browser=False)

        self.assertEqual(port, 4764)
        self.assertEqual(
            [server.server_address for server in FakeServer.created],
            [("127.0.0.1", 4764)],
        )
        self.assertTrue(all(server.closed for server in FakeServer.created))
        restart.assert_called_once_with(
            port=4764,
            host=None,
            tailscale_mode=False,
        )

    def test_restart_command_pins_url_and_does_not_reopen_browser(self) -> None:
        with mock.patch.object(ui.os, "execv") as execv:
            ui._exec_dashboard_restart(
                port=51234,
                host=None,
                tailscale_mode=True,
            )

        executable, command = execv.call_args.args
        self.assertEqual(executable, ui.sys.executable)
        self.assertEqual(command[:4], [
            ui.sys.executable,
            "-m",
            "orchestra_cli",
            "ui",
        ])
        self.assertIn("--port", command)
        self.assertEqual(command[command.index("--port") + 1], "51234")
        self.assertIn("--tailscale", command)
        self.assertIn("--no-open", command)


class DashboardRestartRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".orchestra").mkdir()
        db.connect(self.root).close()
        self.server = ui._DashboardHTTPServer(
            ("127.0.0.1", 0),
            ui.make_handler(self.root),
        )
        self.port = self.server.server_port
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()

    def tearDown(self) -> None:
        if self.thread.is_alive():
            self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=4)
        encoded = json.dumps(body or {}) if method == "POST" else None
        headers = {"Content-Type": "application/json"} if encoded is not None else {}
        try:
            conn.request(method, path, body=encoded, headers=headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read().decode("utf-8"))
        finally:
            conn.close()

    def test_restart_route_stops_listener_with_old_instance_id(self) -> None:
        status, before = self.request("GET", "/api/server/status")
        self.assertEqual(status, 200)
        self.assertTrue(before["restartable"])
        self.assertEqual(before["instance_id"], self.server.instance_id)

        status, accepted = self.request("POST", "/api/server/restart", {})
        self.assertEqual(status, 202)
        self.assertTrue(accepted["restarting"])
        self.assertEqual(accepted["instance_id"], before["instance_id"])

        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())
        self.assertTrue(self.server.restart_requested.is_set())


if __name__ == "__main__":
    unittest.main()
