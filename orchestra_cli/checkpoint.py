"""Backend-neutral orchestrator checkpoint + takeover.

A checkpoint is **durable intent + high-water marks**, not a frozen copy
of every state row. The source orchestrator writes the objective, next
steps, and the largest ``runs.id``, ``messages.id``, and ``feed.id``
observed at write time; takeover re-queries the live DB for everything
**after** those marks and combines it with the durable intent, so the
brief stays fresh even if a long time passes between write and read.

Safety contract (must hold — tested explicitly):
  * never include ``session_ref``, ``pid``, ``log_path``, ``brief_path``,
    ``workdir``, ``branch``, argv, env, or raw transcripts
  * every user-controlled serialized text field is redacted for
    high-confidence credential patterns BEFORE it lands on disk
  * on render, the loaded checkpoint's free-text is re-sanitized as
    defense in depth (so a hand-edited or older-format checkpoint still
    cannot leak credentials)
  * checkpoint writes are atomic (write-temp + rename, mode 0600)
  * takeover is logically read-only — opens the DB in SQLite URI
    ``mode=ro`` so no source row, schema, or journal mode is changed
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestra_cli import db, paths, projects

SCHEMA_VERSION = 1
SCHEMA_TAG = "orchestra.checkpoint/v1"

# --- bounded surface ------------------------------------------------------

INBOX_SNAPSHOT_LIMIT = 10
FEED_SNAPSHOT_LIMIT = 10
NEW_RUNS_LIMIT = 25
NEW_MESSAGES_LIMIT = 25
NEW_FEED_LIMIT = 25
ACTIVE_RUNS_LIMIT = 25
ACTIVE_WORK_ITEMS_LIMIT = 10
WORK_SHOW_MAX_BYTES = 16_000   # bound the `work show` subprocess payload
WORK_SHOW_TIMEOUT = 15         # seconds
OBJECTIVE_MAX_CHARS = 280
NEXT_STEP_MAX_CHARS = 280
NEXT_STEPS_MAX_COUNT = 10
TITLE_MAX_CHARS = 120
TAGS_MAX_CHARS = 120
BODY_PREVIEW_CHARS = 400

# Whitelisted run/message/feed fields. Every name here is metadata; no
# process / session / transcript surface. ``summary`` is intentionally
# absent — it is the worker's last text output and counts as transcript
# content, which the safety contract forbids.
SAFE_RUN_FIELDS = (
    "id", "slug", "agent", "backend", "model", "title",
    "work_item", "team", "requested_by", "status",
    "started_at", "finished_at",
)
SAFE_MESSAGE_FIELDS = (
    "id", "sender", "recipient", "work_item", "run_id",
    "kind", "created_at", "read_at",
)
SAFE_FEED_FIELDS = (
    "id", "author", "tags", "work_item", "run_id", "created_at",
)

#: High-confidence credential patterns. Conservative on purpose — false
#: positives are tolerable; a leaked secret is not.
_CRED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"gho_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    re.compile(r"glpat-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{20,}"),
    re.compile(r"(?i)(?:api[_-]?key|secret|token|password)\s*[=:]\s*[\"']?[A-Za-z0-9._\-/+=]{16,}[\"']?"),
)


def _redact(text: str | None) -> str | None:
    if not isinstance(text, str) or not text:
        return text
    out = text
    for pat in _CRED_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    return out


def _truncate(s, limit: int) -> str | None:
    if not isinstance(s, str):
        return s
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 1)].rstrip() + "\u2026"


def _utc_now() -> str:
    """ISO UTC, microsecond resolution, no ``:`` for filesystem safety."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H-%M-%S") + f".{now.microsecond:06d}Z"


# ---------------------------------------------------------------------------
# Safe-row projection. The ``redact_text`` flag lets us opt fields into
# redaction when they could carry user-controlled secrets.
# ---------------------------------------------------------------------------

def _safe_row(row, fields: tuple[str, ...], *, redact_text: bool = False) -> dict | None:
    if not row:
        return None
    src = dict(row)
    out = {k: src.get(k) for k in fields}
    if redact_text:
        for k in fields:
            if isinstance(out.get(k), str):
                limit = TITLE_MAX_CHARS if k in {"title", "tags"} else BODY_PREVIEW_CHARS
                out[k] = _truncate(_redact(out[k]), limit)
    return out


