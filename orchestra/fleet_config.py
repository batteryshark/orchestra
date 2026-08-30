"""SQLite-managed runtimes, profiles, runway sources, and fleet settings."""

import json
import re
import sqlite3
import uuid
from urllib.parse import parse_qsl, urlsplit

from orchestra import db, paths, runway


RUNTIME_ADAPTERS = frozenset({
    "codex", "claude", "opencode", "reasonix", "exec", "acp",
})
COMMAND_RUNTIME_ADAPTERS = frozenset({"exec", "acp"})
OBSERVER_ADAPTERS = frozenset({"claude", "opencode", "reasonix"})
SOURCE_ADAPTERS = frozenset((*runway.BUILTIN_SOURCE_ADAPTERS, "command"))
_SECRET_OPTION = re.compile(
    r"^--?(?:api[-_]?key|access[-_]?token|auth(?:orization)?|password|"
    r"secret|credential|cookie)(?:=|$)", re.I)
_SECRET_WORDS = {"token", "key", "secret", "password", "credential",
                 "credentials", "cookie", "authorization"}


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _argv(value) -> str:
    if value is None:
        return "[]"
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(part, str) and part for part in value
    ):
        raise ValueError("command must be an argv list of non-empty strings")
    for part in value:
        lowered = part.strip().lower()
        if _SECRET_OPTION.match(lowered) or lowered.startswith(
                ("authorization:", "proxy-authorization:", "cookie:")):
            raise ValueError(
                "command argv must reference credentials through the daemon "
                "environment, harness store, or 0600 secret file")
        if "=" in part:
            key = part.split("=", 1)[0]
            words = set(filter(None, re.split(r"[^a-z0-9]+", key.lower())))
            if words & _SECRET_WORDS:
                raise ValueError(
                    "command argv must not contain credential assignments")
        try:
            parsed = urlsplit(part)
        except ValueError:
            continue
        if parsed.scheme and (parsed.username is not None or
                              parsed.password is not None):
            raise ValueError("command argv must not contain URL credentials")
        if parsed.scheme and any(
                set(filter(None, re.split(r"[^a-z0-9]+", key.lower()))) &
                _SECRET_WORDS for key, _ in parse_qsl(
                    parsed.query, keep_blank_values=True)):
            raise ValueError(
                "command argv must not contain credential-bearing URL queries")
    return _dump(list(value))


def _runtime_command(adapter: str, command) -> tuple[str, str]:
    """Validate the small built-in/custom harness seam as one atomic value."""
    adapter = (adapter or "").strip()
    if adapter not in RUNTIME_ADAPTERS:
        raise ValueError(
            "runtime adapter must be one of: " + ", ".join(sorted(RUNTIME_ADAPTERS))
        )
    encoded = _argv(command)
    has_command = bool(json.loads(encoded))
    if adapter in COMMAND_RUNTIME_ADAPTERS and not has_command:
        raise ValueError(f"{adapter} runtime requires a non-empty command argv")
    if adapter not in COMMAND_RUNTIME_ADAPTERS and has_command:
        raise ValueError(f"{adapter} is built in and does not accept command argv")
    return adapter, encoded


def validate_runtime_command(adapter: str, command) -> None:
    """Revalidate frozen/imported state before it becomes run evidence."""
    _runtime_command(adapter, command)


def _source_command(adapter: str, command) -> tuple[str, str]:
    adapter = (adapter or "").strip()
    if adapter not in SOURCE_ADAPTERS:
        raise ValueError(
            "runway source adapter must be one of: " +
            ", ".join(sorted(SOURCE_ADAPTERS))
        )
    encoded = _argv(command)
    has_command = bool(json.loads(encoded))
    if adapter == "command" and not has_command:
        raise ValueError("command runway source requires a non-empty command argv")
    if adapter != "command" and has_command:
        raise ValueError(f"{adapter} is built in and does not accept command argv")
    return adapter, encoded


