"""Managed profile edits: the write path into ``config.toml`` (DESIGN §5).

Profiles are managed, not hand-edited — the dashboard and ``dromond
profiles`` add, edit and remove them. Three things make that harder than a
dict write:

- **The file is the record, not the database.** The daemon reads config, so
  an edit that landed in SQLite would be invisible to the next run.
- **The file is hand-written and full of comments.** The stdlib has a TOML
  reader and no writer, so this module does *targeted textual surgery*: it
  rewrites only the key lines it was asked to change, inside only the
  ``[profiles.NAME]`` table it was asked to touch. Every other byte —
  comments, blank lines, key order, quoting — survives untouched, and the
  result is re-parsed and diffed against the intended change before it is
  written. A surgery that cannot be verified is refused, never guessed.
- **Write authority is split by cost** (DESIGN §5). An agent may retune a
  note or an effort *downwards*, and nothing else. Adding a provider/model
  entry commits spend — and so does raising an effort, W-0176, or moving the
  tier/priority a planner routes on, W-0181 — so an agent asking for any of
  those gets a Work decision filed for the human and *no* config write. The
  split is enforced here, at the one function both the HTTP route
  and the CLI call; *who is asking* is answered by ``auth`` from the
  credential, never from a header or an environment variable the caller
  chose.

Discovery feeds the model/effort pickers (``profiles.discover``); this
module re-checks the picked values server-side, because a picker is a
convenience and a validation is a guarantee.
"""
import json
import os
import re
import tempfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from dromond import config, paths, profiles, work_client
from dromond.proc import chmod

# The harnesses. The config KEY is still `backend`; "harness" is the word
# every human-facing string uses (W-0181).
BACKENDS = ("opencode", "codex", "claude", "reasonix")

# What an agent may retune on its own (DESIGN §5): the cheap knobs. Anything
# else — a model, a harness, a new profile, a delegation allowlist, and the
# tier/priority a planner routes on — commits spend or widens authority and
# goes to the human as a Work decision.
#
# ``effort`` is here with a direction (W-0176). An agent may LOWER a
# profile's effort freely, and lowering is the only move it can make alone:
# raising one is a spend commitment with no new model entry to catch it, and
# principle 5 says nothing can grant itself the thing it asks for — a worker
# moving its own profile from `low` to `ultra` is exactly that. A raise files
# a Work decision like a model change does. See ``raises_effort``.
AGENT_KEYS = frozenset({"note", "effort"})

# Cheapest first; the order every harness reports its own subset of. An
# effort this does not know cannot be compared, so it counts as a raise.
EFFORT_ORDER = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")


def raises_effort(before, after) -> bool:
    """True unless this is provably a step DOWN (or a clear).

    Provably is the point: an unranked value, or a profile with no effort to
    compare against, goes to the human rather than being guessed cheap.
    """
    if after is None:
        return False  # clearing an effort falls back to the model's default
    if before not in EFFORT_ORDER or after not in EFFORT_ORDER:
        return True
    return EFFORT_ORDER.index(after) > EFFORT_ORDER.index(before)


# Editable keys and their TOML type. Everything else in a profile table
# (extra_args, add_dirs, permission_mode…) stays hand-edited: it is rare,
# fiddly, and a UI for it would be a form nobody opens.
EDITABLE: dict[str, type] = {
    "backend": str, "model": str, "effort": str, "variant": str,
    "tier": int, "priority": int, "sandbox": str, "note": str,
    "timeout": int, "stall_timeout": int,
    "spawn_profiles": list,
}

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# A TOML table header line: [x], [x.y], [[x]]. Deliberately strict, so a
# value line that merely starts with '[' does not end a table.
_HEADER_RE = re.compile(r"^\[\[?[A-Za-z0-9_.\-\"' ]+\]\]?\s*(#.*)?$")


# --- TOML text surgery ------------------------------------------------------

