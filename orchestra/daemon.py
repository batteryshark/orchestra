"""The authoritative v2 scheduler, recovery loop, and HTTP host."""
from __future__ import annotations

import json
import math
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from orchestra import (attention, child_runs, config, db, fleet_config,
                       http, messaging, observer, paths, proc, runway, runtime,
                       runs, runners, scheduler, supervise)

DEFAULT_INTERVAL = 1.0
RUNWAY_INTERVAL_SECONDS = 300
OBSERVER_TIMEOUT_SECONDS = 300
OBSERVER_STOP_GRACE_SECONDS = 5
OBSERVER_OUTPUT_CHARS = 64 * 1024

_OBSERVER_ENV_KEYS = (
    "PATH", "HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA",
    "XDG_DATA_HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL",
    "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)
_OBSERVER_ARGS = {
    "claude": (
        "--safe-mode", "--disable-slash-commands", "--no-chrome",
        "--no-session-persistence", "--strict-mcp-config", "--mcp-config",
        "{}", "--permission-mode", "dontAsk", "--tools", "",
    ),
    "opencode": ("--pure",),
    "reasonix": (
        "--allowed-tools", "", "--permission-mode", "dontAsk",
        "--ablate", "all",
    ),
}
_REASONIX_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REASONIX_AUTH_NAME = re.compile(
    r"(?:^|_)(?:API_?KEY|KEY|TOKEN|SECRET|PASSWORD|CREDENTIALS?)(?:_|$)", re.I)
_REASONIX_PROVIDER_FIELDS = (
    "name", "kind", "base_url", "models", "default", "api_key_env",
    "auth_header", "headers", "context_window", "reasoning_protocol",
    "supported_efforts", "default_effort", "thinking", "vision_models",
    "model_overrides", "preset_id", "preset_version",
)
_REASONIX_OBSERVER_CONFIG = """\
[agent]
system_prompt = "Analyze only the supplied Observer prompt. Do not use tools or external context."

[environment]
enabled = false

[lsp]
enabled = false

[skills]
paths = []
disable_implicit_invocation = true

[notifications]
enabled = false
"""


def serve_http(stop: threading.Event, wake: threading.Event | None = None,
               restart: threading.Event | None = None):
    return http.serve(stop, wake=wake, restart=restart)


def _supervisor_alive(run) -> bool:
    try:
        pid = int(run["supervisor_pid"])
    except (TypeError, ValueError):
        return False
    if pid <= 0 or not proc.alive(pid) or not run["supervisor_pid_identity"]:
        return False
    identity = proc.process_identity(pid)
    return identity is None or identity == run["supervisor_pid_identity"]


def _stop_worker(run) -> tuple[bool, str]:
    if run["pid"] is None:
        return True, "no worker process"
    outcome, detail = proc.signal_owned_group(
        int(run["pid"]), run["pid_identity"], signal.SIGTERM)
    if outcome == "refused":
        return False, detail
    if outcome == "gone":
        return True, detail
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        outcome, detail = proc.signal_owned_group(
            int(run["pid"]), run["pid_identity"], 0)
        if outcome == "gone":
            return True, detail
        if outcome == "refused":
            return False, detail
        time.sleep(0.1)
    outcome, detail = proc.signal_owned_group(
        int(run["pid"]), run["pid_identity"], signal.SIGKILL)
    return outcome != "refused", detail


def _launch_root(con, run_id: int) -> Path:
    row = con.execute(
        "SELECT repo,workdir FROM runs WHERE id=?", (int(run_id),)).fetchone()
    if row is None:
        raise RuntimeError(f"run {run_id} does not exist")
    return Path(row["repo"] or row["workdir"])


def _launch(con, run_id: int, launcher) -> bool:
    try:
        launcher(_launch_root(con, run_id), int(run_id))
        return True
    except BaseException as exc:
        run = runs.find(con, int(run_id))
        if run is not None and run["status"] not in db.RUN_TERMINAL:
            supervise.finalize_run(
                con, run, "failed", None,
                summary=f"Supervisor launch failed: {exc}"[:4000])
        return False


def _recover(con, launcher) -> dict:
    relaunched, failed, refused = [], [], []
    rows = list(con.execute(
        "SELECT * FROM runs WHERE status IN ('starting','running') ORDER BY id"))
    for run in rows:
        run_id = int(run["id"])
        if _supervisor_alive(run):
            continue
        if run["status"] == "starting" and run["supervisor_pid"] is None:
            if _launch(con, run_id, launcher):
                relaunched.append(run_id)
            continue
        stopped, reason = _stop_worker(run)
        if not stopped:
            refused.append({"run_id": run_id, "reason": reason})
            attention.open_request(
                con, kind="alert", run_id=run_id,
                title=f"Run {run_id} could not be recovered",
                body=reason, created_by="orchestra:recovery",
                correlation_id=f"recovery-refused:{run_id}",
                callback_command=config.callback_command())
            continue
        supervise.finalize_run(
            con, runs.find(con, run_id), "failed", None,
            summary=(f"Supervisor {run['supervisor_pid'] or '(unrecorded)'} "
                     "vanished before the run settled."))
        failed.append(run_id)
    return {"relaunched": relaunched, "failed": failed, "refused": refused}


def _capacity(con) -> tuple[int | None, int, dict[str, int]]:
    raw = scheduler.setting(con, "max_active_runs", 8)
    try:
        limit = int(raw)
    except (TypeError, ValueError):
        limit = 8
    limit = None if limit <= 0 else limit
    rows = con.execute(
        "SELECT profile_id,COUNT(*) AS count FROM runs "
        "WHERE status IN ('starting','running') GROUP BY profile_id"
    ).fetchall()
    profiles = {row["profile_id"]: int(row["count"]) for row in rows}
    return limit, sum(profiles.values()), profiles


def _runway_hold(con, source_id: str | None) -> str | None:
    if not source_id:
        return None
    row = con.execute(
        "SELECT * FROM runway_readings WHERE source_id=? ORDER BY id DESC LIMIT 1",
        (source_id,),).fetchone()
    return runway.source_hold(dict(row) if row else None)


def _resume_waiters(con, launcher) -> list[int]:
    if bool(scheduler.setting(con, "paused", False)):
        con.execute(
            "UPDATE runs SET hold_reason='fleet paused' WHERE status='waiting' "
            "AND hold_reason IS NOT 'fleet paused'")
        con.commit()
        return []
    global_limit, active, by_profile = _capacity(con)
    resumed: list[int] = []
    waiters = list(con.execute(
        "SELECT r.*,p.max_concurrency FROM runs r "
        "JOIN profiles p ON p.profile_id=r.profile_id "
        "WHERE r.status='waiting' ORDER BY r.id"))
    for run in waiters:
        run_id = int(run["id"])
        ready = False
        if run["waiting_kind"] == "input":
            ready = con.execute(
                "SELECT 1 FROM attention_requests WHERE run_id=? AND status='open' "
                "AND blocking=1 LIMIT 1", (run_id,)).fetchone() is None
        elif run["waiting_kind"] == "children":
            ready = not child_runs.unsettled_requests(con, run_id) and \
                not child_runs.active_children(con, run_id)
        if not ready:
            continue
        reason = _runway_hold(con, run["runway_source_id"])
        if reason is None and global_limit is not None and active >= global_limit:
            reason = f"global capacity {active}/{global_limit}"
        profile_active = by_profile.get(run["profile_id"], 0)
        if reason is None and run["max_concurrency"] is not None and \
                profile_active >= int(run["max_concurrency"]):
            reason = (f"profile capacity {profile_active}/"
                      f"{run['max_concurrency']}")
        if reason:
            con.execute("UPDATE runs SET hold_reason=? WHERE id=?",
                        (reason, run_id))
            con.commit()
            continue
        if run["waiting_kind"] == "children":
            generation = child_runs.result_generation(con, run_id)
            if generation is None:
                generation = "none"
            correlation = f"children:{run_id}:{generation}"
            if con.execute(
                "SELECT 1 FROM messages WHERE run_id=? AND kind='child_results' "
                "AND correlation_id=? LIMIT 1", (run_id, correlation)
            ).fetchone() is None:
                messaging.post(
                    con, run_id, direction="inbound", sender="orchestra",
                    body=child_runs.results_prompt(con, run_id),
                    kind="child_results", correlation_id=correlation)
        changed = con.execute(
            "UPDATE runs SET status='starting',waiting_kind=NULL,hold_reason=NULL,"
            "pid=NULL,pid_identity=NULL,supervisor_pid=NULL,"
            "supervisor_pid_identity=NULL,finished_at=NULL WHERE id=? "
            "AND status='waiting'", (run_id,))
        con.commit()
        if changed.rowcount != 1:
            continue
        active += 1
        by_profile[run["profile_id"]] = profile_active + 1
        if _launch(con, run_id, launcher):
            resumed.append(run_id)
    return resumed


def _poll_runway(con) -> int:
    last = db.meta_get(con, "runway_polled_at")
    if last:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(
                last.replace("Z", "+00:00"))
            if age.total_seconds() < RUNWAY_INTERVAL_SECONDS:
                return 0
        except ValueError:
            pass
    sources = [dict(row) for row in con.execute(
        "SELECT * FROM runway_sources WHERE enabled=1 AND archived=0 ORDER BY slug")]
    if sources:
        with ThreadPoolExecutor(max_workers=min(4, len(sources))) as pool:
            readings = list(pool.map(runway.poll_source, sources))
        for reading in readings:
            runway.record_source_reading(con, reading)
    db.meta_set(con, "runway_polled_at", db.now())
    con.commit()
    return len(sources)


def _snapshot(row, json_fields: tuple[str, ...]) -> dict:
    data = {key: row[key] for key in row.keys()
            if key not in ("revision", "created_at", "updated_at")}
    for key in json_fields:
        if key not in data:
            continue
        try:
            value = json.loads(data.get(key) or "{}")
        except (TypeError, ValueError):
            value = {}
        data[key.removesuffix("_json")] = value if isinstance(
            value, (dict, list)) else {}
        data.pop(key, None)
    return data


def _observer_snapshots(profile_row, runtime_row) -> tuple[dict, dict]:
    """Freeze a runtime without granting run-scoped environment or tools."""
    profile = _snapshot(profile_row, ("env_json", "config_json"))
    runtime_row = _snapshot(
        runtime_row, ("command_json", "capabilities_json", "config_json"))
    profile["env"] = {}
    profile["sandbox"] = "read-only"
    requested_config = dict(profile.get("config") or {})
    # Variant is model selection, not capability. Every other harness-specific
    # knob is replaced by Orchestra's fixed Observer posture below.
    profile_config = {
        key: requested_config[key] for key in ("variant",)
        if key in requested_config
    }
    profile_config["add_dirs"] = []
    profile_config["acp_permission"] = "deny"
    adapter = str(runtime_row.get("adapter") or "").lower()
    # These replace hand-authored yolo/tool/plugin flags; they are not merged.
    profile_config["extra_args"] = list(_OBSERVER_ARGS.get(adapter, ()))
    profile["config"] = profile_config
    # Built-in Observer adapters consume no runtime configuration. Clearing it
    # also makes imported/frozen env state harmless at the final launch seam.
    runtime_row["config"] = {}
    return profile, runtime_row


def _observer_profile(con, run, settings):
    request = json.loads(run["request_snapshot"] or "{}")
    selected = request.get("observer", "inherit")
    if selected == "off":
        return None
    selector = settings["profile_id"] if selected == "inherit" else selected
    profile = fleet_config.find_profile(con, selector) if selector else None
    if profile is None or profile["archived"] or not profile["enabled"]:
        return None
    runtime_row = fleet_config.find_runtime(con, profile["runtime_id"])
    if runtime_row is None or runtime_row["archived"] or not runtime_row["enabled"]:
        return None
    return _observer_snapshots(profile, runtime_row)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError("Observer runtime timed out")
    return remaining


def _toml_value(value) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(
            f"{json.dumps(str(key))} = {_toml_value(item)}"
            for key, item in value.items()) + " }"
    raise RuntimeError("Reasonix provider configuration is not serializable")


def _reasonix_source_home() -> Path:
    configured = os.environ.get("REASONIX_HOME")
    if configured:
        return Path(configured).expanduser()
    if proc.IS_WIN:
        base = os.environ.get("APPDATA")
        return Path(base) / "reasonix" if base else Path.home() / "reasonix"
    return Path.home() / ".reasonix"


def _dotenv_value(path: Path, name: str) -> str | None:
    if not path.is_file():
        return None
    if os.name == "posix" and path.stat().st_mode & 0o077:
        raise RuntimeError("Reasonix credential store must be mode 0600")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError("Reasonix credential store could not be read") from exc
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, raw = line.partition("=")
        if not separator or key.strip() != name:
            continue
        raw = raw.strip()
        if raw.startswith(("'", '"')):
            try:
                parts = shlex.split(raw, comments=True, posix=True)
            except ValueError as exc:
                raise RuntimeError(
                    f"Reasonix credential {name} is malformed") from exc
            if len(parts) != 1:
                raise RuntimeError(f"Reasonix credential {name} is malformed")
            return parts[0]
        return re.split(r"\s+#", raw, maxsplit=1)[0].strip()
    return None


def _reasonix_provider(profile: dict, source_home: Path) -> tuple[dict, str]:
    location = source_home / "config.toml"
    try:
        raw = tomllib.loads(location.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RuntimeError(
            "Reasonix Observer requires a readable provider config in its "
            "harness home"
        ) from exc
    providers = raw.get("providers")
    if not isinstance(providers, list):
        raise RuntimeError("Reasonix Observer provider config has no providers")
    requested = str(profile.get("model") or raw.get("default_model") or "")
    if not requested:
        raise RuntimeError("Reasonix Observer profile has no model provider")
    provider_name, _, model_name = requested.partition("/")
    exact = [item for item in providers if isinstance(item, dict) and
             item.get("name") == provider_name]
    if exact:
        return exact[0], requested
    model = model_name or requested
    matches = [item for item in providers if isinstance(item, dict) and
               model in (item.get("models") or [])]
    if len(matches) > 1:
        default_provider = str(raw.get("default_model") or "").partition("/")[0]
        preferred = [item for item in matches
                     if item.get("name") == default_provider]
        matches = preferred or matches
    if len(matches) != 1:
        raise RuntimeError(
            f"Reasonix Observer cannot resolve provider for model {requested!r}")
    return matches[0], requested


def _reasonix_isolation(profile: dict, directory: str) -> dict[str, str]:
    source_home = _reasonix_source_home()
    provider, requested = _reasonix_provider(profile, source_home)
    safe = {key: provider[key] for key in _REASONIX_PROVIDER_FIELDS
            if key in provider and provider[key] is not None}
    if not isinstance(safe.get("name"), str) or not safe["name"]:
        raise RuntimeError("Reasonix Observer provider has no name")
    if safe.get("kind") not in {"anthropic", "openai"}:
        raise RuntimeError(
            "Reasonix Observer provider must use the anthropic or openai protocol")
    endpoint = safe.get("base_url")
    parsed_endpoint = urlsplit(endpoint) if isinstance(endpoint, str) else None
    if endpoint is not None and (parsed_endpoint is None or
                                 parsed_endpoint.scheme not in {"http", "https"} or
                                 not parsed_endpoint.hostname):
        raise RuntimeError("Reasonix Observer provider has an invalid base_url")
    credential_name = safe.get("api_key_env", "")
    if not isinstance(credential_name, str) or credential_name and (
            not _REASONIX_ENV_NAME.fullmatch(credential_name) or
            not _REASONIX_AUTH_NAME.search(credential_name) or
            credential_name in _OBSERVER_ENV_KEYS or
            credential_name.startswith(("ORCHESTRA_", "REASONIX_", "OPENCODE_"))
    ):
        raise RuntimeError("Reasonix Observer provider has an invalid api_key_env")
    # Provider-native search is another tool surface; it is always off here.
    safe["web_search"] = False
    fleet_config.validate_nonsecret_mapping({
        key: value for key, value in safe.items() if key != "api_key_env"
    }, "config")

    isolated_home = Path(directory) / ".reasonix"
    isolated_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    lines = ["config_version = 5", f"default_model = {_toml_value(requested)}",
             "", "[[providers]]"]
    lines.extend(f"{key} = {_toml_value(value)}" for key, value in safe.items())
    lines.extend(("", _REASONIX_OBSERVER_CONFIG))
    reasonix_config = Path(directory) / "reasonix.toml"
    reasonix_config.write_text("\n".join(lines), encoding="utf-8")
    proc.chmod(reasonix_config, 0o600)

    result = {
        "REASONIX_HOME": str(isolated_home),
        "REASONIX_STATE_HOME": str(isolated_home / "state"),
        "REASONIX_CACHE_HOME": str(isolated_home / "cache"),
    }
    if credential_name:
        secret = os.environ.get(credential_name)
        if not secret:
            try:
                secret = config.secret_environment().get(credential_name)
            except config.ConfigError:
                secret = None
        if not secret:
            secret = _dotenv_value(source_home / ".env", credential_name)
        if not secret:
            raise RuntimeError(
                f"Reasonix Observer credential {credential_name} is unavailable")
        if "\0" in secret:
            raise RuntimeError(
                f"Reasonix Observer credential {credential_name} is malformed")
        result[credential_name] = secret
    return result


def _observer_environment() -> dict[str, str]:
    """Return OS launch plumbing, never daemon, run, or provider secrets."""
    inherited = {
        key: os.environ[key] for key in _OBSERVER_ENV_KEYS if key in os.environ
    }
    inherited.setdefault("PATH", os.defpath)
    if not proc.IS_WIN:
        inherited.setdefault("HOME", str(Path.home()))
    return proc.enrich_path(inherited)


def _observer_plan(profile: dict, runtime_row: dict, prompt: str,
                   check_id: int, directory: str) -> runtime.LaunchPlan:
    """Build the only process shapes allowed to receive Observer evidence."""
    profile, runtime_row = _observer_snapshots(profile, runtime_row)
    adapter = str(runtime_row.get("adapter") or "").lower()
    if adapter not in fleet_config.OBSERVER_ADAPTERS or \
            adapter not in _OBSERVER_ARGS:
        allowed = ", ".join(sorted(fleet_config.OBSERVER_ADAPTERS))
        raise RuntimeError(
            f"{adapter or 'unknown'} runtime cannot provide a tool-free "
            f"Observer; use {allowed}")

    reasonix_env = (_reasonix_isolation(profile, directory)
                    if adapter == "reasonix" else {})

    plan = runtime.launch_plan(
        runtime_row, profile, workdir=directory,
        title=f"orchestra-observer-{check_id}", prompt=prompt,
        run_id=check_id, inherited_env=_observer_environment())
    argv = list(plan.argv)

    if adapter == "reasonix":
        # `reasonix -p` is the documented print-mode surface that enforces
        # --allowed-tools. Do not rely on the similarly named `run` path.
        if len(argv) < 2 or argv[1] != "run":
            raise RuntimeError("Reasonix Observer launch posture is unsupported")
        argv[1] = "--print"
        plan.env.update(reasonix_env)
    elif adapter == "opencode":
        # The ordinary worker path adds --auto. Observer denies every tool and
        # runs without user/project config, plugins, skills, or a persistent DB.
        try:
            argv.remove("--auto")
        except ValueError as exc:
            raise RuntimeError(
                "OpenCode Observer launch posture is unsupported") from exc
        private = Path(directory) / ".opencode"
        config_dir = private / "config"
        opencode_config = config_dir / "opencode"
        cache_dir = private / "cache"
        for path in (opencode_config, cache_dir):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        plan.env.update({
            "XDG_CONFIG_HOME": str(config_dir),
            "OPENCODE_CONFIG_DIR": str(opencode_config),
            "OPENCODE_DB": str(private / "observer.db"),
            "XDG_CACHE_HOME": str(cache_dir),
            "OPENCODE_PURE": "1",
            "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
            "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
            "OPENCODE_DISABLE_CLAUDE_CODE": "1",
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
            "OPENCODE_PERMISSION": json.dumps(
                {"*": "deny"}, separators=(",", ":")),
            "OPENCODE_CONFIG_CONTENT": json.dumps({
                "permission": {"*": "deny"}, "snapshot": False,
                "plugin": [], "mcp": {}, "instructions": [],
            }, separators=(",", ":")),
        })

    return runtime.LaunchPlan(tuple(argv), plan.env, plan.stdin, plan.adapter)


def _observer_identity(pid: int) -> str | None:
    for _ in range(10):
        identity = proc.process_identity(pid)
        if identity:
            return identity
        time.sleep(0.01)
    return None


def _observer_timeout(profile: dict) -> int:
    try:
        configured = int(profile.get("timeout_seconds") or
                         OBSERVER_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        configured = OBSERVER_TIMEOUT_SECONDS
    return max(1, min(configured, OBSERVER_TIMEOUT_SECONDS))


def _log_tail(log_path: str, limit: int = OBSERVER_OUTPUT_CHARS) -> str:
    try:
        with open(log_path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            return handle.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _observer_turn(profile: dict, runtime_row: dict, prompt: str,
                   check_id: int, log_path: str, *, on_worker=None
                   ) -> tuple[str, dict]:
    timeout = _observer_timeout(profile)
    deadline = time.monotonic() + timeout
    with tempfile.TemporaryDirectory(prefix="orchestra-observer-") as directory:
        plan = _observer_plan(
            profile, runtime_row, prompt, check_id, directory)
        Path(log_path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        Path(log_path).touch(mode=0o600, exist_ok=True)
        proc.chmod(log_path, 0o600)

        def bound_worker(pid: int) -> None:
            identity = _observer_identity(pid)
            if identity is None:
                proc.terminate_group(pid, force=True)
                raise RuntimeError(
                    f"Observer worker {pid} has no durable process identity")
            if on_worker:
                on_worker(pid, identity)

        with open(log_path, "ab") as log:
            worker = subprocess.Popen(
                proc.resolve_cmd(list(plan.argv)),
                stdin=subprocess.PIPE if plan.stdin is not None
                else subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, cwd=directory,
                env=plan.env, **proc.session_kwargs())
            bound_worker(worker.pid)
            try:
                worker.communicate(
                    plan.stdin.encode() if plan.stdin is not None else None,
                    timeout=_remaining(deadline))
            except subprocess.TimeoutExpired:
                proc.terminate_group(worker.pid, force=True)
                worker.wait()
                raise RuntimeError(
                    f"Observer runtime timed out after {timeout} seconds")
        _, parsed = runners.parse_log(log_path)
        output = parsed or _log_tail(log_path)
        if worker.returncode != 0:
            raise RuntimeError(
                f"Observer runtime exited {worker.returncode}: "
                f"{output[-1000:] or 'no output'}")
        usage = runners.parse_usage(log_path, plan.adapter, profile.get("model"))
        return output[-OBSERVER_OUTPUT_CHARS:], usage


def _observer_process_state(pid, expected_identity) -> tuple[str, str]:
    """Return ownership state without ever signalling an unproven PID."""
    if pid is None:
        return "missing", "no process recorded"
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return "refused", f"invalid Observer pid {pid}"
    if pid <= 0:
        return "refused", f"invalid Observer pid {pid}"
    if not proc.alive(pid):
        return "gone", "process already gone"
    if not expected_identity:
        return "refused", f"Observer process {pid} has no recorded identity"
    actual = proc.process_identity(pid)
    if actual is None:
        return "refused", f"Observer process {pid} identity could not be read"
    if actual != expected_identity:
        # A changed identity proves the process we owned is gone. The new
        # occupant is deliberately not signalled.
        return "gone", f"Observer process {pid} was replaced"
    return "alive", f"Observer process {pid} is alive"


def _stop_observer_worker(check) -> tuple[bool, str]:
    state, detail = _observer_process_state(
        check["worker_pid"], check["worker_pid_identity"])
    if state in {"missing", "gone"}:
        return True, detail
    if state == "refused":
        return False, detail
    pid = int(check["worker_pid"])
    outcome, detail = proc.signal_owned_group(
        pid, check["worker_pid_identity"], signal.SIGTERM)
    if outcome == "refused":
        return False, detail
    deadline = time.monotonic() + OBSERVER_STOP_GRACE_SECONDS
    while time.monotonic() < deadline:
        state, detail = _observer_process_state(
            pid, check["worker_pid_identity"])
        if state == "gone":
            return True, detail
        if state == "refused":
            return False, detail
        time.sleep(0.1)
    outcome, detail = proc.signal_owned_group(
        pid, check["worker_pid_identity"], signal.SIGKILL)
    return outcome != "refused", detail


def _observer_alert(con, check, reason: str) -> None:
    attention.open_request(
        con, kind="alert", run_id=int(check["run_id"]),
        title=f"Observer check {check['id']} could not be recovered",
        body=reason, created_by="orchestra:observer-recovery",
        correlation_id=f"observer-recovery-refused:{check['id']}",
        callback_command=config.callback_command())


def _claim_observer(check_id: int) -> bool:
    pid = os.getpid()
    identity = _observer_identity(pid)
    if identity is None:
        return False
    con = db.connect()
    try:
        check = observer.find_check(con, check_id)
        if check is None or check["finished_at"] is not None:
            return False
        if check["supervisor_pid"] not in (None, pid):
            return False
        if check["supervisor_pid"] == pid and \
                check["supervisor_pid_identity"] not in (None, identity):
            return False
        changed = con.execute(
            "UPDATE observer_checks SET supervisor_pid=?,"
            "supervisor_pid_identity=? WHERE id=? AND finished_at IS NULL AND "
            "(supervisor_pid IS NULL OR supervisor_pid=?)",
            (pid, identity, int(check_id), pid))
        con.commit()
        return changed.rowcount == 1
    finally:
        con.close()


def _bind_observer_worker(con, check_id: int, pid: int, identity: str) -> None:
    changed = con.execute(
        "UPDATE observer_checks SET worker_pid=?,worker_pid_identity=? "
        "WHERE id=? AND finished_at IS NULL AND supervisor_pid=?",
        (int(pid), identity, int(check_id), os.getpid()))
    con.commit()
    if changed.rowcount != 1:
        proc.terminate_group(pid, force=True)
        raise RuntimeError(f"Observer check {check_id} no longer owns its worker")


def spawn_observer(check_id: int) -> int:
    """Start one detached Observer supervisor and bind its durable identity."""
    check_id = int(check_id)
    log_path = paths.logs_dir() / f"observer-{check_id}.jsonl"
    log_path.touch(mode=0o600, exist_ok=True)
    proc.chmod(log_path, 0o600)
    con = db.connect()
    try:
        check = observer.find_check(con, check_id)
        if check is None or check["finished_at"] is not None:
            raise RuntimeError(f"Observer check {check_id} is no longer available")
        con.execute("UPDATE observer_checks SET log_path=? WHERE id=?",
                    (str(log_path), check_id))
        con.commit()
    finally:
        con.close()
    command = [sys.executable, "-m", "orchestra", "_observe", str(check_id)]
    handle = open(log_path, "ab")
    try:
        process = subprocess.Popen(
            proc.resolve_cmd(command), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=handle,
            **proc.session_kwargs(detached=True))
    finally:
        handle.close()
    identity = _observer_identity(process.pid)
    if identity is None:
        proc.terminate_group(process.pid, force=True)
        raise RuntimeError(
            f"Observer supervisor {process.pid} has no durable identity")
    con = db.connect()
    try:
        changed = con.execute(
            "UPDATE observer_checks SET supervisor_pid=?,"
            "supervisor_pid_identity=?,log_path=? WHERE id=? AND "
            "(supervisor_pid IS NULL OR supervisor_pid=?)",
            (process.pid, identity, str(log_path), check_id, process.pid))
        con.commit()
    finally:
        con.close()
    if changed.rowcount != 1:
        proc.terminate_group(process.pid, force=True)
        raise RuntimeError(f"Observer check {check_id} was claimed concurrently")
    try:
        threading.Thread(target=process.wait, daemon=True).start()
    except RuntimeError:
        pass
    return process.pid


def _set_observer_delivery(con, check_id: int, status: str, *,
                           audit_id: int | None = None,
                           error: str | None = None) -> None:
    check = observer.find_check(con, check_id)
    if check is None:
        return
    try:
        detail = json.loads(check["detail_json"] or "{}")
    except (TypeError, ValueError):
        detail = {}
    if not isinstance(detail, dict):
        detail = {}
    detail["delivery_status"] = status
    if audit_id is not None:
        detail["control_audit_id"] = int(audit_id)
    if error:
        detail["delivery_error"] = str(error)[:2000]
    else:
        detail.pop("delivery_error", None)
    con.execute(
        "UPDATE observer_checks SET delivery_status=?,delivery_error=?,"
        "control_audit_id=?,detail_json=? WHERE id=? AND delivery_status='pending'",
        (status, str(error)[:2000] if error else None, audit_id,
         json.dumps(detail, ensure_ascii=False, separators=(",", ":")),
         int(check_id)))
    con.commit()


def _control_audit(con, check, action: str) -> int | None:
    row = con.execute(
        "SELECT id FROM control_events WHERE actor='observer' AND action=? "
        "AND target_type='run' AND target_id=? AND request_id=? ORDER BY id LIMIT 1",
        (action, str(check["run_id"]), f"observer:{check['id']}"),
    ).fetchone()
    return int(row["id"]) if row else None


def _deliver_observer_control(con, check) -> bool:
    if check is None or check["finished_at"] is None or \
            check["delivery_status"] != "pending":
        return False
    check_id, run_id = int(check["id"]), int(check["run_id"])
    request_id = f"observer:{check_id}"
    try:
        if check["action"] == "tell":
            existing = con.execute(
                "SELECT id FROM messages WHERE run_id=? AND kind='tell' "
                "AND correlation_id=? ORDER BY id LIMIT 1",
                (run_id, request_id),).fetchone()
            audit_id = _control_audit(con, check, "run.tell")
            if existing is None:
                try:
                    detail = json.loads(check["detail_json"] or "{}")
                except (TypeError, ValueError):
                    detail = {}
                message = detail.get("message") if isinstance(detail, dict) else None
                result = supervise.tell(
                    con, run_id, message or
                    check["reason"] or "Return to the stated mission.",
                    actor="observer", request_id=request_id)
                audit_id = result["control_audit_id"]
            elif audit_id is None:
                with con:
                    audit_id = db.record_control(
                        con, actor="observer", action="run.tell",
                        outcome="recovered", target_type="run",
                        target_id=run_id, request_id=request_id,
                        detail={"message_id": int(existing["id"])})
            _set_observer_delivery(
                con, check_id, "delivered", audit_id=audit_id)
            return True

        if check["action"] == "stop":
            audit_id = _control_audit(con, check, "run.stop")
            if audit_id is None:
                run = runs.find(con, run_id)
                if run is None or run["status"] in db.RUN_TERMINAL:
                    _set_observer_delivery(
                        con, check_id, "skipped",
                        error="run settled before Observer control delivery")
                    return True
                result = supervise.stop(
                    con, run_id, actor="observer", request_id=request_id,
                    reason=check["reason"] or "Stopped by Observer")
                audit_id = result["control_audit_id"]
                if run_id not in result["stopped_run_ids"]:
                    _set_observer_delivery(
                        con, check_id, "skipped", audit_id=audit_id,
                        error="run settled before Observer control delivery")
                    return True
            observer.publish_stop(
                con, run_id=run_id, check_id=check_id,
                reason=check["reason"] or "",
                callback_command=config.callback_command())
            _set_observer_delivery(
                con, check_id, "delivered", audit_id=audit_id)
            return True
    except supervise.ExecutionError as exc:
        _set_observer_delivery(con, check_id, "skipped", error=str(exc))
        return True
    except BaseException as exc:
        # Keep the durable outbox pending. A later daemon tick retries it;
        # correlation/request ids make both control paths idempotent.
        _set_observer_delivery(con, check_id, "pending", error=str(exc))
        return False
    _set_observer_delivery(
        con, check_id, "skipped", error="Observer action requires no control")
    return True


def _deliver_pending_observer_controls(con) -> list[int]:
    delivered = []
    rows = list(con.execute(
        "SELECT * FROM observer_checks WHERE finished_at IS NOT NULL "
        "AND delivery_status='pending' ORDER BY id"))
    for check in rows:
        if _deliver_observer_control(con, check):
            delivered.append(int(check["id"]))
    return delivered


def observe(check_id: int) -> int:
    """Detached entry point for one frozen Observer check."""
    check_id = int(check_id)
    if not _claim_observer(check_id):
        return 1
    con = db.connect()
    try:
        check = observer.find_check(con, check_id)
        if check is None or check["finished_at"] is not None:
            return 1
        try:
            profile = json.loads(check["profile_snapshot"] or "{}")
            runtime_row = json.loads(check["runtime_snapshot"] or "{}")
            if not isinstance(profile, dict) or not isinstance(runtime_row, dict):
                raise ValueError("snapshots must be objects")
            log_path = check["log_path"] or str(
                paths.logs_dir() / f"observer-{check_id}.jsonl")
            if not check["log_path"]:
                con.execute("UPDATE observer_checks SET log_path=? WHERE id=?",
                            (log_path, check_id))
                con.commit()
            output, usage = _observer_turn(
                profile, runtime_row, observer.check_prompt(check), check_id,
                log_path, on_worker=lambda pid, identity: _bind_observer_worker(
                    con, check_id, pid, identity))
            verdict = observer.finish_check(
                con, check_id, output, usage=usage,
                authority=check["authority"])
        except BaseException as exc:
            latest = observer.find_check(con, check_id)
            if latest is not None and latest["finished_at"] is None:
                observer.finish_check(
                    con, check_id, "", error=str(exc),
                    authority=latest["authority"])
            return 1

        del verdict
        _deliver_observer_control(con, observer.find_check(con, check_id))
        return 0
    finally:
        con.close()


def _recover_observer_check(con, check, launcher=spawn_observer) -> dict:
    check_id = int(check["id"])
    state, detail = _observer_process_state(
        check["supervisor_pid"], check["supervisor_pid_identity"])
    if state == "alive":
        return {"state": "active", "check_id": check_id}
    if state == "refused":
        _observer_alert(con, check, detail)
        return {"state": "refused", "check_id": check_id, "reason": detail}
    if state == "missing" and check["worker_pid"] is None:
        try:
            launcher(check_id)
            return {"state": "relaunched", "check_id": check_id}
        except BaseException as exc:
            observer.finish_check(con, check_id, "", error=(
                f"Observer launch failed: {exc}"), authority=check["authority"])
            return {"state": "failed", "check_id": check_id,
                    "reason": str(exc)}
    stopped, reason = _stop_observer_worker(check)
    if not stopped:
        _observer_alert(con, check, reason)
        return {"state": "refused", "check_id": check_id, "reason": reason}
    observer.finish_check(
        con, check_id, "", error=(
            f"Observer supervisor {check['supervisor_pid'] or '(unrecorded)'} "
            "vanished before the check settled."), authority=check["authority"])
    return {"state": "failed", "check_id": check_id}


def _recover_observer(con, launcher=spawn_observer) -> dict:
    """Recover the oldest active check (kept as a focused service seam)."""
    check = observer.active_check(con)
    if check is None:
        return {"state": "idle"}
    return _recover_observer_check(con, check, launcher)


def _recover_observers(con, launcher=spawn_observer) -> list[dict]:
    return [_recover_observer_check(con, check, launcher)
            for check in observer.active_checks(con)]


def _run_observer(con, launcher=spawn_observer) -> list[int]:
    _deliver_pending_observer_controls(con)
    _recover_observers(con, launcher)
    settings = fleet_config.observer(con)
    if not settings or not settings["enabled"]:
        return []
    concurrency = int(settings["max_concurrency"])
    active_count = int(con.execute(
        "SELECT COUNT(*) FROM observer_checks WHERE finished_at IS NULL"
    ).fetchone()[0])
    if active_count >= concurrency:
        return []
    checked: list[int] = []
    candidates = list(con.execute(
        "SELECT * FROM runs AS run WHERE status='running' AND NOT EXISTS ("
        "SELECT 1 FROM observer_checks AS check_row "
        "WHERE check_row.run_id=run.id AND check_row.finished_at IS NULL) "
        "ORDER BY id"
    ))
    for run in candidates:
        selected = _observer_profile(con, run, settings)
        if selected is None:
            continue
        due, _ = observer.due(
            con, int(run["id"]), first_look=int(settings["first_look_seconds"]),
            interval=int(settings["interval_seconds"]),
            min_events=int(settings["minimum_events"]))
        if not due:
            continue
        profile, runtime_row = selected
        prepared = None
        try:
            prepared = observer.prepare_check(
                con, int(run["id"]), profile_id=profile["profile_id"],
                profile_snapshot=profile, runtime_snapshot=runtime_row,
                authority=settings["authority"],
                max_concurrency=concurrency)
            launcher(prepared["check_id"])
        except observer.CheckNotDue:
            active_count = int(con.execute(
                "SELECT COUNT(*) FROM observer_checks WHERE finished_at IS NULL"
            ).fetchone()[0])
            if active_count >= concurrency:
                break
            continue
        except BaseException as exc:
            if prepared is not None:
                latest = observer.find_check(con, prepared["check_id"])
                if latest is not None and latest["finished_at"] is None:
                    observer.finish_check(
                        con, prepared["check_id"], "", error=(
                            f"Observer launch failed: {exc}"),
                        authority=settings["authority"])
            continue
        checked.append(int(run["id"]))
        active_count += 1
        if active_count >= concurrency:
            break
    return checked


def tick(con=None, *, launcher=supervise.spawn_supervisor,
         observer_launcher=spawn_observer) -> dict:
    """Run one deterministic maintenance/admission pass."""
    owned = con is None
    con = con or db.connect()
    try:
        db.meta_set(con, "daemon_last_tick", db.now())
        con.commit()
        proc.harvest_children()
        fallbacks = attention.apply_due_fallbacks(con)
        recovery = _recover(con, launcher)
        child_batches = child_runs.process_pending(con)
        settled = child_runs.settle_requests(con)
        resumed = _resume_waiters(con, launcher)
        runway_count = _poll_runway(con)
        admitted_state = scheduler.admit(con)
        for run_id in admitted_state["skipped"]:
            supervise._after_terminal(con, int(run_id))
        launched = [run_id for run_id in admitted_state["admitted"]
                    if _launch(con, run_id, launcher)]
        observed = _run_observer(con, observer_launcher)
        return {
            "fallbacks": fallbacks, "recovery": recovery,
            "child_batches": child_batches, "settled_child_requests": settled,
            "resumed": resumed, "runway_sources_polled": runway_count,
            "admission": admitted_state, "launched": launched,
            "observed": observed,
        }
    finally:
        if owned:
            con.close()


def _claim_daemon() -> None:
    con = db.connect()
    try:
        raw = db.meta_get(con, "daemon_pid")
        if raw:
            try:
                pid = int(raw)
            except ValueError:
                pid = 0
            expected = db.meta_get(con, "daemon_pid_identity")
            actual = proc.process_identity(pid) if pid and proc.alive(pid) else None
            if pid and proc.alive(pid) and (not expected or not actual or expected == actual):
                raise RuntimeError(f"orchestra daemon is already running as pid {pid}")
        pid = os.getpid()
        db.meta_set(con, "daemon_pid", str(pid))
        db.meta_set(con, "daemon_pid_identity", proc.process_identity(pid) or "")
        db.meta_set(con, "daemon_last_tick", db.now())
        con.commit()
    finally:
        con.close()


def _release_daemon() -> None:
    con = db.connect()
    try:
        if db.meta_get(con, "daemon_pid") == str(os.getpid()):
            con.execute("DELETE FROM meta WHERE key IN ('daemon_pid','daemon_pid_identity')")
            con.commit()
    finally:
        con.close()


def run(interval: float = DEFAULT_INTERVAL, once: bool = False) -> int:
    """Host HTTP and schedule until SIGTERM/SIGINT; ``once`` is offline QA."""
    if interval <= 0:
        raise ValueError("daemon interval must be positive")
    if once:
        tick()
        return 0
    _claim_daemon()
    stop_event, wake_event, restart_event = (
        threading.Event(), threading.Event(), threading.Event())
    previous = {}

    def stopping(signum, _frame):
        del signum
        stop_event.set()
        wake_event.set()

    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, stopping)
    server = threading.Thread(
        target=serve_http, args=(stop_event, wake_event, restart_event),
        daemon=True)
    try:
        server.start()
        time.sleep(0.05)
        if not server.is_alive():
            stop_event.set()
            return 1
        while not stop_event.is_set() and not restart_event.is_set():
            if not server.is_alive():
                print("orchestra daemon: HTTP server stopped unexpectedly",
                      file=sys.stderr)
                return 1
            started = time.monotonic()
            try:
                tick()
            except BaseException as exc:
                print(f"orchestra daemon: tick failed: {exc}", file=sys.stderr)
            remaining = max(0, interval - (time.monotonic() - started))
            wake_event.wait(remaining)
            wake_event.clear()
    finally:
        stop_event.set()
        if server.is_alive():
            server.join(timeout=5)
        _release_daemon()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return 75 if restart_event.is_set() else 0


def main() -> int:
    return run()