def _reject_secret_fields(value, path: str = "config") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            words = set(filter(None, re.split(r"[^a-z0-9]+", str(key).lower())))
            if words & _SECRET_WORDS:
                raise ValueError(
                    f"{path}.{key} looks secret-bearing; use the daemon environment, "
                    "harness credential store, or the 0600 secret file"
                )
            _reject_secret_fields(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_secret_fields(child, f"{path}[{index}]")
    elif isinstance(value, str):
        lowered = value.strip().lower()
        if lowered.startswith(("authorization:", "proxy-authorization:",
                               "cookie:")):
            raise ValueError(f"{path} contains an inline credential")
        try:
            parsed = urlsplit(value)
        except ValueError:
            return
        credential_query = parsed.scheme and any(
            set(filter(None, re.split(r"[^a-z0-9]+", key.lower()))) &
            _SECRET_WORDS for key, _ in parse_qsl(
                parsed.query, keep_blank_values=True))
        if parsed.scheme and (parsed.username is not None or
                              parsed.password is not None or credential_query):
            raise ValueError(f"{path} contains URL credentials")


def _mapping(value, label: str) -> str:
    if value is None:
        return "{}"
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    if label == "env":
        if not all(isinstance(key, str) and key and isinstance(item, str)
                   for key, item in value.items()):
            raise ValueError("env keys and values must be non-empty strings")
    if label == "config" and "env" in value:
        nested = value["env"]
        if not isinstance(nested, dict) or not all(
                isinstance(key, str) and key and isinstance(item, str)
                for key, item in nested.items()):
            raise ValueError(
                "config.env keys and values must be non-empty strings")
    if label in {"config", "env"}:
        _reject_secret_fields(value, label)
    return _dump(value)


def validate_nonsecret_mapping(value, label: str) -> None:
    _mapping(value, label)


def _find(con: sqlite3.Connection, table: str, id_column: str, selector: str):
    value = (selector or "").strip()
    if not value:
        return None
    return con.execute(
        f"SELECT * FROM {table} WHERE {id_column}=? OR slug=?",
        (value, value),
    ).fetchone()


def find_runtime(con: sqlite3.Connection, selector: str):
    return _find(con, "runtimes", "runtime_id", selector)


def find_profile(con: sqlite3.Connection, selector: str):
    return _find(con, "profiles", "profile_id", selector)


def find_runway_source(con: sqlite3.Connection, selector: str):
    return _find(con, "runway_sources", "source_id", selector)


def observer_profile_compatibility(
    con: sqlite3.Connection, profile,
) -> tuple[bool, str | None]:
    """Report whether a profile can run a provably tool-free Observer."""
    row = find_profile(con, profile) if isinstance(profile, str) else profile
    if row is None:
        return False, "profile does not exist"
    if row["archived"]:
        return False, "profile is archived"
    if not row["enabled"]:
        return False, "profile is disabled"
    runtime = find_runtime(con, row["runtime_id"])
    if runtime is None:
        return False, "profile runtime does not exist"
    if runtime["archived"]:
        return False, "profile runtime is archived"
    if not runtime["enabled"]:
        return False, "profile runtime is disabled"
    adapter = str(runtime["adapter"] or "")
    if adapter not in OBSERVER_ADAPTERS:
        allowed = ", ".join(sorted(OBSERVER_ADAPTERS))
        return False, (
            f"{adapter or 'unknown'} runtime cannot provide a tool-free "
            f"Observer; use {allowed}"
        )
    return True, None


def require_observer_profile(con: sqlite3.Connection, selector: str):
    """Resolve one available profile and enforce the Observer boundary."""
    row = find_profile(con, selector)
    compatible, reason = observer_profile_compatibility(con, row)
    if not compatible:
        raise ValueError(f"observer profile {selector!r} is incompatible: {reason}")
    return row


def _all(con: sqlite3.Connection, table: str, *, include_archived: bool):
    where = "" if include_archived else " WHERE archived=0"
    return con.execute(
        f"SELECT * FROM {table}{where} ORDER BY lower(name), slug"
    ).fetchall()


def all_runtimes(con: sqlite3.Connection, *, include_archived: bool = False):
    return _all(con, "runtimes", include_archived=include_archived)


def all_profiles(con: sqlite3.Connection, *, include_archived: bool = False):
    return _all(con, "profiles", include_archived=include_archived)


def all_runway_sources(con: sqlite3.Connection, *, include_archived: bool = False):
    return _all(con, "runway_sources", include_archived=include_archived)


def _slug(con: sqlite3.Connection, table: str, name: str,
          explicit: str | None) -> str:
    base = paths.kebab(explicit if explicit is not None else name)
    candidate, suffix = base, 2
    while con.execute(f"SELECT 1 FROM {table} WHERE slug=?", (candidate,)).fetchone():
        if explicit is not None:
            raise ValueError(f"{table[:-1]} slug {candidate!r} already exists")
        candidate, suffix = f"{base}-{suffix}", suffix + 1
    return candidate


def create_runtime(
    con: sqlite3.Connection,
    name: str,
    adapter: str,
    *,
    slug: str | None = None,
    command=(),
    capabilities=None,
    config=None,
    enabled: bool = True,
    actor: str = "operator",
):
    name = (name or "").strip()
    if not name:
        raise ValueError("runtime name and adapter are required")
    adapter, command_json = _runtime_command(adapter, command)
    runtime_id, timestamp = str(uuid.uuid4()), db.now()
    with con:
        minted = _slug(con, "runtimes", name, slug)
        con.execute(
            "INSERT INTO runtimes(runtime_id,slug,name,adapter," 
            "command_json,capabilities_json,config_json,enabled,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (runtime_id, minted, name, adapter, command_json,
             _mapping(capabilities, "capabilities"), _mapping(config, "config"),
             int(enabled), timestamp, timestamp),
        )
        db.record_control(
            con, actor=actor, action="runtime.create", outcome="ok",
            target_type="runtime", target_id=runtime_id,
            detail={"slug": minted, "adapter": adapter},
        )
    return find_runtime(con, runtime_id)


