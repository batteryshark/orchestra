"""Profile discovery and display helpers (DESIGN D10).

Model/effort lists come from the harnesses themselves — a profile is
assembled from real lists, never typed and hoped. Discovery fails soft per
backend: a missing or broken CLI reports why and the others still print.

The headroom note (``note`` / ``note_at`` on a profile) surfaces in
``dromond profiles`` with its age. It is routing intent for planners and
humans: the D1 planner packet is its dispatch-context consumer (phase 4);
it is never injected into worker briefs.
"""
import json
import subprocess
import tomllib
from datetime import datetime, timezone
from pathlib import Path

DISCOVER_TIMEOUT = 20  # seconds per backend CLI; discovery must never hang

REASONIX_CONFIG = Path("~/.reasonix/config.toml")


def _run(cmd: list[str]) -> tuple[str | None, str | None]:
    """(stdout, error) — exactly one is None."""
    try:
        from dromond.proc import resolve_cmd
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


# --- display ----------------------------------------------------------------

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
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"
