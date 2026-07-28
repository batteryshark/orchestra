import unittest
from argparse import Namespace
from http.server import ThreadingHTTPServer
from unittest import mock

from orchestra_cli import cli, ui


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


if __name__ == "__main__":
    unittest.main()
