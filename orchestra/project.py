"""Resolve a directory through Orchestra's central project registry.

Projects may be adopted locally or cached from Work. The deepest registered
path containing the current directory wins. Everything downstream uses the
stored project id, so renaming a Work-backed project folder loses no settings.
"""
import os
import uuid
from pathlib import Path

from orchestra import paths, work_client

MISS_HINT = """\
orchestra: {path} is not inside a registered project.
Run `orchestra project add .`, or enable [work] and point api_url at a Work
server that knows this directory, then retry."""


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
    from orchestra import db  # local: db imports paths, not project

    root = Path(workspace_root).expanduser()
    ts, count = db.now(), 0
    seen: list = []
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
            seen.append(str(absolute))
            count += 1
    # A Work-sourced row whose path Work no longer names is stale — a moved or
    # deleted project, or a worktree copy that briefly won discovery (I-0013's
    # ghost lived here long after Work was fixed). Prune those; locally
    # adopted rows (work_id NULL) are never Work's to delete.
    if seen:
        marks = ",".join("?" * len(seen))
        con.execute(
            f"DELETE FROM projects WHERE work_id IS NOT NULL AND path NOT IN ({marks})",
            seen)
    con.commit()
    return count


def adopt(con, root: Path, name: str | None = None) -> "Project":
    """Register a project Orchestra owns itself, with no Work behind it.

    Work is the system of record when it is there, and ``remember`` caches its
    list. But the projects table was the ONLY way to address a directory, and
    the only writer was Work — so without it ``dispatch`` could not resolve
    anything and the tool did not run standalone at all.

    A locally adopted project is the same row with ``work_id`` NULL. Nothing
    downstream cares: a run resolves by path and carries ``project_id``, and
    writeback is skipped for a run with no Work item anyway. ``remember`` only
    ever inserts or replaces the paths Work names, and the stale-row prune
    skips work_id-NULL rows, so a later refresh cannot delete this row — and
    if Work is ever told about the same directory, its entry replaces this
    one, which is the right precedence.
    """
    from orchestra import db  # local: db imports paths, not project

    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"orchestra: {root} is not a directory")
    existing = _row(con.execute(
        "SELECT * FROM projects WHERE path=?", (str(root),)).fetchone())
    if existing is not None:
        return existing
    con.execute(
        "INSERT INTO projects(path, project_id, work_id, name, refreshed_at) "
        "VALUES(?,?,NULL,?,?)",
        (str(root), str(uuid.uuid4()), name or root.name, db.now()))
    con.commit()
    return _row(con.execute(
        "SELECT * FROM projects WHERE path=?", (str(root),)).fetchone())


def forget(con, root: Path) -> bool:
    """Drop a locally adopted project. Refuses one that came from Work, since
    the next refresh would put it back and the removal would look broken."""
    root = Path(root).expanduser().resolve()
    row = con.execute("SELECT work_id FROM projects WHERE path=?",
                      (str(root),)).fetchone()
    if row is None:
        return False
    if row["work_id"]:
        raise SystemExit(
            f"orchestra: {root} comes from Work; remove it there, not here")
    con.execute("DELETE FROM projects WHERE path=?", (str(root),))
    con.commit()
    return True


def all_projects(con) -> list:
    return [_row(r) for r in con.execute(
        "SELECT * FROM projects ORDER BY path")]


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
    raw = explicit or paths.env("ORCHESTRA_ROOT") or Path.cwd()
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


def current(con) -> Project | None:
    """The project the working directory belongs to, or None outside them.

    Unlike ``resolve`` this asks no config and refuses nothing: it is for
    reading a number the way a human meant it, where being outside a project
    is an ordinary answer.
    """
    try:
        return _deepest(con, start_dir())
    except (OSError, ValueError):
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
    return workdir_for(con, _row(row))


