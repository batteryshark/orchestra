"""The single v2 run-admission seam."""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from orchestra import brief, db, fleet_config, groups, paths
from orchestra.contracts import RunRequest, child_tier_allowed


_SECRET = re.compile(r"token|secret|password|credential|api.?key|cookie", re.I)


class AdmissionError(ValueError):
    pass


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _redact(value, key: str = ""):
    if _SECRET.search(key):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(k): _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _snapshot(row, json_fields: tuple[str, ...]) -> dict:
    data = {key: row[key] for key in row.keys()
            if key not in ("revision", "created_at", "updated_at")}
    for key in json_fields:
        raw = data.get(key)
        try:
            data[key.removesuffix("_json")] = json.loads(raw or "{}")
        except (TypeError, ValueError):
            data[key.removesuffix("_json")] = {}
        data.pop(key, None)
    return _redact(data)


def _profile_snapshot(row) -> str:
    value = _snapshot(row, ("env_json", "config_json"))
    fleet_config.validate_nonsecret_mapping(value.get("env"), "env")
    fleet_config.validate_nonsecret_mapping(value.get("config"), "config")
    return _json(value)


def _runtime_snapshot(row) -> str:
    value = _snapshot(
        row, ("command_json", "capabilities_json", "config_json"))
    fleet_config.validate_runtime_command(
        str(value.get("adapter") or ""), value.get("command"))
    fleet_config.validate_nonsecret_mapping(value.get("config"), "config")
    return _json(value)


def _run(con, run_id: int):
    return con.execute(
        "SELECT r.*,g.name AS group_name,g.slug AS group_slug,"
        "p.name AS profile_name,p.slug AS profile_slug,p.tier AS profile_tier,"
        "rt.name AS runtime_name,"
        "rt.slug AS runtime_slug FROM runs r "
        "JOIN run_groups g ON g.group_id=r.group_id "
        "JOIN profiles p ON p.profile_id=r.profile_id "
        "JOIN runtimes rt ON rt.runtime_id=r.runtime_id WHERE r.id=?",
        (run_id,),
    ).fetchone()


def find(con, run_id: int):
    return _run(con, int(run_id))


def find_by_request(con, request_id: str):
    row = con.execute("SELECT id FROM runs WHERE request_id=?", (request_id,)).fetchone()
    return _run(con, int(row["id"])) if row else None


