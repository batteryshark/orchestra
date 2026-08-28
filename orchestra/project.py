"""Resolve a directory through Orchestra's central project registry.

The registry is Orchestra's own (DESIGN §2). A row is either adopted locally
or backed by a work source, and the two differ by exactly one column:
``source_ref``, the SOURCE'S OWN identifier for the project. It is opaque
here, like ``runs.ref`` — this module compares it, groups by it and hands it
back, and never parses it for meaning beyond the path shape ``_discover``
documents. Filling source-backed rows is the adapter's job (CONTRACT §7
Enforcement); ``remember_source`` is the one seam it writes through, and
nothing in this file knows which source is on the other side.

The deepest registered path containing the current directory wins. Everything
downstream uses the stored project id, so renaming a project folder loses no
settings.
"""
import os
import uuid
from pathlib import Path

from orchestra import paths

MISS_HINT = """\
orchestra: {path} is not inside a registered project.
Run `orchestra project add .`, or configure a work source that knows this
directory and refresh, then retry."""


class Project:
    """One cached (path -> projectId) mapping. ``path`` is a lookup key only."""

    __slots__ = ("project_id", "path", "source_ref", "name", "archived")

    def __init__(self, project_id: str, path: Path, source_ref: str | None,
                 name: str | None, archived: bool = False):
        self.project_id = project_id
        self.path = path
        # The source's own identifier for this project, or None when the row
        # was adopted locally. Opaque (CONTRACT §7 Enforcement 1).
        self.source_ref = source_ref
        self.name = name
        # DESIGN §1: parked, not deleted. Read by the unattended lanes and
        # the listing surfaces; nothing that reads history looks at it.
        self.archived = archived

    @property
    def slug(self) -> str:
        return self.source_ref or self.name or self.path.name

    def __repr__(self) -> str:
        return f"Project({self.project_id} @ {self.path})"


def _row(row) -> Project | None:
    if row is None:
        return None
    return Project(row["project_id"], Path(row["path"]), row["source_ref"],
                   row["name"],
                   bool(row["archived"]))


# --- cache ------------------------------------------------------------------

def remember_source(con, rows: list) -> int:
    """Write the source-backed rows of the registry. Returns rows written.

    The ONE seam an adapter writes the registry through (CONTRACT §7
    Enforcement): each entry is a plain
    ``(path, project_id, source_ref, name, archived)`` tuple, already
    resolved to an absolute path by whoever knows the source's shape. This
    module owns the table and its rules; it learns nothing about the source
    to enforce them.
    """
    from orchestra import db  # local: db imports paths, not project

    ts, count, seen = db.now(), 0, []
    for path, project_id, source_ref, name, archived in rows:
        if not project_id or not source_ref:
            continue  # a row with no source identity is not source-backed
        con.execute(
            "INSERT OR REPLACE INTO projects(path, project_id, source_ref, name, "
            "refreshed_at, archived) VALUES(?,?,?,?,?,?)",
            (str(path), project_id, source_ref, name, ts, int(bool(archived))))
        seen.append(str(path))
        count += 1
    # A source-backed row whose path the source no longer names is stale — a
    # moved or deleted project, or a worktree copy that briefly won discovery
    # (I-0013's ghost lived here long after the source was fixed). Prune
    # those; locally adopted rows (``source_ref`` NULL) are never a source's
    # to delete.
    if seen:
        marks = ",".join("?" * len(seen))
        con.execute(
            "DELETE FROM projects WHERE source_ref IS NOT NULL "
            f"AND path NOT IN ({marks})", seen)
    con.commit()
    return count


