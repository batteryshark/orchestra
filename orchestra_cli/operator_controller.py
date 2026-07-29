"""Lease-held reconciliation loop for durable autonomous operations."""
from __future__ import annotations

import hashlib
import json
import os
import signal
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestra_cli import (
    availability,
    config,
    db,
    operator_broker,
    operator_replay,
    operator_roster,
    operator_runtime,
    operator_store,
)
from orchestra_cli.usage import default_service

POLL_SECONDS = 5
IDLE_POLL_SECONDS = 15
SHADOW_POLL_SECONDS = 30
CONTROLLER_SCHEMA = """
CREATE TABLE IF NOT EXISTS operator_work_reviews (
  work_item_id TEXT PRIMARY KEY REFERENCES operator_work_items(id),
  profile_name TEXT NOT NULL,
  project_run_id INTEGER NOT NULL,
  state TEXT NOT NULL,
  created_at TEXT NOT NULL,
  finished_at TEXT
);
"""


class ControllerError(RuntimeError):
    pass


def tick(operation_id: str, *, path: Path | None = None) -> dict[str, Any]:
    holder = operator_runtime.controller_holder()
    lease = operator_runtime.acquire_lease(operation_id, holder=holder, path=path)
    try:
        return _reconcile_with_heartbeat(operation_id, lease=lease, path=path)
    finally:
        operator_runtime.release_lease(lease, path=path)


def run(operation_id: str, *, path: Path | None = None) -> None:
    holder = operator_runtime.controller_holder()
    lease = operator_runtime.acquire_lease(operation_id, holder=holder, path=path)
    stopping = False

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    previous_term = signal.signal(signal.SIGTERM, stop)
    previous_int = signal.signal(signal.SIGINT, stop)
    operator_runtime.set_controller_pid(operation_id, os.getpid(), path=path)
    failures = 0
    try:
        while not stopping:
            try:
                result = _reconcile_with_heartbeat(operation_id, lease=lease, path=path)
                failures = 0
                operator_runtime.record_controller_error(operation_id, None, path=path)
            except Exception as exc:
                failures += 1
                operator_runtime.record_controller_error(
                    operation_id, f"{type(exc).__name__}: {exc}", path=path
                )
                lease = operator_runtime.heartbeat_lease(lease, path=path)
                time.sleep(min(30, POLL_SECONDS * (2 ** min(failures - 1, 3))))
                continue
            if result["operation_state"] in operator_runtime.OPERATION_TERMINAL | {"paused"}:
                break
            lease = operator_runtime.heartbeat_lease(lease, path=path)
            delay = (
                SHADOW_POLL_SECONDS
                if result["mode"] == "shadow"
                else (POLL_SECONDS if result["events"] else IDLE_POLL_SECONDS)
            )
            time.sleep(delay)
    finally:
        operator_runtime.set_controller_pid(operation_id, None, path=path)
        try:
            operator_runtime.release_lease(lease, path=path)
        finally:
            signal.signal(signal.SIGTERM, previous_term)
            signal.signal(signal.SIGINT, previous_int)


def _reconcile_with_heartbeat(
    operation_id: str,
    *,
    lease: operator_runtime.Lease,
    path: Path | None,
) -> dict[str, Any]:
    stopped = threading.Event()
    errors: list[BaseException] = []

    def heartbeat() -> None:
        while not stopped.wait(operator_runtime.LEASE_SECONDS / 3):
            try:
                operator_runtime.heartbeat_lease(lease, path=path)
            except BaseException as exc:
                errors.append(exc)
                return

    thread = threading.Thread(
        target=heartbeat,
        name=f"operator-lease-{operation_id}",
        daemon=True,
    )
    thread.start()
    try:
        result = _reconcile(operation_id, lease=lease, path=path)
    finally:
        stopped.set()
        thread.join(timeout=1)
    if errors:
        raise errors[0]
    return result


