"""Normalized traces: one events table, four backend parsers, SSE, retention.

DESIGN §7. The supervisor already tails each backend's JSONL, so ingest maps
every line into ONE shape — assistant text, reasoning, tool call, tool
result, permission request, human injection, lifecycle — and stores a ~2KB
truncated payload plus the byte offset of the line it came from.

**The raw file stays the source of truth.** These JSONL formats are
undocumented and drift, so a parser is best-effort by contract: an unknown
or malformed line is counted and skipped, never raised, and ``expand()``
always goes back to the file for the untruncated value.

Retention: normalized events are kept indefinitely; raw logs age out after
``raw_log_retention_days`` for TERMINAL runs only.

HTTP seam (DESIGN §3, item W-0100): ``http.py`` calls
``stream_run_trace()``, ``stream_daemon_log()``, ``events_for_run()`` and
``run_messages()`` from here. Nothing in this module imports http.
"""
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dromond import db, paths, runners

KINDS = ("assistant_text", "reasoning", "tool_call", "tool_result",
         "permission_request", "human_injection", "lifecycle")

MAX_PAYLOAD = 2048          # ~2KB truncated payload (DESIGN §7)
MAX_CHUNK = 4_000_000       # bytes read per ingest pass
DEFAULT_RAW_RETENTION_DAYS = 30
SSE_RETRY_MS = 3000
DAEMON_LOGS = ("daemon.out.log", "daemon.err.log")


# --- shared helpers ---------------------------------------------------------

def _ev(kind: str, name, payload, ts=None, merge: bool = False) -> dict:
    """One normalized event. ``merge`` marks a STREAMED FRAGMENT: a backend
    that emits an answer token-by-token would otherwise get one row per
    fragment, so ingest appends these into the row they continue."""
    if not isinstance(payload, str):
        payload = _json(payload)
    return {"kind": kind, "name": (str(name) if name else None),
            "payload": payload, "ts": ts, "merge": merge}


def _json(obj) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(obj)


