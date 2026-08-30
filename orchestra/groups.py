"""Managed run groups: stable identity, display name, and archive state."""

import sqlite3
import uuid
from pathlib import Path

from orchestra import db, paths


def _row(con: sqlite3.Connection, selector: str):
    return con.execute(
        "SELECT * FROM run_groups WHERE group_id=? OR slug=?",
        (selector, selector),
    ).fetchone()


def find(con: sqlite3.Connection, selector: str):
    value = (selector or "").strip()
    return _row(con, value) if value else None


def all_groups(con: sqlite3.Connection, *, include_archived: bool = False):
    where = "" if include_archived else " WHERE archived=0"
    return con.execute(
        "SELECT * FROM run_groups" + where + " ORDER BY lower(name), group_id"
    ).fetchall()


def canonical_cwd(value: str) -> str:
    """Validate one daemon-host directory and return its canonical path."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("cwd must be a non-empty directory path")
    try:
        chosen = Path(value).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"cwd is unavailable: {exc}") from exc
    if not chosen.is_dir():
        raise ValueError(f"cwd {chosen} is not a directory")
    return str(chosen)


def create(
    con: sqlite3.Connection,
    name: str,
    *,
    slug: str | None = None,
    cwd: str | None = None,
    actor: str = "operator",
):
    name = (name or "").strip()
    if not name:
        raise ValueError("group name is required")
    explicit = slug is not None
    base = paths.kebab(slug if explicit else name)
    default_cwd = canonical_cwd(cwd) if cwd is not None else None
    timestamp = db.now()
    with con:
        candidate, suffix = base, 2
        while con.execute(
            "SELECT 1 FROM run_groups WHERE slug=?", (candidate,)
        ).fetchone():
            if explicit:
                raise ValueError(f"group slug {candidate!r} already exists")
            candidate, suffix = f"{base}-{suffix}", suffix + 1
        group_id = str(uuid.uuid4())
        con.execute(
            "INSERT INTO run_groups(group_id,slug,name,default_cwd,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (group_id, candidate, name, default_cwd, timestamp, timestamp),
        )
        db.record_control(
            con, actor=actor, action="group.create", outcome="ok",
            target_type="group", target_id=group_id,
            detail={"slug": candidate, "name": name},
        )
    return find(con, group_id)


def set_cwd(
    con: sqlite3.Connection,
    selector: str,
    cwd: str | None,
    *,
    expected_revision: int | None = None,
    actor: str = "operator",
):
    """Replace or clear the default used only by future root runs."""
    row = find(con, selector)
    if row is None:
        raise LookupError(f"no group matches {selector!r}")
    value = canonical_cwd(cwd) if cwd is not None else None
    if value == row["default_cwd"]:
        return row
    revision = int(row["revision"] if expected_revision is None
                   else expected_revision)
    with con:
        changed = con.execute(
            "UPDATE run_groups SET default_cwd=?,revision=revision+1,updated_at=? "
            "WHERE group_id=? AND revision=?",
            (value, db.now(), row["group_id"], revision),
        )
        if changed.rowcount != 1:
            raise RuntimeError("group changed since it was read")
        db.record_control(
            con, actor=actor, action="group.cwd.set" if value else "group.cwd.clear",
            outcome="ok", target_type="group", target_id=row["group_id"],
        )
    return find(con, row["group_id"])


def rename(
    con: sqlite3.Connection,
    selector: str,
    name: str,
    *,
    expected_revision: int | None = None,
    actor: str = "operator",
):
    row = find(con, selector)
    name = (name or "").strip()
    if row is None:
        raise LookupError(f"no group matches {selector!r}")
    if not name:
        raise ValueError("group name is required")
    revision = int(row["revision"] if expected_revision is None else expected_revision)
    with con:
        changed = con.execute(
            "UPDATE run_groups SET name=?,revision=revision+1,updated_at=? "
            "WHERE group_id=? AND revision=?",
            (name, db.now(), row["group_id"], revision),
        )
        if changed.rowcount != 1:
            raise RuntimeError("group changed since it was read")
        db.record_control(
            con, actor=actor, action="group.rename", outcome="ok",
            target_type="group", target_id=row["group_id"],
            detail={"before": row["name"], "after": name},
        )
    return find(con, row["group_id"])


def set_archived(
    con: sqlite3.Connection,
    selector: str,
    archived: bool,
    *,
    expected_revision: int | None = None,
    actor: str = "operator",
):
    row = find(con, selector)
    if row is None:
        raise LookupError(f"no group matches {selector!r}")
    revision = int(row["revision"] if expected_revision is None else expected_revision)
    with con:
        changed = con.execute(
            "UPDATE run_groups SET archived=?,revision=revision+1,updated_at=? "
            "WHERE group_id=? AND revision=?",
            (int(archived), db.now(), row["group_id"], revision),
        )
        if changed.rowcount != 1:
            raise RuntimeError("group changed since it was read")
        db.record_control(
            con, actor=actor, action="group.archive" if archived else "group.restore",
            outcome="ok", target_type="group", target_id=row["group_id"],
        )
    return find(con, row["group_id"])