def _reconcile(
    operation_id: str,
    *,
    lease: operator_runtime.Lease,
    path: Path | None,
) -> dict[str, Any]:
    operation = operator_runtime.get_operation(operation_id, path=path)
    if operation["state"] in operator_runtime.OPERATION_TERMINAL | {"paused"}:
        return _summary(operation, [])
    contract = operator_store.get_contract(
        operation["operator_id"],
        version=operation["contract_version"],
        path=path,
    ).data
    try:
        operator_runtime.assert_live_operation_safe(operation)
    except operator_runtime.RuntimeError as exc:
        reason = str(exc)
        operator_runtime.create_decision(
            operation["id"],
            idempotency_key="containment:" + hashlib.sha256(
                reason.encode("utf-8")
            ).hexdigest()[:24],
            question="The live project containment precondition failed. How should the checkout be restored?",
            why_now=(
                "Operator stopped before dispatching or integrating more work because "
                "a shared integration checkout is dirty, on the wrong branch, or "
                "contains an external worktree link."
            ),
            options=[
                {"id": "owner-restores", "label": "Owner restores a clean checkout"},
                {"id": "stop", "label": "Stop the operation"},
            ],
            recommendation=(
                "Preserve any unique work, remove external links, and restore the "
                "approved clean integration branch before resuming."
            ),
            evidence={"error": reason},
            blocking_scope={"projects": operation["projects"]},
            path=path,
        )
        stopped = operator_runtime.set_operation_state(
            operation["id"],
            "needs_decision",
            reason=reason,
            path=path,
        )
        return _summary(stopped, [{"kind": "containment_precondition", "error": reason}])
    wall_limit = contract["resources"]["max_wall_clock_seconds"]
    if wall_limit is not None:
        activated = datetime.strptime(
            operation["activated_at"], "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - activated).total_seconds() >= wall_limit:
            stopped = operator_runtime.set_operation_state(
                operation_id,
                "failed",
                reason="approved max_wall_clock_seconds was exhausted",
                path=path,
            )
            return _summary(stopped, [{
                "kind": "resource_budget_exhausted",
                "resource": "wall_clock",
            }])
    _version, policy = operator_roster.latest_policy(path=path)
    snapshot = operator_replay.operation_snapshot(operation_id, path=path)
    try:
        usage_snapshot = default_service().snapshot()
    except Exception:
        usage_snapshot = {"providers": []}
    capacity = operator_roster.record_capacity_snapshot(
        policy,
        usage_snapshot if isinstance(usage_snapshot, dict) else {"providers": []},
        path=path,
    )
    resources = {
        project["project_key"]: operator_broker.resource_snapshot(Path(project["root"]))
        for project in operation["projects"]
        if Path(project["root"]).is_dir()
    }
    attempt = operator_runtime.begin_attempt(
        lease,
        {"events": snapshot, "resources": resources},
        path=path,
    )
    events: list[dict[str, Any]] = []
    try:
        if any(not project["available"] for project in snapshot["projects"]):
            operator_runtime.set_operation_state(
                operation_id,
                "waiting",
                reason="one or more project databases are unavailable",
                path=path,
            )
            events.append({"kind": "waiting", "reason": "project unavailable"})
        else:
            _apply_pending_cleanup_actions(operation, events, path=path)
            _reconcile_inflight(operation, contract, policy, events, path=path)
            _reconcile_councils(operation, contract, policy, events, path=path)
            refreshed = operator_runtime.get_operation(operation_id, path=path)
            if refreshed["state"] not in {"paused", "needs_decision"}:
                _dispatch_ready(
                    refreshed,
                    contract,
                    policy,
                    resources,
                    capacity,
                    events,
                    path=path,
                )
            refreshed = operator_runtime.get_operation(operation_id, path=path)
            if refreshed["goals"] and all(
                goal["state"] == "accepted" for goal in refreshed["goals"]
            ) and refreshed["open_decisions"] == 0 and refreshed["active_actions"] == 0:
                operator_runtime.set_operation_state(
                    operation_id,
                    "achieved",
                    reason="all contract goals have verified accepted work",
                    path=path,
                )
                events.append({"kind": "operation_achieved"})
            elif refreshed["state"] == "waiting":
                operator_runtime.set_operation_state(
                    operation_id, "active", reason="reconciliation resumed", path=path
                )
        final = operator_runtime.get_operation(operation_id, path=path)
        operator_runtime.finish_attempt(
            attempt,
            outcome="ok",
            summary=json.dumps(events, sort_keys=True)[:4096],
            path=path,
        )
        operator_replay.operation_snapshot(operation_id, advance_cursors=True, path=path)
        return _summary(final, events)
    except Exception as exc:
        operator_runtime.finish_attempt(
            attempt, outcome="error", summary=str(exc), path=path
        )
        raise


def _reconcile_inflight(
    operation: dict[str, Any],
    contract: dict[str, Any],
    policy: operator_roster.RosterPolicy,
    events: list[dict[str, Any]],
    *,
    path: Path | None,
) -> None:
    for work in operator_runtime.work_items(
        operation["id"],
        states=("dispatched", "running", "handed_off", "verifying", "integrating"),
        path=path,
    ):
        root = Path(work["root"])
        review = _review_row(work["id"], path=path, running_only=False)
        if review and review["state"] == "running":
            _finish_review(operation, work, review, contract, events, path=path)
            continue
        if review and review["state"] == "approved":
            _integrate(
                operation,
                work,
                contract,
                work["complexity"],
                work["verification"],
                events,
                path=path,
            )
            continue
        run = operator_broker.run_status(root, int(work["project_run_id"]))
        if not run or run["status"] not in db.RUN_TERMINAL:
            if work["state"] == "dispatched":
                operator_runtime.transition_work(work["id"], "running", path=path)
            continue
        operator_roster.release_run_reservations(
            int(work["project_run_id"]),
            state="consumed" if run["status"] == "done" else "released",
            path=path,
        )
        if run["status"] != "done":
            _retry_or_escalate(operation, work, contract, run["status"], events, path=path)
            continue
        _verify_and_integrate(operation, work, contract, policy, events, path=path)


def _verify_and_integrate(
    operation: dict[str, Any],
    work: dict[str, Any],
    contract: dict[str, Any],
    policy: operator_roster.RosterPolicy,
    events: list[dict[str, Any]],
    *,
    path: Path | None,
) -> None:
    if not work["branch"] or not work["base_head"]:
        _retry_or_escalate(operation, work, contract, "missing isolated branch", events, path=path)
        return
    root = Path(work["root"])
    complexity = operator_broker.measure_change(
        root, base_head=work["base_head"], branch=work["branch"]
    )
    work_scope = work["requirements"]["scope"]
    scope_violations = operator_broker.scope_violations(
        complexity.get("changed_paths") or [],
        include=work_scope["include"],
        exclude=work_scope["exclude"],
    )
    if scope_violations:
        scope_action = operator_runtime.propose_action(
            operation["id"],
            attempt_id=None,
            idempotency_key=f"scope:{work['id']}:{work['project_run_id']}",
            kind="accept out-of-scope change",
            authority_action="broaden_scope",
            target={"work_item_id": work["id"], "violations": scope_violations},
            evidence={"changed_paths": complexity.get("changed_paths") or []},
            work_item_id=work["id"],
            path=path,
        )
        events.append({
            "kind": "scope_gate",
            "state": scope_action["state"],
            "work": work["id"],
        })
        if scope_action["state"] != "authorized":
            return
    budget = work["change_budget"]
    exceeded = (
        complexity["files"] > budget["max_files"]
        or complexity["added_lines"] > budget["max_added_lines"]
        or complexity.get("dependency_surface_changes", 0)
        > budget["max_new_dependencies"]
        or complexity.get("public_api_changes", 0)
        > budget["max_public_api_changes"]
        or complexity.get("schema_migrations", 0)
        > budget["max_schema_migrations"]
    )
    if exceeded:
        action = operator_runtime.propose_action(
            operation["id"],
            attempt_id=None,
            idempotency_key=f"budget:{work['id']}:{work['project_run_id']}",
            kind="exceed bounded change budget",
            authority_action="exceed_change_budget",
            target={"work_item_id": work["id"], "complexity": complexity},
            evidence={"budget": budget},
            work_item_id=work["id"],
            path=path,
        )
        events.append({"kind": "budget_gate", "state": action["state"], "work": work["id"]})
        if action["state"] != "authorized":
            return
    commands = [
        row for row in contract["quality"]["verification"]
        if row["project_id"] == work["contract_project_id"]
    ]
    verification = operator_broker.verify(
        Path(run_workdir(root, int(work["project_run_id"]))),
        commands,
        phase="worktree",
    )
    if not verification["passed"]:
        operator_runtime.record_work_result(
            work["id"],
            state="failed_retryable",
            complexity=complexity,
            verification=verification,
            failure_fingerprint=_fingerprint(verification),
            path=path,
        )
        events.append({"kind": "verification_failed", "work": work["id"]})
        return
    if work["requires_review"]:
        _dispatch_review(operation, work, contract, policy, complexity, verification, path=path)
        events.append({"kind": "review_dispatched", "work": work["id"]})
        return
    _integrate(operation, work, contract, complexity, verification, events, path=path)


def _dispatch_review(
    operation: dict[str, Any],
    work: dict[str, Any],
    contract: dict[str, Any],
    policy: operator_roster.RosterPolicy,
    complexity: dict[str, Any],
    verification: dict[str, Any],
    *,
    path: Path | None,
) -> None:
    cfg = config.load(Path(work["root"]))
    report = availability.discover(cfg)
    reviewer_work = {
        **work,
        "task_class": "review",
        "minimum_tier": work["minimum_tier"],
        "actuation_mode": "review_only",
    }
    route = operator_roster.route(
        operation,
        reviewer_work,
        contract,
        policy,
        availability_report=report,
        capacity={},
        reviewer_profile=work["selected_profile"],
        path=path,
    )
    if not route.profile:
        raise ControllerError("no independent reviewer satisfies the approved roster")
    reservation = operator_roster.reserve(
        operation["id"],
        work["id"],
        profile_name=route.profile,
        policy=policy,
        minimum_tier=work["minimum_tier"],
        burn_band="small",
        path=path,
    )
    mission = (
        f"Independently review Operator work {work['id']} on branch {work['branch']} "
        f"against base {work['base_head']} and the contract gates. Do not edit. "
        "If and only if it is safe, minimal, and satisfies acceptance, end your "
        "summary with exactly OPERATOR_REVIEW: APPROVE; otherwise end with "
        "OPERATOR_REVIEW: REJECT and explain the blocking defect."
    )
    try:
        dispatched = operator_broker.dispatch(
            root=Path(work["root"]),
            profile_name=route.profile,
            mission=mission,
            work_item_id=work["id"],
            requester=f"operator:{operation['id']}",
            isolated=False,
        )
        operator_roster.bind_reservation(
            reservation.group, project_run_id=dispatched.run_id, path=path
        )
    except Exception:
        operator_roster.release_reservation(reservation.group, path=path)
        raise
    con = _controller_connect(path)
    try:
        con.execute(
            "INSERT OR REPLACE INTO operator_work_reviews("
            "work_item_id, profile_name, project_run_id, state, created_at"
            ") VALUES(?,?,?,?,?)",
            (
                work["id"], route.profile, dispatched.run_id, "running",
                operator_store.now(),
            ),
        )
        con.commit()
    finally:
        con.close()
    operator_runtime.record_work_result(
        work["id"],
        state="verifying",
        complexity=complexity,
        verification=verification,
        path=path,
    )


def _finish_review(
    operation: dict[str, Any],
    work: dict[str, Any],
    review: dict[str, Any],
    contract: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    path: Path | None,
) -> None:
    run = operator_broker.run_status(Path(work["root"]), int(review["project_run_id"]))
    if not run or run["status"] not in db.RUN_TERMINAL:
        return
    operator_roster.release_run_reservations(
        int(review["project_run_id"]),
        state="consumed" if run["status"] == "done" else "released",
        path=path,
    )
    approved = (
        run["status"] == "done"
        and "OPERATOR_REVIEW: APPROVE" in str(run.get("summary") or "")
    )
    con = _controller_connect(path)
    try:
        con.execute(
            "UPDATE operator_work_reviews SET state=?, finished_at=? WHERE work_item_id=?",
            ("approved" if approved else "rejected", operator_store.now(), work["id"]),
        )
        con.commit()
    finally:
        con.close()
    if not approved:
        operator_runtime.record_work_result(
            work["id"],
            state="needs_revision",
            failure_fingerprint=_fingerprint(run.get("summary") or run["status"]),
            path=path,
        )
        events.append({"kind": "review_rejected", "work": work["id"]})
        return
    _integrate(
        operation,
        work,
        contract,
        work["complexity"],
        work["verification"],
        events,
        path=path,
    )


def _integrate(
    operation: dict[str, Any],
    work: dict[str, Any],
    contract: dict[str, Any],
    complexity: dict[str, Any],
    verification: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    path: Path | None,
) -> None:
    action = operator_runtime.propose_action(
        operation["id"],
        attempt_id=None,
        idempotency_key=f"merge:{work['id']}:{work['project_run_id']}",
        kind="merge verified isolated branch",
        authority_action="merge_after_gates",
        target={"branch": work["branch"], "target": work["integration_branch"]},
        evidence={"complexity": complexity, "verification": verification},
        work_item_id=work["id"],
        path=path,
    )
    if operation["mode"] == "shadow" or action["state"] != "authorized":
        return
    claimed = operator_runtime.claim_action(action["id"], path=path)
    if not claimed:
        return
    project_lease = operator_runtime.acquire_project_lease(
        operation["id"],
        work["project_key"],
        holder=operator_runtime.controller_holder(),
        purpose=f"integrate {work['id']}",
        path=path,
    )
    project_heartbeat_stop = threading.Event()
    project_heartbeat_errors: list[BaseException] = []

    def heartbeat_project() -> None:
        while not project_heartbeat_stop.wait(60):
            try:
                operator_runtime.heartbeat_project_lease(
                    work["project_key"], project_lease, path=path
                )
            except BaseException as exc:
                project_heartbeat_errors.append(exc)
                return

    project_heartbeat = threading.Thread(
        target=heartbeat_project,
        name=f"operator-project-lease-{work['project_key']}",
        daemon=True,
    )
    project_heartbeat.start()
    merge_applied = False
    try:
        head = operator_broker.integrate(
            Path(work["root"]),
            branch=work["branch"],
            target_branch=work["integration_branch"],
        )
        merge_applied = True
        operator_runtime.update_project_expected_head(
            operation["id"], work["project_key"], head, path=path
        )
        operator_runtime.finish_action(
            action["id"], state="applied", result={"head": head}, path=path
        )
        integration_verification = operator_broker.verify(
            Path(work["root"]),
            [
                row for row in contract["quality"]["verification"]
                if row["project_id"] == work["contract_project_id"]
            ],
            phase="integration",
        )
        if not integration_verification["passed"]:
            for resource in operator_runtime.resource_leases(
                operation["id"], active_only=True, path=path
            ):
                if resource["project_run_id"] == int(work["project_run_id"]):
                    operator_runtime.release_resource_lease(
                        resource["id"],
                        state="retained",
                        unique_state=False,
                        details={
                            "integrated_head": head,
                            "retention": "post-integration verification failed",
                        },
                        path=path,
                    )
            operator_runtime.record_work_result(
                work["id"],
                state="needs_decision",
                complexity=complexity,
                verification={
                    "worktree": verification,
                    "integration": integration_verification,
                },
                failure_fingerprint=_fingerprint(integration_verification),
                path=path,
            )
            operator_runtime.create_decision(
                operation["id"],
                idempotency_key=f"post-integration:{work['id']}:{head}",
                question="Post-integration verification failed after the merge. Choose a fix-forward action.",
                why_now=(
                    "The merge is durable and history rewriting is forbidden; "
                    "acceptance is blocked."
                ),
                options=[
                    {"id": "fix-forward", "label": "Dispatch a bounded fix-forward"},
                    {"id": "stop", "label": "Stop the operation and inspect"},
                ],
                recommendation="Fix forward on a new isolated branch without weakening gates.",
                evidence={"head": head, "verification": integration_verification},
                blocking_scope={"work_item_id": work["id"]},
                work_item_id=work["id"],
                path=path,
            )
            operator_runtime.set_operation_state(
                operation["id"],
                "needs_decision",
                reason="post-integration verification failed",
                path=path,
            )
            events.append({"kind": "post_integration_failure", "work": work["id"]})
            return
        operator_runtime.record_work_result(
            work["id"],
            state="accepted",
            complexity=complexity,
            verification={
                "worktree": verification,
                "integration": integration_verification,
            },
            path=path,
        )
        resources = [
            resource for resource in operator_runtime.resource_leases(
                operation["id"], active_only=True, path=path
            )
            if resource["project_run_id"] == int(work["project_run_id"])
        ]
        if contract["resources"]["retention"]["integrated"] == "retain":
            for resource in resources:
                operator_runtime.release_resource_lease(
                    resource["id"],
                    state="retained",
                    unique_state=False,
                    details={"integrated_head": head, "retention": "contract"},
                    path=path,
                )
        else:
            cleanup = operator_runtime.propose_action(
                operation["id"],
                attempt_id=None,
                idempotency_key=f"reclaim:{work['id']}:{work['project_run_id']}",
                kind="reclaim integrated worktree",
                authority_action="reclaim_worktree",
                target={
                    "run_id": work["project_run_id"],
                    "branch": work["branch"],
                    "path": run_workdir(Path(work["root"]), int(work["project_run_id"])),
                    "root": work["root"],
                    "target_branch": work["integration_branch"],
                    "resource_ids": [resource["id"] for resource in resources],
                },
                evidence={"integrated_head": head, "unique_state": False},
                path=path,
            )
            if cleanup["state"] == "authorized":
                claimed_cleanup = operator_runtime.claim_action(cleanup["id"], path=path)
                if claimed_cleanup:
                    removed = operator_broker.reclaim_integrated(
                        Path(work["root"]),
                        run_id=int(work["project_run_id"]),
                        branch=work["branch"],
                        target_branch=work["integration_branch"],
                    )
                    operator_runtime.finish_action(
                        cleanup["id"],
                        state="applied",
                        result={"removed": removed},
                        path=path,
                    )
                    for resource in resources:
                        operator_runtime.release_resource_lease(
                            resource["id"],
                            state="released",
                            unique_state=False,
                            details={
                                "integrated_head": head,
                                "cleanup": "git worktree remove",
                            },
                            path=path,
                        )
            elif cleanup["state"] == "denied":
                for resource in resources:
                    operator_runtime.release_resource_lease(
                        resource["id"],
                        state="retained",
                        unique_state=False,
                        details={
                            "integrated_head": head,
                            "retention": "authority denied",
                        },
                        path=path,
                    )
        events.append({"kind": "work_accepted", "work": work["id"], "head": head})
    except Exception as exc:
        if not merge_applied:
            operator_runtime.finish_action(
                action["id"], state="failed", error=str(exc), path=path
            )
            operator_runtime.record_work_result(
                work["id"],
                state="needs_decision",
                complexity=complexity,
                verification=verification,
                failure_fingerprint=_fingerprint({"integration_error": str(exc)}),
                path=path,
            )
            operator_runtime.create_decision(
                operation["id"],
                idempotency_key=f"integration-precondition:{work['id']}:{work['project_run_id']}",
                question="The verified branch cannot be integrated safely. How should the integration checkout be restored?",
                why_now=(
                    "The merge was not applied. Repeating it cannot fix a dirty, "
                    "mis-owned, or wrong-branch integration checkout."
                ),
                options=[
                    {"id": "owner-restores", "label": "Owner restores the checkout"},
                    {"id": "stop", "label": "Stop the operation"},
                ],
                recommendation="Preserve unique work and restore a clean owned checkout.",
                evidence={"error": str(exc), "branch": work["branch"]},
                blocking_scope={
                    "work_item_id": work["id"],
                    "project_id": work["contract_project_id"],
                },
                work_item_id=work["id"],
                path=path,
            )
            operator_runtime.set_operation_state(
                operation["id"],
                "needs_decision",
                reason=f"integration precondition failed: {exc}",
                path=path,
            )
            events.append({
                "kind": "integration_precondition",
                "work": work["id"],
                "error": str(exc),
            })
            return
        raise
    finally:
        project_heartbeat_stop.set()
        project_heartbeat.join(timeout=1)
        operator_runtime.release_project_lease(
            work["project_key"], project_lease, path=path
        )
    if project_heartbeat_errors:
        raise project_heartbeat_errors[0]


def _dispatch_ready(
    operation: dict[str, Any],
    contract: dict[str, Any],
    policy: operator_roster.RosterPolicy,
    resources: dict[str, dict[str, Any]],
    capacity: dict[str, dict[str, Any]],
    events: list[dict[str, Any]],
    *,
    path: Path | None,
) -> None:
    active = operator_runtime.work_items(
        operation["id"],
        states=("dispatched", "running", "verifying", "integrating"),
        path=path,
    )
    slots = max(0, contract["resources"]["max_active_runs"] - len(active))
    active_profiles = _active_project_profiles(operation)
    ready_work = [
        work
        for work in operator_runtime.work_items(
        operation["id"],
        states=("ready", "failed_retryable", "needs_revision"),
        path=path,
        )
        if operator_runtime.dependencies_satisfied(work, path=path)
    ]
    for work in ready_work[:slots]:
        if work["attempt_count"] >= contract["resources"]["max_attempts_per_item"]:
            _escalate_stuck(operation, work, contract, events, path=path)
            continue
        usage = resources.get(work["project_key"], {})
        if (
            not usage.get("measurement_complete", False)
            or
            usage.get("worktree_count", 0) >= contract["resources"]["max_worktrees"]
            or (usage.get("worktree_bytes") or 0)
            >= contract["resources"]["max_worktree_bytes"]
            or usage.get("free_disk_bytes", 0) < contract["resources"]["min_free_disk_bytes"]
        ):
            operator_runtime.set_operation_state(
                operation["id"],
                "needs_decision",
                reason="worktree or disk resource ceiling reached",
                path=path,
            )
            operator_runtime.create_decision(
                operation["id"],
                idempotency_key=f"resource:{work['project_key']}",
                question="Resource ceiling blocks safe isolated work. What should be reclaimed or changed?",
                why_now="The approved disk/worktree budget cannot admit another run.",
                options=[{"id": "reclaim", "label": "Reclaim safe integrated worktrees"}],
                recommendation="Inspect dirty and unique work before changing the ceiling.",
                evidence=usage,
                blocking_scope={"project_key": work["project_key"]},
                work_item_id=work["id"],
                path=path,
            )
            return
        cfg = config.load(Path(work["root"]))
        report = availability.discover(cfg)
        route = operator_roster.route(
            operation,
            work,
            contract,
            policy,
            availability_report=report,
            capacity=capacity,
            active_by_profile=active_profiles,
            path=path,
        )
        action = operator_runtime.propose_action(
            operation["id"],
            attempt_id=None,
            idempotency_key=f"dispatch:{work['id']}:{work['attempt_count'] + 1}",
            kind="dispatch isolated implementation",
            authority_action="edit_isolated_branch",
            target={"work_item_id": work["id"], "profile": route.profile},
            evidence={"routing_decision": route.decision_id, "why": route.explanation},
            work_item_id=work["id"],
            path=path,
        )
        if operation["mode"] == "shadow":
            events.append({
                "kind": "shadow_dispatch",
                "work": work["id"],
                "profile": route.profile,
                "authority": action["state"],
            })
            continue
        if not route.profile:
            _escalate_stuck(operation, work, contract, events, path=path)
            continue
        if action["state"] != "authorized":
            continue
        reservation = operator_roster.reserve(
            operation["id"],
            work["id"],
            profile_name=route.profile,
            policy=policy,
            minimum_tier=work["minimum_tier"],
            burn_band="heavy" if work["minimum_tier"] == "heavy" else "normal",
            path=path,
        )
        claimed = operator_runtime.claim_action(action["id"], path=path)
        if not claimed:
            operator_roster.release_reservation(reservation.group, path=path)
            continue
        try:
            predecessor = {
                "run_id": work["project_run_id"],
                "branch": work["branch"],
                "base_head": work["base_head"],
            } if work["project_run_id"] and work["branch"] else None
            dispatched = operator_broker.dispatch(
                root=Path(work["root"]),
                profile_name=route.profile,
                mission=_mission(contract, work),
                work_item_id=work["id"],
                requester=f"operator:{operation['id']}",
                isolated=True,
                start_point=predecessor["branch"] if predecessor else None,
                comparison_base=predecessor["base_head"] if predecessor else None,
                read_inputs=[
                    {
                        "project_id": project_id,
                        "root": next(
                            project["root"]
                            for project in operation["projects"]
                            if project["project_id"] == project_id
                        ),
                        "commit": "HEAD",
                    }
                    for project_id in work["requirements"].get(
                        "read_dependencies", []
                    )
                ],
            )
            operator_runtime.bind_work_run(
                work["id"],
                profile=route.profile,
                run_id=dispatched.run_id,
                branch=dispatched.branch,
                base_head=dispatched.base_head,
                path=path,
            )
            operator_runtime.register_resource_lease(
                operation["id"],
                work_item_id=work["id"],
                project_key=work["project_key"],
                kind="git_worktree",
                resource_path=dispatched.workdir,
                project_run_id=dispatched.run_id,
                unique_state=True,
                details={
                    "branch": dispatched.branch,
                    "base_head": dispatched.base_head,
                },
                path=path,
            )
            for snapshot in dispatched.input_snapshots:
                source_project = next(
                    project
                    for project in operation["projects"]
                    if project["project_id"] == snapshot["project_id"]
                )
                operator_runtime.register_resource_lease(
                    operation["id"],
                    work_item_id=work["id"],
                    project_key=source_project["project_key"],
                    kind="read_input_snapshot",
                    resource_path=snapshot["path"],
                    project_run_id=dispatched.run_id,
                    unique_state=False,
                    measured_bytes=snapshot["bytes"],
                    details={
                        "commit": snapshot["commit"],
                        "sha256": snapshot["sha256"],
                    },
                    path=path,
                )
            if predecessor:
                _propose_predecessor_cleanup(
                    operation,
                    work,
                    predecessor,
                    dispatched,
                    events,
                    path=path,
                )
            operator_roster.bind_reservation(
                reservation.group, project_run_id=dispatched.run_id, path=path
            )
            operator_runtime.finish_action(
                action["id"],
                state="applied",
                result={"run_id": dispatched.run_id, "branch": dispatched.branch},
                path=path,
            )
            events.append({"kind": "dispatched", "work": work["id"], "run": dispatched.run_id})
        except Exception as exc:
            operator_roster.release_reservation(
                reservation.group, state="released", path=path
            )
            operator_runtime.finish_action(
                action["id"], state="failed", error=str(exc), path=path
            )
            operator_roster.record_health_signal(
                route.profile,
                signal="launch_failure",
                context={"error": str(exc), "work_item_id": work["id"]},
                path=path,
            )
            if isinstance(exc, operator_broker.ContainmentError):
                operator_runtime.record_work_result(
                    work["id"],
                    state="needs_decision",
                    failure_fingerprint=_fingerprint({"containment": str(exc)}),
                    path=path,
                )
                operator_runtime.create_decision(
                    operation["id"],
                    idempotency_key=(
                        f"dispatch-containment:{work['id']}:"
                        f"{work['attempt_count'] + 1}"
                    ),
                    question="The isolated work environment could not be created safely. How should its containment issue be resolved?",
                    why_now=(
                        "Operator refused to dispatch into an external link, "
                        "oversized snapshot, or otherwise uncontained path."
                    ),
                    options=[
                        {"id": "owner-restores", "label": "Owner restores containment"},
                        {"id": "stop", "label": "Stop the operation"},
                    ],
                    recommendation="Remove the bridge or narrow the declared input, then resume.",
                    evidence={"error": str(exc)},
                    blocking_scope={"work_item_id": work["id"]},
                    work_item_id=work["id"],
                    path=path,
                )
                operator_runtime.set_operation_state(
                    operation["id"],
                    "needs_decision",
                    reason=f"dispatch containment failed: {exc}",
                    path=path,
                )
                events.append({
                    "kind": "dispatch_containment",
                    "work": work["id"],
                    "error": str(exc),
                })
                return
            raise


def _apply_pending_cleanup_actions(
    operation: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    path: Path | None,
) -> None:
    for action in operator_runtime.pending_actions(operation["id"], path=path):
        if action["kind"] not in {
            "reclaim integrated worktree",
            "reclaim transferred predecessor worktree",
        }:
            continue
        target = action["target"]
        evidence = action["evidence"]
        if evidence.get("unique_state") is not False:
            raise ControllerError("cleanup action lacks proof that unique state was integrated")
        claimed = operator_runtime.claim_action(action["id"], path=path)
        if not claimed:
            continue
        try:
            if action["kind"] == "reclaim integrated worktree":
                removed = operator_broker.reclaim_integrated(
                    Path(target["root"]),
                    run_id=int(target["run_id"]),
                    branch=target["branch"],
                    target_branch=target["target_branch"],
                )
            else:
                removed = operator_broker.reclaim_transferred_worktree(
                    Path(target["root"]),
                    run_id=int(target["run_id"]),
                    branch=target["branch"],
                    successor_branch=target["successor_branch"],
                )
            operator_runtime.finish_action(
                action["id"], state="applied", result={"removed": removed}, path=path
            )
            for resource_id in target.get("resource_ids") or []:
                operator_runtime.release_resource_lease(
                    resource_id,
                    state="released",
                    unique_state=False,
                    details={
                        "integrated_head": evidence.get("integrated_head"),
                        "cleanup": "git worktree remove after owner approval",
                    },
                    path=path,
                )
            events.append({"kind": "worktree_reclaimed", "run": target["run_id"]})
        except Exception as exc:
            operator_runtime.finish_action(
                action["id"], state="failed", error=str(exc), path=path
            )
            raise


def _propose_predecessor_cleanup(
    operation: dict[str, Any],
    work: dict[str, Any],
    predecessor: dict[str, Any],
    dispatched: operator_broker.Dispatch,
    events: list[dict[str, Any]],
    *,
    path: Path | None,
) -> None:
    resources = [
        resource for resource in operator_runtime.resource_leases(
            operation["id"], active_only=True, path=path
        )
        if resource["project_run_id"] == int(predecessor["run_id"])
    ]
    action = operator_runtime.propose_action(
        operation["id"],
        attempt_id=None,
        idempotency_key=(
            f"reclaim-transfer:{work['id']}:{predecessor['run_id']}:{dispatched.run_id}"
        ),
        kind="reclaim transferred predecessor worktree",
        authority_action="reclaim_worktree",
        target={
            "root": work["root"],
            "run_id": predecessor["run_id"],
            "branch": predecessor["branch"],
            "successor_branch": dispatched.branch,
            "resource_ids": [resource["id"] for resource in resources],
        },
        evidence={
            "unique_state": False,
            "preserved_by_successor_branch": dispatched.branch,
        },
        path=path,
    )
    if action["state"] != "authorized":
        return
    claimed = operator_runtime.claim_action(action["id"], path=path)
    if not claimed:
        return
    try:
        removed = operator_broker.reclaim_transferred_worktree(
            Path(work["root"]),
            run_id=int(predecessor["run_id"]),
            branch=predecessor["branch"],
            successor_branch=dispatched.branch or "",
        )
        operator_runtime.finish_action(
            action["id"], state="applied", result={"removed": removed}, path=path
        )
        for resource in resources:
            operator_runtime.release_resource_lease(
                resource["id"],
                state="released",
                unique_state=False,
                details={
                    "cleanup": "git worktree remove",
                    "preserved_by_successor_branch": dispatched.branch,
                },
                path=path,
            )
        events.append({
            "kind": "predecessor_worktree_reclaimed",
            "run": predecessor["run_id"],
        })
    except Exception as exc:
        operator_runtime.finish_action(
            action["id"], state="failed", error=str(exc), path=path
        )
        raise


def _retry_or_escalate(
    operation: dict[str, Any],
    work: dict[str, Any],
    contract: dict[str, Any],
    failure: Any,
    events: list[dict[str, Any]],
    *,
    path: Path | None,
) -> None:
    fingerprint = _fingerprint(failure)
    state = (
        "failed_terminal"
        if work["attempt_count"] >= contract["resources"]["max_attempts_per_item"]
        else "failed_retryable"
    )
    operator_runtime.record_work_result(
        work["id"], state=state, failure_fingerprint=fingerprint, path=path
    )
    events.append({"kind": state, "work": work["id"], "failure": fingerprint})
    if state == "failed_terminal":
        _escalate_stuck(operation, work, contract, events, path=path)


def _escalate_stuck(
    operation: dict[str, Any],
    work: dict[str, Any],
    contract: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    path: Path | None,
) -> None:
    council = operator_roster.create_council(
        operation["id"],
        work["id"],
        failure_fingerprint=work["failure_fingerprint"] or "attempts_exhausted",
        evidence={
            "problem": (
                f"Work remains stuck after {work['attempt_count']} attempts: "
                f"{work['title']}"
            ),
            "failure_fingerprint": work["failure_fingerprint"],
            "requirements": work["requirements"],
        },
        contract=contract,
        policy=operator_roster.latest_policy(path=path)[1],
        path=path,
    )
    operator_runtime.set_operation_state(
        operation["id"], "needs_decision", reason=f"recovery council {council['id']}", path=path
    )
    operator_runtime.create_decision(
        operation["id"],
        idempotency_key=f"stuck:{work['id']}:{work['attempt_count']}",
        question=f"Work {work['id']} is stuck. Review the bounded recovery council.",
        why_now="Automatic attempts are exhausted; authority cannot be broadened by consensus.",
        options=[
            {"id": "revise", "label": "Approve a bounded revised action"},
            {"id": "stop", "label": "Stop this work item"},
        ],
        recommendation="Wait for independent council opinions before choosing.",
        evidence={"council_id": council["id"]},
        blocking_scope={"work_item_id": work["id"]},
        work_item_id=work["id"],
        path=path,
    )
    events.append({"kind": "recovery_council", "id": council["id"], "work": work["id"]})


def _reconcile_councils(
    operation: dict[str, Any],
    contract: dict[str, Any],
    policy: operator_roster.RosterPolicy,
    events: list[dict[str, Any]],
    *,
    path: Path | None,
) -> None:
    con = operator_roster.connect(path)
    try:
        ids = [
            row["id"]
            for row in con.execute(
                "SELECT id FROM recovery_councils "
                "WHERE operation_id=? AND state='collecting' ORDER BY created_at",
                (operation["id"],),
            )
        ]
    finally:
        con.close()
    work_by_id = {
        row["id"]: row
        for row in operator_runtime.work_items(operation["id"], path=path)
    }
    for council_id in ids:
        council = operator_roster.get_council(council_id, path=path)
        work = work_by_id[council["work_item_id"]]
        for member in council["members"]:
            if member["state"] == "pending":
                mission = (
                    "Independently diagnose this stuck Operator work from the supplied "
                    "evidence. Do not coordinate with other reviewers and do not edit. "
                    "Return one line beginning OPERATOR_COUNCIL: followed by a JSON object "
                    "with exactly diagnosis, evidence, hypotheses, action_key, next_action, "
                    "smallest_surface, deferred, confidence, risks. action_key must be a "
                    "stable lowercase identifier so independently identical proposals can "
                    f"form quorum.\n\nEvidence: {json.dumps(council['evidence'])}"
                )
                reservation = operator_roster.reserve(
                    operation["id"],
                    work["id"],
                    profile_name=member["profile_name"],
                    policy=policy,
                    minimum_tier="heavy",
                    burn_band="small",
                    path=path,
                )
                try:
                    dispatched = operator_broker.dispatch(
                        root=Path(work["root"]),
                        profile_name=member["profile_name"],
                        mission=mission,
                        work_item_id=work["id"],
                        requester=f"operator-council:{council_id}",
                        isolated=False,
                    )
                    operator_roster.bind_reservation(
                        reservation.group,
                        project_run_id=dispatched.run_id,
                        path=path,
                    )
                except Exception:
                    operator_roster.release_reservation(reservation.group, path=path)
                    raise
                operator_roster.bind_council_run(
                    council_id,
                    member["profile_name"],
                    dispatched.run_id,
                    path=path,
                )
                events.append({
                    "kind": "council_member_dispatched",
                    "council": council_id,
                    "profile": member["profile_name"],
                    "run": dispatched.run_id,
                })
            elif member["state"] == "running":
                run = operator_broker.run_status(
                    Path(work["root"]), int(member["project_run_id"])
                )
                if not run or run["status"] not in db.RUN_TERMINAL:
                    continue
                operator_roster.release_run_reservations(
                    int(member["project_run_id"]),
                    state="consumed" if run["status"] == "done" else "released",
                    path=path,
                )
                opinion = _parse_council_opinion(
                    str(run.get("summary") or ""),
                    profile=member["profile_name"],
                )
                resolved = operator_roster.submit_council_opinion(
                    council_id, member["profile_name"], opinion, path=path
                )
                events.append({
                    "kind": "council_opinion",
                    "council": council_id,
                    "profile": member["profile_name"],
                })
                if resolved["state"] in {"quorum", "split"}:
                    events.append({
                        "kind": f"council_{resolved['state']}",
                        "council": council_id,
                        "synthesis": resolved["synthesis"],
                    })


def _parse_council_opinion(summary: str, *, profile: str) -> dict[str, Any]:
    marker = "OPERATOR_COUNCIL:"
    if marker in summary:
        candidate = summary.rsplit(marker, 1)[1].strip()
        try:
            decoded = json.loads(candidate)
            if isinstance(decoded, dict):
                return decoded
        except (json.JSONDecodeError, ValueError):
            pass
    return {
        "diagnosis": "The reviewer did not return a conforming structured opinion.",
        "evidence": [summary[-1000:]] if summary else [],
        "hypotheses": [],
        "action_key": f"invalid_opinion_{_fingerprint(profile)}",
        "next_action": "Do not act on this opinion.",
        "smallest_surface": [],
        "deferred": ["all implementation"],
        "confidence": 0,
        "risks": ["unstructured or failed council response"],
    }


def _controller_connect(path: Path | None):
    con = operator_runtime.connect(path)
    con.executescript(CONTROLLER_SCHEMA)
    return con


def _active_project_profiles(operation: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for project in operation["projects"]:
        try:
            con = db.connect_readonly(Path(project["root"]))
        except Exception:
            continue
        try:
            for row in con.execute(
                "SELECT agent, COUNT(*) AS n FROM runs "
                "WHERE status NOT IN ('done','failed','timeout','killed') GROUP BY agent"
            ):
                counts[row["agent"]] = counts.get(row["agent"], 0) + int(row["n"])
        finally:
            con.close()
    return counts


def _review_row(
    work_item_id: str,
    *,
    path: Path | None,
    running_only: bool = True,
) -> dict[str, Any] | None:
    con = _controller_connect(path)
    try:
        query = "SELECT * FROM operator_work_reviews WHERE work_item_id=?"
        if running_only:
            query += " AND state='running'"
        row = con.execute(query, (work_item_id,)).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def run_workdir(root: Path, run_id: int) -> str:
    row = operator_broker.run_status(root, run_id)
    if not row:
        raise ControllerError(f"project run {run_id} disappeared")
    return row["workdir"]


def _mission(contract: dict[str, Any], work: dict[str, Any]) -> str:
    work_scope = work["requirements"]["scope"]
    return (
        f"Goal: {work['description']}\n\n"
        f"Required gates: {json.dumps(contract['quality']['gates'])}\n"
        f"Writable project: {work['contract_project_id']}\n"
        f"Allowed repository-relative scope: {json.dumps(work_scope['include'])}\n"
        f"Excluded repository-relative scope: {json.dumps(work_scope['exclude'])}\n"
        f"Read-only project snapshots: "
        f"{json.dumps(work['requirements'].get('read_dependencies', []))}\n"
        f"Change budget: {json.dumps(work['change_budget'])}\n\n"
        "Implement only the smallest coherent change needed for this outcome. "
        "Avoid speculative abstractions, unrelated cleanup, new dependencies, and "
        "architecture expansion. Run relevant checks and commit the result."
    )


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()[:16]


def _summary(operation: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "operation_id": operation["id"],
        "operation_state": operation["state"],
        "mode": operation["mode"],
        "work_counts": operation["work_counts"],
        "open_decisions": operation["open_decisions"],
        "events": events,
    }
