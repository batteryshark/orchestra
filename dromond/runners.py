"""Backend command builders and tolerant JSONL output parsing.

Carried from Orchestra [arch keep-2]: one ``build_cmd`` function per
backend, no adapter classes. Parsing is best-effort fallback/telemetry —
status flows from the transcript, never from asking the worker to report.
"""

import json
import sys

from dromond import paths

# Verified against the installed CLIs: `opencode run` has no --add-dir and
# no equivalent directory flag.
ADD_DIR_BACKENDS = ("claude", "codex", "reasonix")

_OPENCODE_DELEGATION_PERMISSIONS = (
    "task",
    "team_create",
    "team_spawn",
    "team_message",
    "team_broadcast",
    "team_tasks_list",
    "team_tasks_add",
    "team_tasks_complete",
    "team_claim",
    "team_results",
    "team_shutdown",
    "team_cleanup",
    "team_merge",
    "team_status",
    "team_view",
)


def apply_backend_env(profile: dict, env: dict[str, str]) -> dict[str, str]:
    """Apply per-process backend policy without changing the user's global config.

    Two OpenCode-only things ride on ``OPENCODE_CONFIG_CONTENT``:

    1. OpenCode's ``run --auto`` can leave a native task child blocked forever
       on its own permission ask. Supervised workers get explicit orchestration
       from Dromond instead, so deny native/plugin delegation to avoid that
       unobservable deadlock. ``opencode_native_subagents = true`` on a profile
       deliberately restores them.
    2. OpenCode has no shell hooks (DESIGN §6), so Dromond's JS plugin is
       delivered PER RUN here rather than installed into the user's own
       ``~/.config/opencode`` — the human's interactive sessions stay clean.
    """
    if profile.get("backend") != "opencode":
        return env
    plugin = paths.opencode_plugin_path()
    deny_delegation = not profile.get("opencode_native_subagents")
    if not deny_delegation and not plugin.exists():
        return env

    raw = env.get("OPENCODE_CONFIG_CONTENT", "")
    try:
        content = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise SystemExit("dromond: OPENCODE_CONFIG_CONTENT must be a JSON object") from exc
    if not isinstance(content, dict):
        raise SystemExit("dromond: OPENCODE_CONFIG_CONTENT must be a JSON object")
    permissions = content.get("permission", {})
    if not isinstance(permissions, dict):
        raise SystemExit("dromond: OPENCODE_CONFIG_CONTENT.permission must be a JSON object")

    content = dict(content)
    if deny_delegation:
        content["permission"] = {
            **permissions,
            **{name: "deny" for name in _OPENCODE_DELEGATION_PERMISSIONS},
        }
    # ponytail: referenced by absolute path, because OPENCODE_CONFIG_CONTENT is
    # inline JSON with no directory to resolve a relative path against. Only
    # added when the file is really there, so a run never dies on a missing
    # plugin; `dromond init` writes it. If a future OpenCode rejects absolute
    # plugin paths, drop the file into a temp OPENCODE_CONFIG_DIR per run.
    if plugin.exists():
        listed = content.get("plugin")
        listed = list(listed) if isinstance(listed, list) else []
        if str(plugin) not in listed:
            content["plugin"] = [*listed, str(plugin)]
    updated = dict(env)
    updated["OPENCODE_CONFIG_CONTENT"] = json.dumps(
        content, ensure_ascii=False, separators=(",", ":")
    )
    return updated


