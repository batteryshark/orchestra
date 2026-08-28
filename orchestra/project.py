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

    __slots__ = ("project_id", "path", "source_ref", "name", "archived",
                 "archived_override", "slug")

    def __init__(self, project_id: str, path: Path, source_ref: str | None,
                 name: str | None, archived: bool = False,
                 archived_override: bool | None = None,
                 slug: str | None = None):
        self.project_id = project_id
        self.path = path
        # The source's own identifier for this project, or None when the row
        # was adopted locally. Opaque (CONTRACT §7 Enforcement 1).
        self.source_ref = source_ref
        self.name = name
        # DESIGN §1: parked, not deleted, and ALREADY DERIVED — this is
        # COALESCE(archived_override, archived, 0), not the source's mirror.
        # Read by the unattended lanes and the listing surfaces; nothing that
        # reads history looks at it.
        self.archived = archived
        # Who decided. None means "still following the source", which is the
        # only thing a surface needs the raw columns for: a row parked with
        # no override was parked by the source, not by the owner.
        self.archived_override = archived_override
        # The HUMAN address (schema v27): lowercase kebab-case, unique across
        # project ids, minted once and stable — it keys the project's own
        # directory under ~/.orchestra/projects/. None only on a row a stale
        # pre-v27 writer inserted; display falls back, lookups do not.
        self.slug = slug or name or path.name

    def __repr__(self) -> str:
        return f"Project({self.project_id} @ {self.path})"


# DESIGN §1: effective archived, spelled ONCE. The owner's override wins over
# the source's mirror; NULL follows the source, which is what auto-parks a
# project the source archived. Every SQL filter uses this string;
# ``_effective`` is the same rule for a row already in hand.
ARCHIVED_SQL = "COALESCE(archived_override, archived, 0)"


def _effective(row) -> bool:
    over = row["archived_override"]
    return bool(row["archived"] if over is None else over)


def _row(row) -> Project | None:
    if row is None:
        return None
    return Project(row["project_id"], Path(row["path"]), row["source_ref"],
                   row["name"],
                   _effective(row),
                   None if row["archived_override"] is None
                   else bool(row["archived_override"]),
                   slug=row["slug"])


# --- cache ------------------------------------------------------------------

def mint_slug(con, base: str, project_id: str) -> str:
    """Mint the project's one human address (schema v27).

    Lowercase kebab-case from ``base``, unique across project ids, suffixed
    ``-2``/``-3`` on a collision. A project that already has a slug keeps it:
    the slug keys ``~/.orchestra/projects/<slug>/``, so a rename or refresh
    must never rewrite it.
    """
    row = con.execute(
        "SELECT slug FROM projects WHERE project_id=? AND slug IS NOT NULL "
        "LIMIT 1", (project_id,)).fetchone()
    if row is not None:
        return row["slug"]
    want = paths.kebab(base)
    slug, n = want, 2
    while con.execute("SELECT 1 FROM projects WHERE slug=? AND project_id<>?",
                      (slug, project_id)).fetchone():
        slug, n = f"{want}-{n}", n + 1
    return slug


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
    minted: dict[str, str] = {}
    for path, project_id, source_ref, name, archived in rows:
        if not project_id or not source_ref:
            continue  # a row with no source identity is not source-backed
        if project_id not in minted:
            minted[project_id] = mint_slug(
                con, name or Path(path).name, project_id)
        # UPSERT, not INSERT OR REPLACE: REPLACE deletes the row first, which
        # would drop ``archived_override`` — the owner's answer — on every
        # refresh. The source still writes its own mirror column (DESIGN §1).
        # ``slug`` keeps its first value the same way: minted once, then the
        # row's own copy wins over whatever this refresh would have minted.
        con.execute(
            "INSERT INTO projects(path, project_id, source_ref, name, "
            "refreshed_at, archived, slug) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET project_id=excluded.project_id, "
            "source_ref=excluded.source_ref, name=excluded.name, "
            "refreshed_at=excluded.refreshed_at, archived=excluded.archived, "
            "slug=COALESCE(projects.slug, excluded.slug)",
            (str(path), project_id, source_ref, name, ts, int(bool(archived)),
             minted[project_id]))
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
    project_id = str(uuid.uuid4())
    con.execute(
        "INSERT INTO projects(path, project_id, source_ref, name, refreshed_at, "
        "slug) VALUES(?,?,NULL,?,?,?)",
        (str(root), project_id, name or root.name, db.now(),
         mint_slug(con, name or root.name, project_id)))
    con.commit()
    return _row(con.execute(
        "SELECT * FROM projects WHERE path=?", (str(root),)).fetchone())


