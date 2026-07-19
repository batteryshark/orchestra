"""Native quota collectors for MiniMax, Kimi, Z.AI, Claude, and Codex.

Each `parse_*` function takes the raw provider JSON and returns normalized
`QuotaWindow` rows. Each `collect_*` function handles credential discovery,
timeouts, and bounded reads, then returns a `ProviderResult`. Collectors never
raise — they always produce a `ProviderResult` with a useful `status` so the UI
can show actionable guidance instead of a hard error.
"""
from __future__ import annotations

import json
import math
import os
import pty
import re
import selectors
import select
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from orchestra_cli.usage.credentials import (
    CredentialError,
    CredentialMissing,
    opencode_api_key,
)
from orchestra_cli.usage.models import (
    ProviderResult,
    QuotaWindow,
    RateLimitResetCredits,
    error_result,
)


MINIMAX_USAGE_URL = "https://www.minimax.io/v1/token_plan/remains"
KIMI_USAGE_URL = "https://api.kimi.com/coding/v1/usages"
ZAI_USAGE_URL = "https://api.z.ai/api/monitor/usage/quota/limit"
MAX_RESPONSE_BYTES = 524_288
HTTP_TIMEOUT_SECONDS = 8.0
CODEX_TIMEOUT_SECONDS = 12.0
CLAUDE_CACHE_MAX_AGE_SECONDS = 300.0
CLAUDE_REFRESH_TIMEOUT_SECONDS = 20.0
MAX_CLAUDE_STATE_BYTES = 5_242_880

_CLAUDE_LIVE_LOCK = threading.Lock()
_CLAUDE_LIVE_USAGE: dict[str, dict[str, float]] | None = None
_CLAUDE_LIVE_FETCHED_AT = 0.0
_CLAUDE_REFRESH_IN_FLIGHT = False

JsonFetcher = Callable[[str, dict[str, str], float], dict[str, Any]]


