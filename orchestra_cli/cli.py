import argparse
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path

from orchestra_cli import (
    brief,
    checkpoint,
    config,
    db,
    docs,
    host,
    names,
    paths,
    projects,
    runners,
    supervise,
    tailscale,
    worktree,
)
from orchestra_cli.usage import (
    assess_targets,
    default_service,
    infer_from_agent,
    infer_provider,
    render_warning_lines,
)


def _identity(args, cfg) -> str:
    return getattr(args, "as_", None) or os.environ.get("ORCHESTRA_SELF") \
        or cfg["settings"].get("default_requester", "orchestrator")


_spawn_supervisor = supervise.spawn_supervisor


def _work_available() -> bool:
    return shutil.which("work") is not None


def _work_log(root: Path, item: str | None, text: str) -> None:
    if item and _work_available():
        try:
            subprocess.run(["work", "log", item, text], cwd=root, capture_output=True, timeout=20)
        except Exception:
            pass


# --- commands --------------------------------------------------------------

def cmd_init(args):
    root = Path.cwd().resolve()
    sd = root / paths.STATE_DIR
    sd.mkdir(exist_ok=True)
    (sd / ".gitignore").write_text(docs.STATE_GITIGNORE)
    if not (sd / "config.toml").exists():
        (sd / "config.toml").write_text(docs.PROJECT_CONFIG_STUB)
    gp = config.ensure_global_config()
    db.connect(root).close()
    if not (root / "ORCHESTRA.md").exists():
        (root / "ORCHESTRA.md").write_text(docs.ORCHESTRA_MD)
    for doc in ["AGENTS.md", "CLAUDE.md"]:
        p = root / doc
        text = p.read_text() if p.exists() else ""
        if "<!-- orchestra -->" not in text:
            p.write_text(text + docs.POINTER)
    if args.work and _work_available() and not (root / ".work").is_dir():
        subprocess.run(["work", "init", str(root)], cwd=root)
    # Register the freshly-initialized root in the multi-project
    # allowlist so `orchestra ui` started anywhere lists it. Idempotent:
    # re-running `orchestra init` keeps the same canonical id and only
    # refreshes the on-disk entry.
    try:
        projects.register(root)
    except Exception as exc:  # pragma: no cover - best-effort
        print(f"  note: could not register project in picker: {exc}")
    print(f"orchestra: initialized {sd}")
    print(f"  global roster config: {gp}")
    print(f"  playbook: {root / 'ORCHESTRA.md'} (pointers added to AGENTS.md / CLAUDE.md)")
    if not (root / ".work").is_dir():
        print("  note: no .work workspace here — run `work init .` (or `orchestra init --work`) "
              "so missions can be tracked durably")


def cmd_roster(args):
    cfg = config.load(_maybe_root())
    print(f"{'agent':<12} {'backend':<9} {'model':<42} role")
    for name, a in sorted(cfg.get("agents", {}).items()):
        model = a.get("model", "(backend default)")
        flags = " [ensemble]" if a.get("ensemble") else ""
        print(f"{name:<12} {a.get('backend', '?'):<9} {model:<42} {a.get('role', '')}{flags}")


def _maybe_root() -> Path | None:
    try:
        return paths.find_root()
    except SystemExit:
        return None


def cmd_team(args):
    root = paths.find_root()
    con = db.connect(root)
    if args.team_cmd == "create":
        con.execute("INSERT OR IGNORE INTO teams(name, about, created_at) VALUES(?,?,?)",
                    (args.name, args.about or "", db.now()))
        tid = con.execute("SELECT id FROM teams WHERE name=?", (args.name,)).fetchone()["id"]
        for a in args.agents or []:
            con.execute("INSERT OR IGNORE INTO members(team_id, agent) VALUES(?,?)", (tid, a))
        con.commit()
        print(f"team '{args.name}' ready" + (f" with {args.agents}" if args.agents else ""))
    elif args.team_cmd == "add":
        row = con.execute("SELECT id FROM teams WHERE name=?", (args.name,)).fetchone()
        if not row:
            raise SystemExit(f"orchestra: no team '{args.name}'")
        for a in args.agents:
            con.execute("INSERT OR IGNORE INTO members(team_id, agent) VALUES(?,?)", (row["id"], a))
        con.commit()
        print(f"added {args.agents} to '{args.name}'")
    else:  # list
        for t in con.execute("SELECT * FROM teams ORDER BY name"):
            members = [r["agent"] for r in con.execute(
                "SELECT agent FROM members WHERE team_id=?", (t["id"],))]
            print(f"{t['name']}: {', '.join(members) or '(empty)'}  {('— ' + t['about']) if t['about'] else ''}")


def cmd_send(args):
    root = paths.find_root()
    cfg = config.load(root)
    con = db.connect(root)
    sender = _identity(args, cfg)
    con.execute("INSERT INTO messages(sender, recipient, body, work_item, run_id, created_at) "
                "VALUES(?,?,?,?,?,?)",
                (sender, args.to, args.body, args.work, args.run, db.now()))
    con.commit()
    print(f"sent {sender} -> {args.to}")


def cmd_broadcast(args):
    root = paths.find_root()
    cfg = config.load(root)
    con = db.connect(root)
    sender = _identity(args, cfg)
    row = con.execute("SELECT id FROM teams WHERE name=?", (args.team,)).fetchone()
    if not row:
        raise SystemExit(f"orchestra: no team '{args.team}'")
    members = [r["agent"] for r in con.execute("SELECT agent FROM members WHERE team_id=?", (row["id"],))]
    n = 0
    for m in members:
        if m == sender:
            continue
        con.execute("INSERT INTO messages(sender, recipient, body, work_item, created_at) "
                    "VALUES(?,?,?,?,?)", (sender, m, f"[broadcast:{args.team}] {args.body}",
                                          args.work, db.now()))
        n += 1
    con.commit()
    print(f"broadcast to {n} member(s) of '{args.team}'")