def build_cmd(profile: dict, *, workdir: str, title: str, prompt: str,
              resume_ref: str | None = None) -> list[str]:
    backend = profile["backend"]
    model = profile.get("model")
    extra = list(profile.get("extra_args", []))
    # DESIGN §12: declared read-only directories outside the worktree.
    # claude/codex/reasonix take --add-dir; `opencode run` has no such flag
    # and would die on an unknown one. ponytail: an OpenCode run simply does
    # not get them -- revisit when opencode grows a directory flag.
    declared = profile.get("add_dirs", [])
    add_dirs = [arg for d in declared for arg in ("--add-dir", d)]
    if declared and backend not in ADD_DIR_BACKENDS:
        add_dirs = []
        print(f"dromond: backend '{backend}' has no --add-dir; ignoring add_dirs "
              f"{declared} for profile {profile.get('name')}", file=sys.stderr)

    if backend == "opencode":
        cmd = ["opencode", "run", "--dir", workdir, "--format", "json", "--auto", "--thinking"]
        if resume_ref:
            cmd += ["--session", resume_ref]
        else:
            cmd += ["--title", title]
        if model:
            cmd += ["-m", model]
        if profile.get("variant"):
            cmd += ["--variant", profile["variant"]]
        return cmd + add_dirs + extra + [prompt]

    if backend == "codex":
        # The sandbox override must be this explicit flag: codex rejects a
        # repeated --sandbox from extra_args and silently ignores the
        # `-c sandbox_mode=...` form when this flag is present.
        flags = ["--cd", workdir,
                 "--sandbox", profile.get("sandbox", "workspace-write"),
                 "--skip-git-repo-check", "--json"]
        if model:
            flags += ["-m", model]
        if profile.get("effort"):
            flags += ["-c", f'model_reasoning_effort="{profile["effort"]}"']
        # Codex emits NO reasoning items at all unless a summary is asked for.
        # Measured on codex-cli 0.147.0, gpt-5.6-sol, same prompt and effort:
        # 0 reasoning events without this flag, 3-5 with it. The allowed values
        # are auto/concise/detailed/none (codex names them when given a bad
        # one). `show_raw_agent_reasoning=true` is also accepted but added
        # nothing to the --json stream — the raw chain of thought comes back
        # encrypted — so it is not passed.
        if not any("model_reasoning_summary" in arg for arg in extra):
            flags += ["-c", 'model_reasoning_summary="detailed"']
        # Slightly richer summaries than `detailed` alone (measured 48 vs 35
        # chars on the same prompt). Summaries are the ceiling, not our floor,
        # and this was settled from three directions rather than assumed:
        #   - 24,753 reasoning items in this machine's Codex history carry
        #     `encrypted_content` and never raw text;
        #   - codex's own `reasoning_text()` falls back to the summary
        #     whenever `content` is empty, which it always is
        #     (exec/src/event_processor_with_human_output.rs);
        #   - a live run with `show_raw_agent_reasoning=true` on the HUMAN
        #     output path still printed only a summary headline.
        # Worth knowing: exec's JSONL processor destructures
        # `ThreadItem::Reasoning { summary, .. }` and drops `content`
        # outright, so `--json` could not carry raw reasoning even if the API
        # sent it. Not our loss today; it would be if that ever changes.
        if not any("model_reasoning_summary_format" in arg for arg in extra):
            flags += ["-c", 'model_reasoning_summary_format="experimental"']
        flags += add_dirs + extra
        if resume_ref:
            # Shared flags belong to `codex exec`, not its `resume` subcommand.
            return ["codex", "exec", *flags, "resume", resume_ref, prompt]
        return ["codex", "exec", *flags, prompt]

    if backend == "claude":
        # Pass the prompt as the VALUE of -p: claude >= 2.1.x rejects a
        # trailing positional prompt when --print/stream-json are set.
        cmd = [
            "claude", "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--forward-subagent-text",
        ]
        # Newer Claude models default thinking.display to "omitted"; request
        # the readable summary unless the profile overrides it.
        if not any(arg == "--thinking-display" or arg.startswith("--thinking-display=")
                   for arg in extra):
            cmd += ["--thinking-display", "summarized"]
        if profile.get("effort") and not any(
            arg == "--effort" or arg.startswith("--effort=") for arg in extra
        ):
            cmd += ["--effort", str(profile["effort"])]
        if resume_ref:
            cmd += ["--resume", resume_ref]
        if model:
            cmd += ["--model", model]
        cmd += add_dirs
        if not extra:
            extra = ["--permission-mode", "acceptEdits",
                     "--allowedTools", "Bash Edit Write Read Glob Grep WebFetch"]
        return cmd + extra

    if backend == "reasonix":
        cmd = ["reasonix", "run", "--dir", workdir,
               "--output-format", "stream-json"]
        if resume_ref:
            cmd += ["--resume", resume_ref]
        if model:
            cmd += ["--model", model]
        if profile.get("effort"):
            cmd += ["--effort", str(profile["effort"])]
        # Reasonix's own `--max-steps`. NOT a profile field any more (W-0181:
        # "what is a step budget and why do we have it") — the editor neither
        # writes nor documents it. A hand-written key is still passed through,
        # so an existing config keeps behaving the way it did.
        if "max_steps" in profile:
            cmd += ["--max-steps", str(profile["max_steps"])]
        # A supervised run has nobody to answer a permission ask, so an
        # unset posture means every write is declined and the worker
        # correctly stops having done nothing.
        if not any(arg.startswith(("--permission-mode", "--yolo", "-y"))
                   for arg in extra):
            cmd += ["--permission-mode", profile.get("permission_mode", "auto")]
        return cmd + add_dirs + extra + [prompt]

    raise SystemExit(f"dromond: unknown backend '{backend}' for profile {profile['name']}")


# --- output parsing (tolerant, best-effort) --------------------------------

