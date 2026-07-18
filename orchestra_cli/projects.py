"""Multi-project Orchestra UI control plane.

Explicit allowlist of canonical Orchestra project roots — adapted (not
ported) from `~/.work/roots.json` in slash-work. Differences from the
reference design:

  * No filesystem browsing and no unbounded scan. A root is only
    "available" if it is *registered* in the registry file AND it still
    has a `.orchestra/` directory. The UI never opens a folder picker.
  * Stable project IDs are a 16-char SHA-256 prefix of the
    realpath-resolved canonical root. Two different realpaths always
    produce different IDs, so renaming a registered root's display name
    or moving it underneath the same canonical path does not change its
    identity. This is the same shape as slash-work's deterministic IDs
    but without that project's initialization ceremony.
  * The registry is a single JSON file at
    ``~/.config/orchestra/projects.json`` (override with
    ``ORCHESTRA_PROJECTS_FILE``). Format:

        {"version": 1, "roots": [{"id": "...", "name": "...", "root": "..."}]}

The HTTP layer (``orchestra_cli.ui``) consumes :func:`list_available`
and :func:`resolve_selection` to enforce the boundary on every request.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from orchestra_cli import paths

REGISTRY_VERSION = 1


def registry_path() -> Path:
    """Where the projects allowlist lives.

    Honors ``ORCHESTRA_PROJECTS_FILE`` (tests set this to a tmp_path)
    and otherwise sits next to the global roster config so a single
    ``~/.config/orchestra/`` directory holds every Orchestra-controlled
    file.
    """
    env = os.environ.get("ORCHESTRA_PROJECTS_FILE")
    if env:
        return Path(env).expanduser()
    return paths.global_config_path().parent / "projects.json"


def project_id(root: Path) -> str:
    """Stable 16-char id derived from the realpath-resolved project root.

    Symlinks are resolved before hashing so a project reached via
    ``~/Code/foo`` and ``/workspace/foo`` (one a symlink to the
    other) collapses to a single canonical identity.
    """
    return hashlib.sha256(str(_canonical(root)).encode("utf-8")).hexdigest()[:16]


def is_orchestra_root(path: Path) -> bool:
    """True iff ``path`` looks like an initialized Orchestra project.

    This is the only thing that distinguishes a "registered" root from
    an "available" one: a registered root may have been deleted on disk
    while we weren't looking; it stays in the registry (so it can be
    unregistered) but the UI marks it unavailable and refuses to route
    API traffic to it.
    """
    try:
        return (Path(path) / paths.STATE_DIR).is_dir()
    except (OSError, ValueError):
        return False


def list_registered() -> list[dict]:
    """Every entry in the registry, in insertion order, deduped by id.

    Roots that have disappeared from disk are still returned (the
    caller decides whether to surface them). ``list_available`` is the
    filtered view the UI serves. Malformed entries (non-dict, missing
    ``root``, unparseable id) are silently skipped — the registry is a
    file humans edit by hand sometimes, and one bad row must not brick
    the picker.
    """
    data = _read(registry_path())
    seen: set[str] = set()
    out: list[dict] = []
    for entry in data["roots"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("root"), str):
            continue
        try:
            root = _canonical(Path(entry["root"]).expanduser())
        except (OSError, ValueError):
            continue
        try:
            pid = entry.get("id") or project_id(root)
            if not isinstance(pid, str) or not pid:
                continue
        except Exception:
            continue
        if pid in seen:
            continue
        seen.add(pid)
        name = entry.get("name")
        out.append({
            "id": pid,
            "name": (name if isinstance(name, str) and name else root.name),
            "root": str(root),
        })
    return out


def list_available() -> list[dict]:
    """Registered roots that still exist as Orchestra projects on disk."""
    out: list[dict] = []
    for entry in list_registered():
        root_path = Path(entry["root"])
        if is_orchestra_root(root_path):
            entry = dict(entry)
            entry["available"] = True
            out.append(entry)
    return out


def register(root: Path, name: str | None = None) -> dict:
    """Add ``root`` to the registry. The root MUST already be an
    initialized Orchestra project (have ``.orchestra/``); we never
    auto-create one — that's an explicit ``orchestra init`` decision.

    Re-registering an existing root (by canonical path or id) updates
    its display name in place rather than producing a duplicate.
    """
    root_path = _canonical(Path(root).expanduser())
    if not is_orchestra_root(root_path):
        raise NotAnOrchestraRoot(str(root_path))
    pid = project_id(root_path)
    data = _read(registry_path())
    data["roots"] = [e for e in data["roots"] if e.get("id") != pid]
    data["roots"].append({
        "id": pid,
        "name": (name or root_path.name),
        "root": str(root_path),
    })
    _atomic_write(registry_path(), data)
    return {"id": pid, "name": (name or root_path.name), "root": str(root_path)}


def unregister(id_or_root: str) -> bool:
    """Remove a root from the registry by id or canonical path.

    Returns True iff something was removed. Idempotent: re-invoking
    with the same argument returns False without raising.
    """
    data = _read(registry_path())
    before = len(data["roots"])
    canonical: str | None
    try:
        canonical = str(_canonical(Path(id_or_root).expanduser()))
    except (OSError, ValueError):
        canonical = None
    data["roots"] = [
        e for e in data["roots"]
        if e.get("id") != id_or_root
        and (canonical is None or e.get("root") != canonical)
    ]
    if len(data["roots"]) == before:
        return False
    _atomic_write(registry_path(), data)
    return True


class NotAnOrchestraRoot(ValueError):
    """Raised by :func:`register` when the path lacks ``.orchestra/``."""


class UnknownProjectError(KeyError):
    """Raised by :func:`resolve_selection` when an explicit project id
    is not in the allowlist. Carries the bad id on ``self.project_id``
    so the HTTP layer can surface it verbatim — ``KeyError``'s built-in
    ``str()`` mangles quotes around the value, which is fine for
    tracebacks but ugly in JSON responses."""
    def __init__(self, project_id: str):
        super().__init__(project_id)
        self.project_id = project_id


def resolve_selection(
    allowed: list[dict],
    requested: str | None,
    default_id: str | None,
) -> dict:
    """Pick the project a request should be served from.

    * ``requested`` is whatever the client sent (the
      ``X-Orchestra-Project`` header value or ``?project=`` query) —
      already trimmed by the caller. ``None`` / empty means "no
      preference, use the default".
    * ``default_id`` is the id of the root ``orchestra ui`` was
      launched from (if that root is in the registry), else ``None``.
    * ``allowed`` is the live allowlist (typically
      :func:`list_available`).

    Raises :class:`UnknownProjectError` if ``requested`` is set but not
    in ``allowed``. The HTTP layer translates that to a 404 so a stale
    bookmarked project id surfaces clearly instead of silently falling
    through to the default. Raises :class:`LookupError` if ``allowed``
    is empty — the UI process should refuse to start without at least
    one available project.
    """
    if not allowed:
        raise LookupError("no orchestra projects are registered and available")
    by_id = {e["id"]: e for e in allowed}
    if requested:
        if requested not in by_id:
            raise UnknownProjectError(requested)
        return by_id[requested]
    if default_id and default_id in by_id:
        return by_id[default_id]
    return allowed[0]


# --- internals -----------------------------------------------------------

def _canonical(path: Path) -> Path:
    """Resolve symlinks and normalize. Falls back to absolute-resolved
    when the path doesn't exist on disk (so a just-deleted root still
    produces the same id it had while live)."""
    try:
        return path.resolve(strict=True)
    except (FileNotFoundError, OSError):
        return Path(path).resolve(strict=False)


def _read(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"version": REGISTRY_VERSION, "roots": []}
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"projects registry is not valid JSON: {path}") from exc
    if data.get("version") != REGISTRY_VERSION or not isinstance(data.get("roots"), list):
        raise ValueError(f"projects registry has unexpected shape: {path}")
    return data


def _atomic_write(path: Path, data: dict) -> None:
    # Mode 0700 on the directory matches slash-work's workspace registry
    # — the file lists project roots the user trusts this service to
    # open, so we keep it private to the user account that owns it.
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except (OSError, PermissionError):
        # Existing shared/system dirs may not be owner-only; we don't
        # fight that, but a freshly-created one (the common case) is.
        pass
    payload = json.dumps(data, indent=2, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