def _text_of(content) -> str:
    """Flatten a content value (string, block list, or anything else)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") if isinstance(b, dict) else str(b) for b in content]
        joined = "".join(p for p in parts if p)
        return joined or _json(content)
    if content is None:
        return ""
    return _json(content)


_TS_KEYS = ("timestamp", "ts", "created_at", "time")


def _ts(obj) -> str | None:
    for key in _TS_KEYS:
        value = obj.get(key)
        if isinstance(value, str) and value:
            return value
    return None


# --- backend parsers --------------------------------------------------------
# Each returns a list of normalized events, [] for "recognized, nothing to
# record", or None for "unrecognized" (which the caller counts as skipped).

def _claude_blocks(blocks) -> list[dict]:
    out = []
    for block in blocks if isinstance(blocks, list) else []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            out.append(_ev("assistant_text", None, block.get("text") or ""))
        elif kind in ("thinking", "redacted_thinking"):
            out.append(_ev("reasoning", None,
                           block.get("thinking") or block.get("data") or ""))
        elif kind == "tool_use":
            out.append(_ev("tool_call", block.get("name"), block.get("input")))
        elif kind == "tool_result":
            out.append(_ev("tool_result", block.get("tool_use_id"),
                           _text_of(block.get("content"))))
    return out


# Claude Code's `system` lines are mostly telemetry. One real run carried 892
# `thinking_tokens` counters and 186 `status` pings against 94 actual thinking
# blocks — recorded as lifecycle they bury the trace they are supposed to
# annotate. Only the subtypes a human would read become events; the rest are
# recognized and dropped, so the skipped counter still sees them.
_CLAUDE_NOISE = {"thinking_tokens", "status"}


def _claude(obj) -> list[dict] | None:
    kind = obj.get("type")
    if kind in ("system", "result"):
        subtype = obj.get("subtype") or kind
        if subtype in _CLAUDE_NOISE:
            return []
        return [_ev("lifecycle", subtype, obj, _ts(obj))]
    if kind == "control_request":
        request = obj.get("request") or {}
        if request.get("subtype") in ("can_use_tool", "permission_request"):
            return [_ev("permission_request",
                        request.get("tool_name") or request.get("subtype"), request)]
        return []
    if kind in ("assistant", "user"):
        message = obj.get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            # `-p` puts the brief here, and a resume puts the delivered
            # message here; both are text put INTO the session by a human.
            return [_ev("human_injection", message.get("role") or kind, content)]
        return _claude_blocks(content)
    if kind in ("stream_event", "control_response", "control_cancel_request"):
        return []  # partials duplicate the assembled message
    return None


_CODEX_TOOL_ITEMS = {"command_execution", "file_change", "patch",
                     "mcp_tool_call", "web_search", "todo_list"}


def _codex(obj) -> list[dict] | None:
    kind = obj.get("type")
    if kind in ("thread.started", "turn.started", "turn.completed",
                "turn.failed", "error", "session.created"):
        return [_ev("lifecycle", kind, obj, _ts(obj))]
    if kind in ("item.started", "item.updated", "item.completed"):
        item = obj.get("item") or {}
        item_type = item.get("type")
        if item.get("status") in ("awaiting_approval", "pending_approval"):
            return [_ev("permission_request", item_type, item, _ts(obj))]
        if item_type == "agent_message":
            return [_ev("assistant_text", None, item.get("text") or "", _ts(obj))] \
                if kind == "item.completed" else []
        if item_type == "reasoning":
            return [_ev("reasoning", None,
                        item.get("text") or _text_of(item.get("summary")), _ts(obj))] \
                if kind == "item.completed" else []
        if item_type in _CODEX_TOOL_ITEMS:
            if kind == "item.started":
                return [_ev("tool_call", item_type, item, _ts(obj))]
            if kind == "item.completed":
                return [_ev("tool_result", item_type, item, _ts(obj))]
            return []
        return []
    return None


def _opencode(obj) -> list[dict] | None:
    kind = obj.get("type") or ""
    if kind.startswith("permission"):
        return [_ev("permission_request",
                    (obj.get("permission") or {}).get("type") or kind,
                    obj.get("permission") or obj, _ts(obj))]
    part = obj.get("part") or {}
    part_type = part.get("type")
    if part_type == "text":
        return [_ev("assistant_text", None, part.get("text") or "", _ts(part))]
    if part_type == "reasoning":
        return [_ev("reasoning", None,
                    part.get("text") or part.get("reasoning") or "", _ts(part))]
    if part_type == "tool":
        state = part.get("state") or {}
        status = state.get("status")
        name = part.get("tool") or state.get("title")
        if status in ("completed", "error"):
            return [_ev("tool_result", name,
                        _text_of(state.get("output") or state.get("error")), _ts(part))]
        if status in ("pending", "running"):
            return [_ev("tool_call", name, state.get("input"), _ts(part))]
        return []
    if part_type in ("step-start", "step-finish") or kind in (
            "step_finish", "session.idle", "session.updated", "session.error",
            "message.updated"):
        return [_ev("lifecycle", kind or part_type, obj, _ts(obj))]
    if kind == "text" and isinstance(obj.get("text"), str):
        return [_ev("assistant_text", None, obj["text"])]  # bare shape, older builds
    if kind.startswith(("message.", "session.", "storage.", "file.", "server.")):
        return []
    return None


# Reasonix discriminates on `kind`, not `type` — except for its FINAL line,
# which is Claude-shaped (`{"type": "result", ...}`) and carries session_id,
# total_cost_usd and token usage. Both shapes have to parse.
_REASONIX_FRAGMENTS = {"text": "assistant_text", "reasoning": "reasoning"}
_REASONIX_LIFECYCLE = ("turn_started", "stream_attempt", "usage")


def _reasonix(obj) -> list[dict] | None:
    kind = obj.get("kind")
    if kind is None:
        # The run's last line only. Claude's parser already reads it, and
        # the whole object becomes the payload so W-0142 (statistics) can
        # read total_cost_usd and usage back out.
        return _claude(obj) if obj.get("type") else None
    mapped = _REASONIX_FRAGMENTS.get(kind)
    if mapped:
        # Streamed token-by-token: hundreds of these per turn. Ingest merges
        # adjacent fragments into one row (see _extend).
        text = obj.get("text")
        return [_ev(mapped, None, text, merge=True)] if isinstance(text, str) else []
    if kind == "message":
        # A COMPLETE reasoning block, not a fragment of the stream above, so
        # it is named and never merged into it.
        reasoning = obj.get("reasoning")
        return [_ev("reasoning", "message", reasoning)] \
            if isinstance(reasoning, str) else []
    tool = obj.get("tool") if isinstance(obj.get("tool"), dict) else {}
    if kind == "tool_dispatch":
        return [_ev("tool_call", tool.get("name"), tool)]
    if kind == "tool_result":
        # The whole tool object: `err` has to stay visible, and `output` is
        # truncated with a byte offset like any other oversized payload.
        return [_ev("tool_result", tool.get("name"), tool)]
    if kind == "tool_progress":
        # Interim output of a still-running tool. It earns a lifecycle row
        # rather than a tool_result, so call/result stay paired one to one
        # and nothing is dropped silently.
        return [_ev("lifecycle", "tool_progress", tool or obj)]
    if kind in _REASONIX_LIFECYCLE:
        return [_ev("lifecycle", kind, obj, _ts(obj))]
    return None


# --- ACP transport (W-0104, DESIGN §6) --------------------------------------
# The second transport feeds THIS table, not a second one: ``acp.py`` writes
# every JSON-RPC frame into the same raw log (tagged ``_dir`` / ``_ts`` /
# ``_method``), so byte offsets, expand-in-place, SSE and retention are the
# machinery already here. Seven kinds, no eighth: a `plan` update is the
# agent reasoning about what it will do, which is what `reasoning` means.

_ACP_UPDATE_KINDS = {
    "agent_message_chunk": "assistant_text",
    "agent_thought_chunk": "reasoning",
    "user_message_chunk": "human_injection",
}
# Methods Dromond sends INTO a session: the brief, a delivered message, and
# Reasonix's mid-turn steer. Text a human put into the run, exactly like the
# string content Claude's `-p` produces.
_ACP_INJECTION_METHODS = ("session/prompt", "_reasonix.io/session/steer")


def _acp_content(value) -> str:
    """An ACP content block is a single object, not Claude's list."""
    if isinstance(value, dict):
        return value.get("text") or value.get("uri") or _json(value)
    return _text_of(value)