class ProviderRequestError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def fetch_json(
    url: str,
    headers: dict[str, str],
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise ProviderRequestError(
            f"Provider returned HTTP {exc.code}", status_code=exc.code
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProviderRequestError("Could not reach provider") from exc
    if len(body) > MAX_RESPONSE_BYTES:
        raise ProviderRequestError("Provider response exceeded the safety limit")
    try:
        parsed = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderRequestError("Provider returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise ProviderRequestError("Provider returned an unexpected response")
    return parsed


def _epoch_iso(value: Any, *, milliseconds: bool = False) -> str | None:
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    seconds = value / 1000 if milliseconds else value
    try:
        return datetime.fromtimestamp(seconds, UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _duration_label(start_ms: Any, end_ms: Any, fallback: str) -> str:
    if not isinstance(start_ms, (int, float)) or not isinstance(end_ms, (int, float)):
        return fallback
    minutes = round((end_ms - start_ms) / 60_000)
    if minutes <= 0:
        return fallback
    if minutes % 10_080 == 0:
        weeks = minutes // 10_080
        return "Weekly" if weeks == 1 else f"{weeks}-week"
    if minutes % 1_440 == 0:
        days = minutes // 1_440
        return "Daily" if days == 1 else f"{days}-day"
    if minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours}-hour"
    return f"{minutes}-minute"


def parse_minimax(payload: dict[str, Any]) -> list[QuotaWindow]:
    base_response = payload.get("base_resp")
    if isinstance(base_response, dict) and base_response.get("status_code") not in (None, 0):
        raise ProviderRequestError("MiniMax rejected the usage request")
    rows = payload.get("model_remains")
    if not isinstance(rows, list):
        raise ProviderRequestError("MiniMax returned no quota windows")
    usable_rows = [row for row in rows if isinstance(row, dict)]
    general = next((row for row in usable_rows if row.get("model_name") == "general"), None)
    row = general or (usable_rows[0] if usable_rows else None)
    if row is None:
        raise ProviderRequestError("MiniMax returned no quota windows")

    windows: list[QuotaWindow] = []
    interval_remaining = row.get("current_interval_remaining_percent")
    if isinstance(interval_remaining, (int, float)):
        windows.append(
            QuotaWindow.from_remaining(
                id="rolling",
                label=_duration_label(row.get("start_time"), row.get("end_time"), "Current window"),
                scope="Coding models",
                remaining_percent=interval_remaining,
                resets_at=_epoch_iso(row.get("end_time"), milliseconds=True),
            )
        )
    weekly_remaining = row.get("current_weekly_remaining_percent")
    if isinstance(weekly_remaining, (int, float)):
        windows.append(
            QuotaWindow.from_remaining(
                id="weekly",
                label=_duration_label(
                    row.get("weekly_start_time"), row.get("weekly_end_time"), "Weekly"
                ),
                scope="Coding models",
                remaining_percent=weekly_remaining,
                resets_at=_epoch_iso(row.get("weekly_end_time"), milliseconds=True),
            )
        )
    if not windows:
        raise ProviderRequestError("MiniMax returned no usable quota percentages")
    return windows


def collect_minimax(*, json_fetcher: JsonFetcher = fetch_json) -> ProviderResult:
    try:
        credential = opencode_api_key(
            ("minimax-coding-plan", "minimax-cn-coding-plan"),
            ("MINIMAX_API_KEY",),
        )
    except CredentialMissing:
        return error_result(
            "minimax",
            "MiniMax",
            "not_configured",
            "Connect the MiniMax Token Plan in OpenCode or set MINIMAX_API_KEY.",
        )
    except CredentialError:
        return error_result(
            "minimax", "MiniMax", "unavailable", "MiniMax credentials could not be read."
        )
    try:
        payload = json_fetcher(
            MINIMAX_USAGE_URL,
            {
                "Authorization": f"Bearer {credential.value}",
                "Content-Type": "application/json",
                "User-Agent": "orchestra-cli/0.1",
            },
            HTTP_TIMEOUT_SECONDS,
        )
        windows = parse_minimax(payload)
    except ProviderRequestError as exc:
        if exc.status_code in (401, 403):
            return error_result(
                "minimax", "MiniMax", "auth_required", "MiniMax rejected the saved API key."
            )
        return error_result("minimax", "MiniMax", "unavailable", str(exc))
    return ProviderResult(
        id="minimax",
        name="MiniMax",
        status="ok",
        plan="Token Plan",
        windows=windows,
        source=credential.source,
    )


def _quota_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    number: float
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def _kimi_window(
    detail: Any,
    *,
    window_id: str,
    label: str,
) -> QuotaWindow | None:
    if not isinstance(detail, dict):
        return None
    limit = _quota_number(detail.get("limit"))
    remaining = _quota_number(detail.get("remaining"))
    if limit is None or remaining is None or limit <= 0 or remaining < 0:
        return None
    return QuotaWindow.from_remaining(
        id=window_id,
        label=label,
        scope="Kimi coding models",
        remaining_percent=remaining / limit * 100,
        resets_at=_normalize_reset_time(detail.get("resetTime")),
    )


def _kimi_duration_label(window: Any) -> str:
    if not isinstance(window, dict):
        return "Rolling window"
    duration = _quota_number(window.get("duration"))
    unit = window.get("timeUnit")
    if duration is None or duration <= 0 or not isinstance(unit, str):
        return "Rolling window"
    unit_minutes = {
        "TIME_UNIT_MINUTE": 1,
        "TIME_UNIT_HOUR": 60,
        "TIME_UNIT_DAY": 1_440,
    }.get(unit)
    if unit_minutes is None:
        return "Rolling window"
    minutes = duration * unit_minutes
    if not minutes.is_integer():
        return "Rolling window"
    return _duration_label(0, int(minutes) * 60_000, "Rolling window")


def parse_kimi(payload: dict[str, Any]) -> list[QuotaWindow]:
    windows: list[QuotaWindow] = []
    weekly = _kimi_window(payload.get("usage"), window_id="weekly", label="Weekly")
    if weekly:
        windows.append(weekly)

    limits = payload.get("limits")
    if isinstance(limits, list):
        for index, item in enumerate(limits):
            if not isinstance(item, dict):
                continue
            window = _kimi_window(
                item.get("detail"),
                window_id=f"rolling-{index}",
                label=_kimi_duration_label(item.get("window")),
            )
            if window:
                windows.append(window)
    if not windows:
        raise ProviderRequestError("Kimi returned no usable quota windows")
    return windows


def collect_kimi(*, json_fetcher: JsonFetcher = fetch_json) -> ProviderResult:
    try:
        credential = opencode_api_key(
            ("kimi-for-coding",),
            ("KIMI_API_KEY", "KIMI_CODE_API_KEY"),
        )
    except CredentialMissing:
        return error_result(
            "kimi",
            "Moonshot AI",
            "not_configured",
            "Connect Kimi for Coding in OpenCode or set KIMI_API_KEY.",
        )
    except CredentialError:
        return error_result(
            "kimi", "Moonshot AI", "unavailable", "Kimi credentials could not be read."
        )
    try:
        payload = json_fetcher(
            KIMI_USAGE_URL,
            {
                "Authorization": f"Bearer {credential.value}",
                "Content-Type": "application/json",
                "User-Agent": "orchestra-cli/0.1",
            },
            HTTP_TIMEOUT_SECONDS,
        )
        windows = parse_kimi(payload)
    except ProviderRequestError as exc:
        if exc.status_code in (401, 403):
            return error_result(
                "kimi", "Moonshot AI", "auth_required", "Kimi rejected the saved API key."
            )
        return error_result("kimi", "Moonshot AI", "unavailable", str(exc))
    return ProviderResult(
        id="kimi",
        name="Moonshot AI",
        status="ok",
        plan="Kimi Code",
        windows=windows,
        source=credential.source,
    )


ZAI_UNIT_NAMES = {
    3: "hour",
    5: "month",
    6: "week",
}


def _zai_window_label(item: dict[str, Any]) -> str:
    number = item.get("number")
    unit = ZAI_UNIT_NAMES.get(item.get("unit"))
    if isinstance(number, (int, float)) and unit:
        number = int(number)
        if number == 1 and unit == "week":
            return "Weekly"
        if number == 1 and unit == "month":
            return "Monthly"
        return f"{number}-{unit}"
    return "Quota window"


def parse_zai(payload: dict[str, Any]) -> list[QuotaWindow]:
    if payload.get("code") not in (None, 200):
        raise ProviderRequestError("Z.AI rejected the usage request")
    data = payload.get("data")
    limits = data.get("limits") if isinstance(data, dict) else None
    if not isinstance(limits, list):
        raise ProviderRequestError("Z.AI returned no quota windows")
    windows: list[QuotaWindow] = []
    for index, item in enumerate(limits):
        if not isinstance(item, dict) or not isinstance(item.get("percentage"), (int, float)):
            continue
        limit_type = item.get("type")
        scope = "MCP tools" if limit_type == "TIME_LIMIT" else "Coding tokens"
        unit = item.get("unit", "unknown")
        number = item.get("number", "unknown")
        windows.append(
            QuotaWindow.from_used(
                id=f"{str(limit_type).lower()}-{unit}-{number}-{index}",
                label=_zai_window_label(item),
                scope=scope,
                used_percent=item["percentage"],
                resets_at=_epoch_iso(item.get("nextResetTime"), milliseconds=True),
            )
        )
    if not windows:
        raise ProviderRequestError("Z.AI returned no usable quota percentages")
    return windows


def collect_zai(*, json_fetcher: JsonFetcher = fetch_json) -> ProviderResult:
    try:
        credential = opencode_api_key(
            ("zai-coding-plan", "zhipuai-coding-plan"),
            ("ZAI_API_KEY", "ZHIPUAI_API_KEY"),
        )
    except CredentialMissing:
        return error_result(
            "zai",
            "Z.AI",
            "not_configured",
            "Connect the Z.AI Coding Plan in OpenCode or set ZAI_API_KEY.",
        )
    except CredentialError:
        return error_result("zai", "Z.AI", "unavailable", "Z.AI credentials could not be read.")
    try:
        payload = json_fetcher(
            ZAI_USAGE_URL,
            {
                "Authorization": credential.value,
                "Accept-Language": "en-US,en",
                "Content-Type": "application/json",
                "User-Agent": "orchestra-cli/0.1",
            },
            HTTP_TIMEOUT_SECONDS,
        )
        windows = parse_zai(payload)
    except ProviderRequestError as exc:
        if exc.status_code in (401, 403):
            return error_result("zai", "Z.AI", "auth_required", "Z.AI rejected the saved API key.")
        return error_result("zai", "Z.AI", "unavailable", str(exc))
    return ProviderResult(
        id="zai",
        name="Z.AI",
        status="ok",
        plan="GLM Coding Plan",
        windows=windows,
        source=credential.source,
    )


CLAUDE_WINDOW_LABELS = {
    "five_hour": "5-hour",
    "seven_day": "Weekly",
    "seven_day_sonnet": "Weekly",
    "seven_day_opus": "Weekly",
    "seven_day_overage_included": "Weekly",
}
CLAUDE_WINDOW_SCOPES = {
    "five_hour": "All Claude models",
    "seven_day": "All Claude models",
    "seven_day_sonnet": "Sonnet",
    "seven_day_opus": "Opus",
    "seven_day_overage_included": "Included overage",
}


def _normalize_reset_time(value: Any) -> str | None:
    if isinstance(value, (int, float)):
        return _epoch_iso(value, milliseconds=value > 10_000_000_000)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def parse_claude(payload: dict[str, Any]) -> list[QuotaWindow]:
    windows: list[QuotaWindow] = []
    for key, label in CLAUDE_WINDOW_LABELS.items():
        item = payload.get(key)
        if not isinstance(item, dict):
            continue
        utilization = item.get("utilization")
        if not isinstance(utilization, (int, float)):
            utilization = item.get("percent")
        if not isinstance(utilization, (int, float)):
            continue
        used_percent = utilization * 100 if 0 <= utilization <= 1 else utilization
        windows.append(
            QuotaWindow.from_used(
                id=key,
                label=label,
                scope=CLAUDE_WINDOW_SCOPES[key],
                used_percent=used_percent,
                resets_at=_normalize_reset_time(item.get("resets_at")),
            )
        )
    scoped_limits = payload.get("limits")
    if isinstance(scoped_limits, list):
        for index, item in enumerate(scoped_limits):
            if not isinstance(item, dict) or item.get("kind") != "weekly_scoped":
                continue
            used = item.get("percent")
            if not isinstance(used, (int, float)):
                continue
            scope = item.get("scope")
            model = scope.get("model") if isinstance(scope, dict) else None
            display_name = model.get("display_name") if isinstance(model, dict) else None
            windows.append(
                QuotaWindow.from_used(
                    id=f"weekly-scoped-{index}",
                    label="Weekly",
                    scope=str(display_name or "Model-specific"),
                    used_percent=used,
                    resets_at=_normalize_reset_time(item.get("resets_at")),
                )
            )
    if not windows:
        raise ProviderRequestError("Claude returned no usable quota percentages")
    return windows


def _read_claude_state(*, state_path: Path | None = None) -> dict[str, Any]:
    path = state_path or Path.home() / ".claude.json"
    try:
        stat = path.stat()
        if stat.st_size > MAX_CLAUDE_STATE_BYTES:
            raise ProviderRequestError("Claude Code state file is unexpectedly large")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProviderRequestError("Claude Code state is not available") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ProviderRequestError("Claude Code state could not be read") from exc
    if not isinstance(payload, dict):
        raise ProviderRequestError("Claude Code state has an unexpected shape")
    return payload


def _claude_cached_usage(state: dict[str, Any]) -> tuple[dict[str, Any], float] | None:
    cached = state.get("cachedUsageUtilization")
    if not isinstance(cached, dict):
        return None
    fetched_at_ms = cached.get("fetchedAtMs")
    utilization = cached.get("utilization")
    if not isinstance(fetched_at_ms, (int, float)) or not isinstance(utilization, dict):
        return None
    return utilization, float(fetched_at_ms)


def _claude_refresh_cwd(state: dict[str, Any]) -> Path | None:
    override = os.environ.get("CLAUDE_USAGE_CWD")
    if override:
        path = Path(override).expanduser()
        return path if path.is_dir() else None
    projects = state.get("projects")
    if not isinstance(projects, dict):
        return None
    for raw_path, settings in reversed(list(projects.items())):
        if not isinstance(settings, dict):
            continue
        trusted = settings.get("hasTrustDialogAccepted") is True
        if not trusted:
            trusted = settings.get("hasCompletedProjectOnboarding") is True
        path = Path(raw_path).expanduser()
        if trusted and path.is_dir():
            return path
    return None


_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


def _clean_terminal_line(line: str) -> str:
    line = _ANSI_ESCAPE.sub("", line)
    line = re.sub(r"\x1b(?:[()][0-9A-Z]|.)", "", line)
    printable = (
        character for character in line if character >= " " or character == "\t"
    )
    return "".join(printable).strip()


def parse_claude_usage_screen(output: bytes | bytearray | str) -> dict[str, dict[str, float]]:
    """Extract live utilization from Claude Code's screen-reader `/usage` view.

    Claude Code can show current values without updating
    `cachedUsageUtilization` in ``~/.claude.json``. The screen-reader view is
    flat text, so parse only its named quota rows and ignore the surrounding
    terminal UI and account diagnostics.
    """
    text = (
        bytes(output).decode("utf-8", errors="replace")
        if isinstance(output, (bytes, bytearray))
        else output
    )
    lines = [
        _clean_terminal_line(line)
        for line in text.replace("\r", "\n").splitlines()
    ]
    labels = {
        "Current session": "five_hour",
        "Current week (all models)": "seven_day",
        "Current week (Sonnet only)": "seven_day_sonnet",
        "Current week (Opus only)": "seven_day_opus",
    }
    usage: dict[str, dict[str, float]] = {}
    for index, line in enumerate(lines):
        key = next((value for label, value in labels.items() if label in line), None)
        if not key:
            continue
        for candidate in lines[index + 1 : index + 4]:
            if "used" not in candidate.lower():
                continue
            percentages = re.findall(r"(\d+(?:\.\d+)?)%", candidate)
            if percentages:
                usage[key] = {"utilization": float(percentages[-1])}
                break
    return usage


def read_claude_live_usage(
    state: dict[str, Any],
    *,
    binary: str | None = None,
    timeout: float = CLAUDE_REFRESH_TIMEOUT_SECONDS,
) -> dict[str, dict[str, float]] | None:
    """Read current plan utilization from Claude Code's own `/usage` view.

    This starts a safe-mode interactive shell in a trusted project, sends the
    built-in command, parses only named percentages, and terminates before the
    optional local-session analysis finishes. No prompt is sent to a model.
    """
    executable = binary or os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    cwd = _claude_refresh_cwd(state)
    if not executable or cwd is None:
        return None
    try:
        master_fd, slave_fd = pty.openpty()
    except OSError:
        return None
    environment = os.environ.copy()
    environment["NO_COLOR"] = "1"
    try:
        process = subprocess.Popen(
            [executable, "--safe-mode", "--ax-screen-reader"],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=str(cwd),
            env=environment,
            close_fds=True,
        )
    except OSError:
        os.close(master_fd)
        os.close(slave_fd)
        return None
    os.close(slave_fd)
    sent_usage = False
    output = bytearray()
    started = time.monotonic()
    deadline = started + timeout
    try:
        while time.monotonic() < deadline:
            elapsed = time.monotonic() - started
            try:
                readable, _, _ = select.select([master_fd], [], [], 0.25)
            except OSError:
                break
            if readable:
                try:
                    chunk = os.read(master_fd, 65_536)
                    if len(output) < 262_144:
                        output.extend(chunk[: 262_144 - len(output)])
                except OSError:
                    break
            ready = b"manual mode on" in output or b"Manual mode on" in output
            if not sent_usage and (ready or elapsed >= 8.0):
                try:
                    os.write(master_fd, b"/usage\r")
                    sent_usage = True
                except OSError:
                    break
            if sent_usage:
                parsed = parse_claude_usage_screen(output)
                if "five_hour" in parsed and "seven_day" in parsed:
                    return parsed
            if process.poll() is not None:
                break
        parsed = parse_claude_usage_screen(output)
        return parsed or None
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        os.close(master_fd)


def _cached_claude_live_usage() -> dict[str, dict[str, float]] | None:
    with _CLAUDE_LIVE_LOCK:
        if (
            _CLAUDE_LIVE_USAGE is None
            or time.monotonic() - _CLAUDE_LIVE_FETCHED_AT > CLAUDE_CACHE_MAX_AGE_SECONDS
        ):
            return None
        return {key: dict(value) for key, value in _CLAUDE_LIVE_USAGE.items()}


def _request_claude_live_refresh(state: dict[str, Any]) -> None:
    """Refresh Claude usage off-request so a slow/rate-limited CLI cannot stall the UI."""
    global _CLAUDE_REFRESH_IN_FLIGHT
    with _CLAUDE_LIVE_LOCK:
        if _CLAUDE_REFRESH_IN_FLIGHT:
            return
        _CLAUDE_REFRESH_IN_FLIGHT = True

    def refresh() -> None:
        global _CLAUDE_LIVE_USAGE, _CLAUDE_LIVE_FETCHED_AT, _CLAUDE_REFRESH_IN_FLIGHT
        try:
            usage = read_claude_live_usage(state)
            if usage:
                with _CLAUDE_LIVE_LOCK:
                    _CLAUDE_LIVE_USAGE = usage
                    _CLAUDE_LIVE_FETCHED_AT = time.monotonic()
        finally:
            with _CLAUDE_LIVE_LOCK:
                _CLAUDE_REFRESH_IN_FLIGHT = False

    threading.Thread(target=refresh, name="orchestra-claude-usage", daemon=True).start()


def collect_claude(*, state_path: Path | None = None) -> ProviderResult:
    try:
        state = _read_claude_state(state_path=state_path)
    except ProviderRequestError:
        return error_result(
            "claude",
            "Claude",
            "auth_required",
            "Open Claude Code and run /usage once to enable plan usage.",
        )
    oauth_account = state.get("oauthAccount")
    subscription = (
        oauth_account.get("subscriptionType")
        if isinstance(oauth_account, dict)
        else None
    )
    plan = f"Claude {str(subscription).title()}" if subscription else "Claude subscription"
    cached = _claude_cached_usage(state)
    age_seconds = (time.time() * 1000 - cached[1]) / 1000 if cached else float("inf")
    live_usage = None
    if age_seconds > CLAUDE_CACHE_MAX_AGE_SECONDS:
        live_usage = _cached_claude_live_usage()
        if live_usage is None:
            _request_claude_live_refresh(state)
        if live_usage:
            old_usage = cached[0] if cached else {}
            base: dict[str, Any] = {}
            for key, item in live_usage.items():
                previous = old_usage.get(key)
                base[key] = {**previous, **item} if isinstance(previous, dict) else item
            cached = (base, time.time() * 1000)
            age_seconds = 0.0
    if cached is None:
        return ProviderResult(
            id="claude",
            name="Claude",
            status="stale",
            plan=plan,
            windows=[],
            message="Refreshing live /usage; no cached snapshot is available yet.",
            source="Claude Code /usage",
        )
    stale = age_seconds > CLAUDE_CACHE_MAX_AGE_SECONDS
    try:
        windows = parse_claude(cached[0])
    except ProviderRequestError as exc:
        return error_result("claude", "Claude", "unavailable", str(exc))
    if stale:
        # Live refresh via the Claude Code screen-reader is best-effort and runs
        # off-request. Show the cached plan percentages while it is pending or
        # unavailable, marked stale with an age hint and the real reset times.
        age_hint = (
            f"~{age_seconds / 3600:.1f}h" if age_seconds >= 3600
            else f"~{age_seconds / 60:.0f}m"
        )
        return ProviderResult(
            id="claude",
            name="Claude",
            status="stale",
            plan=plan,
            windows=windows,
            message=(
                f"Cached /usage snapshot is {age_hint} old; refreshing live usage in "
                "the background. Run /usage in Claude Code if it remains stale."
            ),
            source="Claude Code /usage cache",
        )
    return ProviderResult(
        id="claude",
        name="Claude",
        status="ok",
        plan=plan,
        windows=windows,
        message=None,
        source="Claude Code /usage" if live_usage else "Claude Code /usage cache",
    )


def _codex_window_label(window: dict[str, Any]) -> str:
    duration = window.get("windowDurationMins")
    if not isinstance(duration, (int, float)):
        duration = window.get("window_minutes")
    if not isinstance(duration, (int, float)) or duration <= 0:
        return "Quota window"
    minutes = int(duration)
    if minutes % 10_080 == 0:
        weeks = minutes // 10_080
        return "Weekly" if weeks == 1 else f"{weeks}-week"
    if minutes % 1_440 == 0:
        days = minutes // 1_440
        return "Daily" if days == 1 else f"{days}-day"
    if minutes % 60 == 0:
        return f"{minutes // 60}-hour"
    return f"{minutes}-minute"


def _codex_window(
    bucket_id: str, bucket: dict[str, Any], key: str, window: dict[str, Any]
) -> QuotaWindow | None:
    used = window.get("usedPercent")
    if not isinstance(used, (int, float)):
        used = window.get("used_percent")
    if not isinstance(used, (int, float)):
        return None
    reset = window.get("resetsAt")
    if reset is None:
        reset = window.get("resets_at")
    scope = bucket.get("limitName") or bucket.get("limit_name") or "Codex models"
    return QuotaWindow.from_used(
        id=f"{bucket_id}-{key}",
        label=_codex_window_label(window),
        scope=str(scope),
        used_percent=used,
        resets_at=_epoch_iso(reset),
    )


def parse_codex(payload: dict[str, Any]) -> tuple[list[QuotaWindow], str | None]:
    by_limit_id = payload.get("rateLimitsByLimitId")
    if not isinstance(by_limit_id, dict) or not by_limit_id:
        legacy = payload.get("rateLimits") or payload.get("rate_limits") or payload
        by_limit_id = {str(legacy.get("limitId") or legacy.get("limit_id") or "codex"): legacy}
    ordered_buckets = sorted(
        (
            (str(bucket_id), bucket)
            for bucket_id, bucket in by_limit_id.items()
            if isinstance(bucket, dict)
        ),
        key=lambda item: (item[0] != "codex", item[0]),
    )
    windows: list[QuotaWindow] = []
    plan: str | None = None
    for bucket_id, bucket in ordered_buckets:
        if plan is None:
            raw_plan = bucket.get("planType") or bucket.get("plan_type")
            if isinstance(raw_plan, str):
                plan = raw_plan.replace("_", " ").title()
        for key in ("primary", "secondary"):
            window = bucket.get(key)
            if isinstance(window, dict):
                normalized = _codex_window(bucket_id, bucket, key, window)
                if normalized:
                    windows.append(normalized)
    if not windows:
        raise ProviderRequestError("Codex returned no usable quota percentages")
    return windows, plan


def parse_codex_reset_credits(payload: dict[str, Any]) -> RateLimitResetCredits | None:
    raw = payload.get("rateLimitResetCredits")
    if not isinstance(raw, dict):
        return None
    available_count = raw.get("availableCount")
    if (
        isinstance(available_count, bool)
        or not isinstance(available_count, int)
        or available_count < 0
    ):
        return None

    available = [
        credit
        for credit in raw.get("credits", [])
        if isinstance(credit, dict) and credit.get("status") == "available"
    ] if isinstance(raw.get("credits"), list) else []
    expirations = [
        (expires_at, credit)
        for credit in available
        if (expires_at := _epoch_iso(credit.get("expiresAt"))) is not None
    ]
    detail = min(expirations, key=lambda item: item[0])[1] if expirations else None
    if detail is None and available:
        detail = available[0]
    title = detail.get("title") if detail else None
    expires_at = _epoch_iso(detail.get("expiresAt")) if detail else None
    return RateLimitResetCredits(
        available_count=available_count,
        title=title if isinstance(title, str) and title.strip() else None,
        expires_at=expires_at,
    )


def read_codex_app_server(
    *, binary: str | None = None, timeout: float = CODEX_TIMEOUT_SECONDS
) -> dict[str, Any]:
    executable = binary or os.environ.get("CODEX_BIN") or shutil.which("codex")
    if not executable:
        raise ProviderRequestError("Codex CLI is not installed")
    try:
        process = subprocess.Popen(
            [executable, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        raise ProviderRequestError("Could not start the Codex app server") from exc

    selector: selectors.BaseSelector | None = None
    try:
        assert process.stdin is not None
        assert process.stdout is not None
        messages = (
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "orchestra-cli", "version": "0.1.0"},
                    "capabilities": {"experimentalApi": True},
                },
            },
            {"method": "initialized"},
            {"id": 2, "method": "account/rateLimits/read"},
        )
        for message in messages:
            process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        process.stdin.flush()

        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            wait_for = max(0.0, deadline - time.monotonic())
            if not selector.select(wait_for):
                break
            line = process.stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") != 2:
                continue
            if isinstance(message.get("error"), dict):
                raise ProviderRequestError("Codex rejected the quota request")
            result = message.get("result")
            if not isinstance(result, dict):
                raise ProviderRequestError("Codex returned an unexpected response")
            return result
        raise ProviderRequestError("Codex quota request timed out")
    finally:
        if selector is not None:
            selector.close()
        if process.stdin is not None:
            process.stdin.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        if process.stdout is not None:
            process.stdout.close()


def _tail_bytes(path: Path, limit: int = 1_048_576) -> str:
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - limit))
        data = handle.read(limit)
    return data.decode("utf-8", errors="replace")


