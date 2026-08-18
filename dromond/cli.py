"""dromond CLI. Thin command functions; business logic lives in the modules."""
import argparse
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path

from dromond import (acp, auth, conductor, config, daemon, db, dispatch,
                         hooks, http, merge, messaging, migrate, names, nod,
                         observer, paths, profile_edit, profiles, project, runway,
                         service, supervise, sweeper, traces, worktree)


def _here(con) -> tuple:
    """(project, merged config) for the current directory.

    The project is None outside any Work project: listing runs or profiles
    must still work from an arbitrary directory now that state is central.
    """
    proj = project.try_resolve(con, config.load())
    return proj, config.load(proj.project_id if proj else None)


def _require_project(con):
    """Same, but a command that has to run somewhere refuses to guess."""
    proj = project.resolve(con, config.load())
    return proj, config.load(proj.project_id)


def _requester(cfg: dict) -> str:
    return cfg.get("settings", {}).get("default_requester", "human")


def _gate_dispatch(con, cfg: dict, requester: str) -> None:
    """The pause switch is the one gate on a new run (DESIGN §4) — there is
    no concurrency cap here or anywhere else.

    DESIGN D2 seam: phase 4 inserts the budget-grant check here (an
    agent-initiated dispatch draws from a human-issued grant) before any
    run row is created.
    """
    state = dispatch.pause_state(con)
    if state is not None:
        raise SystemExit(
            f"dromond: dispatch is paused (since {state['at']}"
            + (f" — {state['note']}" if state.get("note") else "")
            + ").\nRun `dromond resume` to start new runs again.")


def _fetch_run(con, run_id: int):
    run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not run:
        raise SystemExit(f"dromond: no run {run_id}")
    return run


# --- commands ---------------------------------------------------------------

def cmd_init(args):
    """Central state means init creates nothing in the project (DESIGN §2):
    it makes sure the shared home exists and reports what this directory
    resolves to."""
    root = Path.cwd().resolve()
    if not (root / ".git").exists():
        res = subprocess.run(["git", "init", "--quiet", str(root)],
                             capture_output=True, text=True)
        if res.returncode != 0:
            raise SystemExit(
                f"dromond: cannot initialize git repository: {res.stderr.strip()}")
    gp = config.ensure_global_config()
    key, minted = http.ensure_key()
    con = db.connect()
    proj, _ = _here(con)
    con.close()
    # DESIGN §6: hooks are mandatory and install HERE, never by a separate
    # command — a backend without its hook cannot be told anything.
    try:
        hook_lines = hooks.install_all()
    except RuntimeError as exc:
        raise SystemExit(f"dromond: {exc}") from exc
    print(f"dromond: state is central at {paths.home()} — {root} gets no directory")
    print(f"  global config: {gp}")
    print(f"  database:      {paths.db_path()}")
    addr = http.bind_address()
    port = int(http.http_cfg().get("port") or http.DEFAULT_PORT)
    print(f"  dashboard:     http://{addr}:{port}/?key=… "
          f"(header {http.HEADER}; {http.KEY_ENV} overrides)")
    # Printed once, at the moment it is minted: it goes into the iOS app and
    # the browser by hand, and it is never printed or logged again.
    print(f"  api key:       {key}" if minted
          else "  api key:       already set (see [http] key in the config)")
    print("  hooks:")
    for line in hook_lines:
        print(f"    {line}")
    if proj:
        print(f"  project:       {proj.name or proj.work_id} ({proj.project_id})")
        print(f"  overrides:     [project.\"{proj.project_id}\"] in the global config")
        # Which profiles this project may STAFF (W-0187). Absent is every
        # profile, and saying so beats printing a list nobody wrote.
        enabled = config.load(proj.project_id).get("enabled_profiles")
        if enabled is None:
            said = "all (no enabled_profiles set)"
        else:
            said = ", ".join(enabled) or "NONE — nothing can be staffed here"
        print(f"  profiles:      {said}")
    else:
        print(f"  project:       {root} is not a known Work project — create it in "
              "Work, then re-run")


def cmd_dispatch(args):
    con = db.connect()
    proj, cfg = _require_project(con)
    root = proj.path
    requester = _requester(cfg)
    _gate_dispatch(con, cfg, requester)
    mission = " ".join(args.mission)
    if args.brief_file:
        mission = Path(args.brief_file).read_text()
    if not mission.strip():
        raise SystemExit("dromond: empty mission (pass text, or --brief-file)")
    # A staffing moment (W-0187): the project's enabled set gates it, and a
    # profile it has not enabled is refused by name rather than swapped.
    profile = config.staff_profile(cfg, args.to)
    title = args.title or mission.strip().splitlines()[0][:80]

    after_ids = []
    for rid in args.after or []:
        _fetch_run(con, rid)
        if rid not in after_ids:
            after_ids.append(rid)
    initial_status = "pending" if after_ids else "spawning"
    run_id, slug = None, None
    # The in-Python collision check is best-effort; the DB UNIQUE constraint
    # is the real guard — on violation, regenerate the slug and retry.
    for _ in range(names.MAX_ATTEMPTS + 4):
        slug = names.assign_slug(con)
        try:
            cur = con.execute(
                "INSERT INTO runs(slug, profile, backend, model, title, requested_by, "
                "workdir, project_id, status, started_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (slug, args.to, profile["backend"], profile.get("model"), title,
                 requester, str(root), proj.project_id, initial_status, db.now()))
            run_id = int(cur.lastrowid)
            break
        except sqlite3.IntegrityError as exc:
            if not names.is_unique_violation(exc):
                raise
            names.reset_memory_cache()
    if run_id is None:
        con.close()
        raise SystemExit("dromond: could not mint a unique run slug — retry dispatch")

    if after_ids:
        for rid in after_ids:
            con.execute(
                "INSERT INTO dispatch_dependencies(run_id, depends_on_run) VALUES(?,?)",
                (run_id, rid))
        con.execute(
            "INSERT INTO deferred_dispatches(run_id, mission, context, use_worktree, "
            "created_at) VALUES(?,?,?,?,?)",
            (run_id, mission, args.context, int(bool(args.worktree)), db.now()))
        con.commit()
        print(f"run {run_id} ({slug}): {args.to} queued after "
              f"{','.join(map(str, after_ids))}")
        # Prerequisites may already be settled; release immediately if so.
        supervise.process_ready(con, supervise.spawn_supervisor)
        con.close()
        return

    run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    try:
        supervise.prepare_launch(con, root, cfg, run, mission=mission,
                                 context=args.context, use_worktree=args.worktree)
        con.commit()
    except BaseException as exc:
        con.execute("UPDATE runs SET status='failed', summary=?, finished_at=? WHERE id=?",
                    (f"Launch setup failed: {exc}", db.now(), run_id))
        con.commit()
        con.close()
        raise
    run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    con.close()
    print(f"run {run_id} ({slug}): {args.to} "
          f"({profile['backend']}/{profile.get('model') or 'default'})"
          + (f" worktree={run['workdir']}" if run["branch"] else ""))
    if args.sync:
        sys.exit(supervise.supervise(root, run_id))
    supervise.spawn_supervisor(root, run_id)
    print(f"dispatched async. `dromond show {run_id}` for details.")


