import argparse
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orchestra_cli import (
    availability,
    brief,
    capabilities,
    cancel,
    child_runs,
    checkpoint,
    config,
    db,
    dependencies,
    docs,
    ensemble,
    host,
    incidents,
    names,
    operator_controller,
    operator_contract,
    operator_replay,
    operator_roster,
    operator_runtime,
    operator_store,
    paths,
    projects,
    reap,
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
from orchestra_cli.usage.spend import with_project_spend
from orchestra_cli.usage.tokens import token_usage


def _identity(args, cfg) -> str:
    return getattr(args, "as_", None) or os.environ.get("ORCHESTRA_SELF") \
        or cfg["settings"].get("default_requester", "orchestrator")


def _run_id(args) -> int | None:
    explicit = getattr(args, "run", None)
    if explicit is not None:
        return explicit
    value = os.environ.get("ORCHESTRA_RUN_ID")
    try:
        return int(value) if value else None
    except ValueError:
        return None


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
    playbook = root / "ORCHESTRA.md"
    refresh_playbook = bool(getattr(args, "refresh_playbook", False))
    if not playbook.exists():
        playbook.write_text(docs.playbook_template(), encoding="utf-8")
        playbook_status = "created"
    elif refresh_playbook:
        try:
            existing = playbook.read_text(encoding="utf-8")
            refreshed = docs.refresh_playbook(existing)
        except (OSError, UnicodeError, docs.PlaybookRefreshError) as exc:
            raise SystemExit(f"orchestra: cannot refresh {playbook}: {exc}") from exc
        if refreshed == existing:
            playbook_status = "already current"
        else:
            playbook.write_text(refreshed, encoding="utf-8")
            playbook_status = "refreshed managed section"
    else:
        playbook_status = "preserved existing file"

    if not (root / ".git").exists():
        try:
            result = subprocess.run(
                ["git", "init", "--quiet", str(root)],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SystemExit(f"orchestra: cannot initialize git repository: {exc}") from exc
        if result.returncode != 0:
            raise SystemExit(
                f"orchestra: cannot initialize git repository: {result.stderr.strip()}"
            )

    sd = root / paths.STATE_DIR
    sd.mkdir(exist_ok=True)
    (sd / ".gitignore").write_text(docs.STATE_GITIGNORE)
    if not (sd / "config.toml").exists():
        (sd / "config.toml").write_text(docs.PROJECT_CONFIG_STUB)
    gp = config.ensure_global_config()
    db.connect(root).close()
    for doc in ["AGENTS.md", "CLAUDE.md"]:
        p = root / doc
        text = p.read_text(encoding="utf-8") if p.exists() else ""
        if "<!-- orchestra -->" not in text:
            p.write_text(text + docs.POINTER, encoding="utf-8")
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
    print(f"  playbook: {playbook} ({playbook_status}; "
          "pointers present in AGENTS.md / CLAUDE.md)")
    if not (root / ".work").is_dir():
        print("  note: no .work workspace here — run `work init .` (or `orchestra init --work`) "
              "so missions can be tracked durably")


def cmd_roster(args):
    cfg = config.load(_maybe_root())
    print(f"{'agent':<12} {'backend':<9} {'tier':<5} {'model':<42} role")
    for name, a in sorted(cfg.get("agents", {}).items()):
        model = a.get("model", "(backend default)")
        tier = a.get("tier", "-")
        flags = " [ensemble]" if a.get("ensemble") else ""
        print(
            f"{name:<12} {a.get('backend', '?'):<9} {str(tier):<5} "
            f"{model:<42} {a.get('role', '')}{flags}"
        )


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
    sender = _identity(args, cfg)
    body = args.body
    if args.body_file:
        message_path = Path(args.body_file)
        try:
            body = message_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise SystemExit(
                f"orchestra: cannot read message file '{message_path}': {exc}"
            ) from exc
    con = db.connect(root)
    try:
        explicit_run_id = getattr(args, "run", None)
        target_run = None
        if explicit_run_id is not None:
            candidate = con.execute(
                "SELECT * FROM runs WHERE id=?", (explicit_run_id,)
            ).fetchone()
            if (
                candidate
                and candidate["agent"] == args.to
                and candidate["status"] not in db.RUN_TERMINAL
            ):
                target_run = candidate
        else:
            active = list(con.execute(
                "SELECT * FROM runs WHERE agent=? "
                "AND status NOT IN ('done','failed','timeout','killed') ORDER BY id",
                (args.to,),
            ))
            if len(active) == 1:
                target_run = active[0]
            elif len(active) > 1:
                choices = ", ".join(
                    f"{row['id']} ({row['slug'] or 'no slug'})" for row in active
                )
                raise SystemExit(
                    f"orchestra: recipient '{args.to}' is ambiguous across active runs: "
                    f"{choices}. Use `orchestra interrupt RUN \"...\"` for guaranteed "
                    "delivery, or pass `--run RUN`."
                )

        if target_run is not None:
            agent = config.agent_cfg(cfg, target_run["agent"])
            if not agent.get("ensemble"):
                if int(target_run["supervisor_protocol"] or 0) < 1:
                    raise SystemExit(
                        f"orchestra: message was not sent: run {target_run['id']}'s "
                        "supervisor cannot guarantee delivery. Stop and resume the run "
                        "under the current Orchestra version."
                    )
                message_id = _record_interrupt(
                    con,
                    target_run,
                    sender=sender,
                    body=body,
                    immediate=False,
                )
                print(
                    f"message #{message_id} scheduled for {args.to} on "
                    f"run {target_run['id']}'s "
                    "next safe action boundary"
                )
                return

        # No active runner can accept an injected message. Keep ordinary inbox
        # behavior for orchestrators, inactive profiles, and ensemble leads.
        # A supervised sender's run remains useful context for those messages.
        run_id = explicit_run_id if explicit_run_id is not None else _run_id(args)
        if target_run is not None:
            run_id = int(target_run["id"])
        elif explicit_run_id is None and args.to in cfg.get("agents", {}):
            # Profile-wide mail is not owned by the sender's supervised run.
            # The next run for this profile can claim the unbound message.
            run_id = None
        con.execute(
            "INSERT INTO messages(sender, recipient, body, work_item, run_id, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (sender, args.to, body, args.work, run_id, db.now()),
        )
        con.commit()
    finally:
        con.close()
    print(f"sent {sender} -> {args.to}")


def _supervised_worker_run(con, *, action: str):
    raw_run_id = os.environ.get("ORCHESTRA_RUN_ID")
    identity = os.environ.get("ORCHESTRA_SELF")
    try:
        run_id = int(raw_run_id) if raw_run_id else None
    except ValueError:
        run_id = None
    if run_id is None or not identity:
        raise SystemExit(
            f"orchestra: {action} is worker-only and requires a supervised run"
        )
    run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not run:
        raise SystemExit(f"orchestra: supervised run {run_id} not found")
    if run["agent"] != identity:
        raise SystemExit(
            f"orchestra: supervised identity mismatch for run {run_id}"
        )
    return run


def _worker_message(args, *, handoff: bool) -> None:
    root = paths.find_root()
    body = " ".join(args.message).strip()
    if not body:
        raise SystemExit(f"orchestra: empty {'handoff' if handoff else 'report'}")

    verification_status = getattr(args, "verification_status", None) if handoff else None
    environment_blocker = getattr(args, "blocked_environment", None) if handoff else None
    if environment_blocker:
        verification_status = "blocked_environment"

    con = db.connect(root)
    try:
        con.execute("BEGIN IMMEDIATE")
        run = _supervised_worker_run(
            con, action="handoff" if handoff else "report"
        )
        run_id = int(run["id"])
        prefix = "HANDOFF" if handoff else "REPORT"
        text = f"{prefix} run {run_id}: {body}"
        if verification_status:
            con.execute(
                "UPDATE runs SET verification_status=?, environment_blocker=? WHERE id=?",
                (verification_status, environment_blocker, run_id),
            )
        if environment_blocker:
            cfg = config.load(root)
            backend, sandbox = _capability_lane(cfg, run["agent"])
            host_identity = socket.gethostname()
            evidence = (
                f"Run {run_id} reported that {environment_blocker} was unavailable: {body}"
            )
            capabilities.record_observation_in_connection(
                con,
                host_identity=host_identity,
                backend=backend,
                profile=run["agent"],
                sandbox_mode=sandbox,
                capability=environment_blocker,
                state="unsupported",
                evidence=evidence,
                probe="worker handoff",
            )
            incidents.record_incident(
                con,
                fingerprint=f"environment-blocked:{environment_blocker}",
                scope=(
                    f"host={host_identity};backend={backend};profile={run['agent']};"
                    f"sandbox={sandbox}"
                ),
                title=f"Environment capability unavailable: {environment_blocker}",
                evidence=evidence,
                run_id=run_id,
                work_item=run["work_item"],
                estimated_lost_seconds=_run_elapsed_seconds(run["started_at"]),
                remediation=(
                    "Route verification to a profile with fresh supported evidence, "
                    "or change the approved sandbox policy and re-probe."
                ),
            )
        con.execute(
            "INSERT INTO messages(sender, recipient, body, work_item, run_id, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (run["agent"], run["requested_by"], text, run["work_item"], run_id, db.now()),
        )
        con.commit()
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()
    print(f"{prefix.lower()} sent for run {run_id} -> {run['requested_by']}")


def cmd_report(args):
    _worker_message(args, handoff=False)


def cmd_handoff(args):
    _worker_message(args, handoff=True)


def _run_elapsed_seconds(started_at: str | None) -> int:
    """Best-effort measured elapsed time for an incident occurrence."""
    if not started_at:
        return 0
    try:
        started = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return 0
    return max(0, int((datetime.now(timezone.utc) - started).total_seconds()))


def cmd_consult(args):
    """Ask the recorded requester, optionally pausing with a bounded fallback."""
    root = paths.find_root()
    cfg = config.load(root)
    question = " ".join(args.question).strip()
    if not question:
        raise SystemExit("orchestra: consultation question must not be empty")
    if len(question) > 4000:
        raise SystemExit("orchestra: consultation question is limited to 4000 characters")
    raw_wait = getattr(args, "wait", None)
    fallback = (getattr(args, "fallback", None) or "").strip()
    if raw_wait is None and fallback:
        raise SystemExit("orchestra: --fallback requires --wait")
    if raw_wait is not None and not fallback:
        raise SystemExit("orchestra: --wait requires a non-empty --fallback")
    if len(fallback) > 4000:
        raise SystemExit("orchestra: consultation fallback is limited to 4000 characters")
    wait_seconds = config.question_wait_seconds(raw_wait) if raw_wait is not None else None

    con = db.connect(root)
    routed_lead_id = None
    consultation_id = None
    try:
        con.execute("BEGIN IMMEDIATE")
        run = _supervised_worker_run(con, action="consult")
        run_id = int(run["id"])
        requester = run["requested_by"]
        created_at = db.now()
        kind = "consult"
        if wait_seconds is not None:
            if run["containment_mode"]:
                raise SystemExit(
                    "orchestra: contained Operator runs cannot pause through consult; "
                    "the controller owns revised instructions and retries"
                )
            if run["status"] != "running":
                raise SystemExit(f"orchestra: run {run_id} is {run['status']}, not running")
            if not run["session_ref"]:
                raise SystemExit(
                    f"orchestra: run {run_id}'s session is not resumable yet; "
                    "continue with the declared fallback"
                )
            if con.execute("SELECT 1 FROM questions WHERE run_id=?", (run_id,)).fetchone():
                raise SystemExit(
                    f"orchestra: run {run_id} already used its one blocking question"
                )
            deadline_at = db.after(wait_seconds)
            con.execute(
                "INSERT INTO questions(run_id, sender, recipient, question, "
                "recommended_default, asked_at, deadline_at) VALUES(?,?,?,?,?,?,?)",
                (run_id, run["agent"], requester, question, fallback, created_at, deadline_at),
            )
            body = (
                f"[QUESTION run {run_id}] {question}\n"
                f"Recommended fallback: {fallback}\n"
                f"Auto-resumes with that fallback in {wait_seconds} seconds.\n"
                f"Answer: `orchestra answer {run_id} \"<answer>\" --as {requester}`"
            )
            kind = "question"
            con.execute("UPDATE runs SET status='waiting_input' WHERE id=?", (run_id,))
        elif run["containment_mode"]:
            body = (
                f"CONSULT run {run_id} ({run['agent']}; worker is continuing): {question}\n"
                "This is a contained Operator run. The consultation is recorded for its "
                "controller and owner; the controller owns any revised instructions or retry."
            )
        else:
            body = (
                f"CONSULT run {run_id} ({run['agent']}; worker is continuing): {question}\n"
                f"Guide the active run with `orchestra interrupt {run_id} "
                f"\"<guidance>\" --as {requester}`; if it already finished, use "
                f"`orchestra resume {run_id} \"<guidance>\" --as {requester}`."
            )
        inserted = con.execute(
            "INSERT INTO messages(sender, recipient, body, work_item, run_id, kind, "
            "created_at) VALUES(?,?,?,?,?,?,?)",
            (
                run["agent"],
                requester,
                body,
                run["work_item"],
                run_id,
                kind,
                created_at,
            ),
        )
        consultation_id = int(inserted.lastrowid)

        # Spawned children retain an exact edge to the supervised lead that
        # requested them. Route the advisory there immediately when its
        # supervisor supports safe delivery; never guess from a profile name.
        lead = None
        if run["lead_run"] is not None:
            lead = con.execute(
                "SELECT * FROM runs WHERE id=? AND status NOT IN "
                "('done','failed','timeout','killed')",
                (run["lead_run"],),
            ).fetchone()
        lead_profile = (
            cfg.get("agents", {}).get(lead["agent"], {})
            if lead is not None else {}
        )
        if (
            lead is not None
            and lead["agent"] == requester
            and int(lead["supervisor_protocol"] or 0) >= 1
            and not lead_profile.get("ensemble")
        ):
            con.execute(
                "UPDATE messages SET read_at=? WHERE id=?",
                (created_at, consultation_id),
            )
            _record_interrupt(
                con,
                lead,
                sender=run["agent"],
                body=body,
                immediate=False,
            )
            routed_lead_id = int(lead["id"])
        else:
            con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()

    if wait_seconds is not None:
        destination = f"lead run {routed_lead_id}" if routed_lead_id else requester
        print(
            f"consultation #{consultation_id} sent to {destination}; run {run_id} paused "
            f"for up to {wait_seconds} seconds before applying its fallback"
        )
    elif routed_lead_id is not None:
        print(
            f"consultation #{consultation_id} routed to requester on lead run "
            f"{routed_lead_id}; run {run_id} keeps working"
        )
    else:
        print(
            f"consultation #{consultation_id} sent to {requester}; run {run_id} "
            "keeps working"
        )


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
    active_run = _run_id(args) if args.name is None else None
    try:
        q = "SELECT * FROM messages WHERE recipient=?"
        params: list[object] = [who]
        if active_run is not None:
            q += " AND (run_id=? OR run_id IS NULL)"
            params.append(active_run)
        if not args.all:
            q += " AND read_at IS NULL" if args.unread else ""
        rows = list(con.execute(q + " ORDER BY id", params))
        if not args.all and not args.unread:
            rows = [r for r in rows if r["read_at"] is None]
        if args.mark_read and rows:
            con.execute(f"UPDATE messages SET read_at=? WHERE id IN "
                        f"({','.join(str(r['id']) for r in rows)}) AND read_at IS NULL",
                        (db.now(),))
            con.commit()
    finally:
        con.close()
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
    if os.environ.get("ORCHESTRA_RUN_ID"):
        raise SystemExit(
            "orchestra: supervised workers cannot use top-level `orchestra dispatch`; "
            "use `orchestra spawn --to AGENT \"mission\"` so the outer supervisor "
            "can launch children outside the worker sandbox"
        )
    requester = _identity(args, cfg)
    mission = " ".join(args.mission)
    if args.brief_file:
        mission = Path(args.brief_file).read_text()
    if not mission.strip():
        raise SystemExit("orchestra: empty mission (pass text, or --brief-file)")
    work_snapshot_text = None
    if args.work:
        # Resolve before creating any run rows. A brief that names an invisible
        # tracker item is not durable context, especially in an isolated worktree.
        work_snapshot_text = brief.work_snapshot(root, args.work, required=True)

    target_agents = [(target, config.agent_cfg(cfg, target)) for target in args.to]
    writes_tree = not bool(getattr(args, "read_only", False))
    if writes_tree and not args.worktree and len(target_agents) > 1:
        raise SystemExit(
            "orchestra: multiple shared-tree writers cannot be dispatched together; "
            "use --worktree for isolated edits or --read-only for independent analysis"
        )
    required_capabilities = list(
        dict.fromkeys(getattr(args, "requires_capability", None) or [])
    )
    for target, agent in target_agents:
        if not required_capabilities:
            break
        backend, sandbox = _capability_lane(cfg, target)
        check = capabilities.check_requirements(
            root,
            host_identity=socket.gethostname(),
            backend=backend,
            profile=target,
            sandbox_mode=sandbox,
            capabilities=required_capabilities,
        )
        if not check.satisfied:
            failures = "; ".join(
                f"{name}={','.join(sorted(getattr(check, name))) or '-'}"
                for name in ("unsupported", "unknown", "missing", "expired")
                if getattr(check, name)
            )
            raise SystemExit(
                f"orchestra: profile '{target}' lacks fresh evidence for required "
                f"environment capabilities ({failures}). Record a probe with "
                "`orchestra capability record`, or choose a proven profile."
            )
    _availability_report, unavailable, availability_warnings = \
        availability.check_profiles(cfg, target_agents)
    if unavailable:
        detail = "\n".join(f"  - {item}" for item in unavailable)
        raise SystemExit(
            "orchestra: unavailable launch profile(s):\n"
            f"{detail}\nRun `orchestra discover` after installing or authenticating the "
            "required backend/provider."
        )
    for warning in dict.fromkeys(availability_warnings):
        print(f"orchestra: availability unknown for {warning} — dispatch continuing",
              file=sys.stderr)
    ensemble_targets = [name for name, agent in target_agents if agent.get("ensemble")]
    if ensemble_targets:
        ensemble.require_plugin(ensemble_targets)

    allow_question = bool(getattr(args, "allow_question", False))
    configured_question_wait = getattr(args, "question_wait", None)
    if configured_question_wait is not None and not allow_question:
        raise SystemExit("orchestra: --question-wait requires --allow-question")
    if configured_question_wait is None:
        configured_question_wait = cfg["settings"].get(
            "question_wait_timeout", config.DEFAULT_QUESTION_WAIT_SECONDS
        )
    question_wait = config.question_wait_seconds(configured_question_wait)
    verification_required = bool(getattr(args, "require_verification", False))
    verification_status = "pending" if verification_required else "not_required"

    con = db.connect(root)
    try:
        after_ids = dependencies.validate(con, list(getattr(args, "after", None) or []))
        wait_for_ids = dependencies.validate(
            con, list(getattr(args, "wait_for", None) or [])
        )
        overlap = set(after_ids) & set(wait_for_ids)
        if overlap:
            raise SystemExit(
                "orchestra: the same prerequisite cannot be both --after and --wait-for: "
                + ", ".join(map(str, sorted(overlap)))
            )
    except BaseException:
        con.close()
        raise
    if args.team:
        if not con.execute("SELECT 1 FROM teams WHERE name=?", (args.team,)).fetchone():
            con.close()
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
    for target, agent in target_agents:
        base_commit = None
        if writes_tree and not args.worktree:
            conflicts = dependencies.shared_writer_conflicts(
                con, exclude_run_ids=[*after_ids, *wait_for_ids]
            )
            if conflicts:
                con.close()
                raise SystemExit(
                    "orchestra: shared-tree writer collision with active/pending run(s) "
                    + ", ".join(map(str, conflicts))
                    + ". Use --worktree for isolated edits or --read-only when this "
                    "dispatch cannot modify the tree."
                )
            if not (after_ids or wait_for_ids) and (root / ".git").exists():
                if worktree.status(root):
                    con.close()
                    raise SystemExit(
                        "orchestra: the integration tree is dirty; commit/stash it, use "
                        "--worktree for isolated edits, or --read-only for analysis"
                    )
                base_commit = worktree.head(root)
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
                    "requested_by, workdir, slug, allow_question, question_wait_seconds, "
                    "verification_required, verification_status, required_capabilities_json, "
                    "writes_tree, base_commit, status, started_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (target, agent["backend"], display_model,
                     args.title or mission[:80],
                     args.work, args.team, requester, str(root), slug,
                     int(allow_question), question_wait,
                     int(verification_required), verification_status,
                     json.dumps(required_capabilities),
                     int(writes_tree), base_commit,
                     "pending" if after_ids or wait_for_ids else "spawning", db.now()))
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
        if writes_tree and not args.worktree:
            conflicts = dependencies.shared_writer_conflicts(
                con,
                exclude_run_ids=[run_id, *after_ids, *wait_for_ids],
            )
            if conflicts:
                con.execute("DELETE FROM runs WHERE id=?", (run_id,))
                con.commit()
                con.close()
                raise SystemExit(
                    "orchestra: shared-tree writer collision with active/pending run(s) "
                    + ", ".join(map(str, conflicts))
                    + ". Use --worktree for isolated edits or --read-only when this "
                    "dispatch cannot modify the tree."
                )
        workdir, branch = str(root), None
        if after_ids or wait_for_ids:
            try:
                dependencies.enqueue(
                    con,
                    run_id,
                    after_ids,
                    mission=mission,
                    context=args.context,
                    use_worktree=bool(args.worktree),
                    wait_for_ids=wait_for_ids,
                    work_snapshot=work_snapshot_text,
                    required_capabilities=required_capabilities,
                    writes_tree=not bool(getattr(args, "read_only", False)),
                )
            except dependencies.SharedWriterConflict as exc:
                con.execute("DELETE FROM runs WHERE id=?", (run_id,))
                con.commit()
                con.close()
                raise SystemExit(
                    "orchestra: pending shared-tree writer collision with run(s) "
                    + ", ".join(map(str, exc.run_ids))
                    + ". Use --worktree for isolated edits or --read-only when this "
                    "dispatch cannot modify the tree."
                ) from exc
        else:
            if args.worktree:
                wt, branch = worktree.create(root, run_id)
                workdir = str(wt)
                base_commit = worktree.head(wt)
            text = brief.compose(root=root, run_id=run_id, agent=agent, mission=mission,
                                 work_item=args.work, team=args.team, requester=requester,
                                 workdir=workdir, extra_context=args.context,
                                 allow_question=allow_question,
                                 question_wait_seconds=question_wait, slug=slug,
                                 work_snapshot_text=work_snapshot_text,
                                 required_capabilities=required_capabilities,
                                 require_verification=verification_required,
                                 writes_tree=writes_tree)
            bp = paths.briefs_dir(root) / f"run-{run_id}.md"
            bp.write_text(text)
            lp = paths.logs_dir(root) / f"run-{run_id}.jsonl"
            lp.touch()
            con.execute(
                "UPDATE runs SET brief_path=?, log_path=?, workdir=?, branch=?, "
                "base_commit=? WHERE id=?",
                (str(bp), str(lp), workdir, branch, base_commit, run_id),
            )
        con.commit()
        run_ids.append(run_id)
        disposition = (
            "queued for prerequisite run(s) "
            + ", ".join(map(str, [*after_ids, *wait_for_ids]))
            if after_ids or wait_for_ids else "dispatched"
        )
        _work_log(root, args.work, f"orchestra: {disposition} run {run_id} ({slug}) to {target} "
                                   f"({agent['backend']}/{agent.get('model') or 'default'})"
                                   + (f" in worktree branch {branch}" if branch else ""))
        print(f"run {run_id} ({slug}): {target} ({agent['backend']}/{agent.get('model') or 'default'})"
              + (f" pending-on={','.join(map(str, after_ids))}" if after_ids else "")
              + (f" wait-for={','.join(map(str, wait_for_ids))}" if wait_for_ids else "")
              + (f" worktree={workdir}" if branch else ""))
    if after_ids or wait_for_ids:
        dependencies.process_ready(con, root, cfg, _spawn_supervisor)
    con.close()
    if after_ids or wait_for_ids:
        if args.sync:
            cmd_wait(argparse.Namespace(
                run_ids=run_ids,
                timeout=None,
                any=False,
                as_=requester,
            ))
        else:
            print(
                "dependency-aware dispatch recorded. It will launch once every prerequisite "
                "meets its declared edge condition (`--after` requires success; `--wait-for` "
                "requires only a terminal outcome). Cancel it with `orchestra cancel RUN`."
            )
        return
    for rid in run_ids:
        if args.sync:
            supervise.supervise(root, rid)
        else:
            _spawn_supervisor(root, rid)
    if not args.sync:
        print(f"dispatched async. `orchestra wait {' '.join(map(str, run_ids))}` blocks until done; "
              f"completions land in inbox '{requester}'.")