def workdir_for(con, proj: "Project | None") -> "Project | None":
    """The project with a directory a run can actually work in (W-0312).

    Work's project path is ORGANIZATIONAL, not a filesystem path: since the
    store-only reset a project is a record, and `projects.mjs` hardcodes
    empty aliasPaths because a store project has no filesystem markers at
    all. Orchestra still had to derive a checkout from that path, so grouping
    the two tools under "Agentic Engineering" pointed every dispatch at a
    directory that was never there (I-0302, runs 59 and 60: one died on the
    worktree guard, the other spawned a supervisor with a cwd that did not
    exist and vanished before writing a byte).

    So the path Work names is a hint, and this is the answer, in order:

    1. Work's own path, when it IS a directory — the common case, unchanged.
    2. The workspace root plus the path's LAST segment. Grouping is Work's
       business; the folder keeps its own name. "Agentic Engineering/
       orchestra" finds ~/Projects/orchestra by itself, which is what should
       have happened the moment the two tools were grouped — no human tells
       Orchestra something it can see.
    3. A checkout bound with ``link`` — the escape hatch for a repository
       that lives somewhere its name does not give away.
    4. An ephemeral workspace under ~/.orchestra/workspaces, for a project
       with no folder anywhere, which is most of them. It is not a git
       repository, and the dispatch skips isolation there rather than
       failing the way run 59 did.

    A LINK WINS EVEN WHEN ITS DIRECTORY IS GONE — it outranks discovery for
    that reason. Linking is how a checkout is claimed, so a claim that stops
    resolving (an external volume left unmounted) must fail where a human
    sees it, never be replaced by an empty workspace an agent would fill.

    The workspace directory is created here, like every other directory
    ``paths`` hands out, and owner-only for the same reason.
    """
    if proj is None or proj.path.is_dir():
        return proj
    linked = con.execute(
        "SELECT * FROM projects WHERE project_id=? AND work_id IS NULL",
        (proj.project_id,)).fetchone()
    if linked is not None:
        return _row(linked)
    found = _discover(con, proj)
    if found is not None:
        return Project(proj.project_id, found, proj.work_id, proj.name)
    return Project(proj.project_id, paths.workspace_dir(proj.project_id),
                   proj.work_id, proj.name)


def _discover(con, proj: "Project") -> Path | None:
    """The folder Work's path names WITHOUT its grouping, if it is one.

    Work stores "Group/thing" and Orchestra cached it under the workspace
    root, so the root is that path with the Work id's segments removed. The
    same root plus the last segment is where the folder actually sits.

    A directory another project already claims is not this one's: two
    projects named "docs" under different groups must not collapse onto the
    same checkout, so an ambiguous hit is declined and the workspace answers
    instead.
    """
    rel = Path(proj.work_id or "")
    depth = len(rel.parts)
    if depth < 2 or depth >= len(proj.path.parts):
        return None  # nothing was grouped, so there is nothing to strip
    root = proj.path.parents[depth - 1]
    candidate = root / rel.parts[-1]
    if not candidate.is_dir():
        return None
    taken = con.execute(
        "SELECT project_id FROM projects WHERE path=? AND project_id<>?",
        (str(candidate), proj.project_id)).fetchone()
    return None if taken else candidate


def is_workspace(root: Path) -> bool:
    """True for an ephemeral workspace Orchestra invented (W-0312).

    The one place isolation is skipped rather than demanded: a workspace has
    no repository to branch from and never had one. A REAL checkout that is
    not a git repository still fails closed when [work] worktree is on — a
    run that quietly shares a checkout it was told to isolate from is the
    thing that guard exists to prevent.
    """
    try:
        return Path(root).resolve().is_relative_to(
            (paths.home() / "workspaces").expanduser().resolve())
    except (OSError, ValueError):
        return False


def link(con, project_path: str, root: Path) -> "Project":
    """Bind a Work project to the checkout it actually lives in.

    The row carries Work's project id with ``work_id`` NULL, so a refresh
    neither replaces it (it inserts by path) nor prunes it (the prune skips
    locally owned rows). Work keeps organizing; Orchestra keeps the address.
    """
    from orchestra import db  # local: db imports paths, not project

    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"orchestra: {root} is not a directory")
    row = con.execute(
        "SELECT * FROM projects WHERE work_id=? OR project_id=? "
        "ORDER BY work_id IS NULL LIMIT 1",
        (project_path, project_path)).fetchone()
    if row is None:
        raise SystemExit(
            f"orchestra: no project matches {project_path!r} — "
            "`orchestra project list` names them")
    con.execute(
        "INSERT OR REPLACE INTO projects(path, project_id, work_id, name, "
        "refreshed_at) VALUES(?,?,NULL,?,?)",
        (str(root), row["project_id"], row["name"], db.now()))
    con.commit()
    return _row(con.execute("SELECT * FROM projects WHERE path=?",
                            (str(root),)).fetchone())


def root_for(con, run) -> Path:
    """A run's project checkout. Falls back to its recorded workdir so a run
    whose project left Work is still supervisable."""
    hit = workdir_for(con, by_id(con, run["project_id"]))
    return hit.path if hit else Path(run["workdir"])


def dir_key_for(con, run) -> str:
    """The worktree directory key: the immutable projectId. A run whose project
    left Work falls back to its workdir name so it stays supervisable."""
    return run["project_id"] or Path(run["workdir"]).name
