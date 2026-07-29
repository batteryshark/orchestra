"""Narrow actuation broker used by the autonomous Operator controller."""
from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestra_cli import (
    availability,
    brief,
    config,
    db,
    names,
    paths,
    supervise,
    worktree,
)

MAX_CAPTURE_BYTES = 128 * 1024


class BrokerError(RuntimeError):
    pass


@dataclass(frozen=True)
class Dispatch:
    run_id: int
    profile: str
    workdir: str
    branch: str | None
    base_head: str | None


def dispatch(
    *,
    root: Path,
    profile_name: str,
    mission: str,
    work_item_id: str,
    requester: str,
    isolated: bool = True,
    start_supervisor: bool = True,
    start_point: str | None = None,
    comparison_base: str | None = None,
) -> Dispatch:
    """Create exactly one ordinary Orchestra run through a checked profile."""
    root = root.resolve()
    cfg = config.load(root)
    agent = config.agent_cfg(cfg, profile_name)
    _report, unavailable, _warnings = availability.check_profiles(
        cfg, [(profile_name, agent)]
    )
    if unavailable:
        raise BrokerError("; ".join(unavailable))
    root_head = _git(root, ["rev-parse", "HEAD"]).strip() or None
    base_head = comparison_base or root_head
    con = db.connect(root)
    run_id: int | None = None
    created_worktree = False
    branch: str | None = None
    try:
        for _attempt in range(names.MAX_ATTEMPTS + 4):
            slug = names.assign_slug(con)
            display_model = agent.get("model")
            try:
                cursor = con.execute(
                    "INSERT INTO runs(agent, backend, model, title, work_item, "
                    "requested_by, workdir, slug, allow_question, status, started_at) "
                    "VALUES(?,?,?,?,?,?,?,?,0,'spawning',?)",
                    (
                        profile_name,
                        agent["backend"],
                        display_model,
                        mission[:80],
                        work_item_id,
                        requester,
                        str(root),
                        slug,
                        db.now(),
                    ),
                )
                run_id = int(cursor.lastrowid)
                break
            except sqlite3.IntegrityError as exc:
                if not names.is_unique_violation(exc):
                    raise
                names.reset_memory_cache()
        if run_id is None:
            raise BrokerError("could not mint a unique run slug")
        workdir = str(root)
        if isolated:
            created, branch = worktree.create(
                root, run_id, start_point=start_point or root_head
            )
            created_worktree = True
            workdir = str(created)
        text = brief.compose(
            root=root,
            run_id=run_id,
            agent=agent,
            mission=mission,
            work_item=work_item_id,
            team=None,
            requester=requester,
            workdir=workdir,
            extra_context=(
                "This is Operator-controlled bounded work. Keep the smallest coherent "
                "change, do not broaden scope, commit your finished changes, and report "
                "verification evidence in the handoff."
            ),
            allow_question=False,
            question_wait_seconds=1800,
            slug=slug,
        )
        brief_path = paths.briefs_dir(root) / f"run-{run_id}.md"
        log_path = paths.logs_dir(root) / f"run-{run_id}.jsonl"
        brief_path.write_text(text, encoding="utf-8")
        log_path.touch()
        con.execute(
            "UPDATE runs SET brief_path=?, log_path=?, workdir=?, branch=? WHERE id=?",
            (str(brief_path), str(log_path), workdir, branch, run_id),
        )
        con.commit()
    except BaseException:
        con.rollback()
        if created_worktree and run_id is not None and branch and root_head:
            try:
                reclaim_integrated(
                    root,
                    run_id=run_id,
                    branch=branch,
                    target_branch=root_head,
                )
            except (BrokerError, OSError):
                pass
        if run_id is not None:
            con.execute(
                "UPDATE runs SET status='failed', exit_code=1, finished_at=? WHERE id=?",
                (db.now(), run_id),
            )
            con.commit()
        raise
    finally:
        con.close()
    if start_supervisor:
        supervise.spawn_supervisor(root, run_id)
    return Dispatch(run_id, profile_name, workdir, branch, base_head)


def run_status(root: Path, run_id: int) -> dict[str, Any] | None:
    con = db.connect_readonly(root)
    try:
        row = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def measure_change(root: Path, *, base_head: str, branch: str) -> dict[str, Any]:
    ancestry = subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", base_head, branch],
        capture_output=True,
        timeout=30,
    )
    if ancestry.returncode != 0:
        raise BrokerError("worker branch no longer descends from its recorded base")
    raw = _git(root, ["diff", "--numstat", f"{base_head}...{branch}"])
    files = 0
    added = 0
    deleted = 0
    changed_paths: list[str] = []
    for line in raw.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        files += 1
        changed_paths.append(parts[2])
        if parts[0].isdigit():
            added += int(parts[0])
        if parts[1].isdigit():
            deleted += int(parts[1])
    dependency_names = {
        "cargo.toml", "go.mod", "package.json", "pyproject.toml",
        "requirements.txt", "gemfile",
    }
    dependency_surfaces = [
        value for value in changed_paths
        if Path(value).name.casefold() in dependency_names
        or Path(value).name.casefold().startswith("requirements")
    ]
    migrations = [
        value for value in changed_paths
        if any(part.casefold() in {"migration", "migrations"} for part in Path(value).parts)
    ]
    public_api = [
        value for value in changed_paths
        if any(part.casefold() in {"api", "include", "public"} for part in Path(value).parts[:-1])
    ]
    return {
        "files": files,
        "added_lines": added,
        "deleted_lines": deleted,
        "changed_paths": changed_paths[:500],
        "dependency_surface_changes": len(dependency_surfaces),
        "dependency_surfaces": dependency_surfaces[:100],
        "schema_migrations": len(migrations),
        "migration_paths": migrations[:100],
        "public_api_changes": len(public_api),
        "public_api_paths": public_api[:100],
    }


