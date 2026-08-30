"""Pure HTTP-v2 application service. Socket handling lives in ``http``."""
from __future__ import annotations

import base64
import json
import os
import platform
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, quote, urlsplit

from orchestra import (
    artifacts, attention, auth, config, db, fleet_config, groups, idempotency,
    messaging, observer, paths, profiles, runs, runway, scheduler, storage,
)
from orchestra.contracts import ContractError, RunRequest


@dataclass(slots=True)
class Response:
    status: int
    data: Any = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class FileResponse:
    path: Path
    media_type: str
    name: str
    download: bool = False


class Problem(RuntimeError):
    def __init__(self, status: int, code: str, message: str, details=None):
        super().__init__(message)
        self.status, self.code, self.details = status, code, details or {}

    def payload(self) -> dict:
        return {"error": {"code": self.code, "message": str(self),
                          "details": self.details}}


def envelope(con, data) -> dict:
    return {"api_version": 2, "instance_id": db.instance_id(con), "data": data}


def _json(raw, fallback=None):
    if raw in (None, ""):
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


def _cursor(value: str | None) -> int | None:
    if not value:
        return None
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode(value + padding).decode()
        result = int(json.loads(decoded)["id"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise Problem(400, "invalid_cursor", "Cursor is invalid.") from exc
    return result


def _encode_cursor(value: int | None) -> str | None:
    if value is None:
        return None
    raw = json.dumps({"id": int(value)}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _page_offset(value: str | None) -> int:
    if not value:
        return 0
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode(value + padding).decode()
        offset = int(json.loads(decoded)["offset"])
        if offset < 0:
            raise ValueError
        return offset
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise Problem(400, "invalid_cursor", "Cursor is invalid.") from exc


def _encode_page_offset(value: int | None) -> str | None:
    if value is None:
        return None
    raw = json.dumps({"offset": int(value)}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _limit(query: dict, default: int = 100) -> int:
    try:
        return max(1, min(int(query.get("limit", default)), 500))
    except (TypeError, ValueError) as exc:
        raise Problem(400, "invalid_limit", "limit must be an integer.") from exc


def _bool(value, default=False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    lowered = str(value).strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise Problem(400, "invalid_boolean", f"{value!r} is not a boolean.")


def _actor(identity: auth.Identity) -> str:
    return f"{identity.kind}:{identity.subject_id}"


def _authorize(identity: auth.Identity | None, authority: str,
               target_run_id: int | None = None) -> auth.Identity:
    try:
        auth.authorize(identity, authority, target_run_id=target_run_id)
    except auth.AuthError as exc:
        status = 401 if identity is None else 403
        raise Problem(status, "unauthorized" if status == 401 else "forbidden",
                      str(exc)) from exc
    return identity  # type: ignore[return-value]


def _operator(identity: auth.Identity | None) -> auth.Identity:
    if identity is None:
        raise Problem(401, "unauthorized", "Authentication required.")
    if identity.kind != "device":
        raise Problem(403, "device_required", "An operator device is required.")
    return identity


def _discovery_error(value) -> str | None:
    """Return a useful category without publishing CLI stderr or host paths."""
    if value is None:
        return None
    lowered = str(value).lower()
    if "not installed" in lowered:
        return "not_installed"
    if "timed out" in lowered:
        return "timed_out"
    if "no model listing" in lowered:
        return "unsupported"
    if "not found" in lowered:
        return "not_configured"
    if "parse" in lowered:
        return "invalid_output"
    return "discovery_failed"


def _catalog_strings(value) -> list[str]:
    return [item for item in (value or ()) if isinstance(item, str)]


def _profile_discovery_payload(raw, *, local_requested: bool,
                               local_models=()) -> dict:
    """Canonical, bounded-key projection of daemon-host harness discovery."""
    raw = raw if isinstance(raw, dict) else {}
    runtimes = {}
    for runtime in ("opencode", "codex", "reasonix", "claude"):
        present = runtime in raw
        result = raw.get(runtime)
        result = result if isinstance(result, dict) else {}
        data = result.get("data")
        if runtime == "opencode" and isinstance(data, dict):
            data = {
                str(provider): _catalog_strings(models)
                for provider, models in data.items()
                if isinstance(provider, str)
            }
        elif runtime == "codex" and isinstance(data, list):
            data = [{
                "model": item["model"],
                "efforts": _catalog_strings(item.get("efforts")),
                "default_effort": item.get("default_effort")
                if isinstance(item.get("default_effort"), str) else None,
            } for item in data
                    if isinstance(item, dict) and isinstance(item.get("model"), str)]
        elif runtime == "reasonix" and isinstance(data, list):
            data = [{
                "provider": item["provider"],
                "models": _catalog_strings(item.get("models")),
                "efforts": _catalog_strings(item.get("efforts")),
                "default_effort": item.get("default_effort")
                if isinstance(item.get("default_effort"), str) else None,
            } for item in data if isinstance(item, dict) and
                    isinstance(item.get("provider"), str)]
        elif runtime == "claude" and data is not None:
            data = None
        elif data is not None:
            data = None
        error = _discovery_error(result.get("error"))
        if not present and error is None:
            error = "discovery_failed"
        if result.get("data") is not None and data is None and error is None:
            error = "invalid_output"
        runtimes[runtime] = {"data": data, "error": error}

    local = []
    if local_requested:
        seen = set()
        for item in local_models or ():
            if not isinstance(item, dict):
                continue
            model_id, source = item.get("id"), item.get("source")
            if not isinstance(model_id, str) or not isinstance(source, str):
                continue
            key = (model_id, source)
            if key not in seen:
                local.append({"id": model_id, "source": source})
                seen.add(key)
    return {"runtimes": runtimes, "local_requested": local_requested,
            "local_models": local}


def _managed(row, json_fields=()) -> dict:
    data = dict(row)
    for key in json_fields:
        data[key.removesuffix("_json")] = _json(data.pop(key, None), {})
    data.pop("token_hash", None)
    return data


def _hold_kind(reason: str | None) -> str | None:
    if not reason:
        return None
    if reason.startswith("waiting for"):
        return "dependency"
    if reason.startswith("fleet paused"):
        return "paused"
    if reason.startswith("global capacity"):
        return "global_capacity"
    if reason.startswith("profile capacity"):
        return "profile_capacity"
    if reason.startswith("runway"):
        return "runway"
    if reason.startswith("scheduled"):
        return "scheduled"
    return "other"


def _safe_profile_snapshot(raw) -> dict:
    value = _json(raw, {}) if not isinstance(raw, dict) else raw
    if not isinstance(value, dict):
        return {}
    return {
        "id": value.get("id") or value.get("profile_id"),
        "slug": value.get("slug"),
        "name": value.get("name"),
        "runtime_id": value.get("runtime_id"),
        "model": value.get("model"),
        "effort": value.get("effort"),
        "tier": value.get("tier"),
        "priority": value.get("priority"),
        "sandbox": value.get("sandbox"),
        "timeout_seconds": value.get("timeout_seconds"),
        "active_cap": value.get("active_cap", value.get("max_concurrency")),
        "runway_source_id": value.get("runway_source_id"),
        "note": value.get("note"),
        "enabled": bool(value.get("enabled", True)),
    }


def _safe_runtime_snapshot(raw) -> dict:
    value = _json(raw, {}) if not isinstance(raw, dict) else raw
    if not isinstance(value, dict):
        return {}
    capabilities = value.get("capabilities")
    capabilities = capabilities if isinstance(capabilities, dict) else {}
    return {
        "id": value.get("id") or value.get("runtime_id"),
        "slug": value.get("slug"),
        "name": value.get("name"),
        "kind": value.get("kind") or value.get("adapter"),
        "enabled": bool(value.get("enabled", True)),
        "supports_steering": bool(capabilities.get("steering", False)),
        "supports_interrupt": bool(capabilities.get("interrupt", False)),
    }


_SECRET_ARG_WORDS = frozenset({
    "auth", "authorization", "bearer", "credential", "credentials", "key",
    "password", "secret", "token",
})


def _secret_arg_name(value: str) -> bool:
    words = set(filter(None, re.split(r"[^a-z0-9]+", value.lower())))
    return bool(words & _SECRET_ARG_WORDS)


def _public_argv(value) -> list[str]:
    """Redact credential-shaped argv while preserving useful launch shape."""
    if not isinstance(value, list):
        return []
    result, redact_next = [], False
    for raw in value:
        part = str(raw)
        if redact_next:
            result.append("<redacted>")
            redact_next = False
            continue
        lowered = part.lower()
        if "authorization:" in lowered or "proxy-authorization:" in lowered:
            result.append("<redacted-header>")
            continue
        if "=" in part:
            name = part.split("=", 1)[0]
            if (name.startswith("-") or re.fullmatch(
                    r"[A-Za-z_][A-Za-z0-9_]*", name)) and \
                    _secret_arg_name(name):
                result.append(name + "=<redacted>")
                continue
        if part.startswith("-") and _secret_arg_name(part):
            result.append(part)
            redact_next = True
            continue
        if "://" in part:
            try:
                parsed = urlsplit(part)
                secret_query = any(
                    _secret_arg_name(name) for name, _ in
                    parse_qsl(parsed.query, keep_blank_values=True))
                if parsed.username is not None or parsed.password is not None or \
                        secret_query:
                    result.append("<redacted-url>")
                    continue
            except ValueError:
                pass
        result.append(part)
    return result


def run_payload(row, *, detail: bool = False) -> dict:
    columns = row.keys()
    usage = {
        "input_tokens": row["tokens_in"], "output_tokens": row["tokens_out"],
        "total_tokens": row["tokens_total"], "cost_usd": row["cost_usd"],
        "cache_read_tokens": (row["tokens_cache_read"]
                              if "tokens_cache_read" in columns else None),
        "cache_write_tokens": (row["tokens_cache_write"]
                               if "tokens_cache_write" in columns else None),
        "source": row["usage_source"],
    }
    value = {
        "id": int(row["id"]), "slug": row["slug"],
        "request_id": row["request_id"], "display": db.run_no(row),
        "group_id": row["group_id"], "group_name": row["group_name"],
        "group_number": row["group_seq"], "profile_id": row["profile_id"],
        "profile_name": row["profile_name"], "runtime_id": row["runtime_id"],
        "runtime_name": row["runtime_name"], "title": row["title"],
        "context": row["mission"] if detail else None, "status": row["status"],
        "hold": ({"kind": _hold_kind(row["hold_reason"]),
                  "detail": row["hold_reason"]} if row["hold_reason"] else None),
        "waiting_kind": row["waiting_kind"], "requested_by": row["requested_by"],
        "ref": row["ref"], "root_run_id": row["root_run_id"],
        "parent_run_id": row["parent_run_id"],
        "retry_of": row["retry_of_run_id"],
        "continuation_of": row["continuation_of_run_id"],
        "attempt": row["attempt"], "queued_at": row["queued_at"],
        "started_at": row["started_at"], "finished_at": row["finished_at"],
        "summary": row["summary"], "exit_code": row["exit_code"],
        "usage": usage,
        "revision": row["revision"],
        "cwd_source": row["cwd_source"],
        "branch": row["branch"], "base_commit": row["base_commit"],
        "head_commit": row["head_commit"],
        "checkpoint_commit": row["checkpoint_commit"],
    }
    if detail:
        value.update({
            "profile_snapshot": _safe_profile_snapshot(row["profile_snapshot"]),
            "runtime_snapshot": _safe_runtime_snapshot(row["runtime_snapshot"]),
            "not_before": row["not_before"],
            "result": ({"summary": row["summary"]}
                       if row["status"] == "completed" else None),
            "failure": ({"message": row["summary"] or "Run did not complete.",
                         "exit_code": row["exit_code"]}
                        if row["status"] in {"failed", "timed_out", "stopped"}
                        else None),
        })
    else:
        value.pop("context")
    return value


class API:
    def __init__(self, con):
        self.con = con

    def response(self, data, status=200, headers=None) -> Response:
        return Response(status, envelope(self.con, data), headers or {})

    def mutation(self, method: str, path: str, body: dict,
                 action: Callable[[], Any], *, actor: str) -> Response:
        request_id = str(body.get("request_id") or "").strip()
        if not request_id:
            raise Problem(400, "request_id_required", "request_id is required.")
        try:
            with db.api_mutation(self.con):
                replay = idempotency.reserve(
                    self.con, request_id, method, path, body)
                if replay is not None:
                    if replay.get("secret_response"):
                        raise Problem(
                            409, "secret_already_issued",
                            "This request succeeded, but its secret is shown only once.")
                    if path == "/api/v2/runs":
                        replay.get("data", {})["created"] = False
                    response = Response(200, replay)
                else:
                    previous_audit_id = int(self.con.execute(
                        "SELECT COALESCE(MAX(id),0) FROM control_events"
                    ).fetchone()[0])
                    data = action()
                    audited = self.con.execute(
                        "SELECT 1 FROM control_events WHERE id>? AND actor=? "
                        "AND request_id=? LIMIT 1",
                        (previous_audit_id, actor, request_id),
                    ).fetchone()
                    if audited is None:
                        db.record_control(
                            self.con, actor=actor, action="api.accepted", outcome="ok",
                            request_id=request_id,
                            detail={"method": method, "path": path},
                        )
                    db.bump_board_revision(self.con)
                    payload = envelope(self.con, data)
                    idempotency.finish(
                        self.con, request_id, payload, commit=False)
                    response = Response(
                        201 if method == "POST" and path in {
                            "/api/v2/runs", "/api/v2/groups",
                            "/api/v2/runtimes", "/api/v2/profiles",
                            "/api/v2/runway-sources"} else 200,
                        payload)
        except idempotency.Conflict as exc:
            raise Problem(409, "request_id_conflict", str(exc)) from exc
        return response

    def secret_mutation(self, method: str, path: str, body: dict,
                        action: Callable[[], Any], *,
                        actor: str | Callable[[Any], str],
                        audit_action: str = "api.accepted",
                        target: Callable[[Any], tuple[str, str]] | None = None
                        ) -> Response:
        """Commit one-shot issuance/consumption with its durable replay marker."""
        request_id = str(body.get("request_id") or "").strip()
        if not request_id:
            raise Problem(400, "request_id_required", "request_id is required.")
        try:
            replay = idempotency.begin_atomic(
                self.con, request_id, method, path, body)
        except idempotency.Conflict as exc:
            raise Problem(409, "request_id_conflict", str(exc)) from exc
        if replay is not None:
            raise Problem(409, "secret_already_issued",
                          "This request succeeded, but its secret is shown only once.")
        try:
            data = action()
            resolved_actor = actor(data) if callable(actor) else actor
            target_type, target_id = target(data) if target else (None, None)
            db.record_control(
                self.con, actor=resolved_actor, action=audit_action, outcome="ok",
                target_type=target_type, target_id=target_id,
                request_id=request_id,
                detail={"method": method, "path": path}
                if audit_action == "api.accepted" else None,
            )
            db.bump_board_revision(self.con)
            payload = envelope(self.con, data)
            idempotency.finish(
                self.con, request_id, {"secret_response": True}, commit=False)
            self.con.commit()
            return Response(201, payload)
        except BaseException:
            if self.con.in_transaction:
                self.con.rollback()
            raise

    def handle(self, method: str, path: str, query: dict[str, str], body: Any,
               identity: auth.Identity | None) -> Response | FileResponse:
        if path == "/api/v2/pairing/redeem" and method == "POST":
            return self._redeem(body)
        if path == "/api/v2/openapi.json" and method == "GET":
            _authorize(identity, "read")
            return Response(200, openapi(), {"Cache-Control": "no-store"})
        if not path.startswith("/api/v2"):
            raise Problem(404, "not_found", "No such v2 resource.")
        segments = [segment for segment in path[len("/api/v2/"):].split("/") if segment]
        if not segments:
            raise Problem(404, "not_found", "No such v2 resource.")

        root = segments[0]
        if root == "snapshot" and method == "GET":
            _authorize(identity, "read")
            return self.response(self._snapshot())
        if root == "statistics" and method == "GET":
            _authorize(identity, "read")
            return self.response(self._statistics(query))
        if root == "service-log" and method == "GET" and len(segments) == 1:
            _authorize(identity, "read")
            return self.response(self._service_log())
        if root == "runs":
            return self._runs(method, segments[1:], query, body, identity)
        if root == "run-feed" and method == "GET":
            _authorize(identity, "read")
            return self.response(self._run_feed(query))
        if root in {"groups", "runtimes", "profiles", "runway-sources"}:
            return self._resources(root, method, segments[1:], query, body, identity)
        if root == "profile-discovery" and method == "GET" and len(segments) == 1:
            _operator(identity)
            local_requested = _bool(query.get("local"), False)
            return self.response(_profile_discovery_payload(
                profiles.discover(), local_requested=local_requested,
                local_models=(profiles.discover_local()
                              if local_requested else ())))
        if root == "settings":
            return self._settings(method, body, identity)
        if root == "observer":
            return self._observer(method, body, identity)
        if root == "scheduler":
            return self._scheduler(method, segments[1:], body, identity)
        if root in {"inbox", "attention-feed", "attention"}:
            return self._attention(root, method, segments[1:], query, body, identity)
        if root == "outbox" and method == "GET" and len(segments) == 1:
            _authorize(identity, "read")
            return self.response(self._outbox(query))
        if root == "artifacts":
            return self._artifact(method, segments[1:], identity)
        if root in {"devices", "service-tokens"}:
            return self._credentials(root, method, segments[1:], body, identity)
        if root == "storage":
            return self._storage(method, segments[1:], body, identity)
        raise Problem(404, "not_found", "No such v2 resource.")

    def _service_log(self) -> dict:
        """Return a bounded merged tail of the daemon's launch-service logs."""
        entries = []
        for stream, filename in (("stdout", "daemon.out.log"),
                                 ("stderr", "daemon.err.log")):
            location = paths.logs_dir() / filename
            try:
                size = location.stat().st_size
                with location.open("rb") as handle:
                    handle.seek(max(0, size - 128 * 1024))
                    raw = handle.read().decode("utf-8", errors="replace")
                entries.append({"stream": stream, "text": raw,
                                "partial": size > 128 * 1024})
            except OSError:
                entries.append({"stream": stream, "text": "", "partial": False})
        return {"observed_at": db.now(), "entries": entries}

    def _snapshot(self) -> dict:
        counts = {}
        for key, table in (("groups", "run_groups"),
                           ("runtimes", "runtimes"), ("profiles", "profiles"),
                           ("runway_sources", "runway_sources")):
            counts[key] = int(self.con.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE archived=0"
                if table != "run_groups" else
                "SELECT COUNT(*) AS n FROM run_groups WHERE archived=0"
            ).fetchone()["n"])
        statuses = {row["status"]: int(row["n"]) for row in self.con.execute(
            "SELECT status,COUNT(*) AS n FROM runs GROUP BY status")}
        inbox = int(self.con.execute(
            "SELECT COUNT(*) AS n FROM attention_requests WHERE status='open'"
        ).fetchone()["n"])
        blocking = int(self.con.execute(
            "SELECT COUNT(*) AS n FROM attention_requests WHERE status='open' "
            "AND blocking=1").fetchone()["n"])
        message_counts = {
            "total": 0, "pending": 0, "delivered": 0,
            "undeliverable": 0, "inbound": 0, "outbound": 0, "system": 0,
        }
        for row in self.con.execute(
                "SELECT direction,status,COUNT(*) AS n FROM messages "
                "GROUP BY direction,status"):
            count = int(row["n"])
            message_counts["total"] += count
            message_counts[row["status"]] += count
            message_counts[row["direction"]] += count
        run_total = sum(statuses.values())
        active = sum(statuses.get(status, 0) for status in db.RUN_ACTIVE)
        observer_row = fleet_config.observer(self.con)
        scheduler_state = scheduler.state(self.con)
        timestamp = db.now()
        instance_name = fleet_config.fleet_setting(
            self.con, "instance_name", "Orchestra")
        if not isinstance(instance_name, str) or not instance_name.strip():
            instance_name = "Orchestra"
        return {
            "generated_at": timestamp,
            "instance": {"name": instance_name.strip(),
                         "platform": platform.system().lower()},
            "daemon": {"status": "healthy", "healthy": True,
                       "last_tick_at": db.meta_get(self.con, "daemon_last_tick")},
            "revision": db.board_revision(self.con),
            "scheduler": {
                "paused": bool(scheduler_state["paused"]),
                "active": scheduler_state["active_runs"],
                "queued": scheduler_state["queued_count"],
                "max_active": scheduler_state["max_active_runs"],
            },
            "counts": {**counts, "runs_total": run_total,
                       "runs_active": active,
                       "runs_queued": statuses.get("queued", 0)},
            "run_statuses": statuses,
            "inbox": {"open": inbox, "blocking": blocking},
            "messages": message_counts,
            "observer": observer_settings_payload(observer_row),
            "storage": storage.report(self.con),
        }

    def _outbox(self, query: dict) -> dict:
        """Newest-first fleet message ledger; the cursor loads older rows."""
        clauses, values = [], []
        before = _cursor(query.get("cursor"))
        if before is not None:
            clauses.append("m.id<?")
            values.append(before)
        direction = query.get("direction")
        if direction:
            if direction not in messaging.DIRECTIONS:
                raise Problem(422, "invalid_message_direction",
                              f"Unknown message direction {direction!r}.")
            clauses.append("m.direction=?")
            values.append(direction)
        status = query.get("status")
        if status:
            if status not in messaging.STATUSES:
                raise Problem(422, "invalid_message_status",
                              f"Unknown message status {status!r}.")
            clauses.append("m.status=?")
            values.append(status)
        kind = query.get("kind")
        if kind is not None:
            kind = str(kind).strip()
            if not kind:
                raise Problem(422, "invalid_message_kind",
                              "kind must be a non-empty string.")
            clauses.append("m.kind=?")
            values.append(kind)
        if query.get("run_id") is not None:
            try:
                run_id = int(query["run_id"])
            except (TypeError, ValueError) as exc:
                raise Problem(422, "invalid_run_id",
                              "run_id must be a positive integer.") from exc
            if run_id < 1:
                raise Problem(422, "invalid_run_id",
                              "run_id must be a positive integer.")
            clauses.append("m.run_id=?")
            values.append(run_id)
        limit = _limit(query)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows_ = [dict(row) for row in self.con.execute(
            "SELECT m.*,g.name AS group_name,r.group_seq FROM messages m "
            "JOIN runs r ON r.id=m.run_id JOIN run_groups g ON "
            "g.group_id=r.group_id" + where +
            " ORDER BY m.id DESC LIMIT ?", (*values, limit + 1))]
        more, rows_ = len(rows_) > limit, rows_[:limit]
        return {
            "items": [message_payload(row) for row in rows_],
            "next_cursor": _encode_cursor(rows_[-1]["id"]) if more else None,
            "has_more": more,
        }

    def _timeline_page(self, table: str, run_id: int, query: dict,
                       projector: Callable[[dict], dict]) -> dict:
        direction = query.get("direction", "older")
        if direction not in {"older", "newer"}:
            raise Problem(422, "invalid_timeline_direction",
                          "direction must be older or newer.")
        cursor = _cursor(query.get("cursor"))
        limit = _limit(query, 200)
        if direction == "older":
            clause, values = ((" AND id<?", (run_id, cursor))
                              if cursor is not None else ("", (run_id,)))
            rows_ = [dict(row) for row in self.con.execute(
                f"SELECT * FROM {table} WHERE run_id=?{clause} "
                "ORDER BY id DESC LIMIT ?", (*values, limit + 1))]
            more, rows_ = len(rows_) > limit, rows_[:limit]
            rows_.reverse()
            next_cursor = _encode_cursor(rows_[0]["id"]) if more else None
            resume_cursor = _encode_cursor(rows_[-1]["id"]) if rows_ else None
        else:
            after = cursor or 0
            rows_ = [dict(row) for row in self.con.execute(
                f"SELECT * FROM {table} WHERE run_id=? AND id>? "
                "ORDER BY id LIMIT ?", (run_id, after, limit + 1))]
            more, rows_ = len(rows_) > limit, rows_[:limit]
            next_cursor = _encode_cursor(rows_[-1]["id"]) if more and rows_ else None
            resume_cursor = (_encode_cursor(rows_[-1]["id"]) if rows_ else
                             _encode_cursor(after) if cursor is not None else None)
        return {
            "items": [projector(row) for row in rows_],
            "next_cursor": next_cursor, "resume_cursor": resume_cursor,
            "has_more": more,
        }

    def _statistics(self, query: dict) -> dict:
        clauses, values = [], []
        selectors = {
            "group": ("g.group_id", "g.slug"),
            "profile": ("p.profile_id", "p.slug"),
        }
        for key, columns in selectors.items():
            if query.get(key):
                clauses.append(f"({columns[0]}=? OR {columns[1]}=?)")
                values.extend((query[key], query[key]))
        status = query.get("status")
        if status:
            if status not in db.RUN_ACTIVE + db.RUN_TERMINAL:
                raise Problem(422, "invalid_run_status",
                              f"Unknown run status {status!r}.")
            clauses.append("r.status=?")
            values.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        joins = (
            " FROM runs r JOIN run_groups g ON g.group_id=r.group_id "
            "JOIN profiles p ON p.profile_id=r.profile_id"
        )
        rows = self.con.execute(
            "SELECT r.status,COUNT(*) AS runs,"
            "COALESCE(SUM(r.tokens_in),0) AS tokens_in,"
            "COALESCE(SUM(r.tokens_out),0) AS tokens_out,"
            "COALESCE(SUM(r.tokens_total),0) AS tokens_total,"
            "COALESCE(SUM(CASE WHEN r.started_at IS NULL THEN 0 ELSE "
            "MAX(0,(julianday(COALESCE(r.finished_at,CURRENT_TIMESTAMP))-"
            "julianday(r.started_at))*86400) END),0) AS agent_seconds,"
            "COALESCE(SUM(r.cost_usd),0) AS cost_usd" + joins + where +
            " GROUP BY r.status", values).fetchall()
        by_status = {row["status"]: int(row["runs"]) for row in rows}
        observer_row = self.con.execute(
            "SELECT COALESCE(SUM(o.tokens_in),0) AS tokens_in,"
            "COALESCE(SUM(o.tokens_out),0) AS tokens_out,"
            "COALESCE(SUM(o.tokens_total),0) AS tokens_total,"
            "COALESCE(SUM(o.cost_usd),0) AS cost_usd "
            "FROM observer_checks o JOIN runs r ON r.id=o.run_id "
            "JOIN run_groups g ON g.group_id=r.group_id "
            "JOIN profiles p ON p.profile_id=r.profile_id" + where,
            values).fetchone()
        worker = {
            "input_tokens": int(sum(row["tokens_in"] for row in rows)),
            "output_tokens": int(sum(row["tokens_out"] for row in rows)),
            "total_tokens": int(sum(row["tokens_total"] for row in rows)),
            "cost_usd": round(sum(row["cost_usd"] or 0 for row in rows), 6),
        }
        observed = {
            "input_tokens": int(observer_row["tokens_in"]),
            "output_tokens": int(observer_row["tokens_out"]),
            "total_tokens": int(observer_row["tokens_total"]),
            "cost_usd": round(observer_row["cost_usd"], 6),
        }
        combined = {key: round(worker[key] + observed[key], 6)
                    if key == "cost_usd" else worker[key] + observed[key]
                    for key in worker}
        return {
            "filters": {key: query.get(key) for key in (*selectors, "status")
                        if query.get(key)},
            "runs": sum(by_status.values()), "by_status": by_status,
            "agent_seconds": round(sum(row["agent_seconds"] for row in rows)),
            "worker_usage": worker,
            "observer_usage": observed, "combined_usage": combined,
        }

    def _list_runs(self, query: dict) -> dict:
        limit, before = _limit(query), _cursor(query.get("cursor"))
        clauses, values = [], []
        if before is not None:
            clauses.append("r.id<?")
            values.append(before)
        filters = {"group": ("g.group_id", "g.slug"),
                   "profile": ("p.profile_id", "p.slug")}
        for key, columns in filters.items():
            if query.get(key):
                clauses.append(f"({columns[0]}=? OR {columns[1]}=?)")
                values.extend((query[key], query[key]))
        if query.get("status"):
            if query["status"] not in db.RUN_ACTIVE + db.RUN_TERMINAL:
                raise Problem(422, "invalid_run_status",
                              f"Unknown run status {query['status']!r}.")
            clauses.append("r.status=?")
            values.append(query["status"])
        if query.get("q"):
            clauses.append("(r.title LIKE ? OR r.mission LIKE ? OR r.ref LIKE ?)")
            pattern = f"%{query['q']}%"
            values.extend((pattern, pattern, pattern))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.con.execute(
            "SELECT r.*,g.name AS group_name,g.slug AS group_slug,"
            "p.name AS profile_name,p.slug AS profile_slug,p.tier AS profile_tier,"
            "rt.name AS runtime_name,"
            "rt.slug AS runtime_slug FROM runs r JOIN run_groups g ON "
            "g.group_id=r.group_id JOIN profiles p ON p.profile_id=r.profile_id "
            "JOIN runtimes rt ON "
            "rt.runtime_id=r.runtime_id" + where + " ORDER BY r.id DESC LIMIT ?",
            (*values, limit + 1)).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        return {"items": [run_payload(row) for row in rows],
                "next_cursor": _encode_cursor(int(rows[-1]["id"])) if more else None,
                "has_more": more}

    def _run_feed(self, query: dict) -> dict:
        try:
            after = int(query.get("after", 0))
        except ValueError as exc:
            raise Problem(400, "invalid_cursor", "after must be an integer.") from exc
        limit = _limit(query, 200)
        rows = self.con.execute(
            "SELECT r.*,g.name AS group_name,g.slug AS group_slug,"
            "p.name AS profile_name,p.slug AS profile_slug,p.tier AS profile_tier,"
            "rt.name AS runtime_name,"
            "rt.slug AS runtime_slug FROM runs r JOIN run_groups g ON "
            "g.group_id=r.group_id JOIN profiles p ON p.profile_id=r.profile_id "
            "JOIN runtimes rt ON "
            "rt.runtime_id=r.runtime_id WHERE r.revision>? "
            "ORDER BY r.revision,r.id LIMIT ?", (after, limit + 1)).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        next_value = max((int(row["revision"]) for row in rows), default=after)
        return {"items": [run_payload(row) for row in rows],
                "next_cursor": next_value, "has_more": more}

    def _runs(self, method: str, tail: list[str], query: dict, body,
              identity) -> Response | FileResponse:
        if not tail:
            if method == "GET":
                _authorize(identity, "read")
                return self.response(self._list_runs(query))
            if method == "POST":
                _authorize(identity, "dispatch")
                if not isinstance(body, dict):
                    raise Problem(400, "invalid_body", "Request body must be an object.")
                def submit():
                    request = RunRequest.from_mapping(body, allow_parent=False)
                    run, created = runs.submit(self.con, request)
                    return {"created": created, "run": run_payload(run, detail=True)}
                return self.mutation(
                    method, "/api/v2/runs", body, submit,
                    actor=_actor(identity))
        try:
            run_id = int(tail[0])
        except (ValueError, IndexError) as exc:
            raise Problem(404, "run_not_found", "Run does not exist.") from exc
        run = runs.find(self.con, run_id)
        if run is None:
            raise Problem(404, "run_not_found", f"Run {run_id} does not exist.")
        if method == "GET":
            _authorize(identity, "read", run_id)
        if len(tail) == 1 and method == "GET":
            payload = run_payload(run, detail=True)
            payload["evidence_pin"] = storage.pin_for_run(self.con, run_id)
            payload["counts"] = {
                "messages": self.con.execute(
                    "SELECT COUNT(*) FROM messages WHERE run_id=?", (run_id,)
                ).fetchone()[0],
                "events": self.con.execute(
                    "SELECT COUNT(*) FROM events WHERE run_id=?", (run_id,)
                ).fetchone()[0],
                "artifacts": self.con.execute(
                    "SELECT COUNT(*) FROM artifacts WHERE run_id=?", (run_id,)
                ).fetchone()[0],
            }
            checks = observer.checks(self.con, run_id)
            observed = {
                "input_tokens": sum(item.get("tokens_in") or 0 for item in checks),
                "output_tokens": sum(item.get("tokens_out") or 0 for item in checks),
                "total_tokens": sum(item.get("tokens_total") or 0 for item in checks),
                "cost_usd": round(sum(item.get("cost_usd") or 0 for item in checks), 6),
                "checks": len(checks),
            }
            payload["observer_usage"] = observed
            worker = payload["usage"]
            payload["combined_usage"] = {
                "input_tokens": (worker["input_tokens"] or 0) +
                                observed["input_tokens"],
                "output_tokens": (worker["output_tokens"] or 0) +
                                 observed["output_tokens"],
                "total_tokens": (worker["total_tokens"] or 0) +
                                observed["total_tokens"],
                "cost_usd": round((worker["cost_usd"] or 0) +
                                  observed["cost_usd"], 6),
            }
            return self.response(payload)
        if len(tail) < 2:
            raise Problem(404, "not_found", "No such run resource.")
        action = tail[1]
        if method == "GET":
            if action == "thread":
                return self.response(self._timeline_page(
                    "messages", run_id, query, message_payload))
            if action == "events":
                return self.response(self._timeline_page(
                    "events", run_id, query, event_payload))
            if action == "lineage":
                family = [runs.find(self.con, item["id"]) for item in
                          self.con.execute(
                              "SELECT id FROM runs WHERE root_run_id=? ORDER BY id",
                              (run["root_run_id"],))]
                by_id = {int(item["id"]): item for item in family}
                def source_id(item):
                    return item["parent_run_id"] or item["retry_of_run_id"] or \
                        item["continuation_of_run_id"]
                ancestors, cursor = [], source_id(run)
                while cursor and int(cursor) in by_id:
                    ancestors.append(by_id[int(cursor)])
                    cursor = source_id(by_id[int(cursor)])
                ancestors.reverse()
                descendant_ids, frontier = set(), {run_id}
                while frontier:
                    next_frontier = {
                        int(item["id"]) for item in family
                        if source_id(item) in frontier and int(item["id"]) not in
                        descendant_ids}
                    descendant_ids.update(next_frontier)
                    frontier = next_frontier
                descendants = [item for item in family
                               if int(item["id"]) in descendant_ids]
                children = [item for item in family
                            if item["parent_run_id"] == run_id]
                return self.response({
                    "root_run_id": run["root_run_id"],
                    "ancestors": [run_payload(item) for item in ancestors],
                    "descendants": [run_payload(item) for item in descendants],
                    "children": [run_payload(item) for item in children],
                    "items": [run_payload(item) for item in family],
                })
            if action == "observer":
                checks = [observer_check_payload(item) for item in
                          observer.checks(self.con, run_id)]
                usage = {
                    "input_tokens": sum(item["usage"]["input_tokens"] or 0
                                        for item in checks),
                    "output_tokens": sum(item["usage"]["output_tokens"] or 0
                                         for item in checks),
                    "total_tokens": sum(item["usage"]["total_tokens"] or 0
                                        for item in checks),
                    "cost_usd": round(sum(item["usage"]["cost_usd"] or 0
                                          for item in checks), 6),
                }
                return self.response({"checks": checks, "usage": usage})
            if action == "artifacts":
                return self.response({
                    "items": [artifact_payload(item) for item in
                              artifacts.for_run(self.con, run_id)]})
            if action == "changes":
                patch_text = None
                if run["diff_path"]:
                    try:
                        patch_text = Path(run["diff_path"]).read_text(
                            encoding="utf-8", errors="replace")
                    except OSError:
                        pass
                branch_exists = merged = False
                if run["branch"] and run["repo"]:
                    from orchestra import worktree
                    root = Path(run["repo"])
                    branch_exists = worktree.branch_exists(root, run["branch"])
                    merged = branch_exists and worktree.branch_merged(
                        root, run["branch"])
                return self.response({"branch": run["branch"],
                                      "branch_exists": branch_exists,
                                      "merged": merged,
                                      "base": run["base_commit"],
                                      "head": run["head_commit"],
                                      "checkpoints": ([{
                                          "id": run["checkpoint_commit"],
                                          "commit": run["checkpoint_commit"],
                                          "created_at": run["finished_at"],
                                      }] if run["checkpoint_commit"] else []),
                                      "patch": patch_text, "diff": patch_text,
                                      "truncated": False})
            if action == "log":
                cursor = self.con.execute(
                    "SELECT raw_pruned_at FROM trace_cursors WHERE run_id=?",
                    (run_id,)).fetchone()
                if cursor and cursor["raw_pruned_at"]:
                    raise Problem(410, "log_pruned", "Run log was explicitly pruned.")
                if not run["log_path"] or not Path(run["log_path"]).is_file():
                    raise Problem(404, "log_not_found", "Run log is unavailable.")
                return FileResponse(Path(run["log_path"]),
                                    "application/x-ndjson", f"run-{run_id}.jsonl")
        if method == "POST":
            if not isinstance(body, dict):
                raise Problem(400, "invalid_body", "Request body must be an object.")
            if action == "artifacts":
                _authorize(identity, "artifact", run_id)
                def publish():
                    item = artifacts.publish(
                        self.con, run_id, str(body.get("path") or ""),
                        name=body.get("name"))
                    return {"artifact": artifact_payload(item)}
                return self.mutation(method, f"/api/v2/runs/{run_id}/artifacts",
                                     body, publish, actor=_actor(identity))
            if action == "attention":
                _authorize(identity, "attention", run_id)
                def open_attention():
                    request, created = attention.open_request(
                        self.con, kind=body.get("kind", "question"),
                        title=body.get("title") or "Input needed",
                        body=body.get("body") or body.get("question") or "",
                        created_by=_actor(identity), run_id=run_id,
                        blocking=_bool(body.get("blocking"), True),
                        choices=body.get("choices"), fallback=body.get("fallback"),
                        proposal=body.get("proposal"),
                        correlation_id=body.get("correlation_id") or body["request_id"],
                        deadline=body.get("deadline"),
                        callback_command=config.callback_command())
                    return {"created": created, "attention": attention_payload(request)}
                return self.mutation(method, f"/api/v2/runs/{run_id}/attention",
                                     body, open_attention, actor=_actor(identity))
            if action == "profile":
                return self._run_profile_change(run, body, identity)
            if action in {"pin", "unpin"}:
                operator = _operator(identity)
                actor = _actor(operator)
                def change_pin():
                    if action == "pin":
                        return {"pin": storage.pin(
                            self.con, run_id, actor=actor, reason=body.get("reason"))}
                    return {"unpinned": storage.unpin(
                        self.con, run_id, actor=actor)}
                return self.mutation(
                    method, f"/api/v2/runs/{run_id}/{action}", body, change_pin,
                    actor=actor)
            return self._control(run, action, body, identity)
        raise Problem(404, "not_found", "No such run resource.")

    def _run_profile_change(self, run, body: dict, identity) -> Response:
        run_id = int(run["id"])
        _authorize(identity, "attention", run_id)
        changes = body.get("changes")
        if not isinstance(changes, dict) or not changes:
            raise Problem(422, "invalid_profile_change",
                          "changes must be a non-empty object.")
        profile = fleet_config.find_profile(self.con, run["profile_id"])
        if profile is None:
            raise Problem(404, "profile_not_found", "Run profile does not exist.")
        effort_order = {"none": 0, "minimal": 1, "low": 2, "medium": 3,
                        "high": 4, "xhigh": 5, "max": 6, "ultra": 7}
        direct = set(changes) <= {"effort", "note"}
        if "effort" in changes:
            before = effort_order.get(str(profile["effort"] or "medium").lower(), 3)
            after = effort_order.get(str(changes["effort"]).lower())
            direct = direct and after is not None and after <= before

        def apply():
            if direct:
                updated = fleet_config.update_profile(
                    self.con, profile["profile_id"], changes, actor=_actor(identity))
                return {"applied": True, "profile": profile_payload(self.con, updated)}
            request, created = attention.open_request(
                self.con, kind="profile_proposal", title=(
                    f"Profile change proposed by {db.run_no(run)}"),
                body="Review the requested profile configuration change.",
                created_by=_actor(identity), run_id=run_id, blocking=False,
                proposal={"profile_id": profile["profile_id"], "changes": changes,
                          "expected_revision": profile["revision"]},
                correlation_id=body["request_id"],
                callback_command=config.callback_command())
            return {"applied": False, "created": created,
                    "attention": attention_payload(request)}
        return self.mutation(
            "POST", f"/api/v2/runs/{run_id}/profile", body, apply,
            actor=_actor(identity))

    def _control(self, run, action: str, body: dict, identity) -> Response:
        from orchestra import child_runs, supervise
        run_id, actor = int(run["id"]), _actor(identity)
        if action == "children":
            _authorize(identity, "delegate", run_id)
            def child():
                item, created = child_runs.enqueue(
                    self.con, parent_run_id=run_id,
                    profiles=[str(body.get("profile") or "")],
                    context=str(body.get("context") or ""), title=body.get("title"),
                    requested_by=actor,
                    request_id=body["request_id"])
                return {"created": created,
                        "child_request": child_request_payload(item)}
            return self.mutation(
                "POST", f"/api/v2/runs/{run_id}/children", body, child,
                actor=actor)
        if action == "retry":
            _authorize(identity, "control", run_id)
            def retry():
                item, created = runs.clone(
                    self.con, run_id, request_id=body["request_id"], kind="retry",
                    requested_by=actor, request=body.get("context"),
                    profile=body.get("profile"))
                return {"created": created, "run": run_payload(item, detail=True)}
            return self.mutation(
                "POST", f"/api/v2/runs/{run_id}/retry", body, retry,
                actor=actor)
        if action == "merge":
            _authorize(identity, "control", run_id)
            def merge():
                from orchestra import worktree
                row = runs.find(self.con, run_id)
                if row["status"] not in db.RUN_TERMINAL:
                    raise Problem(409, "merge_not_ready",
                                  "Only a finished run can be merged.")
                if not row["repo"] or not row["branch"]:
                    raise Problem(409, "merge_no_changes",
                                  "This run has no retained branch to merge.")
                try:
                    result = worktree.merge_into_owner(
                        Path(row["repo"]), row["branch"])
                except RuntimeError as exc:
                    raise Problem(409, "merge_refused", str(exc)) from exc
                return {"merge": result}
            return self.mutation(
                "POST", f"/api/v2/runs/{run_id}/merge", body, merge, actor=actor)
        if action == "continue":
            _authorize(identity, "control", run_id)
            def continuation():
                item, created = runs.clone(
                    self.con, run_id, request_id=body["request_id"],
                    kind="continuation", requested_by=actor,
                    request=body.get("context"),
                    profile=body.get("profile"))
                return {"created": created, "run": run_payload(item, detail=True)}
            return self.mutation("POST", f"/api/v2/runs/{run_id}/continue",
                                 body, continuation, actor=actor)
        actions = {
            "tell": lambda: supervise.tell(
                self.con, run_id, str(body.get("text") or ""), actor,
                request_id=body.get("request_id")),
            "interrupt": lambda: supervise.interrupt(
                self.con, run_id, str(body.get("text") or ""), actor,
                request_id=body.get("request_id")),
            "stop": lambda: supervise.stop(
                self.con, run_id, actor, request_id=body.get("request_id")),
            "stop-tree": lambda: supervise.stop(
                self.con, run_id, actor, request_id=body.get("request_id"), tree=True),
            "check": lambda: supervise.check(
                self.con, run_id, actor, request_id=body.get("request_id")),
        }
        if action not in actions:
            raise Problem(404, "control_not_found", "No such run control.")
        _authorize(identity, "control", run_id)
        return self.mutation("POST", f"/api/v2/runs/{run_id}/{action}", body,
                             lambda: {"control": control_payload(
                                 action, actions[action]())}, actor=actor)

    def _resources(self, resource: str, method: str, tail: list[str], query,
                   body, identity) -> Response:
        if not tail and method == "GET":
            _authorize(identity, "read")
            include = _bool(query.get("include_archived"), False)
            if resource == "groups":
                items = [group_payload(self.con, row) for row in groups.all_groups(
                    self.con, include_archived=include)]
            elif resource == "runtimes":
                items = [runtime_payload(row)
                         for row in fleet_config.all_runtimes(
                             self.con, include_archived=include)]
            elif resource == "profiles":
                items = [profile_payload(self.con, row) for row in
                         fleet_config.all_profiles(self.con, include_archived=include)]
            else:
                items = [source_payload(self.con, row) for row in
                         fleet_config.all_runway_sources(
                             self.con, include_archived=include)]
            limit, offset = _limit(query), _page_offset(query.get("cursor"))
            page = items[offset:offset + limit]
            more = offset + limit < len(items)
            return self.response({
                "items": page,
                "next_cursor": _encode_page_offset(offset + limit) if more else None,
                "has_more": more,
            })
        if len(tail) == 1 and method == "GET":
            _authorize(identity, "read")
            selector = tail[0]
            if resource == "groups":
                row = groups.find(self.con, selector)
                value = group_payload(self.con, row) if row else None
            elif resource == "runtimes":
                row = fleet_config.find_runtime(self.con, selector)
                value = runtime_payload(row) if row else None
            elif resource == "profiles":
                row = fleet_config.find_profile(self.con, selector)
                value = profile_payload(self.con, row) if row else None
            else:
                row = fleet_config.find_runway_source(self.con, selector)
                value = source_payload(self.con, row) if row else None
            if value is None:
                raise Problem(404, "resource_not_found",
                              f"{resource[:-1]} does not exist.")
            return self.response(value)
        if not isinstance(body, dict):
            raise Problem(400, "invalid_body", "Request body must be an object.")
        if not tail and method == "POST" and resource == "groups" \
                and identity is not None and identity.kind == "service":
            # A dispatching integration may create the group it files its
            # runs under; reshaping or archiving groups stays operator work.
            actor = _actor(_authorize(identity, "dispatch"))
        else:
            actor = _actor(_operator(identity))
        if not tail and method == "POST":
            def create():
                clean = {key: value for key, value in body.items()
                         if key != "request_id"}
                if resource == "groups":
                    row = groups.create(self.con, actor=actor, **clean)
                    return {"group": group_payload(self.con, row)}
                if resource == "runtimes":
                    if "lifecycle" in clean:
                        raise ValueError(
                            "runtime lifecycle is not configurable; every runtime is per-run")
                    if "capabilities" in clean:
                        raise ValueError(
                            "runtime capabilities are adapter-owned and not publicly editable")
                    adapter = clean.pop("adapter", None) or clean.pop("kind", None)
                    command = clean.pop("command", None)
                    if command is None:
                        command = clean.pop("argv", ())
                    row = fleet_config.create_runtime(
                        self.con, adapter=adapter, command=command,
                        actor=actor, **clean)
                    return {"runtime": runtime_payload(row)}
                if resource == "profiles":
                    runtime_selector = clean.pop(
                        "runtime", clean.pop("runtime_id", None))
                    runway_selector = clean.pop(
                        "runway_source", clean.pop("runway_source_id", None))
                    if "active_cap" in clean:
                        clean["max_concurrency"] = clean.pop("active_cap")
                    row = fleet_config.create_profile(
                        self.con, runtime=runtime_selector,
                        runway_source=runway_selector, actor=actor, **clean)
                    return {"profile": profile_payload(self.con, row)}
                command = clean.pop("argv", ())
                row = fleet_config.create_runway_source(
                    self.con, command=command, actor=actor, **clean)
                return {"runway_source": source_payload(self.con, row)}
            return self.mutation(
                method, f"/api/v2/{resource}", body, create, actor=actor)
        selector = tail[0] if tail else ""
        if resource == "runway-sources" and len(tail) == 2 and \
                tail[1] == "refresh" and method == "POST":
            def refresh():
                source = fleet_config.find_runway_source(self.con, selector)
                if source is None:
                    raise Problem(404, "runway_source_not_found",
                                  "Runway source does not exist.")
                reading = runway.poll_source(source)
                runway.record_source_reading(self.con, reading)
                return {"runway_source": source_payload(self.con, source)}
            return self.mutation(method, f"/api/v2/runway-sources/{selector}/refresh",
                                 body, refresh, actor=actor)
        if len(tail) == 1 and method == "PATCH":
            def update():
                changes = {key: value for key, value in body.items()
                           if key not in ("request_id", "expected_revision")}
                revision = body.get("expected_revision")
                if not changes:
                    raise ValueError("update requires at least one changed field")
                if resource == "groups":
                    if set(changes) == {"archived"}:
                        row = groups.set_archived(
                            self.con, selector, bool(changes["archived"]),
                            expected_revision=revision, actor=actor)
                    elif set(changes) == {"name"}:
                        row = groups.rename(self.con, selector, changes["name"],
                                            expected_revision=revision, actor=actor)
                    elif set(changes) == {"cwd"}:
                        row = groups.set_cwd(
                            self.con, selector, changes["cwd"],
                            expected_revision=revision, actor=actor)
                    else:
                        raise ValueError(
                            "group update accepts exactly one of name, archived, or cwd")
                    return {"group": group_payload(self.con, row)}
                if resource == "runtimes":
                    if "archived" in changes:
                        if set(changes) != {"archived"}:
                            raise ValueError(
                                "runtime archive/restore must be a standalone update")
                        row = fleet_config.archive_runtime(
                            self.con, selector, bool(changes["archived"]),
                            expected_revision=revision, actor=actor)
                        return {"runtime": runtime_payload(row)}
                    if "capabilities" in changes:
                        raise ValueError(
                            "runtime capabilities are adapter-owned and not publicly editable")
                    if "kind" in changes:
                        changes["adapter"] = changes.pop("kind")
                    if "argv" in changes:
                        changes["command"] = changes.pop("argv")
                    row = fleet_config.update_runtime(
                        self.con, selector, changes, expected_revision=revision,
                        actor=actor)
                    return {"runtime": runtime_payload(row)}
                if resource == "profiles":
                    if "archived" in changes:
                        if set(changes) != {"archived"}:
                            raise ValueError(
                                "profile archive/restore must be a standalone update")
                        row = fleet_config.archive_profile(
                            self.con, selector, bool(changes["archived"]),
                            expected_revision=revision, actor=actor)
                        return {"profile": profile_payload(self.con, row)}
                    if "active_cap" in changes:
                        changes["max_concurrency"] = changes.pop("active_cap")
                    row = fleet_config.update_profile(
                        self.con, selector, changes, expected_revision=revision,
                        actor=actor)
                    return {"profile": profile_payload(self.con, row)}
                if "archived" in changes:
                    if set(changes) != {"archived"}:
                        raise ValueError(
                            "runway source archive/restore must be a standalone update")
                    row = fleet_config.archive_runway_source(
                        self.con, selector, bool(changes["archived"]),
                        expected_revision=revision, actor=actor)
                    return {"runway_source": source_payload(self.con, row)}
                if "argv" in changes:
                    changes["command"] = changes.pop("argv")
                row = fleet_config.update_runway_source(
                    self.con, selector, changes, expected_revision=revision, actor=actor)
                return {"runway_source": source_payload(self.con, row)}
            return self.mutation(
                method, f"/api/v2/{resource}/{selector}", body, update,
                actor=actor)
        raise Problem(404, "not_found", "No such managed resource.")

    def _settings(self, method: str, body, identity) -> Response:
        if method == "GET":
            _authorize(identity, "read")
            return self.response({"items": [
                {"key": row["key"], "value": _json(row["value_json"]),
                 "revision": row["revision"], "updated_by": row["updated_by"],
                 "updated_at": row["updated_at"]}
                for row in self.con.execute(
                    "SELECT * FROM fleet_settings ORDER BY key") ]})
        if method != "PATCH":
            raise Problem(405, "method_not_allowed", "Settings updates require PATCH.")
        _operator(identity)
        if not isinstance(body, dict):
            raise Problem(400, "invalid_body", "Request body must be an object.")
        def update():
            row = fleet_config.set_fleet_setting(
                self.con, body.get("key"), body.get("value"),
                expected_revision=body.get("expected_revision"),
                actor=_actor(identity))
            return {"setting": {
                "key": row["key"], "value": _json(row["value_json"]),
                "revision": row["revision"], "updated_by": row["updated_by"],
                "updated_at": row["updated_at"]}}
        return self.mutation(
            method, "/api/v2/settings", body, update, actor=_actor(identity))

    def _observer(self, method: str, body, identity) -> Response:
        if method == "GET":
            _authorize(identity, "read")
            return self.response(observer_settings_payload(
                fleet_config.observer(self.con)))
        if method != "PATCH":
            raise Problem(405, "method_not_allowed", "Observer updates require PATCH.")
        _operator(identity)
        if not isinstance(body, dict):
            raise Problem(400, "invalid_body", "Request body must be an object.")
        def update():
            current = fleet_config.observer(self.con)
            profile = body.get("profile", body.get("profile_id", current["profile_id"]))
            enabled = _bool(body.get("enabled"), profile is not None)
            updated = fleet_config.configure_observer(
                self.con, enabled=enabled, profile=profile,
                max_concurrency=body.get(
                    "concurrency", current["max_concurrency"]),
                first_look_seconds=int(body.get(
                    "first_look_seconds", body.get(
                        "first_check_seconds", current["first_look_seconds"]))),
                minimum_events=int(body.get(
                    "minimum_events", current["minimum_events"])),
                interval_seconds=int(body.get(
                    "interval_seconds", body.get(
                        "subsequent_check_seconds", current["interval_seconds"]))),
                authority=body.get("authority", current["authority"]),
                expected_revision=body.get("expected_revision"),
                actor=_actor(identity))
            return {"observer": observer_settings_payload(updated)}
        return self.mutation(
            method, "/api/v2/observer", body, update, actor=_actor(identity))

    def _scheduler(self, method: str, tail: list[str], body, identity) -> Response:
        if method == "GET" and not tail:
            _authorize(identity, "read")
            return self.response(scheduler.state(self.con))
        _operator(identity)
        if method == "POST" and tail and tail[0] in {"pause", "resume"}:
            if not isinstance(body, dict):
                raise Problem(400, "invalid_body", "Request body must be an object.")
            paused = tail[0] == "pause"
            return self.mutation(method, f"/api/v2/scheduler/{tail[0]}", body,
                                 lambda: {"scheduler": scheduler.set_paused(
                                     self.con, paused, actor=_actor(identity),
                                     request_id=body["request_id"],
                                     note=body.get("note"))},
                                 actor=_actor(identity))
        raise Problem(404, "not_found", "No such scheduler resource.")

    def _attention(self, root: str, method: str, tail: list[str], query,
                   body, identity) -> Response:
        _authorize(identity, "read" if method == "GET" else "answer")
        if root == "inbox" and method == "GET":
            state = query.get("state", "open")
            if state not in {"open", "resolved", "cancelled"}:
                raise Problem(422, "invalid_attention_state",
                              f"Unknown attention state {state!r}.")
            kind = query.get("kind")
            if kind and kind not in {"question", "decision", "alert",
                                     "profile_proposal"}:
                raise Problem(422, "invalid_attention_kind",
                              f"Unknown attention kind {kind!r}.")
            limit, values = _limit(query), [state, _cursor(query.get("cursor")) or 0]
            where = "status=? AND id>?"
            if kind:
                where += " AND kind=?"
                values.append(kind)
            rows_ = [dict(row) for row in self.con.execute(
                f"SELECT * FROM attention_requests WHERE {where} "
                "ORDER BY id LIMIT ?", (*values, limit + 1))]
            more, rows_ = len(rows_) > limit, rows_[:limit]
            return self.response({
                "items": [attention_payload(item) for item in rows_],
                "next_cursor": _encode_cursor(rows_[-1]["id"]) if more else None,
                "has_more": more,
            })
        if root == "attention-feed" and method == "GET":
            after, limit = int(query.get("after", 0)), _limit(query, 200)
            rows_ = self.con.execute(
                "SELECT * FROM attention_requests WHERE revision>? "
                "ORDER BY revision,id LIMIT ?", (after, limit + 1)).fetchall()
            more, rows_ = len(rows_) > limit, rows_[:limit]
            return self.response({"items": [attention_payload(row) for row in rows_],
                                  "next_cursor": max(
                                      (row["revision"] for row in rows_), default=after),
                                  "has_more": more})
        if root == "attention" and tail:
            try:
                attention_id = int(tail[0])
            except ValueError as exc:
                raise Problem(404, "attention_not_found",
                              "Attention request does not exist.") from exc
            request = self.con.execute(
                "SELECT * FROM attention_requests WHERE id=?", (attention_id,)
            ).fetchone()
            if request is None:
                raise Problem(404, "attention_not_found",
                              "Attention request does not exist.")
            if len(tail) == 1 and method == "GET":
                value = attention_payload(request)
                value["responses"] = [{
                    "id": row["id"], "actor": row["actor"],
                    "response": _json(row["response_json"], {}),
                    "accepted": bool(row["accepted"]),
                    "created_at": row["created_at"],
                } for row in self.con.execute(
                    "SELECT * FROM attention_responses WHERE attention_id=? "
                    "ORDER BY id", (attention_id,))]
                return self.response(value)
            if method == "POST" and len(tail) == 2 and tail[1] in {
                    "answer", "approve", "reject", "acknowledge"}:
                if not isinstance(body, dict):
                    raise Problem(400, "invalid_body", "Request body must be an object.")
                action = tail[1]
                allowed_actions = {
                    "question": {"answer"},
                    "decision": {"answer"},
                    "profile_proposal": {"approve", "reject"},
                    "alert": {"acknowledge"},
                }
                if action not in allowed_actions.get(request["kind"], set()):
                    raise Problem(
                        409, "invalid_attention_action",
                        f"{request['kind']} attention does not support {action}.")
                answer_value, choice_value = body.get("answer"), body.get("choice")
                if answer_value is not None and not isinstance(answer_value, str):
                    raise Problem(422, "invalid_attention_answer",
                                  "answer must be a string.")
                if choice_value is not None and not isinstance(choice_value, str):
                    raise Problem(422, "invalid_attention_answer",
                                  "choice must be a string.")
                response = {"body": answer_value or ""}
                if choice_value:
                    response["choice"] = choice_value
                if action == "answer" and not response["body"].strip() and \
                        not response.get("choice"):
                    raise Problem(422, "empty_attention_answer",
                                  "An answer or choice is required.")
                if action in ("approve", "reject"):
                    response["choice"] = action
                if action == "acknowledge":
                    response["body"] = response["body"] or "Acknowledged"
                def answer_request():
                    apply = None
                    if request["kind"] == "profile_proposal" and \
                            response.get("choice") == "approve":
                        apply = lambda proposal, _response: apply_profile_proposal(
                            self.con, proposal, _actor(identity), commit=False)
                    result = attention.answer(
                        self.con, attention_id, actor=_actor(identity),
                        response=response, authorized=True, on_accept=apply)
                    if not result["accepted"]:
                        raise Problem(409, "attention_already_resolved",
                                      "Attention was already resolved.",
                                      {"resolution": attention_payload(result["request"])})
                    return {"attention": attention_payload(result["request"]),
                            "response_id": result["response_id"]}
                return self.mutation(method,
                                     f"/api/v2/attention/{attention_id}/{action}",
                                     body, answer_request, actor=_actor(identity))
        raise Problem(404, "not_found", "No such attention resource.")

    def _artifact(self, method: str, tail: list[str], identity):
        _authorize(identity, "read")
        if not tail:
            raise Problem(404, "artifact_not_found", "Artifact does not exist.")
        artifact_id = tail[0]
        item = artifacts.get(self.con, artifact_id)
        if item is None:
            raise Problem(404, "artifact_not_found", "Artifact does not exist.")
        if len(tail) == 1 and method == "GET":
            return self.response(artifact_payload(item))
        if len(tail) == 2 and tail[1] in {"content", "download"} and method == "GET":
            if not item.get("available", True):
                raise Problem(410, "artifact_pruned",
                              "Artifact content was explicitly pruned.")
            stored = artifacts.stored_file(self.con, artifact_id)
            if stored is None:
                raise Problem(404, "artifact_not_found", "Artifact does not exist.")
            path, metadata = stored
            return FileResponse(path, metadata["mime_type"], metadata["name"],
                                tail[1] == "download")
        raise Problem(404, "not_found", "No such artifact resource.")

    def _storage(self, method: str, tail: list[str], body, identity) -> Response:
        operator = _operator(identity)
        actor = _actor(operator)
        if method == "GET" and not tail:
            return self.response(storage.report(self.con))
        if method == "GET" and len(tail) == 2 and tail[0] == "prune-plans":
            plan = storage.get_plan(self.con, tail[1])
            if plan is None:
                raise Problem(404, "prune_plan_not_found",
                              "Prune plan does not exist.")
            return self.response(prune_plan_payload(plan))
        if not isinstance(body, dict):
            raise Problem(400, "invalid_body", "Request body must be an object.")
        if method == "POST" and tail == ["prune-plan"]:
            return self.mutation(
                method, "/api/v2/storage/prune-plan", body,
                lambda: {"plan": prune_plan_payload(storage.create_plan(
                    self.con, actor=actor,
                    older_than_days=body.get("older_than_days", 30),
                    kinds=body.get("kinds")))}, actor=actor)
        if method == "POST" and len(tail) == 3 and tail[0] == "prune-plans" \
                and tail[2] == "apply":
            plan_id = tail[1]
            return self.mutation(
                method, f"/api/v2/storage/prune-plans/{plan_id}/apply", body,
                lambda: {"plan": prune_plan_payload(storage.apply_plan(
                    self.con, plan_id, actor=actor))}, actor=actor)
        raise Problem(404, "not_found", "No such storage resource.")

    def _credentials(self, root: str, method: str, tail: list[str], body,
                     identity) -> Response:
        device = _operator(identity)
        if root == "devices":
            if not tail and method == "GET":
                return self.response({"items": [device_payload(row) for row in
                    self.con.execute("SELECT * FROM devices ORDER BY created_at")]})
            if tail == ["pairing"] and method == "POST":
                if not isinstance(body, dict):
                    raise Problem(400, "invalid_body", "Request body must be an object.")
                def pair():
                    value = auth.create_pairing(
                        self.con, created_by_device_id=device.subject_id,
                        commit=False)
                    uri = "orchestra://pair?instance_id=" + quote(
                        db.instance_id(self.con))
                    advertised = os.environ.get("ORCHESTRA_URL", "").strip()
                    if advertised:
                        uri += "&endpoint=" + quote(advertised.rstrip("/"), safe="")
                    value["pairing_uri"] = (
                        uri + "&pairing_id=" + quote(value["pairing_id"]) +
                        "&code=" + quote(value["code"]))
                    return value
                return self.secret_mutation(
                    method, "/api/v2/devices/pairing", body, pair,
                    actor=_actor(device))
            if len(tail) == 1 and method == "PATCH":
                if not isinstance(body, dict) or not _bool(body.get("revoked"), False):
                    raise Problem(422, "invalid_device_update",
                                  "Only revocation is supported.")
                def revoke_device_patch():
                    try:
                        return {"revoked": auth.revoke_device(self.con, tail[0])}
                    except auth.AuthError as exc:
                        raise Problem(409, "last_device", str(exc)) from exc
                return self.mutation(method, f"/api/v2/devices/{tail[0]}",
                                     body, revoke_device_patch,
                                     actor=_actor(device))
        if root == "service-tokens":
            if not tail and method == "GET":
                return self.response({"items": [service_token_payload(row)
                    for row in self.con.execute(
                        "SELECT * FROM service_tokens ORDER BY created_at")]})
            if not tail and method == "POST":
                if not isinstance(body, dict):
                    raise Problem(400, "invalid_body", "Request body must be an object.")
                def create():
                    try:
                        record, token = auth.create_service_token(
                            self.con, body.get("name") or body.get("label"),
                            body.get("authorities"), commit=False)
                    except auth.AuthError as exc:
                        raise Problem(422, "invalid_service_token", str(exc)) from exc
                    return {"service_token": service_token_payload(record),
                            "token": token}
                return self.secret_mutation(
                    method, "/api/v2/service-tokens", body, create,
                    actor=_actor(device))
            if len(tail) == 1 and method == "PATCH":
                if not isinstance(body, dict) or not _bool(body.get("revoked"), False):
                    raise Problem(422, "invalid_service_token_update",
                                  "Only revocation is supported.")
                return self.mutation(
                    method, f"/api/v2/service-tokens/{tail[0]}", body,
                    lambda: {"revoked": auth.revoke_service_token(
                        self.con, tail[0])}, actor=_actor(device))
        raise Problem(404, "not_found", "No such credential resource.")

    def _redeem(self, body) -> Response:
        if not isinstance(body, dict):
            raise Problem(400, "invalid_body", "Request body must be an object.")
        browser = _bool(body.get("browser"), False)
        issued = {}

        def redeem():
            try:
                record, token = auth.redeem_pairing(
                    self.con, str(body.get("pairing_id") or ""),
                    str(body.get("code") or ""),
                    str(body.get("name") or body.get("label") or ""),
                    commit=False)
            except auth.AuthError as exc:
                raise Problem(401, "pairing_failed", str(exc)) from exc
            issued["token"] = token
            payload = {"device": device_payload(record)}
            if not browser:
                payload["token"] = token
            return payload

        response = self.secret_mutation(
            "POST", "/api/v2/pairing/redeem", body, redeem,
            actor=lambda data: f"device:{data['device']['id']}",
            audit_action="device.pair",
            target=lambda data: ("device", data["device"]["id"]),
        )
        if browser:
            response.headers["Set-Cookie"] = (
                f"orchestra_device={issued['token']}; Secure; HttpOnly; "
                "SameSite=Strict; Path=/")
        return response


def group_payload(con, row) -> dict:
    stats = con.execute(
        "SELECT COUNT(*) AS total,SUM(CASE WHEN status IN ('starting','running') "
        "THEN 1 ELSE 0 END) AS active,SUM(CASE WHEN status='queued' THEN 1 ELSE 0 END) "
        "AS queued FROM runs WHERE group_id=?", (row["group_id"],)).fetchone()
    summary = {"runs": int(stats["total"] or 0),
               "active": int(stats["active"] or 0),
               "queued": int(stats["queued"] or 0)}
    return {
        "id": row["group_id"], "slug": row["slug"], "name": row["name"],
        "archived": bool(row["archived"]),
        "cwd_configured": bool(row["default_cwd"]),
        "next_number": int(row["last_run_seq"]) + 1,
        "runs_count": summary["runs"], "stats": summary,
        "revision": row["revision"], "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def runtime_payload(row) -> dict:
    value = _managed(row, ("command_json", "capabilities_json", "config_json"))
    capabilities = value["capabilities"]
    return {
        "id": row["runtime_id"], "slug": row["slug"], "name": row["name"],
        "kind": row["adapter"],
        "argv": _public_argv(value["command"]), "enabled": bool(row["enabled"]),
        "config_configured": bool(value["config"]),
        "archived": bool(row["archived"]),
        "supports_steering": bool(capabilities.get("steering", False)),
        "supports_interrupt": bool(capabilities.get("interrupt", False)),
        "revision": row["revision"], "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def profile_payload(con, row) -> dict:
    runtime_row = fleet_config.find_runtime(con, row["runtime_id"])
    source = fleet_config.find_runway_source(con, row["runway_source_id"] or "")
    observer_compatible, observer_incompatibility = \
        fleet_config.observer_profile_compatibility(con, row)
    stats = {status: int(count) for status, count in con.execute(
        "SELECT status,COUNT(*) FROM runs WHERE profile_id=? GROUP BY status",
        (row["profile_id"],))}
    return {
        "id": row["profile_id"], "slug": row["slug"], "name": row["name"],
        "runtime_id": row["runtime_id"],
        "runtime_name": runtime_row["name"] if runtime_row else None,
        "model": row["model"], "effort": row["effort"],
        "tier": row["tier"], "priority": row["priority"],
        "sandbox": row["sandbox"], "timeout_seconds": row["timeout_seconds"],
        "active_cap": row["max_concurrency"],
        "runway_source_id": row["runway_source_id"],
        "runway_source_name": source["name"] if source else None,
        "env_configured": bool(_json(row["env_json"], {})),
        "config_configured": bool(_json(row["config_json"], {})),
        "observer_compatible": observer_compatible,
        "observer_incompatibility": observer_incompatibility,
        "note": row["note"], "enabled": bool(row["enabled"]),
        "archived": bool(row["archived"]), "stats": stats,
        "revision": row["revision"], "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def source_payload(con, row) -> dict:
    readings = list(con.execute(
        "SELECT * FROM runway_readings WHERE source_id=? "
        "ORDER BY id DESC LIMIT 100", (row["source_id"],)))

    def public_number(value):
        return value if isinstance(value, (int, float)) \
            and not isinstance(value, bool) else None

    def public_text(value):
        return value if isinstance(value, str) and value else None

    def credit_payload(raw) -> dict | None:
        value = raw.get("credits") if isinstance(raw, dict) else None
        if isinstance(value, str):  # readings written before typed credits
            return {"text": value, "count": None, "expires_at": None}
        if not isinstance(value, dict):
            return None
        count = value.get("count")
        count = count if isinstance(count, int) and not isinstance(count, bool) \
            and count >= 0 else None
        text, expires_at = public_text(value.get("text")), \
            public_text(value.get("expires_at"))
        return {"text": text, "count": count, "expires_at": expires_at} \
            if text is not None or count is not None or expires_at is not None \
            else None

    def observation(item, *, live=False) -> dict:
        managed = _managed(item, ("windows_json", "raw_json"))
        windows = []
        for index, window in enumerate(managed.get("windows") or []):
            if not isinstance(window, dict):
                continue
            if live:
                try:
                    window = runway.as_of_now(dict(window))
                except (TypeError, ValueError):
                    window = {**window, "remaining": None, "stale": True,
                              "stale_reason": "invalid reset timestamp"}
            name = str(window.get("name") or window.get("label") or
                       window.get("window") or f"Window {index + 1}")
            remaining = window.get("remaining_percent", window.get("remaining"))
            windows.append({
                "id": str(window.get("id") or name), "name": name,
                "remaining_percent": public_number(remaining),
                "resets_at": public_text(window.get("resets_at")),
                "unit": public_text(window.get("unit")) or "percent",
                "stale": bool(window.get("stale", False)),
                "stale_reason": public_text(window.get("stale_reason")),
                "per_model": bool(window.get("per_model", False)),
            })
        return {
            "observed_at": managed.get("as_of") or managed.get("polled_at"),
            "polled_at": managed.get("polled_at"),
            "fresh_until": managed.get("fresh_until"),
            "definitive": bool(managed.get("definitive")),
            "remaining": public_number(managed.get("remaining")),
            "unit": public_text(managed.get("unit")),
            "resets_at": public_text(managed.get("resets_at")),
            "credits": credit_payload(managed.get("raw")),
            "reason": managed.get("reason"), "burn_rate": None,
            "windows": windows,
        }

    def instant(value):
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    def adjacent_burn(current, previous) -> float | None:
        """Fastest real comparable window, never an average across windows."""
        if not current.get("definitive") or not previous.get("definitive"):
            return None
        current_at = instant(current.get("observed_at"))
        previous_at = instant(previous.get("observed_at"))
        if current_at is None or previous_at is None or current_at <= previous_at:
            return None
        hours = (current_at - previous_at).total_seconds() / 3600
        older = {window["id"]: window for window in previous.get("windows", [])}
        candidates = []
        for window in current.get("windows", []):
            prior = older.get(window["id"])
            if prior is None or window.get("unit") != prior.get("unit") or \
                    str(window.get("unit", "")).lower() not in {
                        "percent", "percentage", "%"}:
                continue
            current_reset = instant(window.get("resets_at"))
            previous_reset = instant(prior.get("resets_at"))
            if current_reset != previous_reset:
                continue
            current_remaining = window.get("remaining_percent")
            prior_remaining = prior.get("remaining_percent")
            if isinstance(current_remaining, bool) or isinstance(prior_remaining, bool):
                continue
            if not isinstance(current_remaining, (int, float)) or not isinstance(
                    prior_remaining, (int, float)):
                continue
            consumed = float(prior_remaining) - float(current_remaining)
            if consumed < 0:
                continue
            candidates.append(consumed / hours)
        return round(max(candidates), 6) if candidates else None

    history = [observation(item) for item in readings]
    for index in range(len(history) - 1):
        history[index]["burn_rate"] = adjacent_burn(
            history[index], history[index + 1])
    current = observation(readings[0], live=True) if readings else None
    if current:
        current["burn_rate"] = history[0]["burn_rate"]
    linked = [dict(item) for item in con.execute(
        "SELECT profile_id,slug,name FROM profiles WHERE runway_source_id=? "
        "AND archived=0 ORDER BY name", (row["source_id"],))]
    fresh = False
    if current and current.get("fresh_until"):
        try:
            until = datetime.fromisoformat(
                str(current["fresh_until"]).replace("Z", "+00:00"))
            fresh = until > datetime.now(timezone.utc)
        except ValueError:
            pass
    return {
        "id": row["source_id"], "slug": row["slug"], "name": row["name"],
        "provider": row["provider"], "account": row["account"],
        "lane": row["lane"], "adapter": row["adapter"],
        "kind": runway.kind_of(
            row["provider"] if row["adapter"] == "command" else row["adapter"]),
        "argv_configured": bool(_json(row["command_json"], [])),
        "config_configured": bool(_json(row["config_json"], {})),
        "enabled": bool(row["enabled"]),
        "archived": bool(row["archived"]), "fresh": fresh,
        "status": ("unknown" if not current or not current.get("definitive")
                   else "current" if fresh else "stale"),
        "observed_at": current.get("observed_at") if current else None,
        "polled_at": current.get("polled_at") if current else None,
        "fresh_until": current.get("fresh_until") if current else None,
        "definitive": current.get("definitive", False) if current else False,
        "remaining": current.get("remaining") if current else None,
        "unit": current.get("unit") if current else None,
        "resets_at": current.get("resets_at") if current else None,
        "credits": current.get("credits") if current else None,
        "reason": current.get("reason") if current else None,
        "burn_rate": current.get("burn_rate") if current else None,
        "windows": current.get("windows", []) if current else [],
        "linked_profile_ids": [item["profile_id"] for item in linked],
        "history": history, "revision": row["revision"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def attention_payload(row) -> dict:
    return {
        "id": str(row["id"]), "correlation_id": row["correlation_id"],
        "run_id": row["run_id"], "kind": row["kind"],
        "state": row["status"], "prompt": row["title"],
        "detail": row["body"], "blocking": bool(row["blocking"]),
        "choices": _json(row["choices_json"], []) or [],
        "fallback": _json(row["fallback_json"]),
        "proposal": _json(row["proposal_json"]),
        "deadline": row["deadline"], "opened_at": row["created_at"],
        "created_by": row["created_by"], "resolved_at": row["resolved_at"],
        "resolution": _json(row["resolution_json"]),
        "resolved_by": row["resolved_by"], "revision": row["revision"],
    }


def observer_settings_payload(row) -> dict:
    value = dict(row)
    return {
        "enabled": bool(value["enabled"]), "profile_id": value["profile_id"],
        "concurrency": int(value["max_concurrency"]),
        "first_check_seconds": value["first_look_seconds"],
        "minimum_events": value["minimum_events"],
        "subsequent_check_seconds": value["interval_seconds"],
        "authority": value["authority"], "revision": value["revision"],
        "updated_by": value["updated_by"], "updated_at": value["updated_at"],
    }


def observer_check_payload(item) -> dict:
    value = dict(item)
    return {
        "id": int(value["id"]), "run_id": int(value["run_id"]),
        "profile_id": value.get("profile_id"), "trigger": value.get("trigger"),
        "judgment": value.get("verdict"),
        "action": value.get("action") or "checking",
        "rationale": value.get("reason"),
        "evidence_from": value.get("event_seq_start"),
        "evidence_to": value.get("event_seq_end"),
        "created_at": value.get("started_at"),
        "finished_at": value.get("finished_at"),
        "log_available": bool(value.get("log_path")) and
                         value.get("log_pruned_at") is None,
        "log_pruned_at": value.get("log_pruned_at"),
        "usage": {
            "input_tokens": value.get("tokens_in"),
            "output_tokens": value.get("tokens_out"),
            "total_tokens": value.get("tokens_total"),
            "cost_usd": value.get("cost_usd"),
        },
        "detail": _json(value.get("detail_json"), {}),
    }


def artifact_payload(item) -> dict:
    return {
        "id": item.get("id") or item.get("artifact_id"),
        "run_id": int(item["run_id"]), "name": item["name"],
        "relative_path": item.get("relative_path"),
        "media_type": item.get("media_type") or item.get("mime_type") or
                      "application/octet-stream",
        "byte_size": int(item.get("byte_size", item.get("size_bytes", 0))),
        "sha256": item["sha256"], "created_at": item.get("created_at"),
        "available": bool(item.get("available", True)),
        "pruned_at": item.get("pruned_at"),
    }


def message_payload(item) -> dict:
    value = dict(item)
    result = {
        "id": int(value["id"]), "run_id": int(value["run_id"]),
        "direction": value.get("direction"), "sender": value.get("sender"),
        "kind": value.get("kind"), "status": value.get("status"),
        "body": value.get("body") or "",
        "correlation_id": value.get("correlation_id"),
        "reply_to": value.get("reply_to"), "created_at": value.get("created_at"),
        "delivered_at": value.get("delivered_at"),
        "undeliverable_at": value.get("undeliverable_at"),
        "delivery_error": value.get("undeliverable_reason"),
    }
    if value.get("group_name") and value.get("group_seq"):
        result["display"] = f"{value['group_name']} #{value['group_seq']}"
    return result


def event_payload(item) -> dict:
    value = dict(item)
    return {
        "id": int(value["id"]), "seq": int(value["seq"]),
        "kind": value["kind"], "name": value.get("name"),
        "payload": value.get("payload") or "",
        "truncated": bool(value.get("truncated", False)),
        "created_at": value.get("created_at"),
    }


def control_payload(action: str, item) -> dict:
    value = dict(item)
    audit_id = value.pop("control_audit_id", None)
    if action in {"tell", "interrupt"}:
        metadata = {key: value.pop(key) for key in (
            "delivery_mode", "resume_mode", "fallback") if key in value}
        result = {"message": message_payload(value), **metadata}
        outcome = "queued"
    else:
        result = value
        outcome = "ok"
    return {"audit_id": audit_id, "action": action,
            "outcome": outcome, "result": result}


def child_request_payload(item) -> dict:
    value = dict(item)
    return {
        "id": int(value["id"]), "request_id": value["request_id"],
        "parent_run_id": int(value["parent_run_id"]),
        "requested_by": value["requested_by"],
        "profiles": _json(value.get("targets_json"), []) or [],
        "context": value["mission"], "title": value.get("title"),
        "status": value["status"],
        "child_run_ids": _json(value.get("child_run_ids_json"), []) or [],
        "error": value.get("error"), "created_at": value.get("created_at"),
        "processed_at": value.get("processed_at"),
    }


def prune_plan_payload(item) -> dict:
    value = dict(item)
    items = []
    for raw in value.get("items") or []:
        items.append({key: raw[key] for key in (
            "kind", "run_id", "check_id", "artifact_id", "size_bytes", "sha256")
            if key in raw})
    raw_result = value.get("result")
    result = None
    if isinstance(raw_result, dict):
        receipts = [{key: raw[key] for key in (
            "kind", "run_id", "check_id", "artifact_id", "status", "reason",
            "bytes") if key in raw}
            for raw in raw_result.get("items") or []]
        result = {
            "items": receipts,
            "pruned_items": int(raw_result.get("pruned_items", 0)),
            "pruned_bytes": int(raw_result.get("pruned_bytes", 0)),
            "skipped_items": int(raw_result.get("skipped_items", 0)),
        }
    return {
        "id": value.get("plan_id"), "criteria": value.get("criteria") or {},
        "items": items, "item_count": len(items),
        "bytes": sum(raw.get("size_bytes", 0) for raw in items),
        "created_by": value.get("created_by"),
        "created_at": value.get("created_at"),
        "applied_at": value.get("applied_at"),
        "result": result,
    }


def device_payload(row) -> dict:
    value = dict(row)
    return {
        "id": value["device_id"], "label": value["name"],
        "created_at": value.get("created_at"),
        "last_used_at": value.get("last_seen_at"),
        "revoked_at": value.get("revoked_at"),
    }


def service_token_payload(row) -> dict:
    value = dict(row)
    raw = value.get("authorities_json")
    return {
        "id": value["token_id"], "label": value["name"],
        "authorities": (value.get("authorities") if raw is None else
                        _json(raw, [])) or [],
        "created_at": value.get("created_at"),
        "last_used_at": value.get("last_seen_at"),
        "revoked_at": value.get("revoked_at"),
    }


def apply_profile_proposal(con, request, actor: str, *, commit: bool = True) -> None:
    proposal = _json(request["proposal_json"], {})
    selector = proposal.get("profile_id") or proposal.get("profile")
    changes = proposal.get("changes") or proposal.get("patch")
    if not selector or not isinstance(changes, dict):
        raise ValueError("profile proposal lacks profile_id and changes")
    fleet_config.update_profile(
        con, selector, changes, expected_revision=proposal.get("expected_revision"),
        actor=actor, commit=commit)


def openapi() -> dict:
    """The complete machine-readable v2 route and core wire contract."""
    # (method, summary, request schema, response kind, public)
    routes = {
        "/health": [("get", "Liveness", None, "health", True)],
        "/api/v2/openapi.json": [
            ("get", "OpenAPI description", None, "openapi", False)],
        "/api/v2/snapshot": [("get", "Fleet snapshot", None, "json", False)],
        "/api/v2/statistics": [("get", "Filtered run statistics", None, "json", False)],
        "/api/v2/service-log": [("get", "Live daemon log tail", None, "json", False)],
        "/api/v2/stream": [("get", "Fleet invalidation stream", None, "sse", False)],
        "/api/v2/run-feed": [("get", "Revision-ordered run feed", None, "json", False)],
        "/api/v2/runs": [
            ("get", "List runs", None, "json", False),
            ("post", "Admit a run", "RunRequest", "json", False)],
        "/api/v2/runs/{run_id}": [("get", "Run detail", None, "json", False)],
        "/api/v2/runs/{run_id}/stream": [
            ("get", "Live normalized run evidence", None, "sse", False)],
        "/api/v2/runs/{run_id}/thread": [("get", "Run thread", None, "json", False)],
        "/api/v2/runs/{run_id}/events": [("get", "Run events", None, "json", False)],
        "/api/v2/runs/{run_id}/lineage": [("get", "Run lineage", None, "json", False)],
        "/api/v2/runs/{run_id}/observer": [("get", "Observer checks", None, "json", False)],
        "/api/v2/runs/{run_id}/artifacts": [
            ("get", "Run artifacts", None, "json", False),
            ("post", "Publish artifact", "Mutation", "json", False)],
        "/api/v2/runs/{run_id}/changes": [("get", "Git evidence", None, "json", False)],
        "/api/v2/runs/{run_id}/log": [("get", "Retained raw log", None, "file", False)],
        "/api/v2/runs/{run_id}/children": [
            ("post", "Delegate child run", "Mutation", "json", False)],
        "/api/v2/runs/{run_id}/attention": [
            ("post", "Open attention", "Mutation", "json", False)],
        "/api/v2/runs/{run_id}/profile": [
            ("post", "Request profile change", "Mutation", "json", False)],
        "/api/v2/runs/{run_id}/tell": [("post", "Tell run", "Mutation", "json", False)],
        "/api/v2/runs/{run_id}/interrupt": [
            ("post", "Interrupt and redirect run", "Mutation", "json", False)],
        "/api/v2/runs/{run_id}/stop": [("post", "Stop run", "Mutation", "json", False)],
        "/api/v2/runs/{run_id}/stop-tree": [
            ("post", "Stop run tree", "Mutation", "json", False)],
        "/api/v2/runs/{run_id}/check": [("post", "Check run", "Mutation", "json", False)],
        "/api/v2/runs/{run_id}/retry": [("post", "Retry as new run", "Mutation", "json", False)],
        "/api/v2/runs/{run_id}/continue": [
            ("post", "Continue as new run", "Mutation", "json", False)],
        "/api/v2/runs/{run_id}/merge": [
            ("post", "Merge run branch into owner checkout", "Mutation", "json", False)],
        "/api/v2/runs/{run_id}/pin": [("post", "Pin run evidence", "Mutation", "json", False)],
        "/api/v2/runs/{run_id}/unpin": [
            ("post", "Unpin run evidence", "Mutation", "json", False)],
        "/api/v2/groups": [("get", "List groups", None, "json", False),
                            ("post", "Create group", "Mutation", "json", False)],
        "/api/v2/groups/{resource_id}": [
            ("get", "Group detail", None, "json", False),
            ("patch", "Update group", "Mutation", "json", False)],
        "/api/v2/runtimes": [("get", "List runtimes", None, "json", False),
                              ("post", "Create runtime", "Mutation", "json", False)],
        "/api/v2/runtimes/{resource_id}": [
            ("get", "Runtime detail", None, "json", False),
            ("patch", "Update runtime", "Mutation", "json", False)],
        "/api/v2/profiles": [("get", "List profiles", None, "json", False),
                              ("post", "Create profile", "Mutation", "json", False)],
        "/api/v2/profile-discovery": [
            ("get", "Discover daemon-host profile catalogs", None, "json", False)],
        "/api/v2/profiles/{resource_id}": [
            ("get", "Profile detail", None, "json", False),
            ("patch", "Update profile", "Mutation", "json", False)],
        "/api/v2/runway-sources": [
            ("get", "List runway sources", None, "json", False),
            ("post", "Create runway source", "Mutation", "json", False)],
        "/api/v2/runway-sources/{resource_id}": [
            ("get", "Runway source detail", None, "json", False),
            ("patch", "Update runway source", "Mutation", "json", False)],
        "/api/v2/runway-sources/{resource_id}/refresh": [
            ("post", "Refresh runway source", "Mutation", "json", False)],
        "/api/v2/settings": [("get", "Fleet settings", None, "json", False),
                             ("patch", "Update fleet setting", "Mutation", "json", False)],
        "/api/v2/observer": [("get", "Observer settings", None, "json", False),
                             ("patch", "Update Observer", "Mutation", "json", False)],
        "/api/v2/scheduler": [("get", "Scheduler state", None, "json", False)],
        "/api/v2/scheduler/pause": [
            ("post", "Pause new starts", "Mutation", "json", False)],
        "/api/v2/scheduler/resume": [
            ("post", "Resume new starts", "Mutation", "json", False)],
        "/api/v2/inbox": [("get", "Attention inbox", None, "json", False)],
        "/api/v2/inbox/stream": [("get", "Inbox stream", None, "sse", False)],
        "/api/v2/outbox": [
            ("get", "Fleet message ledger", None, "json", False)],
        "/api/v2/attention-feed": [
            ("get", "Revision-ordered attention feed", None, "json", False)],
        "/api/v2/attention/{attention_id}": [
            ("get", "Attention detail", None, "json", False)],
        "/api/v2/attention/{attention_id}/answer": [
            ("post", "Answer attention", "AttentionAnswer", "json", False)],
        "/api/v2/attention/{attention_id}/approve": [
            ("post", "Approve proposal", "Mutation", "json", False)],
        "/api/v2/attention/{attention_id}/reject": [
            ("post", "Reject proposal", "Mutation", "json", False)],
        "/api/v2/attention/{attention_id}/acknowledge": [
            ("post", "Acknowledge alert", "Mutation", "json", False)],
        "/api/v2/artifacts/{artifact_id}": [
            ("get", "Artifact metadata", None, "json", False)],
        "/api/v2/artifacts/{artifact_id}/content": [
            ("get", "Artifact content", None, "file", False)],
        "/api/v2/artifacts/{artifact_id}/download": [
            ("get", "Download artifact", None, "file", False)],
        "/api/v2/devices": [("get", "List paired devices", None, "json", False)],
        "/api/v2/devices/pairing": [
            ("post", "Create pairing code", "Mutation", "json", False)],
        "/api/v2/devices/{resource_id}": [
            ("patch", "Revoke device", "Mutation", "json", False)],
        "/api/v2/pairing/redeem": [
            ("post", "Redeem pairing code", "PairingRedeem", "json", True)],
        "/api/v2/service-tokens": [
            ("get", "List service tokens", None, "json", False),
            ("post", "Create service token", "Mutation", "json", False)],
        "/api/v2/service-tokens/{resource_id}": [
            ("patch", "Revoke service token", "Mutation", "json", False)],
        "/api/v2/storage": [("get", "Storage report", None, "json", False)],
        "/api/v2/storage/prune-plan": [
            ("post", "Create dry-run prune plan", "Mutation", "json", False)],
        "/api/v2/storage/prune-plans/{plan_id}": [
            ("get", "Prune plan detail", None, "json", False)],
        "/api/v2/storage/prune-plans/{plan_id}/apply": [
            ("post", "Apply reviewed prune plan", "Mutation", "json", False)],
    }

    request_types = {
        ("post", "/api/v2/runs/{run_id}/artifacts"): "ArtifactPublishRequest",
        ("post", "/api/v2/runs/{run_id}/children"): "ChildRequest",
        ("post", "/api/v2/runs/{run_id}/attention"): "AttentionOpenRequest",
        ("post", "/api/v2/runs/{run_id}/profile"): "ProfileChangeRequest",
        ("post", "/api/v2/runs/{run_id}/tell"): "TextControlRequest",
        ("post", "/api/v2/runs/{run_id}/interrupt"): "TextControlRequest",
        ("post", "/api/v2/runs/{run_id}/retry"): "RetryRequest",
        ("post", "/api/v2/runs/{run_id}/continue"): "ContinueRequest",
        ("post", "/api/v2/runs/{run_id}/pin"): "PinRequest",
        ("post", "/api/v2/groups"): "GroupCreateRequest",
        ("patch", "/api/v2/groups/{resource_id}"): "GroupUpdateRequest",
        ("post", "/api/v2/runtimes"): "RuntimeCreateRequest",
        ("patch", "/api/v2/runtimes/{resource_id}"): "RuntimeUpdateRequest",
        ("post", "/api/v2/profiles"): "ProfileCreateRequest",
        ("patch", "/api/v2/profiles/{resource_id}"): "ProfileUpdateRequest",
        ("post", "/api/v2/runway-sources"): "RunwaySourceCreateRequest",
        ("patch", "/api/v2/runway-sources/{resource_id}"):
            "RunwaySourceUpdateRequest",
        ("patch", "/api/v2/settings"): "SettingUpdateRequest",
        ("patch", "/api/v2/observer"): "ObserverUpdateRequest",
        ("post", "/api/v2/devices/pairing"): "PairingCreateRequest",
        ("patch", "/api/v2/devices/{resource_id}"): "RevokeRequest",
        ("post", "/api/v2/service-tokens"): "ServiceTokenCreateRequest",
        ("patch", "/api/v2/service-tokens/{resource_id}"): "RevokeRequest",
        ("post", "/api/v2/storage/prune-plan"): "PrunePlanRequest",
    }
    response_types = {
        ("get", "/api/v2/snapshot"): "FleetSnapshot",
        ("get", "/api/v2/statistics"): "Statistics",
        ("get", "/api/v2/run-feed"): "RunFeed",
        ("get", "/api/v2/runs"): "RunPage",
        ("post", "/api/v2/runs"): "RunAdmission",
        ("get", "/api/v2/runs/{run_id}"): "Run",
        ("get", "/api/v2/runs/{run_id}/thread"): "TimelineMessagePage",
        ("get", "/api/v2/runs/{run_id}/events"): "TimelineEventPage",
        ("get", "/api/v2/runs/{run_id}/lineage"): "RunLineage",
        ("get", "/api/v2/runs/{run_id}/observer"): "ObserverRunDetail",
        ("get", "/api/v2/runs/{run_id}/artifacts"): "ArtifactList",
        ("post", "/api/v2/runs/{run_id}/artifacts"): "ArtifactResult",
        ("get", "/api/v2/runs/{run_id}/changes"): "RunChanges",
        ("post", "/api/v2/runs/{run_id}/children"): "ChildAdmission",
        ("post", "/api/v2/runs/{run_id}/attention"): "AttentionOpenResult",
        ("post", "/api/v2/runs/{run_id}/profile"): "ProfileChangeResult",
        ("post", "/api/v2/runs/{run_id}/tell"): "ControlResult",
        ("post", "/api/v2/runs/{run_id}/interrupt"): "ControlResult",
        ("post", "/api/v2/runs/{run_id}/stop"): "ControlResult",
        ("post", "/api/v2/runs/{run_id}/stop-tree"): "ControlResult",
        ("post", "/api/v2/runs/{run_id}/check"): "ControlResult",
        ("post", "/api/v2/runs/{run_id}/retry"): "RunAdmission",
        ("post", "/api/v2/runs/{run_id}/continue"): "RunAdmission",
        ("post", "/api/v2/runs/{run_id}/merge"): "JsonObject",
        ("post", "/api/v2/runs/{run_id}/pin"): "JsonObject",
        ("post", "/api/v2/runs/{run_id}/unpin"): "JsonObject",
        ("get", "/api/v2/groups"): "GroupPage",
        ("post", "/api/v2/groups"): "GroupResult",
        ("get", "/api/v2/groups/{resource_id}"): "Group",
        ("patch", "/api/v2/groups/{resource_id}"): "GroupResult",
        ("get", "/api/v2/runtimes"): "RuntimePage",
        ("post", "/api/v2/runtimes"): "RuntimeResult",
        ("get", "/api/v2/runtimes/{resource_id}"): "Runtime",
        ("patch", "/api/v2/runtimes/{resource_id}"): "RuntimeResult",
        ("get", "/api/v2/profiles"): "ProfilePage",
        ("post", "/api/v2/profiles"): "ProfileResult",
        ("get", "/api/v2/profile-discovery"): "ProfileDiscovery",
        ("get", "/api/v2/profiles/{resource_id}"): "Profile",
        ("patch", "/api/v2/profiles/{resource_id}"): "ProfileResult",
        ("get", "/api/v2/runway-sources"): "RunwaySourcePage",
        ("post", "/api/v2/runway-sources"): "RunwaySourceResult",
        ("get", "/api/v2/runway-sources/{resource_id}"): "RunwaySource",
        ("patch", "/api/v2/runway-sources/{resource_id}"): "RunwaySourceResult",
        ("post", "/api/v2/runway-sources/{resource_id}/refresh"):
            "RunwaySourceResult",
        ("get", "/api/v2/settings"): "SettingList",
        ("patch", "/api/v2/settings"): "SettingResult",
        ("get", "/api/v2/observer"): "ObserverSettings",
        ("patch", "/api/v2/observer"): "ObserverSettingsResult",
        ("get", "/api/v2/scheduler"): "SchedulerState",
        ("post", "/api/v2/scheduler/pause"): "SchedulerResult",
        ("post", "/api/v2/scheduler/resume"): "SchedulerResult",
        ("get", "/api/v2/inbox"): "AttentionPage",
        ("get", "/api/v2/outbox"): "MessagePage",
        ("get", "/api/v2/attention-feed"): "AttentionFeed",
        ("get", "/api/v2/attention/{attention_id}"): "Attention",
        ("post", "/api/v2/attention/{attention_id}/answer"): "AttentionResult",
        ("post", "/api/v2/attention/{attention_id}/approve"): "AttentionResult",
        ("post", "/api/v2/attention/{attention_id}/reject"): "AttentionResult",
        ("post", "/api/v2/attention/{attention_id}/acknowledge"):
            "AttentionResult",
        ("get", "/api/v2/artifacts/{artifact_id}"): "Artifact",
        ("get", "/api/v2/devices"): "DeviceList",
        ("post", "/api/v2/devices/pairing"): "PairingCode",
        ("patch", "/api/v2/devices/{resource_id}"): "JsonObject",
        ("post", "/api/v2/pairing/redeem"): "PairingRedemption",
        ("get", "/api/v2/service-tokens"): "ServiceTokenList",
        ("post", "/api/v2/service-tokens"): "ServiceTokenSecretResult",
        ("patch", "/api/v2/service-tokens/{resource_id}"): "JsonObject",
        ("get", "/api/v2/storage"): "StorageReport",
        ("post", "/api/v2/storage/prune-plan"): "PrunePlanResult",
        ("get", "/api/v2/storage/prune-plans/{plan_id}"): "PrunePlan",
        ("post", "/api/v2/storage/prune-plans/{plan_id}/apply"):
            "PrunePlanResult",
    }
    query_parameters = {
        ("get", "/api/v2/statistics"):
            ("Group", "Profile", "Status"),
        ("get", "/api/v2/run-feed"): ("After", "Limit"),
        ("get", "/api/v2/runs"):
            ("Group", "Profile", "Status", "Search", "Limit", "Cursor"),
        ("get", "/api/v2/runs/{run_id}/thread"):
            ("TimelineDirection", "Limit", "Cursor"),
        ("get", "/api/v2/runs/{run_id}/events"):
            ("TimelineDirection", "Limit", "Cursor"),
        ("get", "/api/v2/groups"): ("Limit", "Cursor", "IncludeArchived"),
        ("get", "/api/v2/runtimes"): ("Limit", "Cursor", "IncludeArchived"),
        ("get", "/api/v2/profiles"): ("Limit", "Cursor", "IncludeArchived"),
        ("get", "/api/v2/profile-discovery"): ("LocalDiscovery",),
        ("get", "/api/v2/runway-sources"):
            ("Limit", "Cursor", "IncludeArchived"),
        ("get", "/api/v2/inbox"): ("State", "Kind", "Limit", "Cursor"),
        ("get", "/api/v2/outbox"): (
            "Direction", "MessageStatus", "MessageKind", "RunID", "Limit",
            "Cursor"),
        ("get", "/api/v2/attention-feed"): ("After", "Limit"),
        ("get", "/api/v2/stream"): ("After",),
        ("get", "/api/v2/runs/{run_id}/stream"): ("After",),
        ("get", "/api/v2/inbox/stream"): ("After",),
    }
    created_operations = {
        ("post", path) for path in (
            "/api/v2/runs", "/api/v2/groups",
            "/api/v2/runtimes", "/api/v2/profiles", "/api/v2/runway-sources")
    }
    secret_created_operations = {
        ("post", "/api/v2/devices/pairing"),
        ("post", "/api/v2/service-tokens"),
        ("post", "/api/v2/pairing/redeem"),
    }

    def enveloped(schema_name: str) -> dict:
        return {"allOf": [
            {"$ref": "#/components/schemas/Envelope"},
            {"type": "object", "properties": {"data": {
                "$ref": f"#/components/schemas/{schema_name}"}}},
        ]}

    def json_response(schema_name: str, description="Successful response") -> dict:
        return {"description": description, "content": {"application/json": {
            "schema": enveloped(schema_name)}}}

    error_response = {"description": "Problem", "content": {
        "application/json": {"schema": {"$ref": "#/components/schemas/Problem"}}}}
    paths_ = {}
    for route, definitions in routes.items():
        operations = {}
        parameters = []
        for segment in route.split("/"):
            if segment.startswith("{") and segment.endswith("}"):
                name = segment[1:-1]
                parameters.append({
                    "name": name, "in": "path", "required": True,
                    "schema": {"type": "integer" if name in {
                        "run_id", "attention_id"} else "string"},
                })
        for method, summary, request, kind, public in definitions:
            request = request_types.get((method, route), request)
            response_schema = response_types.get((method, route), "JsonObject")
            success = json_response(response_schema)
            operation = {
                "summary": summary,
                "operationId": method + "_" + "_".join(
                    part.strip("{}") for part in route.split("/") if part),
                "responses": {"200": success, "default": error_response},
            }
            if (method, route) in created_operations:
                operation["responses"] = {
                    "201": success, "200": json_response(
                        response_schema, "Idempotent replay"),
                    "default": error_response,
                }
            elif (method, route) in secret_created_operations:
                operation["responses"] = {"201": success, "default": error_response}
            operation_parameters = list(parameters)
            operation_parameters.extend(
                {"$ref": f"#/components/parameters/{name}"} for name in
                query_parameters.get((method, route), ()))
            if kind == "file":
                operation_parameters.append(
                    {"$ref": "#/components/parameters/Range"})
            if operation_parameters:
                operation["parameters"] = operation_parameters
            if request:
                operation["requestBody"] = {
                    "required": True, "content": {"application/json": {
                        "schema": {"$ref": f"#/components/schemas/{request}"}}}}
            if kind == "health":
                operation["responses"] = {"200": {"description": "Alive", "content": {
                    "application/json": {"schema": {"type": "object",
                        "required": ["status"], "properties": {
                            "status": {"const": "ok"}}}}}}}
            elif kind == "openapi":
                operation["responses"] = {"200": {"description": "OpenAPI 3.1 document",
                    "content": {"application/json": {"schema": {"type": "object"}}}}}
            elif kind == "sse":
                operation["responses"] = {"200": {"description": "SSE stream",
                    "content": {"text/event-stream": {"schema": {"type": "string"}}}}}
            elif kind == "file":
                operation["responses"] = {"200": {"description": "Range-capable bytes",
                    "content": {"application/octet-stream": {
                        "schema": {"type": "string", "format": "binary"}}}},
                    "206": {"description": "Partial bytes", "headers": {
                        "Content-Range": {"schema": {"type": "string"}}},
                        "content": {"application/octet-stream": {
                            "schema": {"type": "string", "format": "binary"}}}},
                    "416": {"description": "Range not satisfiable"},
                    "default": error_response}
            if public:
                operation["security"] = []
            operations[method] = operation
        paths_[route] = operations

    schemas = {
        "Problem": {"type": "object", "required": ["error"], "properties": {
            "error": {"type": "object", "required": ["code", "message"],
                "properties": {"code": {"type": "string"},
                    "message": {"type": "string"}, "details": {"type": "object"}}}}},
        "Envelope": {"type": "object", "required": ["api_version", "instance_id", "data"],
            "properties": {"api_version": {"const": 2},
                "instance_id": {"type": "string", "format": "uuid"},
                "data": {}}},
        "Mutation": {"type": "object", "required": ["request_id"],
            "properties": {"request_id": {"type": "string", "minLength": 1}},
            "additionalProperties": True},
        "AttentionAnswer": {"allOf": [
            {"$ref": "#/components/schemas/Mutation"},
            {"type": "object", "properties": {
                "answer": {"type": "string", "minLength": 1},
                "choice": {"type": "string", "minLength": 1}},
             "anyOf": [{"required": ["answer"]}, {"required": ["choice"]}]}]},
        "PairingRedeem": {"type": "object", "required": ["request_id", "code", "label"],
            "properties": {"request_id": {"type": "string"},
                "pairing_id": {"type": ["string", "null"]},
                "code": {"type": "string", "minLength": 12,
                         "description": "Grouped code; case, spaces, hyphens, and common ambiguous characters are normalized."},
                "label": {"type": "string"},
                "browser": {"type": "boolean"}}},
        "Dependency": {"type": "object", "required": ["run_id"], "properties": {
            "run_id": {"type": "integer"},
            "condition": {"enum": ["success", "terminal"]}}},
        "RunRequest": {"type": "object",
            "required": ["request_id", "profile", "context"],
            "properties": {"request_id": {"type": "string", "minLength": 1},
                "profile": {"type": "string"},
                "context": {"type": "string", "minLength": 1,
                            "description": "The executable request frozen for the worker."},
                "group": {"type": "string", "default": "general"},
                "title": {"type": ["string", "null"]},
                "cwd": {"type": "string", "minLength": 1, "writeOnly": True,
                        "description": "Optional daemon-host directory override; never returned."},
                "ref": {"type": ["string", "null"]},
                "after": {"type": "array", "items": {
                    "$ref": "#/components/schemas/Dependency"}},
                "requested_by": {"type": "string"},
                "observer": {"type": "string", "description":
                    "'inherit', 'off', or an enabled profile id/slug backed "
                    "by a claude, opencode, or reasonix runtime."}},
            "additionalProperties": False},
    }

    def ref(name: str) -> dict:
        return {"$ref": f"#/components/schemas/{name}"}

    def nullable(schema: dict) -> dict:
        return {"anyOf": [schema, {"type": "null"}]}

    def obj(properties: dict, required=()) -> dict:
        value = {"type": "object", "properties": properties}
        if required:
            value["required"] = list(required)
        return value

    def array(name: str) -> dict:
        return {"type": "array", "items": ref(name)}

    def page(name: str, *, numeric_cursor=False) -> dict:
        cursor = {"type": "integer"} if numeric_cursor else {"type": "string"}
        return obj({"items": array(name), "next_cursor": nullable(cursor),
                    "has_more": {"type": "boolean"}},
                   ("items", "next_cursor", "has_more"))

    def timeline_page(name: str) -> dict:
        return obj({"items": array(name),
                    "next_cursor": nullable({"type": "string"}),
                    "resume_cursor": nullable({"type": "string"}),
                    "has_more": {"type": "boolean"}},
                   ("items", "next_cursor", "resume_cursor", "has_more"))

    def result(name: str, key: str) -> dict:
        return obj({key: ref(name)}, (key,))

    def mutation_request(properties: dict, required=(), *,
                         description: str | None = None) -> dict:
        value = {"allOf": [ref("Mutation"), obj(properties, required)]}
        if description:
            value["description"] = description
        return value

    timestamp = nullable({"type": "string", "format": "date-time"})
    string_or_null = nullable({"type": "string"})
    integer_or_null = nullable({"type": "integer"})
    number_or_null = nullable({"type": "number"})
    schemas.update({
        "JsonObject": {"type": "object", "additionalProperties": True},
        "Usage": obj({
            "input_tokens": integer_or_null, "output_tokens": integer_or_null,
            "total_tokens": integer_or_null, "cost_usd": number_or_null,
            "cache_read_tokens": integer_or_null,
            "cache_write_tokens": integer_or_null,
            "source": string_or_null, "checks": {"type": "integer"},
        }, ("input_tokens", "output_tokens", "total_tokens", "cost_usd")),
        "RunHold": obj({"kind": {"type": "string"}, "detail": string_or_null},
                       ("kind", "detail")),
        "EvidencePin": obj({
            "run_id": {"type": "integer"}, "reason": string_or_null,
            "created_by": {"type": "string"}, "created_at": timestamp,
        }, ("run_id", "created_by", "created_at")),
        "RunCounts": obj({
            "messages": {"type": "integer", "minimum": 0},
            "events": {"type": "integer", "minimum": 0},
            "artifacts": {"type": "integer", "minimum": 0},
        }, ("messages", "events", "artifacts")),
        "ProfileSnapshot": obj({
            "id": string_or_null, "slug": string_or_null, "name": string_or_null,
            "runtime_id": string_or_null, "model": string_or_null,
            "effort": string_or_null, "tier": integer_or_null,
            "priority": integer_or_null, "sandbox": string_or_null,
            "timeout_seconds": integer_or_null, "active_cap": integer_or_null,
            "runway_source_id": string_or_null, "note": string_or_null,
            "enabled": {"type": "boolean"},
        }),
        "RuntimeSnapshot": obj({
            "id": string_or_null, "slug": string_or_null, "name": string_or_null,
            "kind": nullable({"enum": sorted(fleet_config.RUNTIME_ADAPTERS)}),
            "enabled": {"type": "boolean"},
            "supports_steering": {"type": "boolean"},
            "supports_interrupt": {"type": "boolean"},
        }),
        "Run": obj({
            "id": {"type": "integer"}, "slug": {"type": "string"},
            "request_id": {"type": "string"}, "display": {"type": "string"},
            "group_id": {"type": "string"}, "group_name": {"type": "string"},
            "group_number": {"type": "integer"},
            "profile_id": {"type": "string"},
            "profile_name": {"type": "string"},
            "runtime_id": {"type": "string"},
            "runtime_name": {"type": "string"},
            "title": string_or_null, "context": string_or_null,
            "status": {"enum": [
                "queued", "starting", "running", "waiting", "completed",
                "failed", "timed_out", "stopped", "skipped"]},
            "hold": nullable(ref("RunHold")), "waiting_kind": string_or_null,
            "requested_by": {"type": "string"}, "ref": string_or_null,
            "root_run_id": integer_or_null, "parent_run_id": integer_or_null,
            "retry_of": integer_or_null, "continuation_of": integer_or_null,
            "attempt": {"type": "integer"}, "queued_at": timestamp,
            "started_at": timestamp, "finished_at": timestamp,
            "summary": string_or_null, "exit_code": integer_or_null,
            "usage": ref("Usage"), "revision": {"type": "integer"},
            "cwd_source": {"enum": ["run", "group", "managed", "inherited"]},
            "branch": string_or_null, "base_commit": string_or_null,
            "head_commit": string_or_null, "checkpoint_commit": string_or_null,
            "result": nullable(ref("JsonObject")),
            "failure": nullable(ref("JsonObject")),
            "profile_snapshot": ref("ProfileSnapshot"),
            "runtime_snapshot": ref("RuntimeSnapshot"),
            "observer_usage": ref("Usage"), "combined_usage": ref("Usage"),
            "evidence_pin": nullable(ref("EvidencePin")),
            "counts": ref("RunCounts"),
        }, ("id", "request_id", "display", "group_id", "group_number",
            "profile_id", "runtime_id", "status", "cwd_source", "usage", "revision")),
        "RunPage": page("Run"),
        "RunFeed": page("Run", numeric_cursor=True),
        "RunAdmission": obj({"created": {"type": "boolean"}, "run": ref("Run")},
                            ("created", "run")),
        "Statistics": obj({
            "filters": ref("JsonObject"), "runs": {"type": "integer"},
            "agent_seconds": {"type": "integer", "minimum": 0},
            "by_status": {"type": "object", "additionalProperties": {
                "type": "integer"}}, "worker_usage": ref("Usage"),
            "observer_usage": ref("Usage"), "combined_usage": ref("Usage"),
        }, ("filters", "runs", "agent_seconds", "by_status", "worker_usage", "observer_usage",
            "combined_usage")),
        "GroupStats": obj({"runs": {"type": "integer"},
                           "active": {"type": "integer"},
                           "queued": {"type": "integer"}},
                          ("runs", "active", "queued")),
        "Group": obj({
            "id": {"type": "string"}, "slug": {"type": "string"},
            "name": {"type": "string"}, "archived": {"type": "boolean"},
            "cwd_configured": {"type": "boolean"},
            "next_number": {"type": "integer"}, "runs_count": {"type": "integer"},
            "stats": ref("GroupStats"), "revision": {"type": "integer"},
            "created_at": timestamp, "updated_at": timestamp,
        }, ("id", "slug", "name", "archived", "cwd_configured",
            "next_number", "runs_count",
            "stats", "revision")),
        "GroupPage": page("Group"), "GroupResult": result("Group", "group"),
        "Runtime": obj({
            "id": {"type": "string"}, "slug": {"type": "string"},
            "name": {"type": "string"},
            "kind": {"enum": sorted(fleet_config.RUNTIME_ADAPTERS)},
            "argv": {"type": "array", "items": {"type": "string"},
                     "description": "Non-secret launch argv. Credential-shaped "
                                    "input is rejected; projection also redacts defensively."},
            "enabled": {"type": "boolean"}, "archived": {"type": "boolean"},
            "config_configured": {"type": "boolean"},
            "supports_steering": {"type": "boolean"},
            "supports_interrupt": {"type": "boolean"},
            "revision": {"type": "integer"}, "created_at": timestamp,
            "updated_at": timestamp,
        }, ("id", "slug", "name", "kind", "argv", "enabled",
            "archived", "config_configured", "supports_steering",
            "supports_interrupt", "revision")),
        "RuntimePage": page("Runtime"),
        "RuntimeResult": result("Runtime", "runtime"),
        "Profile": obj({
            "id": {"type": "string"}, "slug": {"type": "string"},
            "name": {"type": "string"}, "runtime_id": {"type": "string"},
            "runtime_name": string_or_null, "model": string_or_null,
            "effort": string_or_null, "tier": {"enum": [1, 2, 3]},
            "priority": {"type": "integer"}, "sandbox": string_or_null,
            "timeout_seconds": integer_or_null, "active_cap": integer_or_null,
            "runway_source_id": string_or_null,
            "runway_source_name": string_or_null, "note": string_or_null,
            "env_configured": {"type": "boolean"},
            "config_configured": {"type": "boolean"},
            "observer_compatible": {"type": "boolean", "description":
                "True only when this enabled profile and runtime can provide "
                "the tool-free Observer posture."},
            "observer_incompatibility": {**string_or_null, "description":
                "Safe explanation when observer_compatible is false."},
            "enabled": {"type": "boolean"}, "archived": {"type": "boolean"},
            "stats": {"type": "object", "additionalProperties": {
                "type": "integer"}}, "revision": {"type": "integer"},
            "created_at": timestamp, "updated_at": timestamp,
        }, ("id", "slug", "name", "runtime_id", "tier", "priority",
            "env_configured", "config_configured", "observer_compatible",
            "observer_incompatibility", "enabled", "archived", "stats",
            "revision")),
        "ProfilePage": page("Profile"),
        "ProfileResult": result("Profile", "profile"),
        "DiscoveryError": {
            "type": ["string", "null"],
            "enum": [None, "not_installed", "timed_out", "unsupported",
                     "not_configured", "invalid_output", "discovery_failed"],
            "description": "Safe category; raw CLI stderr and host paths are never returned.",
        },
        "CodexDiscoveredModel": obj({
            "model": {"type": "string"},
            "efforts": {"type": "array", "items": {"type": "string"}},
            "default_effort": string_or_null,
        }, ("model", "efforts", "default_effort")),
        "ReasonixDiscoveredProvider": obj({
            "provider": {"type": "string"},
            "models": {"type": "array", "items": {"type": "string"}},
            "efforts": {"type": "array", "items": {"type": "string"}},
            "default_effort": string_or_null,
        }, ("provider", "models", "efforts", "default_effort")),
        "OpenCodeDiscovery": obj({
            "data": nullable({"type": "object", "additionalProperties": {
                "type": "array", "items": {"type": "string"}}}),
            "error": ref("DiscoveryError"),
        }, ("data", "error")),
        "CodexDiscovery": obj({
            "data": nullable(array("CodexDiscoveredModel")),
            "error": ref("DiscoveryError"),
        }, ("data", "error")),
        "ReasonixDiscovery": obj({
            "data": nullable(array("ReasonixDiscoveredProvider")),
            "error": ref("DiscoveryError"),
        }, ("data", "error")),
        "ClaudeDiscovery": obj({
            "data": {"type": "null"}, "error": ref("DiscoveryError"),
        }, ("data", "error")),
        "RuntimeDiscoveries": obj({
            "opencode": ref("OpenCodeDiscovery"),
            "codex": ref("CodexDiscovery"),
            "reasonix": ref("ReasonixDiscovery"),
            "claude": ref("ClaudeDiscovery"),
        }, ("opencode", "codex", "reasonix", "claude")),
        "LocalDiscoveredModel": obj({
            "id": {"type": "string"}, "source": {"type": "string"},
        }, ("id", "source")),
        "ProfileDiscovery": obj({
            "runtimes": ref("RuntimeDiscoveries"),
            "local_requested": {"type": "boolean"},
            "local_models": array("LocalDiscoveredModel"),
        }, ("runtimes", "local_requested", "local_models")),
        "RunwayCredits": obj({
            "text": string_or_null,
            "count": nullable({"type": "integer", "minimum": 0}),
            "expires_at": timestamp,
        }, ("text", "count", "expires_at")),
        "RunwayWindow": obj({
            "id": {"type": "string"}, "name": {"type": "string"},
            "remaining_percent": number_or_null, "resets_at": timestamp,
            "unit": {"type": "string"}, "stale": {"type": "boolean"},
            "stale_reason": string_or_null,
            "per_model": {"type": "boolean"},
        }, ("id", "name", "remaining_percent", "resets_at", "unit", "stale",
            "stale_reason", "per_model")),
        "RunwayObservation": obj({
            "observed_at": timestamp, "polled_at": timestamp,
            "fresh_until": timestamp,
            "definitive": {"type": "boolean"}, "reason": string_or_null,
            "remaining": number_or_null, "unit": string_or_null,
            "resets_at": timestamp,
            "credits": nullable(ref("RunwayCredits")),
            "burn_rate": number_or_null,
            "windows": {"type": "array", "items": ref("RunwayWindow")},
        }, ("observed_at", "polled_at", "fresh_until", "definitive",
            "remaining", "unit", "resets_at", "credits", "reason",
            "burn_rate", "windows")),
        "RunwaySource": obj({
            "id": {"type": "string"}, "slug": {"type": "string"},
            "name": {"type": "string"}, "provider": {"type": "string"},
            "account": {"type": "string"}, "lane": {"type": "string"},
            "adapter": {"enum": sorted(fleet_config.SOURCE_ADAPTERS)},
            "kind": {"enum": ["api", "plan"]},
            "enabled": {"type": "boolean"},
            "argv_configured": {"type": "boolean"},
            "config_configured": {"type": "boolean"},
            "archived": {"type": "boolean"}, "status": {"enum": [
                "unknown", "current", "stale"]}, "fresh": {"type": "boolean"},
            "observed_at": timestamp, "polled_at": timestamp,
            "fresh_until": timestamp, "definitive": {"type": "boolean"},
            "remaining": number_or_null, "unit": string_or_null,
            "resets_at": timestamp,
            "credits": nullable(ref("RunwayCredits")),
            "reason": string_or_null,
            "burn_rate": number_or_null,
            "windows": {"type": "array", "items": ref("RunwayWindow")},
            "linked_profile_ids": {"type": "array", "items": {"type": "string"}},
            "history": {"type": "array", "items": ref("RunwayObservation")},
            "revision": {"type": "integer"}, "created_at": timestamp,
            "updated_at": timestamp,
        }, ("id", "slug", "name", "provider", "account", "lane", "adapter",
            "kind",
            "argv_configured", "config_configured", "enabled", "archived",
            "status", "fresh", "observed_at", "polled_at", "fresh_until",
            "definitive", "remaining", "unit", "resets_at", "credits",
            "reason", "burn_rate", "windows",
            "linked_profile_ids", "history", "revision")),
        "RunwaySourcePage": page("RunwaySource"),
        "RunwaySourceResult": result("RunwaySource", "runway_source"),
        "Message": obj({
            "id": {"type": "integer"}, "run_id": {"type": "integer"},
            "direction": {"enum": ["inbound", "outbound", "system"]},
            "sender": {"type": "string"}, "kind": {"type": "string"},
            "status": {"enum": ["pending", "delivered", "undeliverable"]},
            "body": {"type": "string"}, "correlation_id": string_or_null,
            "reply_to": integer_or_null, "created_at": timestamp,
            "delivered_at": timestamp, "undeliverable_at": timestamp,
            "delivery_error": string_or_null,
            "display": {"type": "string"},
        }, ("id", "run_id", "direction", "sender", "kind", "status", "body",
            "correlation_id", "reply_to", "created_at", "delivered_at",
            "undeliverable_at", "delivery_error")),
        "MessagePage": page("Message"),
        "TimelineMessagePage": timeline_page("Message"),
        "MessageCounts": obj({
            key: {"type": "integer", "minimum": 0} for key in (
                "total", "pending", "delivered", "undeliverable",
                "inbound", "outbound", "system")
        }, ("total", "pending", "delivered", "undeliverable", "inbound",
            "outbound", "system")),
        "InstanceInfo": obj({
            "name": {"type": "string", "minLength": 1, "maxLength": 100},
            "platform": {"type": "string"},
        }, ("name", "platform")),
        "RunEvent": obj({
            "id": {"type": "integer"}, "seq": {"type": "integer"},
            "kind": {"type": "string"}, "name": string_or_null,
            "payload": {"type": "string"}, "truncated": {"type": "boolean"},
            "created_at": timestamp,
        }, ("id", "seq", "kind", "payload", "truncated")),
        "EventPage": page("RunEvent"),
        "TimelineEventPage": timeline_page("RunEvent"),
        "Artifact": obj({
            "id": {"type": "string"}, "run_id": {"type": "integer"},
            "name": {"type": "string"}, "relative_path": string_or_null,
            "media_type": {"type": "string"}, "byte_size": {"type": "integer"},
            "sha256": {"type": "string"}, "created_at": timestamp,
            "available": {"type": "boolean"}, "pruned_at": timestamp,
        }, ("id", "run_id", "name", "media_type", "byte_size", "sha256",
            "available")),
        "ArtifactList": obj({"items": array("Artifact")}, ("items",)),
        "ArtifactResult": result("Artifact", "artifact"),
        "GitCheckpoint": obj({"id": {"type": "string"},
                              "commit": string_or_null, "created_at": timestamp},
                             ("id",)),
        "RunChanges": obj({
            "branch": string_or_null, "base": string_or_null,
            "head": string_or_null,
            "branch_exists": {"type": "boolean"},
            "merged": {"type": "boolean"},
            "checkpoints": {"type": "array", "items": ref("GitCheckpoint")},
            "patch": string_or_null, "diff": string_or_null,
            "truncated": {"type": "boolean"},
        }, ("branch", "base", "head", "checkpoints", "patch", "diff",
            "truncated")),
        "RunLineage": obj({
            "root_run_id": integer_or_null, "items": array("Run"),
            "ancestors": array("Run"), "descendants": array("Run"),
            "children": array("Run"),
        }, ("root_run_id", "items", "ancestors", "descendants", "children")),
        "Attention": obj({
            "id": {"type": "string"}, "correlation_id": {"type": "string"},
            "run_id": integer_or_null, "kind": {"enum": [
                "question", "decision", "alert", "profile_proposal"]},
            "state": {"enum": ["open", "resolved", "cancelled"]},
            "prompt": {"type": "string"}, "detail": {"type": "string"},
            "blocking": {"type": "boolean"},
            "choices": {"type": "array", "items": {}},
            "fallback": {}, "proposal": {}, "deadline": timestamp,
            "opened_at": timestamp, "created_by": {"type": "string"},
            "resolved_at": timestamp, "resolution": {},
            "resolved_by": string_or_null, "revision": integer_or_null,
            "responses": {"type": "array", "items": ref("JsonObject")},
        }, ("id", "correlation_id", "kind", "state", "prompt", "detail",
            "blocking", "choices", "opened_at")),
        "AttentionPage": page("Attention"),
        "AttentionFeed": page("Attention", numeric_cursor=True),
        "AttentionResult": obj({"attention": ref("Attention"),
                                "response_id": {"type": "integer"}},
                               ("attention", "response_id")),
        "AttentionOpenResult": obj({"created": {"type": "boolean"},
                                    "attention": ref("Attention")},
                                   ("created", "attention")),
        "ObserverSettings": obj({
            "enabled": {"type": "boolean"}, "profile_id": string_or_null,
            "concurrency": {"type": "integer", "minimum": 1, "maximum": 8},
            "first_check_seconds": {"type": "integer"},
            "minimum_events": {"type": "integer"},
            "subsequent_check_seconds": {"type": "integer"},
            "authority": {"enum": ["advisory", "tell_only", "correct_then_stop"]},
            "revision": {"type": "integer"}, "updated_by": {"type": "string"},
            "updated_at": timestamp,
        }, ("enabled", "profile_id", "concurrency", "first_check_seconds",
            "minimum_events", "subsequent_check_seconds", "authority", "revision")),
        "ObserverCheck": obj({
            "id": {"type": "integer"}, "run_id": {"type": "integer"},
            "profile_id": string_or_null, "trigger": string_or_null,
            "judgment": string_or_null, "action": {"type": "string"},
            "rationale": string_or_null, "evidence_from": integer_or_null,
            "evidence_to": integer_or_null, "created_at": timestamp,
            "finished_at": timestamp, "log_available": {"type": "boolean"},
            "log_pruned_at": timestamp, "usage": ref("Usage"),
            "detail": ref("JsonObject"),
        }, ("id", "run_id", "action", "log_available", "log_pruned_at",
            "usage", "detail")),
        "ObserverRunDetail": obj({"checks": array("ObserverCheck"),
                                  "usage": ref("Usage")}, ("checks", "usage")),
        "ObserverSettingsResult": result("ObserverSettings", "observer"),
        "Setting": obj({"key": {"type": "string"}, "value": {},
                        "revision": {"type": "integer"},
                        "updated_by": {"type": "string"},
                        "updated_at": timestamp},
                       ("key", "value", "revision", "updated_by", "updated_at")),
        "SettingList": obj({"items": array("Setting")}, ("items",)),
        "SettingResult": result("Setting", "setting"),
        "SchedulerState": obj({
            "paused": {"type": "boolean"}, "max_active_runs": integer_or_null,
            "active_runs": {"type": "integer"},
            "active_by_profile": {"type": "object", "additionalProperties": {
                "type": "integer"}}, "queued_count": {"type": "integer"},
            "queued_runs": {"type": "array", "items": ref("JsonObject")},
        }, ("paused", "active_runs", "active_by_profile", "queued_count",
            "queued_runs")),
        "SchedulerResult": result("SchedulerState", "scheduler"),
        "DaemonState": obj({
            "status": {"const": "healthy"}, "healthy": {"const": True},
            "last_tick_at": timestamp,
        }, ("status", "healthy", "last_tick_at")),
        "SnapshotScheduler": obj({
            "paused": {"type": "boolean"}, "active": {"type": "integer"},
            "queued": {"type": "integer"}, "max_active": {"type": "integer"},
        }, ("paused", "active", "queued", "max_active")),
        "SnapshotCounts": obj({
            key: {"type": "integer", "minimum": 0} for key in (
                "groups", "runtimes", "profiles", "runway_sources",
                "runs_total", "runs_active", "runs_queued")
        }, ("groups", "runtimes", "profiles", "runway_sources",
            "runs_total", "runs_active", "runs_queued")),
        "InboxCounts": obj({
            "open": {"type": "integer", "minimum": 0},
            "blocking": {"type": "integer", "minimum": 0},
        }, ("open", "blocking")),
        "FleetSnapshot": obj({
            "generated_at": timestamp, "instance": ref("InstanceInfo"),
            "daemon": ref("DaemonState"), "revision": {"type": "integer"},
            "scheduler": ref("SnapshotScheduler"),
            "counts": ref("SnapshotCounts"),
            "run_statuses": {"type": "object", "additionalProperties": {
                "type": "integer"}}, "inbox": ref("InboxCounts"),
            "messages": ref("MessageCounts"),
            "observer": ref("ObserverSettings"), "storage": ref("StorageReport"),
        }, ("generated_at", "instance", "daemon", "revision", "scheduler",
            "counts", "run_statuses", "inbox", "messages", "observer",
            "storage")),
        "ControlReceipt": obj({
            "audit_id": {"type": "integer"}, "action": {"type": "string"},
            "outcome": {"type": "string"}, "result": ref("JsonObject"),
        }, ("audit_id", "action", "outcome", "result")),
        "ControlResult": result("ControlReceipt", "control"),
        "ChildRequestRecord": obj({
            "id": {"type": "integer"}, "request_id": {"type": "string"},
            "parent_run_id": {"type": "integer"},
            "requested_by": {"type": "string"},
            "profiles": {"type": "array", "items": {"type": "string"}},
            "context": {"type": "string"}, "title": string_or_null,
            "status": {"type": "string"},
            "child_run_ids": {"type": "array", "items": {"type": "integer"}},
            "error": string_or_null, "created_at": timestamp,
            "processed_at": timestamp,
        }, ("id", "request_id", "parent_run_id", "requested_by", "profiles",
            "context", "status", "child_run_ids")),
        "ChildAdmission": obj({"created": {"type": "boolean"},
                               "child_request": ref("ChildRequestRecord")},
                              ("created", "child_request")),
        "ProfileChangeResult": obj({
            "applied": {"type": "boolean"}, "created": {"type": "boolean"},
            "profile": ref("Profile"), "attention": ref("Attention"),
        }, ("applied",)),
        "Device": obj({
            "id": {"type": "string"}, "label": {"type": "string"},
            "created_at": timestamp, "last_used_at": timestamp,
            "revoked_at": timestamp,
        }, ("id", "label")),
        "DeviceList": obj({"items": array("Device")}, ("items",)),
        "ServiceToken": obj({
            "id": {"type": "string"}, "label": {"type": "string"},
            "authorities": {"type": "array", "items": {"enum": [
                "read", "dispatch", "control", "answer"]}},
            "created_at": timestamp, "last_used_at": timestamp,
            "revoked_at": timestamp,
        }, ("id", "label", "authorities")),
        "ServiceTokenList": obj({"items": array("ServiceToken")}, ("items",)),
        "ServiceTokenSecretResult": obj({"service_token": ref("ServiceToken"),
                                         "token": {"type": "string"}},
                                        ("service_token", "token")),
        "PairingCode": obj({
            "pairing_id": {"type": "string"},
            "code": {"type": "string",
                     "pattern": "^[0-9A-HJKMNP-TV-Z]{4}(?:-[0-9A-HJKMNP-TV-Z]{4}){2}$",
                     "description": "60-bit Crockford-style one-time code, grouped for entry."},
            "expires_at": timestamp, "pairing_uri": {"type": "string"},
        }, ("pairing_id", "code", "expires_at", "pairing_uri")),
        "PairingRedemption": obj({
            "device": ref("Device"),
            "token": {"type": "string",
                      "description": "One-shot native/CLI bearer; omitted when browser=true."},
        }, ("device",)),
        "StorageReport": obj({
            "database_bytes": {"type": "integer"}, "log_bytes": {"type": "integer"},
            "run_log_bytes": {"type": "integer"},
            "observer_log_bytes": {"type": "integer"},
            "artifact_bytes": {"type": "integer"},
            "checkpoint_bytes": {"type": "integer"},
            "worktree_bytes": {"type": "integer"}, "runs": {"type": "integer"},
            "pinned_runs": {"type": "integer"}, "retention": {"type": "string"},
        }, ("database_bytes", "log_bytes", "run_log_bytes",
            "observer_log_bytes", "artifact_bytes", "checkpoint_bytes",
            "worktree_bytes", "runs", "pinned_runs", "retention")),
        "PrunePlanItem": obj({
            "kind": {"type": "string"}, "run_id": {"type": "integer"},
            "check_id": {"type": "integer"},
            "artifact_id": {"type": "string"}, "size_bytes": {"type": "integer"},
            "sha256": {"type": "string"},
        }, ("kind", "run_id", "size_bytes")),
        "PruneResultItem": obj({
            "kind": {"type": "string"}, "run_id": {"type": "integer"},
            "check_id": {"type": "integer"},
            "artifact_id": {"type": "string"}, "status": {"type": "string"},
            "reason": string_or_null, "bytes": {"type": "integer"},
        }, ("kind", "run_id", "status")),
        "PruneResultSummary": obj({
            "items": array("PruneResultItem"),
            "pruned_items": {"type": "integer", "minimum": 0},
            "pruned_bytes": {"type": "integer", "minimum": 0},
            "skipped_items": {"type": "integer", "minimum": 0},
        }, ("items", "pruned_items", "pruned_bytes", "skipped_items")),
        "PrunePlan": obj({
            "id": {"type": "string"}, "criteria": ref("JsonObject"),
            "items": array("PrunePlanItem"), "item_count": {"type": "integer"},
            "bytes": {"type": "integer"}, "created_by": string_or_null,
            "created_at": timestamp, "applied_at": timestamp,
            "result": nullable(ref("PruneResultSummary")),
        }, ("id", "criteria", "items", "item_count", "bytes")),
        "PrunePlanResult": result("PrunePlan", "plan"),
        "ArtifactPublishRequest": mutation_request({
            "path": {"type": "string", "minLength": 1}, "name": string_or_null,
        }, ("path",)),
        "ChildRequest": mutation_request({
            "profile": {"type": "string"},
            "context": {"type": "string", "minLength": 1},
            "title": string_or_null,
        }, ("profile", "context")),
        "AttentionOpenRequest": mutation_request({
            "kind": {"enum": ["question", "decision", "alert", "profile_proposal"]},
            "title": {"type": "string"}, "body": {"type": "string"},
            "blocking": {"type": "boolean"}, "choices": {"type": "array"},
            "fallback": {}, "proposal": {}, "correlation_id": {"type": "string"},
            "deadline": timestamp,
        }, ("body",)),
        "ProfileChangeRequest": mutation_request({
            "changes": {"type": "object", "minProperties": 1}}, ("changes",)),
        "TextControlRequest": mutation_request({
            "text": {"type": "string", "minLength": 1}}, ("text",)),
        "RetryRequest": mutation_request({"context": string_or_null,
                                           "profile": string_or_null}),
        "ContinueRequest": mutation_request({
            "context": {"type": "string", "minLength": 1},
            "profile": string_or_null}, ("context",)),
        "PinRequest": mutation_request({"reason": string_or_null}),
        "GroupCreateRequest": mutation_request({
            "name": {"type": "string"},
            "cwd": {"type": "string", "minLength": 1, "writeOnly": True,
                    "description": "Optional canonical daemon-host default; never returned."}},
                                                ("name",)),
        "GroupUpdateRequest": mutation_request({"name": {"type": "string"},
                                                 "archived": {"type": "boolean"},
                                                 "cwd": {"type": ["string", "null"],
                                                         "minLength": 1,
                                                         "writeOnly": True,
                                                         "description": "Standalone replacement; null clears and omission preserves."},
                                                 "expected_revision": integer_or_null}),
        "RuntimeCreateRequest": {"allOf": [
            ref("Mutation"),
            obj({
                "name": {"type": "string"},
                "kind": {"enum": sorted(fleet_config.RUNTIME_ADAPTERS)},
                "argv": {"type": "array", "items": {"type": "string",
                                                        "minLength": 1}},
                "config": {"type": "object", "writeOnly": True,
                           "description": "Write-only non-secret replacement configuration."},
                "enabled": {"type": "boolean"},
            }, ("name", "kind")),
            {"if": {"properties": {"kind": {"enum": ["exec", "acp"]}},
                    "required": ["kind"]},
             "then": {"required": ["argv"], "properties": {
                 "argv": {"minItems": 1}}},
             "else": {"properties": {"argv": {"maxItems": 0}}}},
        ]},
        "RuntimeUpdateRequest": mutation_request({
            "name": {"type": "string"},
            "kind": {"enum": sorted(fleet_config.RUNTIME_ADAPTERS)},
            "argv": {"type": "array", "items": {"type": "string",
                                                    "minLength": 1},
                     "description": "Required and non-empty for exec/acp; omitted or empty for built-ins."},
            "config": {"type": "object", "writeOnly": True,
                       "description": "Write-only replacement; omission preserves and {} clears."},
            "enabled": {"type": "boolean"}, "archived": {"type": "boolean"},
            "expected_revision": integer_or_null,
        }),
        "ProfileCreateRequest": mutation_request({
            "name": {"type": "string"}, "runtime_id": {"type": "string"},
            "model": string_or_null, "effort": string_or_null,
            "tier": {"enum": [1, 2, 3]}, "priority": {"type": "integer"},
            "sandbox": string_or_null, "timeout_seconds": integer_or_null,
            "active_cap": integer_or_null, "runway_source_id": string_or_null,
            "env": {"type": "object", "writeOnly": True,
                    "additionalProperties": {"type": "string"},
                    "description": "Write-only non-secret environment replacement."},
            "config": {"type": "object", "writeOnly": True,
                       "description": "Write-only non-secret replacement configuration."},
            "note": string_or_null, "enabled": {"type": "boolean"},
        }, ("name", "runtime_id", "tier")),
        "ProfileUpdateRequest": mutation_request({
            "name": {"type": "string"}, "runtime_id": {"type": "string"},
            "model": string_or_null, "effort": string_or_null,
            "tier": {"enum": [1, 2, 3]}, "priority": {"type": "integer"},
            "sandbox": string_or_null, "timeout_seconds": integer_or_null,
            "active_cap": integer_or_null, "runway_source_id": string_or_null,
            "env": {"type": "object", "writeOnly": True,
                    "additionalProperties": {"type": "string"},
                    "description": "Write-only replacement; omission preserves and {} clears."},
            "config": {"type": "object", "writeOnly": True,
                       "description": "Write-only replacement; omission preserves and {} clears."},
            "note": string_or_null, "enabled": {"type": "boolean"},
            "archived": {"type": "boolean"},
            "expected_revision": integer_or_null,
        }),
        "RunwaySourceCreateRequest": {"allOf": [
            ref("Mutation"),
            obj({
                "name": {"type": "string"}, "provider": {"type": "string"},
                "account": {"type": "string"}, "lane": {"type": "string"},
                "adapter": {"enum": sorted(fleet_config.SOURCE_ADAPTERS)},
                "argv": {"type": "array", "items": {"type": "string",
                                                        "minLength": 1},
                         "writeOnly": True,
                         "description": "Write-only replacement; never returned."},
                "config": {"type": "object", "writeOnly": True,
                           "description": "Write-only non-secret configuration; never returned."},
                "enabled": {"type": "boolean"},
            }, ("name", "provider", "adapter")),
            {"if": {"properties": {"adapter": {"const": "command"}},
                    "required": ["adapter"]},
             "then": {"required": ["argv"], "properties": {
                 "argv": {"minItems": 1}}},
             "else": {"properties": {"argv": {"maxItems": 0}}}},
        ]},
        "RunwaySourceUpdateRequest": mutation_request({
            "name": {"type": "string"}, "provider": {"type": "string"},
            "account": {"type": "string"},
            "lane": {"type": "string"},
            "adapter": {"enum": sorted(fleet_config.SOURCE_ADAPTERS)},
            "argv": {"type": "array", "items": {"type": "string",
                                                    "minLength": 1},
                     "writeOnly": True,
                     "description": "Write-only replacement; omission preserves and [] clears."},
            "config": {"type": "object", "writeOnly": True,
                       "description": "Write-only replacement; omission preserves and {} clears."},
            "enabled": {"type": "boolean"}, "archived": {"type": "boolean"},
            "expected_revision": integer_or_null,
        }),
        "SettingUpdateRequest": mutation_request({
            "key": {"enum": [
                "instance_name", "max_active_runs", "paused",
                "delegation_max_depth", "delegation_max_children",
                "delegation_max_active_children"],
                    "description": "Only the fixed managed fleet settings are writable."},
            "value": {"description": "instance_name: non-empty string (max 100); "
                      "paused: boolean; max_active_runs: 1..256; "
                      "delegation_max_depth: 0..10; child limits: 1..100."},
            "expected_revision": integer_or_null,
        }, ("key", "value")),
        "ObserverUpdateRequest": mutation_request({
            "enabled": {"type": "boolean"}, "profile_id": string_or_null,
            "concurrency": {"type": "integer", "minimum": 1, "maximum": 8},
            "first_check_seconds": {"type": "integer"},
            "minimum_events": {"type": "integer"},
            "subsequent_check_seconds": {"type": "integer"},
            "authority": {"enum": ["advisory", "tell_only", "correct_then_stop"]},
            "expected_revision": integer_or_null,
        }, description="profile_id must select an enabled profile using a "
                       "claude, opencode, or reasonix runtime; those adapters "
                       "provide Orchestra's tool-free Observer posture."),
        "PairingCreateRequest": mutation_request({"label": {"type": "string"}},
                                                 ("label",)),
        "RevokeRequest": mutation_request({"revoked": {"const": True}},
                                           ("revoked",)),
        "ServiceTokenCreateRequest": mutation_request({
            "name": {"type": "string"}, "authorities": {"type": "array",
                "minItems": 1, "uniqueItems": True, "items": {"enum": [
                    "read", "dispatch", "control", "answer"]}},
        }, ("name", "authorities")),
        "PrunePlanRequest": mutation_request({
            "older_than_days": {"type": "integer", "minimum": 0},
            "kinds": {"type": "array", "items": {"enum": [
                "raw_logs", "artifacts"]}},
        }),
    })
    parameters_ = {
        "Limit": {"name": "limit", "in": "query", "required": False,
                  "schema": {"type": "integer", "minimum": 1, "maximum": 500,
                             "default": 100}},
        "Cursor": {"name": "cursor", "in": "query", "required": False,
                   "description": "Opaque cursor returned by the previous page.",
                   "schema": {"type": "string"}},
        "After": {"name": "after", "in": "query", "required": False,
                  "description": "Last durably processed revision or event id.",
                  "schema": {"type": "integer", "minimum": 0, "default": 0}},
        "Group": {"name": "group", "in": "query", "required": False,
                  "schema": {"type": "string"}},
        "Profile": {"name": "profile", "in": "query", "required": False,
                    "schema": {"type": "string"}},
        "Status": {"name": "status", "in": "query", "required": False,
                   "schema": {"enum": list(db.RUN_ACTIVE + db.RUN_TERMINAL)}},
        "Search": {"name": "q", "in": "query", "required": False,
                   "schema": {"type": "string"}},
        "IncludeArchived": {"name": "include_archived", "in": "query",
                            "required": False, "schema": {"type": "boolean",
                                                           "default": False}},
        "LocalDiscovery": {"name": "local", "in": "query", "required": False,
                           "description": "Also probe inference servers on the daemon host.",
                           "schema": {"type": "boolean", "default": False}},
        "State": {"name": "state", "in": "query", "required": False,
                  "schema": {"enum": ["open", "resolved", "cancelled"],
                             "default": "open"}},
        "Kind": {"name": "kind", "in": "query", "required": False,
                 "schema": {"enum": [
                     "question", "decision", "alert", "profile_proposal"]}},
        "Direction": {"name": "direction", "in": "query", "required": False,
                      "schema": {"enum": ["inbound", "outbound", "system"]}},
        "TimelineDirection": {
            "name": "direction", "in": "query", "required": False,
            "description": "older (default) loads history; newer durably catches up.",
            "schema": {"enum": ["older", "newer"], "default": "older"}},
        "MessageStatus": {"name": "status", "in": "query", "required": False,
                          "schema": {"enum": [
                              "pending", "delivered", "undeliverable"]}},
        "MessageKind": {"name": "kind", "in": "query", "required": False,
                        "schema": {"type": "string", "minLength": 1}},
        "RunID": {"name": "run_id", "in": "query", "required": False,
                  "schema": {"type": "integer", "minimum": 1}},
        "Range": {"name": "Range", "in": "header", "required": False,
                  "description": "A single RFC 9110 byte range.",
                  "schema": {"type": "string", "pattern": "^bytes="}},
    }
    return {
        "openapi": "3.1.0", "info": {"title": "Orchestra", "version": "2"},
        "servers": [{"url": "/"}], "paths": paths_,
        "components": {"securitySchemes": {
            "bearer": {"type": "http", "scheme": "bearer",
                       "bearerFormat": "Orchestra device, service, or run token"},
            "cookie": {"type": "apiKey", "in": "cookie",
                       "name": "orchestra_device",
                       "description": "Same-origin paired browser device."},
        }, "parameters": parameters_, "schemas": schemas},
        "security": [{"bearer": []}, {"cookie": []}],
    }
