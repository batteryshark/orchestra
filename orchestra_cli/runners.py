"""Backend command builders and output normalization for worker CLIs."""

from datetime import UTC, datetime


def build_cmd(agent: dict, *, workdir: str, title: str, prompt: str,
              resume_ref: str | None = None, add_dirs: list[str] | None = None,
              attach: str | None = None) -> list[str]:
    backend = agent["backend"]
    model = agent.get("model")
    extra = list(agent.get("extra_args", []))

    if backend == "opencode":
        cmd = ["opencode", "run", "--dir", workdir, "--format", "json", "--auto", "--thinking"]
        if attach:
            cmd += ["--attach", attach]
        if resume_ref:
            cmd += ["--session", resume_ref]
        else:
            cmd += ["--title", title]
        if model:
            cmd += ["-m", model]
        if agent.get("variant"):
            cmd += ["--variant", agent["variant"]]
        return cmd + extra + [prompt]

    if backend == "codex":
        # An agent may override the sandbox with `sandbox = "..."` in its roster
        # entry. It cannot be done through extra_args: codex rejects a repeated
        # --sandbox ("cannot be used multiple times"), and the
        # `-c sandbox_mode=...` form is silently beaten by this explicit flag,
        # which looks like it worked and does not. Needed for work that must
        # reach a unix socket outside the workspace -- a Remill lift shells out
        # to Docker, and workspace-write denies the connection.
        flags = ["--cd", workdir,
                 "--sandbox", agent.get("sandbox", "workspace-write"),
                 "--skip-git-repo-check", "--json"]
        for d in add_dirs or []:
            flags += ["--add-dir", d]
        if model:
            flags += ["-m", model]
        if agent.get("effort"):
            flags += ["-c", f'model_reasoning_effort="{agent["effort"]}"']
        flags += extra
        if resume_ref:
            # `--cd`, `--sandbox`, and `--add-dir` belong to `codex exec`, not
            # its `resume` subcommand, so keep shared flags before the command.
            return ["codex", "exec", *flags, "resume", resume_ref, prompt]
        return ["codex", "exec", *flags, prompt]

    if backend == "claude":
        # Pass the prompt as the VALUE of -p, not as a trailing positional:
        # claude CLI >= 2.1.x rejects a trailing positional prompt when
        # --print/--output-format stream-json are set ("Input must be provided
        # either through stdin or as a prompt argument"). `-p <prompt>` works.
        cmd = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose"]
        if resume_ref:
            cmd += ["--resume", resume_ref]
        if model:
            cmd += ["--model", model]
        if not extra:
            extra = ["--permission-mode", "acceptEdits",
                     "--allowedTools", "Bash Edit Write Read Glob Grep WebFetch"]
        return cmd + extra

    raise SystemExit(f"orchestra: unknown backend '{backend}' for agent {agent['name']}")


# --- output parsing (tolerant; the worker protocol, not parsing, is the
# primary reporting channel — this is best-effort fallback/telemetry) -------

SESSION_KEYS = {"sessionID", "session_id", "sessionId", "thread_id", "threadId"}

_CLAUDE_RATE_LIMIT_LABELS = {
    "five_hour": "5-hour usage limit",
    "seven_day": "weekly usage limit",
    "monthly": "monthly usage limit",
}


def claude_rate_limit_text(event: dict) -> str | None:
    """Describe a rejected Claude quota event from its structured fields.

    Claude Code 2.1.220 can pair a ``five_hour`` rate-limit event with a
    synthetic "monthly spend limit" assistant message. The structured event
    is authoritative; consumers should use this text instead of that
    contradictory synthetic copy.
    """
    if event.get("type") != "rate_limit_event":
        return None
    info = event.get("rate_limit_info")
    if not isinstance(info, dict) or info.get("status") != "rejected":
        return None
    raw_type = info.get("rateLimitType")
    if not isinstance(raw_type, str) or not raw_type.strip():
        label = "usage limit"
    else:
        label = _CLAUDE_RATE_LIMIT_LABELS.get(
            raw_type,
            f"{raw_type.replace('_', '-')} usage limit",
        )
    text = f"Claude {label} reached"
    resets_at = info.get("resetsAt")
    if isinstance(resets_at, (int, float)) and resets_at > 0:
        try:
            reset = datetime.fromtimestamp(resets_at, UTC).isoformat()
        except (OverflowError, OSError, ValueError):
            reset = None
        if reset:
            text += f"; resets at {reset}"
    return text


def _is_claude_synthetic_rate_limit_error(event: dict) -> bool:
    return (
        event.get("error") == "rate_limit"
        or (
            event.get("type") == "result"
            and event.get("is_error") is True
            and event.get("terminal_reason") == "api_error"
            and event.get("api_error_status") == 429
        )
    )


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
    import json
    session, last_text = None, None
    claude_rate_limited = False
    try:
        with open(log_path, errors="replace") as f:
            if max_bytes:
                content = f.read(max_bytes).splitlines()
            else:
                content = f
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
                rate_limit_text = claude_rate_limit_text(obj)
                if rate_limit_text:
                    last_text = rate_limit_text
                    claude_rate_limited = True
                    continue
                if claude_rate_limited and _is_claude_synthetic_rate_limit_error(obj):
                    continue
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


def log_outcome(log_path: str) -> str | None:
    """Did the worker in this log finish cleanly? 'done', 'failed' or None.

    Used only to reconcile a run whose supervisor died before it could write
    the completion row: in that case the log is the sole surviving evidence of
    what the worker actually did. None means the log carries no terminal record
    at all, i.e. the run really was cut short rather than merely unreaped.
    """
    import json
    outcome = None
    try:
        with open(log_path, errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                kind = obj.get("type")
                # claude-code: a final result event, explicitly flagged.
                if kind == "result":
                    ok = obj.get("subtype") == "success" and not obj.get("is_error")
                    outcome = "done" if ok else "failed"
                # opencode: the last step carries its stop reason on `part`.
                elif kind == "step_finish":
                    part = obj.get("part")
                    reason = part.get("reason") if isinstance(part, dict) else None
                    if reason:
                        outcome = "done" if reason == "stop" else "failed"
    except OSError:
        return None
    return outcome