def _acp(obj) -> list[dict] | None:
    ts = obj.get("_ts")
    method, params = obj.get("method"), obj.get("params") or {}
    if method == "session/update":
        update = params.get("update") or {}
        kind = update.get("sessionUpdate")
        if kind == "plan":
            return [_ev("reasoning", "plan", update.get("entries"), ts)]
        mapped = _ACP_UPDATE_KINDS.get(kind)
        if mapped:
            # ACP streams these token-by-token, exactly as Reasonix's exec
            # mode does, so they merge into one row instead of hundreds.
            # A human injection is a whole message, never a fragment.
            return [_ev(mapped, None, _acp_content(update.get("content")), ts,
                        merge=(mapped != "human_injection"))]
        if kind in ("tool_call", "tool_call_update"):
            name = update.get("title") or update.get("kind") or update.get("toolCallId")
            if update.get("status") in ("completed", "failed"):
                return [_ev("tool_result", name,
                            _text_of(update.get("content")) or _json(update), ts)]
            raw = update.get("rawInput")
            return [_ev("tool_call", name, raw if raw is not None else update, ts)]
        return [_ev("lifecycle", kind or "session/update", update or params, ts)]
    if method == "session/request_permission":
        tool = params.get("toolCall") or {}
        return [_ev("permission_request",
                    tool.get("title") or tool.get("kind") or method, params, ts)]
    if method in _ACP_INJECTION_METHODS and obj.get("_dir") == "out":
        return [_ev("human_injection", "dromond", _text_of(params.get("prompt")), ts)]
    label = method or obj.get("_method") or "acp"
    if "error" in obj:
        return [_ev("lifecycle", f"{label} error", obj.get("error"), ts)]
    if "result" in obj:
        result = obj.get("result")
        if isinstance(result, dict) and result.get("stopReason"):
            return [_ev("lifecycle", f"stop:{result['stopReason']}", result, ts)]
        return [_ev("lifecycle", label, result, ts)]
    return [_ev("lifecycle", label, params or obj, ts)]


