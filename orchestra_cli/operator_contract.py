"""Versioned, bounded contracts for the Orchestra Operator.

The contract is the authority boundary between an owner and an autonomous
controller.  Keep this module deliberately boring: it accepts one strict JSON
shape, rejects ambiguous or secret-bearing input, and produces canonical bytes
whose SHA-256 digest is what an owner approves.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_TAG_V1 = "orchestra.operator-contract/v1"
SCHEMA_TAG = "orchestra.operator-contract/v2"
MAX_CONTRACT_BYTES = 256 * 1024
MAX_TEXT_CHARS = 16_384
MAX_LIST_ITEMS = 128

AUTHORITY_ACTIONS = (
    "read_evidence",
    "manage_work",
    "manage_workers",
    "reserve_capacity",
    "quarantine_profile",
    "recovery_council",
    "edit_isolated_branch",
    "run_verification",
    "merge_after_gates",
    "update_tracker",
    "reclaim_worktree",
    "broaden_scope",
    "change_roster_policy",
    "add_architecture_surface",
    "exceed_change_budget",
    "change_acceptance",
    "publish_external",
    "rewrite_history",
    "delete_unique_work",
    "disable_gate",
    "consensus_overrides_authority",
    "speculative_work",
)

DEFAULT_AUTHORITY = {
    "read_evidence": "auto",
    "manage_work": "auto",
    "manage_workers": "auto",
    "reserve_capacity": "auto",
    "quarantine_profile": "auto",
    "recovery_council": "auto",
    "edit_isolated_branch": "auto",
    "run_verification": "auto",
    "merge_after_gates": "auto",
    "update_tracker": "auto",
    "reclaim_worktree": "auto",
    "broaden_scope": "ask",
    "change_roster_policy": "ask",
    "add_architecture_surface": "ask",
    "exceed_change_budget": "ask",
    "change_acceptance": "ask",
    "publish_external": "ask",
    "rewrite_history": "deny",
    "delete_unique_work": "deny",
    "disable_gate": "deny",
    "consensus_overrides_authority": "deny",
    "speculative_work": "deny",
}

_ALWAYS_DENIED = {
    "rewrite_history",
    "delete_unique_work",
    "disable_gate",
    "consensus_overrides_authority",
    "speculative_work",
}
_SECRET_KEYS = {
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "password",
    "secret",
    "authorization",
    "credential",
    "credentials",
}
_GOAL_ID = re.compile(r"^[A-Z][A-Z0-9_-]{0,31}$")
_PROJECT_ID = re.compile(r"^[0-9a-f]{16}$")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_SHELL_EXECUTABLES = {
    "bash", "cmd", "dash", "env", "fish", "powershell", "pwsh", "sh", "zsh",
}


class ContractError(ValueError):
    """A contract could not be parsed or did not satisfy the v1 schema."""


@dataclass(frozen=True)
class ValidatedContract:
    data: dict[str, Any]
    canonical_json: str
    sha256: str

    @property
    def canonical_bytes(self) -> bytes:
        return self.canonical_json.encode("utf-8")


class _DuplicateKey(ValueError):
    pass


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise _DuplicateKey(f"duplicate key {key!r}")
        out[key] = value
    return out


def parse_contract(text: str, *, source: str = "<memory>") -> ValidatedContract:
    try:
        size = len(text.encode("utf-8"))
    except UnicodeError as exc:
        raise ContractError(f"{source}: contract is not valid UTF-8 text") from exc
    if size > MAX_CONTRACT_BYTES:
        raise ContractError(
            f"{source}: contract is {size} bytes; limit is {MAX_CONTRACT_BYTES}"
        )

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite number {value}")

    try:
        data = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=reject_constant,
        )
    except (json.JSONDecodeError, _DuplicateKey, ValueError) as exc:
        raise ContractError(f"{source}: invalid contract JSON: {exc}") from exc
    return validate_contract(data, source=source)


def load_contract(path: Path) -> ValidatedContract:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ContractError(f"cannot read contract {path}: {exc}") from exc
    if len(raw) > MAX_CONTRACT_BYTES:
        raise ContractError(
            f"{path}: contract is {len(raw)} bytes; limit is {MAX_CONTRACT_BYTES}"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractError(f"{path}: contract is not valid UTF-8") from exc
    return parse_contract(text, source=str(path))


def validate_contract(data: Any, *, source: str = "<memory>") -> ValidatedContract:
    validator = _Validator()
    validator.contract(data)
    if validator.errors:
        details = "\n".join(f"  - {message}" for message in validator.errors[:24])
        omitted = len(validator.errors) - 24
        if omitted > 0:
            details += f"\n  - … and {omitted} more"
        raise ContractError(f"{source}: invalid Operator contract:\n{details}")

    try:
        canonical = json.dumps(
            data,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:  # defensive; validation should catch it
        raise ContractError(f"{source}: contract cannot be canonicalized: {exc}") from exc
    encoded = canonical.encode("utf-8")
    if len(encoded) > MAX_CONTRACT_BYTES:
        raise ContractError(
            f"{source}: canonical contract is {len(encoded)} bytes; "
            f"limit is {MAX_CONTRACT_BYTES}"
        )
    return ValidatedContract(
        data=data,
        canonical_json=canonical,
        sha256=hashlib.sha256(encoded).hexdigest(),
    )


def project_ids(contract: dict[str, Any]) -> tuple[str, ...]:
    """Return the already-validated registered-project references."""
    return tuple(contract["scope"]["projects"])


def is_v2(contract: dict[str, Any]) -> bool:
    return contract.get("schema") == SCHEMA_TAG


def project_scope(
    contract: dict[str, Any], project_id: str
) -> tuple[list[str], list[str]]:
    """Return repository-relative include/exclude rules for one project."""
    if not is_v2(contract):
        return list(contract["scope"]["include"]), list(contract["scope"]["exclude"])
    for rule in contract["scope"]["project_rules"]:
        if rule["project_id"] == project_id:
            return list(rule["include"]), list(rule["exclude"])
    raise ContractError(f"contract has no scope rule for project {project_id}")


def template(
    *,
    name: str,
    goal: str,
    project_ids: list[str],
    gates: list[str],
    target_branch: str = "main",
    integration_branch: str = "main",
    non_goals: list[str] | None = None,
) -> dict[str, Any]:
    """Build a complete conservative v2 contract for owner refinement."""
    if not project_ids:
        raise ContractError("an Operator contract needs at least one project id")
    primary_project = project_ids[0]
    return {
        "schema": SCHEMA_TAG,
        "name": name,
        "intent": {
            "summary": goal,
            "goals": [{
                "id": "G1",
                "outcome": goal,
                "priority": 1,
                "project_id": primary_project,
                "depends_on": [],
                "requires_review": False,
                "read_dependencies": [],
            }],
            "non_goals": list(non_goals or []),
        },
        "scope": {
            "projects": list(project_ids),
            "target_branch": target_branch,
            "integration_branch": integration_branch,
            "include": [],
            "exclude": [],
            "project_rules": [
                {"project_id": project_id, "include": [], "exclude": []}
                for project_id in project_ids
            ],
            "source_of_truth": [
                "repository instructions and declared verification evidence"
            ],
        },
        "authority": dict(DEFAULT_AUTHORITY),
        "quality": {
            "gates": list(gates),
            "verification": [],
            "independent_review": [
                "architecture, acceptance-methodology, and release changes"
            ],
            "change_discipline": {
                "default": "smallest_coherent",
                "refactoring": "necessary_only",
                "new_dependency": "ask",
                "new_service": "ask",
                "public_api": "ask",
                "schema_migration": "ask",
                "speculative_work": "deny",
                "unrelated_cleanup": "deny",
            },
            "change_budget": {
                "max_files": 20,
                "max_added_lines": 1200,
                "max_new_dependencies": 0,
                "max_public_api_changes": 0,
                "max_schema_migrations": 0,
            },
        },
        "planning": {
            "milestones": ["Establish baseline", "Implement bounded work", "Verify acceptance"],
            "derived_work": True,
            "implementation_latitude": "minimal",
            "max_refactor_files": 5,
        },
        "routing": {
            "minimum_tier": {
                "architecture": "heavy",
                "feature": "generalist",
                "mechanical": "workhorse",
            },
            "preferred_profiles": [],
            "forbidden_profiles": [],
            "reviewer_must_differ": True,
            "heavy_reserve_percent": 25,
            "downgrade_below_minimum": False,
            "recovery_council": {
                "members": ["fable", "gpt-5.6-sol"],
                "minimum_members": 2,
                "quorum": 2,
                "max_without_new_evidence": 1,
                "triggers": [
                    "repeated failure",
                    "verifier conflict",
                    "no credible next action",
                ],
            },
        },
        "resources": {
            "max_active_runs": 4,
            "max_worktrees": 5,
            "max_worktree_bytes": 40 * 1024**3,
            "min_free_disk_bytes": 100 * 1024**3,
            "max_attempts_per_item": 3,
            "max_wall_clock_seconds": None,
            "max_cost_usd": None,
            "retention": {
                "integrated": "remove_after_harvest",
                "failed_hours": 24,
                "dirty": "never_auto_remove",
            },
        },
        "escalation": {
            "triggers": [
                "scope or acceptance methodology must change",
                "unique state cannot be preserved automatically",
                "a declared resource ceiling cannot be recovered automatically",
                "an external action is ready",
            ],
            "permitted_fallbacks": [
                "retry within budget",
                "reroute to another contract-qualified profile",
                "convene one bounded recovery council",
            ],
            "retry_limit": 3,
            "decision_deadline_seconds": None,
        },
        "reporting": {
            "digest": "on_request",
            "notify_immediately": [
                "decision needed",
                "goal accepted",
                "hard resource pressure",
                "operation terminal",
            ],
            "progress_measures": [
                "accepted goals",
                "verified work items",
                "open decisions",
                "resource headroom",
            ],
        },
        "completion": {
            "conditions": list(gates),
            "stop_conditions": [
                "authority boundary prevents all remaining work",
                "resource budget is exhausted without a permitted recovery",
            ],
            "after_goals": "stop",
        },
    }


class _Validator:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def error(self, path: str, message: str) -> None:
        self.errors.append(f"{path}: {message}")

    def obj(
        self,
        value: Any,
        path: str,
        keys: set[str],
        *,
        allow_extra: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(value, dict):
            self.error(path, "must be an object")
            return {}
        string_keys = {key for key in value if isinstance(key, str)}
        for key in value:
            if not isinstance(key, str):
                self.error(path, f"field names must be strings, got {key!r}")
        missing = sorted(keys - string_keys)
        for key in missing:
            self.error(path, f"missing required field {key!r}")
        if not allow_extra:
            for key in sorted(string_keys - keys):
                self.error(path, f"unknown field {key!r}")
        return value

    def text(
        self,
        value: Any,
        path: str,
        *,
        max_chars: int = 1024,
        allow_empty: bool = False,
    ) -> str | None:
        if not isinstance(value, str):
            self.error(path, "must be a string")
            return None
        if not allow_empty and not value.strip():
            self.error(path, "must not be empty")
        if len(value) > min(max_chars, MAX_TEXT_CHARS):
            self.error(path, f"must be at most {min(max_chars, MAX_TEXT_CHARS)} characters")
        if _CONTROL_CHARS.search(value):
            self.error(path, "must not contain control characters")
        return value

    def integer(
        self,
        value: Any,
        path: str,
        *,
        minimum: int = 0,
        maximum: int = 10**12,
        nullable: bool = False,
    ) -> int | None:
        if nullable and value is None:
            return None
        if not isinstance(value, int) or isinstance(value, bool):
            self.error(path, "must be an integer")
            return None
        if not minimum <= value <= maximum:
            self.error(path, f"must be between {minimum} and {maximum}")
        return value

    def number(
        self,
        value: Any,
        path: str,
        *,
        nullable: bool = False,
    ) -> float | int | None:
        if nullable and value is None:
            return None
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            self.error(path, "must be a number")
            return None
        if value < 0 or value > 10**9:
            self.error(path, "must be between 0 and 1000000000")
        return value

    def boolean(self, value: Any, path: str) -> bool | None:
        if not isinstance(value, bool):
            self.error(path, "must be a boolean")
            return None
        return value

    def choice(self, value: Any, path: str, choices: set[str]) -> str | None:
        text = self.text(value, path, max_chars=64)
        if text is not None and text not in choices:
            self.error(path, f"must be one of {', '.join(sorted(choices))}")
        return text

    def text_list(
        self,
        value: Any,
        path: str,
        *,
        minimum: int = 0,
        max_chars: int = 2048,
    ) -> list[str]:
        if not isinstance(value, list):
            self.error(path, "must be an array")
            return []
        if not minimum <= len(value) <= MAX_LIST_ITEMS:
            self.error(
                path,
                f"must contain between {minimum} and {MAX_LIST_ITEMS} items",
            )
        out: list[str] = []
        for index, item in enumerate(value[: MAX_LIST_ITEMS + 1]):
            text = self.text(item, f"{path}[{index}]", max_chars=max_chars)
            if text is not None:
                out.append(text)
        if len(out) != len(set(out)):
            self.error(path, "must not contain duplicate values")
        return out

    def contract(self, value: Any) -> None:
        keys = {
            "schema",
            "name",
            "intent",
            "scope",
            "authority",
            "quality",
            "planning",
            "routing",
            "resources",
            "escalation",
            "reporting",
            "completion",
        }
        root = self.obj(value, "$", keys)
        self._secret_keys(value, "$")
        if not root:
            return
        schema = root.get("schema")
        if schema not in {SCHEMA_TAG_V1, SCHEMA_TAG}:
            self.error(
                "$.schema",
                f"must equal {SCHEMA_TAG_V1!r} or {SCHEMA_TAG!r}",
            )
        self.schema = schema
        self.text(root.get("name"), "$.name", max_chars=160)
        self._intent(root.get("intent"))
        self._scope(root.get("scope"))
        self._authority(root.get("authority"))
        self._quality(root.get("quality"))
        self._planning(root.get("planning"))
        self._routing(root.get("routing"))
        self._resources(root.get("resources"))
        self._escalation(root.get("escalation"))
        self._reporting(root.get("reporting"))
        self._completion(root.get("completion"))
        scope = root.get("scope")
        quality = root.get("quality")
        if isinstance(scope, dict) and isinstance(quality, dict):
            scoped = set(scope.get("projects") or [])
            verification = quality.get("verification")
            if isinstance(verification, list):
                for index, item in enumerate(verification):
                    if (
                        isinstance(item, dict)
                        and item.get("project_id") not in scoped
                    ):
                        self.error(
                            f"$.quality.verification[{index}].project_id",
                            "must reference a project in $.scope.projects",
                        )
        if schema == SCHEMA_TAG and isinstance(scope, dict):
            scoped = set(scope.get("projects") or [])
            goals = (root.get("intent") or {}).get("goals")
            if isinstance(goals, list):
                goal_ids = {
                    item.get("id") for item in goals if isinstance(item, dict)
                }
                for index, item in enumerate(goals):
                    if not isinstance(item, dict):
                        continue
                    if item.get("project_id") not in scoped:
                        self.error(
                            f"$.intent.goals[{index}].project_id",
                            "must reference a project in $.scope.projects",
                        )
                    for dep in item.get("depends_on") or []:
                        if dep not in goal_ids:
                            self.error(
                                f"$.intent.goals[{index}].depends_on",
                                f"references unknown goal {dep!r}",
                            )
                        if dep == item.get("id"):
                            self.error(
                                f"$.intent.goals[{index}].depends_on",
                                "a goal cannot depend on itself",
                            )
                    for project_id in item.get("read_dependencies") or []:
                        if project_id not in scoped:
                            self.error(
                                f"$.intent.goals[{index}].read_dependencies",
                                f"references unscoped project {project_id!r}",
                            )
                        if project_id == item.get("project_id"):
                            self.error(
                                f"$.intent.goals[{index}].read_dependencies",
                                "must not include the goal's writable project",
                            )
                dependency_graph = {
                    item["id"]: list(item.get("depends_on") or [])
                    for item in goals
                    if isinstance(item, dict) and isinstance(item.get("id"), str)
                }
                visiting: set[str] = set()
                visited: set[str] = set()

                def visit(goal_id: str) -> None:
                    if goal_id in visiting:
                        self.error(
                            "$.intent.goals",
                            f"dependency cycle includes {goal_id!r}",
                        )
                        return
                    if goal_id in visited:
                        return
                    visiting.add(goal_id)
                    for dependency in dependency_graph.get(goal_id, []):
                        if dependency in dependency_graph:
                            visit(dependency)
                    visiting.remove(goal_id)
                    visited.add(goal_id)

                for goal_id in dependency_graph:
                    visit(goal_id)

    def _secret_keys(self, value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = str(key).lower().replace("-", "_")
                if normalized in _SECRET_KEYS:
                    self.error(f"{path}.{key}", "credential-bearing fields are forbidden")
                self._secret_keys(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                self._secret_keys(child, f"{path}[{index}]")

    def _intent(self, value: Any) -> None:
        data = self.obj(value, "$.intent", {"summary", "goals", "non_goals"})
        self.text(data.get("summary"), "$.intent.summary", max_chars=4096)
        goals = data.get("goals")
        if not isinstance(goals, list):
            self.error("$.intent.goals", "must be an array")
            goals = []
        elif not 1 <= len(goals) <= MAX_LIST_ITEMS:
            self.error(
                "$.intent.goals",
                f"must contain between 1 and {MAX_LIST_ITEMS} items",
            )
        seen: set[str] = set()
        for index, raw in enumerate(goals[: MAX_LIST_ITEMS + 1]):
            path = f"$.intent.goals[{index}]"
            goal_keys = {"id", "outcome", "priority"}
            if getattr(self, "schema", None) == SCHEMA_TAG:
                goal_keys |= {
                    "project_id",
                    "depends_on",
                    "requires_review",
                    "read_dependencies",
                }
            goal = self.obj(raw, path, goal_keys)
            goal_id = self.text(goal.get("id"), f"{path}.id", max_chars=32)
            if goal_id is not None and not _GOAL_ID.fullmatch(goal_id):
                self.error(
                    f"{path}.id",
                    "must match ^[A-Z][A-Z0-9_-]{0,31}$",
                )
            if goal_id in seen:
                self.error(f"{path}.id", "must be unique")
            if goal_id:
                seen.add(goal_id)
            self.text(goal.get("outcome"), f"{path}.outcome", max_chars=4096)
            self.integer(
                goal.get("priority"),
                f"{path}.priority",
                minimum=1,
                maximum=100,
            )
            if getattr(self, "schema", None) == SCHEMA_TAG:
                project_id = self.text(
                    goal.get("project_id"), f"{path}.project_id", max_chars=64
                )
                if project_id is not None and not _PROJECT_ID.fullmatch(project_id):
                    self.error(
                        f"{path}.project_id",
                        "must be a 16-character lowercase hex registered project id",
                    )
                dependencies = self.text_list(
                    goal.get("depends_on"), f"{path}.depends_on", max_chars=32
                )
                for dep_index, dep in enumerate(dependencies):
                    if not _GOAL_ID.fullmatch(dep):
                        self.error(
                            f"{path}.depends_on[{dep_index}]",
                            "must be a valid goal id",
                        )
                self.boolean(
                    goal.get("requires_review"), f"{path}.requires_review"
                )
                read_dependencies = self.text_list(
                    goal.get("read_dependencies"),
                    f"{path}.read_dependencies",
                    max_chars=64,
                )
                for dep_index, project_id in enumerate(read_dependencies):
                    if not _PROJECT_ID.fullmatch(project_id):
                        self.error(
                            f"{path}.read_dependencies[{dep_index}]",
                            "must be a registered project id",
                        )
        self.text_list(data.get("non_goals"), "$.intent.non_goals", max_chars=2048)

    def _scope(self, value: Any) -> None:
        keys = {
            "projects",
            "target_branch",
            "integration_branch",
            "include",
            "exclude",
            "source_of_truth",
        }
        if getattr(self, "schema", None) == SCHEMA_TAG:
            keys.add("project_rules")
        data = self.obj(value, "$.scope", keys)
        projects = self.text_list(
            data.get("projects"),
            "$.scope.projects",
            minimum=1,
            max_chars=64,
        )
        for index, project_id in enumerate(projects):
            if not _PROJECT_ID.fullmatch(project_id):
                self.error(
                    f"$.scope.projects[{index}]",
                    "must be a 16-character lowercase hex registered project id",
                )
        self.text(data.get("target_branch"), "$.scope.target_branch", max_chars=255)
        self.text(
            data.get("integration_branch"),
            "$.scope.integration_branch",
            max_chars=255,
        )
        self.text_list(data.get("include"), "$.scope.include", max_chars=2048)
        self.text_list(data.get("exclude"), "$.scope.exclude", max_chars=2048)
        self.text_list(
            data.get("source_of_truth"),
            "$.scope.source_of_truth",
            minimum=1,
            max_chars=2048,
        )
        if getattr(self, "schema", None) == SCHEMA_TAG:
            rules = data.get("project_rules")
            if not isinstance(rules, list):
                self.error("$.scope.project_rules", "must be an array")
                rules = []
            elif len(rules) != len(projects):
                self.error(
                    "$.scope.project_rules",
                    "must contain exactly one rule for every scoped project",
                )
            seen_rules: set[str] = set()
            for index, raw in enumerate(rules[: MAX_LIST_ITEMS + 1]):
                path = f"$.scope.project_rules[{index}]"
                rule = self.obj(raw, path, {"project_id", "include", "exclude"})
                project_id = self.text(
                    rule.get("project_id"), f"{path}.project_id", max_chars=64
                )
                if project_id not in projects:
                    self.error(
                        f"{path}.project_id",
                        "must reference a project in $.scope.projects",
                    )
                if project_id in seen_rules:
                    self.error(f"{path}.project_id", "must be unique")
                if project_id:
                    seen_rules.add(project_id)
                self.text_list(rule.get("include"), f"{path}.include", max_chars=2048)
                self.text_list(rule.get("exclude"), f"{path}.exclude", max_chars=2048)

    def _authority(self, value: Any) -> None:
        data = self.obj(value, "$.authority", set(AUTHORITY_ACTIONS))
        for action in AUTHORITY_ACTIONS:
            mode = self.choice(
                data.get(action),
                f"$.authority.{action}",
                {"auto", "ask", "deny"},
            )
            if action in _ALWAYS_DENIED and mode not in {None, "deny"}:
                self.error(
                    f"$.authority.{action}",
                    "is a non-delegable invariant and must be 'deny'",
                )

    def _quality(self, value: Any) -> None:
        data = self.obj(
            value,
            "$.quality",
            {
                "gates",
                "verification",
                "independent_review",
                "change_discipline",
                "change_budget",
            },
        )
        self.text_list(data.get("gates"), "$.quality.gates", minimum=1)
        verification = data.get("verification")
        if not isinstance(verification, list):
            self.error("$.quality.verification", "must be an array")
            verification = []
        elif len(verification) > MAX_LIST_ITEMS:
            self.error(
                "$.quality.verification",
                f"must contain at most {MAX_LIST_ITEMS} items",
            )
        seen_verification: set[tuple[str, str]] = set()
        for index, raw in enumerate(verification[: MAX_LIST_ITEMS + 1]):
            path = f"$.quality.verification[{index}]"
            item = self.obj(
                raw,
                path,
                {
                    "name",
                    "project_id",
                    "argv",
                    "timeout_seconds",
                    "required",
                    "phase",
                },
            )
            name = self.text(item.get("name"), f"{path}.name", max_chars=160)
            project_id = self.text(
                item.get("project_id"),
                f"{path}.project_id",
                max_chars=64,
            )
            if project_id is not None and not _PROJECT_ID.fullmatch(project_id):
                self.error(
                    f"{path}.project_id",
                    "must be a 16-character lowercase hex registered project id",
                )
            argv = self.text_list(
                item.get("argv"),
                f"{path}.argv",
                minimum=1,
                max_chars=4096,
            )
            if argv and Path(argv[0]).is_absolute():
                self.error(
                    f"{path}.argv[0]",
                    "must be a PATH-resolved command, not an absolute executable",
                )
            if argv and Path(argv[0]).name.casefold() in _SHELL_EXECUTABLES:
                self.error(
                    f"{path}.argv[0]",
                    "must invoke a verifier directly; shell and env launchers are forbidden",
                )
            self.integer(
                item.get("timeout_seconds"),
                f"{path}.timeout_seconds",
                minimum=1,
                maximum=86_400,
            )
            self.boolean(item.get("required"), f"{path}.required")
            self.choice(
                item.get("phase"),
                f"{path}.phase",
                {"worktree", "integration", "both"},
            )
            key = (project_id or "", name or "")
            if key in seen_verification:
                self.error(path, "verification names must be unique per project")
            seen_verification.add(key)
        self.text_list(
            data.get("independent_review"),
            "$.quality.independent_review",
        )
        discipline = self.obj(
            data.get("change_discipline"),
            "$.quality.change_discipline",
            {
                "default",
                "refactoring",
                "new_dependency",
                "new_service",
                "public_api",
                "schema_migration",
                "speculative_work",
                "unrelated_cleanup",
            },
        )
        self.choice(
            discipline.get("default"),
            "$.quality.change_discipline.default",
            {"smallest_coherent", "contract_specific"},
        )
        self.choice(
            discipline.get("refactoring"),
            "$.quality.change_discipline.refactoring",
            {"necessary_only", "bounded", "allowed"},
        )
        for key in (
            "new_dependency",
            "new_service",
            "public_api",
            "schema_migration",
            "unrelated_cleanup",
        ):
            self.choice(
                discipline.get(key),
                f"$.quality.change_discipline.{key}",
                {"auto", "ask", "deny"},
            )
        speculative = self.choice(
            discipline.get("speculative_work"),
            "$.quality.change_discipline.speculative_work",
            {"auto", "ask", "deny"},
        )
        if speculative not in {None, "deny"}:
            self.error(
                "$.quality.change_discipline.speculative_work",
                "must be 'deny'",
            )

        budget = self.obj(
            data.get("change_budget"),
            "$.quality.change_budget",
            {
                "max_files",
                "max_added_lines",
                "max_new_dependencies",
                "max_public_api_changes",
                "max_schema_migrations",
            },
        )
        self.integer(
            budget.get("max_files"),
            "$.quality.change_budget.max_files",
            minimum=1,
            maximum=10_000,
        )
        self.integer(
            budget.get("max_added_lines"),
            "$.quality.change_budget.max_added_lines",
            minimum=1,
            maximum=10_000_000,
        )
        for key in (
            "max_new_dependencies",
            "max_public_api_changes",
            "max_schema_migrations",
        ):
            self.integer(
                budget.get(key),
                f"$.quality.change_budget.{key}",
                maximum=10_000,
            )

    def _planning(self, value: Any) -> None:
        data = self.obj(
            value,
            "$.planning",
            {
                "milestones",
                "derived_work",
                "implementation_latitude",
                "max_refactor_files",
            },
        )
        self.text_list(data.get("milestones"), "$.planning.milestones", minimum=1)
        self.boolean(data.get("derived_work"), "$.planning.derived_work")
        self.choice(
            data.get("implementation_latitude"),
            "$.planning.implementation_latitude",
            {"minimal", "bounded", "broad"},
        )
        self.integer(
            data.get("max_refactor_files"),
            "$.planning.max_refactor_files",
            maximum=10_000,
        )

    def _routing(self, value: Any) -> None:
        data = self.obj(
            value,
            "$.routing",
            {
                "minimum_tier",
                "preferred_profiles",
                "forbidden_profiles",
                "reviewer_must_differ",
                "heavy_reserve_percent",
                "downgrade_below_minimum",
                "recovery_council",
            },
        )
        tiers = self.obj(
            data.get("minimum_tier"),
            "$.routing.minimum_tier",
            set(),
            allow_extra=True,
        )
        if not tiers:
            self.error("$.routing.minimum_tier", "must contain at least one task class")
        if len(tiers) > 32:
            self.error("$.routing.minimum_tier", "must contain at most 32 task classes")
        for task_class, tier in list(tiers.items())[:33]:
            self.text(task_class, "$.routing.minimum_tier key", max_chars=64)
            self.choice(
                tier,
                f"$.routing.minimum_tier.{task_class}",
                {"workhorse", "generalist", "heavy"},
            )
        preferred = self.text_list(
            data.get("preferred_profiles"),
            "$.routing.preferred_profiles",
            max_chars=160,
        )
        forbidden = self.text_list(
            data.get("forbidden_profiles"),
            "$.routing.forbidden_profiles",
            max_chars=160,
        )
        overlap = sorted(set(preferred) & set(forbidden))
        if overlap:
            self.error(
                "$.routing",
                f"profiles cannot be both preferred and forbidden: {', '.join(overlap)}",
            )
        self.boolean(
            data.get("reviewer_must_differ"),
            "$.routing.reviewer_must_differ",
        )
        self.integer(
            data.get("heavy_reserve_percent"),
            "$.routing.heavy_reserve_percent",
            maximum=100,
        )
        downgrade = self.boolean(
            data.get("downgrade_below_minimum"),
            "$.routing.downgrade_below_minimum",
        )
        if downgrade is True:
            self.error(
                "$.routing.downgrade_below_minimum",
                "must be false; spare quota cannot override the quality floor",
            )
        council = self.obj(
            data.get("recovery_council"),
            "$.routing.recovery_council",
            {
                "members",
                "minimum_members",
                "quorum",
                "max_without_new_evidence",
                "triggers",
            },
        )
        members = self.text_list(
            council.get("members"),
            "$.routing.recovery_council.members",
            minimum=2,
            max_chars=160,
        )
        minimum_members = self.integer(
            council.get("minimum_members"),
            "$.routing.recovery_council.minimum_members",
            minimum=2,
            maximum=MAX_LIST_ITEMS,
        )
        quorum = self.integer(
            council.get("quorum"),
            "$.routing.recovery_council.quorum",
            minimum=2,
            maximum=MAX_LIST_ITEMS,
        )
        if minimum_members is not None and minimum_members > len(members):
            self.error(
                "$.routing.recovery_council.minimum_members",
                "cannot exceed the number of members",
            )
        if quorum is not None and quorum > len(members):
            self.error(
                "$.routing.recovery_council.quorum",
                "cannot exceed the number of members",
            )
        self.integer(
            council.get("max_without_new_evidence"),
            "$.routing.recovery_council.max_without_new_evidence",
            minimum=1,
            maximum=100,
        )
        self.text_list(
            council.get("triggers"),
            "$.routing.recovery_council.triggers",
            minimum=1,
        )

    def _resources(self, value: Any) -> None:
        data = self.obj(
            value,
            "$.resources",
            {
                "max_active_runs",
                "max_worktrees",
                "max_worktree_bytes",
                "min_free_disk_bytes",
                "max_attempts_per_item",
                "max_wall_clock_seconds",
                "max_cost_usd",
                "retention",
            },
        )
        self.integer(
            data.get("max_active_runs"),
            "$.resources.max_active_runs",
            minimum=1,
            maximum=10_000,
        )
        self.integer(
            data.get("max_worktrees"),
            "$.resources.max_worktrees",
            maximum=100_000,
        )
        self.integer(
            data.get("max_worktree_bytes"),
            "$.resources.max_worktree_bytes",
            maximum=10**15,
        )
        self.integer(
            data.get("min_free_disk_bytes"),
            "$.resources.min_free_disk_bytes",
            maximum=10**15,
        )
        self.integer(
            data.get("max_attempts_per_item"),
            "$.resources.max_attempts_per_item",
            minimum=1,
            maximum=10_000,
        )
        self.integer(
            data.get("max_wall_clock_seconds"),
            "$.resources.max_wall_clock_seconds",
            minimum=1,
            maximum=10**12,
            nullable=True,
        )
        self.number(
            data.get("max_cost_usd"),
            "$.resources.max_cost_usd",
            nullable=True,
        )
        retention = self.obj(
            data.get("retention"),
            "$.resources.retention",
            {"integrated", "failed_hours", "dirty"},
        )
        self.choice(
            retention.get("integrated"),
            "$.resources.retention.integrated",
            {"remove_after_harvest", "retain"},
        )
        self.integer(
            retention.get("failed_hours"),
            "$.resources.retention.failed_hours",
            maximum=24 * 365,
        )
        self.choice(
            retention.get("dirty"),
            "$.resources.retention.dirty",
            {"never_auto_remove", "ask"},
        )

    def _escalation(self, value: Any) -> None:
        data = self.obj(
            value,
            "$.escalation",
            {
                "triggers",
                "permitted_fallbacks",
                "retry_limit",
                "decision_deadline_seconds",
            },
        )
        self.text_list(data.get("triggers"), "$.escalation.triggers", minimum=1)
        self.text_list(
            data.get("permitted_fallbacks"),
            "$.escalation.permitted_fallbacks",
        )
        self.integer(
            data.get("retry_limit"),
            "$.escalation.retry_limit",
            maximum=10_000,
        )
        self.integer(
            data.get("decision_deadline_seconds"),
            "$.escalation.decision_deadline_seconds",
            minimum=1,
            maximum=10**12,
            nullable=True,
        )

    def _reporting(self, value: Any) -> None:
        data = self.obj(
            value,
            "$.reporting",
            {"digest", "notify_immediately", "progress_measures"},
        )
        self.choice(
            data.get("digest"),
            "$.reporting.digest",
            {"on_request", "hourly", "daily", "weekly"},
        )
        self.text_list(
            data.get("notify_immediately"),
            "$.reporting.notify_immediately",
        )
        self.text_list(
            data.get("progress_measures"),
            "$.reporting.progress_measures",
            minimum=1,
        )

    def _completion(self, value: Any) -> None:
        data = self.obj(
            value,
            "$.completion",
            {"conditions", "stop_conditions", "after_goals"},
        )
        self.text_list(
            data.get("conditions"),
            "$.completion.conditions",
            minimum=1,
        )
        self.text_list(
            data.get("stop_conditions"),
            "$.completion.stop_conditions",
        )
        self.choice(
            data.get("after_goals"),
            "$.completion.after_goals",
            {"stop", "maintain"},
        )
