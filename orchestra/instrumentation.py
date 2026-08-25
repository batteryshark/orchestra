"""Run instrumentation derived from normalized traces and result rows."""

import json
import re
from collections import defaultdict, deque
from datetime import datetime, timezone

from orchestra import db


CLASSES = ("build", "test", "search", "read", "git", "wait", "other",
           "unclassified")
_TERMINAL_FAILURES = set(db.RUN_TERMINAL) - {"done"}
_ERROR = re.compile(
    r"\b(error|failed|failure|exception|traceback)\b|exit[_ ]code[^0-9]*[1-9]",
    re.I)
_COMMAND_CLASSIFIERS = (
    ("git", re.compile(r"(?:^|[;&|]\s*)(?:git|gh)(?:\s|$)", re.I)),
    ("wait", re.compile(r"(?:^|[;&|]\s*)(?:sleep|wait)(?:\s|$)", re.I)),
    ("search", re.compile(r"(?:^|[;&|]\s*)(?:rg|grep|find|fd)(?:\s|$)", re.I)),
    ("read", re.compile(r"(?:^|[;&|]\s*)(?:cat|sed|head|tail|less)(?:\s|$)", re.I)),
)
_TEST = re.compile(r"\b(?:pytest|unittest|xctest|test|tests)\b", re.I)
_BUILD = re.compile(r"\b(?:build|compile|xcodebuild|make|cmake|ninja)\b", re.I)
_PATH = re.compile(r"(?:^|\s)([^\s;|]+\.[A-Za-z0-9]{1,10})(?=\s|$|[;|])")


def _epoch(value) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _elapsed(started, finished) -> float:
    start = _epoch(started)
    end = _epoch(finished) if finished else datetime.now(timezone.utc).timestamp()
    return round(max(0.0, (end or 0) - start), 1) if start is not None else 0.0


def _text(payload) -> str:
    try:
        value = json.loads(payload)
    except (TypeError, ValueError):
        return payload or ""
    def flatten(item):
        if isinstance(item, dict):
            return " ".join(flatten(v) for v in item.values())
        if isinstance(item, list):
            return " ".join(flatten(v) for v in item)
        return "" if item is None else str(item)
    return flatten(value)


def _command(payload) -> str:
    try:
        value = json.loads(payload)
    except (TypeError, ValueError):
        return payload or ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(str(part) for part in value)
    if isinstance(value, dict):
        for key in ("command", "cmd"):
            command = value.get(key)
            if isinstance(command, str):
                return command
            if isinstance(command, list):
                return " ".join(str(part) for part in command)
        for child in value.values():
            if isinstance(child, (dict, list)):
                found = _command(json.dumps(child))
                if found:
                    return found
    return ""


def _classify(name, payload) -> tuple[str, str]:
    text = f"{name or ''} {_text(payload)}".strip()
    if not text:
        return "unclassified", ""
    command = _command(payload)
    for kind, pattern in _COMMAND_CLASSIFIERS:
        if pattern.search(command):
            return kind, text
    if _TEST.search(command):
        return "test", text
    if _BUILD.search(command):
        return "build", text
    tool = (name or "").lower()
    if "search" in tool or "query" in tool:
        return "search", text
    if "read" in tool or "view" in tool or tool == "open":
        return "read", text
    if "wait" in tool:
        return "wait", text
    return "other", text


def _failed(payload) -> bool:
    text = _text(payload)
    try:
        value = json.loads(payload)
    except (TypeError, ValueError):
        value = None
    if isinstance(value, dict):
        status = str(value.get("status", "")).lower()
        if status in {"error", "failed", "failure"} or value.get("err"):
            return True
        code = value.get("exit_code", value.get("exitCode"))
        if isinstance(code, int):
            return code != 0
    return bool(_ERROR.search(text))


def _events(con, run_ids) -> dict[int, list]:
    if not run_ids:
        return {}
    marks = ",".join("?" * len(run_ids))
    grouped = defaultdict(list)
    for row in con.execute(
            f"SELECT run_id, kind, name, payload, ts, created_at FROM events "
            f"WHERE run_id IN ({marks}) AND kind IN "
            "('assistant_text','human_injection','tool_call','tool_result','lifecycle') "
            "ORDER BY run_id, seq", run_ids):
        grouped[int(row["run_id"])].append(row)
    return grouped