def cmd_inbox(args):
    root = paths.find_root()
    cfg = config.load(root)
    con = db.connect(root)
    who = args.name or _identity(args, cfg)
    q = "SELECT * FROM messages WHERE recipient=?"
    if not args.all:
        q += " AND read_at IS NULL" if args.unread else ""
    rows = list(con.execute(q + " ORDER BY id", (who,)))
    if not args.all and not args.unread:
        rows = [r for r in rows if r["read_at"] is None]
    if args.json:
        print(json.dumps([dict(r) for r in rows], indent=2))
    elif not rows:
        print(f"(inbox '{who}' empty)")
    else:
        for r in rows:
            tag = "" if r["read_at"] is None else " (read)"
            extra = " ".join(x for x in [f"work:{r['work_item']}" if r["work_item"] else "",
                                         f"run:{r['run_id']}" if r["run_id"] else ""] if x)
            print(f"[{r['id']}] {r['created_at']} from {r['sender']}{tag} {extra}\n  {r['body']}\n")
    if args.mark_read and rows:
        con.execute(f"UPDATE messages SET read_at=? WHERE id IN "
                    f"({','.join(str(r['id']) for r in rows)}) AND read_at IS NULL", (db.now(),))
        con.commit()


def _quota_warnings_enabled(cfg: dict) -> bool:
    """Settings.quota_warn. False opts out; default is on."""
    return bool(cfg.get("settings", {}).get("quota_warn", True))


def _resolve_quota_targets(cfg: dict, targets: list[str]) -> list[tuple[str, str | None]]:
    """Map each --to target to a provider id. Ensemble leads add every model
    in their `model_pool` so the warning fires for every provider the team
    might spin up."""
    resolved: list[tuple[str, str | None]] = []
    for name in targets:
        agent = config.agent_cfg(cfg, name)
        primary = infer_from_agent(agent)
        if isinstance(primary, str):
            resolved.append((name, primary))
        model_pool = agent.get("model_pool")
        if isinstance(model_pool, list):
            for model_id in model_pool:
                if isinstance(model_id, str):
                    backend = agent.get("backend") if isinstance(agent.get("backend"), str) else None
                    inferred = infer_provider(backend, model_id)
                    if isinstance(inferred, str) and inferred != primary:
                        resolved.append((f"{name}:{model_id}", inferred))
    return resolved


def _assess_quota_warnings(cfg: dict, targets: list[str]) -> tuple[list[str], list]:
    """One cached snapshot, then per-target advisories. Never reroutes, never
    blocks, never consumes a Codex reset credit. Fail-open: quota collection
    crashes or returns None are caught so they cannot break dispatch.
    """
    if not _quota_warnings_enabled(cfg):
        return [], []
    try:
        snapshot = default_service().snapshot()
    except Exception:
        return [], []
    if not isinstance(snapshot, dict):
        return [], []
    resolved = _resolve_quota_targets(cfg, targets)
    warnings = assess_targets(snapshot, resolved)
    return render_warning_lines(warnings), warnings


def cmd_dispatch(args):
    root = paths.find_root()
    cfg = config.load(root)
    con = db.connect(root)
    requester = _identity(args, cfg)
    mission = " ".join(args.mission)
    if args.brief_file:
        mission = Path(args.brief_file).read_text()
    if not mission.strip():
        raise SystemExit("orchestra: empty mission (pass text, or --brief-file)")
    if args.team:
        if not con.execute("SELECT 1 FROM teams WHERE name=?", (args.team,)).fetchone():
            raise SystemExit(f"orchestra: no team '{args.team}' (create it first)")

    # Warn-only quota assessment — ONE cached snapshot, no DB inserts yet.
    # The snapshot is read first (bounded and fail-open, never reroutes,
    # never consumes reset credits), then we emit the warning lines to
    # stderr so they're visible before the run rows are even created.
    # --no-quota-warn skips the snapshot entirely (no collectors fire).
    skip_quota = bool(args.no_quota_warn)
    warning_lines: list[str] = []
    if not skip_quota:
        warning_lines, _ = _assess_quota_warnings(cfg, list(args.to))
        for line in warning_lines:
            print(line, file=sys.stderr)

    run_ids = []
    for target in args.to:
        agent = config.agent_cfg(cfg, target)
        display_model = agent.get("model")
        if agent["backend"] == "codex":
            dm, de = config.codex_defaults()
            eff = agent.get("effort") or de
            display_model = (display_model or dm or "codex-default") + (f" ({eff})" if eff else "")
        elif agent.get("variant"):
            display_model = f"{display_model} ({agent['variant']})"
        run_id = None
        slug = None
        # Race defence: the in-Python collision check is best-effort; a
        # parallel dispatcher could mint the same slug between our read and
        # INSERT. The DB partial UNIQUE index is the real guard — on a
        # constraint violation we regenerate the slug and retry, never
        # silently overwrite the original collision.
        for attempt in range(names.MAX_ATTEMPTS + 4):
            slug = names.assign_slug(con)
            try:
                cur = con.execute(
                    "INSERT INTO runs(agent, backend, model, title, work_item, team, "
                    "requested_by, workdir, slug, status, started_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?, 'spawning', ?)",
                    (target, agent["backend"], display_model,
                     args.title or mission[:80],
                     args.work, args.team, requester, str(root), slug, db.now()))
                run_id = cur.lastrowid
                break
            except sqlite3.IntegrityError as exc:
                if not names.is_unique_violation(exc):
                    raise
                names.reset_memory_cache()
                continue
        if run_id is None:
            raise SystemExit(
                f"orchestra: could not mint a unique run slug for {target} "
                f"after repeated collisions — odd, retry dispatch"
            )
        workdir, branch = str(root), None
        if args.worktree:
            wt, branch = worktree.create(root, run_id)
            workdir = str(wt)
        text = brief.compose(root=root, run_id=run_id, agent=agent, mission=mission,
                             work_item=args.work, team=args.team, requester=requester,
                             workdir=workdir, extra_context=args.context)
        bp = paths.briefs_dir(root) / f"run-{run_id}.md"
        bp.write_text(text)
        lp = paths.logs_dir(root) / f"run-{run_id}.jsonl"
        lp.touch()
        con.execute("UPDATE runs SET brief_path=?, log_path=?, workdir=?, branch=? WHERE id=?",
                    (str(bp), str(lp), workdir, branch, run_id))
        con.commit()
        run_ids.append(run_id)
        _work_log(root, args.work, f"orchestra: dispatched run {run_id} ({slug}) to {target} "
                                   f"({agent['backend']}/{agent.get('model') or 'default'})"
                                   + (f" in worktree branch {branch}" if branch else ""))
        print(f"run {run_id} ({slug}): {target} ({agent['backend']}/{agent.get('model') or 'default'})"
              + (f" worktree={workdir}" if branch else ""))
    con.close()
    for rid in run_ids:
        if args.sync:
            supervise.supervise(root, rid)
        else:
            _spawn_supervisor(root, rid)
    if not args.sync:
        print(f"dispatched async. `orchestra wait {' '.join(map(str, run_ids))}` blocks until done; "
              f"completions land in inbox '{requester}'.")