def cmd_spawn(args):
    """Spawn a bounded child batch from the currently supervised worker."""
    root = paths.find_root()
    cfg = config.load(root)
    raw_parent = os.environ.get("ORCHESTRA_RUN_ID")
    identity = os.environ.get("ORCHESTRA_SELF")
    try:
        parent_id = int(raw_parent or "")
    except ValueError:
        raise SystemExit(
            "orchestra: spawn is worker-only and requires ORCHESTRA_RUN_ID from a supervisor"
        )
    mission = " ".join(args.mission)
    if args.brief_file:
        mission = Path(args.brief_file).read_text()
    if not mission.strip():
        raise SystemExit("orchestra: empty child mission (pass text, or --brief-file)")
    for target in args.to:
        config.agent_cfg(cfg, target)
    con = db.connect(root)
    try:
        parent = child_runs.validate_parent(con, cfg, parent_id, identity)
        child_runs.validate_tiers(cfg, parent, list(args.to))
        request_id = child_runs.enqueue(
            con, parent, list(args.to), mission,
            title=args.title, context=args.context,
            shared_workdir=args.shared_workdir,
        )
        deadline = time.monotonic() + 30
        request = None
        while time.monotonic() < deadline:
            request = con.execute(
                "SELECT * FROM spawn_requests WHERE id=?", (request_id,)
            ).fetchone()
            if request and request["status"] in ("accepted", "failed"):
                break
            time.sleep(0.1)
        if request and request["status"] == "pending":
            timed_out = con.execute(
                "UPDATE spawn_requests SET status='failed', error=?, processed_at=? "
                "WHERE id=? AND status='pending'",
                ("outer supervisor did not accept the request within 30 seconds",
                 db.now(), request_id),
            )
            con.commit()
            if timed_out.rowcount == 1:
                request = con.execute(
                    "SELECT * FROM spawn_requests WHERE id=?", (request_id,)
                ).fetchone()
    finally:
        con.close()
    if request and request["status"] == "processing":
        print(
            f"spawn request {request_id}: accepted by the outer supervisor for "
            f"lead run {parent_id}; child setup is still in progress"
        )
        return
    if not request:
        raise SystemExit(
            f"orchestra: spawn request {request_id} disappeared before acknowledgement"
        )
    if request["status"] == "failed":
        raise SystemExit(
            f"orchestra: spawn request {request_id} failed without terminating "
            f"lead run {parent_id}: {request['error'] or 'unknown broker error'}"
        )
    run_ids = json.loads(request["child_run_ids_json"] or "[]")
    if request["error"]:
        print(f"orchestra: warning: {request['error']}", file=sys.stderr)
    for run_id in run_ids:
        print(f"child run {run_id}: spawned for lead run {parent_id}")
    print("Child completions will notify this lead; if it exits first, Orchestra will "
          "resume it exactly once after the batch settles.")


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
    root = paths.find_root()
    cfg = config.load(root)
    con = db.connect(root)
    parent = None
    run_id = None
    try:
        con.execute("BEGIN IMMEDIATE")
        requested = con.execute("SELECT * FROM runs WHERE id=?", (args.run_id,)).fetchone()
        if not requested:
            raise SystemExit(f"orchestra: no run {args.run_id}")
        line = _continuation_line(con, args.run_id)
        parent = line[-1]
        if not parent["session_ref"]:
            raise SystemExit(
                f"orchestra: run {parent['id']} has no session ref; dispatch a fresh run"
            )
        # Session refs identify the backend conversation, so check globally
        # rather than only below the selected lineage node. This also closes
        # off concurrent resumes from a historical, accidentally branched tip.
        active = list(con.execute(
            "SELECT * FROM runs WHERE session_ref=? AND status NOT IN "
            "('done','failed','timeout','killed','interrupt') ORDER BY id",
            (parent["session_ref"],),
        ))
        if active:
            current = active[-1]
            raise SystemExit(
                f"orchestra: run {args.run_id}'s session is already active as run "
                f"{current['id']} ({current['status']}) — use `orchestra interrupt` or "
                "`orchestra queue` instead"
            )
        if parent["status"] == "interrupt":
            # A detached supervisor may have died after stopping its worker but
            # before launching the session resume. Terminalize that orphan while
            # holding the continuation lock, then create its next attempt.
            con.execute(
                "UPDATE runs SET status='killed', finished_at=COALESCE(finished_at, ?) "
                "WHERE id=? AND status='interrupt'",
                (db.now(), parent["id"]),
            )
        requester = _identity(args, cfg) or parent["requested_by"]
        msg = " ".join(args.message)
        run_id = supervise.create_followup(
            con, root, dict(parent), requester, msg,
            title=f"continuation of run {parent['id']}", commit=False,
        )
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()
    requested_note = (f" (requested from run {args.run_id})"
                      if parent["id"] != args.run_id else "")
    print(f"run {run_id}: continuing run {parent['id']}'s session with "
          f"{parent['agent']}{requested_note} (session {parent['session_ref'][:20]}...)")
    if args.sync:
        supervise.supervise(root, run_id)
    else:
        _spawn_supervisor(root, run_id)


