"""Live backend, provider, model, and roster-profile discovery.

The supported CLIs expose different evidence. OpenCode has a model catalog whose
provider prefixes reflect configured providers. Codex exposes its effective model
catalog through ``codex debug models``. Claude exposes login status but no stable
account-specific model-list command. Keep that distinction explicit: ``unknown``
is not silently promoted to ``available`` or ``unavailable``.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable, Mapping


BACKENDS = ("opencode", "codex", "claude")
PROBE_TIMEOUT_SECONDS = 15
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _run(command: list[str], run_fn: Callable | None = None) -> tuple[int | None, str, str]:
    runner = run_fn or subprocess.run
    try:
        result = runner(
            command,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return None, "", "timed out"
    except OSError:
        return None, "", "could not start"
    return result.returncode, result.stdout or "", result.stderr or ""


def _model_lines(output: str) -> list[str]:
    models: set[str] = set()
    for raw in output.splitlines():
        line = _ANSI_RE.sub("", raw).strip()
        if "/" not in line or any(char.isspace() for char in line):
            continue
        provider, model = line.split("/", 1)
        if provider and model:
            models.add(line)
    return sorted(models, key=str.lower)


def _base_result(backend: str, executable: str | None) -> dict:
    if executable is None:
        return {
            "backend": backend,
            "state": "unavailable",
            "detail": f"{backend} CLI is not installed or not on PATH",
            "executable": None,
            "models": None,
        }
    return {
        "backend": backend,
        "state": "unknown",
        "detail": "availability has not been verified",
        "executable": executable,
        "models": None,
    }


def _probe_opencode(executable: str, *, refresh: bool, run_fn: Callable | None) -> dict:
    command = [executable, "models"]
    if refresh:
        command.append("--refresh")
    code, stdout, error = _run(command, run_fn)
    if code is None:
        return {
            "backend": "opencode", "state": "unknown",
            "detail": f"model discovery {error}", "executable": executable,
            "models": None,
        }
    if code != 0:
        return {
            "backend": "opencode", "state": "unknown",
            "detail": f"model discovery exited with status {code}",
            "executable": executable, "models": None,
        }
    models = _model_lines(stdout)
    return {
        "backend": "opencode", "state": "available",
        "detail": f"{len(models)} models reported by OpenCode",
        "executable": executable, "models": models,
    }


def _probe_codex(executable: str, run_fn: Callable | None) -> dict:
    code, stdout, stderr = _run([executable, "login", "status"], run_fn)
    text = f"{stdout}\n{stderr}".lower()
    if code == 0:
        catalog_code, catalog_stdout, _catalog_stderr = _run(
            [executable, "debug", "models"], run_fn
        )
        if catalog_code == 0:
            try:
                payload = json.loads(catalog_stdout)
            except (json.JSONDecodeError, TypeError):
                payload = None
            entries = payload.get("models") if isinstance(payload, dict) else None
            if isinstance(entries, list):
                models = sorted({
                    entry["slug"]
                    for entry in entries
                    if isinstance(entry, dict)
                    and isinstance(entry.get("slug"), str)
                    and entry["slug"]
                }, key=str.lower)
                return {
                    "backend": "codex", "state": "available",
                    "detail": f"authenticated · {len(models)} models reported by Codex",
                    "executable": executable, "models": models,
                }
        return {
            "backend": "codex", "state": "available",
            "detail": "authenticated; model catalog is unavailable",
            "executable": executable, "models": None,
        }
    elif code is None:
        state, detail = "unknown", f"authentication check {stderr}"
    elif any(phrase in text for phrase in ("not logged", "login required", "not authenticated")):
        state, detail = "unavailable", "authentication required"
    else:
        state, detail = "unknown", f"authentication check exited with status {code}"
    return {
        "backend": "codex", "state": state, "detail": detail,
        "executable": executable, "models": None,
    }


def _probe_claude(executable: str, run_fn: Callable | None) -> dict:
    code, stdout, stderr = _run([executable, "auth", "status", "--json"], run_fn)
    if code is None:
        state, detail = "unknown", f"authentication check {stderr}"
    else:
        try:
            payload = json.loads(stdout)
        except (json.JSONDecodeError, TypeError):
            payload = None
        if isinstance(payload, dict) and payload.get("loggedIn") is True:
            state, detail = "available", "authenticated"
        elif isinstance(payload, dict) and payload.get("loggedIn") is False:
            state, detail = "unavailable", "authentication required"
        elif code != 0 and any(
            phrase in f"{stdout}\n{stderr}".lower()
            for phrase in ("not logged", "login required", "not authenticated")
        ):
            state, detail = "unavailable", "authentication required"
        else:
            detail = (f"authentication check exited with status {code}"
                      if code else "authentication check returned unreadable status")
            state = "unknown"
    return {
        "backend": "claude", "state": state, "detail": detail,
        "executable": executable, "models": None,
    }


def _profile_result(name: str, agent: Mapping, backend: Mapping | None) -> dict:
    backend_name = str(agent.get("backend") or "opencode")
    model = agent.get("model") if isinstance(agent.get("model"), str) else None
    base = {
        "name": name,
        "backend": backend_name,
        "model": model,
        "role": str(agent.get("role") or ""),
    }
    if backend_name not in BACKENDS:
        return {**base, "state": "unavailable",
                "detail": f"unsupported backend {backend_name!r}"}
    if backend is None:
        return {**base, "state": "unknown", "detail": "backend was not checked"}
    if backend.get("state") != "available":
        return {**base, "state": backend.get("state", "unknown"),
                "detail": backend.get("detail", "backend availability is unknown")}
    if backend_name in ("opencode", "codex"):
        models = backend.get("models")
        if models is None:
            return {**base, "state": "unknown", "detail": "model catalog is unavailable"}
        if model is None:
            if backend_name == "codex":
                return {**base, "state": "available",
                        "detail": "authenticated; using Codex default model"}
            return {**base, "state": "unknown",
                    "detail": "OpenCode default model cannot be inferred from the catalog"}
        if model in models:
            return {**base, "state": "available",
                    "detail": f"model is in {backend_name.title()}'s catalog"}
        if backend_name == "codex":
            return {**base, "state": "unavailable",
                    "detail": f"{model} is not reported by Codex"}
        provider = model.split("/", 1)[0] if "/" in model else "model"
        return {**base, "state": "unavailable",
                "detail": f"{model} is not reported for provider {provider}"}
    if model:
        return {**base, "state": "unknown",
                "detail": "authenticated; this CLI does not expose a model catalog"}
    return {**base, "state": "available", "detail": "authenticated; using backend default"}


def discover(
    cfg: Mapping,
    *,
    refresh: bool = False,
    only_backends: Iterable[str] | None = None,
    which_fn: Callable[[str], str | None] | None = None,
    run_fn: Callable | None = None,
) -> dict:
    """Return a credential-free live capability report."""
    which = which_fn or shutil.which
    selected = set(BACKENDS if only_backends is None else only_backends)
    backends: list[dict] = []
    for backend_name in BACKENDS:
        if backend_name not in selected:
            continue
        executable = which(backend_name)
        if executable is None:
            backends.append(_base_result(backend_name, None))
        elif backend_name == "opencode":
            backends.append(_probe_opencode(executable, refresh=refresh, run_fn=run_fn))
        elif backend_name == "codex":
            backends.append(_probe_codex(executable, run_fn))
        else:
            backends.append(_probe_claude(executable, run_fn))

    by_backend = {item["backend"]: item for item in backends}
    agents = cfg.get("agents") if isinstance(cfg, Mapping) else None
    if not isinstance(agents, Mapping):
        agents = {}
    roster = [
        _profile_result(name, agent, by_backend.get(str(agent.get("backend") or "opencode")))
        for name, agent in sorted((agents or {}).items())
        if isinstance(agent, Mapping)
    ]

    providers: list[dict] = []
    opencode = by_backend.get("opencode")
    if opencode and isinstance(opencode.get("models"), list):
        counts: dict[str, int] = {}
        for model in opencode["models"]:
            provider = model.split("/", 1)[0]
            counts[provider] = counts.get(provider, 0) + 1
        providers.extend({
            "id": provider, "backend": "opencode", "state": "available",
            "model_count": count,
        } for provider, count in sorted(counts.items()))
    for backend_name in ("codex", "claude"):
        backend = by_backend.get(backend_name)
        if backend:
            providers.append({
                "id": backend_name, "backend": backend_name,
                "state": backend["state"], "model_count": None,
            })
    return {"backends": backends, "providers": providers, "roster": roster}


def search_models(report: Mapping, query: str) -> list[str]:
    needle = query.casefold()
    return sorted({
        model
        for backend in report.get("backends") or []
        for model in (backend.get("models") or [])
        if needle in model.casefold()
    }, key=str.lower)


def check_profiles(cfg: Mapping, profiles: Iterable[tuple[str, Mapping]]) -> tuple[dict, list[str], list[str]]:
    """Discover target backends and return (report, blocking issues, warnings)."""
    profiles = list(profiles)
    selected = {str(agent.get("backend") or "opencode") for _, agent in profiles}
    report = discover(cfg, only_backends=selected)
    by_name = {item["name"]: item for item in report["roster"]}
    issues: list[str] = []
    warnings: list[str] = []
    for name, _agent in profiles:
        item = by_name[name]
        message = f"{name}: {item['detail']}"
        if item["state"] == "unavailable":
            issues.append(message)
        elif item["state"] == "unknown":
            warnings.append(message)
    return report, issues, warnings


def render(report: Mapping, query: str | None = None) -> str:
    """Render a compact human-readable discovery report."""
    lines = ["backends:"]
    for item in report.get("backends") or []:
        lines.append(
            f"  {item['backend']:<9} {item['state']:<11} {item['detail']}"
        )
    lines.append("\nproviders:")
    providers = report.get("providers") or []
    if providers:
        for item in providers:
            model_count = item.get("model_count")
            count = (f" · {model_count} {'model' if model_count == 1 else 'models'}"
                     if isinstance(model_count, int) else "")
            lines.append(f"  {item['id']:<28} {item['state']}{count}")
    else:
        lines.append("  (none discovered)")
    lines.append("\nroster profiles:")
    for item in report.get("roster") or []:
        model = item.get("model") or "(default)"
        lines.append(
            f"  {item['name']:<12} {item['backend']:<9} {model:<42} "
            f"{item['state']} · {item['detail']}"
        )
    if query is not None:
        matches = search_models(report, query)
        lines.append(f"\nmodels matching {query!r}:")
        lines.extend(f"  {model}" for model in matches)
        if not matches:
            lines.append("  (none)")
    else:
        lines.append("\nSearch models with `orchestra discover TEXT`; use --refresh to refresh OpenCode's catalog.")
    return "\n".join(lines)