def _safe_message(row, *, with_body: bool = True) -> dict | None:
    out = _safe_row(row, SAFE_MESSAGE_FIELDS, redact_text=True)
    if not out:
        return None
    if with_body:
        body = row["body"] if isinstance(row, sqlite3.Row) else row.get("body")
        if isinstance(body, str):
            out["body_preview"] = _truncate(_redact(body), BODY_PREVIEW_CHARS)
    return out


def _safe_feed(row) -> dict | None:
    out = _safe_row(row, SAFE_FEED_FIELDS, redact_text=True)
    if not out:
        return None
    body = row["body"] if isinstance(row, sqlite3.Row) else row.get("body")
    if isinstance(body, str):
        out["body_preview"] = _truncate(_redact(body), BODY_PREVIEW_CHARS)
    return out


@dataclass(frozen=True)
class Checkpoint:
    path: Path
    data: dict

    @property
    def source(self) -> str:
        return str(self.data.get("source") or "")

    @property
    def objective(self) -> str | None:
        v = self.data.get("objective")
        return v if isinstance(v, str) else None

    @property
    def next_steps(self) -> list[str]:
        v = self.data.get("next_steps") or []
        return [str(x) for x in v if isinstance(x, str)]

    @property
    def high_water(self) -> dict:
        hw = self.data.get("high_water") or {}
        return hw if isinstance(hw, dict) else {}

    @property
    def work_item(self) -> str | None:
        v = self.data.get("work_item")
        return v if isinstance(v, str) and v else None


# ---------------------------------------------------------------------------
# Work-item sourcing. We parse ``work list`` TSV and ``work show --json``
# directly — the CLI's documented filters are minimal and we don't want
# to depend on undocumented ones. Every helper MUST be safe to call when
# ``work`` is missing or fails (objective inference must be fail-open).
# ---------------------------------------------------------------------------

ACTIVE_WORK_STATUSES = {"in_progress", "review"}


def _parse_work_list_text(text: str, *, limit: int = ACTIVE_WORK_ITEMS_LIMIT) -> list[dict]:
    out: list[dict] = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        wid, status, priority, assignee, title = (
            parts[0], parts[1], parts[2], parts[3], "\t".join(parts[4:]).strip(),
        )
        if not wid.startswith("W-") or status not in ACTIVE_WORK_STATUSES:
            continue
        out.append({
            "id": wid,
            "status": status,
            "priority": priority,
            "assignee": None if assignee in ("", "-") else assignee,
            "title": _truncate(title, TITLE_MAX_CHARS) or "",
        })
        if len(out) >= limit:
            break
    return out


def _work_active_items(root: Path) -> list[dict]:
    if not shutil.which("work"):
        return []
    try:
        proc = subprocess.run(
            ["work", "list"], cwd=root,
            capture_output=True, text=True, timeout=20,
        )
    except Exception:
        return []
    if proc.returncode != 0 or not proc.stdout.strip():
        return []
    return _parse_work_list_text(proc.stdout)


def _pick_fallback_objective(items: list[dict]) -> str | None:
    """Choose the best active item: highest priority wins, ties broken
    by the order ``work list`` returned them. Sorting by W id would let
    an ancient W-0001 beat a current W-0010, so we explicitly preserve
    the CLI's ordering within the best priority bucket."""
    if not items:
        return None
    pri_rank = {"high": 0, "medium": 1, "low": 2}
    best_rank = min(pri_rank.get(it["priority"], 9) for it in items)
    for it in items:
        if pri_rank.get(it["priority"], 9) == best_rank:
            return it["title"]
    return items[0]["title"]


