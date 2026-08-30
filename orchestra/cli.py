"""Orchestra v2 CLI. Normal operations are HTTP clients, never DB writers."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from orchestra import (
    auth, client, config, db, fleet_config, maintenance, migration, paths,
    service,
)


def _request_id(value: str | None, prefix: str) -> str:
    return value or f"{prefix}:{uuid.uuid4()}"


def _data(value):
    return value.get("data", value) if isinstance(value, dict) else value


def _print(value, *, raw: bool = False):
    if raw or not isinstance(value, (dict, list)):
        print(json.dumps(value, indent=2, ensure_ascii=False) if isinstance(
            value, (dict, list)) else value)
        return
    print(json.dumps(value, indent=2, ensure_ascii=False))


def _client(args) -> client.Client:
    return client.Client(getattr(args, "url", None), getattr(args, "token", None))


def _run_id(args) -> int:
    value = getattr(args, "run_id", None) or os.environ.get("ORCHESTRA_RUN_ID")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise SystemExit("orchestra: --run-id is required outside a run") from exc


def cmd_init(args):
    if args.archive_legacy:
        report = migration.archive_legacy(execute=True)
        report["legacy_hooks"] = migration.retire_legacy_hooks(execute=True)
        print(f"archived legacy state at {report['archive']}")
        remaining = [item for item in report["legacy_hooks"]
                     if item.get("error") or
                     item.get("found") != item.get("removed") or
                     item.get("trust_found", 0) !=
                     item.get("trust_removed", 0)]
        if remaining:
            print("warning: some legacy harness hooks could not be retired; "
                  "inspect `orchestra archive-legacy` output")
    location, created = config.ensure()
    con = db.connect()
    try:
        for adapter in ("codex", "claude", "opencode", "reasonix"):
            if fleet_config.find_runtime(con, adapter) is None:
                fleet_config.create_runtime(
                    con, adapter.title(), adapter, slug=adapter,
                    capabilities={"launch": True, "resume": True, "trace": True,
                                  "interrupt": True},
                    actor="init")
        if not con.execute("SELECT 1 FROM profiles LIMIT 1").fetchone():
            for adapter in ("codex", "claude", "opencode", "reasonix"):
                if shutil.which(adapter):
                    fleet_config.create_profile(
                        con, adapter.title(), adapter, slug=adapter, tier=2,
                        actor="init")
        token = None
        if not con.execute("SELECT 1 FROM devices LIMIT 1").fetchone():
            record, token = auth.bootstrap_device(con, args.device_name)
            client.save_token(config.api_url(), token)
            print(f"paired operator device {record['name']} ({record['device_id']})")
        print(f"Orchestra v2 initialized at {paths.state_dir()}")
        print(f"  instance:  {db.instance_id(con)}")
        print(f"  bootstrap: {location}{' (created)' if created else ''}")
        print(f"  API:       {config.api_url()}")
        if token:
            print(f"  token:     {token}")
            print("  stored in Keychain (or the owner-only local fallback); shown once")
        print("Create or import profiles, then dispatch a run or configure a group CWD.")
    finally:
        con.close()


def cmd_daemon(args):
    from orchestra import daemon
    run = getattr(daemon, "run", None) or getattr(daemon, "main", None)
    if run is None:
        raise SystemExit("orchestra: daemon entry point is unavailable")
    return run(interval=args.interval)


def cmd_supervise(args):
    """Internal detached-process entry point used by the daemon."""
    from orchestra import supervise
    return supervise.supervise(Path(args.root), args.run_id)


def cmd_observe(args):
    """Internal detached-process entry point used by the Observer scheduler."""
    from orchestra import daemon
    return daemon.observe(args.check_id)


def cmd_archive(args):
    report = migration.archive_legacy(execute=args.apply)
    report["legacy_hooks"] = migration.retire_legacy_hooks(execute=args.apply)
    _print(report)


def cmd_repair(args):
    con = db.connect()
    try:
        maintenance.require_offline(con)
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        foreign = [tuple(row) for row in con.execute("PRAGMA foreign_key_check")]
        pending = [dict(row) for row in con.execute(
            "SELECT request_id,method,path,created_at FROM request_replays "
            "WHERE response_json IS NULL ORDER BY created_at")]
        report = {"integrity": integrity, "foreign_key_violations": foreign,
                  "unfinished_requests": pending}
        _print(report)
        return 0 if integrity == "ok" and not foreign else 1
    finally:
        con.close()


def cmd_import_v1(args):
    plan = migration.operator_import_plan(
        Path(args.config).expanduser() if args.config else None,
        Path(args.database).expanduser() if args.database else None)
    if not args.apply:
        _print({**plan, "dry_run": True})
        return
    con = db.connect()
    try:
        result = migration.apply_operator_import(con, plan)
    finally:
        con.close()
    _print({"dry_run": False, "applied": result, "dropped": plan["dropped"],
            "never_imported": plan["never_imported"]})


def cmd_backup(args):
    destination = Path(args.destination).expanduser() if args.destination else None
    _print(maintenance.backup(destination))


def cmd_restore(args):
    _print(maintenance.restore(Path(args.backup), apply=args.apply))


def cmd_pair(args):
    parsed = urlsplit(args.pairing)
    endpoint = None
    if parsed.scheme == "orchestra":
        values = parse_qs(parsed.query)
        pairing_id = (values.get("pairing_id") or [""])[0]
        code = (values.get("code") or [""])[0]
        endpoint = (values.get("endpoint") or [None])[0]
    else:
        pairing_id, separator, code = args.pairing.partition(":")
        if not separator:
            pairing_id, code = "", pairing_id
    if not code:
        raise SystemExit("orchestra: pairing must be a code, Orchestra URI, or ID:CODE")
    api_client = client.Client(args.url or endpoint, token="")
    result = api_client.post("/api/v2/pairing/redeem", {
        "request_id": _request_id(getattr(args, "request_id", None), "pair"),
        "pairing_id": pairing_id, "code": code, "label": args.name})
    token = _data(result)["token"]
    client.save_token(api_client.url, token)
    print(f"paired {_data(result)['device']['label']} with {api_client.url}")


def cmd_run(args):
    context = " ".join(args.context).strip()
    if args.file:
        context = Path(args.file).read_text(encoding="utf-8")
    body = {
        "request_id": _request_id(args.request_id, "run"),
        "profile": args.profile, "context": context,
        "group": args.group, "title": args.title, "cwd": args.cwd,
        "ref": args.ref,
        "requested_by": args.requested_by, "observer": args.observer,
        "after": [{"run_id": value, "condition": args.after_condition}
                  for value in args.after],
    }
    result = _data(_client(args).post("/api/v2/runs", body))
    if args.json:
        return _print(result)
    run = result["run"]
    print(f"{run['display']} · global {run['id']} · {run['status']} · "
          f"{run['profile_name']}")
    if run.get("hold"):
        print(f"  held: {run['hold']['detail']}")


def cmd_runs(args):
    result = _data(_client(args).get(
        "/api/v2/runs", group=args.group,
        profile=args.profile, status=args.status, q=args.query,
        limit=args.limit, cursor=args.cursor))
    if args.json:
        return _print(result)
    for run in result["items"]:
        hold = f" · {run['hold']['detail']}" if run.get("hold") else ""
        print(f"{run['display']:<24} {run['status']:<10} "
              f"{run['profile_name']:<18} {(run.get('title') or '')[:70]}{hold}")
    if result.get("next_cursor"):
        print(f"next cursor: {result['next_cursor']}")


def cmd_show(args):
    _print(_data(_client(args).get(f"/api/v2/runs/{args.run_id}")))


def cmd_status(args):
    value = _data(_client(args).get("/api/v2/snapshot"))
    if args.json:
        return _print(value)
    scheduler = value["scheduler"]
    inbox = value["inbox"]
    print(f"{value['instance']['name']} · {value['daemon']['status']} · "
          f"{scheduler['active']} active · {scheduler['queued']} queued · "
          f"{inbox['open']} inbox")
    if scheduler.get("paused"):
        print("  starts paused")


def cmd_run_view(args):
    endpoint = {
        "thread": "thread", "events": "events", "lineage": "lineage",
        "artifacts": "artifacts", "changes": "changes",
        "observations": "observer",
    }[args.command]
    query = {}
    if getattr(args, "cursor", None):
        query["cursor"] = args.cursor
    if getattr(args, "limit", None):
        query["limit"] = args.limit
    _print(_data(_client(args).get(
        f"/api/v2/runs/{args.run_id}/{endpoint}", **query)))


def cmd_statistics(args):
    _print(_data(_client(args).get(
        "/api/v2/statistics", group=args.group,
        profile=args.profile, status=args.status)))


def cmd_control(args):
    action = args.command
    body = {"request_id": _request_id(args.request_id,
                                      f"{action}:{args.run_id}")}
    if hasattr(args, "text"):
        body["text"] = " ".join(args.text).strip()
    if action == "continue":
        body["context"] = body.pop("text")
    for key in ("context", "profile"):
        value = getattr(args, key, None)
        if value is not None:
            body[key] = value
    result = _data(_client(args).post(
        f"/api/v2/runs/{args.run_id}/{action}", body))
    _print(result) if args.json else print(json.dumps(result, ensure_ascii=False))


def cmd_children(args):
    run_id = _run_id(args)
    body = {"request_id": _request_id(args.request_id, f"child:{run_id}"),
            "profile": args.profile, "context": " ".join(args.context),
            "title": args.title}
    _print(_data(_client(args).post(
        f"/api/v2/runs/{run_id}/children", body)))


def cmd_ask(args):
    run_id = _run_id(args)
    body = {"request_id": _request_id(args.request_id, f"attention:{run_id}"),
            "kind": args.kind, "title": args.title,
            "body": " ".join(args.question), "blocking": not args.nonblocking,
            "choices": args.choice, "deadline": args.deadline}
    _print(_data(_client(args).post(
        f"/api/v2/runs/{run_id}/attention", body)))


def cmd_artifact(args):
    run_id = _run_id(args)
    body = {"request_id": _request_id(args.request_id,
                                      f"artifact:{run_id}"),
            "path": args.path, "name": args.name}
    _print(_data(_client(args).post(
        f"/api/v2/runs/{run_id}/artifacts", body)))


def cmd_resource(args):
    noun = args.resource
    action = args.action or args.default_action
    api_client = _client(args)
    if action == "list":
        result = _data(api_client.get(f"/api/v2/{noun}",
                                      include_archived=args.include_archived))
        if args.json:
            return _print(result)
        for item in result["items"]:
            suffix = " [archived]" if item.get("archived") else ""
            print(f"{item.get('slug',''):<22} {item.get('name','')}{suffix}")
        return
    body = {"request_id": _request_id(args.request_id,
                                      f"{noun}:{action}")}
    if action == "create":
        body.update(_pairs(args.value, plain_name=noun == "groups"))
        response = api_client.post(f"/api/v2/{noun}", body)
    else:
        body.update(_pairs(args.value))
        response = api_client.patch(f"/api/v2/{noun}/{args.selector}", body)
    _print(_data(response))


def cmd_profile_discovery(args):
    """Ask the Orchestra host for its real model/effort catalogs."""
    _print(_data(_client(args).get(
        "/api/v2/profile-discovery", local=args.local)))


def _pairs(values, *, plain_name: bool = False) -> dict:
    if plain_name and values and all("=" not in value for value in values):
        return {"name": " ".join(values)}
    out = {}
    for value in values or []:
        key, separator, raw = value.partition("=")
        if not separator:
            raise SystemExit(f"orchestra: expected KEY=JSON, got {value!r}")
        try:
            out[key] = json.loads(raw)
        except ValueError:
            out[key] = raw
    return out


def cmd_inbox(args):
    action = args.action or args.default_action
    api_client = _client(args)
    if action == "list":
        result = _data(api_client.get(
            "/api/v2/inbox", state=args.state, kind=args.kind, limit=args.limit,
            cursor=args.cursor))
        if args.json:
            return _print(result)
        for item in result["items"]:
            print(f"{item['id']:>5} {item['kind']:<18} {item['title']}"
                  + (f" · {item['display']}" if item.get("display") else ""))
        if result.get("next_cursor"):
            print(f"next cursor: {result['next_cursor']}")
        return
    body = {"request_id": _request_id(args.request_id,
                                      f"answer:{args.attention_id}"),
            "answer": " ".join(args.answer), "choice": args.choice}
    _print(_data(api_client.post(
        f"/api/v2/attention/{args.attention_id}/{action}", body)))


def cmd_outbox(args):
    result = _data(_client(args).get(
        "/api/v2/outbox", direction=args.direction, status=args.status,
        kind=args.kind, run_id=args.run_id, limit=args.limit,
        cursor=args.cursor))
    if args.json:
        return _print(result)
    for item in result["items"]:
        display = item.get("display") or f"run {item['run_id']}"
        body = " ".join((item.get("body") or "").split())
        print(f"{item['id']:>6} {display:<24} {item['direction']:<8} "
              f"{item['status']:<13} {item['kind']:<18} {body[:90]}")
    if result.get("next_cursor"):
        print(f"next cursor: {result['next_cursor']}")


def cmd_pause(args):
    body = {"request_id": _request_id(args.request_id, f"scheduler:{args.command}"),
            "note": " ".join(args.note) if hasattr(args, "note") else None}
    _print(_data(_client(args).post(f"/api/v2/scheduler/{args.command}", body)))


def cmd_runway(args):
    action = args.action or args.default_action
    api_client = _client(args)
    if action == "list":
        return _print(_data(api_client.get("/api/v2/runway-sources")))
    body = {"request_id": _request_id(args.request_id,
                                      f"runway:{args.source}:refresh")}
    _print(_data(api_client.post(
        f"/api/v2/runway-sources/{args.source}/refresh", body)))


def cmd_devices(args):
    action = args.action or args.default_action
    api_client = _client(args)
    if action == "list":
        return _print(_data(api_client.get("/api/v2/devices")))
    body = {"request_id": _request_id(args.request_id, f"device:{action}")}
    if action == "pairing":
        body["label"] = args.label
        return _print(_data(api_client.post("/api/v2/devices/pairing", body)))
    body["revoked"] = True
    _print(_data(api_client.patch(f"/api/v2/devices/{args.device_id}", body)))


def cmd_service_tokens(args):
    action = args.action or args.default_action
    api_client = _client(args)
    if action == "list":
        return _print(_data(api_client.get("/api/v2/service-tokens")))
    body = {"request_id": _request_id(args.request_id, f"service:{action}")}
    if action == "create":
        body.update({"name": args.name, "authorities": args.authority})
        return _print(_data(api_client.post("/api/v2/service-tokens", body)))
    body["revoked"] = True
    _print(_data(api_client.patch(
        f"/api/v2/service-tokens/{args.token_id}", body)))


def cmd_storage(args):
    action = args.action or args.default_action
    api_client = _client(args)
    if action == "report":
        return _print(_data(api_client.get("/api/v2/storage")))
    if action == "plan":
        body = {
            "request_id": _request_id(args.request_id, "storage:plan"),
            "older_than_days": args.older_than_days,
        }
        if args.kind:
            body["kinds"] = args.kind
        return _print(_data(api_client.post("/api/v2/storage/prune-plan", body)))
    body = {"request_id": _request_id(
        args.request_id, f"storage:apply:{args.plan_id}")}
    _print(_data(api_client.post(
        f"/api/v2/storage/prune-plans/{args.plan_id}/apply", body)))


def cmd_settings(args):
    action = args.action or args.default_action
    api_client = _client(args)
    if action == "list":
        return _print(_data(api_client.get("/api/v2/settings")))
    try:
        value = json.loads(args.value)
    except ValueError:
        value = args.value
    body = {"request_id": _request_id(args.request_id,
                                      f"setting:{args.key}"),
            "key": args.key, "value": value}
    if args.expected_revision is not None:
        body["expected_revision"] = args.expected_revision
    _print(_data(api_client.patch("/api/v2/settings", body)))


def cmd_observer_settings(args):
    action = args.action or args.default_action
    api_client = _client(args)
    if action == "show":
        return _print(_data(api_client.get("/api/v2/observer")))
    body = {"request_id": _request_id(args.request_id, "observer:update"),
            **_pairs(args.value)}
    _print(_data(api_client.patch("/api/v2/observer", body)))


def cmd_pin(args):
    body = {"request_id": _request_id(
        args.request_id, f"evidence:{args.command}:{args.run_id}")}
    if args.command == "pin" and args.reason:
        body["reason"] = args.reason
    _print(_data(_client(args).post(
        f"/api/v2/runs/{args.run_id}/{args.command}", body)))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orchestra",
                                     description="Run an agent fleet on this machine")
    parser.add_argument("--url")
    parser.add_argument("--token")
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init.add_argument("--device-name", default="CLI")
    init.add_argument("--archive-legacy", action="store_true")
    init.set_defaults(func=cmd_init)
    daemon = sub.add_parser("daemon")
    daemon.add_argument("--interval", type=float, default=1.0)
    daemon.set_defaults(func=cmd_daemon)
    internal = sub.add_parser("_supervise", help=argparse.SUPPRESS)
    internal.add_argument("run_id", type=int)
    internal.add_argument("--root", required=True)
    internal.set_defaults(func=cmd_supervise)
    observe = sub.add_parser("_observe", help=argparse.SUPPRESS)
    observe.add_argument("check_id", type=int)
    observe.set_defaults(func=cmd_observe)
    archive = sub.add_parser("archive-legacy")
    archive.add_argument("--apply", action="store_true")
    archive.set_defaults(func=cmd_archive)
    repair = sub.add_parser("repair")
    repair.set_defaults(func=cmd_repair)
    backup = sub.add_parser("backup")
    backup.add_argument("destination", nargs="?")
    backup.set_defaults(func=cmd_backup)
    restore = sub.add_parser("restore")
    restore.add_argument("backup")
    restore.add_argument("--apply", action="store_true")
    restore.set_defaults(func=cmd_restore)
    importer = sub.add_parser("import-v1")
    importer.add_argument("--config")
    importer.add_argument("--database")
    importer.add_argument("--apply", action="store_true")
    importer.set_defaults(func=cmd_import_v1)
    pair = sub.add_parser("pair")
    pair.add_argument("pairing")
    pair.add_argument("--name", required=True)
    pair.add_argument("--request-id")
    pair.set_defaults(func=cmd_pair)

    status = sub.add_parser("status")
    status.set_defaults(func=cmd_status)

    run = sub.add_parser("run", aliases=["dispatch"])
    run.add_argument("--profile", required=True)
    run.add_argument("--group", default="general")
    run.add_argument("--title")
    run.add_argument("--cwd")
    run.add_argument("--ref")
    run.add_argument("--requested-by", default="cli")
    run.add_argument("--observer", default="inherit")
    run.add_argument("--after", type=int, action="append", default=[])
    run.add_argument("--after-condition", choices=("success", "terminal"),
                     default="success")
    run.add_argument("--file")
    run.add_argument("--request-id")
    run.add_argument("context", nargs="*")
    run.set_defaults(func=cmd_run)
    runs_parser = sub.add_parser("runs")
    for flag in ("group", "profile", "status"):
        runs_parser.add_argument(f"--{flag}")
    runs_parser.add_argument("--query")
    runs_parser.add_argument("--limit", type=int, default=100)
    runs_parser.add_argument("--cursor")
    runs_parser.set_defaults(func=cmd_runs)
    show = sub.add_parser("show")
    show.add_argument("run_id", type=int)
    show.set_defaults(func=cmd_show)
    for name in ("thread", "events", "lineage", "artifacts", "changes",
                 "observations"):
        view = sub.add_parser(name)
        view.add_argument("run_id", type=int)
        if name in {"thread", "events"}:
            view.add_argument("--cursor")
            view.add_argument("--limit", type=int, default=200)
        view.set_defaults(func=cmd_run_view)
    statistics = sub.add_parser("statistics")
    for flag in ("group", "profile", "status"):
        statistics.add_argument(f"--{flag}")
    statistics.set_defaults(func=cmd_statistics)

    for name in ("tell", "interrupt"):
        control = sub.add_parser(name)
        control.add_argument("run_id", type=int)
        control.add_argument("text", nargs="+")
        control.add_argument("--request-id")
        control.set_defaults(func=cmd_control)
    continuation = sub.add_parser("continue")
    continuation.add_argument("run_id", type=int)
    continuation.add_argument("text", nargs="+")
    continuation.add_argument("--profile")
    continuation.add_argument("--request-id")
    continuation.set_defaults(func=cmd_control)
    for name in ("stop", "stop-tree", "check"):
        control = sub.add_parser(name)
        control.add_argument("run_id", type=int)
        control.add_argument("--request-id")
        control.set_defaults(func=cmd_control)
    retry = sub.add_parser("retry")
    retry.add_argument("run_id", type=int)
    retry.add_argument("--context")
    retry.add_argument("--profile")
    retry.add_argument("--request-id")
    retry.set_defaults(func=cmd_control)
    child = sub.add_parser("child")
    child.add_argument("--run-id", type=int)
    child.add_argument("--profile", required=True)
    child.add_argument("--title")
    child.add_argument("--request-id")
    child.add_argument("context", nargs="+")
    child.set_defaults(func=cmd_children)
    ask = sub.add_parser("ask")
    ask.add_argument("--run-id", type=int)
    ask.add_argument("--kind", choices=("question", "decision"), default="question")
    ask.add_argument("--title", default="Input needed")
    ask.add_argument("--choice", action="append")
    ask.add_argument("--deadline")
    ask.add_argument("--nonblocking", action="store_true")
    ask.add_argument("--request-id")
    ask.add_argument("question", nargs="+")
    ask.set_defaults(func=cmd_ask)
    artifact = sub.add_parser("artifact")
    artifact.add_argument("--run-id", type=int)
    artifact.add_argument("--name")
    artifact.add_argument("--request-id")
    artifact.add_argument("path")
    artifact.set_defaults(func=cmd_artifact)

    for noun in ("groups", "runtimes", "profiles", "runway-sources"):
        resource = sub.add_parser(noun)
        resource.set_defaults(resource=noun, default_action="list",
                              include_archived=False, func=cmd_resource)
        actions = resource.add_subparsers(dest="action")
        listing = actions.add_parser("list")
        listing.add_argument("--include-archived", action="store_true")
        listing.set_defaults(func=cmd_resource)
        create = actions.add_parser("create")
        create.add_argument("value", nargs="+", help="KEY=JSON")
        create.add_argument("--request-id")
        create.set_defaults(func=cmd_resource)
        update = actions.add_parser("update")
        update.add_argument("selector")
        update.add_argument("value", nargs="+", help="KEY=JSON")
        update.add_argument("--request-id")
        update.set_defaults(func=cmd_resource)
        if noun == "profiles":
            discover = actions.add_parser("discover")
            discover.add_argument(
                "--local", action="store_true",
                help="also probe standard localhost inference-server ports")
            discover.set_defaults(func=cmd_profile_discovery)

    inbox = sub.add_parser("inbox")
    inbox.set_defaults(default_action="list", state="open", kind=None, limit=100,
                       cursor=None, func=cmd_inbox)
    inbox_actions = inbox.add_subparsers(dest="action")
    inbox_list = inbox_actions.add_parser("list")
    inbox_list.add_argument("--state", default="open")
    inbox_list.add_argument("--kind")
    inbox_list.add_argument("--limit", type=int, default=100)
    inbox_list.add_argument("--cursor")
    inbox_list.set_defaults(func=cmd_inbox)
    for name in ("answer", "approve", "reject", "acknowledge"):
        answer = inbox_actions.add_parser(name)
        answer.add_argument("attention_id", type=int)
        answer.add_argument("answer", nargs="*")
        answer.add_argument("--choice")
        answer.add_argument("--request-id")
        answer.set_defaults(func=cmd_inbox)

    outbox = sub.add_parser("outbox")
    outbox.add_argument("--direction",
                        choices=("inbound", "outbound", "system"))
    outbox.add_argument("--status",
                        choices=("pending", "delivered", "undeliverable"))
    outbox.add_argument("--kind")
    outbox.add_argument("--run-id", type=int)
    outbox.add_argument("--limit", type=int, default=100)
    outbox.add_argument("--cursor")
    outbox.set_defaults(func=cmd_outbox)

    for name in ("pause", "resume"):
        command = sub.add_parser(name)
        command.add_argument("note", nargs="*")
        command.add_argument("--request-id")
        command.set_defaults(func=cmd_pause)
    runway_parser = sub.add_parser("runway")
    runway_parser.set_defaults(default_action="list", func=cmd_runway)
    runway_actions = runway_parser.add_subparsers(dest="action")
    runway_actions.add_parser("list").set_defaults(func=cmd_runway)
    refresh = runway_actions.add_parser("refresh")
    refresh.add_argument("source")
    refresh.add_argument("--request-id")
    refresh.set_defaults(func=cmd_runway)

    devices = sub.add_parser("devices")
    devices.set_defaults(default_action="list", func=cmd_devices)
    device_actions = devices.add_subparsers(dest="action")
    device_actions.add_parser("list").set_defaults(func=cmd_devices)
    pairing = device_actions.add_parser("pairing")
    pairing.add_argument("--label", default="New device")
    pairing.add_argument("--request-id")
    pairing.set_defaults(func=cmd_devices)
    revoke = device_actions.add_parser("revoke")
    revoke.add_argument("device_id")
    revoke.add_argument("--request-id")
    revoke.set_defaults(func=cmd_devices)

    tokens = sub.add_parser("service-tokens")
    tokens.set_defaults(default_action="list", func=cmd_service_tokens)
    token_actions = tokens.add_subparsers(dest="action")
    token_actions.add_parser("list").set_defaults(func=cmd_service_tokens)
    create_token = token_actions.add_parser("create")
    create_token.add_argument("name")
    create_token.add_argument("--authority", action="append", required=True)
    create_token.add_argument("--request-id")
    create_token.set_defaults(func=cmd_service_tokens)
    revoke_token = token_actions.add_parser("revoke")
    revoke_token.add_argument("token_id")
    revoke_token.add_argument("--request-id")
    revoke_token.set_defaults(func=cmd_service_tokens)

    storage_parser = sub.add_parser("storage")
    storage_parser.set_defaults(default_action="report", func=cmd_storage)
    storage_actions = storage_parser.add_subparsers(dest="action")
    storage_actions.add_parser("report").set_defaults(func=cmd_storage)
    plan = storage_actions.add_parser("plan")
    plan.add_argument("--older-than-days", type=int, default=30)
    plan.add_argument("--kind", action="append",
                      choices=("raw_logs", "artifacts"))
    plan.add_argument("--request-id")
    plan.set_defaults(func=cmd_storage)
    apply = storage_actions.add_parser("apply")
    apply.add_argument("plan_id")
    apply.add_argument("--request-id")
    apply.set_defaults(func=cmd_storage)

    settings = sub.add_parser("settings")
    settings.set_defaults(default_action="list", func=cmd_settings)
    setting_actions = settings.add_subparsers(dest="action")
    setting_actions.add_parser("list").set_defaults(func=cmd_settings)
    set_setting = setting_actions.add_parser("set")
    set_setting.add_argument("key")
    set_setting.add_argument("value")
    set_setting.add_argument("--expected-revision", type=int)
    set_setting.add_argument("--request-id")
    set_setting.set_defaults(func=cmd_settings)

    observer_parser = sub.add_parser("observer")
    observer_parser.set_defaults(default_action="show", func=cmd_observer_settings)
    observer_actions = observer_parser.add_subparsers(dest="action")
    observer_actions.add_parser("show").set_defaults(func=cmd_observer_settings)
    observer_update = observer_actions.add_parser("update")
    observer_update.add_argument("value", nargs="+", help="KEY=JSON")
    observer_update.add_argument("--request-id")
    observer_update.set_defaults(func=cmd_observer_settings)
    for name in ("pin", "unpin"):
        pin = sub.add_parser(name)
        pin.add_argument("run_id", type=int)
        if name == "pin":
            pin.add_argument("--reason")
        pin.add_argument("--request-id")
        pin.set_defaults(func=cmd_pin)

    service_parser = sub.add_parser("service")
    service_parser.add_argument("action", choices=("install", "uninstall", "status", "restart"))
    service_parser.add_argument("--start", action="store_true")
    service_parser.set_defaults(func=lambda args: getattr(service, args.action)(
        args.start) if args.action == "install" else getattr(service, args.action)())
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.func(args)
        return 0 if result is None else result
    except client.ClientError as exc:
        print(f"orchestra: {exc}", file=sys.stderr)
        return 1
    except (ValueError, RuntimeError) as exc:
        print(f"orchestra: {exc}", file=sys.stderr)
        return 1
