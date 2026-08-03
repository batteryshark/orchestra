"""Durable, exactly-once launch sequencing for top-level dispatches."""
from __future__ import annotations

import json
import socket
import sqlite3
from pathlib import Path
from typing import Callable

from orchestra_cli import brief, capabilities, config, db, paths, worktree


REQUIRES_SUCCESS = "requires_success"
WAIT_FOR = "wait_for"
EDGE_KINDS = frozenset((REQUIRES_SUCCESS, WAIT_FOR))
_TERMINAL_STATUSES_SQL = ", ".join(f"'{status}'" for status in db.RUN_TERMINAL)
# A user-cancelled producer is not evidence that its work failed. Success
# dependents remain held until the producer is resumed or the dependent is cancelled.
_UNSUCCESSFUL_TERMINAL_STATUSES = frozenset({"failed", "timeout"})
_UNSUCCESSFUL_TERMINAL_STATUSES_SQL = ", ".join(
    f"'{status}'" for status in db.RUN_TERMINAL
    if status in _UNSUCCESSFUL_TERMINAL_STATUSES
)
# `done` means the worker process exited cleanly.  A prerequisite that asked
# for verification is successful only when its handoff explicitly records it.
# Keep this predicate in one place: launch readiness and failure propagation
# must never disagree about whether an edge is satisfiable.
_SUCCESSFUL_PREREQUISITE_SQL = (
    "(prerequisite.status='done' AND "
    "(COALESCE(prerequisite.verification_required, 0)=0 "
    "OR prerequisite.verification_status='verified'))"
)
_UNSUCCESSFUL_PREREQUISITE_SQL = (
    "(prerequisite.status IN (" + _UNSUCCESSFUL_TERMINAL_STATUSES_SQL + ") OR "
    "(prerequisite.status='done' "
    "AND COALESCE(prerequisite.verification_required, 0)=1 "
    "AND COALESCE(prerequisite.verification_status, 'pending')!='verified'))"
)


def _validate_edge_kind(kind: str) -> str:
    if kind not in EDGE_KINDS:
        choices = ", ".join(sorted(EDGE_KINDS))
        raise ValueError(f"unknown dependency kind {kind!r}; expected one of {choices}")
    return kind


def validate(con: sqlite3.Connection, run_ids: list[int]) -> list[int]:
    """Return de-duplicated dependency IDs, rejecting unknown runs."""
    ordered = list(dict.fromkeys(run_ids))
    if not ordered:
        return []
    rows = con.execute(
        f"SELECT id FROM runs WHERE id IN ({','.join('?' for _ in ordered)})",
        ordered,
    ).fetchall()
    found = {int(row["id"]) for row in rows}
    missing = [run_id for run_id in ordered if run_id not in found]
    if missing:
        raise SystemExit(
            "orchestra: --after references unknown run(s): "
            + ", ".join(map(str, missing))
        )
    return ordered


def enqueue(
    con: sqlite3.Connection,
    run_id: int,
    dependency_ids: list[int],
    *,
    mission: str,
    context: str | None,
    use_worktree: bool,
    dependency_kind: str = REQUIRES_SUCCESS,
    wait_for_ids: list[int] | None = None,
    work_snapshot: str | None = None,
    required_capabilities: list[str] | None = None,
    writes_tree: bool = True,
) -> None:
    """Record a deferred dispatch with one kind of prerequisite edge.

    ``requires_success`` preserves the historical ``--after`` behavior. A
    ``wait_for`` edge only sequences work: any terminal prerequisite releases
    it, including a failed, timed-out, or killed prerequisite.
    """
    dependency_kind = _validate_edge_kind(dependency_kind)
    if work_snapshot is not None and not isinstance(work_snapshot, str):
        raise ValueError("work_snapshot must be a string or None")
    requirements = list(dict.fromkeys(required_capabilities or []))
    if any(not isinstance(item, str) or not item.strip() for item in requirements):
        raise ValueError("required_capabilities must contain non-empty strings")
    if writes_tree and not use_worktree:
        conflicts = pending_shared_writer_conflicts(
            con, [*dependency_ids, *(wait_for_ids or [])]
        )
        if conflicts:
            raise SharedWriterConflict(conflicts)
    edges = [(run_id, dependency_id, dependency_kind) for dependency_id in dependency_ids]
    edges.extend((run_id, dependency_id, WAIT_FOR) for dependency_id in (wait_for_ids or []))
    con.executemany(
        "INSERT INTO dispatch_dependencies(run_id, depends_on_run, kind) VALUES(?,?,?)",
        edges,
    )
    con.execute("UPDATE runs SET writes_tree=? WHERE id=?", (int(writes_tree), run_id))
    con.execute(
        "INSERT INTO deferred_dispatches("
        "run_id, mission, context, use_worktree, work_snapshot, "
        "required_capabilities_json, status, created_at"
        ") VALUES(?,?,?,?,?,?,'pending',?)",
        (
            run_id, mission, context, int(use_worktree), work_snapshot,
            json.dumps(requirements), db.now(),
        ),
    )


