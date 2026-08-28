"""Orchestra's project registry: IDENTITY, not filesystem (schema v29).

A project row is a slug, a name, provenance, and the parked flags — nothing
else. Orchestra is a runner: the checkout a run works in is the CALLER's to
supply at dispatch, and the durable answer to "where does this project run"
is the run history itself (``runs.repo``), not a stored path. A source
adapter caches its own label-to-folder map in its own table (``checkouts``,
owned by ``sweeper.py``); the core never reads it (CONTRACT §7 Enforcement).

Settings, run numbering, and the per-project state directory all key on
``project_id``; humans address a project by ``slug``.
"""
import uuid
from pathlib import Path

from orchestra import paths


class Project:
    """One project identity. No path: a checkout belongs to a dispatch."""

    __slots__ = ("project_id", "slug", "name", "local", "archived",
                 "archived_override")

    def __init__(self, project_id: str, slug: str | None, name: str | None,
                 local: bool = True, archived: bool = False,
                 archived_override: bool | None = None):
        self.project_id = project_id
        # The HUMAN address (schema v27): lowercase kebab-case, unique,
        # minted once and stable — it keys ~/.orchestra/projects/<slug>/.
        self.slug = slug or name or project_id
        self.name = name
        # Provenance, not a source's name: True when the owner minted the
        # row, False when an adapter cached it from its source.
        self.local = local
        # DESIGN §1: parked, not deleted, and ALREADY DERIVED — this is
        # COALESCE(archived_override, archived, 0), not the source's mirror.
        self.archived = archived
        # Who decided. None means "still following the source": a row parked
        # with no override was parked by the source, not by the owner.
        self.archived_override = archived_override

    def __repr__(self) -> str:
        return f"Project({self.slug} · {self.project_id})"


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
    return Project(row["project_id"], row["slug"], row["name"],
                   bool(row["local"]), _effective(row),
                   None if row["archived_override"] is None
                   else bool(row["archived_override"]))


# --- identity ---------------------------------------------------------------

def mint_slug(con, base: str, project_id: str) -> str:
    """Mint the project's one human address (schema v27).

    Lowercase kebab-case from ``base``, unique, suffixed ``-2``/``-3`` on a
    collision. A project that already has a slug keeps it: the slug keys
    ``~/.orchestra/projects/<slug>/``, so a rename or refresh must never
    rewrite it.
    """
    row = con.execute(
        "SELECT slug FROM projects WHERE project_id=? AND slug IS NOT NULL",
        (project_id,)).fetchone()
    if row is not None:
        return row["slug"]
    want = paths.kebab(base)
    slug, n = want, 2
    while con.execute("SELECT 1 FROM projects WHERE slug=? AND project_id<>?",
                      (slug, project_id)).fetchone():
        slug, n = f"{want}-{n}", n + 1
    return slug


def create(con, name: str) -> "Project":
    """Mint a project the owner names. Identity only — no directory is asked
    for and none is stored; the first dispatch supplies a checkout."""
    from orchestra import db  # local: db imports paths, not project

    name = (name or "").strip()
    if not name:
        raise SystemExit("orchestra: a project needs a name")
    hit = by_slug(con, paths.kebab(name))
    if hit is not None:
        return hit  # idempotent: the same name is the same project
    project_id = str(uuid.uuid4())
    con.execute(
        "INSERT INTO projects(project_id, slug, name, local, refreshed_at) "
        "VALUES(?,?,?,1,?)",
        (project_id, mint_slug(con, name, project_id), name, db.now()))
    con.commit()
    return by_id(con, project_id)


def remember_identity(con, entries: list, prune: bool = True) -> int:
    """The ONE seam an adapter writes identities through (CONTRACT §7).

    ``entries`` is a list of ``(project_id, name, archived)``; the adapter
    keeps its label-to-folder map in its own table. The upsert never touches
    ``archived_override`` (the owner's answer) and never rewrites a slug.
    ``prune`` retires cached identities the source no longer names — but
    only ones nothing here holds onto: no runs, no owner override.
    """
    from orchestra import db  # local: db imports paths, not project

    ts, count, keep = db.now(), 0, []
    for project_id, name, archived in entries:
        if not project_id:
            continue
        con.execute(
            "INSERT INTO projects(project_id, slug, name, local, "
            "refreshed_at, archived) VALUES(?,?,?,0,?,?) "
            "ON CONFLICT(project_id) DO UPDATE SET name=excluded.name, "
            "refreshed_at=excluded.refreshed_at, archived=excluded.archived",
            (project_id, mint_slug(con, name or project_id, project_id),
             name, ts, int(bool(archived))))
        keep.append(project_id)
        count += 1
    if prune:
        marks = ",".join("?" * len(keep)) or "''"
        con.execute(
            f"DELETE FROM projects WHERE local=0 AND project_id NOT IN ({marks}) "
            "AND archived_override IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.project_id = "
            "projects.project_id)", keep)
    con.commit()
    return count