def cmd_reply(args):
    root = paths.find_root()
    cfg = config.load(root)
    con = db.connect(root)
    parent = con.execute("SELECT * FROM runs WHERE id=?", (args.run_id,)).fetchone()
    if not parent:
        raise SystemExit(f"orchestra: no run {args.run_id}")
    if parent["status"] not in db.RUN_TERMINAL:
        raise SystemExit(f"orchestra: run {args.run_id} is still {parent['status']} — "
                         "use `orchestra send` to leave it a message instead")
    if not parent["session_ref"]:
        raise SystemExit(f"orchestra: run {args.run_id} has no session ref; dispatch a fresh run")
    requester = _identity(args, cfg) or parent["requested_by"]
    msg = " ".join(args.message)
    followup = (f"{msg}\n\n(Orchestra follow-up on run {args.run_id}. First check "
                f"`orchestra inbox {parent['agent']} --unread --mark-read`. Same coordination "
                f"protocol: finish with `orchestra send {requester} \"HANDOFF: ...\" --as {parent['agent']}`"
                + (f", log progress with `work log {parent['work_item']} ...`" if parent["work_item"] else "") + ".)")
    run_id = supervise.create_followup(con, root, dict(parent), requester, followup,
                                       title=f"reply to run {args.run_id}")
    con.close()
    print(f"run {run_id}: follow-up to {parent['agent']} (session {parent['session_ref'][:20]}...)")
    if args.sync:
        supervise.supervise(root, run_id)
    else:
        _spawn_supervisor(root, run_id)


def cmd_runs(args):
    root = paths.find_root()
    con = db.connect(root)
    q = "SELECT * FROM runs" + (" WHERE status NOT IN ('done','failed','timeout','killed')"
                                if args.active else "") + " ORDER BY id"
    rows = list(con.execute(q))
    if args.json:
        print(json.dumps([dict(r) for r in rows], indent=2))
        return
    if not rows:
        print("(no runs)")
        return
    print(f"{'id':<4} {'agent':<10} {'status':<8} {'work':<8} {'started':<21} title")
    for r in rows:
        print(f"{r['id']:<4} {r['agent']:<10} {r['status']:<8} {r['work_item'] or '-':<8} "
              f"{r['started_at']:<21} {(r['title'] or '')[:60]}")


def cmd_run_show(args):
    root = paths.find_root()
    con = db.connect(root)
    r = con.execute("SELECT * FROM runs WHERE id=?", (args.run_id,)).fetchone()
    if not r:
        raise SystemExit(f"orchestra: no run {args.run_id}")
    for k in r.keys():
        v = r[k]
        if k == "summary" and v:
            print(f"{k}:\n  " + v.replace("\n", "\n  "))
        else:
            print(f"{k}: {v}")


def cmd_logs(args):
    root = paths.find_root()
    con = db.connect(root)
    r = con.execute("SELECT log_path FROM runs WHERE id=?", (args.run_id,)).fetchone()
    if not r or not r["log_path"] or not Path(r["log_path"]).is_file():
        raise SystemExit(f"orchestra: no log for run {args.run_id}")
    lines = Path(r["log_path"]).read_text(errors="replace").splitlines()
    if args.pretty:
        shown = 0
        for line in lines:
            line = line.strip()
            if line.startswith("{"):
                try:
                    texts = runners._dig(json.loads(line), {"text"})
                except ValueError:
                    texts = []
                for t in texts:
                    print(t)
                    shown += 1
            elif line:
                print(line)
                shown += 1
        if not shown:
            print("(no textual output parsed; try without --pretty)")
    else:
        for line in lines[-args.tail:]:
            print(line)


def cmd_wait(args):
    import time
    root = paths.find_root()
    con = db.connect(root)
    if args.run_ids:
        targets = set(args.run_ids)
    else:
        targets = {r["id"] for r in con.execute(
            "SELECT id FROM runs WHERE status NOT IN ('done','failed','timeout','killed')")}
    if not targets:
        print("no active runs")
        return
    print(f"waiting on runs: {sorted(targets)}")
    deadline = time.time() + args.timeout if args.timeout else None
    pending = set(targets)
    while pending:
        if deadline and time.time() > deadline:
            print(f"timeout; still pending: {sorted(pending)}")
            sys.exit(2)
        rows = con.execute(
            f"SELECT id, agent, status, exit_code FROM runs WHERE id IN "
            f"({','.join(map(str, pending))}) AND status IN ('done','failed','timeout','killed')").fetchall()
        for r in rows:
            print(f"run {r['id']} ({r['agent']}) -> {r['status']}"
                  + (f" exit {r['exit_code']}" if r["exit_code"] not in (None, 0) else ""))
            pending.discard(r["id"])
            if args.any:
                return
        if pending:
            time.sleep(2)
    print("all runs finished — check your inbox: `orchestra inbox <you> --unread --mark-read`")


