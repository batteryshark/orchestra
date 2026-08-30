"""Runtime launch contracts.

Runs describe work. Profiles choose capacity. Runtimes are the replaceable
process/session boundary that actually speaks to an agent harness.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping

from orchestra import runners


BUILTIN_ADAPTERS = frozenset(("codex", "claude", "opencode", "reasonix"))
ADAPTERS = BUILTIN_ADAPTERS | {"exec", "acp"}
_PLACEHOLDERS = frozenset((
    "{workdir}", "{title}", "{prompt}", "{session_ref}", "{run_id}",
))
_PLACEHOLDER_RE = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")


class RuntimeError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class LaunchPlan:
    argv: tuple[str, ...]
    env: dict[str, str]
    stdin: str | None
    adapter: str


def _json_object(raw: Any, field: str) -> dict:
    if raw in (None, ""):
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{field} must be a JSON object") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{field} must be a JSON object")
    return value


def _json_argv(raw: Any) -> list[str]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise RuntimeError("runtime command must be a JSON argv array") from exc
    if not isinstance(raw, list) or not raw:
        raise RuntimeError("runtime command must be a non-empty argv array")
    if any(not isinstance(part, str) or not part for part in raw):
        raise RuntimeError("every runtime argv part must be a non-empty string")
    return list(raw)


def _expand(part: str, values: Mapping[str, str]) -> str:
    unknown = {token for token in _tokens(part) if token not in _PLACEHOLDERS}
    if unknown:
        raise RuntimeError("unknown runtime placeholder(s): " +
                           ", ".join(sorted(unknown)))
    for token, value in values.items():
        part = part.replace("{" + token + "}", value)
    return part


def _tokens(value: str) -> set[str]:
    return set(_PLACEHOLDER_RE.findall(value))


def launch_plan(runtime: Mapping[str, Any], profile: Mapping[str, Any], *,
                workdir: str, title: str, prompt: str, run_id: int,
                session_ref: str | None = None,
                inherited_env: Mapping[str, str] | None = None) -> LaunchPlan:
    """Build argv/env without invoking a shell or assuming one process/run."""
    adapter = str(runtime.get("adapter") or "").strip().lower()
    if adapter not in ADAPTERS:
        raise RuntimeError(
            f"unknown runtime adapter {adapter!r}; expected one of " +
            ", ".join(sorted(ADAPTERS)))
    config = _json_object(runtime.get("config_json", runtime.get("config")),
                          "runtime config")
    profile_config = _json_object(
        profile.get("config_json", profile.get("config")), "profile config")
    merged = {**profile_config, **{
        key: value for key, value in profile.items()
        if key not in ("config", "config_json") and value is not None
    }}

    env = dict(inherited_env if inherited_env is not None else os.environ)
    runtime_env = config.get("env", {})
    profile_env = _json_object(
        profile.get("env_json", merged.get("env", {})), "profile env")
    if not isinstance(runtime_env, dict) or not isinstance(profile_env, dict):
        raise RuntimeError("runtime and profile env must be objects")
    for source in (runtime_env, profile_env):
        for key, value in source.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise RuntimeError("runtime environment keys and values must be strings")
            env[key] = value

    if adapter in BUILTIN_ADAPTERS:
        builtin_profile = {**merged, "backend": adapter,
                           "name": merged.get("name", "profile")}
        argv = runners.build_cmd(
            builtin_profile, workdir=workdir, title=title, prompt=prompt,
            resume_ref=session_ref,
        )
        env = runners.apply_backend_env(builtin_profile, env)
        return LaunchPlan(tuple(argv), env, None, adapter)

    command = runtime.get("command_json", runtime.get("command"))
    argv = _json_argv(command)
    values = {
        "workdir": workdir,
        "title": title,
        "prompt": prompt,
        "session_ref": session_ref or "",
        "run_id": str(run_id),
    }
    expanded = tuple(_expand(part, values) for part in argv)
    prompt_mode = config.get("prompt", "stdin")
    if prompt_mode not in ("stdin", "argv"):
        raise RuntimeError("runtime config prompt must be 'stdin' or 'argv'")
    if prompt_mode == "argv" and not any("{prompt}" in part for part in argv):
        raise RuntimeError("argv prompt mode requires a {prompt} placeholder")
    stdin = prompt if prompt_mode == "stdin" else None
    return LaunchPlan(expanded, env, stdin, adapter)