def _run_status_label(run: sqlite3.Row) -> str:
    status = str(run["status"])
    if status in db.RUN_TERMINAL and int(run["verification_required"] or 0):
        return f"{status}/{run['verification_status']}"
    return status


def cmd_runs(args):
    root = paths.find_root()
    con = db.connect(root)
    for item in reap.reap_orphans(con, root):
        print(f"reconciled: {item['note']}", file=sys.stderr)
    q = "SELECT * FROM runs" + (" WHERE status NOT IN ('done','failed','timeout','killed')"
                                if args.active else "") + " ORDER BY id"
    rows = list(con.execute(q))
    if args.json:
        print(json.dumps([dict(r) for r in rows], indent=2))
        con.close()
        return
    if not rows:
        print("(no runs)")
        con.close()
        return
    print(f"{'id':<4} {'agent':<10} {'status':<18} {'work':<8} {'started':<21} title")
    for r in rows:
        waiting = dependencies.pending_on(con, r["id"]) if r["status"] == "pending" else []
        held = dependencies.held_on(con, r["id"]) if r["status"] == "pending" else []
        status = (
            f"held-on-{','.join(map(str, held))}" if held
            else f"pending-on-{','.join(map(str, waiting))}" if waiting
            else _run_status_label(r)
        )
        print(f"{r['id']:<4} {r['agent']:<10} {status:<18} {r['work_item'] or '-':<8} "
              f"{r['started_at']:<21} {(r['title'] or '')[:60]}")
    con.close()