class SharedWriterConflict(RuntimeError):
    def __init__(self, run_ids: list[int]):
        self.run_ids = run_ids
        super().__init__(
            "shared-tree writer would collide with pending run(s): "
            + ", ".join(map(str, run_ids))
        )


def shared_writer_conflicts(
    con: sqlite3.Connection, *, exclude_run_ids: list[int] | None = None
) -> list[int]:
    """Return currently executing writers that share the integration tree."""
    params: list[int] = list(dict.fromkeys(exclude_run_ids or []))
    exclude = ""
    if params:
        exclude = f" AND run.id NOT IN ({','.join('?' for _ in params)})"
    return [
        int(row["run_id"])
        for row in con.execute(
            "SELECT run.id AS run_id FROM runs run "
            "LEFT JOIN deferred_dispatches deferred ON deferred.run_id=run.id "
            "WHERE run.status NOT IN ('pending','done','failed','timeout','killed') "
            "AND run.writes_tree=1 AND run.branch IS NULL "
            "AND COALESCE(deferred.use_worktree, 0)=0"
            + exclude
            + " ORDER BY run.id",
            params,
        )
    ]


def pending_shared_writer_conflicts(
    con: sqlite3.Connection, prerequisite_ids: list[int]
) -> list[int]:
    """Return pending shared writers gated by any same prerequisite."""
    ids = list(dict.fromkeys(prerequisite_ids))
    if not ids:
        return []
    return [
        int(row["run_id"])
        for row in con.execute(
            "SELECT DISTINCT deferred.run_id FROM deferred_dispatches deferred "
            "JOIN runs run ON run.id=deferred.run_id "
            "JOIN dispatch_dependencies edge ON edge.run_id=deferred.run_id "
            "WHERE deferred.status='pending' AND deferred.use_worktree=0 "
            "AND run.writes_tree=1 AND edge.depends_on_run IN "
            f"({','.join('?' for _ in ids)}) ORDER BY deferred.run_id",
            ids,
        )
    ]


def pending_on(con: sqlite3.Connection, run_id: int) -> list[int]:
    return [
        int(row["depends_on_run"])
        for row in con.execute(
            "SELECT d.depends_on_run FROM dispatch_dependencies d "
            "JOIN runs prerequisite ON prerequisite.id=d.depends_on_run "
            "WHERE d.run_id=? AND (prerequisite.status NOT IN "
            f"({_TERMINAL_STATUSES_SQL}) OR "
            f"(COALESCE(d.kind, '{REQUIRES_SUCCESS}')!='{WAIT_FOR}' "
            "AND prerequisite.status='killed')) ORDER BY d.depends_on_run",
            (run_id,),
        )
    ]


def held_on(con: sqlite3.Connection, run_id: int) -> list[int]:
    return [
        int(row["depends_on_run"])
        for row in con.execute(
            "SELECT edge.depends_on_run FROM dispatch_dependencies edge "
            "JOIN runs prerequisite ON prerequisite.id=edge.depends_on_run "
            f"WHERE edge.run_id=? AND COALESCE(edge.kind, '{REQUIRES_SUCCESS}')!='{WAIT_FOR}' "
            "AND prerequisite.status='killed' ORDER BY edge.depends_on_run",
            (run_id,),
        )
    ]


