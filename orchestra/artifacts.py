"""Explicit, immutable run artifact publication."""
from __future__ import annotations

import hashlib
import mimetypes
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

from orchestra import paths


_SECURE_OPEN_SUPPORTED = hasattr(os, "O_NOFOLLOW") and \
    hasattr(os, "O_DIRECTORY") and os.open in os.supports_dir_fd


class ArtifactError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class StagedArtifact:
    artifact_id: str
    run_id: int
    name: str
    source_path: str
    stored_path: str
    media_type: str
    size: int
    sha256: str


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _open_source(workdir: Path, relative_path: str) -> int:
    """Open a workdir file without ever resolving a component by pathname.

    Python exposes ``openat(2)`` through ``dir_fd``.  Walking from a stable
    root descriptor means a directory renamed and replaced by a symlink while
    publication is in progress cannot redirect the final open.
    """
    relative = Path(relative_path)
    if not relative_path or relative.is_absolute():
        raise ArtifactError("artifact path must be relative to the run workdir")
    parts = tuple(part for part in relative.parts if part not in ("", "."))
    if not parts or ".." in parts:
        raise ArtifactError("artifact path may not escape the run workdir")
    if not _SECURE_OPEN_SUPPORTED:
        raise ArtifactError(
            "secure artifact publication is unsupported on this platform")

    try:
        canonical = workdir.resolve(strict=True)
        directory_flags = getattr(os, "O_SEARCH", os.O_RDONLY) | \
            os.O_DIRECTORY | os.O_NOFOLLOW
        file_flags = os.O_RDONLY | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            directory_flags |= os.O_CLOEXEC
            file_flags |= os.O_CLOEXEC
        if hasattr(os, "O_NONBLOCK"):
            file_flags |= os.O_NONBLOCK

        directory_fd = os.open(canonical.anchor, directory_flags)
        try:
            for part in canonical.parts[1:] + parts[:-1]:
                child_fd = os.open(part, directory_flags, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = child_fd
            descriptor = os.open(parts[-1], file_flags, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise ArtifactError("artifact must be an existing workdir file") from exc

    try:
        regular = stat.S_ISREG(os.fstat(descriptor).st_mode)
    except OSError as exc:
        os.close(descriptor)
        raise ArtifactError("artifact file could not be inspected") from exc
    if not regular:
        os.close(descriptor)
        raise ArtifactError("artifact must be a regular file")
    return descriptor


def stage(run_id: int, workdir: str | Path, relative_path: str, *,
          name: str | None = None, artifact_id: str | None = None) -> StagedArtifact:
    """Copy one declared file into immutable Orchestra-owned storage."""
    artifact_id = artifact_id or str(uuid.uuid4())
    safe_name = Path(name or Path(relative_path).name).name
    if not safe_name or safe_name in (".", ".."):
        raise ArtifactError("artifact name must name a file")
    destination_dir = paths.run_artifacts_dir(run_id)
    destination = destination_dir / f"{paths.slugify(artifact_id)}-{safe_name}"
    if destination.exists():
        raise ArtifactError("artifact id already exists")
    temporary = destination.with_name(destination.name + ".partial")

    descriptor = _open_source(Path(workdir), relative_path)
    digest = hashlib.sha256()
    try:
        with os.fdopen(descriptor, "rb") as incoming, open(temporary, "xb") as outgoing:
            info = os.fstat(incoming.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ArtifactError("artifact must be a regular file")
            while chunk := incoming.read(1024 * 1024):
                digest.update(chunk)
                outgoing.write(chunk)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        os.replace(temporary, destination)
        if os.name == "posix":
            destination.chmod(0o400)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise

    size = destination.stat().st_size
    media_type = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
    return StagedArtifact(
        artifact_id=artifact_id,
        run_id=int(run_id),
        name=safe_name,
        source_path=str(Path(relative_path)),
        stored_path=str(destination),
        media_type=media_type,
        size=size,
        sha256=digest.hexdigest(),
    )


def publish(con, run_id: int, relative_path: str, *, name: str | None = None) -> dict:
    """Register one workdir file. The database never trusts a client workdir."""
    run = con.execute("SELECT id, workdir FROM runs WHERE id=?", (run_id,)).fetchone()
    if run is None:
        raise ArtifactError(f"run {run_id} does not exist")
    duplicate = con.execute(
        "SELECT artifact_id FROM artifacts WHERE run_id=? AND relative_path=?",
        (run_id, relative_path),
    ).fetchone()
    if duplicate:
        raise ArtifactError(f"{relative_path!r} is already published for run {run_id}")
    item = stage(run_id, run["workdir"], relative_path, name=name)
    try:
        con.execute(
            "INSERT INTO artifacts(artifact_id,run_id,name,relative_path,stored_path,"
            "source_path,mime_type,size_bytes,sha256,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (item.artifact_id, item.run_id, item.name, item.source_path,
             item.stored_path, str(Path(run["workdir"]) / relative_path),
             item.media_type, item.size, item.sha256, _now()),
        )
        con.commit()
    except BaseException:
        try:
            Path(item.stored_path).chmod(0o600)
            Path(item.stored_path).unlink()
        except OSError:
            pass
        raise
    return get(con, item.artifact_id) or {}


def _payload(row) -> dict:
    return {
        "id": row["artifact_id"], "artifact_id": row["artifact_id"],
        "run_id": int(row["run_id"]),
        "name": row["name"],
        "relative_path": row["relative_path"],
        "media_type": row["mime_type"], "mime_type": row["mime_type"],
        "byte_size": int(row["size_bytes"]),
        "size_bytes": int(row["size_bytes"]),
        "sha256": row["sha256"],
        "created_at": row["created_at"],
        "available": row["pruned_at"] is None,
        "pruned_at": row["pruned_at"],
    }


def get(con, artifact_id: str) -> dict | None:
    row = con.execute(
        "SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
    return _payload(row) if row else None


def for_run(con, run_id: int) -> list[dict]:
    return [_payload(row) for row in con.execute(
        "SELECT * FROM artifacts WHERE run_id=? ORDER BY created_at,artifact_id",
        (run_id,),
    )]


def stored_file(con, artifact_id: str) -> tuple[Path, dict] | None:
    row = con.execute(
        "SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
    if row is None:
        return None
    if row["pruned_at"] is not None:
        raise ArtifactError("artifact content was pruned")
    root = paths.artifacts_dir().resolve()
    candidate = Path(row["stored_path"]).resolve(strict=True)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ArtifactError("artifact storage path escaped Orchestra state") from exc
    if not candidate.is_file() or candidate.is_symlink():
        raise ArtifactError("artifact storage file is unavailable")
    return candidate, _payload(row)


def byte_range(size: int, header: str | None) -> tuple[int, int] | None:
    """Inclusive HTTP byte range, or None when the whole file was requested."""
    if not header:
        return None
    if not header.startswith("bytes=") or "," in header:
        raise ArtifactError("only one byte range is supported")
    raw_start, separator, raw_end = header[6:].partition("-")
    if not separator:
        raise ArtifactError("invalid byte range")
    try:
        if raw_start:
            start = int(raw_start)
            end = int(raw_end) if raw_end else size - 1
        else:
            suffix = int(raw_end)
            if suffix <= 0:
                raise ValueError
            start, end = max(0, size - suffix), size - 1
    except ValueError as exc:
        raise ArtifactError("invalid byte range") from exc
    if start < 0 or start >= size or end < start:
        raise ArtifactError("byte range is outside the artifact")
    return start, min(end, size - 1)