SESSION_KEYS = {"sessionID", "session_id", "sessionId", "thread_id", "threadId"}


def _dig(obj, keys: set[str]) -> list[str]:
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and isinstance(v, str) and v:
                out.append(v)
            else:
                out.extend(_dig(v, keys))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_dig(v, keys))
    return out


def parse_log(log_path: str, max_bytes: int | None = None) -> tuple[str | None, str | None]:
    """Return (session_ref, last_text) best-effort from a JSONL worker log.
    max_bytes limits the scan (cheap early session-ref sniffing)."""
    session, last_text = None, None
    try:
        with open(log_path, errors="replace") as f:
            content = f.read(max_bytes).splitlines() if max_bytes else f
            for line in content:
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if session is None:
                    refs = _dig(obj, SESSION_KEYS)
                    if refs:
                        session = refs[0]
                # claude-code result event
                if obj.get("type") == "result" and isinstance(obj.get("result"), str):
                    last_text = obj["result"]
                    continue
                texts = _dig(obj, {"text"})
                if texts:
                    last_text = texts[-1]
    except OSError:
        pass
    return session, last_text


# --- usage capture (DESIGN §11) ---------------------------------------------
# One reader per backend, each keyed to the event that carries that backend's
# OWN totals. Verified against real transcripts (see tests/test_runners.py
# fixtures). Anything else is unrecognized and yields null: the run row says
# "not captured" rather than a guessed mapping.

EMPTY_USAGE = {"tokens_in": None, "tokens_out": None, "tokens_total": None,
               "cost_usd": None, "usage_source": None}


def _num(value):
    """The value if it is a number, else None. A bool is not a token count
    and a string is not a number."""
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _sum(*values):
    """Sum of the numeric parts, or None when none of them was a number: an
    absent count is not a zero count (DESIGN §11 — null, never a wrong
    number). A reported zero IS a number and stays zero."""
    found = [v for v in (_num(v) for v in values) if v is not None]
    return sum(found) if found else None


def _usage_claude(obj):
    """Claude Code: the final ``result`` event. Assistant messages repeat
    usage per message (and partial-message events repeat it again), so
    summing those double counts. ``input_tokens`` EXCLUDES cache tokens."""
    if obj.get("type") != "result" or not isinstance(obj.get("usage"), dict):
        return None
    usage = obj["usage"]
    tin = _sum(usage.get("input_tokens"), usage.get("cache_read_input_tokens"),
               usage.get("cache_creation_input_tokens"))
    tout = _sum(usage.get("output_tokens"))
    if tin is None and tout is None:
        return None
    return tin, tout, _sum(tin, tout), _num(obj.get("total_cost_usd"))


def _usage_reasonix(obj):
    """Reasonix: the final ``result`` event. Its ``input_tokens`` already
    INCLUDES cache read + creation (verified: 94976 + 19719 == 114695), so
    adding them again would double the input. Per-turn ``kind: usage`` lines
    are ignored — the result event is the authoritative session total."""
    if obj.get("type") != "result" or not isinstance(obj.get("usage"), dict):
        return None
    usage = obj["usage"]
    tin, tout = _sum(usage.get("input_tokens")), _sum(usage.get("output_tokens"))
    if tin is None and tout is None:
        return None
    cost = _num(obj.get("total_cost_usd"))
    if cost is None and obj.get("currency") in (None, "USD", "usd"):
        cost = _num(obj.get("total_cost"))  # another currency stays null, not mislabelled
    return tin, tout, _sum(tin, tout), cost


def _usage_codex(obj):
    """Codex ``exec --json``: the ``turn.completed`` event. ``input_tokens``
    already includes ``cached_input_tokens``. Codex reports no cost at all,
    so cost stays null for this backend — a token count is not a price."""
    if obj.get("type") != "turn.completed" or not isinstance(obj.get("usage"), dict):
        return None
    usage = obj["usage"]
    tin, tout = _sum(usage.get("input_tokens")), _sum(usage.get("output_tokens"))
    if tin is None and tout is None:
        return None
    total = _num(usage.get("total_tokens"))
    return tin, tout, total if total is not None else _sum(tin, tout), None


def _usage_opencode(obj):
    """OpenCode: every ``step-finish`` part carries that step's tokens and
    cost, so the run total is their sum. ``tokens.total`` = input + output +
    cache.read (verified against a real transcript)."""
    part = obj.get("part") or {}
    tokens = part.get("tokens")
    if part.get("type") != "step-finish" or not isinstance(tokens, dict):
        return None
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    tin = _sum(tokens.get("input"), cache.get("read"), cache.get("write"))
    tout = _sum(tokens.get("output"))
    if tin is None and tout is None:
        return None
    total = _num(tokens.get("total"))
    return tin, tout, total if total is not None else _sum(tin, tout), _num(part.get("cost"))