def adopt(con, root: Path, name: str | None = None) -> "Project":
    """Register a project Orchestra owns itself, with no source behind it.

    A work source is the system of record when one is configured, and
    ``remember_source`` caches its list. But the projects table was the ONLY
    way to address a directory, and the only writer was that source — so
    without it ``dispatch`` could not resolve anything and the tool did not
    run standalone at all.

    A locally adopted project is the same row with ``source_ref`` NULL.
    Nothing downstream cares: a run resolves by path and carries
    ``project_id``, and writeback is skipped for a run with no ref anyway.
    ``remember_source`` only ever inserts or replaces the paths the source
    names, and the stale-row prune skips source_ref-NULL rows, so a later
    refresh cannot delete this row — and if the source is ever told about the
    same directory, its entry replaces this one, which is the right
    precedence.
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
        "INSERT INTO projects(path, project_id, source_ref, name, refreshed_at) "
        "VALUES(?,?,NULL,?,?)",
        (str(root), str(uuid.uuid4()), name or root.name, db.now()))
    con.commit()
    return _row(con.execute(
        "SELECT * FROM projects WHERE path=?", (str(root),)).fetchone())


def source_owned(root, source_ref: str, verb: str = "archive") -> str:
    """Why a source-backed row refuses a local edit — ONE wording.

    Named, not described: the human reading this has to know WHERE to go, and
    the identifier the source gave the project is the only address this module
    has (CONTRACT §7 — it is not parsed, just quoted back). The CLI raises it,
    the HTTP surface returns it, and the dashboard prints it in the row where
    a local project would show its archive control.
    """
    return (f"orchestra: {root} comes from the work source that owns "
            f"{source_ref!r}; {verb} it there, not here")


def forget(con, root: Path) -> bool:
    """Drop a locally adopted project. Refuses one a work source owns, since
    the next refresh would put it back and the removal would look broken."""
    root = Path(root).expanduser().resolve()
    row = con.execute("SELECT source_ref FROM projects WHERE path=?",
                      (str(root),)).fetchone()
    if row is None:
        return False
    if row["source_ref"]:
        raise SystemExit(source_owned(root, row["source_ref"], "remove"))
    con.execute("DELETE FROM projects WHERE path=?", (str(root),))
    con.commit()
    return True


def set_archived(con, root: Path, archived: bool) -> bool:
    """Park or unpark a locally adopted project. Refuses one a work source
    owns, since that source owns the flag: the next refresh would overwrite
    the local answer and the change would look broken."""
    root = Path(root).expanduser().resolve()
    row = con.execute("SELECT source_ref FROM projects WHERE path=?",
                      (str(root),)).fetchone()
    if row is None:
        return False
    if row["source_ref"]:
        raise SystemExit(source_owned(root, row["source_ref"]))
    con.execute("UPDATE projects SET archived=? WHERE path=?",
                (int(archived), str(root)))
    con.commit()
    return True


def all_projects(con, include_archived: bool = False) -> list:
    where = "" if include_archived else " WHERE archived=0"
    return [_row(r) for r in con.execute(
        f"SELECT * FROM projects{where} ORDER BY path")]


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


def resolve(con, cfg: dict, explicit: str | None = None,
            refresh=None) -> Project:
    """The project containing this directory. Refreshes once on a miss.

    ``refresh`` is the adapter's ``(con, cfg) -> int`` re-read of its source's
    project list. Passing it is how a caller that HAS a source keeps the
    warm-the-cold-cache behaviour; omitting it means the registry answers
    from what it already holds. Core code cannot reach a source itself
    (CONTRACT §7 Enforcement 3), and a callable in the signature is smaller
    than a registry of listeners.
    """
    start = start_dir(explicit)
    hit = _deepest(con, start)
    if hit is None and refresh is not None and refresh(con, cfg):
        hit = _deepest(con, start)
    if hit is None:
        raise SystemExit(MISS_HINT.format(path=start))
    return hit


def try_resolve(con, cfg: dict, explicit: str | None = None,
                refresh=None) -> Project | None:
    try:
        return resolve(con, cfg, explicit, refresh)
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


def by_source_ref(con, ref: str | None) -> Project | None:
    """Resolve a source's own project reference to a checkout.

    The ref is matched as itself first and as an absolute local path second,
    because a source is free to use either and this module is not allowed to
    know which one it did (CONTRACT §7 Enforcement 1).
    """
    if not ref:
        return None
    row = con.execute(
        "SELECT * FROM projects WHERE source_ref=? ORDER BY LENGTH(path) LIMIT 1",
        (ref,)).fetchone()
    if row is None:
        row = con.execute("SELECT * FROM projects WHERE path=?",
                          (str(Path(ref).expanduser()),)).fetchone()
    return workdir_for(con, _row(row))


def workdir_for(con, proj: "Project | None") -> "Project | None":
    """The project with a directory a run can actually work in (W-0312).

    A source's project reference is ORGANIZATIONAL, not a filesystem path: a
    record system groups projects and may hold no filesystem marker at all.
    Orchestra still had to derive a checkout from that reference, so grouping
    two tools under "Agentic Engineering" pointed every dispatch at a
    directory that was never there (I-0302, runs 59 and 60: one died on the
    worktree guard, the other spawned a supervisor with a cwd that did not
    exist and vanished before writing a byte).

    So the reference is a hint, and this is the answer, in order:

    1. The reference itself, when it IS a directory — the common case.
    2. The workspace root plus the reference's LAST segment. Grouping is the
       source's business; the folder keeps its own name. "Agentic
       Engineering/orchestra" finds ~/Projects/orchestra by itself, which is
       what should have happened the moment the two tools were grouped — no
       human tells Orchestra something it can see.
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
        "SELECT * FROM projects WHERE project_id=? AND source_ref IS NULL",
        (proj.project_id,)).fetchone()
    if linked is not None:
        hit = _row(linked)
        # Parking belongs to the PROJECT, not to the row that holds its
        # address: a linked checkout does not unpark what the source parked.
        hit.archived = hit.archived or proj.archived
        return hit
    found = _discover(con, proj)
    if found is not None:
        return Project(proj.project_id, found, proj.source_ref, proj.name,
                       proj.archived)
    return Project(proj.project_id, paths.workspace_dir(proj.project_id),
                   proj.source_ref, proj.name, proj.archived)