def create_runway_source(
    con: sqlite3.Connection,
    name: str,
    provider: str,
    *,
    account: str = "",
    lane: str = "",
    adapter: str,
    slug: str | None = None,
    command=(),
    config=None,
    enabled: bool = True,
    actor: str = "operator",
):
    name, provider = (name or "").strip(), (provider or "").strip()
    if not name or not provider:
        raise ValueError("runway source name, provider, and adapter are required")
    adapter, command_json = _source_command(adapter, command)
    source_id, timestamp = str(uuid.uuid4()), db.now()
    with con:
        minted = _slug(con, "runway_sources", name, slug)
        con.execute(
            "INSERT INTO runway_sources(source_id,slug,name,provider,account,lane," 
            "adapter,command_json,config_json,enabled,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (source_id, minted, name, provider, (account or "").strip(),
             (lane or "").strip(), adapter, command_json,
             _mapping(config, "config"), int(enabled), timestamp, timestamp),
        )
        db.record_control(
            con, actor=actor, action="runway_source.create", outcome="ok",
            target_type="runway_source", target_id=source_id,
            detail={"slug": minted, "provider": provider,
                    "account": account, "lane": lane},
        )
    return find_runway_source(con, source_id)


def _available(con: sqlite3.Connection, table: str, id_column: str,
               selector: str, label: str):
    row = _find(con, table, id_column, selector)
    if row is None or row["archived"] or not row["enabled"]:
        raise ValueError(f"{label} {selector!r} is not available")
    return row


def create_profile(
    con: sqlite3.Connection,
    name: str,
    runtime: str,
    *,
    tier: int,
    slug: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    priority: int = 0,
    sandbox: str | None = None,
    timeout_seconds: int | None = None,
    max_concurrency: int | None = None,
    runway_source: str | None = None,
    env=None,
    config=None,
    note: str | None = None,
    enabled: bool = True,
    actor: str = "operator",
):
    name = (name or "").strip()
    if not name:
        raise ValueError("profile name is required")
    if int(tier) not in (1, 2, 3):
        raise ValueError("profile tier must be 1, 2, or 3")
    runtime_row = _available(con, "runtimes", "runtime_id", runtime, "runtime")
    source_row = None if runway_source is None else _available(
        con, "runway_sources", "source_id", runway_source, "runway source")
    profile_id, timestamp = str(uuid.uuid4()), db.now()
    with con:
        minted = _slug(con, "profiles", name, slug)
        con.execute(
            "INSERT INTO profiles(profile_id,slug,name,runtime_id,model,effort," 
            "tier,priority,sandbox,timeout_seconds,max_concurrency,runway_source_id," 
            "env_json,config_json,note,enabled,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (profile_id, minted, name, runtime_row["runtime_id"], model, effort,
             int(tier), int(priority), sandbox, timeout_seconds, max_concurrency,
             source_row["source_id"] if source_row else None,
             _mapping(env, "env"), _mapping(config, "config"), note,
             int(enabled), timestamp, timestamp),
        )
        db.record_control(
            con, actor=actor, action="profile.create", outcome="ok",
            target_type="profile", target_id=profile_id,
            detail={"slug": minted, "runtime_id": runtime_row["runtime_id"],
                    "tier": int(tier)},
        )
    return find_profile(con, profile_id)