USAGE_PARSERS = {"claude": _usage_claude, "codex": _usage_codex,
                 "opencode": _usage_opencode, "reasonix": _usage_reasonix}


def parse_usage(log_path: str, backend: str) -> dict:
    """Token/cost totals for a run, from its own worker log (DESIGN §11).

    Best-effort by contract: an unknown backend, an unreadable log, or a log
    with no recognizable usage event returns every value None. It never
    raises and never guesses a mapping — null is the documented degradation.
    """
    parser = USAGE_PARSERS.get(backend)
    if parser is None:
        return dict(EMPTY_USAGE)
    tin = tout = total = cost = None
    seen = False
    try:
        with open(log_path, errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                try:
                    found = parser(obj)
                except (AttributeError, TypeError, ValueError):
                    continue  # a familiar event with an unfamiliar interior
                if found is None:
                    continue
                seen = True
                line_in, line_out, line_total, line_cost = found
                tin, tout = _sum(tin, line_in), _sum(tout, line_out)
                total, cost = _sum(total, line_total), _sum(cost, line_cost)
    except OSError:
        return dict(EMPTY_USAGE)
    if not seen:
        return dict(EMPTY_USAGE)
    return {"tokens_in": _int(tin), "tokens_out": _int(tout), "tokens_total": _int(total),
            "cost_usd": round(cost, 6) if cost is not None else None,
            "usage_source": backend}


def _int(value) -> int | None:
    return None if value is None else int(value)


def _find_command(obj) -> str | None:
    """First ``command`` value at any depth, rendered as a string.

    Separate from _dig because a command is usually a list of argv parts,
    and _dig only collects strings.
    """
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "command":
                if isinstance(value, list):
                    return " ".join(str(part) for part in value)
                if isinstance(value, str) and value:
                    return value
            found = _find_command(value)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_command(value)
            if found:
                return found
    return None


def parse_progress(log_path: str) -> str | None:
    """One line describing what a live run has been doing, from its log alone.

    Deterministic transcript reading, never a question put to the worker --
    a status report costs a model turn, this costs a file read.
    """
    actions, last, said = 0, None, None
    try:
        with open(log_path, errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                command = _find_command(obj)
                if command:
                    actions += 1
                    last = command
                texts = _dig(obj, {"text"})
                if texts and isinstance(texts[-1], str):
                    said = texts[-1].strip().splitlines()[0] if texts[-1].strip() else said
    except OSError:
        return None
    if not actions and not said:
        return None
    parts = [f"{actions} tool call{'s' if actions != 1 else ''}"] if actions else []
    if last:
        parts.append(f"last: {last.strip()[:120]}")
    elif said:
        parts.append(f"last said: {said[:120]}")
    return "; ".join(parts)[:400]


# Every harness words it differently, and none of them says it in a structured
# event: Reasonix prints `error: no session matches "<ref>"`, Claude Code
# "No conversation found with session ID", Codex "session not found". A resume
# that hits any of these is not recoverable BY RESUMING — the conversation is
# gone — so the run starts fresh instead of failing the item (live run 27).
SESSION_GONE = ("no session matches", "no such session", "session not found",
                "no conversation found with session", "unknown session")


def session_missing(log_path: str) -> str | None:
    """The backend's own words for 'the session you asked me to resume is gone',
    or None. Read from the run's own output; no backend-specific parsing,
    because none of them puts this in the JSONL."""
    try:
        with open(log_path, errors="replace") as handle:
            for line in handle:
                lowered = line.lower()
                if any(phrase in lowered for phrase in SESSION_GONE):
                    return line.strip()[:300]
    except OSError:
        pass
    return None


def parse_failure(log_path: str) -> str | None:
    """Return the most useful error text from a worker log, or None.

    A failed run whose backend never emitted assistant text (revoked auth,
    exhausted quota, rejected flag) otherwise reports nothing a human can
    act on. Prefers structured error events, falls back to the last stderr
    ERROR line the backend printed.
    """
    error, stderr_line = None, None
    try:
        with open(log_path, errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                if line.startswith("{"):
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if obj.get("type") in ("error", "turn.failed"):
                        message = obj.get("message")
                        if not isinstance(message, str):
                            nested = obj.get("error")
                            message = nested.get("message") if isinstance(nested, dict) else None
                        if isinstance(message, str) and message.strip():
                            error = message.strip()
                elif " ERROR " in line:
                    stderr_line = line.split(" ERROR ", 1)[-1].strip()
    except OSError:
        return None
    return (error or stderr_line or None) and (error or stderr_line)[:1500]