PARSERS = {"claude": _claude, "codex": _codex,
           "opencode": _opencode, "reasonix": _reasonix}


def parse_line(backend: str, line: str) -> list[dict] | None:
    """Map one raw JSONL line to normalized events.

    Returns None when the line is malformed or unrecognized — the caller
    counts it. This function never raises for bad input.
    """
    line = line.strip()
    if not line or not line.startswith("{"):
        return None
    try:
        obj = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    # W-0104: an ACP frame is self-describing, so the transport is read off
    # the line rather than looked up per run. No backend's own stream-json
    # carries a `jsonrpc` key.
    parser = _acp if obj.get("jsonrpc") == "2.0" else PARSERS.get(backend)
    try:
        events = parser(obj) if parser else None
    except Exception:  # a drifted format must never take the supervisor down
        events = None
    if events is None and runners._dig(obj, runners.SESSION_KEYS):
        # Every backend announces its session id somehow; that is lifecycle
        # whatever else the line turned out to be.
        return [_ev("lifecycle", "session", obj, _ts(obj))]
    return events


# --- ingest -----------------------------------------------------------------

def cursor(con, run_id: int):
    return con.execute("SELECT * FROM trace_cursors WHERE run_id=?",
                       (run_id,)).fetchone()


def _save_cursor(con, run_id: int, offset: int, seq: int, skipped: int) -> None:
    con.execute(
        "INSERT INTO trace_cursors(run_id, byte_offset, seq, skipped, updated_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET "
        "byte_offset=excluded.byte_offset, seq=excluded.seq, "
        "skipped=excluded.skipped, updated_at=excluded.updated_at",
        (run_id, offset, seq, skipped, db.now()))


def _insert(con, run_id: int, seq: int, event: dict,
            offset: int, length: int) -> dict | None:
    """Write one event row. Returns it as the new "last row" for merging."""
    payload = event["payload"] or ""
    cur = con.execute(
        "INSERT OR IGNORE INTO events(run_id, seq, kind, name, payload, "
        "payload_len, truncated, byte_offset, byte_length, ts, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, seq, event["kind"], event["name"], payload[:MAX_PAYLOAD],
         len(payload), int(len(payload) > MAX_PAYLOAD), offset, length,
         event.get("ts"), db.now()))
    if cur.rowcount != 1:
        return None
    return {"id": int(cur.lastrowid), "kind": event["kind"], "name": event["name"],
            "byte_offset": offset, "byte_length": length,
            "payload_len": len(payload)}


def _last_event(con, run_id: int) -> dict | None:
    row = con.execute(
        "SELECT id, kind, name, byte_offset, byte_length, payload_len FROM events "
        "WHERE run_id=? ORDER BY seq DESC LIMIT 1", (run_id,)).fetchone()
    return dict(row) if row else None