_RUNTIME_FIELDS = {
    "name", "adapter", "command_json", "capabilities_json",
    "config_json", "enabled",
}
_SOURCE_FIELDS = {
    "name", "provider", "account", "lane", "adapter", "command_json",
    "config_json", "enabled",
}
_PROFILE_FIELDS = {
    "name", "runtime_id", "model", "effort", "tier", "priority", "sandbox",
    "timeout_seconds", "max_concurrency", "runway_source_id", "env_json",
    "config_json", "note", "enabled",
}


def _update(
    con: sqlite3.Connection,
    table: str,
    id_column: str,
    row,
    changes: dict,
    allowed: set[str],
    *,
    expected_revision: int | None,
    actor: str,
    action: str,
    commit: bool = True,
):
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"fields are not editable: {', '.join(sorted(unknown))}")
    if not changes:
        return row
    revision = int(row["revision"] if expected_revision is None else expected_revision)
    values = list(changes.values())
    assignments = ",".join(f"{field}=?" for field in changes)
    if not commit and not con.in_transaction:
        raise RuntimeError("commit=False requires a caller-owned transaction")

    def apply():
        changed = con.execute(
            f"UPDATE {table} SET {assignments},revision=revision+1,updated_at=? "
            f"WHERE {id_column}=? AND revision=?",
            (*values, db.now(), row[id_column], revision),
        )
        if changed.rowcount != 1:
            raise RuntimeError(f"{table[:-1]} changed since it was read")
        db.record_control(
            con, actor=actor, action=action, outcome="ok",
            target_type=table[:-1], target_id=row[id_column],
            detail={"fields": sorted(changes)},
        )
    if commit:
        with con:
            apply()
    else:
        apply()
    return _find(con, table, id_column, row[id_column])


def update_runtime(con: sqlite3.Connection, selector: str, changes: dict, *,
                   expected_revision: int | None = None,
                   actor: str = "operator"):
    row = find_runtime(con, selector)
    if row is None:
        raise LookupError(f"no runtime matches {selector!r}")
    cooked = dict(changes)
    raw_fields = {"command_json", "capabilities_json", "config_json"} & cooked.keys()
    if raw_fields:
        raise ValueError("use command, capabilities, or config instead of raw JSON")
    command_changed = "command" in cooked
    requested_command = cooked.pop(
        "command", json.loads(row["command_json"] or "[]"))
    adapter_changed = "adapter" in cooked
    adapter, command_json = _runtime_command(
        cooked.get("adapter", row["adapter"]), requested_command)
    if adapter_changed:
        cooked["adapter"] = adapter
    if command_changed:
        cooked["command_json"] = command_json
    if "capabilities" in cooked:
        cooked["capabilities_json"] = _mapping(
            cooked.pop("capabilities"), "capabilities")
    if "config" in cooked:
        cooked["config_json"] = _mapping(cooked.pop("config"), "config")
    if "enabled" in cooked:
        cooked["enabled"] = int(bool(cooked["enabled"]))
    return _update(con, "runtimes", "runtime_id", row, cooked,
                   _RUNTIME_FIELDS, expected_revision=expected_revision,
                   actor=actor, action="runtime.update")


def update_runway_source(con: sqlite3.Connection, selector: str, changes: dict, *,
                         expected_revision: int | None = None,
                         actor: str = "operator"):
    row = find_runway_source(con, selector)
    if row is None:
        raise LookupError(f"no runway source matches {selector!r}")
    cooked = dict(changes)
    raw_fields = {"command_json", "config_json"} & cooked.keys()
    if raw_fields:
        raise ValueError("use command or config instead of raw JSON")
    for field in ("name", "provider"):
        if field in cooked:
            if not isinstance(cooked[field], str) or not cooked[field].strip():
                raise ValueError(f"runway source {field} must be a non-empty string")
            cooked[field] = cooked[field].strip()
    for field in ("account", "lane"):
        if field in cooked:
            if not isinstance(cooked[field], str):
                raise ValueError(f"runway source {field} must be a string")
            cooked[field] = cooked[field].strip()
    command_changed = "command" in cooked
    requested_command = cooked.pop(
        "command", json.loads(row["command_json"] or "[]"))
    adapter_changed = "adapter" in cooked
    adapter, command_json = _source_command(
        cooked.get("adapter", row["adapter"]), requested_command)
    if adapter_changed:
        cooked["adapter"] = adapter
    if command_changed:
        cooked["command_json"] = command_json
    if "config" in cooked:
        cooked["config_json"] = _mapping(cooked.pop("config"), "config")
    if "enabled" in cooked:
        cooked["enabled"] = int(bool(cooked["enabled"]))
    return _update(con, "runway_sources", "source_id", row, cooked,
                   _SOURCE_FIELDS, expected_revision=expected_revision,
                   actor=actor, action="runway_source.update")