def source_owned(root, source_ref: str, verb: str = "remove") -> str:
    """Why a source-backed row refuses a local edit — ONE wording.

    Named, not described: the human reading this has to know WHERE to go, and
    the identifier the source gave the project is the only address this module
    has (CONTRACT §7 — it is not parsed, just quoted back). ``forget`` is the
    ONE caller: removing a row the next refresh re-creates is genuinely
    broken. Archiving is not — that is Orchestra's own surface, so it takes
    an override instead of a refusal (DESIGN §1).
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
    """Park or unpark ANY registered project, source-backed or not.

    Archiving means "hide this from Orchestra and stop dispatching for it" —
    Orchestra's own decision about its own surface. Mirroring the source's
    flag as the only writer conflated the source's record with the owner's
    local preference, so this refused a source-backed project and left the
    owner nothing to park it with. It writes ``archived_override``, never the
    source's mirror: the human decided here, and the human always wins
    (DESIGN §1). Unlike ``forget``, no refresh can undo it.
    """
    root = Path(root).expanduser().resolve()
    row = con.execute("SELECT path FROM projects WHERE path=?",
                      (str(root),)).fetchone()
    if row is None:
        return False
    con.execute("UPDATE projects SET archived_override=? WHERE path=?",
                (int(archived), str(root)))
    con.commit()
    return True


def all_projects(con, include_archived: bool = False) -> list:
    where = "" if include_archived else f" WHERE {ARCHIVED_SQL}=0"
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


def by_slug(con, slug: str | None) -> Project | None:
    """Resolve the human address. Alias and link rows share one slug, so the
    pick mirrors ``link``'s preference: the source-backed row first, then the
    shortest path; ``workdir_for`` turns either into a usable checkout."""
    if not slug:
        return None
    return _row(con.execute(
        "SELECT * FROM projects WHERE slug=? "
        "ORDER BY source_ref IS NULL, LENGTH(path) LIMIT 1",
        (slug,)).fetchone())


def find(con, selector: str) -> Project | None:
    """The project a caller NAMES: slug first, then project id, then a
    registered path. This is `dispatch --project` and the HTTP dispatch
    route; returns the registered row, so callers that need a directory a
    run can work in pass it through ``workdir_for``."""
    hit = by_slug(con, selector) or by_id(con, selector)
    if hit is not None:
        return hit
    try:
        target = str(Path(selector).expanduser().resolve())
    except (OSError, ValueError):
        return None
    return _row(con.execute("SELECT * FROM projects WHERE path=?",
                            (target,)).fetchone())


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
    4. An ephemeral workspace — ~/.orchestra/projects/<slug>/workspace, or
       the pre-v27 ~/.orchestra/workspaces/<id> when one already holds
       state — for a project with no folder anywhere, which is most of
       them. It is not a git repository, and the dispatch skips isolation
       there rather than failing the way run 59 did.

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
                       proj.archived, proj.archived_override, slug=proj.slug)
    # Pre-v27 workspaces were keyed by project id under ~/.orchestra/
    # workspaces. One that exists keeps its state where it already lives — a
    # run may be standing in it — so only a project with no workspace yet
    # gets the slug-keyed directory under ~/.orchestra/projects/.
    legacy = paths.home().expanduser() / "workspaces" \
        / paths.slugify(proj.project_id)
    ws = legacy if legacy.is_dir() else paths.workspace_dir(proj.slug)
    return Project(proj.project_id, ws, proj.source_ref, proj.name,
                   proj.archived, proj.archived_override, slug=proj.slug)


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
        home = paths.home().expanduser().resolve()
        target = Path(root).resolve()
        if target.is_relative_to(home / "workspaces"):
            return True  # pre-v27 layout, kept where its state lives
        parts = target.relative_to(home / "projects").parts \
            if target.is_relative_to(home / "projects") else ()
        return len(parts) >= 2 and parts[1] == "workspace"
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
        "refreshed_at, slug) VALUES(?,?,NULL,?,?,?)",
        (str(root), row["project_id"], row["name"], db.now(), row["slug"]))
    con.commit()
    return _row(con.execute("SELECT * FROM projects WHERE path=?",
                            (str(root),)).fetchone())


def root_for(con, run) -> Path:
    """The checkout this run branched from and lands into.

    The run's own ``repo`` (schema v28) wins: a project is not one checkout,
    so the registry's default path cannot say where THIS run's branch lives.
    A pre-v28 row falls back to the registry, then to its recorded workdir,
    so a run whose project left the source is still supervisable.
    """
    repo = run["repo"] if "repo" in run.keys() else None
    if repo and Path(repo).is_dir():
        return Path(repo)
    hit = workdir_for(con, by_id(con, run["project_id"]))
    return hit.path if hit else Path(run["workdir"])


def run_artifacts(con, run) -> tuple[Path, Path]:
    """Where one run's brief and raw log live: (brief_path, log_path).

    A worker run files under its project by the project's own run number —
    ``projects/<slug>/runs/run-<seq>/`` — because that number is the one
    humans quote from the board. A row with no project number or no project
    (a control turn, a pre-v27 row) stays in the flat ``briefs/`` and
    ``logs/`` layout, keyed by the globally unique row id. Readers never
    derive these paths: the run row's ``brief_path``/``log_path`` are the
    record, so the two layouts coexist without a shim.
    """
    seq = run["project_seq"] if "project_seq" in run.keys() else None
    if seq and run["project_id"]:
        base = paths.run_dir(dir_key_for(con, run), int(seq))
        return base / "brief.md", base / "log.jsonl"
    run_id = int(run["id"])
    return (paths.briefs_dir() / f"run-{run_id}.md",
            paths.logs_dir() / f"run-{run_id}.jsonl")


def dir_key_for(con, run) -> str:
    """The worktree directory key: the project's slug (schema v27). A row a
    stale writer left slugless falls back to the stable project id, and a run
    whose project left the registry falls back to its workdir name so it
    stays supervisable."""
    row = con.execute(
        "SELECT slug FROM projects WHERE project_id=? AND slug IS NOT NULL "
        "LIMIT 1", (run["project_id"],)).fetchone() if run["project_id"] else None
    if row is not None:
        return row["slug"]
    return run["project_id"] or Path(run["workdir"]).name
