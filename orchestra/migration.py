"""One-way v1 retirement. No v1 shape reaches the running v2 daemon."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from orchestra import fleet_config, paths


LEGACY_NAMES = (
    "orchestra.db", "orchestra.db-shm", "orchestra.db-wal",
    "logs", "briefs", "projects", "worktrees", "backups", "hooks",
)


class MigrationError(RuntimeError):
    pass


_SECRET = re.compile(r"token|secret|password|credential|api.?key|cookie", re.I)
_TIERS = {"cheap": 1, "low": 1, "workhorse": 1, "mid": 2, "medium": 2,
          "generalist": 2, "high": 3, "heavy": 3, "frontier": 3}
_IMPORTABLE_BUILTIN_RUNTIMES = frozenset({
    "codex", "claude", "opencode", "reasonix",
})
_LEGACY_HOOK_SUFFIXES = ("", " --bind", " --event PostCompact")


def _legacy_hook(command, backend: str) -> bool:
    base = f"orchestra hook --backend {backend}"
    return isinstance(command, str) and command in {
        base + suffix for suffix in _LEGACY_HOOK_SUFFIXES
    }


def _without_legacy_hooks(value, backend: str) -> tuple[object | None, int]:
    """Remove our exact old handler from either supported harness shape."""
    if not isinstance(value, dict):
        return value, 0
    nested = value.get("hooks")
    if isinstance(nested, list):
        kept = [item for item in nested if not (
            isinstance(item, dict) and
            _legacy_hook(item.get("command"), backend)
        )]
        removed = len(nested) - len(kept)
        if not removed:
            return value, 0
        if not kept:
            return None, removed
        changed = dict(value)
        changed["hooks"] = kept
        return changed, removed
    if _legacy_hook(value.get("command"), backend):
        return None, 1
    return value, 0


def _write_json_atomic(path: Path, value: dict) -> None:
    _write_atomic(path, json.dumps(value, indent=2) + "\n")


def _write_atomic(path: Path, text: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "posix":
            os.chmod(temporary, path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _codex_trust_records(hooks_path: Path, hooks: dict) -> list[tuple[str, str]]:
    records = []
    resolved = hooks_path.resolve()
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        for group_index, group in enumerate(groups):
            handlers = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(handlers, list):
                continue
            for handler_index, handler in enumerate(handlers):
                if not isinstance(handler, dict) or not _legacy_hook(
                        handler.get("command"), "codex"):
                    continue
                key = f"{resolved}:{event.lower()}:{group_index}:{handler_index}"
                digest = "sha256:" + hashlib.sha256(json.dumps(
                    handler, sort_keys=True, separators=(",", ":")
                ).encode()).hexdigest()
                records.append((key, digest))
    return records


def _without_codex_trust(text: str,
                         records: list[tuple[str, str]]) -> tuple[str, int]:
    """Delete only trust tables whose key and handler hash both match v1."""
    def header(key: str) -> str:
        escaped = key.replace("\\", "\\\\").replace('"', '\\"')
        return f'[hooks.state."{escaped}"]'

    wanted = {header(key): digest for key, digest in records}
    lines = text.splitlines(keepends=True)
    kept, removed, index = [], 0, 0
    while index < len(lines):
        header = lines[index].strip()
        if header not in wanted:
            kept.append(lines[index])
            index += 1
            continue
        end = index + 1
        while end < len(lines) and not re.match(r"^\s*\[", lines[end]):
            end += 1
        block = lines[index:end]
        digest = wanted[header]
        if any(line.strip() == f'trusted_hash = "{digest}"' for line in block):
            removed += 1
        else:
            kept.extend(block)
        index = end
    return "".join(kept), removed


def retire_legacy_hooks(*, execute: bool = False,
                        configs: list[tuple[str, Path]] | None = None) -> list[dict]:
    """Plan or remove only v1 hook commands Orchestra installed globally."""
    targets = configs if configs is not None else [
        ("claude", paths.claude_settings_path()),
        ("codex", paths.codex_home() / "hooks.json"),
        ("reasonix", paths.reasonix_settings_path()),
    ]
    report = []
    for backend, path in targets:
        item = {"backend": backend, "path": str(path), "found": 0,
                "removed": 0}
        if not path.is_file():
            report.append(item)
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            hooks = document.get("hooks") if isinstance(document, dict) else None
            if not isinstance(hooks, dict):
                raise ValueError('root "hooks" field is not an object')
            trust_records = _codex_trust_records(path, hooks) \
                if backend == "codex" else []
            changed = dict(hooks)
            removed = 0
            for event, entries in hooks.items():
                if not isinstance(entries, list):
                    continue
                kept = []
                for entry in entries:
                    replacement, count = _without_legacy_hooks(entry, backend)
                    removed += count
                    if replacement is not None:
                        kept.append(replacement)
                changed[event] = kept
            item["found"] = removed
            if execute and removed:
                updated = dict(document)
                updated["hooks"] = changed
                _write_json_atomic(path, updated)
                item["removed"] = removed
            if backend == "codex" and trust_records:
                trust_path = path.with_name("config.toml")
                if trust_path.is_file():
                    trust_text = trust_path.read_text(encoding="utf-8")
                    cleaned, trust_removed = _without_codex_trust(
                        trust_text, trust_records)
                    item["trust_found"] = trust_removed
                    item["trust_removed"] = 0
                    if execute and trust_removed:
                        _write_atomic(trust_path, cleaned)
                        item["trust_removed"] = trust_removed
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            item["error"] = str(exc)
        report.append(item)
    return report


def legacy_items(base: Path | None = None) -> list[Path]:
    root = (base or paths.home()).resolve()
    candidates = [root / "fleet", *(root / name for name in LEGACY_NAMES)]
    return [item for item in candidates if item.exists() or item.is_symlink()]


def archive_legacy(*, execute: bool = False, base: Path | None = None,
                   stamp: str | None = None) -> dict:
    """Plan or perform a narrow move of known legacy state into an archive."""
    root = (base or paths.home()).resolve()
    when = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = root / "archives" / f"pre-v2-{when}"
    items = legacy_items(root)
    moves = [{"from": str(item), "to": str(destination / item.name)}
             for item in items]
    report = {
        "archive": str(destination),
        "execute": execute,
        "moves": moves,
        "note": "v1 execution history is archived, never imported into v2",
    }
    if not execute or not items:
        return report
    if destination.exists():
        raise MigrationError(f"archive already exists: {destination}")
    destination.mkdir(mode=0o700, parents=True)
    completed: list[tuple[Path, Path]] = []
    try:
        for item in items:
            if item.is_symlink():
                raise MigrationError(f"refusing to archive symbolic link: {item}")
            target = destination / item.name
            os.replace(item, target)
            completed.append((item, target))
        manifest = destination / "manifest.json"
        manifest.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        if os.name == "posix":
            manifest.chmod(0o600)
    except BaseException:
        for original, archived in reversed(completed):
            try:
                os.replace(archived, original)
            except OSError:
                pass
        try:
            destination.rmdir()
        except OSError:
            pass
        raise
    return report


def _clean_mapping(value, dropped: list[str], path: str) -> dict:
    if not isinstance(value, dict):
        return {}
    clean = {}
    for key, item in value.items():
        if _SECRET.search(str(key)):
            dropped.append(f"{path}.{key}: secret-bearing value not imported")
        elif isinstance(item, dict):
            clean[key] = _clean_mapping(item, dropped, f"{path}.{key}")
        else:
            clean[key] = item
    return clean


def _tier(value) -> int:
    if isinstance(value, int) and value in (1, 2, 3):
        return value
    return _TIERS.get(str(value or "").lower(), 2)


def operator_import_plan(config_path: Path | None = None,
                         legacy_db: Path | None = None) -> dict:
    """Read old operator config offline and produce a redacted v2 plan."""
    config_path = config_path or paths.global_config_path()
    dropped: list[str] = []
    try:
        top = tomllib.loads(config_path.read_text(encoding="utf-8")) \
            if config_path.is_file() else {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise MigrationError(f"cannot read legacy config {config_path}: {exc}") from exc
    old_profiles = top.get("profiles") or top.get("agents") or {}
    runtimes, profiles, sources = {}, [], {}
    for name, raw in old_profiles.items():
        if not isinstance(raw, dict):
            continue
        backend = str(raw.get("backend") or "").strip().lower()
        if not backend:
            dropped.append(f"profile {name}: no harness/backend")
            continue
        if str(raw.get("transport") or "").strip().lower() == "acp":
            dropped.append(
                f"profile {name}: ACP transport requires an explicit v2 argv "
                "runtime and was not imported")
            continue
        if backend not in _IMPORTABLE_BUILTIN_RUNTIMES:
            dropped.append(
                f"profile {name}: unsupported legacy backend {backend!r}; "
                "create an exec or ACP runtime explicitly")
            continue
        runtimes.setdefault(backend, {"name": backend.title(), "slug": backend,
                                     "adapter": backend})
        model = raw.get("model")
        provider = backend if backend in {"codex", "claude"} else \
            str(model or "").split("/", 1)[0].lower()
        lane = str(raw.get("lane") or "")
        source_slug = None
        if provider in {"codex", "claude", "deepseek", "kimi", "minimax", "xai"}:
            key = (provider, "", lane)
            source_slug = "-".join(part for part in key if part) or provider
            sources.setdefault(key, {"name": provider.title() +
                                      (f" {lane}" if lane else ""),
                                     "slug": source_slug, "provider": provider,
                                     "account": "", "lane": lane,
                                     "adapter": provider})
        elif provider:
            dropped.append(f"profile {name}: custom runway provider {provider!r} "
                           "needs an explicit argv adapter")
        env = _clean_mapping(raw.get("env") or {}, dropped, f"profiles.{name}.env")
        recognized = {"backend", "transport", "model", "effort", "tier", "priority",
                      "sandbox", "timeout", "max_concurrency", "note", "lane", "env"}
        extra = _clean_mapping({key: value for key, value in raw.items()
                                if key not in recognized}, dropped,
                               f"profiles.{name}.config")
        profiles.append({
            "name": str(name), "slug": paths.kebab(str(name)), "runtime": backend,
            "tier": _tier(raw.get("tier")), "model": model,
            "effort": raw.get("effort"), "priority": int(raw.get("priority", 0)),
            "sandbox": raw.get("sandbox"), "timeout_seconds": raw.get("timeout"),
            "max_concurrency": raw.get("max_concurrency"),
            "runway_source": source_slug, "env": env, "config": extra,
            "note": raw.get("note"),
        })
    settings = {}
    old_settings = top.get("settings") or {}
    if "max_active_runs" in old_settings:
        settings["max_active_runs"] = old_settings["max_active_runs"]
    observer = None
    if old_settings.get("observer_profile"):
        selector = str(old_settings["observer_profile"])
        selected = next((item for item in profiles if selector in {
            item["name"], item["slug"],
        }), None)
        if selected is None:
            dropped.append(
                f"observer profile {selector!r} was not imported; Observer "
                "configuration was not imported")
        elif selected["runtime"] not in fleet_config.OBSERVER_ADAPTERS:
            dropped.append(
                f"observer profile {selector!r}: {selected['runtime']} runtime "
                "cannot provide a tool-free Observer; Observer configuration "
                "was not imported")
        else:
            observer = {
                "enabled": True, "profile": selected["slug"],
                "first_look_seconds": int(
                    old_settings.get("observer_first_look", 300)),
                "minimum_events": 5,
                "interval_seconds": int(
                    old_settings.get("observer_interval", 1800)),
                "authority": "correct_then_stop",
            }

    groups = []
    candidate = legacy_db
    if candidate is None:
        for root in paths.legacy_state_candidates():
            if (root / "orchestra.db").is_file():
                candidate = root / "orchestra.db"
                break
    if candidate and candidate.is_file():
        try:
            legacy = sqlite3.connect(f"file:{candidate}?mode=ro", uri=True)
            legacy.row_factory = sqlite3.Row
            if legacy.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='projects'"
            ).fetchone():
                columns = {row[1] for row in legacy.execute(
                    "PRAGMA table_info(projects)")}
                if "root" in columns:
                    project_rows = legacy.execute(
                        "SELECT project_id,name,root,archived FROM projects ORDER BY name")
                else:
                    project_rows = legacy.execute(
                        "SELECT p.project_id,p.name,p.archived,("
                        "SELECT r.repo FROM runs r WHERE r.project_id=p.project_id "
                        "AND r.repo IS NOT NULL AND r.repo<>'' "
                        "ORDER BY r.started_at DESC,r.id DESC LIMIT 1) AS root "
                        "FROM projects p ORDER BY p.name")
                archived_roots = [candidate.parent / name for name in LEGACY_NAMES
                                  if name != "orchestra.db"]
                for row in project_rows:
                    root = Path(row["root"] or "").expanduser()
                    resolved = root.resolve() if row["root"] else None
                    archived_with_v1 = resolved is not None and any(
                        resolved == base.resolve() or base.resolve() in resolved.parents
                        for base in archived_roots)
                    if row["archived"] or resolved is None or archived_with_v1 \
                            or not resolved.is_dir():
                        continue
                    groups.append({"name": row["name"],
                                   "slug": paths.kebab(row["name"]),
                                   "cwd": str(resolved)})
            legacy.close()
        except sqlite3.Error as exc:
            dropped.append(f"legacy project directories not read: {exc}")
    return {
        "source_config": str(config_path),
        "source_database": str(candidate) if candidate else None,
        "runtimes": list(runtimes.values()), "runway_sources": list(sources.values()),
        "profiles": profiles, "settings": settings, "observer": observer,
        "groups": groups, "dropped": dropped,
        "never_imported": ["runs", "messages", "events", "numbering", "Nod",
                           "source state", "landing/merge state", "shared keys"],
    }


def apply_operator_import(con, plan: dict, *, actor: str = "v1-import") -> dict:
    """Apply a previously inspected plan to an empty or partially built v2 DB."""
    from orchestra import fleet_config, groups

    made = {"runtimes": [], "runway_sources": [], "profiles": [], "groups": [],
            "settings": [], "observer": False}
    for item in plan.get("runtimes", []):
        if fleet_config.find_runtime(con, item["slug"]) is None:
            row = fleet_config.create_runtime(con, actor=actor, **item)
            made["runtimes"].append(row["runtime_id"])
    for item in plan.get("runway_sources", []):
        if fleet_config.find_runway_source(con, item["slug"]) is None:
            row = fleet_config.create_runway_source(con, actor=actor, **item)
            made["runway_sources"].append(row["source_id"])
    for item in plan.get("profiles", []):
        if fleet_config.find_profile(con, item["slug"]) is None:
            clean = dict(item)
            if clean.get("runway_source") and fleet_config.find_runway_source(
                    con, clean["runway_source"]) is None:
                clean["runway_source"] = None
            row = fleet_config.create_profile(con, actor=actor, **clean)
            made["profiles"].append(row["profile_id"])
    for item in plan.get("groups", []):
        if groups.find(con, item["slug"]) is None:
            row = groups.create(con, actor=actor, **item)
            made["groups"].append(row["group_id"])
    for key, value in plan.get("settings", {}).items():
        fleet_config.set_fleet_setting(con, key, value, actor=actor)
        made["settings"].append(key)
    observer = plan.get("observer")
    if observer and fleet_config.find_profile(con, observer.get("profile") or ""):
        fleet_config.configure_observer(con, actor=actor, **observer)
        made["observer"] = True
    return made
