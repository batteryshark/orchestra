"""The one long-lived process (DESIGN §2).

Foreground loop, launchd-supervised: each tick recovers abandoned admissions,
resumes held policy work, releases dependency-ready runs, and then checks Work.
SIGTERM/SIGINT stop it between ticks, so launchd's stop signal never lands
mid-pass.

The HTTP surface and dashboard (§3) attach at ``serve_http`` below: one
process, one port, the same database. The dashboard's "sweep now" button
sets ``wake``, so a tick starts at once instead of waiting out the interval.
"""
import os
import signal
import sys
import threading
import traceback
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orchestra import (conductor, config, db, http, nod, observer, proc, project,
                         supervise, runway, sweeper, work_client, worktree)

DEFAULT_INTERVAL = 60
SUPERVISOR_HANDOFF_GRACE_SECONDS = 300


def serve_http(stop: threading.Event, wake: threading.Event | None = None,
               restart: threading.Event | None = None):
    """The HTTP surface (DESIGN §3, W-0100). The daemon owns the port; the
    server stops when ``stop`` is set. Returns None when no shared secret is
    configured — an unauthenticated snapshot is not an option."""
    return http.serve(stop, wake=wake, restart=restart)


def _harvest_children() -> int:
    """Clear any dead children out of the process table.

    Supervisor launches now attach their own waiter, but harvesting remains
    a backstop for children launched before that waiter existed or by another
    path. A ZOMBIE pid still answers `kill(pid, 0)`, which once left run 9 in
    `spawning` forever.
    """
    return proc.harvest_children()


def _alive(pid: int) -> bool:
    """True only for a process that could still be working.

    `kill(pid, 0)` succeeds on a zombie, so it alone is not liveness. A
    zombie has exited; anything waiting on its run must treat it as gone.
    """
    return proc.alive(pid)


def _launch_started_at(value: str | None) -> datetime | None:
    try:
        started = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return started.replace(tzinfo=timezone.utc) if started.tzinfo is None else started


def _reap_orphans(con) -> list[int]:
    """A supervisor that died without writing a terminal status leaves a run
    that nothing will ever finish. Mark it, so the board stops lying."""
    reaped = []
    rows = con.execute(
        f"SELECT id, supervisor_pid FROM runs WHERE status NOT IN {db.TERMINAL_SQL} "
        "AND supervisor_pid IS NOT NULL")
    for row in list(rows):
        if _alive(int(row["supervisor_pid"])):
            continue
        changed = con.execute(
            "UPDATE runs SET status='failed', finished_at=?, "
            "summary=COALESCE(summary || char(10), '') || ? "
            f"WHERE id=? AND status NOT IN {db.TERMINAL_SQL}",
            (db.now(), f"Supervisor process {row['supervisor_pid']} vanished.",
             row["id"]))
        if changed.rowcount == 1:
            reaped.append(int(row["id"]))
    con.commit()  # a lost CAS still opened a write transaction

    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=SUPERVISOR_HANDOFF_GRACE_SECONDS)
    candidates = list(con.execute(
        "SELECT * FROM runs WHERE status='spawning' "
        "AND supervisor_pid IS NULL ORDER BY id"))
    for candidate in candidates:
        started = _launch_started_at(candidate["started_at"])
        if started is not None and started > cutoff:
            continue
        # Either the child claim or this terminal compare-and-swap wins. A
        # delayed child that loses exits before it reads the brief or starts a
        # worker. Preparation may also refresh started_at after the scan, so
        # freshness is judged again while holding the write lock.
        con.execute("BEGIN IMMEDIATE")
        current = con.execute(
            "SELECT started_at FROM runs WHERE id=? AND status='spawning' "
            "AND supervisor_pid IS NULL", (int(candidate["id"]),)).fetchone()
        if current is None:
            con.rollback()
            continue
        started = _launch_started_at(current["started_at"])
        if started is not None and started > cutoff:
            con.rollback()
            continue
        reason = (f"no supervisor claimed run within "
                  f"{SUPERVISOR_HANDOFF_GRACE_SECONDS}s" if started is not None
                  else "invalid start time and no supervisor claimed run")
        claimed = con.execute(
            "UPDATE runs SET status='failed', finished_at=?, summary=? "
            "WHERE id=? AND status='spawning' AND supervisor_pid IS NULL",
            (db.now(), f"Launch setup failed: {reason}", int(candidate["id"])))
        if claimed.rowcount == 1:
            con.execute(
                "UPDATE deferred_dispatches SET status='failed', processed_at=?, "
                "error=? WHERE run_id=? AND status IN ('pending','processing','fired')",
                (db.now(), reason, int(candidate["id"])))
        con.commit()
        if claimed.rowcount != 1:
            continue
        run_id = int(candidate["id"])
        run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        root = project.root_for(con, run)
        if run["branch"]:
            root = worktree.main_root(Path(run["workdir"])) or root
        supervise.fail_launch(
            con, root, run_id, reason)
        reaped.append(run_id)
    return reaped


RUNWAY_EVERY_SECONDS = 300


