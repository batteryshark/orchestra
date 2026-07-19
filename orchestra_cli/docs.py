"""Project documentation templates written by :command:`orchestra init`."""

from importlib.resources import files


PLAYBOOK_MANAGED_START = "<!-- orchestra:managed:start -->"
PLAYBOOK_MANAGED_END = "<!-- orchestra:managed:end -->"


class PlaybookRefreshError(ValueError):
    """Raised when an existing playbook cannot be refreshed without data loss."""


def playbook_template() -> str:
    """Load the canonical playbook shipped in the installed package."""
    return files("orchestra_cli").joinpath("templates", "ORCHESTRA.md").read_text(
        encoding="utf-8"
    )


def _managed_bounds(text: str) -> tuple[int, int]:
    if text.count(PLAYBOOK_MANAGED_START) != 1 or text.count(PLAYBOOK_MANAGED_END) != 1:
        raise PlaybookRefreshError(
            "existing ORCHESTRA.md has no unique managed section; preserve its project "
            "doctrine and migrate it manually into a newly generated playbook"
        )
    start = text.index(PLAYBOOK_MANAGED_START)
    end = text.index(PLAYBOOK_MANAGED_END) + len(PLAYBOOK_MANAGED_END)
    if end <= start:
        raise PlaybookRefreshError("existing ORCHESTRA.md has malformed managed markers")
    return start, end


def refresh_playbook(existing: str) -> str:
    """Replace only Orchestra's managed section in an existing playbook.

    Text before and after the markers belongs to the project and is preserved
    byte-for-byte. Unmarked legacy files are deliberately rejected because
    Orchestra cannot distinguish generic text from project doctrine safely.
    """
    current = playbook_template()
    current_start, current_end = _managed_bounds(current)
    existing_start, existing_end = _managed_bounds(existing)
    return (
        existing[:existing_start]
        + current[current_start:current_end]
        + existing[existing_end:]
    )


POINTER = """
<!-- orchestra -->
## Multi-agent orchestration

This project uses the `orchestra` CLI. If you are running interactively here,
read `ORCHESTRA.md` before substantial work. Use the project's durable tracker
for plans and decisions, delegate bounded work with `orchestra dispatch`, and
verify worker evidence before accepting a handoff.
"""

PROJECT_CONFIG_STUB = """\
# Project-level orchestra overrides (merged over ~/.config/orchestra/config.toml).
# [agents.myagent]
# backend = "opencode"
# model = "provider/model"
# role = "..."

[settings]
# timeout = 36000        # hard cap for runaway workers (10 hours)
# stall_timeout = 1800   # no worker output before termination; 0 disables
"""

STATE_GITIGNORE = """\
logs/
worktrees/
briefs/
checkpoints/
*.db
*.db-shm
*.db-wal
"""