def _setting(con, key: str, default: int) -> int:
    value = fleet_config.fleet_setting(con, key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _depth(con, run_id: int) -> int:
    row = con.execute(
        "WITH RECURSIVE lineage(id,parent_run_id,retry_of_run_id,"
        "continuation_of_run_id,depth) AS ("
        "SELECT id,parent_run_id,retry_of_run_id,continuation_of_run_id,0 "
        "FROM runs WHERE id=? UNION ALL "
        "SELECT r.id,r.parent_run_id,r.retry_of_run_id,r.continuation_of_run_id,"
        "lineage.depth+CASE WHEN lineage.parent_run_id IS NOT NULL THEN 1 ELSE 0 END "
        "FROM lineage JOIN runs r ON r.id=COALESCE(lineage.parent_run_id,"
        "lineage.retry_of_run_id,lineage.continuation_of_run_id)) "
        "SELECT MAX(depth) AS depth FROM lineage", (run_id,),
    ).fetchone()
    return int(row["depth"] or 0)


def _validate_parent(con, parent, profile) -> None:
    if parent["status"] in db.RUN_TERMINAL:
        raise AdmissionError(f"parent run {parent['id']} is terminal")
    depth = _depth(con, int(parent["id"])) + 1
    if depth > _setting(con, "delegation_max_depth", 2):
        raise AdmissionError("delegation depth limit reached")
    count = con.execute(
        "SELECT COUNT(*) AS n FROM runs WHERE parent_run_id=?",
        (parent["id"],),
    ).fetchone()["n"]
    if int(count) >= _setting(con, "delegation_max_children", 3):
        raise AdmissionError("parent child-run limit reached")
    active = con.execute(
        f"SELECT COUNT(*) AS n FROM runs WHERE parent_run_id=? "
        f"AND status NOT IN {db.TERMINAL_SQL}", (parent["id"],),
    ).fetchone()["n"]
    if int(active) >= _setting(con, "delegation_max_active_children", 3):
        raise AdmissionError("parent active-child limit reached")
    parent_tier = _frozen_tier(parent)
    if not child_tier_allowed(parent_tier, int(profile["tier"])):
        raise AdmissionError(
            f"tier {parent_tier} parent cannot delegate upward "
            f"to tier {profile['tier']}")


def _frozen_tier(run) -> int:
    try:
        return int(json.loads(run["profile_snapshot"])["tier"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return int(run["profile_tier"])


def _delegation_parent(con, source):
    """Nearest parent edge behind a retry/continuation lineage, if any."""
    current = source
    seen: set[int] = set()
    while current is not None:
        current_id = int(current["id"])
        if current_id in seen:
            raise AdmissionError("run lineage contains a cycle")
        seen.add(current_id)
        if current["parent_run_id"] is not None:
            return _run(con, int(current["parent_run_id"]))
        previous = current["retry_of_run_id"] or current["continuation_of_run_id"]
        if previous is None:
            return None
        current = _run(con, int(previous))
    return None


def _write_brief(con, run_id: int) -> None:
    run = _run(con, run_id)
    profile = json.loads(run["profile_snapshot"])
    runtime = json.loads(run["runtime_snapshot"])
    may_delegate = _depth(con, run_id) < _setting(con, "delegation_max_depth", 2)
    text = brief.compose(
        run_id=run_id, display_number=db.run_no(run),
        profile_name=profile.get("name") or run["profile_name"],
        runtime_name=runtime.get("name") or run["runtime_name"],
        request=run["mission"], requester=run["requested_by"],
        group_name=run["group_name"], workdir=run["workdir"],
        context=run["context"], may_delegate=may_delegate,
    )
    location = paths.run_dir(run_id) / "brief.md"
    location.write_text(text, encoding="utf-8")
    if hasattr(location, "chmod"):
        location.chmod(0o600)
    log_path = paths.run_dir(run_id) / "worker.jsonl"
    con.execute("UPDATE runs SET brief_path=?,log_path=? WHERE id=?",
                (str(location), str(log_path), run_id))


def submit(con, request: RunRequest) -> tuple[object, bool]:
    """Validate, snapshot, and allocate one durable queued run."""
    existing = find_by_request(con, request.request_id)
    if existing:
        return existing, False
    group = groups.find(con, request.group)
    profile = fleet_config.find_profile(con, request.profile)
    if group is None:
        raise AdmissionError(f"no group matches {request.group!r}")
    if profile is None:
        raise AdmissionError(f"no profile matches {request.profile!r}")
    runtime = fleet_config.find_runtime(con, profile["runtime_id"])
    if runtime is None:
        raise AdmissionError("profile runtime does not exist")
    if profile["archived"] or not profile["enabled"]:
        raise AdmissionError("profile is unavailable")
    if runtime["archived"] or not runtime["enabled"]:
        raise AdmissionError("profile runtime is unavailable")
    parent = _run(con, request.parent_run_id) if request.parent_run_id else None
    if request.parent_run_id and parent is None:
        raise AdmissionError(f"parent run {request.parent_run_id} does not exist")
    if parent:
        if group["group_id"] != parent["group_id"]:
            raise AdmissionError("child runs inherit their parent's group")
        _validate_parent(con, parent, profile)
    elif group["archived"]:
        raise AdmissionError("archived group does not accept root runs")
    if parent:
        cwd, cwd_source = str(parent["cwd"]), "inherited"
    elif request.cwd is not None:
        try:
            cwd = groups.canonical_cwd(request.cwd)
        except ValueError as exc:
            raise AdmissionError(str(exc)) from exc
        cwd_source = "run"
    elif group["default_cwd"]:
        try:
            cwd = groups.canonical_cwd(group["default_cwd"])
        except ValueError as exc:
            raise AdmissionError(f"group default {exc}") from exc
        cwd_source = "group"
    else:
        cwd = str(paths.group_workspace(group["slug"]).resolve())
        cwd_source = "managed"

    dependencies: list[tuple[int, str]] = []
    for dependency in request.after:
        if _run(con, dependency.run_id) is None:
            raise AdmissionError(f"dependency run {dependency.run_id} does not exist")
        dependencies.append((dependency.run_id, dependency.condition))

    observer = request.observer
    if observer not in ("inherit", "off"):
        try:
            fleet_config.require_observer_profile(con, observer)
        except ValueError as exc:
            raise AdmissionError(str(exc)) from exc
    elif observer == "inherit":
        settings = fleet_config.observer(con)
        if settings["enabled"]:
            try:
                fleet_config.require_observer_profile(
                    con, settings["profile_id"])
            except ValueError as exc:
                raise AdmissionError(
                    f"configured Observer is unavailable: {exc}") from exc

    request_data = request.as_dict()
    request_data.update({
        "group_id": group["group_id"],
        "profile_id": profile["profile_id"], "runtime_id": runtime["runtime_id"],
    })
    title = request.title or request.context.strip().splitlines()[0][:120]
    timestamp = db.now()
    try:
        with con:
            cursor = con.execute(
                "INSERT INTO runs(request_id,group_id,profile_id,runtime_id,"
                "runway_source_id,"
                "parent_run_id,title,mission,context,requested_by,ref,status,queued_at,"
                "cwd,cwd_source,workdir,isolation,profile_snapshot,runtime_snapshot,"
                "request_snapshot) VALUES(?,?,?,?,?,?,?,?,?,?,?,'queued',?,?,?,?,'auto',?,?,?)",
                (request.request_id, group["group_id"],
                 profile["profile_id"], runtime["runtime_id"],
                 profile["runway_source_id"], request.parent_run_id,
                 title, request.context, None, request.requested_by,
                 request.ref, timestamp, cwd, cwd_source, cwd,
                 _profile_snapshot(profile),
                 _runtime_snapshot(runtime),
                 _json(_redact(request_data))),
            )
            run_id = int(cursor.lastrowid)
            con.executemany(
                "INSERT INTO run_dependencies(run_id,depends_on_run_id,condition) "
                "VALUES(?,?,?)", ((run_id, dep, condition)
                                  for dep, condition in dependencies),
            )
            _write_brief(con, run_id)
            db.record_control(
                con, actor=request.requested_by, action="run.submit", outcome="ok",
                target_type="run", target_id=run_id, request_id=request.request_id,
                detail={"parent_run_id": request.parent_run_id},
            )
    except sqlite3.IntegrityError as exc:
        existing = find_by_request(con, request.request_id)
        if existing is not None:
            return existing, False
        raise exc
    return _run(con, run_id), True


def clone(con, source_run_id: int, *, request_id: str, kind: str,
          requested_by: str, request: str | None = None,
          profile: str | None = None,
          not_before: str | None = None) -> tuple[object, bool]:
    """Create a separately numbered retry or continuation from frozen facts."""
    if kind not in ("retry", "continuation"):
        raise AdmissionError("lineage clone kind must be retry or continuation")
    existing = find_by_request(con, request_id)
    if existing:
        return existing, False
    source = _run(con, source_run_id)
    if source is None:
        raise AdmissionError(f"run {source_run_id} does not exist")
    if source["status"] not in db.RUN_TERMINAL:
        raise AdmissionError("retry and continuation require a terminal source run")
    mission = source["mission"] if kind == "retry" and request is None \
        else (request or "").strip()
    if not mission:
        raise AdmissionError("continuation instructions are required")
    try:
        frozen_cwd = groups.canonical_cwd(source["cwd"])
    except ValueError as exc:
        raise AdmissionError(f"source {exc}") from exc
    profile_id, runtime_id = source["profile_id"], source["runtime_id"]
    runway_source_id = source["runway_source_id"]
    profile_snapshot, runtime_snapshot = (
        source["profile_snapshot"], source["runtime_snapshot"])
    if profile:
        chosen = fleet_config.find_profile(con, profile)
        if chosen is None or chosen["archived"] or not chosen["enabled"]:
            raise AdmissionError(f"profile {profile!r} is unavailable")
        chosen_runtime = fleet_config.find_runtime(con, chosen["runtime_id"])
        if chosen_runtime is None or chosen_runtime["archived"] or \
                not chosen_runtime["enabled"]:
            raise AdmissionError("selected profile runtime is unavailable")
        delegation_parent = _delegation_parent(con, source)
        if delegation_parent is not None and not child_tier_allowed(
                _frozen_tier(delegation_parent), int(chosen["tier"])):
            raise AdmissionError(
                f"delegated lineage cannot escalate to tier {chosen['tier']}")
        profile_id, runtime_id = chosen["profile_id"], chosen_runtime["runtime_id"]
        runway_source_id = chosen["runway_source_id"]
        profile_snapshot = _profile_snapshot(chosen)
        runtime_snapshot = _runtime_snapshot(chosen_runtime)
    cloned_context = source["context"] if kind == "retry" else None
    if kind == "continuation":
        continuation_context = []
        if source["context"]:
            continuation_context.append(
                "Prior context:\n" + str(source["context"]).strip())
        if source["summary"]:
            continuation_context.append(
                "Prior result:\n" + str(source["summary"]).strip())
        cloned_context = "\n\n".join(continuation_context) or None
    request_data = {
        "request_id": request_id, "context": mission,
        "group_id": source["group_id"], "profile_id": profile_id,
        "runtime_id": runtime_id, f"{kind}_of_run_id": source_run_id,
    }
    lineage_column = "retry_of_run_id" if kind == "retry" \
        else "continuation_of_run_id"
    attempt = int(source["attempt"]) + (1 if kind == "retry" else 0)
    try:
        with con:
            cursor = con.execute(
                "INSERT INTO runs(request_id,group_id,profile_id,runtime_id,"
                "runway_source_id,"
                f"{lineage_column},attempt,title,mission,context,requested_by,ref,status,"
                "queued_at,not_before,cwd,cwd_source,workdir,isolation,profile_snapshot,"
                "runtime_snapshot,request_snapshot) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'queued',?,?,?,?,?,'auto',?,?,?)",
                (request_id, source["group_id"], profile_id, runtime_id,
                 runway_source_id, source_run_id, attempt,
                 source["title"], mission, cloned_context, requested_by,
                 source["ref"], db.now(), not_before, frozen_cwd, "inherited",
                 frozen_cwd, profile_snapshot,
                 runtime_snapshot, _json(_redact(request_data))),
            )
            run_id = int(cursor.lastrowid)
            _write_brief(con, run_id)
            db.record_control(
                con, actor=requested_by, action=f"run.{kind}", outcome="ok",
                target_type="run", target_id=run_id, request_id=request_id,
                detail={"source_run_id": source_run_id},
            )
    except sqlite3.IntegrityError as exc:
        existing = find_by_request(con, request_id)
        if existing is not None:
            return existing, False
        raise exc
    return _run(con, run_id), True
