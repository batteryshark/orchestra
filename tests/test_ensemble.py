from __future__ import annotations

import contextlib
import copy
import io
import json
import sqlite3
import tempfile
import tomllib
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from orchestra_cli import cli, config, db, ensemble, paths


def default_config() -> dict:
    return tomllib.loads(config.DEFAULT_CONFIG)


def dispatch_args(target: str) -> Namespace:
    return Namespace(
        mission=["boundary test"],
        brief_file=None,
        work=None,
        team=None,
        to=[target],
        title=None,
        context=None,
        worktree=False,
        sync=False,
        no_quota_warn=True,
        as_=None,
    )


class DefaultRosterTests(unittest.TestCase):
    def test_default_roster_does_not_include_ensemble(self) -> None:
        self.assertNotIn("ensemble", default_config()["agents"])


class PluginDetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "opencode.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_plugins(self, entries: list[str]) -> None:
        self.path.write_text(json.dumps({"plugin": entries}))

    def test_exact_pinned_plugin_entry_is_detected(self) -> None:
        self.write_plugins([ensemble.TESTED_PLUGIN_SPEC])
        status = ensemble.plugin_status(self.path)
        self.assertTrue(status.configured)
        self.assertEqual(status.detail, ensemble.TESTED_PLUGIN_SPEC)

    def test_similar_plugin_name_does_not_false_positive(self) -> None:
        self.write_plugins(["some-ensemble-helper", "@hueyexe/opencode-ensemble-extra@1.0.0"])
        self.assertFalse(ensemble.plugin_status(self.path).configured)

    def test_malformed_config_is_reported_as_unconfigured(self) -> None:
        self.path.write_text("{not-json")
        status = ensemble.plugin_status(self.path)
        self.assertFalse(status.configured)
        self.assertIn("could not parse", status.detail)


class DoctorScopeTests(unittest.TestCase):
    def run_doctor(self, cfg: dict, status: ensemble.PluginStatus | None = None) -> str:
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(cli, "_maybe_root", return_value=None))
            stack.enter_context(mock.patch.object(cli.config, "load", return_value=cfg))
            stack.enter_context(mock.patch.object(cli.shutil, "which", return_value=None))
            if status is not None:
                stack.enter_context(mock.patch.object(
                    cli.ensemble, "plugin_status", return_value=status,
                ))
            stack.enter_context(contextlib.redirect_stdout(out))
            cli.cmd_doctor(Namespace())
        return out.getvalue()

    def test_doctor_skips_plugin_check_without_ensemble_agent(self) -> None:
        output = self.run_doctor(default_config())
        self.assertNotIn("opencode-ensemble plugin", output)

    def test_doctor_reports_missing_plugin_for_opted_in_agent(self) -> None:
        cfg = default_config()
        cfg["agents"]["ensemble"] = {
            "backend": "opencode", "model": "provider/model", "ensemble": True,
        }
        output = self.run_doctor(cfg, ensemble.PluginStatus(False, "not configured"))
        self.assertIn("optional opencode-ensemble plugin: MISSING", output)


class DispatchBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".orchestra").mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_ordinary_opencode_dispatch_does_not_require_plugin(self) -> None:
        cfg = default_config()
        with mock.patch.object(cli.paths, "find_root", return_value=self.root), \
                mock.patch.object(cli.config, "load", return_value=cfg), \
                mock.patch.object(cli, "_spawn_supervisor") as spawn:
            cli.cmd_dispatch(dispatch_args("minimax"))
        spawn.assert_called_once()
        con = db.connect(self.root)
        try:
            count = con.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"]
        finally:
            con.close()
        self.assertEqual(count, 1)

    def test_missing_plugin_fails_before_run_database_or_supervisor(self) -> None:
        cfg = copy.deepcopy(default_config())
        cfg["agents"]["ensemble"] = {
            "backend": "opencode",
            "model": "provider/model",
            "ensemble": True,
            "role": "optional lead",
        }
        missing = self.root / "missing-opencode.json"
        with mock.patch.object(cli.paths, "find_root", return_value=self.root), \
                mock.patch.object(cli.config, "load", return_value=cfg), \
                mock.patch.object(cli.ensemble, "OPENCODE_CONFIG", missing), \
                mock.patch.object(cli, "_spawn_supervisor") as spawn:
            with self.assertRaises(SystemExit) as raised:
                cli.cmd_dispatch(dispatch_args("ensemble"))
        self.assertIn(ensemble.TESTED_PLUGIN_SPEC, str(raised.exception))
        self.assertFalse(paths.db_path(self.root).exists())
        self.assertFalse((self.root / ".orchestra" / "logs").exists())
        self.assertFalse((self.root / ".orchestra" / "briefs").exists())
        spawn.assert_not_called()


class StoreBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_missing_database_degrades_to_empty_state(self) -> None:
        store = ensemble.Store(self.root / "missing.db")
        self.assertEqual(store.teams(self.root), [])
        self.assertEqual(store.messages("team-1"), [])

    def test_incompatible_database_degrades_to_empty_state(self) -> None:
        path = self.root / "incompatible.db"
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE unrelated(id TEXT)")
        con.commit()
        con.close()
        store = ensemble.Store(path)
        self.assertEqual(store.teams(self.root), [])
        self.assertEqual(store.messages("team-1"), [])

    def test_compatible_database_is_serialized_through_adapter(self) -> None:
        path = self.root / "ensemble.db"
        con = sqlite3.connect(path)
        con.executescript("""
            CREATE TABLE team(
                id TEXT, project_id TEXT, name TEXT, status TEXT,
                lead_session_id TEXT, time_created INTEGER
            );
            CREATE TABLE team_member(
                team_id TEXT, name TEXT, model TEXT, status TEXT,
                execution_status TEXT, session_id TEXT
            );
            CREATE TABLE team_task(
                team_id TEXT, content TEXT, status TEXT, priority TEXT,
                assignee TEXT, time_created INTEGER
            );
            CREATE TABLE team_message(
                team_id TEXT, from_name TEXT, to_name TEXT, content TEXT,
                time_created INTEGER
            );
        """)
        con.execute("INSERT INTO team VALUES(?,?,?,?,?,?)", (
            "team-1", str(self.root), "review", "active", "lead-session", 1,
        ))
        con.execute("INSERT INTO team_member VALUES(?,?,?,?,?,?)", (
            "team-1", "scout", "provider/model", "busy", "running", "session-1",
        ))
        con.execute("INSERT INTO team_task VALUES(?,?,?,?,?,?)", (
            "team-1", "inspect", "in_progress", "high", "scout", 1,
        ))
        con.execute("INSERT INTO team_message VALUES(?,?,?,?,?)", (
            "team-1", "scout", "lead", "found it", 1,
        ))
        con.commit()
        con.close()

        store = ensemble.Store(path)
        teams = store.teams(self.root)
        self.assertEqual(teams[0]["name"], "review")
        self.assertEqual(teams[0]["members"][0]["name"], "scout")
        self.assertEqual(teams[0]["tasks"][0]["content"], "inspect")
        self.assertEqual(store.messages("team-1")[0]["content"], "found it")


if __name__ == "__main__":
    unittest.main()
