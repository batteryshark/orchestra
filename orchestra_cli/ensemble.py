"""Optional integration boundary for the OpenCode Ensemble plugin.

Orchestra does not require Ensemble for ordinary OpenCode workers.  This
module owns the two places where the optional plugin crosses into Orchestra:
detecting its explicit OpenCode configuration and reading its SQLite state for
the dashboard.  Callers never depend on the plugin's Python/TypeScript code.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path


PLUGIN_NAME = "@hueyexe/opencode-ensemble"
TESTED_PLUGIN_SPEC = f"{PLUGIN_NAME}@0.16.0"
OPENCODE_CONFIG = Path("~/.config/opencode/opencode.json").expanduser()
ENSEMBLE_DB = Path("~/.config/opencode/ensemble.db").expanduser()


@dataclass(frozen=True)
class PluginStatus:
    configured: bool
    detail: str


def configured_agents(cfg: dict) -> list[str]:
    """Return roster names that explicitly opt into Ensemble behavior."""
    agents = cfg.get("agents", {})
    if not isinstance(agents, dict):
        return []
    return [
        name for name, agent in agents.items()
        if isinstance(name, str) and isinstance(agent, dict) and agent.get("ensemble") is True
    ]


def plugin_status(path: Path | None = None) -> PluginStatus:
    """Inspect OpenCode's structured plugin list for an exact Ensemble entry."""
    config_path = path or OPENCODE_CONFIG
    if not config_path.is_file():
        return PluginStatus(False, f"no OpenCode config at {config_path}")
    try:
        raw = json.loads(config_path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return PluginStatus(False, f"could not parse {config_path}: {exc}")
    plugins = raw.get("plugin") if isinstance(raw, dict) else None
    if not isinstance(plugins, list):
        return PluginStatus(False, f"no plugin list in {config_path}")
    for entry in plugins:
        if isinstance(entry, str) and (entry == PLUGIN_NAME or entry.startswith(PLUGIN_NAME + "@")):
            return PluginStatus(True, entry)
    return PluginStatus(False, f"{PLUGIN_NAME} is not configured in {config_path}")


def require_plugin(agent_names: list[str] | tuple[str, ...], path: Path | None = None) -> None:
    """Fail before dispatch state is created when an Ensemble target is unusable."""
    status = plugin_status(path)
    if status.configured:
        return
    names = ", ".join(agent_names)
    raise SystemExit(
        f"orchestra: Ensemble agent(s) {names} require the optional OpenCode plugin.\n"
        f"Add {TESTED_PLUGIN_SPEC!r} to the 'plugin' list in {path or OPENCODE_CONFIG}, "
        "restart OpenCode, then run `orchestra doctor`.\n"
        f"Detected: {status.detail}"
    )


class Store:
    """Best-effort, read-only view of Ensemble's external SQLite schema."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or ENSEMBLE_DB

    def _connect(self) -> sqlite3.Connection | None:
        if not self.path.is_file():
            return None
        try:
            con = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            return con
        except (OSError, sqlite3.Error):
            return None

    def teams(self, root: Path) -> list[dict]:
        con = self._connect()
        if con is None:
            return []
        try:
            teams = []
            for team in con.execute(
                "SELECT * FROM team WHERE project_id=? "
                "ORDER BY time_created DESC LIMIT 8",
                (str(root),),
            ):
                members = [dict(row) for row in con.execute(
                    "SELECT name, model, status, execution_status, session_id "
                    "FROM team_member WHERE team_id=?",
                    (team["id"],),
                )]
                tasks = [dict(row) for row in con.execute(
                    "SELECT content, status, priority, assignee FROM team_task "
                    "WHERE team_id=? ORDER BY time_created",
                    (team["id"],),
                )]
                teams.append({
                    "id": team["id"],
                    "name": team["name"],
                    "status": team["status"],
                    "lead_session": team["lead_session_id"],
                    "members": members,
                    "tasks": tasks,
                })
            return teams
        except (sqlite3.Error, IndexError, KeyError):
            return []
        finally:
            con.close()

    def messages(self, team_id: str) -> list[dict]:
        con = self._connect()
        if con is None:
            return []
        try:
            return [dict(row) for row in con.execute(
                "SELECT from_name, to_name, content, time_created FROM team_message "
                "WHERE team_id=? ORDER BY time_created LIMIT 200",
                (team_id,),
            )]
        except sqlite3.Error:
            return []
        finally:
            con.close()


store = Store()
