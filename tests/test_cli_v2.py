import argparse
import os
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from orchestra import brief, cli, client


class CLITests(unittest.TestCase):
    def test_worker_run_token_wins_over_inherited_operator_token(self):
        with patch.dict(os.environ, {
            "ORCHESTRA_TOKEN": "operator",
            "ORCHESTRA_RUN_TOKEN": "run-scoped",
        }, clear=False):
            self.assertEqual(client.load_token("http://fleet"), "run-scoped")

    def test_internal_supervisor_command_reaches_execution_entry_point(self):
        args = cli.build_parser().parse_args(
            ["_supervise", "42", "--root", "/tmp/scope"])
        with patch("orchestra.supervise.supervise", return_value=7) as supervise:
            self.assertEqual(args.func(args), 7)
        supervise.assert_called_once_with(Path("/tmp/scope"), 42)

    def test_internal_observer_command_reaches_isolated_entry_point(self):
        args = cli.build_parser().parse_args(["_observe", "17"])
        with patch("orchestra.daemon.observe", return_value=0,
                   create=True) as observe:
            self.assertEqual(args.func(args), 0)
        observe.assert_called_once_with(17)

    def test_retry_and_continue_send_explicit_overrides(self):
        api_client = Mock()
        api_client.post.return_value = {"data": {"created": True}}
        with patch("orchestra.cli._client", return_value=api_client):
            retry = cli.build_parser().parse_args(
                ["retry", "9", "--profile", "cheap", "--context", "try again"])
            retry.func(retry)
            continuation = cli.build_parser().parse_args(
                ["continue", "9", "more", "work", "--profile", "strong"])
            continuation.func(continuation)
        retry_body = api_client.post.call_args_list[0].args[1]
        self.assertEqual(retry_body["profile"], "cheap")
        self.assertEqual(retry_body["context"], "try again")
        continue_body = api_client.post.call_args_list[1].args[1]
        self.assertEqual(continue_body["context"], "more work")
        self.assertEqual(continue_body["profile"], "strong")

    def test_pair_accepts_a_code_without_redundant_pairing_id(self):
        api_client = Mock(url="http://fleet")
        api_client.post.return_value = {"data": {
            "token": "token", "device": {"label": "Phone"}}}
        args = argparse.Namespace(pairing="op_code", url="http://fleet",
                                  name="Phone")
        with patch("orchestra.cli.client.Client", return_value=api_client), \
                patch("orchestra.cli.client.save_token"):
            cli.cmd_pair(args)
        self.assertEqual(api_client.post.call_args.args[1]["pairing_id"], "")
        self.assertEqual(api_client.post.call_args.args[1]["code"], "op_code")

    def test_worker_brief_names_the_real_artifact_command(self):
        self.assertIn("`orchestra artifact PATH`", brief.PROTOCOL)
        self.assertNotIn("artifact publish", brief.PROTOCOL)

    def test_dispatch_alias_and_plain_group_name_are_ergonomic(self):
        dispatched = cli.build_parser().parse_args([
            "dispatch", "--profile", "quick", "--cwd", "/tmp", "do", "it"])
        self.assertIs(dispatched.func, cli.cmd_run)
        self.assertEqual(dispatched.cwd, "/tmp")
        group = cli.build_parser().parse_args(
            ["groups", "create", "Long lived research"])
        api_client = Mock()
        api_client.post.return_value = {"data": {"group": {"id": "g"}}}
        with patch("orchestra.cli._client", return_value=api_client):
            group.func(group)
        self.assertEqual(api_client.post.call_args.args[1]["name"],
                         "Long lived research")

    def test_profile_discovery_runs_on_the_configured_orchestra_host(self):
        api_client = Mock()
        api_client.get.return_value = {"data": {
            "codex": {"data": ["gpt"], "error": None},
            "local": {"data": [{"id": "local", "source": "ollama"}],
                      "error": None},
        }}
        args = cli.build_parser().parse_args(["profiles", "discover", "--local"])
        with patch("orchestra.cli._client", return_value=api_client), \
                patch("orchestra.cli._print") as output:
            args.func(args)
        api_client.get.assert_called_once_with(
            "/api/v2/profile-discovery", local=True)
        output.assert_called_once_with({
            "codex": {"data": ["gpt"], "error": None},
            "local": {"data": [{"id": "local", "source": "ollama"}],
                      "error": None},
        })

    def test_managed_collections_and_inbox_have_useful_default_views(self):
        for command, function, action in (
                ("groups", cli.cmd_resource, "list"),
                ("profiles", cli.cmd_resource, "list"),
                ("inbox", cli.cmd_inbox, "list"),
                ("runway", cli.cmd_runway, "list"),
                ("devices", cli.cmd_devices, "list"),
                ("service-tokens", cli.cmd_service_tokens, "list"),
                ("storage", cli.cmd_storage, "report"),
                ("settings", cli.cmd_settings, "list"),
                ("observer", cli.cmd_observer_settings, "show")):
            with self.subTest(command=command):
                args = cli.build_parser().parse_args([command])
                self.assertIs(args.func, function)
                self.assertEqual(args.default_action, action)

    def test_outbox_forwards_fleet_message_filters(self):
        api_client = Mock()
        api_client.get.return_value = {"data": {"items": [],
                                                       "next_cursor": None}}
        args = cli.build_parser().parse_args([
            "outbox", "--direction", "outbound", "--status", "delivered",
            "--kind", "question", "--run-id", "8"])
        with patch("orchestra.cli._client", return_value=api_client):
            args.func(args)
        self.assertEqual(api_client.get.call_args.args[0], "/api/v2/outbox")
        self.assertEqual(api_client.get.call_args.kwargs["direction"], "outbound")
        self.assertEqual(api_client.get.call_args.kwargs["run_id"], 8)

    def test_inbox_forwards_cursor_for_complete_history_navigation(self):
        api_client = Mock()
        api_client.get.return_value = {"data": {"items": [],
                                                       "next_cursor": None}}
        args = cli.build_parser().parse_args([
            "inbox", "list", "--state", "resolved", "--kind", "decision",
            "--limit", "25", "--cursor", "opaque-page"])
        with patch("orchestra.cli._client", return_value=api_client):
            args.func(args)
        self.assertEqual(api_client.get.call_args.args[0], "/api/v2/inbox")
        self.assertEqual(api_client.get.call_args.kwargs, {
            "state": "resolved", "kind": "decision", "limit": 25,
            "cursor": "opaque-page",
        })

    def test_settings_and_observer_updates_are_http_mutations(self):
        api_client = Mock()
        api_client.patch.return_value = {"data": {"updated": True}}
        setting = cli.build_parser().parse_args([
            "settings", "set", "max_active_runs", "12"])
        observer = cli.build_parser().parse_args([
            "observer", "update", "enabled=true", "profile=watcher"])
        with patch("orchestra.cli._client", return_value=api_client):
            setting.func(setting)
            observer.func(observer)
        self.assertEqual(api_client.patch.call_args_list[0].args[0],
                         "/api/v2/settings")
        self.assertEqual(api_client.patch.call_args_list[0].args[1]["value"], 12)
        self.assertEqual(api_client.patch.call_args_list[1].args[0],
                         "/api/v2/observer")
        self.assertTrue(api_client.patch.call_args_list[1].args[1]["enabled"])


if __name__ == "__main__":
    unittest.main()