def _fmt(value) -> str:
    """One TOML value. json.dumps escapes exactly what a TOML basic string
    escapes for the characters a profile can hold."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        # ensure_ascii=False: a TOML basic string holds unicode literally, and
        # a note full of — escapes is a config nobody wants to read.
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_fmt(v) for v in value) + "]"
    raise ValueError(f"cannot write {type(value).__name__} to TOML")


def _split_comment(part: str) -> tuple[str, str]:
    """('value', '   # trailing comment') — a '#' inside a string is not one.

    The gap before the comment comes back with it, so a column someone
    aligned by hand stays where they put it.
    """
    quote = None
    escaped = False
    for i, ch in enumerate(part):
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            value = part[:i]
            return value.rstrip(), value[len(value.rstrip()):] + part[i:].rstrip("\n")
    return part.rstrip(), ""


def _header_names(line: str) -> str:
    """A header line, normalized for comparison: no spaces, and a
    single-quoted key spelled the way the double-quoted one is."""
    return line.strip().replace(" ", "").replace("'", '"')


def _profile_header(name: str) -> set[str]:
    return {f"[profiles.{name}]", f'[profiles."{name}"]'}


def _span(lines: list[str], wanted: set[str]) -> tuple[int, int] | None:
    """(first, last+1) line indexes of the table with one of these headers."""
    for i, line in enumerate(lines):
        if _header_names(line) not in wanted:
            continue
        end = len(lines)
        for j in range(i + 1, len(lines)):
            if _HEADER_RE.match(lines[j].strip()):
                end = j
                break
        return i, end
    return None


def _key_line(lines: list[str], start: int, end: int, key: str) -> int | None:
    pattern = re.compile(rf"^\s*(?:{re.escape(key)}|\"{re.escape(key)}\")\s*=")
    for i in range(start, end):
        if pattern.match(lines[i]):
            return i
    return None


def render(text: str, name: str, changes: dict, delete: bool = False,
           header: str | None = None) -> str:
    """The edited file text. Only the named table's key lines move.

    ``header`` names a table other than ``[profiles.NAME]`` — the project
    tables ``set_enabled`` writes (W-0187) go through the same surgery, the
    same verification and the same atomic write.
    """
    lines = text.splitlines(keepends=True)
    wanted = {header} if header else _profile_header(name)
    span = _span(lines, wanted)

    if delete:
        if span is None:
            raise KeyError(name)
        start, end = span
        # Drop the blank line the table left behind, not the next table's
        # own leading comment block.
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
        return "".join(lines[:start] + lines[end:])

    if span is None:
        keys = [f"{k} = {_fmt(v)}\n" for k, v in changes.items() if v is not None]
        if not keys:
            return text  # clearing a key from a table that does not exist
        block = [f"\n{header or f'[profiles.{name}]'}\n"] + keys
        if text and not text.endswith("\n"):
            text += "\n"
        return text + "".join(block)

    start, end = span
    for key, value in changes.items():
        at = _key_line(lines, start, end, key)
        if value is None:
            if at is not None:
                del lines[at]
                end -= 1
            continue
        if at is None:
            insert = end
            while insert > start + 1 and not lines[insert - 1].strip():
                insert -= 1
            lines.insert(insert, f"{key} = {_fmt(value)}\n")
            end += 1
            continue
        indent, _, rest = lines[at].partition("=")
        keep = _split_comment(rest)[1]
        lines[at] = f"{indent.rstrip()} = {_fmt(value)}{keep}\n"
    return "".join(lines)


def _tables(text: str) -> dict:
    return tomllib.loads(text)


def _verify(before: str, after: str, name: str, expected: dict | None) -> None:
    """Refuse a surgery that did anything but the intended change.

    ponytail: a multi-line array (``spawn_profiles = [\\n "a",\\n]``) is the
    known ceiling — replacing its first line strands the rest and the parse
    below fails, so the edit is refused with the reason instead of writing a
    broken config. Collapse the array onto one line, or teach this a
    continuation scan when someone actually hits it.
    """
    try:
        new = _tables(after)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(
            f"the edit would not parse as TOML ({exc}); nothing was written. "
            "A profile key written across several lines is the usual cause — "
            "collapse it onto one line and retry.") from exc
    old = _tables(before)
    if new.get("profiles", {}).get(name) != expected:
        raise ValueError(f"the edit did not produce the intended [profiles.{name}]; "
                         "nothing was written")
    def strip(parsed: dict) -> dict:
        """Everything except the profile under edit. ``profiles`` is always
        present on both sides, so adding the very first one is not read as
        'another table changed'."""
        rest = dict(parsed)
        rest["profiles"] = {n: t for n, t in (rest.get("profiles") or {}).items()
                            if n != name}
        return rest

    if strip(old) != strip(new):
        raise ValueError("the edit would have changed another table; "
                         "nothing was written")


def _verify_enabled(before: str, after: str, project_id: str, expected) -> None:
    """Same contract as ``_verify``, for the project table (W-0187): the
    enabled set is exactly what was asked for, and nothing else moved."""
    try:
        new = _tables(after)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"the edit would not parse as TOML ({exc}); "
                         "nothing was written") from exc
    old = _tables(before)
    table = (new.get("project") or {}).get(project_id) or {}
    if table.get("enabled_profiles") != expected:
        raise ValueError(f'the edit did not produce the intended [project."'
                         f'{project_id}"] enabled_profiles; nothing was written')

    def strip(parsed: dict) -> dict:
        rest = dict(parsed)
        projects = dict(rest.get("project") or {})
        one = dict(projects.get(project_id) or {})
        one.pop("enabled_profiles", None)
        projects[project_id] = one
        rest["project"] = projects
        return rest

    if strip(old) != strip(new):
        raise ValueError("the edit would have changed another table; "
                         "nothing was written")


def set_enabled(project_id: str, names) -> dict:
    """Write ``[project."<id>"] enabled_profiles`` (W-0187).

    ``names`` is the list the project enables, or None to remove the key —
    which is what "every profile is enabled" means. It is deliberately NOT
    written as a list of every current profile name: that list goes stale the
    moment an eleventh profile is added, and silently disables it.

    Human-only, like every other config write reached from the dashboard's
    project view; the route is unlisted in ``auth.ROUTES``, which is what
    makes it so.
    """
    if names is not None:
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            return {"applied": False,
                    "error": "enabled_profiles must be a list of profile names"}
        names = [n.strip() for n in names if n.strip()]
        configured = set(config.load().get("profiles", {}))
        unknown = sorted(set(names) - configured)
        if unknown:
            return {"applied": False,
                    "error": f"not a configured profile: {', '.join(unknown)}; "
                             f"configured: {', '.join(sorted(configured)) or 'none'}"}
    path = config.ensure_global_config()
    text = path.read_text(encoding="utf-8")
    header = f'[project."{project_id}"]'
    try:
        after = render(text, project_id, {"enabled_profiles": names},
                       header=header)
        _verify_enabled(text, after, project_id, names)
    except ValueError as exc:
        return {"applied": False, "error": str(exc)}
    write_atomic(path, after)
    return {"applied": True, "project_id": project_id,
            "enabled_profiles": names}


def write_atomic(path: Path, text: str) -> None:
    """Temp + rename in the same directory, mode 0600 before it is visible:
    the file holds the HTTP shared secret, and a half-written config is a
    daemon that cannot launch anything."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".config-", suffix=".toml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# --- discovery for the pickers ----------------------------------------------

