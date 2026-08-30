"""Explicit, reviewable retention for v2 run evidence."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from orchestra import db, paths, traces


KINDS = frozenset(("raw_logs", "artifacts"))


class StorageError(ValueError):
    pass


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size if path.is_file() and not path.is_symlink() else 0
    except OSError:
        return 0


def _tree_size(root: Path) -> int:
    if not root.is_dir():
        return 0
    total = 0
    for item in root.rglob("*"):
        total += _file_size(item)
    return total


def report(con) -> dict:
    if not con.in_transaction:
        reconcile(con)
    run_log_bytes = sum(_file_size(Path(row[0])) for row in con.execute(
        "SELECT log_path FROM runs WHERE log_path IS NOT NULL"))
    service_log_bytes = _tree_size(paths.logs_dir())
    observer_log_bytes = sum(_file_size(Path(row[0])) for row in con.execute(
        "SELECT log_path FROM observer_checks WHERE log_path IS NOT NULL "
        "AND log_pruned_at IS NULL"))
    artifact_bytes = sum(_file_size(Path(row[0])) for row in con.execute(
        "SELECT stored_path FROM artifacts WHERE pruned_at IS NULL"))
    checkpoint_paths = set()
    for row in con.execute(
            "SELECT diff_path FROM runs WHERE diff_path IS NOT NULL"):
        checkpoint_paths.add(Path(row[0]))
    return {
        "database_bytes": _file_size(paths.db_path()),
        "log_bytes": run_log_bytes + service_log_bytes,
        "run_log_bytes": run_log_bytes,
        "observer_log_bytes": observer_log_bytes,
        "artifact_bytes": artifact_bytes,
        "checkpoint_bytes": sum(_file_size(path) for path in checkpoint_paths),
        "worktree_bytes": _tree_size(paths.worktrees_dir()),
        "runs": int(con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]),
        "pinned_runs": int(con.execute(
            "SELECT COUNT(*) FROM evidence_pins").fetchone()[0]),
        "retention": "indefinite",
    }


def pin(con, run_id: int, *, actor: str, reason: str | None = None) -> dict:
    if not con.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone():
        raise LookupError(f"run {run_id} does not exist")
    timestamp = db.now()
    with con:
        con.execute(
            "INSERT INTO evidence_pins(run_id,reason,created_by,created_at) "
            "VALUES(?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET "
            "reason=excluded.reason,created_by=excluded.created_by,"
            "created_at=excluded.created_at",
            (run_id, reason, actor, timestamp))
        db.record_control(
            con, actor=actor, action="evidence.pin", target_type="run",
            target_id=run_id, detail={"reason": reason} if reason else None,
            outcome="ok")
    return dict(con.execute(
        "SELECT * FROM evidence_pins WHERE run_id=?", (run_id,)).fetchone())


def unpin(con, run_id: int, *, actor: str) -> bool:
    with con:
        removed = con.execute(
            "DELETE FROM evidence_pins WHERE run_id=?", (run_id,)).rowcount == 1
        db.record_control(
            con, actor=actor, action="evidence.unpin", target_type="run",
            target_id=run_id, outcome="ok" if removed else "not_pinned")
    return removed


def pin_for_run(con, run_id: int) -> dict | None:
    row = con.execute(
        "SELECT * FROM evidence_pins WHERE run_id=?", (run_id,)).fetchone()
    return dict(row) if row else None


def _safe_file(raw: str, roots: Iterable[Path]) -> Path | None:
    candidate = Path(raw)
    if candidate.is_symlink():
        return None
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None
    if not resolved.is_file():
        return None
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return resolved
        except ValueError:
            continue
    return None


def _selected_kinds(value) -> tuple[str, ...]:
    if value is None:
        return tuple(sorted(KINDS))
    if not isinstance(value, (list, tuple)) or not value:
        raise StorageError("kinds must be a non-empty list")
    selected = tuple(dict.fromkeys(str(item) for item in value))
    unknown = set(selected) - KINDS
    if unknown:
        raise StorageError("unknown prune kind(s): " + ", ".join(sorted(unknown)))
    return selected


def create_plan(con, *, actor: str, older_than_days: int = 30,
                kinds=None) -> dict:
    reconcile(con)
    try:
        days = int(older_than_days)
    except (TypeError, ValueError) as exc:
        raise StorageError("older_than_days must be an integer") from exc
    if days < 0:
        raise StorageError("older_than_days must be zero or greater")
    selected = _selected_kinds(kinds)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    rows = con.execute(
        "SELECT r.* FROM runs r LEFT JOIN evidence_pins p ON p.run_id=r.id "
        f"WHERE r.status IN {db.TERMINAL_SQL} AND r.finished_at IS NOT NULL "
        "AND r.finished_at<=? AND p.run_id IS NULL ORDER BY r.id", (cutoff,)
    ).fetchall()
    items: list[dict] = []
    log_roots = (paths.state_dir() / "runs", paths.logs_dir())
    artifact_roots = (paths.artifacts_dir(),)
    for run in rows:
        run_id = int(run["id"])
        if "raw_logs" in selected and run["log_path"]:
            path = _safe_file(run["log_path"], log_roots)
            if path:
                info = path.stat()
                items.append({
                    "kind": "raw_log", "run_id": run_id, "path": str(path),
                    "size_bytes": info.st_size, "mtime_ns": info.st_mtime_ns,
                })
        if "raw_logs" in selected:
            for check in con.execute(
                    "SELECT id,log_path FROM observer_checks WHERE run_id=? "
                    "AND finished_at IS NOT NULL AND log_path IS NOT NULL "
                    "AND log_pruned_at IS NULL ORDER BY id", (run_id,)):
                path = _safe_file(check["log_path"], log_roots)
                if path:
                    info = path.stat()
                    items.append({
                        "kind": "observer_log", "run_id": run_id,
                        "check_id": int(check["id"]), "path": str(path),
                        "size_bytes": info.st_size, "mtime_ns": info.st_mtime_ns,
                    })
        if "artifacts" in selected:
            for artifact in con.execute(
                    "SELECT * FROM artifacts WHERE run_id=? AND pruned_at IS NULL "
                    "ORDER BY artifact_id", (run_id,)):
                path = _safe_file(artifact["stored_path"], artifact_roots)
                if path:
                    items.append({
                        "kind": "artifact", "run_id": run_id,
                        "artifact_id": artifact["artifact_id"], "path": str(path),
                        "size_bytes": int(artifact["size_bytes"]),
                        "sha256": artifact["sha256"],
                    })
    plan_id = str(uuid.uuid4())
    criteria = {"older_than_days": days, "cutoff": cutoff,
                "kinds": list(selected)}
    timestamp = db.now()
    with con:
        con.execute(
            "INSERT INTO prune_plans(plan_id,criteria_json,items_json,created_by,"
            "created_at) VALUES(?,?,?,?,?)",
            (plan_id, json.dumps(criteria, separators=(",", ":")),
             json.dumps(items, separators=(",", ":")), actor, timestamp))
        db.record_control(
            con, actor=actor, action="storage.prune_plan", target_type="prune_plan",
            target_id=plan_id, detail={"items": len(items), "bytes": sum(
                item["size_bytes"] for item in items)}, outcome="dry_run")
    return get_plan(con, plan_id) or {}


def get_plan(con, plan_id: str) -> dict | None:
    row = con.execute(
        "SELECT * FROM prune_plans WHERE plan_id=?", (plan_id,)).fetchone()
    if not row:
        return None
    value = dict(row)
    value["criteria"] = json.loads(value.pop("criteria_json"))
    value["items"] = json.loads(value.pop("items_json"))
    raw_result = value.pop("result_json")
    value["result"] = json.loads(raw_result) if raw_result else None
    value["item_count"] = len(value["items"])
    value["bytes"] = sum(item["size_bytes"] for item in value["items"])
    return value


def _still_eligible(con, run_id: int, cutoff: str) -> bool:
    return con.execute(
        "SELECT 1 FROM runs r LEFT JOIN evidence_pins p ON p.run_id=r.id "
        f"WHERE r.id=? AND r.status IN {db.TERMINAL_SQL} "
        "AND r.finished_at IS NOT NULL AND r.finished_at<=? AND p.run_id IS NULL",
        (run_id, cutoff)).fetchone() is not None


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _trash_root(*, create: bool = False) -> Path:
    root = paths.state_dir() / ".prune-trash"
    return paths.owner_dir(root) if create else root


def _plan_trash_dir(plan_id: str, *, create: bool = False) -> Path:
    plan_key = hashlib.sha256(plan_id.encode()).hexdigest()
    plan_dir = _trash_root(create=create) / plan_key
    return paths.owner_dir(plan_dir) if create else plan_dir


def _staged_path(plan_id: str, item: dict, *, create: bool = False) -> Path:
    item_key = hashlib.sha256(json.dumps(
        item, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()
    return _plan_trash_dir(plan_id, create=create) / f"{item_key}.staged"


def _sync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _stage_removal(path: Path, staged: Path) -> None:
    if staged.exists() or staged.is_symlink():
        raise StorageError("planned evidence already has an unresolved staged copy")
    os.replace(path, staged)
    try:
        _sync_directory(path.parent)
        _sync_directory(staged.parent)
    except OSError:
        os.replace(staged, path)
        raise


def _discard_staged(staged: Path) -> None:
    staged.unlink()
    _sync_directory(staged.parent)
    try:
        staged.parent.rmdir()
    except OSError:
        return
    _sync_directory(staged.parent.parent)


def _metadata_pruned(con, item: dict) -> bool:
    if item["kind"] == "raw_log":
        row = con.execute(
            "SELECT t.raw_pruned_at FROM runs r LEFT JOIN trace_cursors t "
            "ON t.run_id=r.id WHERE r.id=?",
            (item["run_id"],)).fetchone()
        return row is None or row["raw_pruned_at"] is not None
    if item["kind"] == "observer_log":
        row = con.execute(
            "SELECT log_pruned_at FROM observer_checks WHERE id=? AND run_id=?",
            (item["check_id"], item["run_id"])).fetchone()
        return row is None or row["log_pruned_at"] is not None
    if item["kind"] == "artifact":
        row = con.execute(
            "SELECT pruned_at FROM artifacts WHERE artifact_id=? AND run_id=?",
            (item["artifact_id"], item["run_id"])).fetchone()
        return row is None or row["pruned_at"] is not None
    raise StorageError("plan contains an unknown item kind")


def _item_roots(item: dict) -> tuple[Path, ...]:
    return (paths.artifacts_dir(),) if item["kind"] == "artifact" else (
        paths.state_dir() / "runs", paths.logs_dir())


def _matches_plan(path: Path, item: dict) -> bool:
    try:
        info = path.stat()
    except OSError:
        return False
    if info.st_size != item["size_bytes"]:
        return False
    if item["kind"] == "artifact":
        return _digest(path) == item["sha256"]
    return info.st_mtime_ns == item["mtime_ns"]


def _restore_staged(staged: Path, item: dict) -> None:
    try:
        info = staged.lstat()
    except OSError as exc:
        raise StorageError("staged evidence is unavailable") from exc
    if not stat.S_ISREG(info.st_mode):
        raise StorageError("staged evidence is not a regular file")
    if info.st_size != item["size_bytes"]:
        raise StorageError("staged evidence changed during recovery")
    if item["kind"] == "artifact" and _digest(staged) != item["sha256"]:
        raise StorageError("staged artifact changed during recovery")

    original = Path(item["path"])
    parent = original.parent.resolve(strict=True)
    if not any(_within(parent, root.resolve()) for root in _item_roots(item)):
        raise StorageError("staged evidence destination escaped Orchestra state")
    current = _safe_file(str(original), _item_roots(item))
    if current:
        if not _matches_plan(current, item):
            raise StorageError(
                "retained path conflicts with its staged evidence copy")
        _discard_staged(staged)
        return
    if original.exists() or original.is_symlink():
        raise StorageError("retained path is no longer a regular evidence file")
    os.replace(staged, original)
    _sync_directory(original.parent)
    _sync_directory(staged.parent)
    try:
        staged.parent.rmdir()
    except OSError:
        pass


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _reconcile_item(con, plan_id: str, item: dict) -> str | None:
    staged = _staged_path(plan_id, item)
    if not staged.exists() and not staged.is_symlink():
        return None
    if _metadata_pruned(con, item):
        _discard_staged(staged)
        return "discarded"
    _restore_staged(staged, item)
    return "restored"


def reconcile(con, plan_id: str | None = None) -> dict:
    """Finish or roll back interrupted prune renames from durable DB truth."""
    if not _trash_root().exists():
        return {"restored": 0, "discarded": 0}
    if db.in_api_mutation(con):
        return _reconcile_locked(con, plan_id)
    with db.api_mutation(con):
        return _reconcile_locked(con, plan_id)


def _reconcile_locked(con, plan_id: str | None) -> dict:
    root = _trash_root()
    if not root.exists():
        return {"restored": 0, "discarded": 0}
    if plan_id is None:
        rows = con.execute(
            "SELECT plan_id,items_json FROM prune_plans ORDER BY created_at,plan_id"
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT plan_id,items_json FROM prune_plans WHERE plan_id=?",
            (plan_id,)).fetchall()
    result = {"restored": 0, "discarded": 0}
    for row in rows:
        for item in json.loads(row["items_json"]):
            action = _reconcile_item(con, row["plan_id"], item)
            if action:
                result[action] += 1
        try:
            _plan_trash_dir(row["plan_id"]).rmdir()
        except OSError:
            pass
    try:
        root.rmdir()
    except OSError:
        pass
    return result


def apply_plan(con, plan_id: str, *, actor: str) -> dict:
    if db.in_api_mutation(con):
        reconcile(con, plan_id)
        return _apply_plan(con, plan_id, actor=actor)
    try:
        with db.api_mutation(con):
            reconcile(con, plan_id)
            _apply_plan(con, plan_id, actor=actor)
    finally:
        reconcile(con, plan_id)
    return get_plan(con, plan_id) or {}


def _apply_plan(con, plan_id: str, *, actor: str) -> dict:
    if not db.in_api_mutation(con):
        raise RuntimeError("prune apply requires an atomic mutation")
    plan = get_plan(con, plan_id)
    if plan is None:
        raise LookupError("prune plan does not exist")
    if plan["applied_at"]:
        return plan
    cutoff = plan["criteria"]["cutoff"]
    results: list[dict] = []
    log_roots = (paths.state_dir() / "runs", paths.logs_dir())
    artifact_roots = (paths.artifacts_dir(),)
    for item in plan["items"]:
        outcome = {"kind": item["kind"], "run_id": item["run_id"]}
        if item.get("artifact_id"):
            outcome["artifact_id"] = item["artifact_id"]
        if item.get("check_id"):
            outcome["check_id"] = item["check_id"]
        if not _still_eligible(con, item["run_id"], cutoff):
            outcome.update(status="skipped", reason="run changed or evidence is pinned")
            results.append(outcome)
            continue
        con.execute("SAVEPOINT api_prune_item")
        staged = None
        try:
            if item["kind"] == "raw_log":
                row = con.execute(
                    "SELECT log_path FROM runs WHERE id=?", (item["run_id"],)
                ).fetchone()
                path = _safe_file(item["path"], log_roots)
                if not row or not path or \
                        Path(row["log_path"]).resolve() != path:
                    raise StorageError("planned log is no longer the retained log")
                info = path.stat()
                if info.st_size != item["size_bytes"] or \
                        info.st_mtime_ns != item["mtime_ns"]:
                    raise StorageError("planned log changed after review")
                traces.ingest(con, item["run_id"])
                staged = _staged_path(plan_id, item, create=True)
                _stage_removal(path, staged)
                timestamp = db.now()
                con.execute(
                    "INSERT INTO trace_cursors(run_id,byte_offset,seq,skipped,"
                    "raw_pruned_at,updated_at) VALUES(?,0,0,0,?,?) ON CONFLICT(run_id) "
                    "DO UPDATE SET raw_pruned_at=excluded.raw_pruned_at,"
                    "updated_at=excluded.updated_at",
                    (item["run_id"], timestamp, timestamp))
            elif item["kind"] == "observer_log":
                row = con.execute(
                    "SELECT run_id,log_path,finished_at,log_pruned_at FROM "
                    "observer_checks WHERE id=?", (item["check_id"],)
                ).fetchone()
                path = _safe_file(item["path"], log_roots)
                if not row or row["run_id"] != item["run_id"] or \
                        row["finished_at"] is None or row["log_pruned_at"] or \
                        not path or Path(row["log_path"]).resolve() != path:
                    raise StorageError(
                        "planned Observer log is no longer the retained log")
                info = path.stat()
                if info.st_size != item["size_bytes"] or \
                        info.st_mtime_ns != item["mtime_ns"]:
                    raise StorageError("planned Observer log changed after review")
                staged = _staged_path(plan_id, item, create=True)
                _stage_removal(path, staged)
                con.execute(
                    "UPDATE observer_checks SET log_pruned_at=? WHERE id=?",
                    (db.now(), item["check_id"]))
            elif item["kind"] == "artifact":
                row = con.execute(
                    "SELECT * FROM artifacts WHERE artifact_id=? AND pruned_at IS NULL",
                    (item["artifact_id"],)).fetchone()
                path = _safe_file(item["path"], artifact_roots)
                if not row or row["run_id"] != item["run_id"] or \
                        not path or Path(row["stored_path"]).resolve() != path:
                    raise StorageError("planned artifact is no longer retained")
                if path.stat().st_size != item["size_bytes"] or \
                        _digest(path) != item["sha256"]:
                    raise StorageError("planned artifact changed after review")
                staged = _staged_path(plan_id, item, create=True)
                _stage_removal(path, staged)
                con.execute(
                    "UPDATE artifacts SET pruned_at=? WHERE artifact_id=?",
                    (db.now(), item["artifact_id"]))
            else:
                raise StorageError("plan contains an unknown item kind")
            con.execute("RELEASE SAVEPOINT api_prune_item")
            outcome.update(status="pruned", bytes=item["size_bytes"])
        except (OSError, StorageError) as exc:
            if con.in_transaction:
                con.execute("ROLLBACK TO SAVEPOINT api_prune_item")
                con.execute("RELEASE SAVEPOINT api_prune_item")
            if staged is not None:
                try:
                    _reconcile_item(con, plan_id, item)
                except (OSError, StorageError) as recovery_error:
                    raise StorageError(
                        f"prune failed and staged evidence recovery failed: "
                        f"{recovery_error}") from exc
            reason = str(exc) if isinstance(exc, StorageError) else \
                "evidence file operation failed"
            outcome.update(status="skipped", reason=reason)
        results.append(outcome)
    summary = {
        "items": results,
        "pruned_items": sum(item["status"] == "pruned" for item in results),
        "pruned_bytes": sum(item.get("bytes", 0) for item in results),
        "skipped_items": sum(item["status"] != "pruned" for item in results),
    }
    timestamp = db.now()
    with con:
        con.execute(
            "UPDATE prune_plans SET applied_by=?,applied_at=?,result_json=? "
            "WHERE plan_id=? AND applied_at IS NULL",
            (actor, timestamp, json.dumps(summary, separators=(",", ":")), plan_id))
        db.record_control(
            con, actor=actor, action="storage.prune_apply",
            target_type="prune_plan", target_id=plan_id, detail=summary,
            outcome="ok" if not summary["skipped_items"] else "partial")
    return get_plan(con, plan_id) or {}