def _extend(con, last: dict | None, event: dict, offset: int, length: int) -> bool:
    """Append a streamed fragment to the row it continues, or decline.

    Reasonix streams assistant text and reasoning token by token — 1,028
    `text` lines for two runs — and one row per fragment would make the
    trace unreadable. A fragment merges ONLY into the row built from the
    immediately preceding line with the same kind and name, so anything
    interleaved (a tool call, a complete `message` block) ends the run of
    fragments and the next one starts a fresh row. Holding the state in the
    row itself is what makes this survive the supervisor's 0.5s ingest
    passes without a buffer.
    """
    if not event.get("merge") or not last or last["kind"] != event["kind"] \
            or last["name"] != event["name"] or last["byte_offset"] < 0:
        return False
    end = last["byte_offset"] + last["byte_length"]
    if not end < offset <= end + 2:  # +2 tolerates a CRLF line ending
        return False
    payload = event["payload"] or ""
    total = last["payload_len"] + len(payload)
    byte_length = offset + length - last["byte_offset"]
    con.execute(
        "UPDATE events SET payload=substr(payload || ?, 1, ?), payload_len=?, "
        "truncated=?, byte_length=? WHERE id=?",
        (payload, MAX_PAYLOAD, total, int(total > MAX_PAYLOAD), byte_length,
         last["id"]))
    last["payload_len"], last["byte_length"] = total, byte_length
    return True


def ingest(con, run_id: int, log_path=None, backend: str | None = None) -> dict:
    """Append normalized events for whatever the raw log grew since last pass.

    Idempotent: the per-run byte cursor only moves forward, so calling this
    on every supervisor poll costs one seek and one read of the new bytes.
    Returns {"events": n, "skipped": n, "offset": n}.
    """
    if log_path is None or backend is None:
        run = con.execute("SELECT log_path, backend FROM runs WHERE id=?",
                          (run_id,)).fetchone()
        if not run:
            return {"events": 0, "skipped": 0, "offset": 0}
        log_path = log_path or run["log_path"]
        backend = backend or run["backend"]
    row = cursor(con, run_id)
    offset = int(row["byte_offset"]) if row else 0
    seq = int(row["seq"]) if row else 0
    skipped = int(row["skipped"]) if row else 0
    if not log_path or (row and row["raw_pruned_at"]):
        return {"events": 0, "skipped": skipped, "offset": offset}
    try:
        with open(log_path, "rb") as handle:
            handle.seek(offset)
            data = handle.read(MAX_CHUNK)
    except OSError:
        return {"events": 0, "skipped": skipped, "offset": offset}
    end = data.rfind(b"\n")
    if end < 0:
        if len(data) == MAX_CHUNK:
            # One pathological line must not wedge ingest forever.
            _save_cursor(con, run_id, offset + len(data), seq, skipped + 1)
            con.commit()
            return {"events": 0, "skipped": skipped + 1, "offset": offset + len(data)}
        return {"events": 0, "skipped": skipped, "offset": offset}
    written, line_start = 0, offset
    last = _last_event(con, run_id)
    for raw in data[:end + 1].splitlines(keepends=True):
        length = len(raw.rstrip(b"\r\n"))
        events = parse_line(backend, raw.decode("utf-8", "replace"))
        if events is None:
            skipped += 1
        for event in events or []:
            if _extend(con, last, event, line_start, length):
                continue  # a streamed fragment of the row before it
            seq += 1
            last = _insert(con, run_id, seq, event, line_start, length)
            written += 1
        line_start += len(raw)
    _save_cursor(con, run_id, offset + end + 1, seq, skipped)
    con.commit()
    return {"events": written, "skipped": skipped, "offset": offset + end + 1}


def record_injection(con, run_id: int, sender: str, body: str) -> None:
    """A human message delivered into a run has no raw-file backing, so it is
    written straight into the normalized stream with byte_offset -1."""
    _record_synthetic(con, run_id, _ev("human_injection", sender, body))


def record_lifecycle(con, run_id: int, name: str, payload: str = "") -> None:
    """Same, for something a hook observed that the raw log does not carry
    (DESIGN §6: OpenCode's ``permission.asked`` reaches Dromond only here)."""
    _record_synthetic(con, run_id, _ev("lifecycle", name, payload))