def forget(con, selector: str) -> bool:
    """Drop a project the owner minted. Refuses an adapter-cached one: the
    next refresh would put it back and the removal would look broken."""
    hit = find(con, selector)
    if hit is None:
        return False
    if not hit.local:
        raise SystemExit(
            f"orchestra: {hit.slug} is cached from a work source; archive it "
            "here, or remove it at the source")
    con.execute("DELETE FROM projects WHERE project_id=?", (hit.project_id,))
    con.commit()
    return True


def set_archived(con, selector: str, archived: bool) -> Project | None:
    """Park or unpark ANY project, source-backed or not.

    Archiving means "hide this from Orchestra and stop dispatching for it" —
    Orchestra's own decision about its own surface. It writes
    ``archived_override``, never the source's mirror: the human decided here,
    and the human always wins (DESIGN §1). Unlike ``forget``, no refresh can
    undo it.
    """
    hit = find(con, selector)
    if hit is None:
        return None
    con.execute("UPDATE projects SET archived_override=? WHERE project_id=?",
                (int(archived), hit.project_id))
    con.commit()
    return by_id(con, hit.project_id)


def all_projects(con, include_archived: bool = False) -> list:
    where = "" if include_archived else f" WHERE {ARCHIVED_SQL}=0"
    return [_row(r) for r in con.execute(
        f"SELECT * FROM projects{where} ORDER BY slug")]


# --- lookups ----------------------------------------------------------------

def by_id(con, project_id: str | None) -> Project | None:
    if not project_id:
        return None
    return _row(con.execute("SELECT * FROM projects WHERE project_id=?",
                            (project_id,)).fetchone())


def by_slug(con, slug: str | None) -> Project | None:
    if not slug:
        return None
    return _row(con.execute("SELECT * FROM projects WHERE slug=?",
                            (slug,)).fetchone())


def find(con, selector: str) -> Project | None:
    """The project a caller NAMES: slug first, then project id."""
    return by_slug(con, selector) or by_id(con, selector)


def start_dir(explicit: str | None = None) -> Path:
    raw = explicit or paths.env("ORCHESTRA_ROOT") or Path.cwd()
    return Path(raw).expanduser().resolve()


def for_dir(con, start: Path | None = None) -> Project | None:
    """The project that usually runs in this directory, from the run history.

    The runner's own records ARE the address book: the deepest ``runs.repo``
    containing ``start`` names the project, so a directory that has hosted a
    run resolves forever, and no path table exists to go stale. A directory
    that never hosted one is an ordinary None — the caller asks for
    ``--project`` once, and the next run teaches the history.
    """
    # ponytail: scans the distinct (project, repo) pairs; index or cache it
    # if a database ever holds tens of thousands of distinct checkouts.
    try:
        start = Path(start) if start is not None else start_dir()
    except (OSError, ValueError):
        return None
    best, best_len = None, -1
    for row in con.execute(
            "SELECT project_id, repo, MAX(id) AS latest FROM runs "
            "WHERE layer IS NULL AND repo IS NOT NULL AND project_id IS NOT "
            "NULL GROUP BY project_id, repo"):
        p = Path(row["repo"])
        if (start == p or p in start.parents) and len(row["repo"]) > best_len:
            best, best_len = row["project_id"], len(row["repo"])
    return by_id(con, best)


def current(con) -> Project | None:
    """The project the working directory belongs to, or None outside any.
    History-based, refuses nothing: being outside a project is an ordinary
    answer when reading a run number the way a human meant it."""
    return for_dir(con)


def last_root(con, project_id: str) -> Path | None:
    """Where this project last ran: the newest recorded checkout that still
    exists. The runner's history is the default, not a stored setting."""
    for row in con.execute(
            "SELECT repo, MAX(id) AS latest FROM runs WHERE project_id=? "
            "AND layer IS NULL AND repo IS NOT NULL "
            "GROUP BY repo ORDER BY latest DESC LIMIT 20", (project_id,)):
        p = Path(row["repo"])
        if p.is_dir():
            return p
    return None


def root_for(con, run) -> Path:
    """The checkout this run branched from and lands into: the run's own
    ``repo`` (v28, backfilled at v29). A row whose repo is gone or was never
    stamped falls back to where the project last ran, then to its recorded
    workdir, so the run stays supervisable."""
    repo = run["repo"] if "repo" in run.keys() else None
    if repo and Path(repo).is_dir():
        return Path(repo)
    fallback = last_root(con, run["project_id"]) if run["project_id"] else None
    return fallback if fallback is not None else Path(run["workdir"])


def guard_run_path(root) -> Path:
    """Refuse a caller-named checkout inside Orchestra's own state directory:
    a worktree of the run database is never what anyone meant, and a run
    writing there writes into every other run's records. The one exception
    is a project's ephemeral workspace, which is a legitimate place for a
    run to stand."""
    root = Path(root).expanduser().resolve()
    home = paths.home().expanduser().resolve()
    if root.is_relative_to(home) and not is_workspace(root):
        raise SystemExit(
            f"orchestra: {root} is inside Orchestra's own state directory "
            f"({home}) — runs work in your checkouts, not in the run records")
    return root


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