def _decline_failed(con: sqlite3.Connection) -> int:
    """Transitively decline pending runs whose prerequisites cannot succeed."""
    total = 0
    while True:
        con.execute("BEGIN IMMEDIATE")
        try:
            rows = list(con.execute(
                "SELECT DISTINCT deferred.run_id, run.requested_by, run.work_item "
                "FROM deferred_dispatches deferred "
                "JOIN runs run ON run.id=deferred.run_id "
                "JOIN dispatch_dependencies edge ON edge.run_id=deferred.run_id "
                "JOIN runs prerequisite ON prerequisite.id=edge.depends_on_run "
                "WHERE deferred.status='pending' AND run.status='pending' "
                f"AND COALESCE(edge.kind, '{REQUIRES_SUCCESS}')!='{WAIT_FOR}' "
                f"AND {_UNSUCCESSFUL_PREREQUISITE_SQL} "
                "ORDER BY deferred.run_id"
            ))
            for row in rows:
                failed = list(con.execute(
                    "SELECT prerequisite.id, prerequisite.status, prerequisite.verification_status "
                    "FROM dispatch_dependencies edge "
                    "JOIN runs prerequisite ON prerequisite.id=edge.depends_on_run "
                    f"WHERE edge.run_id=? AND COALESCE(edge.kind, '{REQUIRES_SUCCESS}')!='{WAIT_FOR}' "
                    f"AND {_UNSUCCESSFUL_PREREQUISITE_SQL} "
                    "ORDER BY prerequisite.id",
                    (row["run_id"],),
                ))
                detail = ", ".join(
                    f"{item['id']} ({item['status']}"
                    + (f"/{item['verification_status']}" if item["status"] == "done" else "")
                    + ")"
                    for item in failed
                )
                summary = f"Not launched: prerequisite run(s) did not succeed: {detail}"
                changed = con.execute(
                    "UPDATE deferred_dispatches SET status='declined', processed_at=?, error=? "
                    "WHERE run_id=? AND status='pending'",
                    (db.now(), summary, row["run_id"]),
                )
                if changed.rowcount != 1:
                    continue
                con.execute(
                    "UPDATE runs SET status='failed', summary=?, finished_at=? "
                    "WHERE id=? AND status='pending'",
                    (summary, db.now(), row["run_id"]),
                )
                con.execute(
                    "INSERT INTO messages(sender, recipient, body, work_item, run_id, "
                    "created_at) VALUES('orchestra',?,?,?,?,?)",
                    (
                        row["requested_by"], summary, row["work_item"],
                        row["run_id"], db.now(),
                    ),
                )
                con.execute(
                    "INSERT INTO feed(author, body, work_item, run_id, created_at, tags) "
                    "VALUES('orchestra',?,?,?,?, 'run')",
                    (summary, row["work_item"], row["run_id"], db.now()),
                )
                total += 1
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        if not rows:
            return total


def decline_failed(con: sqlite3.Connection) -> int:
    """Public failure propagation hook for cancellation and reconciliation."""
    return _decline_failed(con)


def cancellation_impact(
    con: sqlite3.Connection,
    run_ids: int | tuple[int, ...] | list[int],
) -> dict[str, list[int]]:
    """Preview pending deferred dispatches affected by cancelling ``run_ids``.

    The function intentionally performs no writes. Cancelling a
    ``requires_success`` prerequisite holds its dependent; independent failed
    or timed-out edges still decline and cascade. A ``wait_for`` dependent is
    reported only when *all* of its edges would be satisfied after that
    hypothetical cascade, making ``unblocked_run_ids`` genuinely launchable.
    """
    if isinstance(run_ids, int):
        requested = {run_ids}
    else:
        requested = set(run_ids)
    if not requested:
        return {"declined_run_ids": [], "held_run_ids": [], "unblocked_run_ids": []}

    placeholders = ",".join("?" for _ in requested)
    cancelled = {
        int(row["id"])
        for row in con.execute(
            "SELECT id FROM runs WHERE id IN ("
            + placeholders
            + f") AND status NOT IN ({_TERMINAL_STATUSES_SQL})",
            sorted(requested),
        )
    }
    # A terminal or nonexistent run has no cancellation transition to model.
    if not cancelled:
        return {"declined_run_ids": [], "held_run_ids": [], "unblocked_run_ids": []}

    rows = list(con.execute(
        "SELECT edge.run_id, edge.depends_on_run, edge.kind, prerequisite.status, "
        "prerequisite.verification_required, prerequisite.verification_status "
        "FROM dispatch_dependencies edge "
        "JOIN deferred_dispatches deferred ON deferred.run_id=edge.run_id "
        "JOIN runs dependent ON dependent.id=edge.run_id "
        "JOIN runs prerequisite ON prerequisite.id=edge.depends_on_run "
        "WHERE deferred.status='pending' AND dependent.status='pending' "
        "ORDER BY edge.run_id, edge.depends_on_run"
    ))
    outgoing: dict[int, set[int]] = {}
    incoming: dict[int, list[tuple[int, str, str]]] = {}
    for row in rows:
        dependent = int(row["run_id"])
        if dependent in cancelled:
            continue
        prerequisite = int(row["depends_on_run"])
        # A malformed legacy value fails closed as requires-success rather
        # than silently allowing work to launch after a failed prerequisite.
        kind = row["kind"] if row["kind"] == WAIT_FOR else REQUIRES_SUCCESS
        observed_status = row["status"]
        if (
            observed_status == "done"
            and int(row["verification_required"] or 0)
            and row["verification_status"] != "verified"
        ):
            # For a success edge, a clean process exit without the required
            # acceptance outcome is semantically a failed prerequisite.  The
            # preview must agree with the real dependency launcher.
            observed_status = "failed"
        outgoing.setdefault(prerequisite, set()).add(dependent)
        incoming.setdefault(dependent, []).append((prerequisite, kind, observed_status))

    declined: set[int] = set()
    held: set[int] = set()
    unblocked: set[int] = set()
    newly_terminal = set(cancelled)
    while newly_terminal:
        next_terminal: set[int] = set()
        for prerequisite in newly_terminal:
            for dependent in outgoing.get(prerequisite, set()):
                edges = incoming[dependent]
                effective_edges = [
                    (
                        kind,
                        "killed" if edge_prerequisite in cancelled
                        else "failed" if edge_prerequisite in declined
                        else observed_status,
                    )
                    for edge_prerequisite, kind, observed_status in edges
                ]

                if any(
                    kind == REQUIRES_SUCCESS
                    and status in _UNSUCCESSFUL_TERMINAL_STATUSES
                    for kind, status in effective_edges
                ):
                    if dependent in declined:
                        continue
                    declined.add(dependent)
                    held.discard(dependent)
                    unblocked.discard(dependent)
                    next_terminal.add(dependent)
                    continue

                if any(
                    kind == REQUIRES_SUCCESS and status == "killed"
                    for kind, status in effective_edges
                ):
                    held.add(dependent)
                    unblocked.discard(dependent)
                    continue

                if all(
                    (status == "done")
                    if kind == REQUIRES_SUCCESS
                    else (status in db.RUN_TERMINAL)
                    for kind, status in effective_edges
                ) and dependent not in declined:
                    unblocked.add(dependent)
        newly_terminal = next_terminal

    return {
        "declined_run_ids": sorted(declined),
        "held_run_ids": sorted(held),
        "unblocked_run_ids": sorted(unblocked),
    }