def _work_show_item(root: Path, work_item: str) -> dict | None:
    """Bounded, fail-open ``work show ITEM --json`` for the exact
    ``--work`` item the source passed. Returns ``None`` on any failure."""
    if not shutil.which("work") or not work_item:
        return None
    try:
        proc = subprocess.run(
            ["work", "show", work_item, "--json"],
            cwd=root, capture_output=True, text=True,
            timeout=WORK_SHOW_TIMEOUT,
        )
    except Exception:
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    raw = proc.stdout.encode("utf-8", errors="replace")[:WORK_SHOW_MAX_BYTES]
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _objective_from_work_item(root: Path, work_item: str) -> tuple[str | None, dict | None]:
    """Bounded best-effort objective from the exact ``--work`` item.

    Priority: ``title`` > first requirement > first acceptance > ``goal``.
    Each candidate is redacted and bounded to ``OBJECTIVE_MAX_CHARS``.
    Returns ``(objective, source_summary)``; ``source_summary`` is a tiny
    dict describing what we used, surfaced in the checkpoint for audit.
    """
    data = _work_show_item(root, work_item)
    if not data:
        return None, None
    candidates: list[tuple[str, Any]] = []
    title = data.get("title")
    if isinstance(title, str) and title.strip():
        candidates.append(("title", title))
    sections = data.get("sections") or {}
    if isinstance(sections, dict):
        for key in ("goal", "plan", "notes"):
            v = sections.get(key)
            if isinstance(v, str) and v.strip():
                first_line = v.strip().splitlines()[0]
                candidates.append((key, first_line))
        # Specific structured fields win over free text.
        reqs = sections.get("requirements")
        if isinstance(reqs, list):
            for r in reqs:
                if isinstance(r, dict):
                    txt = r.get("text")
                    if isinstance(txt, str) and txt.strip():
                        candidates.append(("requirement", txt.strip()))
                        break
                elif isinstance(r, str) and r.strip():
                    candidates.append(("requirement", r.strip()))
                    break
        accs = sections.get("acceptanceCriteria")
        if isinstance(accs, list):
            for a in accs:
                if isinstance(a, dict):
                    txt = a.get("text")
                    if isinstance(txt, str) and txt.strip():
                        candidates.append(("acceptance", txt.strip()))
                        break
                elif isinstance(a, str) and a.strip():
                    candidates.append(("acceptance", a.strip()))
                    break
    for source, raw in candidates:
        bounded = _truncate(_redact(raw), OBJECTIVE_MAX_CHARS)
        if isinstance(bounded, str) and bounded:
            return bounded, {"work_item": work_item, "field": source}
    return None, {"work_item": work_item, "field": None}


def _infer_objective(root: Path, *, explicit: str | None,
                     work_item: str | None) -> tuple[str | None, list[dict], dict | None]:
    """Resolution order:

      1. ``explicit`` objective (caller-supplied) wins.
      2. ``work_item`` anchor: ``work show ITEM --json``, bounded.
      3. Active-items fallback that preserves ``work list`` ordering.

    ``active_items`` is returned in either case so the brief can still
    surface the live work list.
    """
    active_items = _work_active_items(root)
    if explicit:
        bounded = _truncate(_redact(explicit), OBJECTIVE_MAX_CHARS)
        return (bounded if isinstance(bounded, str) else None), active_items, None
    if work_item:
        obj, summary = _objective_from_work_item(root, work_item)
        if obj:
            return obj, active_items, summary
    obj = _pick_fallback_objective(active_items)
    if obj:
        obj = _truncate(_redact(obj), OBJECTIVE_MAX_CHARS)
    return (obj if isinstance(obj, str) else None), active_items, None


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def _max_id(con: sqlite3.Connection, table: str) -> int | None:
    row = con.execute(f"SELECT MAX(id) AS m FROM {table}").fetchone()
    val = row["m"] if row else None
    return int(val) if isinstance(val, int) else None


def _newest_n(con: sqlite3.Connection, where_clause: str, where_params: tuple,
              limit: int, *, order: str = "id") -> list[sqlite3.Row]:
    """DESC LIMIT then reverse — keeps the FRESHEST entries when LIMIT clips.

    A naive ``ORDER BY id LIMIT N`` keeps the OLDEST and drops exactly
    the most recent handoff, which is precisely the entry the successor
    needs. This helper applies the same defensive ordering everywhere.
    """
    q = f"SELECT * FROM {where_clause} ORDER BY {order} DESC LIMIT ?"
    rows = list(con.execute(q, where_params + (limit,)))
    return rows[::-1]


