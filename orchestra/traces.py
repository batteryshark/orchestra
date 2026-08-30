"""Normalized traces: one events table for every supported runtime stream.

The supervisor tails each runtime's JSONL, so ingest maps
every line into ONE shape — assistant text, reasoning, tool call, tool
result, permission request, human injection, lifecycle — and stores a ~2KB
truncated payload plus the byte offset of the line it came from.

**The raw file stays the source of truth.** These JSONL formats are
undocumented and drift, so a parser is best-effort by contract: an unknown
or malformed line is counted and skipped, never raised. Raw files remain the
full-fidelity record; normalized rows remain useful after explicit pruning.
"""
import json
import time
from pathlib import Path

from orchestra import db, profiles, runners

KINDS = ("assistant_text", "reasoning", "tool_call", "tool_result",
         "permission_request", "human_injection", "lifecycle")

MAX_PAYLOAD = 2048          # ~2KB truncated payload (DESIGN §13)
MAX_CHUNK = 4_000_000       # bytes read per ingest pass


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
# annotate. `hook_started`/`hook_response` bracket tool calls the trace
# already shows as tool_call/tool_result. Only the subtypes a human would
# read become events; the rest are recognized and dropped. The raw log keeps
# every line either way.
_CLAUDE_NOISE = {"thinking_tokens", "status", "hook_started", "hook_response"}


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
    if kind in ("turn.failed", "error"):
        return [_ev("lifecycle", kind, obj, _ts(obj))]
    if kind in ("thread.started", "turn.started", "turn.completed",
                "session.created"):
        # Thread and turn markers frame items the trace already carries;
        # measured on the live store, chatter like this was ~530 of ~1300
        # events. Usage rides `turn.completed` but is read from the raw log
        # (runners.parse_usage), never from stored events.
        return []
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
        if status == "running":
            # Every tool logs the same part at "pending" and again at
            # "running" (222 calls vs 111 results on one bash-heavy run), so
            # only the running part is the tool_call.
            return [_ev("tool_call", name, state.get("input"), _ts(part))]
        return []
    if kind == "session.error":
        return [_ev("lifecycle", kind, obj, _ts(obj))]
    if part_type in ("step-start", "step-finish") or kind in (
            "step_start", "step_finish"):
        # Step markers bracket parts the trace already carries; session.idle,
        # session.updated and message.updated fall to the prefix catch-all
        # below. Boundary watching (supervise) and usage capture (runners)
        # read these from the raw log, never from stored events.
        return []
    if kind == "text" and isinstance(obj.get("text"), str):
        return [_ev("assistant_text", None, obj["text"])]  # bare shape, older builds
    if kind.startswith(("message.", "session.", "storage.", "file.", "server.")):
        return []
    return None


# Reasonix discriminates on `kind`, not `type` — except for its FINAL line,
# which is Claude-shaped (`{"type": "result", ...}`) and carries session_id,
# total_cost_usd and token usage. Both shapes have to parse.
_REASONIX_FRAGMENTS = {"text": "assistant_text", "reasoning": "reasoning"}
# Turn markers, stream retries, interim tool output and per-turn usage
# counters: measured on the live store, chatter like this was ~530 of ~1300
# events. The final Claude-shaped result line is the authoritative usage
# source (runners._usage_reasonix) and stays stored; per-turn `usage` lines
# duplicate it.
_REASONIX_NOISE = ("turn_started", "stream_attempt", "usage", "tool_progress")


def _reasonix(obj) -> list[dict] | None:
    kind = obj.get("kind")
    if kind is None:
        # The run's last line only. Claude's parser already reads it, and
        # the whole object becomes the payload so statistics can
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
    if kind in _REASONIX_NOISE:
        return []
    return None


# --- ACP transport (DESIGN §5) ----------------------------------------------
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
# Methods Orchestra sends INTO a session: the brief, a delivered message, and
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
        return [_ev("human_injection", "orchestra", _text_of(params.get("prompt")), ts)]
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
    # An ACP frame is self-describing, so the transport is read off
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


# --- progress ---------------------------------------------------------------

# A tool's most identifying argument. `command` is often argv rather than a
# string, which is why runners._find_command reads it and _dig reads the rest.
ARG_KEYS = {"filePath", "file_path", "path", "pattern", "query", "url"}


