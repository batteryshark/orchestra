"""Durable, exactly-once launch sequencing for top-level dispatches."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Callable

from orchestra_cli import brief, config, db, paths, worktree


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
) -> None:
    con.executemany(
        "INSERT INTO dispatch_dependencies(run_id, depends_on_run) VALUES(?,?)",
        [(run_id, dependency_id) for dependency_id in dependency_ids],
    )
    con.execute(
        "INSERT INTO deferred_dispatches(run_id, mission, context, use_worktree, "
        "status, created_at) VALUES(?,?,?,?, 'pending', ?)",
        (run_id, mission, context, int(use_worktree), db.now()),
    )


def pending_on(con: sqlite3.Connection, run_id: int) -> list[int]:
    return [
        int(row["depends_on_run"])
        for row in con.execute(
            "SELECT d.depends_on_run FROM dispatch_dependencies d "
            "JOIN runs prerequisite ON prerequisite.id=d.depends_on_run "
            "WHERE d.run_id=? AND prerequisite.status NOT IN "
            "('done','failed','timeout','killed') ORDER BY d.depends_on_run",
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
                "AND prerequisite.status IN ('failed','timeout','killed') "
                "ORDER BY deferred.run_id"
            ))
            for row in rows:
                failed = list(con.execute(
                    "SELECT prerequisite.id, prerequisite.status "
                    "FROM dispatch_dependencies edge "
                    "JOIN runs prerequisite ON prerequisite.id=edge.depends_on_run "
                    "WHERE edge.run_id=? AND prerequisite.status IN "
                    "('failed','timeout','killed') ORDER BY prerequisite.id",
                    (row["run_id"],),
                ))
                detail = ", ".join(
                    f"{item['id']} ({item['status']})" for item in failed
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


def _claim_ready(con: sqlite3.Connection) -> list[dict]:
    con.execute("BEGIN IMMEDIATE")
    try:
        ready = list(con.execute(
            "SELECT deferred.*, run.agent, run.requested_by, run.work_item, run.team, "
            "run.title, run.allow_question, run.question_wait_seconds, run.slug "
            "FROM deferred_dispatches deferred JOIN runs run ON run.id=deferred.run_id "
            "WHERE deferred.status='pending' AND run.status='pending' "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM dispatch_dependencies edge "
            "  JOIN runs prerequisite ON prerequisite.id=edge.depends_on_run "
            "  WHERE edge.run_id=deferred.run_id AND prerequisite.status!='done'"
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


def _setup(
    con: sqlite3.Connection,
    root: Path,
    cfg: dict,
    request: dict,
) -> None:
    run_id = int(request["run_id"])
    agent = config.agent_cfg(cfg, request["agent"])
    workdir, branch = str(root), None
    if request["use_worktree"]:
        isolated, branch = worktree.create(root, run_id)
        workdir = str(isolated)
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
    )
    brief_path = paths.briefs_dir(root) / f"run-{run_id}.md"
    brief_path.write_text(text)
    log_path = paths.logs_dir(root) / f"run-{run_id}.jsonl"
    log_path.touch()
    con.execute("BEGIN IMMEDIATE")
    try:
        run_changed = con.execute(
            "UPDATE runs SET brief_path=?, log_path=?, workdir=?, branch=? "
            "WHERE id=? AND status='spawning'",
            (str(brief_path), str(log_path), workdir, branch, run_id),
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
