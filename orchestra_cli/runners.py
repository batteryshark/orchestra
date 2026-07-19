"""Backend command builders for opencode / codex / claude workers."""


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
        flags = ["--cd", workdir, "--sandbox", "workspace-write",
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