def _recent_messages_to(con: sqlite3.Connection, recipient: str,
                        limit: int) -> list[dict]:
    rows = _newest_n(
        con, "messages WHERE recipient=?", (recipient,), limit,
    )
    return [m for m in (_safe_message(r) for r in rows) if m]


def _recent_feed(con: sqlite3.Connection, limit: int) -> list[dict]:
    rows = _newest_n(con, "feed", (), limit)
    return [f for f in (_safe_feed(r) for r in rows) if f]


def _active_runs(con: sqlite3.Connection, limit: int) -> list[dict]:
    rows = _newest_n(
        con,
        "runs WHERE status NOT IN ('done','failed','timeout','killed')",
        (), limit,
    )
    return [r for r in (_safe_row(rr, SAFE_RUN_FIELDS, redact_text=True)
                        for rr in rows) if r]


def _rows_since(con: sqlite3.Connection, table: str, after_id: int | None,
                limit: int) -> list[sqlite3.Row]:
    if after_id is None:
        return _newest_n(con, table, (), limit)
    return _newest_n(con, f"{table} WHERE id > ?", (after_id,), limit)


def build_checkpoint(
    root: Path,
    *,
    source: str,
    objective: str | None = None,
    next_steps: list[str] | None = None,
    work_item: str | None = None,
    inbox_snapshot_limit: int = INBOX_SNAPSHOT_LIMIT,
    feed_snapshot_limit: int = FEED_SNAPSHOT_LIMIT,
) -> dict:
    """Assemble the checkpoint document. Does NOT write to disk."""
    obj, active_items, objective_source = _infer_objective(
        root, explicit=objective, work_item=work_item,
    )
    bounded_next = [
        t for t in (
            _truncate(_redact(s), NEXT_STEP_MAX_CHARS)
            for s in (next_steps or [])[:NEXT_STEPS_MAX_COUNT]
            if isinstance(s, str) and s.strip()
        ) if isinstance(t, str)
    ]
    con = db.connect(root)  # checkpoint WRITE — needs read+write for INSERT/MAX
    try:
        high_water = {
            "max_run_id": _max_id(con, "runs"),
            "max_message_id": _max_id(con, "messages"),
            "max_feed_id": _max_id(con, "feed"),
        }
        active_runs = _active_runs(con, ACTIVE_RUNS_LIMIT)
        source_inbox = _recent_messages_to(con, source, inbox_snapshot_limit)
        feed_snapshot = _recent_feed(con, feed_snapshot_limit)
    finally:
        con.close()
    try:
        proj_id = projects.project_id(root)
    except Exception:
        proj_id = None
    # Work tracker fields are user-controlled too; sanitize every string,
    # not only the title.
    bounded_items = []
    for it in active_items:
        item = dict(it)
        for key, value in item.items():
            if isinstance(value, str):
                limit = TITLE_MAX_CHARS if key in {"title", "assignee"} else 64
                item[key] = _clean_text(value, limit)
        bounded_items.append(item)
    if isinstance(objective_source, dict):
        objective_source = {
            "work_item": _clean_text(objective_source.get("work_item"), 64),
            "field": _clean_text(objective_source.get("field"), 64),
        }
    safe_source = _clean_text(source, TITLE_MAX_CHARS) or "orchestrator"
    safe_work_item = _clean_text(work_item, 64)
    return {
        "version": SCHEMA_VERSION,
        "schema": SCHEMA_TAG,
        "created_at": _utc_now(),
        "source": safe_source,
        "project": {
            "root": _clean_text(str(root), 4096),
            "name": _clean_text(root.name, TITLE_MAX_CHARS),
            "id": proj_id,
        },
        "objective": obj,
        "next_steps": bounded_next,
        "work_item": safe_work_item,
        "objective_source": objective_source,
        "high_water": high_water,
        "active_runs": active_runs,
        "source_inbox_snapshot": source_inbox,
        "feed_snapshot": feed_snapshot,
        "active_work_items": bounded_items,
    }


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _checkpoint_path(root: Path, source: str, when: str | None = None) -> Path:
    safe_source = re.sub(r"[^A-Za-z0-9_.-]", "_", source)[:32] or "orchestrator"
    return paths.checkpoints_dir(root, create=True) / f"{safe_source}-{when or _utc_now()}.json"


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def write_checkpoint(
    root: Path,
    *,
    source: str,
    objective: str | None = None,
    next_steps: list[str] | None = None,
    work_item: str | None = None,
) -> Path:
    payload = build_checkpoint(
        root, source=source, objective=objective,
        next_steps=next_steps, work_item=work_item,
    )
    target = _checkpoint_path(root, payload["source"])
    _atomic_write_json(target, payload)
    return target