def update_profile(con: sqlite3.Connection, selector: str, changes: dict, *,
                   expected_revision: int | None = None,
                   actor: str = "operator", commit: bool = True):
    row = find_profile(con, selector)
    if row is None:
        raise LookupError(f"no profile matches {selector!r}")
    cooked = dict(changes)
    raw_fields = {"env_json", "config_json"} & cooked.keys()
    if raw_fields:
        raise ValueError("use env or config instead of raw JSON")
    if "runtime" in cooked:
        runtime = _available(con, "runtimes", "runtime_id",
                             cooked.pop("runtime"), "runtime")
        cooked["runtime_id"] = runtime["runtime_id"]
    elif "runtime_id" in cooked:
        cooked["runtime_id"] = _available(
            con, "runtimes", "runtime_id", cooked["runtime_id"], "runtime"
        )["runtime_id"]
    if "runway_source" in cooked:
        selector = cooked.pop("runway_source")
        cooked["runway_source_id"] = None if selector is None else _available(
            con, "runway_sources", "source_id", selector, "runway source"
        )["source_id"]
    elif "runway_source_id" in cooked and cooked["runway_source_id"] is not None:
        cooked["runway_source_id"] = _available(
            con, "runway_sources", "source_id", cooked["runway_source_id"],
            "runway source",
        )["source_id"]
    if "env" in cooked:
        cooked["env_json"] = _mapping(cooked.pop("env"), "env")
    if "config" in cooked:
        cooked["config_json"] = _mapping(cooked.pop("config"), "config")
    if "enabled" in cooked:
        cooked["enabled"] = int(bool(cooked["enabled"]))
    if "tier" in cooked and int(cooked["tier"]) not in (1, 2, 3):
        raise ValueError("profile tier must be 1, 2, or 3")
    return _update(con, "profiles", "profile_id", row, cooked,
                   _PROFILE_FIELDS, expected_revision=expected_revision,
                   actor=actor, action="profile.update", commit=commit)


def _archive(con: sqlite3.Connection, table: str, id_column: str, row,
             archived: bool, *, expected_revision: int | None,
             actor: str):
    return _update(
        con, table, id_column, row, {"archived": int(archived)},
        {"archived"}, expected_revision=expected_revision, actor=actor,
        action=f"{table[:-1]}.{'archive' if archived else 'restore'}",
    )


def archive_profile(con: sqlite3.Connection, selector: str, archived: bool = True,
                    *, expected_revision: int | None = None,
                    actor: str = "operator"):
    row = find_profile(con, selector)
    if row is None:
        raise LookupError(f"no profile matches {selector!r}")
    return _archive(con, "profiles", "profile_id", row, archived,
                    expected_revision=expected_revision, actor=actor)


def archive_runtime(con: sqlite3.Connection, selector: str, archived: bool = True,
                    *, expected_revision: int | None = None,
                    actor: str = "operator"):
    row = find_runtime(con, selector)
    if row is None:
        raise LookupError(f"no runtime matches {selector!r}")
    if archived and con.execute(
        "SELECT 1 FROM profiles WHERE runtime_id=? AND archived=0 LIMIT 1",
        (row["runtime_id"],),
    ).fetchone():
        raise ValueError("archive profiles using this runtime first")
    return _archive(con, "runtimes", "runtime_id", row, archived,
                    expected_revision=expected_revision, actor=actor)


def archive_runway_source(con: sqlite3.Connection, selector: str,
                          archived: bool = True, *,
                          expected_revision: int | None = None,
                          actor: str = "operator"):
    row = find_runway_source(con, selector)
    if row is None:
        raise LookupError(f"no runway source matches {selector!r}")
    if archived and con.execute(
        "SELECT 1 FROM profiles WHERE runway_source_id=? AND archived=0 LIMIT 1",
        (row["source_id"],),
    ).fetchone():
        raise ValueError("unlink or archive profiles using this runway source first")
    return _archive(con, "runway_sources", "source_id", row, archived,
                    expected_revision=expected_revision, actor=actor)


def fleet_setting(con: sqlite3.Connection, key: str, default=None):
    row = con.execute(
        "SELECT value_json FROM fleet_settings WHERE key=?", (key,)
    ).fetchone()
    return default if row is None else json.loads(row["value_json"])


