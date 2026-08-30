"""Offline, owner-operated backup and restore for one v2 state directory."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import uuid
from pathlib import Path, PurePosixPath

from orchestra import db, paths, proc


FORMAT = "orchestra-v2-backup-1"
_DATABASE_FILES = frozenset(("orchestra.db", "orchestra.db-wal", "orchestra.db-shm"))
_DAEMON_META = ("daemon_pid", "daemon_pid_identity")


class MaintenanceError(RuntimeError):
    pass


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def _daemon_is_running(con: sqlite3.Connection) -> bool:
    raw = db.meta_get(con, "daemon_pid")
    if not raw:
        return False
    try:
        pid = int(raw)
    except ValueError:
        return False
    if not proc.alive(pid):
        return False
    expected = db.meta_get(con, "daemon_pid_identity")
    actual = proc.process_identity(pid)
    return not expected or not actual or expected == actual


def require_offline(con: sqlite3.Connection, *, require_settled: bool = False) -> None:
    if _daemon_is_running(con):
        raise MaintenanceError("stop the Orchestra daemon before offline maintenance")
    if require_settled:
        rows = con.execute(
            f"SELECT id FROM runs WHERE status NOT IN {db.TERMINAL_SQL} ORDER BY id"
        ).fetchall()
        if rows:
            ids = ", ".join(str(row["id"]) for row in rows[:10])
            suffix = "…" if len(rows) > 10 else ""
            raise MaintenanceError(
                f"backup requires every run to be terminal; active run(s): {ids}{suffix}")


def _copy_state(source: Path, target: Path) -> list[str]:
    skipped: list[str] = []
    for root, directories, filenames in os.walk(source, followlinks=False):
        current = Path(root)
        relative = current.relative_to(source)
        kept = []
        for name in directories:
            item = current / name
            rel = item.relative_to(source)
            if rel.parts and rel.parts[0] == "backups":
                continue
            if item.is_symlink():
                skipped.append(rel.as_posix())
                continue
            kept.append(name)
            (target / rel).mkdir(mode=0o700, parents=True, exist_ok=True)
        directories[:] = kept
        for name in filenames:
            item = current / name
            rel = item.relative_to(source)
            if (not rel.parts or rel.parts[0] == "backups" or
                    (len(rel.parts) == 1 and name in _DATABASE_FILES)):
                continue
            if item.is_symlink() or not item.is_file():
                skipped.append(rel.as_posix())
                continue
            destination = target / rel
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copy2(item, destination, follow_symlinks=False)
    return sorted(skipped)


def _database_info(path: Path) -> dict:
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise MaintenanceError(f"invalid backup database: {exc}") from exc
    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise MaintenanceError(f"backup database integrity check failed: {integrity}")
        meta = dict(con.execute("SELECT key,value FROM meta"))
        if meta.get("schema_version") != db.SCHEMA_VERSION:
            raise MaintenanceError("backup is not an Orchestra v2 database")
        active = int(con.execute(
            f"SELECT COUNT(*) FROM runs WHERE status NOT IN {db.TERMINAL_SQL}"
        ).fetchone()[0])
        if active:
            raise MaintenanceError("backup contains non-terminal runs")
        return {"schema_version": meta["schema_version"],
                "instance_id": meta.get("instance_id"), "integrity": integrity}
    except sqlite3.Error as exc:
        raise MaintenanceError(f"invalid backup database: {exc}") from exc
    finally:
        con.close()


def backup(destination: Path | None = None) -> dict:
    state = paths.home() / "v2"
    database = state / "orchestra.db"
    if not database.is_file():
        raise MaintenanceError("Orchestra v2 is not initialized")
    if destination is None:
        stamp = db.now().replace(":", "").replace("-", "")
        destination = paths.backups_dir() / f"orchestra-v2-{stamp}.tar.gz"
    destination = Path(destination).expanduser()
    if destination.exists():
        raise MaintenanceError(f"backup destination already exists: {destination}")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    source = db.connect(database)
    try:
        require_offline(source, require_settled=True)
        instance_id = db.instance_id(source)
        with tempfile.TemporaryDirectory(
                prefix=".orchestra-backup-", dir=destination.parent) as raw:
            bundle = Path(raw) / "orchestra-v2"
            staged_state = bundle / "state"
            staged_state.mkdir(mode=0o700, parents=True)
            skipped = _copy_state(state, staged_state)
            snapshot = sqlite3.connect(staged_state / "orchestra.db")
            try:
                source.backup(snapshot)
                snapshot.executemany("DELETE FROM meta WHERE key=?",
                                     ((key,) for key in _DAEMON_META))
                snapshot.commit()
            finally:
                snapshot.close()
            database_info = _database_info(staged_state / "orchestra.db")
            files = []
            for item in sorted(staged_state.rglob("*")):
                if item.is_file() and not item.is_symlink():
                    files.append({
                        "path": item.relative_to(staged_state).as_posix(),
                        "size": item.stat().st_size,
                        "sha256": _digest(item),
                    })
            manifest = {
                "format": FORMAT,
                "created_at": db.now(),
                "schema_version": db.SCHEMA_VERSION,
                "instance_id": instance_id,
                "database": database_info,
                "files": files,
                "skipped_symlinks": skipped,
            }
            (bundle / "manifest.json").write_text(
                json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            with tarfile.open(destination, "x:gz", dereference=False) as archive:
                archive.add(bundle, arcname="orchestra-v2", recursive=True)
    finally:
        source.close()
    if os.name == "posix":
        destination.chmod(0o600)
    return {"backup": str(destination), "instance_id": instance_id,
            "files": len(files), "bytes": destination.stat().st_size,
            "skipped_symlinks": skipped}


def _safe_member(member: tarfile.TarInfo) -> PurePosixPath:
    name = PurePosixPath(member.name)
    if (name.is_absolute() or not name.parts or name.parts[0] != "orchestra-v2" or
            any(part in ("", ".", "..") for part in name.parts)):
        raise MaintenanceError(f"unsafe backup member: {member.name}")
    if not member.isdir() and not member.isfile():
        raise MaintenanceError(f"unsupported backup member: {member.name}")
    return name


def _extract_and_validate(archive_path: Path, destination: Path) -> tuple[dict, Path]:
    seen: set[str] = set()
    try:
        archive = tarfile.open(archive_path, "r:*")
    except (OSError, tarfile.TarError) as exc:
        raise MaintenanceError(f"cannot open backup: {exc}") from exc
    with archive:
        for member in archive.getmembers():
            name = _safe_member(member)
            normalized = name.as_posix()
            if normalized in seen:
                raise MaintenanceError(f"duplicate backup member: {normalized}")
            seen.add(normalized)
            output = destination.joinpath(*name.parts)
            if member.isdir():
                output.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise MaintenanceError(f"cannot read backup member: {normalized}")
            with source, output.open("xb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
            if os.name == "posix":
                output.chmod(member.mode & 0o700 or 0o600)

    bundle = destination / "orchestra-v2"
    manifest_path = bundle / "manifest.json"
    state = bundle / "state"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MaintenanceError(f"backup has no valid manifest: {exc}") from exc
    if manifest.get("format") != FORMAT or \
            manifest.get("schema_version") != db.SCHEMA_VERSION:
        raise MaintenanceError("backup format or schema version is incompatible")
    declared = manifest.get("files")
    if not isinstance(declared, list):
        raise MaintenanceError("backup manifest has no file inventory")
    expected: set[str] = set()
    for record in declared:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise MaintenanceError("backup manifest contains an invalid file record")
        relative = PurePosixPath(record["path"])
        if relative.is_absolute() or any(part in ("", ".", "..")
                                         for part in relative.parts):
            raise MaintenanceError("backup manifest contains an unsafe file path")
        path = state.joinpath(*relative.parts)
        if relative.as_posix() in expected or not path.is_file() or path.is_symlink():
            raise MaintenanceError(f"backup file is missing or duplicated: {relative}")
        expected.add(relative.as_posix())
        if path.stat().st_size != record.get("size") or \
                _digest(path) != record.get("sha256"):
            raise MaintenanceError(f"backup file failed verification: {relative}")
    actual = {item.relative_to(state).as_posix() for item in state.rglob("*")
              if item.is_file() and not item.is_symlink()}
    if actual != expected:
        raise MaintenanceError("backup file inventory does not match its contents")
    info = _database_info(state / "orchestra.db")
    if info["instance_id"] != manifest.get("instance_id"):
        raise MaintenanceError("backup instance identity does not match its manifest")
    return manifest, state


def inspect_backup(archive_path: Path) -> dict:
    archive_path = Path(archive_path).expanduser()
    with tempfile.TemporaryDirectory(prefix=".orchestra-verify-") as raw:
        manifest, _ = _extract_and_validate(archive_path, Path(raw))
        return {"valid": True, "backup": str(archive_path),
                "instance_id": manifest["instance_id"],
                "created_at": manifest["created_at"],
                "files": len(manifest["files"])}


def _rebase_feed_revisions(database: Path, floor: int) -> int:
    """Move restored feed rows above the displaced instance's cursor floor.

    A restore may roll the database backward while paired clients retain their
    last run/attention/global cursors. Giving every restored feed row a fresh,
    unique revision makes those clients reconcile the restored truth instead
    of waiting for the old counter to catch up.
    """
    con = sqlite3.connect(database)
    try:
        con.row_factory = sqlite3.Row
        current = con.execute(
            "SELECT value FROM meta WHERE key='board_revision'"
        ).fetchone()
        try:
            counter = max(int(current["value"] if current else 0), int(floor))
        except (TypeError, ValueError):
            counter = max(0, int(floor))
        con.execute("BEGIN IMMEDIATE")
        for table in ("runs", "attention_requests"):
            for row in con.execute(f"SELECT id FROM {table} ORDER BY id"):
                counter += 1
                con.execute(
                    f"UPDATE {table} SET revision=? WHERE id=?",
                    (counter, row["id"]),
                )
        counter += 1
        con.execute(
            "INSERT INTO meta(key,value) VALUES('board_revision',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(counter),),
        )
        con.commit()
        return counter
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def restore(archive_path: Path, *, apply: bool = False) -> dict:
    archive_path = Path(archive_path).expanduser().resolve(strict=True)
    if not apply:
        return {**inspect_backup(archive_path), "applied": False}
    home = paths.home()
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    current = home / "v2"
    revision_floor = 0
    if (current / "orchestra.db").is_file():
        con = db.connect(current / "orchestra.db")
        try:
            require_offline(con)
            revision_floor = db.board_revision(con)
        finally:
            con.close()
    with tempfile.TemporaryDirectory(prefix=".orchestra-restore-", dir=home) as raw:
        manifest, staged = _extract_and_validate(archive_path, Path(raw))
        restored_revision = _rebase_feed_revisions(
            staged / "orchestra.db", revision_floor)
        archives = paths.archives_dir()
        stamp = db.now().replace(":", "").replace("-", "")
        displaced = archives / f"v2-pre-restore-{stamp}"
        if displaced.exists():
            displaced = archives / f"v2-pre-restore-{stamp}-{uuid.uuid4().hex[:8]}"
        moved = False
        try:
            if current.exists():
                os.replace(current, displaced)
                moved = True
            os.replace(staged, current)
        except BaseException:
            if moved and not current.exists() and displaced.exists():
                os.replace(displaced, current)
            raise
    if os.name == "posix":
        current.chmod(0o700)
    return {"valid": True, "applied": True, "backup": str(archive_path),
            "instance_id": manifest["instance_id"],
            "revision": restored_revision,
            "previous_state": str(displaced) if moved else None}