def _list_checkpoint_files(root: Path, *, create: bool = False) -> list[Path]:
    d = paths.checkpoints_dir(root, create=create)
    return sorted(d.glob("*.json"))


def _checkpoint_created_at(path: Path) -> tuple[str, str]:
    """Return ``(created_at, name)`` for tie-break sorting.

    Reads ``created_at`` from the JSON; falls back to filename when the
    file is unreadable / corrupt so the listing still produces something
    useful for the operator. Lexicographic filename order across
    different source prefixes is NOT a valid ordering — different sources
    can produce filenames like ``codex-...`` and ``claude-...`` that
    sort lexicographically instead of by time, so we always parse the
    JSON.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        ca = data.get("created_at")
        if isinstance(ca, str):
            return ca, path.name
    except (OSError, ValueError):
        pass
    return "", path.name


def list_checkpoints(root: Path, source: str | None = None) -> list[Path]:
    files = _list_checkpoint_files(root)
    if source is None:
        candidates = files
    else:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", source)[:32] or "orchestrator"
        candidates = [p for p in files if p.name.startswith(safe + "-")]
    return sorted(candidates, key=_checkpoint_created_at, reverse=True)


def _mapping_list(value) -> bool:
    return isinstance(value, list) and all(isinstance(row, dict) for row in value)


def _valid_high_water(value) -> bool:
    if not isinstance(value, dict):
        return False
    return all(
        key in value
        and (value[key] is None or (isinstance(value[key], int)
                                    and not isinstance(value[key], bool)
                                    and value[key] >= 0))
        for key in ("max_run_id", "max_message_id", "max_feed_id")
    )


# Required top-level fields. Nested rows are mapping-only here; the
# sanitizer below projects them onto explicit field whitelists.
_REQUIRED_FIELDS: tuple[tuple[str, Any], ...] = (
    ("version", lambda v: isinstance(v, int)),
    ("schema", lambda v: v == SCHEMA_TAG),
    ("created_at", lambda v: isinstance(v, str)),
    ("source", lambda v: isinstance(v, str) and v),
    ("project", lambda v: isinstance(v, dict)),
    ("objective", lambda v: v is None or isinstance(v, str)),
    ("next_steps", lambda v: isinstance(v, list)
     and all(isinstance(x, str) for x in v)),
    ("work_item", lambda v: v is None or isinstance(v, str)),
    ("objective_source", lambda v: v is None or isinstance(v, dict)),
    ("high_water", _valid_high_water),
    ("active_runs", _mapping_list),
    ("source_inbox_snapshot", _mapping_list),
    ("feed_snapshot", _mapping_list),
    ("active_work_items", _mapping_list),
)


def _clean_text(value, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    return _truncate(_redact(value), limit)


def _clean_row(row: dict, fields: tuple[str, ...], *,
               body: bool = False) -> dict:
    """Whitelist and bound a row loaded from a checkpoint.

    Checkpoints are local 0600 files, but an explicit ``--checkpoint``
    path may be hand-edited. Re-projecting on load prevents unknown keys,
    oversized strings, or an older writer from bypassing current policy.
    """
    out = {}
    for key in fields:
        value = row.get(key)
        if isinstance(value, str):
            limit = TITLE_MAX_CHARS if key in {"title", "tags"} else BODY_PREVIEW_CHARS
            value = _clean_text(value, limit)
        out[key] = value
    if body:
        out["body_preview"] = _clean_text(row.get("body_preview"), BODY_PREVIEW_CHARS)
    return out


def _sanitize_loaded(data: dict) -> dict:
    project = data["project"]
    objective_source = data["objective_source"]
    return {
        "version": data["version"],
        "schema": SCHEMA_TAG,
        "created_at": _clean_text(data["created_at"], 64),
        "source": _clean_text(data["source"], TITLE_MAX_CHARS),
        "project": {
            "root": _clean_text(project.get("root"), 4096),
            "name": _clean_text(project.get("name"), TITLE_MAX_CHARS),
            "id": _clean_text(project.get("id"), 64),
        },
        "objective": _clean_text(data["objective"], OBJECTIVE_MAX_CHARS),
        "next_steps": [
            text for text in (
                _clean_text(value, NEXT_STEP_MAX_CHARS)
                for value in data["next_steps"][:NEXT_STEPS_MAX_COUNT]
            ) if text
        ],
        "work_item": _clean_text(data["work_item"], 64),
        "objective_source": ({
            "work_item": _clean_text(objective_source.get("work_item"), 64),
            "field": _clean_text(objective_source.get("field"), 64),
        } if isinstance(objective_source, dict) else None),
        "high_water": {
            key: data["high_water"][key]
            for key in ("max_run_id", "max_message_id", "max_feed_id")
        },
        "active_runs": [
            _clean_row(row, SAFE_RUN_FIELDS)
            for row in data["active_runs"][-ACTIVE_RUNS_LIMIT:]
        ],
        "source_inbox_snapshot": [
            _clean_row(row, SAFE_MESSAGE_FIELDS, body=True)
            for row in data["source_inbox_snapshot"][-INBOX_SNAPSHOT_LIMIT:]
        ],
        "feed_snapshot": [
            _clean_row(row, SAFE_FEED_FIELDS, body=True)
            for row in data["feed_snapshot"][-FEED_SNAPSHOT_LIMIT:]
        ],
        "active_work_items": [
            {
                "id": _clean_text(row.get("id"), 64),
                "status": _clean_text(row.get("status"), 32),
                "priority": _clean_text(row.get("priority"), 32),
                "assignee": _clean_text(row.get("assignee"), TITLE_MAX_CHARS),
                "title": _clean_text(row.get("title"), TITLE_MAX_CHARS),
            }
            for row in data["active_work_items"][-ACTIVE_WORK_ITEMS_LIMIT:]
        ],
    }


def load_checkpoint(path: Path) -> Checkpoint:
    """Validate and load a checkpoint. Every malformed-shape failure
    surfaces as :class:`CheckpointError` (CLI maps that to a clear
    SystemExit) instead of an AttributeError during rendering."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise CheckpointError(f"checkpoint not found: {path}") from exc
    except OSError as exc:
        raise CheckpointError(f"cannot read checkpoint {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise CheckpointError(f"checkpoint is not valid JSON: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise CheckpointError(f"checkpoint root must be a JSON object: {path}")
    schema = data.get("schema")
    if schema != SCHEMA_TAG:
        raise UnsupportedCheckpointError(
            f"checkpoint at {path} has schema {schema!r}; "
            f"this build understands {SCHEMA_TAG!r}"
        )
    for name, check in _REQUIRED_FIELDS:
        if name not in data:
            raise CheckpointError(
                f"checkpoint at {path} missing required field {name!r}"
            )
        try:
            ok = check(data[name])
        except Exception as exc:
            raise CheckpointError(
                f"checkpoint at {path} field {name!r} failed shape check: {exc}"
            ) from exc
        if not ok:
            raise CheckpointError(
                f"checkpoint at {path} field {name!r} has wrong shape"
            )
    # Defense in depth: re-sanitize and re-bound the full payload. The
    # file may come from an older build or an explicit hand-edited path.
    sanitized = _sanitize_loaded(data)
    return Checkpoint(path=path, data=sanitized)


def latest_checkpoint(root: Path, source: str | None = None) -> Checkpoint | None:
    files = list_checkpoints(root, source=source)
    return load_checkpoint(files[0]) if files else None


class CheckpointError(Exception):
    """Generic checkpoint failure."""


class UnsupportedCheckpointError(CheckpointError):
    """Schema mismatch — the checkpoint is from a newer/older build."""


# ---------------------------------------------------------------------------
# Takeover — read-only combination of checkpoint + live DB state
# ---------------------------------------------------------------------------


def _new_messages_for_source(con: sqlite3.Connection, source: str,
                             max_message_id: int | None,
                             limit: int) -> list[dict]:
    """Post-watermark messages scoped to the SOURCE recipient.

    The recipient filter is non-negotiable: we must never surface
    unrelated worker / other-orchestrator mail. When no high-water mark
    existed at checkpoint time we fall back to the most-recent N
    messages addressed to the source (none → empty list)."""
    if max_message_id is None:
        rows = _newest_n(
            con, "messages WHERE recipient=?", (source,), limit,
        )
    else:
        rows = _newest_n(
            con,
            "messages WHERE id > ? AND recipient=?",
            (max_message_id, source), limit,
        )
    return [m for m in (_safe_message(rr) for rr in rows) if m]


def _collect_takeover_state(root: Path, checkpoint: Checkpoint) -> dict:
    """Pure read: gather current DB state combined with checkpoint intent.

    Uses :func:`db.connect_readonly` so no schema migration or source-row
    write can run. SQLite may maintain WAL/SHM reader bookkeeping when
    the database is already in WAL mode; logical state remains unchanged.
    """
    hw = checkpoint.high_water
    source = checkpoint.source
    con = db.connect_readonly(root)
    try:
        active_runs = _active_runs(con, ACTIVE_RUNS_LIMIT)
        new_runs = [
            r for r in (_safe_row(rr, SAFE_RUN_FIELDS, redact_text=True)
                        for rr in _rows_since(con, "runs",
                                              hw.get("max_run_id"),
                                              NEW_RUNS_LIMIT)) if r
        ]
        new_messages = _new_messages_for_source(
            con, source, hw.get("max_message_id"), NEW_MESSAGES_LIMIT,
        )
        new_feed = [
            f for f in (_safe_feed(rr)
                        for rr in _rows_since(con, "feed",
                                              hw.get("max_feed_id"),
                                              NEW_FEED_LIMIT)) if f
        ]
    finally:
        con.close()
    return {
        "active_runs": active_runs,
        "new_runs": new_runs,
        "saved_messages": list(checkpoint.data.get("source_inbox_snapshot") or []),
        "new_messages": new_messages,
        "saved_feed": list(checkpoint.data.get("feed_snapshot") or []),
        "new_feed": new_feed,
    }


def render_takeover_brief(root: Path, checkpoint: Checkpoint, *,
                          target: str) -> str:
    """Render a markdown cold-start brief from ``checkpoint`` + live state."""
    state = _collect_takeover_state(root, checkpoint)
    proj = checkpoint.data.get("project") or {}
    parts = [
        f"# Orchestra takeover — {proj.get('name') or root.name}\n",
        f"Resumed from **{checkpoint.source or '(unknown)'}** at "
        f"{checkpoint.data.get('created_at') or '(no timestamp)'} "
        f"by **{target}** on `{proj.get('root') or root}`.\n",
        "Combines the saved intent below with CURRENT DB state — "
        "anything in *Since checkpoint* happened after the source wrote "
        "the file; everything in *Saved by source* is the durable slice "
        "the source committed before going down.\n",
        "## Objective\n",
        (checkpoint.objective or "(no objective supplied — see Active work items)\n"),
    ]
    if checkpoint.work_item:
        parts.append(f"_Anchored on work item **{checkpoint.work_item}**._\n")
    if checkpoint.data.get("objective_source"):
        src = checkpoint.data["objective_source"]
        field = src.get("field") if isinstance(src, dict) else None
        if field:
            parts.append(f"_Objective derived from ``work show {checkpoint.work_item} --json`` field ``{field}``._\n")
    ns = checkpoint.next_steps
    parts.append("## Next steps the source left for you\n")
    parts.append("\n".join(f"- {s}" for s in ns) + "\n" if ns else "(none recorded)\n")
    parts.append("## Active runs (current state)\n")
    parts.append(_format_run_table(state["active_runs"]) or "(none)\n")
    parts.append("## Runs started after the checkpoint\n")
    parts.append(_format_run_table(state["new_runs"]) or "(none)\n")
    parts.append("## Messages to the source (saved snapshot)\n")
    parts.append(_format_message_list(state["saved_messages"]) or "(none)\n")
    parts.append("## Messages received by the source after the checkpoint\n")
    parts.append(_format_message_list(state["new_messages"]) or "(none)\n")
    parts.append("## Findings saved by the source\n")
    parts.append(_format_feed_list(state["saved_feed"]) or "(none)\n")
    parts.append("## Findings logged after the checkpoint\n")
    parts.append(_format_feed_list(state["new_feed"]) or "(none)\n")
    items = _work_active_items(root)
    parts.append("## Active work items (live)\n")
    if items:
        parts.append("\n".join(
            f"- **{it['id']}** [{it['status']}, {it['priority']}] "
            f"(assigned: {it['assignee'] or '-'}) — {it['title']}"
            for it in items
        ) + "\n")
    else:
        parts.append("(none in_progress/review, or `work` CLI not on PATH)\n")
    parts.append(
        "## Coordination protocol\n\n"
        "Run `orchestra roster` first, then `orchestra status`. "
        f"Read your inbox with `orchestra inbox {target} --unread --mark-read`. "
        f"Dispatch via `orchestra dispatch --to <agent> --as {target} \"<mission>\"`. "
        f"When you stop, write your own checkpoint with `orchestra checkpoint --as "
        f"{target} --objective \"...\" --next \"...\"` and finish with "
        "`orchestra send <requester> \"HANDOFF run ...\"`.\n\n"
        "## Sensitive fields intentionally excluded\n\n"
        "Checkpoints and briefs never contain provider session ids, "
        "process PIDs, worker transcript paths, brief paths, runner argv, "
        "or environment variables. Every free-text field (objective, "
        "next steps, titles, tags, bodies) is redacted for credential-"
        "shaped patterns BEFORE it lands on disk AND re-sanitized on "
        "render as defense in depth.\n"
    )
    return "\n".join(parts)


# --- markdown formatters ---------------------------------------------------


def _format_run_table(runs: list[dict]) -> str:
    if not runs:
        return ""
    lines = ["| id | slug | agent | status | work | started | title |",
             "|---|---|---|---|---|---|---|"]
    for r in runs:
        title_cell = _truncate(str(r.get("title") or ""), 60) or "-"
        # The string has already been redacted on the way into the row;
        # re-redact here as defense in depth.
        title_cell = _redact(title_cell) or "-"
        lines.append(
            f"| {r.get('id', '')} | {r.get('slug') or '-'} | {r.get('agent', '')} "
            f"| {r.get('status', '')} | {r.get('work_item') or '-'} "
            f"| {r.get('started_at') or '-'} | {title_cell} |"
        )
    return "\n".join(lines) + "\n"


def _format_message_list(msgs: list[dict]) -> str:
    if not msgs:
        return ""
    lines = []
    for m in msgs:
        meta = (f"[{m.get('id')}] {m.get('created_at') or '-'} "
                f"from {m.get('sender') or '?'} -> {m.get('recipient') or '?'}")
        if m.get("kind"):
            meta += f" (kind={m['kind']})"
        extras = " ".join(x for x in [
            f"work:{m['work_item']}" if m.get("work_item") else "",
            f"run:{m['run_id']}" if m.get("run_id") else "",
        ] if x)
        if extras:
            meta += " " + extras
        lines.append(meta)
        body = _redact(m.get("body_preview") or "") or ""
        if body:
            lines.append("  " + body.replace("\n", "\n  "))
    return "\n".join(lines) + "\n"


def _format_feed_list(entries: list[dict]) -> str:
    if not entries:
        return ""
    lines = []
    for f in entries:
        meta = (f"[{f.get('id')}] {f.get('created_at') or '-'} "
                f"{f.get('author') or '?'}")
        tags = _truncate(_redact(f.get("tags") or ""), TAGS_MAX_CHARS) or ""
        extras = " ".join(x for x in [
            f"work:{f['work_item']}" if f.get("work_item") else "",
            f"run:{f['run_id']}" if f.get("run_id") else "",
            f"[{tags}]" if tags else "",
        ] if x)
        if extras:
            meta += " " + extras
        lines.append(meta)
        body = _redact(f.get("body_preview") or "") or ""
        if body:
            lines.append("  " + body.replace("\n", "\n  "))
    return "\n".join(lines) + "\n"