def _print_dispatch(state: dict) -> None:
    """The live run count sits beside the pause switch on purpose: seeing it
    and being able to stop it is what replaces a concurrency ceiling
    (DESIGN §4). There is no cap to print."""
    if state["paused"]:
        print(f"dispatch: PAUSED since {state['paused_at']}"
              + (f" — {state['pause_note']}" if state["pause_note"] else "")
              + f" · {state['live_runs']} live runs still going")
    else:
        print(f"dispatch: running · {state['live_runs']} live runs")
    if state["waiting"]:
        print(f"## waiting ({len(state['waiting'])}) — not started, not in_progress")
        for w in state["waiting"]:
            detail = f": {w['detail']}" if w["detail"] else ""
            print(f"  {w['item_id']} [{w['reason']}{detail}] since {w['enqueued_at']}")
    print()


def cmd_pause(args):
    con = db.connect()
    state = dispatch.pause(con, " ".join(args.note) or None)
    live = dispatch.live_runs(con)
    con.close()
    print(f"dromond: dispatch paused at {state['at']}"
          + (f" — {state['note']}" if state["note"] else ""))
    print(f"  {live} in-flight run(s) untouched; no new run starts until "
          "`dromond resume`.")


def cmd_resume(args):
    con = db.connect()
    was = dispatch.resume(con)
    state = dispatch.state(con)
    con.close()
    if was is None:
        print("dromond: dispatch was not paused")
    else:
        print(f"dromond: dispatch resumed (paused since {was['at']})")
    print(f"  {state['live_runs']} live runs, {len(state['waiting'])} waiting — "
          "the next sweep and the next daemon tick release them, in order.")


def cmd_status(args):
    con = db.connect()
    proj, _ = _here(con)
    here = f"{proj.name or proj.work_id} ({proj.path})" if proj else "no Work project"
    # Central state, so this is the whole workspace, not one project.
    print(f"dromond @ {paths.home()} — here: {here}\n")
    _print_dispatch(dispatch.state(con))
    active = list(con.execute(
        f"SELECT * FROM runs WHERE status NOT IN {db.TERMINAL_SQL} ORDER BY id"))
    print(f"## active runs ({len(active)})")
    for r in active:
        pending = [str(row["depends_on_run"]) for row in con.execute(
            "SELECT depends_on_run FROM dispatch_dependencies WHERE run_id=? "
            "ORDER BY depends_on_run", (r["id"],))] if r["status"] == "pending" else []
        label = f"pending-on-{','.join(pending)}" if pending else r["status"]
        print(f"  run {r['id']}: {r['profile']} [{label}] since {r['started_at']} — "
              f"{(r['title'] or '')[:50]}")
    recent = list(con.execute(
        f"SELECT * FROM runs WHERE status IN {db.TERMINAL_SQL} "
        "ORDER BY id DESC LIMIT 5"))
    if recent:
        print("## recent finished")
        for r in recent[::-1]:
            print(f"  run {r['id']}: {r['profile']} -> {r['status']} — "
                  f"{(r['title'] or '')[:50]}")
    con.close()