CLAUDE_NOTE = ("the claude harness publishes no model listing; the model is "
               "typed here and `--effort` is accepted on any of them")

_CACHE: dict = {}
OPTIONS_TTL = 300  # seconds; discovery shells out to three CLIs


def picker_options(found: dict) -> dict:
    """Discovery, reshaped for a model/effort picker.

    ``supports_effort`` False is OpenCode's whole story: it has no effort
    flag at all, so the control disables itself there rather than accepting
    a value the launch would silently drop.
    """
    out: dict[str, dict] = {}
    oc = found.get("opencode", {})
    out["opencode"] = {
        "supports_effort": False,
        "effort_note": "opencode has no --effort flag; it takes a `variant` instead",
        "free_model": False,
        "models": [{"id": f"{prov}/{m}", "efforts": []}
                   for prov in sorted(oc.get("data") or {})
                   for m in (oc.get("data") or {})[prov]],
        "error": oc.get("error"),
    }
    cx = found.get("codex", {})
    out["codex"] = {
        "supports_effort": True, "free_model": False,
        "models": [{"id": m["model"], "efforts": list(m["efforts"]),
                    "default_effort": m.get("default_effort")}
                   for m in (cx.get("data") or [])],
        "error": cx.get("error"),
    }
    rx = found.get("reasonix", {})
    out["reasonix"] = {
        "supports_effort": True, "free_model": False,
        "models": [{"id": f"{p['provider']}/{m}", "efforts": list(p["efforts"]),
                    "default_effort": p.get("default_effort")}
                   for p in (rx.get("data") or []) for m in p["models"]],
        "error": rx.get("error"),
    }
    out["claude"] = {
        "supports_effort": True,
        # ponytail: claude is the one backend discovery cannot enumerate, so
        # its model and effort stay typed. Swap in a real list the day
        # `claude` grows a listing command.
        "free_model": True, "free_effort": True, "models": [],
        "error": (found.get("claude") or {}).get("error") or CLAUDE_NOTE,
    }
    return out


def discovery_options(force: bool = False) -> dict:
    """Cached picker options. Discovery costs three subprocesses."""
    now = datetime.now(timezone.utc).timestamp()
    if force or not _CACHE or now - _CACHE.get("at", 0) > OPTIONS_TTL:
        _CACHE.update(at=now, options=picker_options(profiles.discover()))
    return _CACHE["options"]