def cmd_queue(args):
    root = paths.find_root()
    cfg = config.load(root)
    con = db.connect(root)
    r = con.execute("SELECT * FROM runs WHERE id=?", (args.run_id,)).fetchone()
    if not r:
        raise SystemExit(f"orchestra: no run {args.run_id}")
    sender = _identity(args, cfg)
    msg = " ".join(args.message)
    if r["status"] in db.RUN_TERMINAL:
        if not r["session_ref"]:
            raise SystemExit(f"orchestra: run {args.run_id} has no session to resume — "
                             "dispatch a fresh run instead")
        text = (f"{msg}\n\n(Queued follow-up on run {args.run_id}. Same protocol: finish with "
                f"`orchestra send {sender} \"HANDOFF: ...\" --as {r['agent']}`.)")
        rid = supervise.create_followup(con, root, dict(r), sender, text)
        supervise.spawn_supervisor(root, rid)
        print(f"run {args.run_id} already finished — follow-up dispatched now as run {rid}")
    else:
        con.execute("INSERT INTO messages(sender, recipient, body, run_id, kind, created_at) "
                    "VALUES(?,?,?,?, 'queued', ?)",
                    (sender, r["agent"], msg, args.run_id, db.now()))
        con.commit()
        print(f"queued — will be auto-delivered as a session follow-up when run "
              f"{args.run_id} completes")


