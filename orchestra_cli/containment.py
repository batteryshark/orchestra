"""Fail-closed launch policy for filesystem-contained Operator workers."""
from __future__ import annotations

from typing import Any, Mapping
from pathlib import Path

MODES = {"operator-write", "operator-read"}
_FORBIDDEN_CODEX_ARGS = {
    "--add-dir",
    "--sandbox",
    "--dangerously-bypass-approvals-and-sandbox",
    "--dangerously-bypass-hook-trust",
    "--config",
    "-c",
    "--profile",
    "-p",
}
_HARDENED_CODEX_ARGS = [
    "--ignore-user-config",
    "--ignore-rules",
    "--disable",
    "multi_agent",
    "--disable",
    "hooks",
    "-c",
    'approval_policy="never"',
    "-c",
    "sandbox_workspace_write.writable_roots=[]",
    "-c",
    "sandbox_workspace_write.network_access=false",
]


class ContainmentPolicyError(ValueError):
    pass


def apply_profile(agent: Mapping[str, Any], mode: str) -> dict[str, Any]:
    if mode not in MODES:
        raise ContainmentPolicyError(f"unknown Operator containment mode {mode!r}")
    if agent.get("backend") != "codex":
        raise ContainmentPolicyError(
            f"live Operator profile {agent.get('name')!r} uses "
            f"{agent.get('backend')!r}; only Codex has an enforceable configured "
            "filesystem sandbox in this release"
        )
    configured = agent.get("sandbox", "workspace-write")
    allowed = {"workspace-write"} if mode == "operator-write" else {
        "workspace-write", "read-only"
    }
    if configured not in allowed:
        raise ContainmentPolicyError(
            f"live Operator profile {agent.get('name')!r} requests unsafe "
            f"Codex sandbox {configured!r}"
        )
    extra = [str(value) for value in agent.get("extra_args", [])]
    if any(
        value in _FORBIDDEN_CODEX_ARGS
        or any(value.startswith(flag + "=") for flag in _FORBIDDEN_CODEX_ARGS)
        for value in extra
    ):
        raise ContainmentPolicyError(
            f"live Operator profile {agent.get('name')!r} contains a sandbox-"
            "broadening extra argument"
        )
    if agent.get("ensemble"):
        raise ContainmentPolicyError(
            "live Operator profiles cannot use an ensemble host"
        )
    return {
        **dict(agent),
        "sandbox": "read-only" if mode == "operator-read" else "workspace-write",
        "extra_args": [*extra, *_HARDENED_CODEX_ARGS],
    }


def additional_write_dirs(
    root: Path, workdir: Path, mode: str | None
) -> list[str]:
    """Return ordinary worker control-plane access, never for contained runs."""
    if mode is not None:
        if mode not in MODES:
            raise ContainmentPolicyError(
                f"unknown Operator containment mode {mode!r}"
            )
        return []
    return [str(root)] if workdir != root else []
