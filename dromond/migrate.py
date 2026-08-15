"""Fold a pre-W-0163 per-project ``.maestro/`` database into ``~/.dromond``.

The legacy directory keeps its old name: it was written when the product was
called Maestro and it is already on disk (see ``paths.LEGACY_STATE_DIR``).
This is a DIFFERENT thing from the ``~/.maestro`` -> ``~/.dromond`` move in
``paths.adopt_legacy_home`` — that one relocates central state, this one folds
in per-project state.

Copy-only. The legacy database is opened read-only and nothing the user owns
is deleted or moved — the old directory stays until the human removes it.

Run ids are renumbered (two projects both start at 1), so every reference —
``parent_run``, ``messages.run_id``, both dependency tables — is remapped
through one id map. Slugs are UNIQUE workspace-wide now, so a slug that
already exists is dropped rather than colliding; the run id is the address.
"""
import shutil
import sqlite3
from pathlib import Path

from dromond import db, paths

# meta is deliberately not folded: its keys are Work cursors and a schema
# version, both of which belong to the central database's own history.
SKIPPED_TABLES = ("meta",)


def find_legacy_db(start: Path) -> Path | None:
    """Walk up for a legacy state directory, the way the CLI used to."""
    start = start.expanduser().resolve()
    for candidate in [start, *start.parents]:
        p = candidate / paths.LEGACY_STATE_DIR / paths.LEGACY_STATE_DB
        if p.is_file():
            return p
    return None


def _columns(con, table: str) -> list[str]:
    return [r["name"] for r in con.execute(f"PRAGMA table_info({table})")]


def _copy_artifact(src: str | None, dest_dir: Path, name: str) -> str | None:
    if not src:
        return None
    source = Path(src)
    if not source.is_file():
        return src  # gone already; keep the record honest rather than inventing one
    dest = dest_dir / name
    shutil.copy2(source, dest)
    return str(dest)


def fold(legacy_db: Path, project_id: str | None, con=None) -> dict:
    """Copy every run (and its messages, dependencies, artifacts) across.

    ``con`` is the central connection; opened here when not supplied.
    Returns a report: counts plus the id map.
    """
    # ponytail: row-by-row through Python, one legacy database per call. A
    # project's run history is hundreds of rows; batch it if that ever changes.
    owned = con is None
    con = con or db.connect()
    src = sqlite3.connect(f"file:{legacy_db}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        run_cols = [c for c in _columns(src, "runs")
                    if c in _columns(con, "runs") and c != "id"]
        taken = {r["slug"] for r in con.execute(
            "SELECT slug FROM runs WHERE slug IS NOT NULL")}
        id_map: dict[int, int] = {}
        dropped_slugs = 0
        for row in src.execute("SELECT * FROM runs ORDER BY id"):
            values = {c: row[c] for c in run_cols}
            if values.get("slug") in taken:
                values["slug"] = None
                dropped_slugs += 1
            elif values.get("slug"):
                taken.add(values["slug"])
            values["parent_run"] = id_map.get(row["parent_run"])
            values["project_id"] = project_id or values.get("project_id")
            cur = con.execute(
                f"INSERT INTO runs({','.join(values)}) "
                f"VALUES({','.join('?' * len(values))})", list(values.values()))
            new_id = int(cur.lastrowid)
            id_map[int(row["id"])] = new_id
            con.execute(
                "UPDATE runs SET brief_path=?, log_path=? WHERE id=?",
                (_copy_artifact(values.get("brief_path"), paths.briefs_dir(),
                                f"run-{new_id}.md"),
                 _copy_artifact(values.get("log_path"), paths.logs_dir(),
                                f"run-{new_id}.jsonl"),
                 new_id))

        counts = {"runs": len(id_map), "dropped_slugs": dropped_slugs}
        for table, keys in (("messages", ("run_id",)),
                            ("dispatch_dependencies", ("run_id", "depends_on_run")),
                            ("deferred_dispatches", ("run_id",))):
            cols = [c for c in _columns(src, table)
                    if c in _columns(con, table) and c != "id"]
            if not cols:
                counts[table] = 0
                continue
            moved = 0
            for row in src.execute(f"SELECT * FROM {table}"):
                values = {c: row[c] for c in cols}
                if any(id_map.get(values[k]) is None for k in keys):
                    continue  # a reference to a run that did not come across
                for k in keys:
                    values[k] = id_map[values[k]]
                con.execute(
                    f"INSERT OR IGNORE INTO {table}({','.join(values)}) "
                    f"VALUES({','.join('?' * len(values))})", list(values.values()))
                moved += 1
            counts[table] = moved
        con.commit()
    finally:
        src.close()
        if owned:
            con.close()
    counts["id_map"] = id_map
    counts["skipped_tables"] = list(SKIPPED_TABLES)
    return counts