def _claim_ready(con: sqlite3.Connection) -> list[dict]:
    con.execute("BEGIN IMMEDIATE")
    try:
        ready = list(con.execute(
            "SELECT deferred.*, run.agent, run.requested_by, run.work_item, run.team, "
            "run.title, run.allow_question, run.question_wait_seconds, "
            "run.verification_required, run.writes_tree, run.slug "
            "FROM deferred_dispatches deferred JOIN runs run ON run.id=deferred.run_id "
            "WHERE deferred.status='pending' AND run.status='pending' "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM dispatch_dependencies edge "
            "  JOIN runs prerequisite ON prerequisite.id=edge.depends_on_run "
            "  WHERE edge.run_id=deferred.run_id AND ("
            f"    (COALESCE(edge.kind, '{REQUIRES_SUCCESS}')!='{WAIT_FOR}' "
            f"AND NOT {_SUCCESSFUL_PREREQUISITE_SQL}) OR "
            f"    (edge.kind='{WAIT_FOR}' AND prerequisite.status NOT IN ({_TERMINAL_STATUSES_SQL}))"
            "  )"
            ") ORDER BY deferred.run_id"
        ))
        claimed: list[dict] = []
        for row in ready:
            changed = con.execute(
                "UPDATE deferred_dispatches SET status='processing' "
                "WHERE run_id=? AND status='pending'",
                (row["run_id"],),
            )
            if changed.rowcount != 1:
                continue
            con.execute(
                "UPDATE runs SET status='spawning', started_at=? "
                "WHERE id=? AND status='pending'",
                (db.now(), row["run_id"]),
            )
            claimed.append(dict(row))
        con.execute("COMMIT")
        return claimed
    except BaseException:
        con.execute("ROLLBACK")
        raise


def _stored_requirements(request: dict) -> list[str]:
    """Decode the dispatch-time requirement list; fail closed if DB data is bad."""
    try:
        parsed = json.loads(request.get("required_capabilities_json") or "[]")
    except (TypeError, ValueError) as exc:
        raise RuntimeError("deferred capability requirements are malformed") from exc
    if not isinstance(parsed, list) or any(
        not isinstance(item, str) or not item.strip() for item in parsed
    ):
        raise RuntimeError("deferred capability requirements are malformed")
    return list(dict.fromkeys(parsed))