def _run(row, events) -> dict:
    calls = []
    pending = defaultdict(deque)
    turns = compactions = 0
    for event in events:
        if event["kind"] == "assistant_text":
            turns += 1
        if event["kind"] == "lifecycle" and "compact" in (event["name"] or "").lower():
            compactions += 1
        if event["kind"] == "tool_call":
            kind, text = _classify(event["name"], event["payload"])
            call = {"class": kind, "text": text, "seconds": None, "error": False}
            calls.append(call)
            pending[event["name"] or ""].append((call, _epoch(event["ts"] or event["created_at"])))
        elif event["kind"] == "tool_result":
            queue = pending[event["name"] or ""]
            if not queue:
                queue = next((q for q in pending.values() if q), deque())
            if queue:
                call, started = queue.popleft()
                finished = _epoch(event["ts"] or event["created_at"])
                if started is not None and finished is not None:
                    call["seconds"] = round(max(0, finished - started), 3)
                call["error"] = _failed(event["payload"])

    tools = {kind: {"calls": 0, "errors": 0, "error_rate": 0.0,
                    "seconds": 0.0, "timed_calls": 0} for kind in CLASSES}
    failed_commands = defaultdict(int)
    read_files = defaultdict(int)
    for call in calls:
        bucket = tools[call["class"]]
        bucket["calls"] += 1
        bucket["errors"] += int(call["error"])
        if call["seconds"] is not None:
            bucket["seconds"] = round(bucket["seconds"] + call["seconds"], 3)
            bucket["timed_calls"] += 1
        if call["error"] and call["text"]:
            failed_commands[call["text"][:500]] += 1
        if call["class"] == "read":
            for path in _PATH.findall(call["text"]):
                read_files[path] += 1
    for bucket in tools.values():
        if bucket["calls"]:
            bucket["error_rate"] = round(bucket["errors"] / bucket["calls"], 3)
    total = len(calls)
    classified = total - tools["unclassified"]["calls"]
    return {
        "run_id": int(row["id"]), "slug": row["slug"], "profile": row["profile"],
        "model": row["model"], "status": row["status"],
        "seconds": _elapsed(row["started_at"], row["finished_at"]),
        "tokens": row["tokens_total"], "turns": turns, "compactions": compactions,
        "tool_calls": total,
        "classification_rate": round(classified / total, 3) if total else 1.0,
        "tools": tools,
        "failed_commands": failed_commands, "read_files": read_files,
        "landed": row["landing_status"] == "ok",
    }


def report(con, limit: int = 30, project_id: str | None = None) -> dict:
    """Instrumentation for the latest ``limit`` worker runs, newest first."""
    limit = max(1, min(int(limit), 500))
    rows = list(con.execute(
        "SELECT id, slug, profile, model, status, started_at, finished_at, "
        "tokens_total, landing_status FROM runs WHERE layer IS NULL"
        + (" AND project_id=?" if project_id else "")
        + " ORDER BY id DESC LIMIT ?", ((project_id, limit) if project_id else (limit,))))
    events = _events(con, [int(row["id"]) for row in rows])
    runs = [_run(row, events[int(row["id"])]) for row in rows]

    tools = {kind: {"calls": 0, "errors": 0, "error_rate": 0.0,
                    "seconds": 0.0, "timed_calls": 0} for kind in CLASSES}
    gaps = defaultdict(lambda: {"kind": "", "value": "", "count": 0, "runs": set()})
    comparisons = {}
    terminal = failures = landings = tool_calls = unclassified = 0
    for run in runs:
        for kind in CLASSES:
            for field in ("calls", "errors", "seconds", "timed_calls"):
                tools[kind][field] += run["tools"][kind][field]
        tool_calls += run["tool_calls"]
        unclassified += run["tools"]["unclassified"]["calls"]
        if run["status"] in db.RUN_TERMINAL:
            terminal += 1
            failures += int(run["status"] in _TERMINAL_FAILURES)
        landings += int(run["landed"])
        key = (run["profile"], run["model"] or "")
        item = comparisons.setdefault(key, {"profile": key[0], "model": run["model"],
                                             "runs": 0, "terminal": 0, "failures": 0,
                                             "landings": 0, "seconds": 0.0})
        item["runs"] += 1
        item["terminal"] += int(run["status"] in db.RUN_TERMINAL)
        item["failures"] += int(run["status"] in _TERMINAL_FAILURES)
        item["landings"] += int(run["landed"])
        item["seconds"] += run["seconds"]
        for kind, values in (("failing command", run.pop("failed_commands")),
                             ("re-read file", run.pop("read_files"))):
            for value, count in values.items():
                gap = gaps[(kind, value)]
                gap.update(kind=kind, value=value)
                gap["count"] += count
                gap["runs"].add(run["run_id"])
    for bucket in tools.values():
        bucket["seconds"] = round(bucket["seconds"], 3)
        bucket["error_rate"] = (
            round(bucket["errors"] / bucket["calls"], 3)
            if bucket["calls"] else 0.0)
    for item in comparisons.values():
        item["failure_rate"] = (
            round(item["failures"] / item["terminal"], 3)
            if item["terminal"] else None)
        item["landings_per_hour"] = (
            round(item["landings"] * 3600 / item["seconds"], 3)
            if item["seconds"] else None)
        item["seconds"] = round(item["seconds"], 1)
    candidates = [{**g, "runs": sorted(g["runs"])} for g in gaps.values() if g["count"] >= 2]
    candidates.sort(key=lambda g: (-g["count"], g["kind"], g["value"]))
    seconds = sum(run["seconds"] for run in runs)
    return {
        "window": limit, "runs_count": len(runs), "runs": runs, "tools": tools,
        "turns": sum(run["turns"] for run in runs),
        "compactions": sum(run["compactions"] for run in runs),
        "tokens": (sum(run["tokens"] for run in runs
                       if run["tokens"] is not None)
                   if any(run["tokens"] is not None for run in runs) else None),
        "failure_rate": round(failures / terminal, 3) if terminal else None,
        "landings_per_hour": round(landings * 3600 / seconds, 3) if seconds else None,
        "classification_rate": (
            round((tool_calls - unclassified) / tool_calls, 3)
            if tool_calls else 1.0),
        "comparisons": sorted(
            comparisons.values(), key=lambda x: (x["profile"], x["model"] or "")),
        "gap_candidates": candidates,
    }