_SETTING_INTEGER_BOUNDS = {
    "max_active_runs": (1, 256),
    "delegation_max_depth": (0, 10),
    "delegation_max_children": (1, 100),
    "delegation_max_active_children": (1, 100),
}


def _validated_setting(key: str, value):
    if key not in db.DEFAULT_FLEET_SETTINGS:
        raise ValueError(
            "unknown fleet setting; allowed keys: " +
            ", ".join(sorted(db.DEFAULT_FLEET_SETTINGS)))
    if key == "instance_name":
        if not isinstance(value, str) or not value.strip():
            raise ValueError("instance_name must be a non-empty string")
        value = value.strip()
        if len(value) > 100:
            raise ValueError("instance_name must be at most 100 characters")
        return value
    if key == "paused":
        if not isinstance(value, bool):
            raise ValueError("paused must be a boolean")
        return value
    minimum, maximum = _SETTING_INTEGER_BOUNDS[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}")
    return value


def set_fleet_setting(
    con: sqlite3.Connection,
    key: str,
    value,
    *,
    expected_revision: int | None = None,
    actor: str = "operator",
):
    key = (key or "").strip()
    if not key:
        raise ValueError("fleet setting key is required")
    value = _validated_setting(key, value)
    encoded, timestamp = _dump(value), db.now()
    with con:
        row = con.execute(
            "SELECT revision FROM fleet_settings WHERE key=?", (key,)
        ).fetchone()
        if row is None:
            if expected_revision is not None:
                raise RuntimeError("fleet setting changed since it was read")
            con.execute(
                "INSERT INTO fleet_settings(key,value_json,updated_by,updated_at) "
                "VALUES(?,?,?,?)", (key, encoded, actor, timestamp),
            )
        else:
            revision = int(row["revision"] if expected_revision is None
                           else expected_revision)
            changed = con.execute(
                "UPDATE fleet_settings SET value_json=?,revision=revision+1," 
                "updated_by=?,updated_at=? WHERE key=? AND revision=?",
                (encoded, actor, timestamp, key, revision),
            )
            if changed.rowcount != 1:
                raise RuntimeError("fleet setting changed since it was read")
        db.record_control(
            con, actor=actor, action="fleet_setting.set", outcome="ok",
            target_type="fleet_setting", target_id=key,
        )
    return con.execute("SELECT * FROM fleet_settings WHERE key=?", (key,)).fetchone()


def observer(con: sqlite3.Connection):
    return con.execute("SELECT * FROM observer_settings WHERE singleton=1").fetchone()


def configure_observer(
    con: sqlite3.Connection,
    *,
    enabled: bool,
    profile: str | None,
    max_concurrency: int = 1,
    first_look_seconds: int = 300,
    minimum_events: int = 5,
    interval_seconds: int = 1800,
    authority: str = "correct_then_stop",
    expected_revision: int | None = None,
    actor: str = "operator",
):
    if authority not in {"advisory", "tell_only", "correct_then_stop"}:
        raise ValueError(f"unknown observer authority {authority!r}")
    if (isinstance(max_concurrency, bool) or
            not isinstance(max_concurrency, int) or
            not 1 <= max_concurrency <= 8):
        raise ValueError("Observer concurrency must be an integer from 1 to 8")
    profile_row = None if profile is None else require_observer_profile(
        con, profile)
    if enabled and profile_row is None:
        raise ValueError("enabled Observer requires an explicit profile")
    current = observer(con)
    revision = int(current["revision"] if expected_revision is None
                   else expected_revision)
    with con:
        changed = con.execute(
            "UPDATE observer_settings SET enabled=?,profile_id=?," 
            "max_concurrency=?,first_look_seconds=?,minimum_events=?,"
            "interval_seconds=?,authority=?," 
            "revision=revision+1,updated_by=?,updated_at=? "
            "WHERE singleton=1 AND revision=?",
            (int(enabled), profile_row["profile_id"] if profile_row else None,
             max_concurrency, int(first_look_seconds), int(minimum_events),
             int(interval_seconds), authority, actor, db.now(), revision),
        )
        if changed.rowcount != 1:
            raise RuntimeError("Observer settings changed since they were read")
        db.record_control(
            con, actor=actor, action="observer.configure", outcome="ok",
            target_type="observer", target_id="singleton",
            detail={"enabled": bool(enabled),
                    "profile_id": profile_row["profile_id"] if profile_row else None,
                    "max_concurrency": max_concurrency,
                    "authority": authority},
        )
    return observer(con)