def _record_synthetic(con, run_id: int, event: dict) -> None:
    row = cursor(con, run_id)
    seq = (int(row["seq"]) if row else 0) + 1
    _insert(con, run_id, seq, event, -1, 0)
    _save_cursor(con, run_id, int(row["byte_offset"]) if row else 0, seq,
                 int(row["skipped"]) if row else 0)
    con.commit()


# --- reading ----------------------------------------------------------------

def _as_dict(row) -> dict:
    out = dict(row)
    out["truncated"] = bool(out.get("truncated"))
    return out


def events_for_run(con, run_id: int, after_id: int = 0,
                   limit: int = 500) -> list[dict]:
    """Append-only page of normalized events, oldest first."""
    return [_as_dict(r) for r in con.execute(
        "SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id LIMIT ?",
        (run_id, after_id, limit))]


def expand(con, event_id: int) -> dict:
    """Untruncated payload for one event, re-read from the raw file.

    The raw line is the source of truth; the stored payload is the fallback
    when the raw log has aged out or the event never had a file backing.
    """
    row = con.execute(
        "SELECT e.*, r.log_path, r.backend FROM events e "
        "JOIN runs r ON r.id=e.run_id WHERE e.id=?", (event_id,)).fetchone()
    if not row:
        raise KeyError(f"no event {event_id}")
    result = _as_dict(row)
    result["raw"] = None
    result["source"] = "stored"
    if row["byte_offset"] < 0 or not row["log_path"]:
        return result
    try:
        with open(row["log_path"], "rb") as handle:
            handle.seek(row["byte_offset"])
            raw = handle.read(row["byte_length"]).decode("utf-8", "replace")
    except OSError:
        return result  # raw log pruned; the truncated payload is what is left
    result["raw"] = raw
    result["source"] = "raw"
    # Re-derive the full payload from the same parser that produced the row,
    # so an expanded event reads identically to a short one. One line can
    # yield several events (a Claude message carries text + tool_use), so
    # match by position first and fall back to the first same-kind block.
    lines = raw.splitlines()
    if len(lines) > 1:
        # A coalesced fragment run spans its lines; rebuild it the way
        # ingest did rather than positionally.
        merged = "".join(
            candidate["payload"]
            for line in lines for candidate in parse_line(row["backend"], line) or []
            if candidate.get("merge") and candidate["kind"] == row["kind"])
        if merged:
            result["payload"] = merged
            result["truncated"] = False
        return result
    candidates = parse_line(row["backend"], raw) or []
    siblings = [r["id"] for r in con.execute(
        "SELECT id FROM events WHERE run_id=? AND byte_offset=? ORDER BY seq",
        (row["run_id"], row["byte_offset"]))]
    index = siblings.index(event_id) if event_id in siblings else -1
    if 0 <= index < len(candidates) and candidates[index]["kind"] == row["kind"]:
        candidates = [candidates[index]]
    for candidate in candidates:
        if candidate["kind"] == row["kind"]:
            result["payload"] = candidate["payload"]
            result["truncated"] = False
            break
    return result


# --- inbox / outbox ---------------------------------------------------------

# Inbound = written TO the run. An `ask` is the run's own question, so it is
# outbound; the human's `answer` comes back in (DESIGN §6).
_INBOUND_KINDS = {"interrupt", "tell", "answer", ""}