def _discover(con, proj: "Project") -> Path | None:
    """The folder the source's reference names WITHOUT its grouping.

    A source stores "Group/thing" and Orchestra cached it under the workspace
    root, so the root is that path with the reference's segments removed. The
    same root plus the last segment is where the folder actually sits. This
    is the ONE place the ref's path shape is read, and it reads it as shape
    alone — segments, not meaning (CONTRACT §7 Enforcement 1).

    A directory another project already claims is not this one's: two
    projects named "docs" under different groups must not collapse onto the
    same checkout, so an ambiguous hit is declined and the workspace answers
    instead.
    """
    rel = Path(proj.source_ref or "")
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
    """Bind a source-backed project to the checkout it actually lives in.

    The row carries the same ``project_id`` with ``source_ref`` NULL, so a
    refresh neither replaces it (it inserts by path) nor prunes it (the prune
    skips locally owned rows). The source keeps organizing; Orchestra keeps
    the address.
    """
    from orchestra import db  # local: db imports paths, not project

    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"orchestra: {root} is not a directory")
    row = con.execute(
        "SELECT * FROM projects WHERE source_ref=? OR project_id=? "
        "ORDER BY source_ref IS NULL LIMIT 1",
        (project_path, project_path)).fetchone()
    if row is None:
        raise SystemExit(
            f"orchestra: no project matches {project_path!r} — "
            "`orchestra project list` names them")
    con.execute(
        "INSERT OR REPLACE INTO projects(path, project_id, source_ref, name, "
        "refreshed_at) VALUES(?,?,NULL,?,?)",
        (str(root), row["project_id"], row["name"], db.now()))
    con.commit()
    return _row(con.execute("SELECT * FROM projects WHERE path=?",
                            (str(root),)).fetchone())


def root_for(con, run) -> Path:
    """A run's project checkout. Falls back to its recorded workdir so a run
    whose project left the source is still supervisable."""
    hit = workdir_for(con, by_id(con, run["project_id"]))
    return hit.path if hit else Path(run["workdir"])


def dir_key_for(con, run) -> str:
    """The worktree directory key: the immutable projectId. A run whose project
    left the source falls back to its workdir name so it stays supervisable."""
    return run["project_id"] or Path(run["workdir"]).name