def _poll_runway(con) -> int:
    """Keep provider readings current without a human opening the dashboard.

    The conductor's runway_low trigger reads stored polls, so if only the
    dashboard polled, an unattended Orchestra would go blind exactly when it is
    most on its own (owner, 2026-08-14). Never raises: every adapter already
    fails soft, and a provider outage must not end a tick.
    """
    last = db.meta_get(con, "runway_polled_at")
    if last:
        try:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(last)).total_seconds()
            if age < RUNWAY_EVERY_SECONDS:
                return 0
        except ValueError:
            pass  # unparsable stamp: poll and rewrite it
    try:
        results = runway.poll_all(config.load())
        runway.record(con, results)
    except Exception as exc:  # a provider must never end a tick
        print(f"orchestra daemon: runway poll failed: {exc}", file=sys.stderr)
        return 0
    db.meta_set(con, "runway_polled_at", datetime.now(timezone.utc).isoformat())
    con.commit()
    return len(results)


def _act_on_nod_answers(con, cfg: dict) -> list[dict]:
    """The acting half of the human loop: answered merge cards do something.

    Never raises, matching _poll_runway: an unreachable Nod or a broken act
    must not end a tick. The pass itself is cheap when idle — one SQL query,
    no network — and skips polling entirely when Nod is not configured.
    """
    try:
        return nod.act_on_answers(con, cfg)
    except Exception as exc:
        print(f"orchestra daemon: nod answers pass failed: {exc!r}",
              file=sys.stderr)
        return []


def tick() -> dict:
    """One pass. Returns a small report; never raises for a Work-side fault."""
    cfg = config.load()
    report = {"swept": [], "conducted": [], "released": [], "reaped": [],
              "resumed_retries": [], "resumed_judgments": [],
              "paused": False, "runway": 0, "nod_answers": []}
    con = db.connect()
    try:
        _harvest_children()
        report["reaped"] = _reap_orphans(con)
        report["paused"] = http.dispatch_paused(con)
        if not report["paused"]:
            report["resumed_retries"] = observer.resume_deferred_retries(
                con, launcher=supervise.spawn_supervisor)
            report["resumed_judgments"] = conductor.resume_deferred_judgments(
                con, launcher=supervise.spawn_supervisor)
        # Retry reservations repoint dependency edges before settlement. This
        # ordering prevents a paused-then-resumed retry from losing its waiting
        # dependents to the failed original run.
        report["released"] = supervise.process_ready(
            con, supervise.spawn_supervisor)
        report["runway"] = _poll_runway(con)
        report["nod_answers"] = _act_on_nod_answers(con, cfg)
        # ponytail: one project-list fetch per tick keeps the cache warm at the
        # cost of an HTTP round trip a minute; drive it off a Work event when
        # phase 3's hooks land.
        project.refresh(con, cfg)
    finally:
        con.close()
    client = work_client.from_cfg(cfg)
    if client is not None:
        report["swept"] = sweeper.sweep(cfg, client)
        # The conductor rides the same tick (DESIGN §10). It reads the board
        # and costs ZERO tokens unless a goal has an event worth judging.
        # ponytail: that is a second whole-board GET per tick; share the
        # sweeper's fetch once one of the two passes owns it.
        report["conducted"] = conductor.pass_once(cfg, client)
    return report


def run(interval: int | None = None, once: bool = False) -> int:
    cfg = config.load()
    if interval is None:
        interval = int(cfg.get("work", {}).get("poll_interval", DEFAULT_INTERVAL) or
                       DEFAULT_INTERVAL)
    stop = threading.Event()
    wake = threading.Event()  # "sweep now" from the dashboard, and shutdown
    restart = threading.Event()  # "restart" from the dashboard

    def halt(*_):
        stop.set()
        wake.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, halt)
    serve_http(stop, wake, restart)
    print(f"orchestra daemon: pid {os.getpid()}, every {interval}s", flush=True)
    # W-0189: said ONCE, at startup, because a spin observer that cannot run
    # means nothing is watching any run — and that used to be silent.
    for line in observer.status_report(cfg):
        print("orchestra daemon:" + line, flush=True)
    while True:
        started = time.time()
        error = None
        try:
            report = tick()
        except Exception as exc:  # a bad tick must not end the daemon
            # The repr alone is not enough to fix anything: a tick that failed
            # every pass for hours was one line of AttributeError with no file
            # and no line number. Print the traceback too.
            print(f"orchestra daemon: tick failed: {exc!r}", file=sys.stderr, flush=True)
            traceback.print_exc(file=sys.stderr)
            report, error = {}, repr(exc)
        for name, entries in report.items():
            if entries:
                print(f"orchestra daemon: {name}: {entries}", flush=True)
        try:
            http.record_health(report, error)
        except Exception as exc:  # health is a readout, never a reason to stop
            print(f"orchestra daemon: health write failed: {exc!r}",
                  file=sys.stderr, flush=True)
        if once or stop.is_set() or restart.is_set():
            break
        wake.wait(max(1, interval - (time.time() - started)))
        wake.clear()
        if stop.is_set() or restart.is_set():
            break
    if restart.is_set():
        # Replace this process image rather than exiting: the pid survives, so
        # a LaunchAgent does not have to race us, and the supervisors we
        # started stay exactly where they are.
        print("orchestra daemon: restarting", flush=True)
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(sys.executable, [sys.executable, "-m", "orchestra",
                                  "daemon", "--interval", str(interval)])
    print("orchestra daemon: stopped", flush=True)
    return 0