def run_messages(con, run_id: int) -> list[dict]:
    """Every message for one run, badged queued / delivered / answered.

    DESIGN §7: knowing what happened to a message is the feature. Rendering
    belongs to the dashboard; this is only the data.

    ponytail: "answered" is inferred — an inbound message counts as answered
    once the run produced an outbound message after it was delivered. There
    is no reply linkage in `messages` yet; add a `reply_to` column with
    `ask` (§6/§8) and read it here instead.
    """
    rows = list(con.execute(
        "SELECT id, sender, body, kind, created_at, delivery_offset, delivered_at, "
        "undeliverable_at, undeliverable_reason "
        "FROM messages WHERE run_id=? ORDER BY id", (run_id,)))
    outbound_after = {}
    latest_outbound = None
    for row in reversed(rows):
        outbound_after[row["id"]] = latest_outbound
        if row["kind"] not in _INBOUND_KINDS:
            latest_outbound = row["id"]
    out = []
    for row in rows:
        inbound = row["kind"] in _INBOUND_KINDS
        if not inbound:
            state = "delivered"          # the run said it; nothing to await
        elif row["undeliverable_at"]:
            # DESIGN §6: marked and surfaced, never dropped, never re-aimed
            # at a later run.
            state = "undeliverable"
        elif not row["delivered_at"]:
            state = "queued"
        elif outbound_after[row["id"]] is not None:
            state = "answered"
        else:
            state = "delivered"
        out.append({
            "id": row["id"], "sender": row["sender"], "body": row["body"],
            "kind": row["kind"], "created_at": row["created_at"],
            "delivered_at": row["delivered_at"],
            "direction": "inbound" if inbound else "outbound",
            "state": state,
            "undeliverable_reason": row["undeliverable_reason"],
            # DESIGN §7 boundary-pending badge: queued behind a safe action
            # boundary the backend has not reached yet.
            "pending_boundary": bool(inbound and not row["delivered_at"]
                                     and row["delivery_offset"] is not None),
        })
    return out


# --- retention --------------------------------------------------------------

def retention_days(cfg: dict | None = None) -> int:
    settings = (cfg or {}).get("settings", {}) if isinstance(cfg, dict) else {}
    try:
        days = int(settings.get("raw_log_retention_days",
                                DEFAULT_RAW_RETENTION_DAYS))
    except (TypeError, ValueError):
        days = DEFAULT_RAW_RETENTION_DAYS
    return max(1, days)


def prune_raw_logs(con, days: int | None = None, cfg: dict | None = None,
                   dry_run: bool = False, now=None) -> list[dict]:
    """Delete raw logs of TERMINAL runs older than ``days``. Never a live run.

    Normalized events are kept indefinitely, so pruning loses only the
    expand-in-place detail. Ingest runs once more per candidate first, so
    nothing unread is thrown away.
    """
    days = days if days is not None else retention_days(cfg)
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=days)) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = list(con.execute(
        "SELECT r.id, r.log_path, r.backend, r.status, r.finished_at FROM runs r "
        "LEFT JOIN trace_cursors c ON c.run_id=r.id "
        f"WHERE r.status IN {db.TERMINAL_SQL} AND r.finished_at IS NOT NULL "
        "AND r.finished_at < ? AND r.log_path IS NOT NULL "
        "AND c.raw_pruned_at IS NULL ORDER BY r.id", (cutoff,)))
    pruned = []
    for row in rows:
        path = Path(row["log_path"])
        if not path.is_file():
            continue
        size = path.stat().st_size
        entry = {"run_id": int(row["id"]), "log_path": str(path),
                 "bytes": size, "status": row["status"],
                 "finished_at": row["finished_at"]}
        if dry_run:
            pruned.append(entry)
            continue
        ingest(con, int(row["id"]), str(path), row["backend"])
        try:
            path.unlink()
        except OSError as exc:
            entry["error"] = str(exc)
            pruned.append(entry)
            continue
        con.execute(
            "INSERT INTO trace_cursors(run_id, byte_offset, seq, skipped, "
            "raw_pruned_at, updated_at) VALUES(?,0,0,0,?,?) "
            "ON CONFLICT(run_id) DO UPDATE SET raw_pruned_at=excluded.raw_pruned_at, "
            "updated_at=excluded.updated_at",
            (int(row["id"]), db.now(), db.now()))
        con.commit()
        pruned.append(entry)
    return pruned


# --- SSE (called by the http.py seam, DESIGN §3 / W-0100) -------------------

def sse(data, *, event: str | None = None, event_id=None) -> str:
    """One SSE frame. Payload is JSON; multi-line bodies stay legal."""
    body = data if isinstance(data, str) else _json(data)
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    if event:
        lines.append(f"event: {event}")
    lines += [f"data: {chunk}" for chunk in body.split("\n")]
    return "\n".join(lines) + "\n\n"