def cmd_run_show(args):
    root = paths.find_root()
    con = db.connect(root)
    reap.reap_orphans(con, root)
    r = con.execute("SELECT * FROM runs WHERE id=?", (args.run_id,)).fetchone()
    if not r:
        con.close()
        raise SystemExit(f"orchestra: no run {args.run_id}")
    for k in r.keys():
        v = r[k]
        if k == "summary" and v:
            print(f"{k}:\n  " + v.replace("\n", "\n  "))
        else:
            print(f"{k}: {v}")
    dependency_ids = [
        row["depends_on_run"] for row in con.execute(
            "SELECT depends_on_run FROM dispatch_dependencies WHERE run_id=? "
            "ORDER BY depends_on_run",
            (args.run_id,),
        )
    ]
    if dependency_ids:
        print("depends_on: " + ", ".join(map(str, dependency_ids)))
    con.close()


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
    cfg = config.load(root)
    requester = _identity(args, cfg)
    con = db.connect(root)
    reap.reap_orphans(con, root)
    if args.run_ids:
        targets = set(args.run_ids)
    else:
        targets = {r["id"] for r in con.execute(
            "SELECT id FROM runs WHERE status NOT IN ('done','failed','timeout','killed')")}
    if not targets:
        print("no active runs")
        con.close()
        return
    print(f"waiting on runs: {sorted(targets)}")
    deadline = time.time() + args.timeout if args.timeout else None
    pending = set(targets)
    while pending:
        if deadline and time.time() > deadline:
            print(f"timeout; still pending: {sorted(pending)}")
            con.close()
            sys.exit(2)
        consultation_query = (
            "SELECT * FROM messages WHERE kind='consult' AND recipient=? "
            f"AND read_at IS NULL AND run_id IN ({','.join(map(str, pending))}) "
            "ORDER BY id"
        )
        consultations = list(con.execute(consultation_query, (requester,)))
        if consultations:
            # Claim after acquiring the write lock so two orchestrators waiting
            # under the same identity cannot both believe they received it.
            con.execute("BEGIN IMMEDIATE")
            consultations = list(con.execute(consultation_query, (requester,)))
            read_at = db.now()
            if consultations:
                con.execute(
                    f"UPDATE messages SET read_at=? WHERE id IN "
                    f"({','.join(str(row['id']) for row in consultations)}) "
                    "AND read_at IS NULL",
                    (read_at,),
                )
            con.execute("COMMIT")
        if consultations:
            print("guidance requested; workers are still running:")
            for message in consultations:
                print(f"\n[{message['id']}] {message['body']}")
            con.close()
            return
        rows = con.execute(
            f"SELECT id, agent, status, exit_code FROM runs WHERE id IN "
            f"({','.join(map(str, pending))}) AND status IN ('done','failed','timeout','killed')").fetchall()
        for r in rows:
            print(f"run {r['id']} ({r['agent']}) -> {r['status']}"
                  + (f" exit {r['exit_code']}" if r["exit_code"] not in (None, 0) else ""))
            pending.discard(r["id"])
            if args.any:
                con.close()
                return
        if pending:
            time.sleep(2)
            # An orphaned run never changes state on its own, so without this
            # the wait below would block until the timeout — or forever.
            for item in reap.reap_orphans(con, root):
                print(f"reconciled: {item['note']}", file=sys.stderr)
    print("all runs finished — check your inbox: `orchestra inbox <you> --unread --mark-read`")
    con.close()


def _question_run_id(args) -> int:
    raw = getattr(args, "run_id", None) or os.environ.get("ORCHESTRA_RUN_ID")
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise SystemExit("orchestra: question command needs --run or $ORCHESTRA_RUN_ID")


def cmd_ask(args):
    root = paths.find_root()
    cfg = config.load(root)
    sender = _identity(args, cfg)
    run_id = _question_run_id(args)
    question = " ".join(args.question).strip()
    recommended = args.default.strip()
    if not question or not recommended:
        raise SystemExit("orchestra: both the question and --default fallback must be non-empty")
    if len(question) > 4000 or len(recommended) > 4000:
        raise SystemExit("orchestra: question and fallback are limited to 4000 characters each")

    con = db.connect(root)
    try:
        con.execute("BEGIN IMMEDIATE")
        run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not run:
            raise SystemExit(f"orchestra: no run {run_id}")
        if run["agent"] != sender:
            raise SystemExit(
                f"orchestra: run {run_id} belongs to {run['agent']}; ask as that worker"
            )
        if not run["allow_question"]:
            raise SystemExit(
                f"orchestra: run {run_id} was not dispatched with --allow-question; "
                "continue with a documented assumption"
            )
        if run["status"] != "running":
            raise SystemExit(f"orchestra: run {run_id} is {run['status']}, not running")
        if not run["session_ref"]:
            raise SystemExit(
                f"orchestra: run {run_id}'s session is not resumable yet; "
                "continue with the recommended default"
            )
        if con.execute("SELECT 1 FROM questions WHERE run_id=?", (run_id,)).fetchone():
            raise SystemExit(
                f"orchestra: run {run_id} already used its one blocking question"
            )
        wait_seconds = int(run["question_wait_seconds"])
        asked_at, deadline_at = db.now(), db.after(wait_seconds)
        con.execute(
            "INSERT INTO questions(run_id, sender, recipient, question, recommended_default, "
            "asked_at, deadline_at) VALUES(?,?,?,?,?,?,?)",
            (run_id, sender, run["requested_by"], question, recommended, asked_at, deadline_at),
        )
        body = (
            f"[QUESTION run {run_id}] {question}\n"
            f"Recommended default: {recommended}\n"
            f"Auto-resumes with that default in {wait_seconds} seconds.\n"
            f"Answer: `orchestra answer {run_id} \"<answer>\" --as {run['requested_by']}`"
        )
        con.execute(
            "INSERT INTO messages(sender, recipient, body, work_item, run_id, kind, created_at) "
            "VALUES(?,?,?,?,?, 'question', ?)",
            (sender, run["requested_by"], body, run["work_item"], run_id, asked_at),
        )
        con.execute("UPDATE runs SET status='waiting_input' WHERE id=?", (run_id,))
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()
    print(
        f"run {run_id} paused for one answer; it will use the recommended default "
        f"after {wait_seconds} seconds"
    )


def cmd_answer(args):
    root = paths.find_root()
    cfg = config.load(root)
    answered_by = _identity(args, cfg)
    run_id = args.run_id
    answer = " ".join(args.answer).strip()
    if not answer:
        raise SystemExit("orchestra: answer must not be empty")
    if len(answer) > 4000:
        raise SystemExit("orchestra: answer is limited to 4000 characters")

    con = db.connect(root)
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT q.*, r.status AS run_status, r.requested_by FROM questions q "
            "JOIN runs r ON r.id=q.run_id WHERE q.run_id=?",
            (run_id,),
        ).fetchone()
        if not row:
            raise SystemExit(f"orchestra: run {run_id} has no blocking question")
        if row["recipient"] != answered_by:
            raise SystemExit(
                f"orchestra: question for run {run_id} is addressed to {row['recipient']}"
            )
        if row["status"] != "waiting" or row["run_status"] != "waiting_input":
            raise SystemExit(
                f"orchestra: question for run {run_id} is already {row['status']}"
            )
        answered_at = db.now()
        con.execute(
            "UPDATE questions SET status='answered', answered_at=?, answered_by=?, answer=? "
            "WHERE run_id=? AND status='waiting'",
            (answered_at, answered_by, answer, run_id),
        )
        con.execute(
            "UPDATE messages SET read_at=COALESCE(read_at, ?) "
            "WHERE run_id=? AND kind='question'",
            (answered_at, run_id),
        )
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()
    print(f"answered run {run_id}; the worker will resume its saved session")


def cmd_queue(args):
    root = paths.find_root()
    cfg = config.load(root)
    con = db.connect(root)
    followup_id = None
    queued_id = None
    try:
        con.execute("BEGIN IMMEDIATE")
        r = con.execute("SELECT * FROM runs WHERE id=?", (args.run_id,)).fetchone()
        if not r:
            raise SystemExit(f"orchestra: no run {args.run_id}")
        sender = _identity(args, cfg)
        msg = " ".join(args.message)
        if r["status"] in db.RUN_TERMINAL:
            if not r["session_ref"]:
                raise SystemExit(f"orchestra: run {args.run_id} has no session to resume — "
                                 "dispatch a fresh run instead")
            followup_id = supervise.create_followup(
                con, root, dict(r), sender, msg, commit=False
            )
        else:
            cur = con.execute(
                "INSERT INTO messages(sender, recipient, body, run_id, kind, created_at) "
                "VALUES(?,?,?,?, 'queued', ?)",
                (sender, r["agent"], msg, args.run_id, db.now()),
            )
            queued_id = int(cur.lastrowid)
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()
    if followup_id is not None:
        supervise.spawn_supervisor(root, followup_id)
        print(f"run {args.run_id} already finished — follow-up dispatched now as run "
              f"{followup_id}")
    else:
        print(f"queued message {queued_id} — will be auto-delivered as a session follow-up "
              f"when run {args.run_id} completes; recall with `orchestra recall {queued_id}`")


def cmd_recall(args):
    root = paths.find_root()
    cfg = config.load(root)
    recalled_by = _identity(args, cfg)
    con = db.connect(root)
    row = None
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM messages WHERE id=?", (args.message_id,)).fetchone()
        if not row:
            raise SystemExit(f"orchestra: no message {args.message_id}")
        if row["kind"] != "queued":
            raise SystemExit(
                f"orchestra: message {args.message_id} is not a queued follow-up"
            )
        if row["sender"] != recalled_by:
            raise SystemExit(
                f"orchestra: queued message {args.message_id} belongs to {row['sender']}; "
                "recall it as that sender"
            )
        if row["recalled_at"]:
            raise SystemExit(f"orchestra: queued message {args.message_id} was already recalled")
        if row["read_at"]:
            raise SystemExit(
                f"orchestra: queued message {args.message_id} was already delivered and "
                "cannot be recalled"
            )
        recalled_at = db.now()
        updated = con.execute(
            "UPDATE messages SET recalled_at=?, recalled_by=?, read_at=? "
            "WHERE id=? AND kind='queued' AND sender=? AND read_at IS NULL "
            "AND recalled_at IS NULL",
            (recalled_at, recalled_by, recalled_at, args.message_id, recalled_by),
        )
        if updated.rowcount != 1:
            raise SystemExit(
                f"orchestra: queued message {args.message_id} changed while recall was in "
                "progress; inspect the run before retrying"
            )
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()
    print(f"recalled queued message {args.message_id} for run {row['run_id']}")


