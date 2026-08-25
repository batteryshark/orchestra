"""Profile discovery and display helpers (DESIGN D10).

Model/effort lists come from the supported harnesses themselves — a profile is
assembled from real lists, never typed and hoped. Discovery fails soft per
backend: a missing or broken CLI reports why and the others still print.

The headroom note (``note`` / ``note_at`` on a profile) surfaces in
``orchestra profiles`` with its age. It is routing intent for planners and
humans: the D1 planner packet is its dispatch-context consumer (phase 4);
it is never injected into worker briefs.
"""
import json
import subprocess
import tomllib
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DISCOVER_TIMEOUT = 20  # seconds per backend CLI; discovery must never hang

REASONIX_CONFIG = Path("~/.reasonix/config.toml")


def _run(cmd: list[str]) -> tuple[str | None, str | None]:
    """(stdout, error) — exactly one is None."""
    try:
        from orchestra.proc import resolve_cmd
        res = subprocess.run(resolve_cmd(cmd), capture_output=True, text=True,
                             encoding="utf-8", errors="replace",
                             timeout=DISCOVER_TIMEOUT)
    except FileNotFoundError:
        return None, f"{cmd[0]} is not installed"
    except subprocess.TimeoutExpired:
        return None, f"`{' '.join(cmd)}` timed out after {DISCOVER_TIMEOUT}s"
    if res.returncode != 0:
        detail = (res.stderr or res.stdout or "").strip().splitlines()
        return None, f"`{' '.join(cmd)}` failed: {detail[0][:200] if detail else res.returncode}"
    return res.stdout, None


# --- parsers (pure; tested against fixture output) --------------------------

def parse_opencode_models(text: str) -> dict[str, list[str]]:
    """`opencode models` prints one provider/model id per line."""
    providers: dict[str, list[str]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or "/" not in line:
            continue
        provider, _, model = line.partition("/")
        providers.setdefault(provider, []).append(model)
    return providers


def parse_codex_models(text: str) -> list[dict]:
    """`codex debug models` prints one JSON object with a `models` list."""
    data = json.loads(text)
    out = []
    for m in data.get("models", []):
        if not isinstance(m, dict) or not m.get("slug"):
            continue
        out.append({
            "model": m["slug"],
            "efforts": [lv.get("effort") for lv in
                        m.get("supported_reasoning_levels") or []
                        if isinstance(lv, dict) and lv.get("effort")],
            "default_effort": m.get("default_reasoning_level"),
        })
    return out


def parse_reasonix_config(text: str) -> list[dict]:
    """Reasonix declares providers, models and efforts in its own config."""
    data = tomllib.loads(text)
    out = []
    for p in data.get("providers", []):
        if not isinstance(p, dict) or not p.get("name"):
            continue
        out.append({
            "provider": p["name"],
            "models": list(p.get("models") or []),
            "efforts": list(p.get("supported_efforts") or []),
            "default_effort": p.get("default_effort"),
        })
    return out


# --- discovery --------------------------------------------------------------

def discover(runner=_run, reasonix_config: Path = REASONIX_CONFIG) -> dict:
    """{backend: {"data": parsed | None, "error": str | None}} per backend."""
    results: dict[str, dict] = {}

    def attempt(backend, cmd, parser):
        out, err = runner(cmd)
        if out is not None:
            try:
                return {"data": parser(out), "error": None}
            except (ValueError, KeyError) as exc:
                err = f"could not parse `{' '.join(cmd)}` output: {exc}"
        return {"data": None, "error": err}

    results["opencode"] = attempt("opencode", ["opencode", "models"],
                                  parse_opencode_models)
    results["codex"] = attempt("codex", ["codex", "debug", "models"],
                               parse_codex_models)
    path = reasonix_config.expanduser()
    try:
        results["reasonix"] = {"data": parse_reasonix_config(path.read_text(encoding="utf-8")),
                               "error": None}
    except OSError:
        results["reasonix"] = {"data": None, "error": f"{path} not found"}
    except ValueError as exc:
        results["reasonix"] = {"data": None, "error": f"could not parse {path}: {exc}"}
    results["claude"] = {"data": None,
                         "error": "no model listing command; set model on the profile"}
    return results


# --- local inference servers (W-0306 idea 3) --------------------------------

# Default localhost ports only: Ollama's own listing, and the OpenAI-style
# /v1/models that LM Studio and vLLM serve. The probe exists so a local-model
# profile can be assembled without remembering a model id no harness CLI
# lists (docs/investigations/W-0306-yeschef.md).
LOCAL_SERVERS = (
    ("ollama", "http://127.0.0.1:11434/api/tags"),
    ("lmstudio", "http://127.0.0.1:1234/v1/models"),
    ("vllm", "http://127.0.0.1:8000/v1/models"),
)
LOCAL_TIMEOUT = 1  # seconds per probe; a closed local port refuses at once


def _fetch_json(url: str):
    with urllib.request.urlopen(url, timeout=LOCAL_TIMEOUT) as res:
        return json.loads(res.read().decode("utf-8", "replace"))


def parse_local_models(source: str, data) -> list[str]:
    """Model names from one server's listing: Ollama's ``/api/tags`` is
    ``{"models": [{"name": …}]}``, the OpenAI shape is ``{"data": [{"id": …}]}``."""
    key = "name" if source == "ollama" else "id"
    rows = data.get("models" if source == "ollama" else "data") or []
    return [r[key] for r in rows
            if isinstance(r, dict) and isinstance(r.get(key), str)]


def discover_local(fetch=None, servers=None) -> list[dict]:
    """``[{"id", "source"}]`` from whatever local inference servers answer.

    Every failure is silence, never an error entry: on most machines every
    probe hits a closed port, and that is not news. A machine without these
    servers must see nothing new (W-0306 idea 3).
    """
    fetch = fetch or _fetch_json
    found: list[dict] = []
    for source, url in (LOCAL_SERVERS if servers is None else servers):
        try:
            names = parse_local_models(source, fetch(url))
        except Exception:  # silent by contract: absent, refused, or garbage
            continue
        found.extend({"id": name, "source": source} for name in names)
    return found


# --- display ----------------------------------------------------------------

def age_text(seconds: float) -> str:
    """'2h ago' from an age in seconds. One phrasing for every surface that
    shows how old a fact is — a profile note here, a run's last trace write
    in the progress heartbeat (traces.progress)."""
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def note_age(note_at: str | None, now: datetime | None = None) -> str | None:
    """'2h ago' from an ISO timestamp; None when absent or unreadable."""
    if not note_at:
        return None
    try:
        then = datetime.fromisoformat(note_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    seconds = ((now or datetime.now(timezone.utc)) - then).total_seconds()
    return age_text(max(seconds, 0))