def cmd_runs(args):
    con = db.connect()
    where = [f"status NOT IN {db.TERMINAL_SQL}"] if args.active else []
    params = []
    if args.here:
        proj, _ = _here(con)
        where.append("project_id IS ?")
        params.append(proj.project_id if proj else None)
    rows = list(con.execute(
        "SELECT r.*, (SELECT work_id FROM projects p WHERE p.project_id=r.project_id "
        "LIMIT 1) AS project FROM runs r"
        + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id", params))
    if args.json:
        print(json.dumps([dict(r) for r in rows], indent=2))
        con.close()
        return
    if not rows:
        print("(no runs)")
        con.close()
        return
    print(f"{'id':<4} {'slug':<18} {'project':<16} {'profile':<10} "
          f"{'status':<10} {'started':<21} title")
    for r in rows:
        print(f"{r['id']:<4} {r['slug'] or '-':<18} {(r['project'] or '-')[:16]:<16} "
              f"{r['profile']:<10} {r['status']:<10} {r['started_at']:<21} "
              f"{(r['title'] or '')[:50]}")
    con.close()


def cmd_show(args):
    con = db.connect()
    r = _fetch_run(con, args.run_id)
    for k in r.keys():
        v = r[k]
        if k == "summary" and v:
            print(f"{k}:\n  " + v.replace("\n", "\n  "))
        else:
            print(f"{k}: {v}")
    deps = [str(row["depends_on_run"]) for row in con.execute(
        "SELECT depends_on_run FROM dispatch_dependencies WHERE run_id=? "
        "ORDER BY depends_on_run", (args.run_id,))]
    if deps:
        print("depends_on: " + ", ".join(deps))
    # DESIGN §6: an undelivered message is surfaced, not swallowed.
    for row in messaging.undeliverable(con, args.run_id):
        print(f"UNDELIVERED message {row['id']} from {row['sender']} "
              f"({row['undeliverable_reason']}):\n  "
              + row["body"].strip().replace("\n", "\n  "))
    con.close()


def _continuation_line(con, run_id: int):
    """Return a run and every session continuation descended from it."""
    return list(con.execute(
        "WITH RECURSIVE continuation_ids(id) AS ("
        "SELECT ? UNION SELECT r.id FROM runs r "
        "JOIN continuation_ids c ON r.parent_run=c.id) "
        "SELECT r.* FROM runs r JOIN continuation_ids c ON c.id=r.id ORDER BY r.id",
        (run_id,),
    ))


def cmd_reply(args):
    con = db.connect()
    parent_run = _fetch_run(con, args.run_id)
    cfg = config.load(parent_run["project_id"])
    root = project.root_for(con, parent_run)
    parent = _continuation_line(con, args.run_id)[-1]
    if not parent["session_ref"]:
        con.close()
        raise SystemExit(
            f"dromond: run {parent['id']} has no session ref; dispatch a fresh run")
    active = con.execute(
        f"SELECT id, status FROM runs WHERE session_ref=? AND status NOT IN {db.TERMINAL_SQL} "
        "ORDER BY id DESC LIMIT 1", (parent["session_ref"],)).fetchone()
    if active:
        con.close()
        raise SystemExit(
            f"dromond: run {args.run_id}'s session is already active as run "
            f"{active['id']} ({active['status']}) — use `dromond interrupt` instead")
    run_id = supervise.create_followup(
        con, root, dict(parent), _requester(cfg), " ".join(args.message))
    con.close()
    requested_note = (f" (requested from run {args.run_id})"
                      if parent["id"] != args.run_id else "")
    print(f"run {run_id}: continuing run {parent['id']}'s session with "
          f"{parent['profile']}{requested_note}")
    if args.sync:
        supervise.supervise(root, run_id)
    else:
        supervise.spawn_supervisor(root, run_id)


def cmd_interrupt(args):
    """`tell` and `interrupt` both land here: same row, same delivery. A run
    that already finished is NOT a target — DESIGN §6 forbids re-aiming a
    message at a later run, so the caller is sent to `reply` instead."""
    if args.message_file:
        try:
            body = Path(args.message_file).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise SystemExit(
                f"dromond: cannot read message file '{args.message_file}': {exc}") from exc
    else:
        body = " ".join(args.message)
    if not body.strip():
        raise SystemExit("dromond: the message must not be empty")
    con = db.connect()
    r = _fetch_run(con, args.run_id)
    cfg = config.load(r["project_id"])
    if r["status"] in db.RUN_TERMINAL:
        con.close()
        raise SystemExit(f"dromond: run {args.run_id} already {r['status']} — "
                         f"use `dromond reply {args.run_id} \"...\"` instead")
    if not r["session_ref"]:
        con.close()
        raise SystemExit(f"dromond: run {args.run_id}'s session isn't identified yet "
                         "(happens ~10s after spawn) — retry in a moment")
    # W-0104: an ACP run holds a live protocol channel, so there is no safe
    # boundary to queue behind and nothing to kill — the supervisor steers the
    # message into the running turn (Reasonix) or sends it as the next prompt
    # on the same session (OpenCode). The row carries no delivery_offset, so
    # the delivery state stops claiming a pending boundary.
    live = acp.run_transport(cfg, r["profile"]) == "acp"
    messaging.queue_tell(con, args.run_id, _requester(cfg), body, r["log_path"],
                         boundary=not live)
    if args.now:
        con.execute(f"UPDATE runs SET status='interrupt' WHERE id=? "
                    f"AND status NOT IN {db.TERMINAL_SQL}", (args.run_id,))
        con.commit()
        if r["pid"] and not live:
            try:
                os.killpg(r["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
        how = ("the worker's turn is cancelled gracefully over ACP and the same "
               "session continues with the message" if live else
               "the worker resumes its session with the message and continues "
               "the mission")
        print(f"run {args.run_id} interrupted now — {how}")
    else:
        con.commit()
        if live:
            when = ("mid-turn" if r["backend"] == "reasonix"
                    else "at the end of the current turn, on the same live session")
            print(f"message queued for run {args.run_id} over ACP — delivered "
                  f"{when}, with no kill and no resume")
        else:
            print(f"message queued for run {args.run_id}'s next safe action boundary; "
                  f"`dromond interrupt {args.run_id} \"...\" --now` for an emergency stop")
    con.close()


def cmd_ask(args):
    """The worker's blocking verb (DESIGN §6).

    It does not block THIS process: it files the question and returns. The
    session is what blocks — its Stop hook holds it open until the answer
    (or the declared fallback) comes back through `dromond hook`.
    """
    question = " ".join(args.question).strip()
    if not question:
        raise SystemExit("dromond: ask needs a question")
    if args.target not in ("human", "me", "owner"):
        raise SystemExit(
            f"dromond: ask can only target the human, not '{args.target}'. "
            "v1 peer-to-peer scope is human-to-run and parent-to-child; use "
            "`dromond tell <run> \"...\"` to send a run a message.")
    run_id = args.run_id or paths.env("DROMOND_RUN_ID") or None
    if not run_id:
        raise SystemExit("dromond: ask runs inside a run — pass --run RUN outside one")
    con = db.connect()
    run = _fetch_run(con, int(run_id))
    if run["status"] in db.RUN_TERMINAL:
        con.close()
        raise SystemExit(f"dromond: run {run_id} is already {run['status']}")
    cfg = config.load(run["project_id"])
    try:
        request_id, seconds = messaging.file_question(con, cfg, run, question)
    except (nod.NodError, nod.NodChannelError) as exc:
        # A worker must hear that its question did NOT go out, not a traceback.
        raise SystemExit(f"dromond: the question was not filed: {exc}") from None
    finally:
        con.close()
    print(f"question filed with the human as Nod request {request_id}. "
          f"End your turn now — the answer arrives as your next instruction. "
          f"If nobody answers within {seconds}s you will be told to proceed "
          f"on your own judgement.")


def cmd_hook(args):
    """The one hook binary Claude, Codex and Reasonix all run (DESIGN §6).

    Reads the harness's event JSON on stdin, prints the harness's answer on
    stdout. Prints nothing and touches nothing outside a Dromond run.
    """
    payload = {}
    if not sys.stdin.isatty():
        try:
            raw = sys.stdin.read()
            payload = json.loads(raw) if raw.strip() else {}
        except (OSError, UnicodeError, ValueError):
            payload = {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        text = hooks.run_hook(args.backend, payload, bind=args.bind,
                              event=args.event, session=args.session)
    except Exception as exc:  # a hook must never take the harness down with it
        print(f"dromond hook: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        text = None
    out = hooks.render(args.backend, text)
    if out:
        print(out)


def cmd_kill(args):
    con = db.connect()
    r = _fetch_run(con, args.run_id)
    if r["status"] in db.RUN_TERMINAL:
        print(f"run {args.run_id} already {r['status']}")
        con.close()
        return
    con.execute(
        "UPDATE deferred_dispatches SET status='cancelled', processed_at=? "
        "WHERE run_id=? AND status='pending'", (db.now(), args.run_id))
    con.execute(
        f"UPDATE runs SET status='killed', finished_at=? WHERE id=? "
        f"AND status NOT IN {db.TERMINAL_SQL}", (db.now(), args.run_id))
    con.commit()
    if r["pid"]:
        try:
            os.killpg(r["pid"], signal.SIGTERM)
            print(f"sent SIGTERM to run {args.run_id} (pgid {r['pid']})")
        except ProcessLookupError:
            print(f"run {args.run_id} marked killed (process already gone)")
    else:
        print(f"run {args.run_id} marked killed")
    # Dependents of a killed prerequisite are declined synchronously; no
    # supervisor may exist to do it for a pending run.
    supervise.process_ready(con, supervise.spawn_supervisor)
    con.close()


def cmd_check(args):
    """`dromond check <run>` — the spin observer's judgement, on demand.

    Same three layers and the same three outcomes as the scheduled look
    (DESIGN §7): a correction is delivered, a stop escalates with its
    reasoning, and everything else just prints.
    """
    con = db.connect()
    _fetch_run(con, args.run_id)  # exits with a clear message for a bad id
    try:
        result = http.check_run(con, args.run_id, observe=not args.mechanical)
    finally:
        con.close()
    print(f"run {args.run_id}: {result['verdict']}")
    for key in ("alive", "silent_for", "elapsed_seconds"):
        print(f"  {key}: {result[key]}")
    seen = result.get("observer") or {}
    if seen.get("error"):
        print(f"  observer: {seen['error']}")
    elif seen.get("skipped"):
        print(f"  observer: skipped — {seen['skipped']}")
    elif seen:
        print(f"  observer ({seen['action']}): {seen['reason']}")
    if result.get("loop"):
        print(f"  loop check ({result['loop']['action']}): {result['loop']['reason']}")


def _here_cfg() -> dict:
    """Config merged with the current directory's per-project overrides."""
    con = db.connect()
    try:
        return _here(con)[1]
    finally:
        con.close()


PICK = "\0pick"  # `--model` with no value means "show me the real list"


def _authority() -> str:
    """The CLI's half of the DESIGN §5 split.

    A worker's environment carries its own per-run token (W-0176); a human's
    shell does not. The token is checked against the database, so it is a
    credential and not a claim — but only at the HTTP surface is that
    containment: this process reads the config file and the database
    directly, so a worker that unsets the variable is limited by the
    filesystem, not by this function. The DROMOND_RUN_ID fallback stays for
    exactly that reason — it costs nothing and it still catches the honest
    worker whose token was never minted.
    """
    con = db.connect()
    try:
        if auth.run_from_env(con):
            return "agent"
    finally:
        con.close()
    return "agent" if paths.env("DROMOND_RUN_ID") else "human"


def _choose(label: str, choices: list[str], current=None, free: bool = False):
    """Numbered pick from a real list — the CLI half of the dashboard's
    picker. Never invents a list; ``free`` allows typing where discovery has
    nothing to offer (claude publishes no model listing)."""
    if not choices:
        if not free:
            raise SystemExit(f"dromond: nothing to pick for {label} — run "
                             "`dromond profiles discover` to see why")
        return input(f"{label} (typed; discovery has no list) "
                     f"[{current or ''}]: ").strip() or current
    print(f"\n{label}:")
    for i, choice in enumerate(choices, 1):
        print(f"  {i:>2}. {choice}" + ("  (current)" if choice == current else ""))
    while True:
        raw = input(f"pick 1-{len(choices)}"
                    + (f" [{current}]" if current else "") + ": ").strip()
        if not raw and current:
            return current
        if raw.isdigit() and 1 <= int(raw) <= len(choices):
            return choices[int(raw) - 1]
        print("  not one of the numbers above")


def _report(result: dict, name: str) -> None:
    if result.get("error"):
        raise SystemExit(f"dromond: {result['error']}")
    if not result.get("applied"):
        print(f"profile {name}: this change commits spend, so it went to the "
              f"human as a Work decision ({result.get('decision') or 'filed'}).")
        print("  needs: " + ", ".join(result.get("needs") or []))
        return
    if result.get("removed"):
        print(f"profile {name}: removed from {paths.global_config_path()}")
    elif result.get("unchanged"):
        print(f"profile {name}: already that way, nothing written")
    else:
        print(f"profile {name}: {', '.join(result['changed'])} "
              f"→ {paths.global_config_path()}")


def _profiles_set(args) -> None:
    """Add or edit one profile, with the same pickers the dashboard uses."""
    name = args.name
    existing = dict(config.load().get("profiles", {}).get(name) or {})
    options = profile_edit.discovery_options()
    changes: dict = {}

    backend = args.backend
    if backend == PICK or (backend is None and not existing):
        backend = _choose("harness", list(profile_edit.BACKENDS),
                          existing.get("backend"))
    if backend:
        changes["backend"] = backend
    chosen = backend or existing.get("backend") or "opencode"
    opts = options.get(chosen) or {}
    models = [m["id"] for m in opts.get("models") or []]

    model = args.model
    if model == PICK or (model is None and not existing):
        model = _choose(f"{chosen} model", models, existing.get("model"),
                        free=bool(opts.get("free_model")) or not models)
    if model:
        changes["model"] = model

    if opts.get("supports_effort") is False:
        if args.effort:
            raise SystemExit(f"dromond: {chosen} takes no effort — "
                             + str(opts.get("effort_note") or ""))
        if existing.get("effort"):
            changes["effort"] = ""  # a value the launch would silently drop
    else:
        effort = args.effort
        picked = next((m for m in opts.get("models") or []
                       if m["id"] == (model or existing.get("model"))), {})
        if effort == PICK:
            effort = _choose("effort", list(picked.get("efforts") or []),
                             existing.get("effort"),
                             free=bool(opts.get("free_effort")))
        if effort:
            changes["effort"] = effort

    for flag, key in (("variant", "variant"), ("tier", "tier"),
                      ("note", "note"), ("sandbox", "sandbox")):
        value = getattr(args, flag, None)
        if value is not None:
            changes[key] = value
    if args.priority is not None:
        changes["priority"] = args.priority
    if args.spawn is not None:
        changes["spawn_profiles"] = [n for n in args.spawn.split(",") if n.strip()]

    _report(profile_edit.save(name, changes, authority=_authority(),
                              options=options), name)


def cmd_profiles(args):
    if args.action == "note":
        _report(profile_edit.save(args.name, {"note": " ".join(args.text)},
                                  authority=_authority()), args.name)
        return
    if args.action == "set":
        return _profiles_set(args)
    if args.action == "rm":
        _report(profile_edit.save(args.name, {}, delete=True,
                                  authority=_authority()), args.name)
        return
    if args.action == "discover":  # needs no config; discovery asks the tools
        found = profiles.discover()
        print("dromond profiles discover — what the installed harnesses offer\n")
        oc = found["opencode"]
        print("## opencode (`opencode models`)")
        if oc["error"]:
            print(f"  unavailable: {oc['error']}")
        else:
            for provider, models in sorted(oc["data"].items()):
                shown = ", ".join(models[:8]) + \
                    (f", … +{len(models) - 8} more" if len(models) > 8 else "")
                print(f"  {provider} ({len(models)}): {shown}")
        cx = found["codex"]
        print("\n## codex (`codex debug models`)")
        if cx["error"]:
            print(f"  unavailable: {cx['error']}")
        else:
            for m in cx["data"]:
                efforts = "|".join(m["efforts"]) or "-"
                print(f"  {m['model']}  efforts: {efforts}"
                      + (f" (default {m['default_effort']})" if m["default_effort"] else ""))
        rx = found["reasonix"]
        print(f"\n## reasonix ({profiles.REASONIX_CONFIG})")
        if rx["error"]:
            print(f"  unavailable: {rx['error']}")
        else:
            for p in rx["data"]:
                efforts = "|".join(p["efforts"]) or "-"
                print(f"  {p['provider']}: {', '.join(p['models'])}  efforts: {efforts}"
                      + (f" (default {p['default_effort']})" if p["default_effort"] else ""))
        print(f"\n## claude\n  {found['claude']['error']}")
        return
    entries = _here_cfg().get("profiles", {})
    if not entries:
        print("(no profiles configured)")
        return
    # Routing order (W-0181): priority first, `nice`-style — lower is more
    # preferred — then name. The same order the dashboard and the planner see.
    print(f"{'name':<12} {'harness':<9} {'model':<24} {'effort':<7} "
          f"{'pri':<4} {'tier':<13} spawn")
    for name in sorted(entries, key=lambda n: (config.priority_of(entries[n]), n)):
        p = entries[name]
        spawn_list = p.get("spawn_profiles") or []
        tier = config.tier_of(p.get("tier"))
        print(f"{name:<12} {p.get('backend', 'opencode'):<9} "
              f"{p.get('model') or '(harness default)':<24} "
              f"{p.get('effort') or '-':<7} "
              f"{config.priority_of(p):<4} "
              f"{(f'{tier} {config.TIERS[tier]}' if tier else '-'):<13} "
              f"{', '.join(spawn_list) if spawn_list else '(no delegation)'}")
        age = profiles.note_age(p.get("note_at"))
        if p.get("note"):
            print(f"{'':<12} note: {p['note']}" + (f" ({age})" if age else ""))


def cmd_doctor(args):
    print("dromond doctor\n")
    for tool in ("codex", "claude", "opencode", "reasonix", "git"):
        path = shutil.which(tool)
        print(f"  {tool:<9} {'available · ' + path if path else 'not found'}")
    gp = paths.global_config_path()
    print(f"\n  global config: {gp} ({'present' if gp.is_file() else 'absent'})")
    print(f"  dromond home: {paths.home()}")
    con = db.connect()
    print(f"  database:     {paths.db_path()}")
    proj, cfg = _here(con)
    print(f"  here:         {proj if proj else 'not inside a known Work project'}")
    total = con.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"]
    active = con.execute(
        f"SELECT COUNT(*) AS n FROM runs WHERE status NOT IN {db.TERMINAL_SQL}"
    ).fetchone()["n"]
    known = con.execute(
        "SELECT COUNT(DISTINCT project_id) AS n FROM projects").fetchone()["n"]
    print(f"  runs: {total} total, {active} active across {known} cached projects")
    print(f"  profiles: {', '.join(sorted(cfg.get('profiles', {}))) or '(none)'}")
    # W-0189: a spin observer that cannot run means NOTHING is watching any
    # run, and that used to be silent. Doctor names the fix.
    for line in observer.status_report(cfg):
        print(line)
    print(f"  service:  {service.plist_path()} "
          f"({'installed' if service.plist_path().exists() else 'not installed'})")
    # DESIGN §6: a backend whose hook is missing (or whose Codex trust was
    # never provisioned) cannot be told anything, so doctor says so plainly.
    for line in hooks.hook_report():
        print(line)
    stranded = messaging.undeliverable(con)
    if stranded:
        runs_hit = sorted({int(r["run_id"]) for r in stranded})
        print(f"  messages: {len(stranded)} undelivered on run(s) "
              + ", ".join(str(r) for r in runs_hit))
    channels = nod.from_cfg(cfg)
    print("  nod:      " + (", ".join(
        f"{role} {'configured' if channels and role in channels.configured else 'unconfigured'}"
        for role in nod.ROLES) if cfg.get("nod", {}).get("enabled")
        else "off ([nod] enabled = false)"))
    # Whether a secret exists, never the secret.
    port = int(http.http_cfg(cfg).get("port") or http.DEFAULT_PORT)
    surface = (f"http://{http.bind_address(cfg)}:{port}/" if http.load_key(cfg)
               else "off — no shared secret; run `dromond init`")
    print(f"  http:     {surface}")
    legacy = migrate.find_legacy_db(Path.cwd())
    if legacy:
        print(f"  legacy state: {legacy} — fold it in with `dromond migrate`")
    con.close()


def cmd_sweep(args):
    cfg = config.load()  # per-project overrides resolve per swept item
    client = sweeper.client_from_cfg(cfg)
    if client is None:
        raise SystemExit(
            "dromond: the sweeper is off — set [work] enabled = true and "
            f"api_url in {paths.global_config_path()}")
    if args.watch:
        interval = args.interval or int(sweeper.work_cfg(cfg).get("poll_interval", 60))
        print(f"dromond sweep: watching {client.api_url} as "
              f"'{client.identity}' every {interval}s")
        sweeper.watch(cfg, client, interval)
        return
    actions = sweeper.sweep(cfg, client)
    if not actions:
        print("sweep: nothing to do")
    for a in actions:
        detail = {k: v for k, v in a.items() if k not in ("action", "item", "run")}
        print(f"sweep: {a['action']} {a['item']} ↔ run {a['run']}"
              + (f" {detail}" if detail else ""))


def cmd_work_status(args):
    con = db.connect()
    rows = list(con.execute(
        "SELECT * FROM runs WHERE work_item IS NOT NULL ORDER BY id"))
    con.close()
    if not rows:
        print("(no runs are mapped to Work items)")
        return
    print(f"{'item':<14} {'run':<5} {'slug':<18} {'status':<10} reported")
    for r in rows:
        print(f"{r['work_item']:<14} {r['id']:<5} {r['slug'] or '-':<18} "
              f"{r['status']:<10} {r['work_reported_at'] or '-'}")


def cmd_daemon(args):
    sys.exit(daemon.run(interval=args.interval, once=args.once))


def cmd_service(args):
    if args.action == "install":
        sys.exit(service.install(start=args.start))
    if args.action == "uninstall":
        sys.exit(service.uninstall())
    if args.action == "restart":
        sys.exit(service.restart())
    sys.exit(service.status())


def cmd_migrate(args):
    """Fold a legacy per-project database into ~/.dromond. Copies only."""
    start = Path(args.path).expanduser().resolve() if args.path else Path.cwd()
    legacy = migrate.find_legacy_db(start)
    if legacy is None:
        raise SystemExit(f"dromond: no legacy {paths.LEGACY_STATE_DIR}/{paths.LEGACY_STATE_DB} "
                         f"at or above {start} — nothing to migrate")
    con = db.connect()
    project_id = args.project_id
    if not project_id:
        proj = project.try_resolve(con, config.load(), str(legacy.parent.parent))
        project_id = proj.project_id if proj else None
    if not project_id:
        print(f"dromond migrate: {legacy.parent.parent} maps to no Work project; "
              "runs come across without a project id (pass --project-id to set one)")
    report = migrate.fold(legacy, project_id, con)
    con.close()
    print(f"dromond migrate: folded {legacy} into {paths.db_path()}")
    for key in ("runs", "messages", "dispatch_dependencies", "deferred_dispatches"):
        print(f"  {key}: {report[key]}")
    if report["dropped_slugs"]:
        print(f"  slugs dropped as duplicates: {report['dropped_slugs']} "
              "(run ids are the address)")
    print(f"  not copied: {', '.join(report['skipped_tables'])} "
          "(Work cursors and schema version stay the central database's own)")
    print(f"  nothing was deleted — remove {legacy.parent} yourself when satisfied")

def cmd_runway(args):
    """DESIGN §11: poll every provider adapter, store the poll, print it.
    Never fails on a provider — an adapter outage prints as unknown."""
    results = runway.poll_all(config.load())
    con = db.connect()
    runway.record(con, results)
    con.close()
    if args.json:
        print(json.dumps([r.as_dict() for r in results], indent=2))
        return
    print(f"{'provider':<10} {'window':<8} {'remaining':<14} {'resets':<14} note")
    for r in results:
        for line in runway.format_lines(r):  # one row per window (W-0179)
            print(line)


def _dur(seconds: float | None) -> str:
    if not seconds:
        return "0m"
    minutes = int(seconds) // 60
    return f"{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


def _stat(value, money: bool = False) -> str:
    """Null is "not captured" and prints as such — never as a zero."""
    if value is None:
        return "–"
    return f"${value:,.4f}" if money else f"{value:,}"


def _spend(value, billing: str) -> str:
    """A plan-backed run has no price (W-0179). "plan" says so; a 0 or a bare
    dash both read as free work."""
    return "plan" if billing == "plan" else _stat(value, money=True)


def cmd_stats(args):
    """DESIGN §11 statistics, the same numbers the dashboard shows — it is
    the same function. Tokens/cost read the run rows the supervisor stamped
    at completion; a dash means no backend usage was captured."""
    con = db.connect()
    stats = http._statistics(con)  # one owner for the numbers, two surfaces
    con.close()
    if args.json:
        print(json.dumps(stats, indent=2))
        return
    print(f"runs         {stats['runs_total']} total, {stats['runs_active']} active")
    print(f"worker time  {_dur(stats['worker_seconds'])}")
    print(f"tokens       {_stat(stats['tokens_total'])}")
    print(f"cost         {_stat(stats['cost_usd'], money=True)}"
          + (f"  ({stats['plan_runs']} plan-backed runs have no price)"
             if stats.get("plan_runs") else ""))
    by_status = "  ".join(f"{k} {stats['by_status'][k]}"
                          for k in sorted(stats["by_status"])) or "–"
    print(f"by status    {by_status}\n")
    print(f"{'profile':<20} {'runs':>5} {'active':>7} {'time':>9} "
          f"{'tokens':>14} {'cost':>12}")
    for p in stats["by_profile"]:
        print(f"{p['profile'][:20]:<20} {p['runs']:>5} {p['active']:>7} "
              f"{_dur(p['seconds']):>9} {_stat(p['tokens']):>14} "
              f"{_spend(p['cost'], p.get('billing', 'api')):>12}")


def cmd_merge(args):
    criteria = Path(args.criteria_file).read_text() if args.criteria_file else ""
    con = db.connect()
    proj = project.resolve(con, config.load())
    # The by-hand retry judges tripwires against the same mission the
    # automatic landing does — the row is found by its branch name.
    row = con.execute("SELECT * FROM runs WHERE branch=? ORDER BY id DESC LIMIT 1",
                      (args.branch,)).fetchone()
    mission = merge.run_mission(dict(row)) if row else ""
    con.close()
    result = merge.merge_run(proj.path, args.branch, criteria=criteria,
                             mission=mission,
                             item_id=args.item,
                             settings=config.load(proj.project_id))
    print(json.dumps(result, indent=2))
    if not result["ok"]:
        raise SystemExit(1)


def cmd_prune(args):
    """W-0172: sweep run worktrees nobody owns. A live run's checkout is
    skipped whatever it looks like."""
    con = db.connect()
    report = worktree.prune(con, force=args.force)
    con.close()
    removed = 0
    for wt in report["worktrees"]:
        if wt["removed"]:
            removed += 1
            print(f"prune: removed {wt['workdir']}")
            for lost in wt["discarded"]:
                print(f"  discarded: {lost}")
        elif wt["kept"]:
            print(f"prune: kept {wt['workdir']} — {wt['kept']}")
        else:
            print(f"prune: FAILED {wt['workdir']} — {wt['error']}")
    for d in report["dirs"]:
        print(f"prune: removed empty project directory {d}")
    kept = [wt for wt in report["worktrees"] if not wt["removed"]]
    print(f"prune: {removed} worktree(s) removed, {len(kept)} kept, "
          f"{len(report['dirs'])} empty directory(ies) removed")
    if any(wt["kept"] and not wt.get("live") for wt in kept) and not args.force:
        print("prune: pass --force to remove those and discard what they hold "
              "(a live run's worktree is never removed)")


def cmd_project(args):
    """Address a directory without Work. DESIGN §2: state is central, so this
    writes a row in ~/.dromond, never a marker file in the project."""
    con = db.connect()
    try:
        if args.action == "list":
            rows = project.all_projects(con)
            if not rows:
                print("no projects. `dromond project add .` registers this one.")
                return
            width = max(len(str(r.path)) for r in rows)
            for r in rows:
                source = "work" if r.work_id else "local"
                print(f"{str(r.path):<{width}}  {source:<5}  {r.name or ''}")
            return
        if args.action == "forget":
            target = Path(args.path or ".").expanduser().resolve()
            print(f"forgot {target}" if project.forget(con, target)
                  else f"dromond: {target} was not registered")
            return
        adopted = project.adopt(con, Path(args.path or "."), args.name)
        print(f"{adopted.path}\n  project id: {adopted.project_id}"
              f"\n  name:       {adopted.name}")
        print("\nDispatch into it with:\n"
              f"  dromond dispatch --to <profile> \"<mission>\"")
    finally:
        con.close()


def cmd_traces(args):
    """DESIGN §7 retention: normalized events live forever, raw logs do not.
    A live run is never touched — only terminal runs age out."""
    con = db.connect()
    if args.action == "messages":
        rows = traces.run_messages(con, args.run_id)
        con.close()
        print(json.dumps(rows, indent=2))
        return
    days = args.days if args.days is not None else traces.retention_days(config.load())
    pruned = traces.prune_raw_logs(con, days=days, dry_run=args.dry_run)
    con.close()
    if not pruned:
        print(f"traces: no raw log older than {days}d on a terminal run")
        return
    total = sum(p["bytes"] for p in pruned)
    verb = "would prune" if args.dry_run else "pruned"
    for p in pruned:
        note = f" ({p['error']})" if p.get("error") else ""
        print(f"traces: {verb} run {p['run_id']} {p['log_path']} "
              f"{p['bytes']}B{note}")
    print(f"traces: {verb} {len(pruned)} raw log(s), {total}B "
          "(normalized events kept)")


def _nod_or_exit() -> "nod.Nod":
    channels = nod.from_cfg(config.load())
    if channels is None:
        raise SystemExit(
            "dromond: the human loop is off — set [nod] enabled = true in "
            f"{paths.global_config_path()}, and write base_url plus a "
            f"<channel>_channel/<channel>_token pair to "
            f"{nod.DEFAULT_SECRETS_FILE} (chmod 600)")
    return channels


def _nod_client_for(channels, con, args):
    """The channel client for a request id the user named.

    ``--channel`` wins; otherwise the channel recorded when the card was
    filed decides. A token only works for its own channel, so this never
    falls back to trying both.
    """
    if args.channel:
        return channels.for_role(args.channel)
    return channels.for_request(con, args.request_id)


def cmd_nod(args):
    """Manual surface for the human loop (DESIGN §8). Not wired to runs yet."""
    channels = _nod_or_exit()
    con = db.connect()
    try:
        if args.action == "status":
            print(f"nod: {channels.base_url}")
            for role in nod.ROLES:
                client = channels.clients.get(role)
                print(f"  {role:<10} "
                      + (f"channel {client.channel_id}" if client
                         else "unconfigured (no token/channel pair)"))
            try:
                nod.health(channels.base_url, timeout=5)
                print("  health     reachable")
            except nod.NodError as exc:
                print(f"  health     {exc}")
        elif args.action == "test":
            got = nod.alert(
                channels, "Dromond can reach this Nod server and issue requests.\n\n"
                          "Nothing is wrong. Dismiss this card.",
                title="Dromond: Nod configuration check", con=con)
            print(f"filed {got['request_id']} to the {nod.ALERTS} channel"
                  + (" (deduped)" if got.get("deduped") else ""))
        elif args.action == "show":
            client = _nod_client_for(channels, con, args)
            view = client.decision(args.request_id)
            decision = view.get("decision") or {}
            print(f"{view.get('request_id', args.request_id)}  {view.get('status')}"
                  f"  [{client.role}]")
            if decision:
                print(f"  option: {decision.get('option_id')} "
                      f"({decision.get('option_kind')})")
                if decision.get("text"):
                    print(f"  text:   {decision['text']}")
                print(f"  at:     {decision.get('resolved_at')}")
            row = con.execute("SELECT * FROM nod_requests WHERE request_id=?",
                              (args.request_id,)).fetchone()
            if row:
                print(f"  local:  kind={row['kind']} run={row['run_id']} "
                      f"item={row['work_item']}")
        elif args.action == "cancel":
            client = _nod_client_for(channels, con, args)
            client.cancel(args.request_id)
            print(f"cancelled {args.request_id} on the {client.role} channel")
    except (nod.NodError, nod.NodChannelError) as exc:
        raise SystemExit(f"dromond: {exc}")
    finally:
        con.close()


def cmd_supervise(args):
    # W-0099: this process is where a completion files proposals and where a
    # judgment failure is noticed, so it is where both planner seams attach.
    conductor.attach()
    sys.exit(supervise.supervise(Path(args.root), args.run_id))


# --- parser -----------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        prog="dromond",
        description="Local control plane: dispatch codex/claude/opencode/reasonix "
                    "workers, supervise them to completion, tell/ask/reply/kill.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="prepare the central ~/.dromond home and "
                                    "report this directory's Work project")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("dispatch", help="dispatch a mission to a worker profile, async")
    s.add_argument("mission", nargs="*")
    s.add_argument("--to", required=True, metavar="PROFILE", help="launch profile name")
    s.add_argument("--after", type=int, action="append", metavar="RUN",
                   help="launch only after this run succeeds (repeatable)")
    s.add_argument("--brief-file", help="read the mission from a file")
    s.add_argument("--context", help="extra context appended to the brief")
    s.add_argument("--title")
    s.add_argument("--worktree", action="store_true",
                   help="run in an isolated git worktree")
    s.add_argument("--sync", action="store_true", help="supervise in the foreground")
    s.set_defaults(fn=cmd_dispatch)

    s = sub.add_parser("status", help="workspace overview: dispatch state, "
                                      "live run count, waiting items, runs")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("pause", help="stop new runs starting; in-flight runs "
                                     "are untouched (DESIGN §4)")
    s.add_argument("note", nargs="*", help="why, shown wherever the pause is")
    s.set_defaults(fn=cmd_pause)

    s = sub.add_parser("resume", help="allow new runs to start again")
    s.set_defaults(fn=cmd_resume)

    s = sub.add_parser("runs", help="list runs across every project")
    s.add_argument("--active", action="store_true")
    s.add_argument("--here", action="store_true",
                   help="only this directory's project")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_runs)

    s = sub.add_parser("show", help="run details")
    s.add_argument("run_id", type=int)
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("reply", help="continue a finished run's backend session")
    s.add_argument("run_id", type=int)
    s.add_argument("message", nargs="+")
    s.add_argument("--sync", action="store_true")
    s.set_defaults(fn=cmd_reply)

    # `tell` and `interrupt` write the SAME row: tell is DESIGN §6's name for
    # the non-blocking, safe-boundary delivery, interrupt --now is the
    # emergency stop variant of it.
    s = sub.add_parser("tell", help="send a running worker a message, delivered "
                                    "at its next safe boundary (DESIGN §6)")
    s.add_argument("run_id", type=int)
    s.add_argument("message", nargs="*")
    s.add_argument("--file", dest="message_file", help="read the message from a file")
    s.set_defaults(fn=cmd_interrupt, now=False)

    s = sub.add_parser("ask", help="ask the human a blocking question; the run's "
                                   "session is held open until Nod answers")
    s.add_argument("target", help="who to ask — 'human'")
    s.add_argument("question", nargs="+")
    s.add_argument("--run", dest="run_id", type=int,
                   help="the asking run (default: $DROMOND_RUN_ID)")
    s.set_defaults(fn=cmd_ask)

    s = sub.add_parser("hook", help="internal: the lifecycle hook every backend "
                                    "runs; installed by `dromond init`")
    s.add_argument("--backend", default="claude",
                   choices=("claude", "codex", "opencode", "reasonix"))
    s.add_argument("--bind", action="store_true",
                   help="SessionStart: record the harness session id")
    s.add_argument("--event", help="the event name, when the harness cannot "
                                   "put it on stdin (OpenCode's plugin)")
    s.add_argument("--session", help="harness session id, same reason")
    s.set_defaults(fn=cmd_hook)

    s = sub.add_parser("interrupt",
                       help="deliver a message to a running worker (guaranteed)")
    s.add_argument("run_id", type=int)
    s.add_argument("message", nargs="*")
    s.add_argument("--file", dest="message_file", help="read the message from a file")
    s.add_argument("--now", action="store_true",
                   help="stop immediately instead of at the next safe boundary")
    s.set_defaults(fn=cmd_interrupt)

    s = sub.add_parser("kill", help="stop a run")
    s.add_argument("run_id", type=int)
    s.set_defaults(fn=cmd_kill)

    s = sub.add_parser("check", help="judge a run now: stall, loop, and a cheap "
                                     "out-of-band observer turn (DESIGN §7)")
    s.add_argument("run_id", type=int)
    s.add_argument("--mechanical", action="store_true",
                   help="skip the observer turn; liveness and loop shape only")
    s.set_defaults(fn=cmd_check)

    s = sub.add_parser("sweep", help="one Work intervention pass (CONTRACT §4)")
    s.add_argument("--watch", action="store_true", help="keep sweeping")
    s.add_argument("--interval", type=int,
                   help="watch heartbeat in seconds (fallback signal only)")
    s.set_defaults(fn=cmd_sweep)

    s = sub.add_parser("work-status", help="Work item ↔ run mapping")
    s.set_defaults(fn=cmd_work_status)

    s = sub.add_parser("profiles",
                       help="list, add, edit and remove launch profiles; "
                            "discover models; set notes")
    s.set_defaults(fn=cmd_profiles, action=None)
    psub = s.add_subparsers(dest="action")
    psub.add_parser("discover",
                    help="enumerate models/efforts the installed harnesses offer")
    pn = psub.add_parser("note", help="set a profile's headroom note")
    pn.add_argument("name", metavar="PROFILE")
    pn.add_argument("text", nargs="+", help='e.g. "10%% weekly left, resets Sunday"')

    # Parity with the dashboard editor: the same pickers, the same config
    # file, the same authority split. A bare --model/--effort/--backend
    # offers the real list rather than taking a typed string.
    ps = psub.add_parser("set", help="add or edit a profile (writes the config file)")
    ps.add_argument("name", metavar="PROFILE")
    for flag, helptext in (("backend", "harness: opencode|codex|claude|reasonix"),
                           ("model", "model id, from discovery"),
                           ("effort", "reasoning effort the model declares")):
        ps.add_argument(f"--{flag}", nargs="?", const=PICK, help=helptext)
    ps.add_argument("--variant", help="opencode's stand-in for effort")
    ps.add_argument("--tier", help="1 workhorse | 2 generalist | 3 heavy; "
                                   "tier 1 volunteers the observer, tier 2 the planner")
    ps.add_argument("--priority", type=int,
                    help="0-99, like a linux nice value: LOWER is more "
                         "preferred. Orders profiles of the same tier (default 50)")
    ps.add_argument("--sandbox", help="codex execution sandbox")
    ps.add_argument("--spawn", metavar="A,B",
                    help="delegation allowlist; every name must already exist "
                         "(empty string clears it)")
    ps.add_argument("--note", help="headroom note; its age is stamped now")

    pr = psub.add_parser("rm", help="remove a profile from the config file")
    pr.add_argument("name", metavar="PROFILE")

    s = sub.add_parser("runway", help="provider quota/balance, stored per poll")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_runway)

    s = sub.add_parser("stats", help="runs, worker time, tokens and cost per "
                                     "profile (DESIGN §11)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_stats)

    s = sub.add_parser("merge", help="verify a run branch and land it on the base (DESIGN §9)")
    s.add_argument("branch", help="run branch, e.g. dromond/run-7")
    s.add_argument("--item", help="Work item id, for the merge commit message")
    s.add_argument("--criteria-file", help="acceptance criteria file for the agent review")
    s.set_defaults(fn=cmd_merge)

    s = sub.add_parser("prune", help="remove run worktrees nobody owns and the "
                                     "empty project directories they leave")
    s.add_argument("--force", action="store_true",
                   help="also remove worktrees holding uncommitted or unmerged "
                        "work, reporting what was discarded")
    s.set_defaults(fn=cmd_prune)

    s = sub.add_parser("nod", help="the human loop: file/inspect Nod decision cards")
    s.set_defaults(fn=cmd_nod)
    nsub = s.add_subparsers(dest="action", required=True)
    nsub.add_parser("status", help="which channels have a token, and is Nod up")
    nsub.add_parser("test", help="file a dismiss-only alert to prove config works")
    for action, helptext in (("show", "read a request's decision"),
                             ("cancel", "withdraw a pending request")):
        ns = nsub.add_parser(action, help=helptext)
        ns.add_argument("request_id")
        ns.add_argument("--channel", choices=nod.ROLES,
                        help="channel the request was filed to; default: the "
                             "channel recorded when Dromond filed it")

    s = sub.add_parser("doctor", help="check tools and config health")
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("daemon", help="run the control plane in the foreground "
                                      "(sweeper + supervision, DESIGN §2)")
    s.add_argument("--interval", type=int, help="seconds between ticks")
    s.add_argument("--once", action="store_true", help="one tick, then exit")
    s.set_defaults(fn=cmd_daemon)

    s = sub.add_parser("service", help="manage the launchd LaunchAgent for the daemon")
    s.add_argument("action", choices=("install", "uninstall", "status", "restart"))
    s.add_argument("--start", action="store_true",
                   help="also load and start it now (install only)")
    s.set_defaults(fn=cmd_service)

    s = sub.add_parser("migrate", help="fold a legacy per-project .maestro database "
                                       "into ~/.dromond (copies; deletes nothing)")
    s.add_argument("path", nargs="?", help="directory to search upward from")
    s.add_argument("--project-id", help="Work projectId to stamp on the copied runs")
    s.set_defaults(fn=cmd_migrate)

    s = sub.add_parser("project", help="register a directory Dromond may "
                                       "dispatch into, without Work")
    s.set_defaults(fn=cmd_project, action="add", path=None, name=None)
    psub = s.add_subparsers(dest="action")
    pa = psub.add_parser("add", help="adopt a directory as a project")
    pa.add_argument("path", nargs="?", help="default: the current directory")
    pa.add_argument("--name", help="default: the directory's own name")
    pl = psub.add_parser("list", help="every project, from Work or adopted here")
    pl.set_defaults(path=None, name=None)
    pf = psub.add_parser("forget", help="drop a locally adopted project")
    pf.add_argument("path", nargs="?", help="default: the current directory")
    pf.set_defaults(name=None)

    s = sub.add_parser("traces", help="run traces: raw-log retention and "
                                      "a run's inbox/outbox (DESIGN §7)")
    s.set_defaults(fn=cmd_traces, action="prune", run_id=None, days=None,
                   dry_run=False)
    tsub = s.add_subparsers(dest="action")
    tp = tsub.add_parser("prune", help="age out raw backend logs of TERMINAL "
                                       "runs; normalized events are kept")
    tp.add_argument("--days", type=int,
                    help="override settings.raw_log_retention_days")
    tp.add_argument("--dry-run", action="store_true")
    tp.set_defaults(run_id=None)
    tm = tsub.add_parser("messages", help="one run's messages, badged "
                                          "queued/delivered/answered")
    tm.add_argument("run_id", type=int)
    tm.set_defaults(days=None, dry_run=False)

    s = sub.add_parser("_supervise")  # internal: detached supervisor entry
    s.add_argument("run_id", type=int)
    s.add_argument("--root", required=True)
    s.set_defaults(fn=cmd_supervise)

    args = p.parse_args()
    args.fn(args)