def cached_options() -> dict | None:
    """What discovery already found, or None. Never shells out: a config
    write must not block for 20s per backend behind a missing CLI."""
    return _CACHE.get("options")


# --- validation -------------------------------------------------------------

TIER_ERROR = ("tier must be 1 (workhorse — well-defined bounded tasks), "
              "2 (generalist) or 3 (heavy — the frontier model)")
PRIORITY_ERROR = (f"priority must be {config.PRIORITY_MIN}-{config.PRIORITY_MAX}, "
                  "like a linux process nice value: LOWER is more preferred")


def _coerce(changes: dict) -> tuple[dict, str | None]:
    """Normalize a request body: '' clears a key, types are checked."""
    clean: dict = {}
    for key, raw in changes.items():
        if key in ("note_at", "name"):
            continue
        want = EDITABLE.get(key)
        if want is None:
            return {}, (f"'{key}' is not an editable profile key; editable: "
                        + ", ".join(sorted(EDITABLE)))
        if raw is None or (isinstance(raw, str) and not raw.strip()) or raw == []:
            clean[key] = None  # explicit clear
            continue
        if key == "tier":
            # A named tier ("cheap", "workhorse") is accepted and written as
            # its number, so an old config migrates the first time it is saved.
            clean[key] = config.tier_of(raw)
            if clean[key] is None:
                return {}, TIER_ERROR
        elif want is int:
            try:
                clean[key] = int(raw)
            except (TypeError, ValueError):
                return {}, f"{key} must be a whole number"
            if clean[key] < 0:
                return {}, f"{key} must not be negative"
        elif want is list:
            if not isinstance(raw, list) or not all(isinstance(v, str) for v in raw):
                return {}, f"{key} must be a list of profile names"
            clean[key] = [v.strip() for v in raw if v.strip()] or None
        else:
            if not isinstance(raw, str):
                return {}, f"{key} must be a string"
            clean[key] = raw.strip()
    return clean, None


def validate(name: str, merged: dict, changes: dict, others: set[str],
             options: dict | None) -> str | None:
    """One error line, or None. ``merged`` is the profile as it would be."""
    if not NAME_RE.match(name):
        return (f"'{name}' is not a usable profile name: letters, digits, "
                "dot, dash and underscore only")
    backend = merged.get("backend") or "opencode"
    if backend not in BACKENDS:
        return f"unknown harness '{backend}'; pick one of {', '.join(BACKENDS)}"
    opts = (options or {}).get(backend) or {}

    # Routing metadata (W-0181): a planner reads these, so a value outside the
    # range is refused here rather than silently ignored at routing time. The
    # merged value may be a legacy named tier from the file — that maps.
    if merged.get("tier") is not None and config.tier_of(merged["tier"]) is None:
        return TIER_ERROR
    if merged.get("priority") is not None:
        try:
            value = int(merged["priority"])
        except (TypeError, ValueError):
            return PRIORITY_ERROR
        if not config.PRIORITY_MIN <= value <= config.PRIORITY_MAX:
            return PRIORITY_ERROR

    if merged.get("effort") and opts.get("supports_effort") is False:
        return (f"the {backend} harness takes no reasoning effort — "
                + str(opts.get("effort_note") or "it has no --effort flag"))
    if not merged.get("model") and not opts.get("free_model", True):
        return (f"pick a model for {backend}: a profile with no model launches "
                "whatever the harness defaults to, which is the guessing "
                "discovery exists to end")
    known = {m["id"]: m for m in opts.get("models") or []}
    model = merged.get("model")
    if known and model and model not in known:
        return (f"'{model}' is not a model the {backend} harness reports; "
                f"discovery lists {len(known)} — run `dromond profiles discover`")
    effort = merged.get("effort")
    supported = (known.get(model) or {}).get("efforts") or []
    if effort and supported and effort not in supported:
        return (f"the {backend} harness's model '{model}' supports "
                f"{', '.join(supported)} — not '{effort}'")

    # DESIGN §5: a delegation allowlist may not name a profile that does not
    # exist. Rejected here rather than at spawn time, where the worker only
    # finds out mid-run.
    for target in merged.get("spawn_profiles") or []:
        if target != name and target not in others:
            return (f"spawn_profiles names '{target}', which is not a configured "
                    f"profile; configured: {', '.join(sorted(others)) or 'none'}")
    return None


# --- the one write path -----------------------------------------------------

def _file_profiles(text: str) -> dict:
    try:
        return _tables(text).get("profiles", {}) or {}
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{paths.global_config_path()} does not parse: {exc}") from exc


