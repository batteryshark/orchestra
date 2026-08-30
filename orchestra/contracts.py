"""Small, strict value objects at Orchestra's v2 boundary.

The daemon accepts neutral requests. These objects deliberately know nothing
about caller-specific workflow, routing, acceptance, or delivery policy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


RUN_STATES = (
    "queued",
    "starting",
    "running",
    "waiting",
    "completed",
    "failed",
    "timed_out",
    "stopped",
    "skipped",
)
TERMINAL_STATES = frozenset(RUN_STATES[-5:])
WAITING_KINDS = frozenset(("input", "children"))
DEPENDENCY_CONDITIONS = frozenset(("success", "terminal"))


class ContractError(ValueError):
    """A public v2 value is malformed."""


def _text(value: Any, field_name: str, *, required: bool = False,
          maximum: int | None = None) -> str | None:
    if value is None:
        if required:
            raise ContractError(f"{field_name} is required")
        return None
    if not isinstance(value, str):
        raise ContractError(f"{field_name} must be a string")
    value = value.strip()
    if required and not value:
        raise ContractError(f"{field_name} must not be empty")
    if maximum is not None and len(value) > maximum:
        raise ContractError(f"{field_name} must be at most {maximum} characters")
    return value or None


def _id(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise ContractError(f"{field_name} must be a positive run id")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{field_name} must be a positive run id") from exc
    if result < 1:
        raise ContractError(f"{field_name} must be a positive run id")
    return result


@dataclass(frozen=True, slots=True)
class Dependency:
    run_id: int
    condition: str = "success"

    @classmethod
    def from_value(cls, value: Any) -> "Dependency":
        if not isinstance(value, Mapping):
            raise ContractError("each after entry must be an object")
        unknown = set(value) - {"run_id", "condition"}
        if unknown:
            raise ContractError(
                "unknown after field(s): " + ", ".join(sorted(unknown)))
        condition = value.get("condition", "success")
        if condition not in DEPENDENCY_CONDITIONS:
            allowed = ", ".join(sorted(DEPENDENCY_CONDITIONS))
            raise ContractError(f"after.condition must be one of: {allowed}")
        return cls(_id(value.get("run_id"), "after.run_id"), condition)

    def as_dict(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "condition": self.condition}


@dataclass(frozen=True, slots=True)
class RunRequest:
    request_id: str
    profile: str
    context: str
    group: str = "general"
    title: str | None = None
    cwd: str | None = None
    ref: str | None = None
    after: tuple[Dependency, ...] = field(default_factory=tuple)
    requested_by: str = "operator"
    observer: str = "inherit"
    parent_run_id: int | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], *,
                     allow_parent: bool = True) -> "RunRequest":
        if not isinstance(value, Mapping):
            raise ContractError("request body must be an object")
        if not allow_parent and "parent_run_id" in value:
            raise ContractError(
                "parent_run_id is internal; delegate through /runs/{id}/children")
        accepted = {
            "request_id", "profile", "context", "group", "title", "cwd",
            "ref", "after", "requested_by",
            "observer", "parent_run_id",
        }
        unknown = set(value) - accepted
        if unknown:
            raise ContractError("unknown run request field(s): " +
                                ", ".join(sorted(unknown)))

        raw_after = value.get("after", [])
        if not isinstance(raw_after, list):
            raise ContractError("after must be an array")
        after = tuple(Dependency.from_value(item) for item in raw_after)
        if len({dep.run_id for dep in after}) != len(after):
            raise ContractError("after must not name the same run twice")

        observer = _text(value.get("observer", "inherit"), "observer",
                         required=True, maximum=128)
        parent = value.get("parent_run_id")
        return cls(
            request_id=_text(value.get("request_id"), "request_id", required=True,
                             maximum=200) or "",
            profile=_text(value.get("profile"), "profile", required=True,
                          maximum=128) or "",
            context=_text(value.get("context"), "context", required=True) or "",
            group=_text(value.get("group", "general"), "group", required=True,
                        maximum=128) or "general",
            title=_text(value.get("title"), "title", maximum=200),
            cwd=_text(value.get("cwd"), "cwd"),
            ref=_text(value.get("ref"), "ref", maximum=500),
            after=after,
            requested_by=_text(value.get("requested_by", "operator"),
                               "requested_by", required=True, maximum=128)
                         or "operator",
            observer=observer or "inherit",
            parent_run_id=None if parent is None else _id(parent, "parent_run_id"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "profile": self.profile,
            "context": self.context,
            "group": self.group,
            "title": self.title,
            "cwd": self.cwd,
            "ref": self.ref,
            "after": [dependency.as_dict() for dependency in self.after],
            "requested_by": self.requested_by,
            "observer": self.observer,
            "parent_run_id": self.parent_run_id,
        }


def child_tier_allowed(parent_tier: int, child_tier: int) -> bool:
    """Children may use the parent's capability tier or a cheaper one."""
    return parent_tier in (1, 2, 3) and child_tier in (1, 2, 3) \
        and child_tier <= parent_tier