def progress(log_path: str, backend: str) -> str | None:
    """One line describing what a live run has been doing, read from the
    trace at call time.

    Current fields carry current facts: a progress line that repeats a
    stale count makes a healthy run indistinguishable from a hung one.
    Counted through ``parse_line`` so every tool the backend
    reports is one -- counting shell commands alone froze run 234 at "1
    tool call" for 40 minutes while it made 84.

    Carries how long ago the trace last grew, because the count alone still
    hides a hang: a run stuck on its first tool repeats "1 tool call"
    forever, and only the age separates that from a run between tools. The
    same fact `orchestra check` reports as "log written Ns ago".

    Deterministic transcript reading, never a question put to the worker --
    a status report costs a model turn, this costs a file read.
    """
    calls = results = 0
    last_call = last_result = said = None
    try:
        with open(log_path, errors="replace") as handle:
            for line in handle:
                for event in parse_line(backend, line) or ():
                    kind = event["kind"]
                    if kind == "tool_call":
                        calls += 1
                        last_call = (event["name"], line)
                    elif kind == "tool_result":
                        results += 1
                        last_result = (event["name"], line)
                    elif kind == "assistant_text" and event["payload"].strip():
                        said = event["payload"].strip().splitlines()[0]
        quiet = time.time() - Path(log_path).stat().st_mtime
    except (OSError, TypeError):   # a run claimed before its log path is set
        return None
    # Finished tools are the one count every backend agrees on: opencode
    # logs only the completed part, and Reasonix streams `tool_dispatch`
    # twice per tool (`partial`), so counting calls would double it. Calls
    # stand in only before the first tool has returned.
    actions = results or calls
    if not actions and not said:
        return None
    parts = [f"{actions} tool call{'s' if actions != 1 else ''}"] if actions else []
    last = last_call or last_result
    if last:
        parts.append(f"last: {_action_label(*last)}")
    elif said:
        parts.append(f"last said: {said[:120]}")
    age = f"log written {profiles.age_text(max(quiet, 0))}"
    head = "; ".join(parts)
    # The age is the hang signal: never let the 400-char cap cut it off.
    # Trim the head (the tool label), which is the least important part.
    room = 400 - len(age) - 2   # "; " joins head and age
    if len(head) > room:
        head = head[:room]
    return f"{head}; {age}" if head else age


def _action_label(name: str | None, line: str) -> str:
    """A tool's name plus its argument, best-effort — each backend buries
    the argument at a different depth, and Reasonix carries none at all.

    Whitespace collapses: a heredoc or a `python -c` script is a real
    command, and its newlines would break the one-line progress note into
    an unreadable block on the board.
    """
    try:
        obj = json.loads(line)
    except ValueError:
        return name or "tool"
    arg = runners._find_command(obj) or next(iter(runners._dig(obj, ARG_KEYS)), "")
    return " ".join(f"{name or 'tool'} {arg}".split())[:120]


# --- ingest -----------------------------------------------------------------

def cursor(con, run_id: int):
    return con.execute("SELECT * FROM trace_cursors WHERE run_id=?",
                       (run_id,)).fetchone()


def _backend(snapshot: str | None) -> str:
    try:
        value = json.loads(snapshot or "{}")
    except (TypeError, ValueError):
        return ""
    return str(value.get("adapter") or "") if isinstance(value, dict) else ""


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
        run = con.execute("SELECT log_path,runtime_snapshot FROM runs WHERE id=?",
                          (run_id,)).fetchone()
        if not run:
            return {"events": 0, "skipped": 0, "offset": 0}
        log_path = log_path or run["log_path"]
        backend = backend or _backend(run["runtime_snapshot"])
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


def drain(con, run_id: int, log_path=None, backend: str | None = None) -> dict:
    """Ingest every complete line currently present, including large bursts."""
    events = 0
    while True:
        before = cursor(con, run_id)
        previous = int(before["byte_offset"]) if before else 0
        result = ingest(con, run_id, log_path, backend)
        events += int(result["events"])
        if int(result["offset"]) <= previous:
            return {**result, "events": events}


def record_injection(con, run_id: int, sender: str, body: str) -> None:
    """A human message delivered into a run has no raw-file backing, so it is
    written straight into the normalized stream with byte_offset -1."""
    _record_synthetic(con, run_id, _ev("human_injection", sender, body))


def record_lifecycle(con, run_id: int, name: str, payload: str = "") -> None:
    """Record a supervisor lifecycle fact with no raw-file backing."""
    _record_synthetic(con, run_id, _ev("lifecycle", name, payload))


def _record_synthetic(con, run_id: int, event: dict) -> None:
    row = cursor(con, run_id)
    seq = (int(row["seq"]) if row else 0) + 1
    _insert(con, run_id, seq, event, -1, 0)
    _save_cursor(con, run_id, int(row["byte_offset"]) if row else 0, seq,
                 int(row["skipped"]) if row else 0)
    con.commit()