def cmd_interrupt(args):
    root = paths.find_root()
    cfg = config.load(root)
    con = db.connect(root)
    r = con.execute("SELECT * FROM runs WHERE id=?", (args.run_id,)).fetchone()
    if not r:
        raise SystemExit(f"orchestra: no run {args.run_id}")
    if r["status"] in db.RUN_TERMINAL:
        raise SystemExit(f"orchestra: run {args.run_id} already {r['status']} — "
                         f"use `orchestra reply {args.run_id} \"...\"` instead")
    agent = config.agent_cfg(cfg, r["agent"])
    if agent.get("ensemble"):
        raise SystemExit("orchestra: ensemble leads can't be interrupted (their team runs "
                         "server-side); use `orchestra send` — the lead reads its inbox "
                         "when teammates wake it")
    if not r["session_ref"]:
        raise SystemExit(f"orchestra: run {args.run_id}'s session isn't identified yet "
                         "(happens ~10s after spawn) — retry in a moment, or `orchestra send` "
                         "to queue the message")
    sender = _identity(args, cfg)
    con.execute("INSERT INTO messages(sender, recipient, body, run_id, created_at) "
                "VALUES(?,?,?,?,?)",
                (sender, r["agent"], f"[INTERRUPT] {' '.join(args.message)}",
                 args.run_id, db.now()))
    con.execute("UPDATE runs SET status='interrupt' WHERE id=?", (args.run_id,))
    con.commit()
    if r["pid"]:
        try:
            os.killpg(r["pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
    print(f"run {args.run_id} interrupted — worker will resume its session, read the "
          f"message, and continue the mission")


def cmd_kill(args):
    root = paths.find_root()
    con = db.connect(root)
    r = con.execute("SELECT * FROM runs WHERE id=?", (args.run_id,)).fetchone()
    if not r:
        raise SystemExit(f"orchestra: no run {args.run_id}")
    if r["status"] in db.RUN_TERMINAL:
        print(f"run {args.run_id} already {r['status']}")
        return
    con.execute("UPDATE runs SET status='killed' WHERE id=?", (args.run_id,))
    con.commit()
    if r["pid"]:
        try:
            os.killpg(r["pid"], signal.SIGTERM)
            print(f"sent SIGTERM to run {args.run_id} (pgid {r['pid']})")
        except ProcessLookupError:
            print(f"run {args.run_id} process already gone; marked killed")


def cmd_note(args):
    root = paths.find_root()
    cfg = config.load(root)
    con = db.connect(root)
    author = _identity(args, cfg)
    con.execute("INSERT INTO feed(author, body, tags, work_item, run_id, created_at) "
                "VALUES(?,?,?,?,?,?)",
                (author, args.body, args.tags or "", args.work, args.run, db.now()))
    con.commit()
    _work_log(root, args.work, f"[{author}] {args.body}")
    print("noted")


def cmd_feed(args):
    root = paths.find_root()
    con = db.connect(root)
    q, params = "SELECT * FROM feed", []
    if args.tag:
        q += " WHERE tags LIKE ?"
        params.append(f"%{args.tag}%")
    q += " ORDER BY id DESC LIMIT ?"
    params.append(args.limit)
    rows = list(con.execute(q, params))[::-1]
    if not rows:
        print("(feed empty)")
    for r in rows:
        extra = " ".join(x for x in [f"work:{r['work_item']}" if r["work_item"] else "",
                                     f"run:{r['run_id']}" if r["run_id"] else "",
                                     f"[{r['tags']}]" if r["tags"] else ""] if x)
        print(f"{r['created_at']} {r['author']}: {r['body']} {extra}")


def cmd_status(args):
    root = paths.find_root()
    con = db.connect(root)
    print(f"orchestra @ {root}\n")
    active = list(con.execute(
        "SELECT * FROM runs WHERE status NOT IN ('done','failed','timeout','killed') ORDER BY id"))
    print(f"## active runs ({len(active)})")
    for r in active:
        print(f"  run {r['id']}: {r['agent']} [{r['status']}] work:{r['work_item'] or '-'} "
              f"since {r['started_at']} — {(r['title'] or '')[:50]}")
    recent = list(con.execute("SELECT * FROM runs WHERE status IN "
                              "('done','failed','timeout','killed') ORDER BY id DESC LIMIT 5"))
    if recent:
        print("## recent finished")
        for r in recent[::-1]:
            print(f"  run {r['id']}: {r['agent']} -> {r['status']} — {(r['title'] or '')[:50]}")
    unread = list(con.execute("SELECT recipient, COUNT(*) n FROM messages WHERE read_at IS NULL "
                              "GROUP BY recipient ORDER BY n DESC"))
    print("## unread inboxes")
    for u in unread:
        print(f"  {u['recipient']}: {u['n']} unread")
    if not unread:
        print("  (all read)")
    teams = list(con.execute("SELECT name FROM teams"))
    if teams:
        print("## teams: " + ", ".join(t["name"] for t in teams))
    feed_rows = list(con.execute("SELECT * FROM feed ORDER BY id DESC LIMIT 5"))
    if feed_rows:
        print("## recent findings")
        for r in feed_rows[::-1]:
            print(f"  {r['author']}: {r['body'][:90]}")
    if _work_available() and (root / ".work").is_dir():
        try:
            out = subprocess.run(["work", "list"], cwd=root, capture_output=True,
                                 text=True, timeout=20).stdout.strip()
            if out:
                print("\n## work tracker")
                print("\n".join("  " + line for line in out.splitlines()[:20]))
        except Exception:
            pass


def cmd_checkpoint(args):
    """Write a durable, backend-neutral checkpoint under ``.orchestra/checkpoints/``.

    The checkpoint is intent + high-water marks: the source identity,
    objective, next steps, the anchored ``--work`` item (when supplied),
    and the largest run/message/feed IDs observed at write time.
    Takeover re-queries the live DB for everything after those marks so
    the brief stays current even if a long time passes between
    checkpoint and takeover.

    Free-text fields (objective, next steps, run titles, work titles,
    feed tags, bodies) are redacted for credential patterns before
    serialization. Process / session / transcript surfaces
    (``session_ref``, ``pid``, ``log_path``, ``brief_path``, ``workdir``,
    ``branch``, argv, env) are never written — only ``SAFE_*`` field
    whitelists make it onto disk.
    """
    root = paths.find_root()
    cfg = config.load(root)
    source = _identity(args, cfg)
    if not source:
        raise SystemExit("orchestra: checkpoint needs --as <identity> "
                         "(or $ORCHESTRA_SELF / settings.default_requester)")
    objective = (getattr(args, "objective", None) or "").strip() or None
    next_steps = list(getattr(args, "next", None) or [])
    work_item = (getattr(args, "work", None) or "").strip() or None
    path = checkpoint.write_checkpoint(
        root, source=source, objective=objective,
        next_steps=next_steps, work_item=work_item,
    )
    print(f"checkpoint: {source} -> {path}")
    if work_item:
        _work_log(root, work_item,
                  f"checkpoint written by {source} -> {path.name}")


def cmd_takeover(args):
    """Print a cold-start continuation brief from a saved checkpoint.

    Read-only by contract: ``takeover`` never INSERTs, UPDATEs, or
    DELETEs anything in the source DB. Selection precedence:
    ``--checkpoint <path>`` > ``--from <source>`` > latest of all
    checkpoints. The brief advertises both ``source`` (who handed off)
    and ``target`` (who is taking over) explicitly.
    """
    root = paths.find_root()
    cfg = config.load(root)
    target = _identity(args, cfg)
    if not target:
        raise SystemExit("orchestra: takeover needs --as <identity>")

    ck = None
    if getattr(args, "checkpoint", None):
        try:
            ck = checkpoint.load_checkpoint(Path(args.checkpoint))
        except checkpoint.CheckpointError as exc:
            raise SystemExit(f"orchestra: {exc}") from exc
    elif getattr(args, "from_", None):
        try:
            ck = checkpoint.latest_checkpoint(root, source=args.from_)
        except checkpoint.CheckpointError as exc:
            raise SystemExit(f"orchestra: {exc}") from exc
        if not ck:
            raise SystemExit(
                f"orchestra: no checkpoints found for source {args.from_!r}"
            )
    else:
        try:
            ck = checkpoint.latest_checkpoint(root)
        except checkpoint.CheckpointError as exc:
            raise SystemExit(f"orchestra: {exc}") from exc
        if not ck:
            raise SystemExit(
                "orchestra: no checkpoints found — "
                "have the source orchestrator run `orchestra checkpoint --as <source>` first"
            )

    checkpoint_project = ck.data.get("project") or {}
    expected_project_id = projects.project_id(root)
    if checkpoint_project.get("id") != expected_project_id:
        raise SystemExit(
            "orchestra: checkpoint belongs to a different project; "
            "run takeover from the checkpoint's Orchestra root"
        )

    brief_text = checkpoint.render_takeover_brief(root, ck, target=target)

    if args.json:
        print(json.dumps({
            "checkpoint_path": str(ck.path),
            "source": ck.source,
            "created_at": ck.data.get("created_at"),
            "objective": ck.objective,
            "next_steps": ck.next_steps,
            "work_item": ck.work_item,
            "objective_source": ck.data.get("objective_source"),
            "high_water": ck.high_water,
            "target": target,
            "brief": brief_text,
        }, indent=2))
        return

    sys.stdout.write(brief_text)


def cmd_doctor(args):
    root = _maybe_root()
    cfg = config.load(root)
    print("orchestra doctor\n")
    for tool in ["opencode", "codex", "claude", "work", "git"]:
        path = shutil.which(tool)
        print(f"  {tool:<9} {'OK  ' + path if path else 'MISSING'}")
    models = ""
    if shutil.which("opencode"):
        try:
            models = subprocess.run(["opencode", "models"], capture_output=True,
                                    text=True, timeout=60).stdout
        except Exception:
            pass
    print("\n  roster:")
    for name, a in sorted(cfg.get("agents", {}).items()):
        m = a.get("model")
        status = "ok"
        if a.get("backend") == "opencode" and m and models and m not in models:
            status = f"MODEL NOT FOUND in `opencode models`"
        print(f"    {name:<12} {a.get('backend'):<9} {m or '(default)':<42} {status}")
    oc_cfg = Path("~/.config/opencode/opencode.json").expanduser()
    if oc_cfg.is_file():
        ensemble = "ensemble" in oc_cfg.read_text()
        print(f"\n  opencode-ensemble plugin: {'installed' if ensemble else 'NOT in ' + str(oc_cfg)}")
    if root:
        print(f"\n  project root: {root}")
        print(f"  work tracker: {'present' if (root / '.work').is_dir() else 'absent (run `work init .`)'}")


def cmd_host(args):
    if args.host_cmd == "stop":
        print("host stopped" if host.stop() else "host was not running")
    elif args.host_cmd == "start":
        print(f"host: {host.ensure(args.port)}")
    else:  # status
        u = host.url()
        s = host.state() or {}
        print(f"host: {u or 'not running'}"
              + (f" (pid {s.get('pid')})" if u else "")
              + f"\nensemble dashboard: http://localhost:4747 (when a team is active)"
              + f"\nlog: {host.LOG_FILE}")


def _format_reset_credits(resets: dict | None) -> str:
    """Render the Codex rate-limit reset-credit line. Always emit a value
    when the wire carries ``rate_limit_resets`` (even when the count is 0):
    operators expect to see "0 reset credits available", not a missing row.
    """
    if not isinstance(resets, dict):
        return ""
    count = resets.get("available_count")
    if not isinstance(count, int) or count < 0:
        return ""
    credits_label = "reset credit available" if count == 1 else "reset credits available"
    return f" · {count} {credits_label}"


def cmd_usage(args):
    print("## provider runway")
    snap = default_service().snapshot(force=args.refresh)
    rec = snap.get("recommendation") or {}
    if rec:
        print(f"  best runway: {rec.get('provider_name')} "
              f"({rec.get('headroom_percent'):.0f}% headroom across coding windows)")
    else:
        print("  (no provider returned a usable coding headroom yet)")
    for row in snap.get("providers") or []:
        plan = row.get("plan") or "—"
        headroom = row.get("headroom_percent")
        headroom_s = f"{headroom:.0f}%" if isinstance(headroom, (int, float)) else "n/a"
        resets = row.get("rate_limit_resets")
        # Only the Codex collector populates `rate_limit_resets`; other
        # providers leave it None and we render the count line only when
        # the wire carries an actual Codex reset-credit record.
        reset_note = _format_reset_credits(resets)
        print(f"  {row.get('name'):<8} [{row.get('status'):<12}] {plan:<22} "
              f"headroom {headroom_s}{reset_note}")

    print()
    # --- per-project worker token burn: this is project-local data, the only
    # piece the shared service doesn't already provide. Keep it. The runs
    # lookup is read into memory inside a try/finally so the connection
    # closes even when the project has zero runs (the empty-agg path).
    root = _maybe_root()
    if not root:
        return
    rows: list = []
    con = db.connect(root)
    try:
        rows = list(con.execute(
            "SELECT agent, log_path FROM runs WHERE log_path IS NOT NULL"
        ))
    finally:
        con.close()
    agg = {}
    for r in rows:
        lp = Path(r["log_path"])
        if not lp.is_file():
            continue
        a = agg.setdefault(r["agent"], {"runs": 0, "in": 0, "out": 0, "reason": 0, "cache": 0, "cost": 0.0})
        a["runs"] += 1
        for line in lp.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            part = obj.get("part")
            if isinstance(part, dict) and part.get("type") == "step-finish":  # opencode
                tk = part.get("tokens") or {}
                a["in"] += tk.get("input", 0)
                a["out"] += tk.get("output", 0)
                a["reason"] += tk.get("reasoning", 0)
                a["cache"] += (tk.get("cache") or {}).get("read", 0)
                a["cost"] += part.get("cost") or 0
            elif obj.get("type") == "turn.completed":  # codex
                u = obj.get("usage") or {}
                a["in"] += u.get("input_tokens", 0)
                a["out"] += u.get("output_tokens", 0)
                a["cache"] += u.get("cached_input_tokens", 0)
            elif obj.get("type") == "result":  # claude
                u = obj.get("usage") or {}
                a["in"] += u.get("input_tokens", 0)
                a["out"] += u.get("output_tokens", 0)
                a["cache"] += u.get("cache_read_input_tokens", 0)
                a["cost"] += obj.get("total_cost_usd") or 0
    print(f"## worker token burn ({root.name})")
    if not agg:
        print("  (no runs)")
        return
    fmt = lambda n: f"{n/1000:.0f}k" if n >= 1000 else str(n)
    print(f"  {'agent':<12}{'runs':<6}{'input':<9}{'output':<9}{'reasoning':<11}{'cache-read':<12}cost")
    for name, a in sorted(agg.items()):
        cost = f"${a['cost']:.2f}" if a["cost"] else "-"
        print(f"  {name:<12}{a['runs']:<6}{fmt(a['in']):<9}{fmt(a['out']):<9}"
              f"{fmt(a['reason']):<11}{fmt(a['cache']):<12}{cost}")


def cmd_ui(args):
    from orchestra_cli import ui
    # --tailscale and --host are mutually exclusive: --tailscale DISCOVERS
    # the right interface; explicit --host would silently override the
    # discovery and undermine the safety promise, so we reject the
    # combination up-front.
    if args.tailscale and args.host:
        raise SystemExit(
            "orchestra: --tailscale and --host cannot be combined. "
            "--tailscale discovers and binds the machine's Tailnet IPv4; "
            "drop --host or drop --tailscale."
        )
    try:
        ui.serve(
            paths.find_root(),
            port=args.port,
            open_browser=not args.no_open,
            host=args.host,
            tailscale_mode=args.tailscale,
        )
    except tailscale.TailscaleError as exc:
        raise SystemExit(f"orchestra: {exc}") from exc


def cmd_project(args):
    """Multi-project picker allowlist (lives in ~/.config/orchestra/projects.json).

    The picker only ever shows entries managed here. ``forget`` is the
    picker-side remove: it deletes the registry row but never touches
    the project's files or its ``.orchestra/`` state — that data stays
    on disk so the user can re-register later or keep working from the
    project root via the CLI.
    """
    if args.project_cmd == "register":
        target = Path(args.path).expanduser().resolve() if args.path else Path.cwd().resolve()
        if not projects.is_orchestra_root(target):
            raise SystemExit(
                f"orchestra: {target} is not an Orchestra project "
                f"(no .orchestra/ directory). Run `orchestra init` there first.")
        entry = projects.register(target, name=args.name)
        print(f"registered: {entry['id']}  {entry['name']}\n  {entry['root']}")
    elif args.project_cmd == "forget":
        if not args.id_or_path:
            raise SystemExit("orchestra: `orchestra project forget` needs an id or path")
        removed = projects.unregister(args.id_or_path)
        if removed:
            print(f"forgot: {args.id_or_path}  "
                  "(project files and .orchestra/ left untouched)")
        else:
            raise SystemExit(f"orchestra: nothing matched `{args.id_or_path}` in the picker")
    else:  # list
        rows = projects.list_registered()
        if not rows:
            print("(no projects registered — run `orchestra init`, or "
                  "`orchestra project register <path>`)")
            return
        print(f"{'id':<16} {'name':<22} root")
        for r in rows:
            avail = "" if projects.is_orchestra_root(Path(r["root"])) else "  (unavailable)"
            print(f"{r['id']:<16} {r['name']:<22} {r['root']}{avail}")


def cmd_supervise(args):
    sys.exit(supervise.supervise(Path(args.root), args.run_id))


# --- parser ---------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(prog="orchestra",
                                description="Multi-agent orchestration: dispatch opencode/codex/claude "
                                            "workers with teams, inboxes, and slash-work tracking")
    sub = p.add_subparsers(dest="cmd", required=True)

    def ident(sp):
        sp.add_argument("--as", dest="as_", help="acting identity (default: $ORCHESTRA_SELF)")

    s = sub.add_parser("init", help="initialize .orchestra in the current directory")
    s.add_argument("--work", action="store_true", help="also `work init` a tracker workspace here")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("roster", help="list configured worker agents")
    s.set_defaults(fn=cmd_roster)

    s = sub.add_parser("doctor", help="check tools, models, and config health")
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("team", help="manage teams")
    ts = s.add_subparsers(dest="team_cmd", required=True)
    # Distinct locals — see the comment on `project` below for why.
    t_create = ts.add_parser("create")
    t_create.add_argument("name")
    t_create.add_argument("agents", nargs="*")
    t_create.add_argument("--about")
    t_add = ts.add_parser("add")
    t_add.add_argument("name")
    t_add.add_argument("agents", nargs="+")
    ts.add_parser("list")
    s.set_defaults(fn=cmd_team)

    s = sub.add_parser("send", help="send a message to an agent/orchestrator inbox")
    s.add_argument("to")
    s.add_argument("body")
    s.add_argument("--work", help="related work item (W-XXXX)")
    s.add_argument("--run", type=int, help="related run id")
    ident(s)
    s.set_defaults(fn=cmd_send)

    s = sub.add_parser("broadcast", help="message every member of a team")
    s.add_argument("body")
    s.add_argument("--team", required=True)
    s.add_argument("--work")
    ident(s)
    s.set_defaults(fn=cmd_broadcast)

    s = sub.add_parser("inbox", help="read an inbox")
    s.add_argument("name", nargs="?")
    s.add_argument("--unread", action="store_true")
    s.add_argument("--all", action="store_true", help="include read messages")
    s.add_argument("--mark-read", action="store_true")
    s.add_argument("--json", action="store_true")
    ident(s)
    s.set_defaults(fn=cmd_inbox)

    s = sub.add_parser("dispatch", help="dispatch a mission to worker agent(s), async")
    s.add_argument("mission", nargs="*")
    s.add_argument("--to", action="append", required=True, help="roster agent (repeatable to fan out)")
    s.add_argument("--work", help="work item to track against (W-XXXX)")
    s.add_argument("--team")
    s.add_argument("--title")
    s.add_argument("--context", help="extra context appended to the brief")
    s.add_argument("--brief-file", help="read mission text from a file")
    s.add_argument("--worktree", action="store_true", help="isolate in a git worktree (skills auto-synced)")
    s.add_argument("--sync", action="store_true", help="block until the run finishes")
    s.add_argument("--no-quota-warn", action="store_true",
                   help="suppress the warn-only provider-headroom check (default: on)")
    ident(s)
    s.set_defaults(fn=cmd_dispatch)

    s = sub.add_parser("reply", help="continue a finished run's session with a follow-up")
    s.add_argument("run_id", type=int)
    s.add_argument("message", nargs="+")
    s.add_argument("--sync", action="store_true")
    ident(s)
    s.set_defaults(fn=cmd_reply)

    s = sub.add_parser("runs", help="list runs")
    s.add_argument("--active", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_runs)

    s = sub.add_parser("run", help="run details")
    rs = s.add_subparsers(dest="run_cmd", required=True)
    r_show = rs.add_parser("show")
    r_show.add_argument("run_id", type=int)
    s.set_defaults(fn=cmd_run_show)

    s = sub.add_parser("logs", help="show a run's worker output")
    s.add_argument("run_id", type=int)
    s.add_argument("--tail", type=int, default=40)
    s.add_argument("--pretty", action="store_true", help="extract readable text from JSONL")
    s.set_defaults(fn=cmd_logs)

    s = sub.add_parser("wait", help="block until runs finish (default: all active)")
    s.add_argument("run_ids", nargs="*", type=int)
    s.add_argument("--any", action="store_true", help="return after the first completion")
    s.add_argument("--timeout", type=int, default=0)
    ident(s)  # accepted for consistency; wait is identity-agnostic
    s.set_defaults(fn=cmd_wait)

    s = sub.add_parser("host", help="manage the persistent opencode host (used by ensemble runs)")
    s.add_argument("host_cmd", nargs="?", default="status", choices=["status", "start", "stop"])
    s.add_argument("--port", type=int, default=host.DEFAULT_PORT,
                   help=f"opencode serve port for the ensemble host (default {host.DEFAULT_PORT}); "
                        "ensemble dispatches attach to whatever host is recorded as running")
    s.set_defaults(fn=cmd_host)

    s = sub.add_parser("usage", help="cached provider runway + per-agent token burn for this project")
    s.add_argument("--refresh", action="store_true", help="force a fresh quota snapshot")
    s.set_defaults(fn=cmd_usage)

    s = sub.add_parser("ui", help="shared read-only dashboard for registered projects")
    s.add_argument("--port", type=int, default=None,
                   help="UI port; defaults to a 4764 preference (falls back to OS-chosen when 4764 is busy). "
                        "Any other explicit value is pinned — a busy port fails clearly.")
    s.add_argument("--host", default=None,
                   help="bind host. Default: 127.0.0.1. Accepts loopback or a Tailscale IPv4; "
                        "wildcard and ordinary LAN hosts are rejected. Mutually exclusive with --tailscale.")
    s.add_argument("--tailscale", action="store_true",
                   help="discover this machine's Tailscale IPv4 and bind only that interface. "
                        "Fails clearly if Tailscale is unavailable.")
    s.add_argument("--no-open", action="store_true", help="don't open a browser")
    s.set_defaults(fn=cmd_ui)

    s = sub.add_parser("project", help="manage the multi-project picker allowlist")
    ps = s.add_subparsers(dest="project_cmd", required=True)
    # Do NOT name these locals `p` — `p` is the root ArgumentParser and
    # the function ends with `args = p.parse_args()`. Shadowing it makes
    # every CLI invocation dispatch against whichever child parser was
    # assigned last (silent, ugly, hard to spot). Distinct names below.
    ps.add_parser("list", help="list registered project roots")
    p_register = ps.add_parser("register",
                               help="add a project root to the picker allowlist")
    p_register.add_argument("path", nargs="?", help="path to register (default: current directory)")
    p_register.add_argument("--name", help="display name override (default: directory basename)")
    p_forget = ps.add_parser("forget",
                             help="remove a project from the picker allowlist "
                                  "(never deletes project data)")
    p_forget.add_argument("id_or_path", nargs="?", help="project id or canonical path to remove")
    s.set_defaults(fn=cmd_project)

    s = sub.add_parser("queue", help="queue a follow-up for a running worker; auto-delivered "
                                     "(session resume) when its current run completes")
    s.add_argument("run_id", type=int)
    s.add_argument("message", nargs="+")
    ident(s)
    s.set_defaults(fn=cmd_queue)

    s = sub.add_parser("interrupt", help="guaranteed delivery to a RUNNING worker: "
                                         "pause it, inject the message, resume the mission")
    s.add_argument("run_id", type=int)
    s.add_argument("message", nargs="+")
    ident(s)
    s.set_defaults(fn=cmd_interrupt)

    s = sub.add_parser("kill", help="terminate a running worker")
    s.add_argument("run_id", type=int)
    s.set_defaults(fn=cmd_kill)

    s = sub.add_parser("note", help="log a finding to the shared feed")
    s.add_argument("body")
    s.add_argument("--tags")
    s.add_argument("--work")
    s.add_argument("--run", type=int)
    ident(s)
    s.set_defaults(fn=cmd_note)

    s = sub.add_parser("feed", help="show the shared findings feed")
    s.add_argument("--limit", type=int, default=25)
    s.add_argument("--tag")
    s.set_defaults(fn=cmd_feed)

    s = sub.add_parser("status", help="project snapshot: runs, inboxes, feed, tracker")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("checkpoint",
                       help="write a durable, backend-neutral handoff "
                            "checkpoint under .orchestra/checkpoints/")
    s.add_argument("--objective", help="one-line statement of what this "
                                       "wave is trying to accomplish. "
                                       "Resolution order: --objective > "
                                       "--work anchor (work show --json) > "
                                       "active-items fallback")
    s.add_argument("--next", action="append", default=[],
                   help="next step the successor should take (repeatable)")
    s.add_argument("--work", help="anchor work item (W-XXXX); the "
                                  "checkpoint persists it and infers the "
                                  "objective from `work show ITEM --json`. "
                                  "Also progress-logged like --work on dispatch.")
    ident(s)
    s.set_defaults(fn=cmd_checkpoint)

    s = sub.add_parser("takeover",
                       help="render a cold-start continuation brief from a "
                            "saved checkpoint (strictly read-only)")
    s.add_argument("--from", dest="from_",
                   help="resume from the latest checkpoint whose filename prefix "
                        "matches this source identity (e.g. 'codex')")
    s.add_argument("--checkpoint", help="explicit checkpoint path (overrides --from)")
    s.add_argument("--json", action="store_true",
                   help="print the brief + metadata as JSON instead of markdown")
    ident(s)
    s.set_defaults(fn=cmd_takeover)

    s = sub.add_parser("_supervise")
    s.add_argument("run_id", type=int)
    s.add_argument("--root", required=True)
    s.set_defaults(fn=cmd_supervise)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