def _pause(stop, seconds: float) -> None:
    """Sleep, but wake immediately when the seam's stop Event is set."""
    if stop is not None and hasattr(stop, "wait"):
        stop.wait(seconds)
    else:
        time.sleep(seconds)


def _terminal(con, run_id: int) -> bool:
    row = con.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
    return bool(row) and row["status"] in db.RUN_TERMINAL


def stream_run_trace(run_id: int, after_id: int = 0, *, stop=None,
                     poll: float = 1.0, con=None, page: int = 500):
    """Append-only SSE stream of one run's normalized events.

    Yields ``str`` frames; the http seam writes them as
    ``text/event-stream``. ``after_id`` is the client's ``Last-Event-ID``.
    Ends with an ``end`` frame once the run is terminal and drained, so a
    finished run's stream closes instead of polling forever.
    """
    owned = con is None
    con = con or db.connect()
    try:
        yield f"retry: {SSE_RETRY_MS}\n\n"
        while stop is None or not stop.is_set():
            batch = events_for_run(con, run_id, after_id, page)
            for row in batch:
                after_id = row["id"]
                yield sse(row, event="trace", event_id=row["id"])
            if len(batch) == page:
                continue  # drain a backlog before sleeping
            if _terminal(con, run_id):
                # One more pass: the supervisor's final ingest may land
                # between the SELECT and the status read.
                final = events_for_run(con, run_id, after_id, page)
                for row in final:
                    after_id = row["id"]
                    yield sse(row, event="trace", event_id=row["id"])
                if not final:
                    yield sse({"run_id": run_id, "reason": "terminal"}, event="end")
                    return
                continue
            yield ": keepalive\n\n"
            _pause(stop, poll)
    finally:
        if owned:
            con.close()


def daemon_log_paths() -> list[Path]:
    return [paths.logs_dir() / name for name in DAEMON_LOGS]


def parse_daemon_cursor(last_event_id: str | None) -> dict[str, int]:
    """Decode a daemon-log ``Last-Event-ID`` (``out.log@1234,err.log@0``)."""
    out: dict[str, int] = {}
    for chunk in (last_event_id or "").split(","):
        name, _, offset = chunk.partition("@")
        if name.strip() and offset.strip().isdigit():
            out[name.strip()] = int(offset)
    return out


def stream_daemon_log(after: dict[str, int] | None = None, *, stop=None,
                      poll: float = 1.0, tail_bytes: int = 8192,
                      files=None):
    """Append-only SSE stream of the daemon's OWN log (DESIGN §7).

    Separate from a run trace: this answers "is the service healthy and
    doing its job" — sweeper passes, claims, dispatches, escalations,
    errors. Tails stdout and stderr; with no cursor it starts ``tail_bytes``
    back from the end so a fresh viewer sees recent context, not the year.
    Runs until ``stop`` is set or the consumer closes the generator.
    """
    after = dict(after or {})
    targets = [Path(f) for f in files] if files else daemon_log_paths()
    offsets = {}
    for path in targets:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        offsets[path.name] = after.get(path.name, max(0, size - tail_bytes))
    yield f"retry: {SSE_RETRY_MS}\n\n"
    while stop is None or not stop.is_set():
        moved = False
        for path in targets:
            try:
                with open(path, "rb") as handle:
                    handle.seek(offsets[path.name])
                    data = handle.read(MAX_CHUNK)
            except OSError:
                continue
            end = data.rfind(b"\n")
            if end < 0:
                continue
            for raw in data[:end].split(b"\n"):
                line = raw.decode("utf-8", "replace").rstrip("\r")
                offsets[path.name] += len(raw) + 1
                if not line.strip():
                    continue
                moved = True
                yield sse({"file": path.name, "line": line}, event="daemon",
                          event_id=",".join(f"{n}@{o}" for n, o in offsets.items()))
        if not moved:
            yield ": keepalive\n\n"
            _pause(stop, poll)
