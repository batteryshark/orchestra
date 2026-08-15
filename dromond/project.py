"""Resolve a directory to a Work project (DESIGN §2).

The CLI no longer walks up for a state directory — there is none. It asks
Work which projects exist and matches the deepest one containing the
current directory. Everything downstream keys on Work's immutable
``projectId``, so renaming a project folder loses no settings.

The mapping is cached in the central database: an offline CLI still
resolves, and only a miss costs a refresh.
"""
import os
from pathlib import Path

from dromond import paths, work_client

MISS_HINT = """\
dromond: {path} is not inside a known Work project.
Create the project in Work (a `.work/project.json` marker), or point
[work] api_url at the right server, then retry."""


class Project:
    """One cached (path -> projectId) mapping. ``path`` is a lookup key only."""

    __slots__ = ("project_id", "path", "work_id", "name")

    def __init__(self, project_id: str, path: Path, work_id: str | None,
                 name: str | None):
        self.project_id = project_id
        self.path = path
        self.work_id = work_id
        self.name = name

    @property
    def slug(self) -> str:
        return self.work_id or self.name or self.path.name

    def __repr__(self) -> str:
        return f"Project({self.project_id} @ {self.path})"


def _row(row) -> Project | None:
    if row is None:
        return None
    return Project(row["project_id"], Path(row["path"]), row["work_id"], row["name"])


# --- cache ------------------------------------------------------------------

def remember(con, workspace_root: str, entries: list) -> int:
    """Cache Work's project list. Relative paths resolve against the workspace
    root; each aliasPath gets its own row so it resolves too."""
    from dromond import db  # local: db imports paths, not project

    root = Path(workspace_root).expanduser()
    ts, count = db.now(), 0
    for entry in entries:
        project_id = entry.get("projectId")
        if not project_id:
            continue  # a project Work has not stamped yet is not addressable
        for rel in [entry.get("path"), *(entry.get("aliasPaths") or [])]:
            if not rel:
                continue
            p = Path(rel).expanduser()
            absolute = p if p.is_absolute() else root / p
            con.execute(
                "INSERT OR REPLACE INTO projects(path, project_id, work_id, name, "
                "refreshed_at) VALUES(?,?,?,?,?)",
                (str(absolute), project_id, entry.get("id"), entry.get("name"), ts))
            count += 1
    con.commit()
    return count


def refresh(con, cfg: dict) -> int:
    """Re-read the project list from Work. Returns rows cached (0 when Work
    is off or unreachable — an offline miss must not crash the CLI)."""
    client = work_client.from_cfg(cfg)
    if client is None:
        return 0
    root = client.workspace_root()
    entries = client.projects()
    if root is None or entries is None:
        return 0
    return remember(con, root, entries)


# --- lookups ----------------------------------------------------------------

def _deepest(con, start: Path) -> Project | None:
    # ponytail: full scan of a table with tens of rows; index it if a
    # workspace ever holds thousands of projects.
    best = None
    for row in con.execute("SELECT * FROM projects"):
        p = Path(row["path"])
        if start == p or p in start.parents:
            if best is None or len(row["path"]) > len(best["path"]):
                best = row
    return _row(best)


def start_dir(explicit: str | None = None) -> Path:
    raw = explicit or paths.env("DROMOND_ROOT") or Path.cwd()
    return Path(raw).expanduser().resolve()


def resolve(con, cfg: dict, explicit: str | None = None) -> Project:
    """The project containing this directory. Refreshes once on a miss."""
    start = start_dir(explicit)
    hit = _deepest(con, start)
    if hit is None and refresh(con, cfg):
        hit = _deepest(con, start)
    if hit is None:
        raise SystemExit(MISS_HINT.format(path=start))
    return hit


def try_resolve(con, cfg: dict, explicit: str | None = None) -> Project | None:
    try:
        return resolve(con, cfg, explicit)
    except SystemExit:
        return None


def by_id(con, project_id: str | None) -> Project | None:
    if not project_id:
        return None
    return _row(con.execute(
        "SELECT * FROM projects WHERE project_id=? ORDER BY LENGTH(path) LIMIT 1",
        (project_id,)).fetchone())


def by_work_path(con, project_path: str | None) -> Project | None:
    """Resolve the ``projectPath`` a Work item carries (its Work project id,
    or an absolute local path)."""
    if not project_path:
        return None
    row = con.execute("SELECT * FROM projects WHERE work_id=? ORDER BY LENGTH(path) "
                      "LIMIT 1", (project_path,)).fetchone()
    if row is None:
        row = con.execute("SELECT * FROM projects WHERE path=?",
                          (str(Path(project_path).expanduser()),)).fetchone()
    return _row(row)


def root_for(con, run) -> Path:
    """A run's project checkout. Falls back to its recorded workdir so a run
    whose project left Work is still supervisable."""
    hit = by_id(con, run["project_id"])
    return hit.path if hit else Path(run["workdir"])


def dir_key_for(con, run) -> str:
    """The worktree directory key: the immutable projectId. A run whose project
    left Work falls back to its workdir name so it stays supervisable."""
    return run["project_id"] or Path(run["workdir"]).name