def scope_violations(
    changed_paths: list[str],
    *,
    include: list[str],
    exclude: list[str],
) -> list[dict[str, str]]:
    violations: list[dict[str, str]] = []
    for raw in changed_paths:
        normalized = raw.replace("\\", "/").lstrip("./")
        if not normalized or normalized.startswith("../"):
            violations.append({"path": raw, "reason": "invalid repository-relative path"})
            continue
        if include and not any(_scope_match(normalized, pattern) for pattern in include):
            violations.append({"path": normalized, "reason": "outside include patterns"})
        if any(_scope_match(normalized, pattern) for pattern in exclude):
            violations.append({"path": normalized, "reason": "matches an exclude pattern"})
    return violations


def _scope_match(path: str, pattern: str) -> bool:
    normalized_pattern = pattern.replace("\\", "/").lstrip("./")
    prefix = normalized_pattern.rstrip("/")
    return (
        fnmatch.fnmatch(path, normalized_pattern)
        or path == prefix
        or (bool(prefix) and path.startswith(prefix + "/"))
    )


def verify(
    workdir: Path,
    commands: list[dict[str, Any]],
    *,
    phase: str,
) -> dict[str, Any]:
    results = []
    for command in commands:
        if command["phase"] not in {phase, "both"}:
            continue
        try:
            process = subprocess.Popen(
                command["argv"],
                cwd=workdir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={**os.environ, "CI": "1"},
                start_new_session=True,
            )
            stdout, stderr = process.communicate(timeout=command["timeout_seconds"])
            output = ((stdout or "") + (stderr or ""))[-MAX_CAPTURE_BYTES:]
            results.append({
                "name": command["name"],
                "required": command["required"],
                "returncode": process.returncode,
                "passed": process.returncode == 0,
                "output_tail": output,
            })
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
            results.append({
                "name": command["name"],
                "required": command["required"],
                "returncode": None,
                "passed": False,
                "output_tail": (
                    f"timed out after {exc.timeout} seconds\n"
                    + (stdout or "")
                    + (stderr or "")
                )[-MAX_CAPTURE_BYTES:],
            })
    return {
        "phase": phase,
        "passed": all(row["passed"] for row in results if row["required"]),
        "commands": results,
    }


def integrate(root: Path, *, branch: str, target_branch: str) -> str:
    if _git(root, ["status", "--porcelain"]).strip():
        raise BrokerError("integration checkout is dirty")
    current = _git(root, ["branch", "--show-current"]).strip()
    if current != target_branch:
        raise BrokerError(
            f"integration checkout is on {current!r}, expected {target_branch!r}"
        )
    completed = subprocess.run(
        ["git", "-C", str(root), "merge", "--no-ff", "--no-edit", branch],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise BrokerError((completed.stderr or completed.stdout).strip())
    return _git(root, ["rev-parse", "HEAD"]).strip()


def resource_snapshot(root: Path) -> dict[str, Any]:
    worktrees = paths.worktrees_dir(root)
    size: int | None = 0
    if worktrees.is_dir():
        try:
            measured = subprocess.run(
                ["du", "-sk", str(worktrees)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            size = (
                int(measured.stdout.split()[0]) * 1024
                if measured.returncode == 0 and measured.stdout.split()
                else None
            )
        except (OSError, ValueError, subprocess.TimeoutExpired):
            size = None
    stat = os.statvfs(root)
    return {
        "worktree_count": sum(
            1 for entry in worktrees.iterdir() if entry.is_dir()
        ) if worktrees.is_dir() else 0,
        "worktree_bytes": size,
        "measurement_complete": size is not None,
        "free_disk_bytes": stat.f_bavail * stat.f_frsize,
    }


def reclaim_integrated(root: Path, *, run_id: int, branch: str, target_branch: str) -> bool:
    workdir = paths.worktrees_dir(root) / f"run-{run_id}"
    if not workdir.is_dir():
        return False
    if _git(workdir, ["status", "--porcelain"]).strip():
        raise BrokerError("refusing to reclaim a dirty worktree")
    ancestor = subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", branch, target_branch],
        capture_output=True,
    )
    if ancestor.returncode != 0:
        raise BrokerError("refusing to reclaim unique, unintegrated work")
    removed = subprocess.run(
        ["git", "-C", str(root), "worktree", "remove", str(workdir)],
        capture_output=True,
        text=True,
    )
    if removed.returncode != 0:
        raise BrokerError(removed.stderr.strip())
    subprocess.run(
        ["git", "-C", str(root), "branch", "-d", branch],
        capture_output=True,
        text=True,
    )
    return True


def reclaim_transferred_worktree(
    root: Path,
    *,
    run_id: int,
    branch: str,
    successor_branch: str,
) -> bool:
    workdir = paths.worktrees_dir(root) / f"run-{run_id}"
    if not workdir.is_dir():
        return False
    if _git(workdir, ["status", "--porcelain"]).strip():
        raise BrokerError("refusing to reclaim a dirty predecessor worktree")
    ancestor = subprocess.run(
        [
            "git", "-C", str(root), "merge-base", "--is-ancestor",
            branch, successor_branch,
        ],
        capture_output=True,
    )
    if ancestor.returncode != 0:
        raise BrokerError("successor branch does not preserve predecessor state")
    removed = subprocess.run(
        ["git", "-C", str(root), "worktree", "remove", str(workdir)],
        capture_output=True,
        text=True,
    )
    if removed.returncode != 0:
        raise BrokerError(removed.stderr.strip())
    return True


def _git(root: Path, args: list[str]) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise BrokerError((completed.stderr or completed.stdout).strip())
    return completed.stdout
