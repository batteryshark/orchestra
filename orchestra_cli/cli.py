import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from orchestra_cli import brief, config, db, docs, paths, runners, supervise, worktree


def _identity(args, cfg) -> str:
    return getattr(args, "as_", None) or os.environ.get("ORCHESTRA_SELF") \
        or cfg["settings"].get("default_requester", "orchestrator")


def _spawn_supervisor(root: Path, run_id: int) -> None:
    exe = shutil.which("orchestra")
    cmd = [exe, "_supervise", str(run_id), "--root", str(root)] if exe else \
        [sys.executable, "-m", "orchestra_cli", "_supervise", str(run_id), "--root", str(root)]
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


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
    run_ids = []
    for target in args.to:
        agent = config.agent_cfg(cfg, target)
        cur = con.execute(
            "INSERT INTO runs(agent, backend, model, title, work_item, team, requested_by, "
            "workdir, status, started_at) VALUES(?,?,?,?,?,?,?,?, 'spawning', ?)",
            (target, agent["backend"], agent.get("model"), args.title or mission[:80],
             args.work, args.team, requester, str(root), db.now()))
        run_id = cur.lastrowid
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
        _work_log(root, args.work, f"orchestra: dispatched run {run_id} to {target} "
                                   f"({agent['backend']}/{agent.get('model') or 'default'})"
                                   + (f" in worktree branch {branch}" if branch else ""))
        print(f"run {run_id}: {target} ({agent['backend']}/{agent.get('model') or 'default'})"
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
    followup = (f"{msg}\n\n(Orchestra follow-up on run {args.run_id}. Same coordination protocol: "
                f"finish with `orchestra send {requester} \"HANDOFF: ...\" --as {parent['agent']}`"
                + (f", log progress with `work log {parent['work_item']} ...`" if parent["work_item"] else "") + ".)")
    cur = con.execute(
        "INSERT INTO runs(agent, backend, model, title, work_item, team, requested_by, workdir, "
        "branch, parent_run, session_ref, status, started_at) VALUES(?,?,?,?,?,?,?,?,?,?,?, 'spawning', ?)",
        (parent["agent"], parent["backend"], parent["model"], f"reply to run {args.run_id}",
         parent["work_item"], parent["team"], requester, parent["workdir"], parent["branch"],
         args.run_id, parent["session_ref"], db.now()))
    run_id = cur.lastrowid
    bp = paths.briefs_dir(root) / f"run-{run_id}.md"
    bp.write_text(followup)
    lp = paths.logs_dir(root) / f"run-{run_id}.jsonl"
    lp.touch()
    con.execute("UPDATE runs SET brief_path=?, log_path=? WHERE id=?", (str(bp), str(lp), run_id))
    con.commit()
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
    t = ts.add_parser("create")
    t.add_argument("name")
    t.add_argument("agents", nargs="*")
    t.add_argument("--about")
    t = ts.add_parser("add")
    t.add_argument("name")
    t.add_argument("agents", nargs="+")
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
    r = rs.add_parser("show")
    r.add_argument("run_id", type=int)
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
    s.set_defaults(fn=cmd_wait)

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

    s = sub.add_parser("_supervise")
    s.add_argument("run_id", type=int)
    s.add_argument("--root", required=True)
    s.set_defaults(fn=cmd_supervise)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