def _require_current_capabilities(root: Path, agent: dict, requirements: list[str]) -> None:
    """Prevent a queued run from launching after its environment evidence changed."""
    if not requirements:
        return
    backend = agent["backend"]
    sandbox = (
        agent.get("sandbox", "workspace-write")
        if backend == "codex"
        else "orchestra-unrestricted"
    )
    check = capabilities.check_requirements(
        root,
        host_identity=socket.gethostname(),
        backend=backend,
        profile=agent["name"],
        sandbox_mode=str(sandbox),
        capabilities=requirements,
    )
    if check.satisfied:
        return
    failures = "; ".join(
        f"{name}={','.join(sorted(getattr(check, name))) or '-'}"
        for name in ("unsupported", "unknown", "missing", "expired")
        if getattr(check, name)
    )
    raise RuntimeError(
        "required environment capabilities no longer have fresh positive evidence "
        f"for {agent['name']} ({failures})"
    )


def _setup(
    con: sqlite3.Connection,
    root: Path,
    cfg: dict,
    request: dict,
) -> None:
    run_id = int(request["run_id"])
    agent = config.agent_cfg(cfg, request["agent"])
    requirements = _stored_requirements(request)
    # Recheck before a worktree or brief is written.  A later runner change,
    # sandbox change, or evidence expiry must not turn a queued run into an
    # unverified launch merely because it was admissible hours earlier.
    _require_current_capabilities(root, agent, requirements)
    snapshot = request.get("work_snapshot")
    if request["work_item"] and snapshot is None:
        # Legacy queued rows predate immutable snapshots. Resolve their tracker
        # context before creating a worktree so a missing item leaves no leaked
        # checkout behind.
        snapshot = brief.work_snapshot(root, request["work_item"], required=True)
    workdir, branch, base_commit = str(root), None, None
    if request["use_worktree"]:
        isolated, branch = worktree.create(root, run_id)
        workdir = str(isolated)
        base_commit = worktree.head(isolated)
    elif request["writes_tree"] and (root / ".git").exists():
        if worktree.status(root):
            raise RuntimeError(
                "integration tree is dirty when deferred writer became ready; "
                "commit/stash it or use an isolated worktree"
            )
        base_commit = worktree.head(root)
    text = brief.compose(
        root=root,
        run_id=run_id,
        agent=agent,
        mission=request["mission"],
        work_item=request["work_item"],
        team=request["team"],
        requester=request["requested_by"],
        workdir=workdir,
        extra_context=request["context"],
        allow_question=bool(request["allow_question"]),
        question_wait_seconds=int(request["question_wait_seconds"]),
        slug=request["slug"],
        require_work_snapshot=bool(request["work_item"]),
        work_snapshot_text=snapshot,
        required_capabilities=requirements,
        require_verification=bool(request["verification_required"]),
        writes_tree=bool(request["writes_tree"]),
    )
    brief_path = paths.briefs_dir(root) / f"run-{run_id}.md"
    brief_path.write_text(text)
    log_path = paths.logs_dir(root) / f"run-{run_id}.jsonl"
    log_path.touch()
    con.execute("BEGIN IMMEDIATE")
    try:
        run_changed = con.execute(
            "UPDATE runs SET brief_path=?, log_path=?, workdir=?, branch=?, base_commit=? "
            "WHERE id=? AND status='spawning'",
            (str(brief_path), str(log_path), workdir, branch, base_commit, run_id),
        )
        request_changed = con.execute(
            "UPDATE deferred_dispatches SET status='fired', processed_at=? "
            "WHERE run_id=? AND status='processing'",
            (db.now(), run_id),
        )
        if run_changed.rowcount != 1 or request_changed.rowcount != 1:
            raise RuntimeError("dispatch was cancelled while launch setup was in progress")
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise


def process_ready(
    con: sqlite3.Connection,
    root: Path,
    cfg: dict,
    launcher: Callable[[Path, int], None],
) -> list[dict]:
    """Decline failed chains and launch each newly-ready run at most once."""
    results: list[dict] = []
    _decline_failed(con)
    for request in _claim_ready(con):
        run_id = int(request["run_id"])
        try:
            _setup(con, root, cfg, request)
            launcher(root, run_id)
            results.append({"run_id": run_id, "status": "fired"})
        except BaseException as exc:
            error = str(exc)[:1000] or exc.__class__.__name__
            con.execute(
                "UPDATE deferred_dispatches SET status='failed', processed_at=?, error=? "
                "WHERE run_id=? AND status IN ('processing','fired')",
                (db.now(), error, run_id),
            )
            con.execute(
                "UPDATE runs SET status='failed', summary=?, finished_at=? "
                "WHERE id=? AND status='spawning'",
                (f"Deferred dispatch launch failed: {error}", db.now(), run_id),
            )
            con.commit()
            results.append({"run_id": run_id, "status": "failed", "error": error})
    _decline_failed(con)
    return results