def _record_interrupt(con, run, *, sender: str, body: str, immediate: bool) -> int:
    """Persist one guaranteed runner delivery and place its timeline marker."""
    created_at = db.now()
    try:
        pending_offset = Path(run["log_path"]).stat().st_size
    except (OSError, TypeError):
        pending_offset = 0
    cur = con.execute("INSERT INTO messages(sender, recipient, body, run_id, kind, "
                      "created_at, delivered_at, delivery_offset) "
                      "VALUES(?,?,?,?, 'interrupt', ?, ?, ?)",
                      (sender, run["agent"], f"[INTERRUPT] {body}",
                       run["id"], created_at, created_at if immediate else None,
                       pending_offset))
    message_id = int(cur.lastrowid)
    if immediate:
        con.execute("UPDATE runs SET status='interrupt' WHERE id=?", (run["id"],))
    con.commit()
    delivery_offset = supervise.append_delivery_event(run["log_path"], {
        "message_id": message_id,
        "delivery": "interrupt",
        "sender": sender,
        "recipient": run["agent"],
        "body": body,
        "created_at": created_at,
        "phase": "delivered" if immediate else "pending",
    })
    if delivery_offset is not None:
        con.execute(
            "UPDATE messages SET delivery_offset=? "
            "WHERE id=? AND delivered_at IS NULL",
            (delivery_offset, message_id),
        )
    con.commit()
    if immediate and run["pid"]:
        try:
            os.killpg(run["pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
    return message_id


def cmd_interrupt(args):
    root = paths.find_root()
    cfg = config.load(root)
    message_file = getattr(args, "message_file", None)
    if message_file:
        source = Path(message_file)
        try:
            body = source.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise SystemExit(
                f"orchestra: cannot read interrupt file '{source}': {exc}"
            ) from exc
    else:
        message = getattr(args, "message", [])
        body = message if isinstance(message, str) else " ".join(message)
    if not body.strip():
        raise SystemExit("orchestra: interrupt message must not be empty")
    con = db.connect(root)
    r = con.execute("SELECT * FROM runs WHERE id=?", (args.run_id,)).fetchone()
    if not r:
        raise SystemExit(f"orchestra: no run {args.run_id}")
    if r["status"] in db.RUN_TERMINAL:
        raise SystemExit(f"orchestra: run {args.run_id} already {r['status']} — "
                         f"use `orchestra resume {args.run_id} \"...\"` instead")
    agent = config.agent_cfg(cfg, r["agent"])
    if agent.get("ensemble"):
        raise SystemExit("orchestra: ensemble leads can't be interrupted (their team runs "
                         "server-side); use `orchestra send` — the lead reads its inbox "
                         "when teammates wake it")
    if not r["session_ref"]:
        raise SystemExit(f"orchestra: run {args.run_id}'s session isn't identified yet "
                         "(happens ~10s after spawn) — retry in a moment")
    sender = _identity(args, cfg)
    immediate = bool(getattr(args, "now", False))
    if not immediate and int(r["supervisor_protocol"] or 0) < 1:
        con.close()
        raise SystemExit(
            f"orchestra: run {args.run_id}'s detached supervisor predates safe interrupts; "
            f"use `orchestra interrupt {args.run_id} \"...\" --now`, or stop and reply "
            "to resume it under the current supervisor"
        )
    _record_interrupt(con, r, sender=sender, body=body, immediate=immediate)
    con.close()
    if immediate:
        print(f"run {args.run_id} interrupted now — worker will resume its session, read "
              "the message, and continue the mission")
    else:
        print(f"interrupt scheduled for run {args.run_id}'s next safe action boundary; "
              f"use `orchestra interrupt {args.run_id} \"...\" --now` for an emergency stop")


def cmd_kill(args):
    root = paths.find_root()
    cfg = config.load(root)
    con = db.connect(root)
    try:
        if getattr(args, "dry_run", False):
            preview = cancel.preview_stop(con, args.run_id)
            if preview is None:
                raise SystemExit(f"orchestra: no run {args.run_id}")
            print(f"would stop run {args.run_id} (currently {preview['status']})")
            if preview["descendant_ids"]:
                print("would also stop active child run(s): "
                      + ", ".join(map(str, preview["descendant_ids"])))
            print("would decline success-dependent run(s): "
                  + (", ".join(map(str, preview["declined_run_ids"])) or "none"))
            print("would hold success-dependent run(s): "
                  + (", ".join(map(str, preview["held_run_ids"])) or "none"))
            print("would release wait-only run(s): "
                  + (", ".join(map(str, preview["unblocked_run_ids"])) or "none"))
            return

        result = cancel.stop_run(con, args.run_id)
        if not result:
            raise SystemExit(f"orchestra: no run {args.run_id}")
        if not result.stopped:
            print(f"run {args.run_id} already {result.status}")
            return
        if result.signal_sent:
            print(f"sent SIGTERM to run {args.run_id} (pgid {result.pid})")
        else:
            print(f"run {args.run_id} marked killed ({result.reason})")
        if result.descendant_ids:
            print(f"also stopped {len(result.descendant_ids)} active child run(s): "
                  + ", ".join(map(str, result.descendant_ids)))
        if result.declined_run_ids:
            print("declined success-dependent run(s): "
                  + ", ".join(map(str, result.declined_run_ids)))
        if result.held_run_ids:
            print("held success-dependent run(s): "
                  + ", ".join(map(str, result.held_run_ids))
                  + " (resume the cancelled producer to rebind them)")
        if result.unblocked_run_ids:
            print("released wait-only run(s): "
                  + ", ".join(map(str, result.unblocked_run_ids)))
        # stop_run changes dependency readiness synchronously. Reconcile here
        # instead of relying on a supervisor that may not exist for a pending run.
        dependencies.process_ready(con, root, cfg, _spawn_supervisor)
    finally:
        con.close()


def cmd_note(args):
    root = paths.find_root()
    cfg = config.load(root)
    con = db.connect(root)
    author = _identity(args, cfg)
    con.execute("INSERT INTO feed(author, body, tags, work_item, run_id, created_at) "
                "VALUES(?,?,?,?,?,?)",
                (author, args.body, args.tags or "", args.work, _run_id(args), db.now()))
    con.commit()
    _work_log(root, args.work, f"[{author}] {args.body}")
    print("noted")


def _capability_lane(cfg: dict, profile: str) -> tuple[str, str]:
    agent = config.agent_cfg(cfg, profile)
    backend = agent["backend"]
    # `sandbox` is a Codex exec flag. Orchestra does not wrap OpenCode or
    # Claude in it, even if an irrelevant key appears in a merged roster.
    sandbox = (
        agent.get("sandbox", "workspace-write")
        if backend == "codex"
        else "orchestra-unrestricted"
    )
    return backend, str(sandbox)


def cmd_capability_record(args):
    root = paths.find_root()
    cfg = config.load(root)
    backend, sandbox = _capability_lane(cfg, args.profile)
    ttl = None if args.permanent else timedelta(seconds=args.ttl_seconds)
    observation = capabilities.record_observation(
        root,
        host_identity=args.host or socket.gethostname(),
        backend=backend,
        profile=args.profile,
        sandbox_mode=sandbox,
        capability=args.capability,
        state=args.state,
        evidence=args.evidence,
        probe=args.probe,
        ttl=ttl,
    )
    print(
        f"{observation.profile}/{observation.sandbox_mode} "
        f"{observation.capability}={observation.state}"
    )


def cmd_capability_check(args):
    root = paths.find_root()
    cfg = config.load(root)
    backend, sandbox = _capability_lane(cfg, args.profile)
    result = capabilities.check_requirements(
        root,
        host_identity=args.host or socket.gethostname(),
        backend=backend,
        profile=args.profile,
        sandbox_mode=sandbox,
        capabilities=args.capabilities,
    )
    payload = {
        "satisfied": result.satisfied,
        **{
            name: sorted(getattr(result, name))
            for name in ("supported", "unsupported", "unknown", "missing", "expired")
        },
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    if not result.satisfied:
        raise SystemExit(1)


def cmd_capability_list(args):
    root = paths.find_root()
    rows = capabilities.list_observations(
        root,
        host_identity=args.host,
        backend=args.backend,
        profile=args.profile,
        sandbox_mode=args.sandbox,
        capability=args.capability,
        include_expired=not args.fresh,
    )
    if args.json:
        print(json.dumps([
            {
                "host_identity": row.host_identity,
                "backend": row.backend,
                "profile": row.profile,
                "sandbox_mode": row.sandbox_mode,
                "capability": row.capability,
                "state": row.state,
                "evidence": row.evidence,
                "probe": row.probe,
                "observed_at": row.observed_at.isoformat(),
                "expires_at": row.expires_at.isoformat() if row.expires_at else None,
            }
            for row in rows
        ], indent=2, sort_keys=True))
        return
    if not rows:
        print("no capability observations")
        return
    for row in rows:
        expiry = row.expires_at.isoformat() if row.expires_at else "permanent"
        print(
            f"{row.host_identity} {row.profile}/{row.sandbox_mode} "
            f"{row.capability}={row.state} observed={row.observed_at.isoformat()} "
            f"expires={expiry} — {row.evidence}"
        )


def cmd_incident_record(args):
    root = paths.find_root()
    con = db.connect(root)
    try:
        incident = incidents.record_incident(
            con,
            fingerprint=args.fingerprint,
            scope=args.scope,
            title=args.title,
            evidence=args.evidence,
            run_id=args.run,
            work_item=args.work,
            estimated_lost_seconds=args.lost_seconds,
            remediation=args.remediation,
        )
        con.commit()
    finally:
        con.close()
    print(f"incident {incident['id']} {incident['state']} occurrences={incident['occurrence_count']}")


def cmd_incident_list(args):
    root = paths.find_root()
    con = db.connect(root)
    try:
        rows = incidents.list_incidents(
            con, state=args.state, scope=args.scope, fingerprint=args.fingerprint
        )
    finally:
        con.close()
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return
    if not rows:
        print("no systemic incidents")
        return
    for row in rows:
        print(
            f"{row['id']} [{row['state']}] {row['title']} "
            f"occurrences={row['occurrence_count']} lost={row['estimated_lost_seconds']}s "
            f"scope={row['scope']}"
        )


def cmd_incident_state(args):
    root = paths.find_root()
    con = db.connect(root)
    try:
        incident = incidents.set_incident_state(
            con,
            args.incident_id,
            args.state,
            remediation=args.remediation,
            resolution_evidence=args.resolution_evidence,
        )
        con.commit()
    finally:
        con.close()
    print(f"incident {incident['id']} -> {incident['state']}")


def cmd_feed(args):
    root = paths.find_root()
    con = db.connect(root)
    try:
        q, params = "SELECT * FROM feed", []
        if args.tag:
            q += " WHERE tags LIKE ?"
            params.append(f"%{args.tag}%")
        q += " ORDER BY id DESC LIMIT ?"
        params.append(args.limit)
        rows = list(con.execute(q, params))
    finally:
        con.close()
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
        waiting = dependencies.pending_on(con, r["id"]) if r["status"] == "pending" else []
        held = dependencies.held_on(con, r["id"]) if r["status"] == "pending" else []
        status = (
            f"held-on-{','.join(map(str, held))}" if held
            else f"pending-on-{','.join(map(str, waiting))}" if waiting
            else _run_status_label(r)
        )
        print(f"  run {r['id']}: {r['agent']} [{status}] work:{r['work_item'] or '-'} "
              f"since {r['started_at']} — {(r['title'] or '')[:50]}")
    recent = list(con.execute("SELECT * FROM runs WHERE status IN "
                              "('done','failed','timeout','killed') ORDER BY id DESC LIMIT 5"))
    if recent:
        print("## recent finished")
        for r in recent[::-1]:
            print(f"  run {r['id']}: {r['agent']} -> {_run_status_label(r)} — "
                  f"{(r['title'] or '')[:50]}")
    open_incidents = incidents.list_incidents(con, state="open")
    print(f"## open systemic incidents ({len(open_incidents)})")
    for incident in open_incidents[:10]:
        print(f"  {incident['id']}: {incident['title']} "
              f"({incident['occurrence_count']} occurrence(s), "
              f"{incident['estimated_lost_seconds']}s estimated lost)")
    if not open_incidents:
        print("  (none)")
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
    con.close()


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


def cmd_discover(args):
    root = _maybe_root()
    cfg = config.load(root)
    report = availability.discover(cfg, refresh=getattr(args, "refresh", False))
    if getattr(args, "json", False):
        payload = dict(report)
        if getattr(args, "query", None) is not None:
            payload["model_matches"] = availability.search_models(report, args.query)
        print(json.dumps(payload, indent=2))
        return
    print("orchestra discover\n")
    print(availability.render(report, getattr(args, "query", None)))


def cmd_doctor(args):
    root = _maybe_root()
    cfg = config.load(root)
    report = availability.discover(cfg, refresh=getattr(args, "refresh", False))
    print("orchestra doctor\n")
    print(availability.render(report))
    print("\nauxiliary tools:")
    for tool in ["work", "git"]:
        path = shutil.which(tool)
        print(f"  {tool:<9} {'available · ' + path if path else 'unavailable'}")
    ensemble_agents = ensemble.configured_agents(cfg)
    if ensemble_agents:
        status = ensemble.plugin_status()
        state = f"configured ({status.detail})" if status.configured else f"MISSING — {status.detail}"
        print(f"\n  optional opencode-ensemble plugin: {state}")
    if root:
        print(f"\n  project root: {root}")
        print(f"  work tracker: {'present' if (root / '.work').is_dir() else 'absent (run `work init .`)'}")


def cmd_host(args):
    if args.host_cmd == "stop":
        print("host stopped" if host.stop() else "host was not running")
    elif args.host_cmd == "start":
        ensemble.require_plugin(["host"])
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


def _format_account_balance(balance: dict | None) -> str:
    if not isinstance(balance, dict):
        return ""
    remaining = balance.get("remaining")
    currency = balance.get("currency")
    if not isinstance(remaining, (int, float)) or currency != "USD":
        return ""
    note = f" · ${remaining:.2f} balance"
    spent = balance.get("spent")
    if isinstance(spent, (int, float)):
        scope = balance.get("spent_scope")
        note += f" · ${spent:.2f} spent"
        if isinstance(scope, str) and scope.strip():
            note += f" ({scope.strip()})"
    return note


def cmd_usage(args):
    print("## provider runway")
    root = _maybe_root()
    snap = with_project_spend(
        default_service().snapshot(force=args.refresh),
        root,
    )
    rec = snap.get("recommendation") or {}
    if rec:
        print(f"  best runway: {rec.get('provider_name')} "
              f"({rec.get('headroom_percent'):.0f}% headroom across coding windows)")
    else:
        print("  (no provider returned a usable coding headroom yet)")
    provider_rows = snap.get("providers") or []
    provider_name_width = max(
        8, *(len(str(row.get("name") or "")) for row in provider_rows)
    )
    for row in provider_rows:
        plan = row.get("plan") or "—"
        headroom = row.get("headroom_percent")
        headroom_s = f"{headroom:.0f}%" if isinstance(headroom, (int, float)) else "n/a"
        resets = row.get("rate_limit_resets")
        # Only the Codex collector populates `rate_limit_resets`; other
        # providers leave it None and we render the count line only when
        # the wire carries an actual Codex reset-credit record.
        reset_note = _format_reset_credits(resets)
        balance_note = _format_account_balance(row.get("account_balance"))
        print(f"  {row.get('name'):<{provider_name_width}} [{row.get('status'):<12}] {plan:<22} "
              f"headroom {headroom_s}{reset_note}{balance_note}")

    print()
    # --- per-project worker token burn: this is project-local data, the only
    # piece the shared service doesn't already provide. Keep it. The runs
    # lookup is read into memory inside a try/finally so the connection
    # closes even when the project has zero runs (the empty-agg path).
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
        usage = token_usage(lp)
        a["in"] += usage["input"]
        a["out"] += usage["output"]
        a["reason"] += usage["reasoning"]
        a["cache"] += usage["cache_read"]
        a["cost"] += usage["cost"]
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
            _maybe_root(),
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


def _registered_operator_projects() -> list[dict]:
    rows = []
    for entry in projects.list_registered():
        row = dict(entry)
        row["available"] = projects.is_orchestra_root(Path(row["root"]))
        rows.append(row)
    return rows


def _require_registered_project_ids(project_ids: list[str]) -> None:
    registered = {row["id"] for row in projects.list_registered()}
    missing = sorted(set(project_ids) - registered)
    if missing:
        raise operator_store.OperatorStoreError(
            "unregistered project ids: "
            + ", ".join(missing)
            + " (use `orchestra project list`)"
        )


def _write_new_private_file(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    created = False
    try:
        fd = os.open(path, flags, 0o600)
        created = True
        with os.fdopen(fd, "wb") as output:
            output.write(payload)
    except FileExistsError as exc:
        raise operator_store.OperatorStoreError(
            f"refusing to overwrite existing file: {path}"
        ) from exc
    except OSError as exc:
        if created:
            try:
                path.unlink()
            except OSError:
                pass
        raise operator_store.OperatorStoreError(
            f"cannot write {path}: {exc}"
        ) from exc


def cmd_operator(args):
    """Manage Operator contracts, policy, operations, and replay."""
    try:
        if args.operator_cmd == "template":
            _require_registered_project_ids(args.project)
            data = operator_contract.template(
                name=args.name,
                goal=args.goal,
                project_ids=args.project,
                gates=args.gate,
                target_branch=args.target_branch,
                integration_branch=args.integration_branch,
                non_goals=args.non_goal,
            )
            validated = operator_contract.validate_contract(
                data,
                source="generated template",
            )
            payload = (
                json.dumps(data, indent=2, ensure_ascii=False) + "\n"
            ).encode("utf-8")
            if args.output:
                output_path = Path(args.output).expanduser()
                _write_new_private_file(output_path, payload)
                print(f"wrote contract template: {output_path}")
                print(f"sha256:{validated.sha256}")
            else:
                sys.stdout.write(payload.decode("utf-8"))
            return

        if args.operator_cmd == "validate":
            validated = operator_contract.load_contract(
                Path(args.file).expanduser()
            )
            _require_registered_project_ids(
                list(operator_contract.project_ids(validated.data))
            )
            print(
                f"valid: {validated.data['name']} "
                f"sha256:{validated.sha256}"
            )
            return

        if args.operator_cmd == "draft":
            validated = operator_contract.load_contract(
                Path(args.file).expanduser()
            )
            result = operator_store.save_draft(
                validated,
                _registered_operator_projects(),
            )
            disposition = "created" if result.created else "already stored"
            print(
                f"{disposition}: {result.operator_id} "
                f"{result.name} contract v{result.version}"
            )
            print(f"  sha256:{result.sha256}")
            if result.changed_paths:
                print("  changed:")
                for path in result.changed_paths:
                    print(f"    {path}")
            print(
                "  approve only after review:\n"
                f"    orchestra operator approve {result.operator_id} "
                f"--version {result.version} --hash {result.sha256}"
            )
            return

        if args.operator_cmd == "approve":
            result = operator_store.approve(
                args.identifier,
                version=args.version,
                sha256=args.hash,
                approved_by=args.by,
            )
            disposition = "approved" if result.created else "already approved"
            print(
                f"{disposition}: {result.operator_id} "
                f"contract v{result.version} sha256:{result.sha256}"
            )
            print("  no operation is active")
            return

        if args.operator_cmd == "list":
            statuses = operator_store.list_statuses()
            if args.json:
                print(json.dumps(statuses, indent=2, sort_keys=True))
                return
            if not statuses:
                print("(no Operators — create a contract with `orchestra operator template`)")
                return
            for index, status in enumerate(statuses):
                if index:
                    print()
                print(operator_store.render_status(status))
            return

        if args.operator_cmd == "show":
            status = operator_store.get_status(args.identifier)
            if args.json:
                print(json.dumps(status, indent=2, sort_keys=True))
            else:
                print(operator_store.render_status(status))
            return

        if args.operator_cmd == "export":
            contract = operator_store.get_contract(
                args.identifier,
                version=args.version,
            )
            payload = contract.canonical_bytes
            if args.output:
                output_path = Path(args.output).expanduser()
                _write_new_private_file(output_path, payload)
                print(f"exported canonical contract: {output_path}")
                print(f"sha256:{contract.sha256}")
            else:
                sys.stdout.write(payload.decode("utf-8"))
            return

        if args.operator_cmd == "roster":
            if args.roster_cmd == "bootstrap":
                root = paths.find_root()
                policy = operator_roster.bootstrap_policy(config.load(root))
                version, created = operator_roster.save_policy(
                    policy, source=f"bootstrap:{root}"
                )
                if args.output:
                    _write_new_private_file(
                        Path(args.output).expanduser(),
                        (json.dumps(policy.data, indent=2) + "\n").encode(),
                    )
                print(
                    f"{'created' if created else 'already stored'} roster policy "
                    f"v{version} sha256:{policy.sha256}"
                )
                print(
                    "  review inferred tiers, capabilities, contraindications, and "
                    "shared quota pools before approval"
                )
                return
            if args.roster_cmd == "draft":
                policy = operator_roster.parse_policy(
                    Path(args.file).expanduser().read_text(encoding="utf-8"),
                    source=args.file,
                )
                version, created = operator_roster.save_policy(
                    policy, source=f"file:{args.file}"
                )
                print(
                    f"{'created' if created else 'already stored'} roster policy "
                    f"v{version} sha256:{policy.sha256}"
                )
                return
            if args.roster_cmd == "approve":
                created = operator_roster.approve_policy(
                    version=args.version,
                    sha256=args.hash,
                    approved_by=args.by,
                )
                print(f"{'approved' if created else 'already approved'} roster policy v{args.version}")
                return
            if args.roster_cmd == "show":
                version, policy = operator_roster.latest_policy(
                    require_approved=not args.include_draft
                )
                if args.json:
                    print(json.dumps(
                        {"version": version, "sha256": policy.sha256, **policy.data},
                        indent=2,
                    ))
                else:
                    print(f"roster policy v{version} sha256:{policy.sha256}")
                    for profile in policy.data["profiles"]:
                        print(
                            f"  {profile['name']:<14} {profile['tier']:<10} "
                            f"{profile['backend']}/{profile['model'] or 'default'} "
                            f"{'enabled' if profile['enabled'] else 'disabled'}"
                        )
                return

        if args.operator_cmd == "start":
            roster_version, roster_policy = operator_roster.latest_policy()
            operation = operator_runtime.start_operation(
                args.identifier,
                mode=args.mode,
                priority=args.priority,
                registered_projects=_registered_operator_projects(),
                roster_version=roster_version,
                roster_sha256=roster_policy.sha256,
            )
            print(
                f"queued {operation['id']} in {operation['mode']} mode "
                f"with {len(operation['goals'])} goal(s), roster "
                f"v{operation['roster_version']}"
            )
            if not args.no_background:
                pid = _spawn_operator_controller(operation["id"])
                operator_runtime.set_controller_pid(operation["id"], pid)
                print(f"  controller pid {pid}")
            return

        if args.operator_cmd == "tick":
            result = operator_controller.tick(args.identifier)
            print(json.dumps(result, indent=2, sort_keys=True))
            return

        if args.operator_cmd == "run":
            operator_controller.run(args.identifier)
            return

        if args.operator_cmd in {"pause", "resume", "stop"}:
            target_state = {
                "pause": "paused",
                "resume": "queued",
                "stop": "stopped",
            }[args.operator_cmd]
            current = operator_runtime.get_operation(args.identifier)
            updated = operator_runtime.set_operation_state(
                current["id"], target_state, reason=args.reason
            )
            if args.operator_cmd in {"pause", "stop"} and current["controller_pid"]:
                _stop_operator_controller(int(current["controller_pid"]))
            if args.operator_cmd == "resume" and not args.no_background:
                pid = _spawn_operator_controller(updated["id"])
                operator_runtime.set_controller_pid(updated["id"], pid)
            print(f"{updated['id']}: {target_state}")
            return

        if args.operator_cmd == "operations":
            rows = operator_runtime.list_operations()
            if args.json:
                print(json.dumps(rows, indent=2, sort_keys=True))
            elif not rows:
                print("(no operations)")
            else:
                for row in rows:
                    print(
                        f"{row['id']}  {row['mode']:<6} {row['state']:<14} "
                        f"goals={sum(g['state'] == 'accepted' for g in row['goals'])}/"
                        f"{len(row['goals'])} decisions={row['open_decisions']}"
                    )
            return

        if args.operator_cmd == "status":
            operation = operator_runtime.get_operation(args.identifier)
            payload = {
                **operation,
                "work": operator_runtime.work_items(operation["id"]),
                "decisions": operator_runtime.decisions(operation["id"], state="open"),
                "actions": operator_runtime.pending_actions(operation["id"]),
                "resources": operator_runtime.resource_leases(operation["id"]),
            }
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(
                    f"{operation['id']}  {operation['mode']} / {operation['state']}  "
                    f"controller={operation['controller_pid'] or 'stopped'}"
                )
                print(f"  reason: {operation['state_reason'] or 'none'}")
                print(
                    "  work: "
                    + (", ".join(
                        f"{key}={value}" for key, value in sorted(
                            operation["work_counts"].items()
                        )
                    ) or "none")
                )
                print(f"  open decisions: {operation['open_decisions']}")
                for goal in operation["goals"]:
                    print(f"  {goal['id']}: {goal['state']} — {goal['outcome']}")
            return

        if args.operator_cmd == "decisions":
            rows = operator_runtime.decisions(
                args.identifier, state=None if args.all else "open"
            )
            print(json.dumps(rows, indent=2, sort_keys=True))
            return

        if args.operator_cmd == "answer":
            operator_runtime.answer_decision(
                args.decision_id, answer=args.answer, answered_by=args.by
            )
            print(f"{args.decision_id}: answered")
            return

        if args.operator_cmd == "replay":
            if args.replay_cmd == "import-archive":
                row = operator_replay.import_archive(
                    Path(args.archive), member=args.member, label=args.label
                )
                print(json.dumps(row, indent=2, sort_keys=True))
                return
            if args.replay_cmd == "import-live":
                source = Path(args.database).expanduser()
                row = (
                    operator_replay.import_project(source)
                    if source.is_dir()
                    else operator_replay.import_live_database(source, label=args.label)
                )
                print(json.dumps(row, indent=2, sort_keys=True))
                return
            if args.replay_cmd == "list":
                print(json.dumps(operator_replay.list_sources(), indent=2, sort_keys=True))
                return
            if args.replay_cmd == "show":
                print(json.dumps(
                    operator_replay.replay_state(args.source_id, at=args.at),
                    indent=2,
                    sort_keys=True,
                ))
                return

        raise operator_store.OperatorStoreError(
            f"unsupported operator command {args.operator_cmd!r}"
        )
    except (
        operator_contract.ContractError,
        operator_store.OperatorStoreError,
        operator_runtime.RuntimeError,
        operator_roster.RosterError,
        operator_replay.ReplayError,
        operator_controller.ControllerError,
    ) as exc:
        raise SystemExit(f"orchestra: {exc}") from exc


def _spawn_operator_controller(operation_id: str) -> int:
    command = [sys.executable, "-m", "orchestra_cli", "_operator_control", operation_id]
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return int(process.pid)


def _stop_operator_controller(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)


def cmd_operator_control(args):
    operator_controller.run(args.operation_id)


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
    s.add_argument("--refresh-playbook", action="store_true",
                   help="update only Orchestra's managed ORCHESTRA.md section")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("roster", help="list configured worker agents")
    s.set_defaults(fn=cmd_roster)

    s = sub.add_parser("doctor", help="check tools, models, and config health")
    s.add_argument("--refresh", action="store_true",
                   help="refresh OpenCode's model catalog before checking")
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("discover", help="discover runnable backends, providers, models, and profiles")
    s.add_argument("query", nargs="?", help="case-insensitive model search text")
    s.add_argument("--refresh", action="store_true",
                   help="refresh OpenCode's model catalog before checking")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_discover)

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

    s = sub.add_parser(
        "send",
        help="message an agent; a sole active run receives it at a safe boundary",
    )
    s.add_argument("to")
    send_source = s.add_mutually_exclusive_group(required=True)
    send_source.add_argument("body", nargs="?", help="message text")
    send_source.add_argument("--file", dest="body_file", metavar="PATH",
                             help="read the complete message from a UTF-8 file")
    s.add_argument("--work", help="related work item (W-XXXX)")
    s.add_argument(
        "--run",
        type=int,
        help="target active run id, or related run id for ordinary inbox mail",
    )
    ident(s)
    s.set_defaults(fn=cmd_send)

    s = sub.add_parser(
        "report",
        help="send a run-bound update to this worker's requester",
    )
    s.add_argument("message", nargs="+")
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser(
        "consult",
        help="ask this worker's requester; optionally pause with a bounded fallback",
    )
    s.add_argument("question", nargs="+")
    s.add_argument("--wait", type=int, metavar="SECONDS",
                   help="pause this run for a bounded answer window")
    s.add_argument("--fallback",
                   help="required assumption to apply automatically when --wait expires")
    s.set_defaults(fn=cmd_consult)

    s = sub.add_parser(
        "handoff",
        help="send this supervised run's final handoff to its requester",
    )
    s.add_argument("message", nargs="+")
    handoff_verification = s.add_mutually_exclusive_group()
    handoff_verification.add_argument(
        "--verified",
        action="store_const",
        dest="verification_status",
        const="verified",
        help="record that the required acceptance evidence passed",
    )
    handoff_verification.add_argument(
        "--unverified",
        action="store_const",
        dest="verification_status",
        const="unverified",
        help="record that the work was not verified",
    )
    handoff_verification.add_argument(
        "--blocked-environment",
        metavar="CAPABILITY",
        help="record an unavailable environment capability and raise an incident",
    )
    s.set_defaults(fn=cmd_handoff)

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
    s.add_argument("--after", action="append", type=int, metavar="RUN_ID",
                   help="launch only after this run succeeds (repeatable)")
    s.add_argument("--wait-for", action="append", type=int, metavar="RUN_ID",
                   help="launch after this run settles, regardless of outcome (repeatable)")
    s.add_argument("--read-only", action="store_true",
                   help="declare no tree writes; permits safe shared dependency fan-out")
    s.add_argument(
        "--requires-capability",
        action="append",
        dest="requires_capability",
        metavar="NAME",
        help="require fresh positive evidence for this environment capability (repeatable)",
    )
    s.add_argument(
        "--require-verification",
        action="store_true",
        help="a successful process exit is not accepted until a handoff records verification",
    )
    s.add_argument("--sync", action="store_true", help="block until the run finishes")
    s.add_argument("--allow-question", action="store_true",
                   help="allow one genuine blocking question with an automatic fallback")
    s.add_argument("--question-wait", type=int, metavar="SECONDS",
                   help="answer window for --allow-question (default: config or 1800)")
    s.add_argument("--no-quota-warn", action="store_true",
                   help="suppress the warn-only provider-headroom check (default: on)")
    ident(s)
    s.set_defaults(fn=cmd_dispatch)

    s = sub.add_parser("spawn", help="spawn bounded child runs from the active worker")
    s.add_argument("mission", nargs="*")
    s.add_argument("--to", action="append", required=True,
                   help="roster agent (repeatable for a child batch)")
    s.add_argument("--title")
    s.add_argument("--context", help="extra context appended to each child brief")
    s.add_argument("--brief-file", help="read child mission text from a file")
    s.add_argument("--shared-workdir", action="store_true",
                   help="opt out of the default isolated child worktree")
    s.set_defaults(fn=cmd_spawn)

    s = sub.add_parser("resume", help="continue a finished run's existing agent session")
    s.add_argument("run_id", type=int)
    s.add_argument("message", nargs="+")
    s.add_argument("--sync", action="store_true")
    ident(s)
    s.set_defaults(fn=cmd_reply)

    s = sub.add_parser("reply", help="compatibility alias for `orchestra resume`")
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

    s = sub.add_parser(
        "wait",
        help="block until runs finish or a worker requests advisory guidance",
    )
    s.add_argument("run_ids", nargs="*", type=int)
    s.add_argument("--any", action="store_true", help="return after the first completion")
    s.add_argument("--timeout", type=int, default=0)
    ident(s)
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

    s = sub.add_parser("ui", help="shared dashboard for registered projects")
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

    s = sub.add_parser(
        "operator",
        help="design and approve durable autonomous-operation contracts",
    )
    ops = s.add_subparsers(dest="operator_cmd", required=True)
    op_template = ops.add_parser(
        "template",
        help="write a complete conservative contract for owner refinement",
    )
    op_template.add_argument("name", help="human-readable Operator name")
    op_template.add_argument("--goal", required=True, help="first observable goal")
    op_template.add_argument(
        "--project",
        action="append",
        required=True,
        help="registered project id (repeatable)",
    )
    op_template.add_argument(
        "--gate",
        action="append",
        required=True,
        help="required acceptance gate (repeatable)",
    )
    op_template.add_argument(
        "--non-goal",
        action="append",
        default=[],
        help="explicit exclusion (repeatable)",
    )
    op_template.add_argument("--target-branch", default="main")
    op_template.add_argument("--integration-branch", default="main")
    op_template.add_argument(
        "--output",
        help="write a new owner-private file instead of stdout (never overwrites)",
    )

    op_validate = ops.add_parser(
        "validate",
        help="validate a contract and print its canonical approval hash",
    )
    op_validate.add_argument("file")

    op_draft = ops.add_parser(
        "draft",
        help="store an immutable contract draft in the user control plane",
    )
    op_draft.add_argument("file")

    op_approve = ops.add_parser(
        "approve",
        help="approve the latest contract version by exact SHA-256",
    )
    op_approve.add_argument("identifier", help="Operator id or name")
    op_approve.add_argument("--version", type=int, required=True)
    op_approve.add_argument("--hash", required=True)
    op_approve.add_argument("--by", default="owner", help="approval audit label")

    op_list = ops.add_parser("list", help="list deterministic Operator status")
    op_list.add_argument("--json", action="store_true")

    op_show = ops.add_parser("show", help="show one deterministic Operator status")
    op_show.add_argument("identifier", help="Operator id or name")
    op_show.add_argument("--json", action="store_true")

    op_export = ops.add_parser(
        "export",
        help="reconstruct canonical contract bytes from durable state",
    )
    op_export.add_argument("identifier", help="Operator id or name")
    op_export.add_argument("--version", type=int)
    op_export.add_argument(
        "--output",
        help="write a new owner-private file instead of stdout (never overwrites)",
    )

    op_roster = ops.add_parser(
        "roster", help="manage the versioned owner-approved model roster and quota pools"
    )
    op_rosters = op_roster.add_subparsers(dest="roster_cmd", required=True)
    roster_bootstrap = op_rosters.add_parser(
        "bootstrap", help="infer a reviewable policy draft from current launch profiles"
    )
    roster_bootstrap.add_argument("--output")
    roster_draft = op_rosters.add_parser("draft", help="store a reviewed roster policy")
    roster_draft.add_argument("file")
    roster_approve = op_rosters.add_parser("approve", help="approve the latest policy by hash")
    roster_approve.add_argument("--version", type=int, required=True)
    roster_approve.add_argument("--hash", required=True)
    roster_approve.add_argument("--by", default="owner")
    roster_show = op_rosters.add_parser("show")
    roster_show.add_argument("--include-draft", action="store_true")
    roster_show.add_argument("--json", action="store_true")

    op_start = ops.add_parser(
        "start", help="activate an approved contract and start its durable controller"
    )
    op_start.add_argument("identifier", help="Operator id or name")
    op_start.add_argument("--mode", choices=["shadow", "live"], default="shadow")
    op_start.add_argument("--priority", type=int, default=50)
    op_start.add_argument(
        "--no-background", action="store_true", help="create state without starting a controller"
    )

    op_tick = ops.add_parser("tick", help="run one lease-held reconciliation attempt")
    op_tick.add_argument("identifier", help="operation, Operator id, or Operator name")
    op_run = ops.add_parser("run", help="run the controller in the foreground until terminal")
    op_run.add_argument("identifier", help="operation, Operator id, or Operator name")
    for command in ("pause", "resume", "stop"):
        control = ops.add_parser(command, help=f"{command} an operation")
        control.add_argument("identifier")
        control.add_argument("--reason", default=f"owner requested {command}")
        if command == "resume":
            control.add_argument("--no-background", action="store_true")

    op_operations = ops.add_parser("operations", help="list current and historical operations")
    op_operations.add_argument("--json", action="store_true")
    op_status = ops.add_parser("status", help="show goals, work, decisions, and controller state")
    op_status.add_argument("identifier")
    op_status.add_argument("--json", action="store_true")
    op_decisions = ops.add_parser("decisions", help="list owner decisions")
    op_decisions.add_argument("identifier")
    op_decisions.add_argument("--all", action="store_true")
    op_answer = ops.add_parser("answer", help="answer an escalated Operator decision")
    op_answer.add_argument("decision_id")
    op_answer.add_argument("answer")
    op_answer.add_argument("--by", default="owner")

    op_replay = ops.add_parser("replay", help="import and inspect historical run evidence")
    op_replays = op_replay.add_subparsers(dest="replay_cmd", required=True)
    replay_archive = op_replays.add_parser(
        "import-archive", help="import only the Orchestra database from a ZIP"
    )
    replay_archive.add_argument("archive")
    replay_archive.add_argument("--member", default=".orchestra/orchestra.db")
    replay_archive.add_argument("--label")
    replay_live = op_replays.add_parser(
        "import-live", help="read a live project database without mutating it"
    )
    replay_live.add_argument("database", help="project root or orchestra.db")
    replay_live.add_argument("--label")
    op_replays.add_parser("list")
    replay_show = op_replays.add_parser("show")
    replay_show.add_argument("source_id")
    replay_show.add_argument("--at", help="UTC replay clock bound")
    s.set_defaults(fn=cmd_operator)

    s = sub.add_parser("queue", help="queue a follow-up for a running worker; auto-delivered "
                                     "(session resume) when its current run completes")
    s.add_argument("run_id", type=int)
    s.add_argument("message", nargs="+")
    ident(s)
    s.set_defaults(fn=cmd_queue)

    s = sub.add_parser("recall", help="recall an undelivered queued follow-up")
    s.add_argument("message_id", type=int)
    ident(s)
    s.set_defaults(fn=cmd_recall)

    s = sub.add_parser("ask", help="use an opted-in run's one blocking question and pause it")
    s.add_argument("question", nargs="+")
    s.add_argument("--default", required=True,
                   help="recommended fallback used automatically when unanswered")
    s.add_argument("--run", dest="run_id", type=int,
                   help="active run id (default: $ORCHESTRA_RUN_ID)")
    ident(s)
    s.set_defaults(fn=cmd_ask)

    s = sub.add_parser("answer", help="answer a paused worker and resume its saved session")
    s.add_argument("run_id", type=int)
    s.add_argument("answer", nargs="+")
    ident(s)
    s.set_defaults(fn=cmd_answer)

    s = sub.add_parser("interrupt", help="guaranteed delivery to a RUNNING worker: "
                                         "deliver at the next safe action boundary")
    s.add_argument("run_id", type=int)
    interrupt_source = s.add_mutually_exclusive_group(required=True)
    interrupt_source.add_argument("message", nargs="*", help="message text")
    interrupt_source.add_argument("--file", dest="message_file", metavar="PATH",
                                  help="read the complete message from a UTF-8 file")
    s.add_argument("--now", action="store_true",
                   help="stop immediately instead of waiting for a safe boundary")
    ident(s)
    s.set_defaults(fn=cmd_interrupt)

    s = sub.add_parser("cancel", help="cancel a pending or active run")
    s.add_argument("run_id", type=int)
    s.add_argument("--dry-run", action="store_true",
                   help="show child and dependency impact without changing state")
    s.set_defaults(fn=cmd_kill)

    s = sub.add_parser("kill", help="terminate a running worker")
    s.add_argument("run_id", type=int)
    s.add_argument("--dry-run", action="store_true",
                   help="show child and dependency impact without changing state")
    s.set_defaults(fn=cmd_kill)

    s = sub.add_parser("note", help="log a finding to the shared feed")
    s.add_argument("body")
    s.add_argument("--tags")
    s.add_argument("--work")
    s.add_argument("--run", type=int)
    ident(s)
    s.set_defaults(fn=cmd_note)

    s = sub.add_parser(
        "capability",
        help="record and inspect evidence about a worker launch environment",
    )
    capability_sub = s.add_subparsers(dest="capability_action", required=True)
    capability_record = capability_sub.add_parser("record", help="record one probe result")
    capability_record.add_argument("capability")
    capability_record.add_argument("--profile", required=True)
    capability_record.add_argument(
        "--state", required=True, choices=sorted(capabilities.CAPABILITY_STATES)
    )
    capability_record.add_argument("--evidence", required=True)
    capability_record.add_argument("--probe")
    capability_record.add_argument("--host")
    expiry = capability_record.add_mutually_exclusive_group()
    expiry.add_argument("--ttl-seconds", type=int, default=7 * 24 * 60 * 60)
    expiry.add_argument("--permanent", action="store_true")
    capability_record.set_defaults(fn=cmd_capability_record)

    capability_check = capability_sub.add_parser(
        "check", help="check fresh evidence for one profile"
    )
    capability_check.add_argument("capabilities", nargs="+")
    capability_check.add_argument("--profile", required=True)
    capability_check.add_argument("--host")
    capability_check.set_defaults(fn=cmd_capability_check)

    capability_list = capability_sub.add_parser("list", help="list recorded observations")
    capability_list.add_argument("--host")
    capability_list.add_argument("--backend")
    capability_list.add_argument("--profile")
    capability_list.add_argument("--sandbox")
    capability_list.add_argument("--capability")
    capability_list.add_argument("--fresh", action="store_true")
    capability_list.add_argument("--json", action="store_true")
    capability_list.set_defaults(fn=cmd_capability_list)

    s = sub.add_parser("incident", help="manage recurring systemic issues")
    incident_sub = s.add_subparsers(dest="incident_action", required=True)
    incident_record = incident_sub.add_parser("record", help="record an occurrence")
    incident_record.add_argument("--fingerprint", required=True)
    incident_record.add_argument("--scope", required=True)
    incident_record.add_argument("--title", required=True)
    incident_record.add_argument("--evidence", required=True)
    incident_record.add_argument("--run", type=int)
    incident_record.add_argument("--work")
    incident_record.add_argument("--lost-seconds", type=int, default=0)
    incident_record.add_argument("--remediation")
    incident_record.set_defaults(fn=cmd_incident_record)

    incident_list = incident_sub.add_parser("list", help="list systemic incidents")
    incident_list.add_argument("--state", choices=sorted(incidents.INCIDENT_STATES))
    incident_list.add_argument("--scope")
    incident_list.add_argument("--fingerprint")
    incident_list.add_argument("--json", action="store_true")
    incident_list.set_defaults(fn=cmd_incident_list)

    incident_state = incident_sub.add_parser("state", help="change incident state")
    incident_state.add_argument("incident_id", type=int)
    incident_state.add_argument("state", choices=sorted(incidents.INCIDENT_STATES))
    incident_state.add_argument("--remediation")
    incident_state.add_argument("--resolution-evidence")
    incident_state.set_defaults(fn=cmd_incident_state)

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

    s = sub.add_parser("_operator_control")
    s.add_argument("operation_id")
    s.set_defaults(fn=cmd_operator_control)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