def read_recent_codex_snapshot(*, sessions_path: Path | None = None) -> dict[str, Any] | None:
    root = sessions_path or Path.home() / ".codex/sessions"
    try:
        rollouts = sorted(
            root.glob("*/*/*/rollout-*.jsonl"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )[:5]
    except OSError:
        return None
    for rollout in rollouts:
        try:
            lines = _tail_bytes(rollout).splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if '"rate_limits"' not in line and '"rateLimits"' not in line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else event
            snapshot = payload.get("rate_limits") or payload.get("rateLimits")
            if isinstance(snapshot, dict):
                return snapshot
    return None


def collect_codex(
    *, app_server_reader: Callable[[], dict[str, Any]] = read_codex_app_server
) -> ProviderResult:
    try:
        payload = app_server_reader()
        windows, plan = parse_codex(payload)
        return ProviderResult(
            id="codex",
            name="Codex",
            status="ok",
            plan=plan or "ChatGPT plan",
            windows=windows,
            rate_limit_resets=parse_codex_reset_credits(payload),
            source="Codex app server",
        )
    except ProviderRequestError as live_error:
        fallback = read_recent_codex_snapshot()
        if fallback:
            try:
                windows, plan = parse_codex(fallback)
                return ProviderResult(
                    id="codex",
                    name="Codex",
                    status="stale",
                    plan=plan or "ChatGPT plan",
                    windows=windows,
                    message=f"Live query failed; showing the latest local session snapshot ({live_error}).",
                    source="Recent Codex session",
                )
            except ProviderRequestError:
                pass
        return error_result("codex", "Codex", "unavailable", str(live_error))