def _decide(cfg: dict, name: str, changes: dict, keys: set[str],
            client=None) -> dict:
    """An agent asked for a change that commits spend: file it, do not do it."""
    client = work_client.from_cfg(cfg) if client is None else client
    detail = "\n".join(
        [f"An agent asked Dromond to change profile '{name}':"]
        + [f"  {k} = {changes[k]!r}" if k in changes else f"  {k}"
           for k in sorted(keys)]
        + ["", "Adding or changing a provider/model entry commits spend, and "
           "so does raising a reasoning effort, so DESIGN §5 keeps it a human "
           "call. Apply it from the dashboard (profiles → edit) or with "
           "`dromond profiles set`."])
    result = {"applied": False, "authority": "agent", "profile": name,
              "needs": sorted(keys), "detail": detail}
    if client is None:
        result["error"] = ("this change needs a human, and [work] is not "
                           "enabled, so no decision could be filed")
        return result
    try:
        created = client.create_decision(
            title=f"Dromond profile '{name}': {', '.join(sorted(keys))}"[:300],
            detail=detail[:19000],
            options=["Apply it", "Decline"],
            # Work refuses an agent decision without a recommendationReason.
            recommendation_reason=("No lean: this change commits spend, which "
                                   "DESIGN §5 keeps a human call."))
    except work_client.WorkError as exc:
        result["error"] = f"work rejected the decision ({exc.code})"
        return result
    result["decision"] = (created or {}).get("id") if isinstance(created, dict) else None
    result["filed"] = created is not None
    return result


def save(name: str, changes: dict, *, authority: str = "human",
         delete: bool = False, options: dict | None = None,
         work=None) -> dict:
    """Add, edit or remove one profile in the config FILE.

    Returns ``{"applied": True, ...}``, or a dict carrying ``error``, or —
    for an agent asking for a spend-committing change — the filed decision.
    """
    path = config.ensure_global_config()
    text = path.read_text(encoding="utf-8")
    try:
        current = _file_profiles(text)
    except ValueError as exc:
        return {"applied": False, "error": str(exc)}
    existing = dict(current.get(name) or {})
    others = set(current) - {name}

    if delete:
        if name not in current:
            return {"applied": False, "error": f"no profile '{name}' to remove"}
        if authority == "agent":
            return _decide(config.load(), name, {}, {"remove the profile"}, work)
        orphaned = sorted(o for o in others
                          if name in (current[o].get("spawn_profiles") or []))
        if orphaned:
            return {"applied": False,
                    "error": f"'{name}' is in the spawn_profiles of "
                             f"{', '.join(orphaned)} — clear it there first"}
        try:
            after = render(text, name, {}, delete=True)
            _verify(text, after, name, None)
        except (KeyError, ValueError) as exc:
            return {"applied": False, "error": str(exc)}
        write_atomic(path, after)
        return {"applied": True, "profile": name, "removed": True}

    clean, error = _coerce(changes)
    if error:
        return {"applied": False, "error": error}
    if not clean:
        return {"applied": False, "error": "nothing to change"}
    if clean.get("note") is not None:
        clean["note_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    elif "note" in clean:
        clean["note_at"] = None

    touched = {k for k, v in clean.items()
               if k != "note_at" and existing.get(k) != v
               and not (v is None and k not in existing)}
    if not touched:
        return {"applied": True, "profile": name, "changed": [],
                "unchanged": True}
    if name not in current:
        touched.add("create the profile")
        if not clean.get("backend"):
            # A note or an effort alone must never conjure a profile: the
            # next dispatch would launch a harness nobody chose.
            return {"applied": False,
                    "error": f"no profile '{name}': a new one needs a harness "
                             "and a model, picked from what discovery reports"}
    if authority == "agent":
        needs = touched - AGENT_KEYS
        if "effort" in touched and raises_effort(existing.get("effort"),
                                                 clean.get("effort")):
            needs = needs | {"effort"}
        if needs:
            return _decide(config.load(), name, clean, needs, work)

    merged = {k: v for k, v in {**existing, **clean}.items() if v is not None}
    error = validate(name, merged, clean, others, options)
    if error:
        return {"applied": False, "error": error}

    expected = {k: v for k, v in merged.items()}
    try:
        after = render(text, name, clean)
        _verify(text, after, name, expected)
    except ValueError as exc:
        return {"applied": False, "error": str(exc)}
    write_atomic(path, after)
    config.forget_profile_note(name)  # the file is the record now, not the sidecar
    return {"applied": True, "profile": name, "authority": authority,
            "changed": sorted(touched), "created": name not in current}
