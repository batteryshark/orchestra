"""Provider runway adapters (DESIGN §11).

One adapter per provider, each returning the same ``Runway`` record. Four
read a remote endpoint, two read CLI-owned local state.

Key handling (W-0182): a key comes from OPENCODE'S OWN CREDENTIAL STORE,
``~/.local/share/opencode/auth.json``, falling back to a named environment
variable. It is read inside the adapter, used for one request, and never
returned, stored, printed, or logged. ``raw`` is assembled from named fields
and then scrubbed, so no response body (which may echo a key) reaches the
database or the screen. ``api_key`` returns the value and a SOURCE LABEL;
only the label is ever safe to show.

Failure model: every adapter fails soft. Unreachable, unauthorized, garbage
JSON, or a local file whose shape drifted all yield a ``Runway`` with
``remaining=None`` and a ``reason``. Adapters never raise (DESIGN §11:
unknown runway means the provider is available and marked unknown; dispatch
never blocks on a failed scraper).

Windows, not providers, are the unit of a reading (W-0179): a provider is
ONE record carrying EVERY window it reports, because a 5-hour limit and a
weekly limit are two facts about one plan. There is no ``limit`` on a
window (W-0182) — every window is a percentage of itself, so a limit is
always 100 and says nothing.

Staleness survives only where it changes meaning (W-0182): a reading whose
window has already reset is history, and says so. The age of a reading is
kept on the record for the conductor, which must not trigger on old numbers,
and is no longer narrated to the reader.

Two provider KINDS (W-0179). A ``plan`` provider (Claude, Codex, Kimi,
MiniMax, Grok) is a subscription: it shows consumption against its windows
and never a price. Only an ``api`` provider (DeepSeek, anything billed per
token) shows money — DeepSeek's account balance is the point of its card.

A plan may still bank something spendable, though (W-0184): Codex and Grok
both hold usage RESETS, which are not money and belong on a plan's card. Any
adapter can put a finished phrase in ``raw["credits"]`` and every surface
renders it in the one block DeepSeek's balance already used.
"""
import functools
import selectors
import shutil
import subprocess
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from dromond import db

HTTP_TIMEOUT = 10

# OpenCode already holds these credentials; a second copy in a second place
# is a second thing to rotate. The named environment variable stays as the
# fallback for a machine that does not run OpenCode.
OPENCODE_AUTH = Path("~/.local/share/opencode/auth.json")

# provider -> (auth.json entry, environment variable fallback)
KEY_SOURCES = {
    "deepseek": ("deepseek", "DEEPSEEK_API_KEY"),
    "kimi": ("kimi-for-coding", "KIMI_CODING_API_KEY"),
    "minimax": ("minimax-coding-plan", "MINIMAX_API_KEY"),
    "xai": ("xai", "XAI_API_KEY"),
}

DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"

# Kimi's coding plan is a separate surface from the Moonshot open platform,
# with its own key (sk-kimi-…). Undocumented endpoint, verified 2026-08-13.
KIMI_USAGE_URL = "https://api.kimi.com/coding/v1/usages"

# MiniMax Token Plan. Note the www host — the api host does not serve it.
MINIMAX_USAGE_URL = "https://www.minimax.io/v1/token_plan/remains"
MINIMAX_MODEL = "general"  # the coding models; the other row is video

# xAI's DOCUMENTED api (api.x.ai) publishes no quota route — every usage-shaped
# path under /v1 is 404. The SuperGrok SUBSCRIPTION does, on the hosts the Grok
# CLI talks to, and that subscription is what OpenCode logs into. Three calls,
# verified live 2026-08-14: the user id, the billing window keyed by it, and a
# gRPC-Web RPC for banked usage resets.
XAI_USER_URL = "https://cli-chat-proxy.grok.com/v1/user"
XAI_BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
XAI_RESETS_URL = "https://grok.com/prod_mc_billing.ConsumerUiSvc/GetRemainingResets"

# Renewing OpenCode's OAuth grant. The client id is OpenCode's own, public by
# construction in a PKCE flow and not a secret.
XAI_TOKEN_URL = "https://auth.x.ai/oauth2/token"
XAI_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_REFRESH_SKEW = 120  # seconds before expiry; the grant lives about two hours

CLAUDE_CACHE = Path("~/.claude.json")

CODEX_SESSIONS = Path("~/.codex/sessions")
CODEX_SCAN_FILES = 5  # a just-started session has no token_count event yet

# A reading older than this is a hint, not a budget. The conductor refuses to
# trigger on one; the surfaces no longer narrate it.
STALE_AFTER_H = 1

# Subscriptions. Everything else is billed per token and may show money.
PLAN_PROVIDERS = frozenset({"claude", "codex", "kimi", "minimax", "xai"})

# Windows are named by their length, so one labeller serves every provider:
# Claude's five_hour/seven_day and Codex's window_minutes are the same thing.
WINDOW_LABELS = {60: "hourly", 300: "5h", 1440: "daily", 10080: "weekly"}

_SECRET_NAME = re.compile(
    r"key|token|secret|auth|password|credential|cookie|session", re.I)
_SECRET_VALUE = re.compile(r"^(sk-|Bearer\s)|^[A-Za-z0-9_\-]{32,}$")


@dataclass
class Runway:
    """(provider, remaining, unit, resets_at, raw) plus the fields the failure
    model needs: ``as_of`` (how fresh the number is) and ``reason`` (why it is
    unknown). ``remaining is None`` means unknown.

    ``kind`` says whether money exists for this provider at all, ``stale``
    that a window in the reading has already reset, and ``windows`` carries
    EVERY window the provider reports. The scalar fields are the tightest
    live window — dispatch and the conductor need one number. For an ``api``
    provider with no window they are the account balance and its currency.
    """
    provider: str
    remaining: float | None = None
    unit: str = ""
    resets_at: str | None = None
    raw: dict = field(default_factory=dict)
    as_of: str | None = None
    reason: str | None = None
    kind: str = "api"
    stale: bool = False
    windows: list = field(default_factory=list)

    def __post_init__(self) -> None:
        self.kind = kind_of(self.provider)

    @property
    def known(self) -> bool:
        return self.remaining is not None

    def as_dict(self) -> dict:
        return asdict(self)


def kind_of(provider: str) -> str:
    """``plan`` never shows money; ``api`` is billed per token and may."""
    return "plan" if provider in PLAN_PROVIDERS else "api"


def provider_of(backend: str, model: str | None = None) -> str:
    """Which provider a RUN spends against, for the same plan/api split.

    ``claude`` and ``codex`` are their own subscription. ``opencode`` and
    ``reasonix`` route to whatever the model names, so the provider is the
    model's prefix — ``kimi-for-coding/k3`` is the Kimi coding plan,
    ``deepseek/…`` is billed per token.

    ponytail: a name table plus a prefix match, not a billing lookup. A new
    subscription routed through opencode reads as ``api`` until it is added
    to PLAN_PROVIDERS, which shows money that does not exist. Upgrade path:
    a ``billing = "plan"`` key on the profile, when a provider outgrows the
    table.
    """
    if backend in ("claude", "codex"):
        return backend
    head = (model or "").split("/")[0].strip().lower()
    if head.startswith("kimi") or head.startswith("moonshot"):
        return "kimi"
    if head.startswith("minimax"):
        return "minimax"
    if head.startswith("xai") or head.startswith("grok"):
        return "xai"
    return head or backend


def unknown(provider: str, reason: str) -> Runway:
    return Runway(provider, reason=reason)


def soft(provider: str):
    """Every exception becomes an unknown-with-reason. No adapter raises."""
    def wrap(fn):
        @functools.wraps(fn)
        def inner(*a, **kw):
            try:
                return fn(*a, **kw)
            except Exception as exc:  # noqa: BLE001 — that is the contract
                return unknown(provider, f"{type(exc).__name__}: {exc}")
        return inner
    return wrap


# --- helpers ----------------------------------------------------------------

def _scrub(obj):
    """Redact anything credential-shaped before it reaches raw."""
    if isinstance(obj, dict):
        return {k: "[redacted]" if _SECRET_NAME.search(str(k)) else _scrub(v)
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    if isinstance(obj, str) and _SECRET_VALUE.search(obj):
        return "[redacted]"
    return obj


def _iso(value) -> str | None:
    """ISO-8601 UTC from an ISO string, epoch seconds, or epoch millis."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("timestamp is a bool")
    if isinstance(value, (int, float)):
        secs = value / 1000 if value > 1e11 else value
        dt = datetime.fromtimestamp(secs, timezone.utc)
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seconds_until(iso: str | None) -> float | None:
    if not iso:
        return None
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (dt - datetime.now(timezone.utc)).total_seconds()


def expired(iso: str | None) -> bool:
    left = _seconds_until(iso)
    return left is not None and left <= 0


def age_hours(iso: str | None, now: float | None = None) -> float | None:
    """How old a reading is, in hours. None when there is no timestamp."""
    if not iso:
        return None
    stamped = datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    return max(0.0, ((time.time() if now is None else now) - stamped) / 3600)


def age_text(hours: float | None) -> str:
    if hours is None:
        return "unknown age"
    return f"{hours:.0f}h" if hours >= 1 else f"{hours * 60:.0f}m"


def window_label(minutes) -> str:
    """'weekly' beats '10080m window' on a dashboard read at a glance.

    Named lengths win; anything else is rendered in the largest whole unit it
    divides into. A table of four exact matches left minimax's 240-minute
    window reading '240m', which is arithmetic the reader should not have to
    do to learn it is four hours -- and four hours is a real difference from
    the five that Claude and Kimi give.
    """
    if not isinstance(minutes, (int, float)) or isinstance(minutes, bool):
        return "window"
    m = int(minutes)
    if m in WINDOW_LABELS:
        return WINDOW_LABELS[m]
    if m > 0 and m % 1440 == 0:
        return f"{m // 1440}d"
    if m > 0 and m % 60 == 0:
        return f"{m // 60}h"
    return f"{m}m"


def make_window(label: str, remaining: float, resets_at: str | None) -> dict:
    """One reported window: a percentage of itself and when it refills.

    No ``limit`` (W-0182) — a window's limit is always 100% of the window, so
    the field carried no information. STALE means the reset has already passed;
    the flag stays because the conductor must not trigger on a number that is
    really history, but nothing narrates it to the reader. The daemon polls on
    its own schedule, so a reading that old is a fault to fix, not a caption to
    write (owner, 2026-08-14).
    """
    return {"label": label, "remaining": remaining, "unit": "percent",
            "resets_at": resets_at, "stale": expired(resets_at),
            "stale_reason": None}


def as_of_now(window: dict) -> dict:
    """A window as it stands NOW, which is not always what was measured.

    A window whose reset has already passed says NOTHING about the present. It
    refilled at that moment and has been drawn down since by an amount nobody
    measured. Reporting the reading from before the reset is not a stale
    number, it is a wrong one: Claude's five-hour window read 88% for two days
    after it had reset, because ~/.claude.json is written by Claude Code and
    Dromond cannot refresh it by polling harder.

    So an expired window reports unknown, and says why. Every surface already
    draws unknown as "no bar" rather than an empty one, which is the honest
    shape: not "you have none left", but "nobody knows".
    """
    if not expired(window.get("resets_at")):
        return window
    return {**window, "remaining": None, "stale": True,
            "stale_reason": "reset since this was read"}


def from_windows(provider: str, windows: list[dict], as_of: str | None = None,
                 raw: dict | None = None) -> Runway:
    """Scalar fields = the tightest window that has NOT itself reset, since
    that is the one dispatch and the conductor care about; ``windows`` carries
    all of them for display."""
    live = [w for w in windows if not expired(w["resets_at"])] or windows
    tight = min(live, key=lambda w: w["remaining"])
    return Runway(provider, remaining=tight["remaining"], unit=tight["unit"],
                  resets_at=tight["resets_at"], as_of=as_of,
                  stale=any(w["stale"] for w in windows), windows=windows,
                  raw=raw or {})


def until_text(iso: str | None) -> str:
    """Days, hours and minutes (W-0182). "in 2d 7h 41m" is a plan; "in 55h"
    is arithmetic the reader has to do."""
    left = _seconds_until(iso)
    if left is None:
        return "-"
    if left <= 0:
        return "now"
    minutes = int(left // 60)
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    parts = ([f"{days}d"] if days else []) + \
            ([f"{hours}h"] if days or hours else []) + [f"{minutes}m"]
    return "in " + " ".join(parts)


def _number(value) -> float:
    """Accepts DeepSeek's and Kimi's decimal-as-string numbers too."""
    return float(value)


def _percent(remaining, total) -> float | None:
    """A window as a percentage of itself, or None when either side is junk."""
    try:
        remaining, total = _number(remaining), _number(total)
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    return max(0.0, min(100.0, remaining / total * 100))


def api_key(provider: str, auth_path: Path | str = OPENCODE_AUTH):
    """``(key, source)`` — OpenCode's credential store first, then the named
    environment variable. On failure ``(None, reason)``.

    ONLY the source label may be shown; the key itself belongs to the one
    frame that puts it in an Authorization header. ``xai`` is stored as an
    OAuth grant, so the bearer is its ``access`` token rather than a ``key``.
    """
    entry_name, env = KEY_SOURCES[provider]
    try:
        data = json.loads(Path(auth_path).expanduser().read_text(encoding="utf-8"))
        entry = data.get(entry_name)
        value = (entry.get("key") or entry.get("access")) if isinstance(entry, dict) else None
        if isinstance(value, str) and value.strip():
            return value.strip(), f"opencode auth.json ({entry_name})"
    except (OSError, ValueError, TypeError):
        pass  # no OpenCode on this machine, or a file it rewrote mid-read
    value = os.environ.get(env, "").strip()
    if value:
        return value, f"${env}"
    return None, (f"no key for {entry_name} in opencode auth.json, and "
                  f"${env} is not set")


def _fetch(url: str, key: str | None = None, headers: dict | None = None,
           data: bytes | None = None):
    """(response headers, body bytes, error). The key lives in this frame only;
    error strings never include the response body, which can echo a key."""
    req = urllib.request.Request(url, data=data, headers={
        "Accept": "application/json",
        # grok.com answers 403 to the default Python-urllib agent.
        "User-Agent": "dromond/1",
        **({"Authorization": f"Bearer {key}"} if key else {}),
        **(headers or {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return ({k.lower(): v for k, v in resp.headers.items()},
                    resp.read(), None)
    except urllib.error.HTTPError as exc:
        return {}, b"", f"http {exc.code} {exc.reason}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {}, b"", f"unreachable: {getattr(exc, 'reason', exc)}"


def _get_json(url: str, provider: str, headers: dict | None = None,
              auth_path: Path | str = OPENCODE_AUTH):
    """(data, error), with the provider's key looked up for this one request."""
    key, source = api_key(provider, auth_path)
    if not key:
        return None, source
    _, body, err = _fetch(url, key, headers)
    if err:
        return None, err
    try:
        return json.loads(body or b"{}"), None
    except ValueError:
        return None, "response was not JSON"


# --- DeepSeek ---------------------------------------------------------------

@soft("deepseek")
def parse_deepseek(data: dict) -> Runway:
    infos = (data or {}).get("balance_infos") or []
    if not infos:
        return unknown("deepseek", "no balance_infos in response")
    info = next((i for i in infos if i.get("currency") == "USD"), infos[0])
    return Runway(
        "deepseek",
        remaining=_number(info["total_balance"]),
        unit=info.get("currency") or "?",
        raw=_scrub({
            "is_available": (data or {}).get("is_available"),
            "granted_balance": info.get("granted_balance"),
            "topped_up_balance": info.get("topped_up_balance"),
        }),
    )


@soft("deepseek")
def deepseek(auth_path: Path | str = OPENCODE_AUTH,
             url: str = DEEPSEEK_BALANCE_URL) -> Runway:
    """Prepaid balance — no window, so no resets_at. DeepSeek is the one
    provider billed per token, so its money IS its runway (W-0182)."""
    data, err = _get_json(url, "deepseek", auth_path=auth_path)
    return unknown("deepseek", err) if err else parse_deepseek(data)


# --- Kimi coding plan -------------------------------------------------------

# The window is described rather than named: {"duration": 300,
# "timeUnit": "TIME_UNIT_MINUTE"} is the same 5-hour window Claude reports.
_TIME_UNIT_MINUTES = {"TIME_UNIT_MINUTE": 1, "TIME_UNIT_HOUR": 60,
                      "TIME_UNIT_DAY": 1440, "TIME_UNIT_WEEK": 10080}


@soft("kimi")
def parse_kimi(data: dict) -> Runway:
    """``limits[]``: one entry per rolling window, each with a limit, what is
    left of it, and when it refills. Both sides arrive as decimal STRINGS.

    ``usage`` is a SECOND window, not a summary of the first (W-0184): on a
    Kimi For Coding plan ``limits[]`` carries only the 5-hour burst window,
    and the plan-wide quota the owner actually runs out of lives in ``usage``
    — as USED rather than remaining, and with its own reset. Reporting only
    ``limits`` hid a window that was already at zero (owner, 2026-08-14).

    ponytail: ``usage`` states no duration, so its length is assumed weekly —
    what the plan sells and what the owner calls it. If Kimi ever puts a
    ``window`` beside ``usage``, label it from that instead.
    """
    windows = []
    for entry in (data or {}).get("limits") or []:
        if not isinstance(entry, dict):
            continue
        window, detail = entry.get("window") or {}, entry.get("detail") or {}
        percent = _percent(detail.get("remaining"), detail.get("limit"))
        if percent is None:
            continue
        minutes = _TIME_UNIT_MINUTES.get(window.get("timeUnit"), 0) * \
            (window.get("duration") or 0)
        windows.append(make_window(window_label(minutes or None), percent,
                                   _iso(detail.get("resetTime"))))
    usage = (data or {}).get("usage") or {}
    if not any(w["label"] == "weekly" for w in windows):
        try:
            left = _number(usage["limit"]) - _number(usage["used"])
        except (KeyError, TypeError, ValueError):
            left = None
        percent = None if left is None else _percent(left, usage.get("limit"))
        if percent is not None:  # 0% is a reading, and the one that matters
            windows.append(make_window("weekly", percent,
                                       _iso(usage.get("resetTime"))))
    if not windows:
        return unknown("kimi", "no usable window in limits[] or usage")
    return from_windows("kimi", windows, raw=_scrub({
        "membership": ((data or {}).get("user") or {})
        .get("membership", {}).get("level"),
        "plan": (data or {}).get("subType"),
    }))


@soft("kimi")
def kimi(auth_path: Path | str = OPENCODE_AUTH, url: str = KIMI_USAGE_URL) -> Runway:
    """Coding-plan quota (Kimi For Coding), not the open-platform balance."""
    data, err = _get_json(url, "kimi", auth_path=auth_path)
    return unknown("kimi", err) if err else parse_kimi(data)


# --- MiniMax Token Plan -----------------------------------------------------

@soft("minimax")
def parse_minimax(data: dict) -> Runway:
    """``model_remains[]``: one row per model family, each carrying a rolling
    window and a weekly window as REMAINING PERCENTAGES with epoch-millis
    ends. The ``general`` row is the coding models; the other is video.

    ``base_resp.status_code`` is MiniMax's real error channel — a rejected
    request still answers 200.
    """
    status = ((data or {}).get("base_resp") or {}).get("status_code")
    if status not in (None, 0):
        return unknown("minimax", f"minimax refused the request (status {status})")
    rows = [r for r in ((data or {}).get("model_remains") or [])
            if isinstance(r, dict)]
    row = next((r for r in rows if r.get("model_name") == MINIMAX_MODEL), None) \
        or (rows[0] if rows else None)
    if row is None:
        return unknown("minimax", "no model_remains in response")
    windows = []
    for percent_key, start_key, end_key in (
            ("current_interval_remaining_percent", "start_time", "end_time"),
            ("current_weekly_remaining_percent", "weekly_start_time",
             "weekly_end_time")):
        percent = row.get(percent_key)
        if not isinstance(percent, (int, float)) or isinstance(percent, bool):
            continue
        minutes = None
        if isinstance(row.get(start_key), (int, float)) and \
                isinstance(row.get(end_key), (int, float)):
            minutes = round((row[end_key] - row[start_key]) / 60000)
        windows.append(make_window(window_label(minutes), float(percent),
                                   _iso(row.get(end_key))))
    if not windows:
        return unknown("minimax", "no remaining-percent field in model_remains")
    return from_windows("minimax", windows,
                        raw=_scrub({"model_name": row.get("model_name")}))


@soft("minimax")
def minimax(auth_path: Path | str = OPENCODE_AUTH,
            url: str = MINIMAX_USAGE_URL) -> Runway:
    data, err = _get_json(url, "minimax", auth_path=auth_path)
    return unknown("minimax", err) if err else parse_minimax(data)


# --- Grok / xAI -------------------------------------------------------------

# The grant OpenCode stores lives about two hours, so a strictly read-only
# adapter is blind by mid-afternoon. Dromond renews it — but it is writing into
# ANOTHER application's credential store, so the write is the careful part:
# temp file in the same directory, mode 0600, os.replace, every other
# provider's entry carried across untouched, and nothing written at all if any
# step fails. Renewal happens only in the last two minutes of the grant's life,
# so a five-minute poll touches the file about once every two hours.

def _write_auth(path: Path, data: dict) -> str | None:
    """Replace OpenCode's auth.json atomically, or change nothing. Returns an
    error string, never raises. The file is never truncated in place, so a
    crash mid-write leaves the previous credentials whole."""
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)  # atomic within the directory; keeps mode 0600
        tmp = None
        return None
    except (OSError, TypeError, ValueError) as exc:
        return f"the renewed grant could not be saved to {path}: {exc}"
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _xai_renew(path: Path, entry: dict) -> tuple[str | None, str | None]:
    """(bearer, error). Trade the refresh token for a new pair and hand it back
    to OpenCode.

    xAI CONSUMES the refresh token it is given, so the pair on disk is
    re-read after the exchange: if another process rotated it while this
    request was in flight, that one is the live grant and this one writes
    nothing rather than overwriting it with a pair nobody else knows about.
    """
    refresh = entry.get("refresh")
    if not isinstance(refresh, str) or not refresh:
        return None, "the OpenCode xai grant expired and has no refresh token"
    _, body, err = _fetch(XAI_TOKEN_URL, headers={
        "Content-Type": "application/x-www-form-urlencoded"},
        data=urllib.parse.urlencode({
            "grant_type": "refresh_token", "refresh_token": refresh,
            "client_id": XAI_CLIENT_ID}).encode("ascii"))
    if err:
        return None, f"renewing the xai grant failed ({err}); reconnect xAI in OpenCode"
    try:
        tokens = json.loads(body or b"{}")
        access, seconds = tokens["access_token"], tokens.get("expires_in")
    except (ValueError, KeyError, TypeError):
        return None, "renewing the xai grant returned no access token"
    if not isinstance(access, str) or not access:
        return None, "renewing the xai grant returned no access token"
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) \
            or seconds <= 0:
        seconds = 3600

    try:
        latest = json.loads(path.read_text(encoding="utf-8"))
        current = latest["xai"]
        if not isinstance(current, dict):
            raise ValueError("the xai entry is not an object")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return None, f"the grant was renewed but {path} could not be re-read: {exc}"
    if current.get("refresh") != refresh:  # somebody else rotated it first
        live = current.get("access")
        return (live, None) if isinstance(live, str) and live else \
            (None, "another process rotated the xai grant mid-request")
    latest["xai"] = {**current, "access": access,
                     "refresh": tokens.get("refresh_token") or refresh,
                     "expires": int((time.time() + seconds) * 1000)}
    err = _write_auth(path, latest)
    return (None, err) if err else (access, None)


def _xai_bearer(auth_path: Path | str = OPENCODE_AUTH,
                now: float | None = None) -> tuple[str | None, str | None]:
    """(bearer, error) for the Grok subscription hosts.

    ponytail: expiry is taken from OpenCode's own ``expires`` rather than
    decoded out of the JWT. A wrong one only means renewing late, which reads
    as an http 401 and an honest unknown. Decode the ``exp`` claim if that
    ever happens in practice.
    """
    path = Path(auth_path).expanduser()
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))["xai"]
        access = entry["access"]
    except (OSError, ValueError, KeyError, TypeError):
        entry, access = None, None
    if not isinstance(entry, dict) or not isinstance(access, str) or not access:
        key, source = api_key("xai", auth_path)  # the environment fallback
        return (key, None) if key else (None, source)
    expires = entry.get("expires")
    now = time.time() if now is None else now
    if isinstance(expires, (int, float)) and not isinstance(expires, bool) \
            and expires / 1000 > now + XAI_REFRESH_SKEW:
        return access, None
    return _xai_renew(path, entry)


def _varint(data: bytes, i: int) -> tuple[int, int]:
    """Protobuf base-128: seven bits a byte, low group first, high bit set on
    every byte but the last."""
    value = shift = 0
    while i < len(data):
        byte = data[i]
        i, value, shift = i + 1, value | (byte & 0x7F) << shift, shift + 7
        if not byte & 0x80:
            return value, i
        if shift > 63:
            raise ValueError("oversized protobuf varint")
    raise ValueError("truncated protobuf varint")


def _proto_fields(data: bytes):
    """(field number, wire type, value) for one protobuf message.

    No schema and no protobuf library — the wire format is self-describing
    enough to walk, because every field states its wire type and every wire
    type states its own length. Unknown fields are yielded and ignored by the
    caller, which is exactly how a protobuf reader is meant to behave.
    """
    i = 0
    while i < len(data):
        key, i = _varint(data, i)
        number, wire = key >> 3, key & 7
        if wire == 0:  # varint
            value, i = _varint(data, i)
        elif wire == 2:  # length-delimited: bytes, string, submessage
            length, i = _varint(data, i)
            value, i = data[i:i + length], i + length
            if len(value) != length:
                raise ValueError("truncated length-delimited protobuf field")
        elif wire in (1, 5):  # fixed64, fixed32
            width = 8 if wire == 1 else 4
            value, i = data[i:i + width], i + width
            if len(value) != width:
                raise ValueError("truncated fixed-width protobuf field")
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        yield number, wire, value


def _grpc_web_messages(body: bytes, headers: dict) -> list[bytes]:
    """Unwrap gRPC-Web frames: one flag byte, four big-endian length bytes,
    then the payload. Flag 0x80 marks the trailer frame, which is HTTP-header
    text carrying ``grpc-status`` — the real error channel, since a failed RPC
    still answers HTTP 200. Only the status number is read; the message may
    quote the request.
    """
    messages, status, i = [], headers.get("grpc-status"), 0
    while i + 5 <= len(body):
        flags, length = body[i], int.from_bytes(body[i + 1:i + 5], "big")
        i += 5
        payload, i = body[i:i + length], i + length
        if len(payload) != length:
            raise ValueError("truncated gRPC-Web frame")
        if flags & 0x80:
            for line in payload.decode("utf-8", "replace").splitlines():
                name, sep, value = line.partition(":")
                if sep and name.strip().lower() == "grpc-status":
                    status = value.strip()
        elif flags & 0x01:
            raise ValueError("compressed gRPC-Web frames are not supported")
        else:
            messages.append(payload)
    if i != len(body):
        raise ValueError("trailing bytes after the last gRPC-Web frame")
    if status not in (None, "", "0"):
        raise ValueError(f"the grok reset rpc failed with grpc status {status}")
    if not messages:  # zero credits is an EMPTY message, not a missing one
        raise ValueError("the grok reset rpc returned no protobuf message")
    return messages


def _proto_timestamp(data: bytes):
    """google.protobuf.Timestamp — field 1 is seconds. Nanoseconds (field 2)
    are ignored: an expiry a month out does not need them."""
    seconds = next((v for n, w, v in _proto_fields(data) if n == 1 and w == 0),
                   None)
    try:
        return None if not seconds else datetime.fromtimestamp(seconds, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def parse_xai_resets(body: bytes, headers: dict | None = None,
                     now: datetime | None = None) -> dict:
    """Banked usage resets: repeated field 10, each one a credit whose field 30
    is when it expires. A credit ALSO carries a redeemable id, which this
    deliberately never reads — a count and an expiry are the whole story a
    dashboard needs, and an id that is never parsed cannot leak.

    Returns ``{"available": n, "soonest_expiry": iso|None}``. Already-expired
    credits are not spendable and are not counted.
    """
    now = now or datetime.now(timezone.utc)
    expiries = []
    for message in _grpc_web_messages(body, headers or {}):
        for number, wire, value in _proto_fields(message):
            if number != 10 or wire != 2:
                continue
            when = next((_proto_timestamp(v) for n, w, v
                         in _proto_fields(bytes(value)) if n == 30 and w == 2),
                        None)
            if when and when > now:
                expiries.append(when)
    return {"available": len(expiries),
            "soonest_expiry": _iso(min(expiries).timestamp()) if expiries else None}


@soft("xai")
def parse_xai(data: dict, resets: dict | None = None) -> Runway:
    """``config.currentPeriod`` is the subscription window — weekly on a
    SuperGrok plan — and ``creditUsagePercent`` is the percent USED of it.

    The body is proto3 JSON, which OMITS zero-valued scalars: no
    ``creditUsagePercent`` means nothing has been spent this period, not that
    the field went missing. The period itself has to be there, though —
    without it there is no window and no reading.
    """
    config = (data or {}).get("config")
    period = config.get("currentPeriod") if isinstance(config, dict) else None
    if not isinstance(period, dict):
        return unknown("xai", "no currentPeriod in the grok billing response")
    used = config.get("creditUsagePercent")
    used = float(used) if isinstance(used, (int, float)) \
        and not isinstance(used, bool) else 0.0
    starts, ends = _iso(period.get("start")), _iso(period.get("end"))
    minutes = None
    if starts and ends:
        minutes = round((_seconds_until(ends) - _seconds_until(starts)) / 60)
    window = make_window(window_label(minutes), max(0.0, 100.0 - used), ends)
    raw = {"period": period.get("type"), "starts_at": starts}
    if resets is not None:
        raw["credits"] = _reset_credits_text(resets)
    return from_windows("xai", [window], raw=_scrub(raw))


def _reset_credits_text(resets: dict) -> str:
    """The credits block, phrased as resets: a count plus when the next one
    lapses, because a banked reset the owner never spends is one they lose."""
    count = resets.get("available") or 0
    text = f"{count} banked reset credit" + ("" if count == 1 else "s")
    return f"{text} · soonest expires {resets['soonest_expiry'][:10]}" \
        if resets.get("soonest_expiry") else text


@soft("xai")
def xai(auth_path: Path | str = OPENCODE_AUTH) -> Runway:
    """The SuperGrok subscription window, plus whatever usage resets are banked.

    Three calls: the user id, the billing window (which needs that id in a
    header), and the reset rpc. The reset rpc is a nice-to-have — if it fails
    the window still reports, because a missing extra must not cost the number
    the card exists for.
    """
    token, err = _xai_bearer(auth_path)
    if err:
        return unknown("xai", err)
    user, err = _fetch(XAI_USER_URL, token)[1:]
    if err:
        return unknown("xai", err)
    try:
        user_id = json.loads(user or b"{}")["userId"]
    except (ValueError, KeyError, TypeError):
        return unknown("xai", "grok did not recognise the credential")
    if not isinstance(user_id, str) or not user_id.strip():
        return unknown("xai", "grok did not recognise the credential")
    _, body, err = _fetch(XAI_BILLING_URL, token, {"x-userid": user_id})
    if err:
        return unknown("xai", err)
    try:
        billing = json.loads(body or b"{}")
    except ValueError:
        return unknown("xai", "the grok billing response was not JSON")
    return parse_xai(billing, _xai_resets(token))


def _xai_resets(token: str) -> dict | None:
    """An EMPTY protobuf message in a five-byte gRPC-Web frame — no fields, so
    no bytes — asking what usage resets are banked. None when the rpc did not
    answer usably, which the card simply omits."""
    headers, body, err = _fetch(XAI_RESETS_URL, token, {
        "Accept": "application/grpc-web+proto",
        "Content-Type": "application/grpc-web+proto",
        "Origin": "https://grok.com",
        "X-Grpc-Web": "1"}, data=b"\x00\x00\x00\x00\x00")
    if err:
        return None
    try:
        return parse_xai_resets(body, headers)
    except (ValueError, TypeError, UnicodeDecodeError):
        return None  # shape drift in an extra never costs the window


# --- Claude -----------------------------------------------------------------

@soft("claude")
def parse_claude(data: dict, now_ms: float | None = None) -> Runway:
    """``cachedUsageUtilization``: percent USED per window, plus resets_at.

    BOTH windows are reported (W-0179). A 5-hour limit and a weekly limit are
    two separate facts about the account; returning only the tighter one hid
    the other. The cache lags — hours, sometimes a day — so an old reading is
    flagged stale rather than suppressed.

    ponytail: Claude Code owns ~/.claude.json and may change this key without
    notice — shape drift degrades to unknown, it never guesses.
    """
    cache = (data or {}).get("cachedUsageUtilization")
    if not isinstance(cache, dict):
        return unknown("claude", "no cachedUsageUtilization in ~/.claude.json")
    fetched = cache.get("fetchedAtMs")
    if not isinstance(fetched, (int, float)):
        return unknown("claude", "cachedUsageUtilization has no fetchedAtMs")
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    age_h = max(0.0, (now_ms - fetched) / 3600000)
    as_of = _iso(fetched)

    windows = []
    for name, minutes in (("five_hour", 300), ("seven_day", 10080)):
        w = (cache.get("utilization") or {}).get(name)
        if not isinstance(w, dict) or not isinstance(w.get("utilization"), (int, float)):
            continue
        windows.append(make_window(window_label(minutes),
                                   100 - float(w["utilization"]),
                                   _iso(w.get("resets_at"))))
    if not windows:
        return unknown("claude", f"no utilization window in the cache "
                                 f"(as of {as_of}, {age_text(age_h)} ago)")
    return from_windows("claude", windows, as_of=as_of,
                        raw=_scrub({"as_of_age_h": round(age_h, 1)}))


@soft("claude")
def claude(path: Path | str = CLAUDE_CACHE, now_ms: float | None = None) -> Runway:
    p = Path(path).expanduser()
    if not p.is_file():
        return unknown("claude", f"{p} not found")
    return parse_claude(json.loads(p.read_text(encoding="utf-8")), now_ms)


# --- Codex ------------------------------------------------------------------

@soft("codex")
def parse_codex(lines) -> Runway:
    """Last ``token_count`` event of a rollout JSONL: rate_limits from the
    live response headers.

    ``primary`` and ``secondary`` are both reported when present (W-0179);
    on a weekly-only account secondary is null and one window is the whole
    truth. Each is named by its ``window_minutes`` — 10080 reads "weekly".

    ponytail: codex-cli owns ~/.codex/sessions and may change this format
    without notice — shape drift degrades to unknown.
    """
    last = None
    for line in lines:
        if '"token_count"' not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        payload = event.get("payload") or {}
        if payload.get("type") == "token_count" and payload.get("rate_limits"):
            last = event
    if last is None:
        return unknown("codex", "no token_count event with rate_limits")

    limits = last["payload"]["rate_limits"]
    as_of = _iso(last.get("timestamp"))
    windows = []
    for slot in ("primary", "secondary"):
        w = limits.get(slot)
        if not isinstance(w, dict) or not isinstance(
                w.get("used_percent"), (int, float)):
            continue
        windows.append(make_window(window_label(w.get("window_minutes")),
                                   100 - float(w["used_percent"]),
                                   _iso(w.get("resets_at"))))
    if not windows:
        return unknown("codex", "rate_limits has no window with used_percent")
    return from_windows("codex", windows, as_of=as_of, raw=_scrub({
        "plan_type": limits.get("plan_type"),
        "rate_limit_reached_type": limits.get("rate_limit_reached_type"),
        "credits": _codex_credits_text(limits.get("credits")),
    }))


def _codex_credits_text(credits) -> str | None:
    """``credits`` on a Codex plan is banked RESETS, not money — which is why
    a plan provider may show it (W-0184). ZERO is the reading the owner asked
    for: "no resets banked" is a fact, and an absent line is not."""
    if not isinstance(credits, dict):
        return None
    if credits.get("unlimited"):
        return "unlimited resets"
    try:
        count = int(float(credits["balance"]))
    except (KeyError, TypeError, ValueError):
        return None
    return f"{count} banked reset" + ("" if count == 1 else "s")


def parse_codex_live(result: dict) -> Runway:
    """``account/rateLimits/read`` from the Codex app server: usedPercent per
    window, right now. The ``rateLimits`` block is the account's own limit;
    ``rateLimitsByLimitId`` carries per-model ones that are not the account's
    headroom, so only the former is read."""
    limits = (result or {}).get("rateLimits")
    if not isinstance(limits, dict):
        return unknown("codex", "app server returned no rateLimits")
    windows = []
    for slot in ("primary", "secondary"):
        w = limits.get(slot)
        if not isinstance(w, dict) or not isinstance(
                w.get("usedPercent"), (int, float)):
            continue
        windows.append(make_window(
            window_label(w.get("windowDurationMins")),
            100 - float(w["usedPercent"]),
            _iso((w.get("resetsAt") or 0) * 1000 if w.get("resetsAt") else None)))
    if not windows:
        return unknown("codex", "no usable window in rateLimits")
    return from_windows("codex", windows, as_of=db.now(), raw=_scrub({
        "plan_type": limits.get("planType"),
        "rate_limit_reached_type": limits.get("rateLimitReachedType"),
        "credits": _codex_credits_text(limits.get("credits")),
    }))


def read_codex_app_server(timeout: float = 20.0) -> dict:
    """Ask codex-cli for the account's limits over its stdio app server.

    The session files this used to scrape are a RECORD, not a reading: the
    newest one is as old as the last time Codex happened to write a
    token_count event. That was 23 hours stale and showed 100% headroom while
    the account was actually at 11% -- a number that decides whether to
    dispatch. Asking is the only way to know.
    """
    exe = shutil.which("codex")
    if not exe:
        raise RuntimeError("codex is not installed")
    proc = subprocess.Popen([exe, "app-server", "--stdio"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, bufsize=1)
    sel = None
    try:
        for message in ({"id": 1, "method": "initialize", "params": {
                            "clientInfo": {"name": "dromond", "version": "1"},
                            "capabilities": {"experimentalApi": True}}},
                        {"method": "initialized"},
                        {"id": 2, "method": "account/rateLimits/read"}):
            proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        proc.stdin.flush()
        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not sel.select(max(0.0, deadline - time.monotonic())):
                break
            line = proc.stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("id") != 2:
                continue
            if isinstance(message.get("error"), dict):
                raise RuntimeError("codex refused the quota request")
            if isinstance(message.get("result"), dict):
                return message["result"]
            raise RuntimeError("codex returned an unexpected response")
        raise RuntimeError("codex quota request timed out")
    finally:
        if sel is not None:
            sel.close()
        for stream in (proc.stdin, proc.stdout):
            if stream is not None:
                stream.close()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()


@soft("codex")
def codex(sessions_dir: Path | str = CODEX_SESSIONS, reader=None) -> Runway:
    """Ask the app server; fall back to the session files it used to scrape.

    The fallback is not equivalent and says so -- it is a recording, and its
    age rides on the entry -- but a machine without codex-cli on PATH should
    still see whatever was last written rather than nothing.
    """
    try:
        live = parse_codex_live((reader or read_codex_app_server)())
        if live.known:
            return live
        why = live.reason or "app server gave no reading"
    except Exception as exc:
        why = str(exc)
    fallback = _codex_from_sessions(sessions_dir)
    if fallback.known:
        return fallback
    return unknown("codex", f"{why}; and no usable session snapshot")


def _codex_from_sessions(sessions_dir: Path | str = CODEX_SESSIONS) -> Runway:
    d = Path(sessions_dir).expanduser()
    files = sorted(d.glob("**/rollout-*.jsonl"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return unknown("codex", f"no rollout-*.jsonl under {d}")
    result = unknown("codex", "no token_count event with rate_limits")
    for path in files[:CODEX_SCAN_FILES]:
        with path.open(encoding="utf-8", errors="replace") as fh:
            result = parse_codex(fh)
        if result.known:
            return result
    return result  # newest files carried no usable snapshot; last reason wins


# --- polling and storage ----------------------------------------------------

ADAPTERS = (claude, codex, deepseek, kimi, minimax, xai)


def skipped(cfg: dict | None) -> set:
    """Providers the owner has no plan with, from ``[runway] skip``.

    A provider whose subscription has lapsed refuses every request forever, and
    an adapter cannot tell that apart from an outage — so it reports the
    provider's own error code, and both surfaces show an orange "Not reported"
    that reads as a fault. Saying so in config turns a permanent false alarm
    into a fact, and stops polling something that cannot answer.
    """
    names = ((cfg or {}).get("runway") or {}).get("skip") or []
    return {str(n).strip().lower() for n in names if str(n).strip()}


def poll_all(cfg: dict | None = None) -> list[Runway]:
    """Four of the six adapters are network-bound and independent, so they run
    together: polled in series a refresh costs the SUM of the timeouts, which
    is what a human waits behind on the dashboard's refresh button. Every
    adapter is already @soft, so a worker thread cannot raise out of the pool.

    A skipped provider is still REPORTED, as unknown with a reason. Dropping
    the row entirely would make a lapsed plan look like a provider that never
    existed, and the owner would have no way to see what they turned off.
    """
    off = skipped(cfg)
    live = [a for a in ADAPTERS if a.__name__ not in off]
    with ThreadPoolExecutor(max_workers=max(1, len(live))) as pool:
        results = list(pool.map(lambda adapter: adapter(), live))
    results += [unknown(name, "no plan configured (skipped in [runway] skip)")
                for name in sorted(off) if name in {a.__name__ for a in ADAPTERS}]
    return sorted(results, key=lambda r: r.provider)


def record(con, results) -> None:
    """Append one row per poll (schema v4) so a later view can show pace and
    time-to-reset. Unknowns are stored too — the gap is data.

    ponytail: ``limit_value`` is left unwritten, not dropped (W-0182 removed
    the field — a window's limit is always 100% of itself). Reclaim the
    column the next time this table needs a migration for another reason.
    """
    con.executemany(
        "INSERT INTO runway_polls(provider, remaining, unit, "
        "resets_at, as_of, reason, raw, windows, polled_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        [(r.provider, r.remaining, r.unit, r.resets_at, r.as_of,
          r.reason, json.dumps(_scrub(r.raw)), json.dumps(r.windows), db.now())
         for r in results])
    con.commit()


def _amount(remaining: float, unit: str) -> str:
    """A percentage is headroom; anything else is an account balance."""
    return f"{remaining:.0f}% left" if unit == "percent" \
        else f"{remaining:g} {unit}".strip()


def credits_text(raw) -> str | None:
    """The credits block's line, whatever a provider banks there: DeepSeek's
    money, Codex's banked resets, Grok's banked reset credits (W-0184). Every
    adapter that has one writes it as a finished phrase, so no surface has to
    know which provider means what."""
    text = raw.get("credits") if isinstance(raw, dict) else None
    return text if isinstance(text, str) and text else None


def latest_polls(con) -> list[dict]:
    """Newest poll per provider (DESIGN §11). Unknowns included: the gap is
    data, and unknown never blocks anything."""
    return [dict(r) for r in con.execute(
        "SELECT provider, remaining, limit_value, unit, resets_at, reason, "
        "as_of, MAX(id) AS id FROM runway_polls GROUP BY provider ORDER BY provider")]


def entry_text(entry: dict) -> str:
    """One poll ROW (a database dict, not a ``Runway``) as one readable
    phrase. Shared by every packet a model reads, so the conductor's runway
    block and the staffing turn's per-profile line say the same thing."""
    if entry.get("remaining") is None:
        return f"unknown ({entry.get('reason') or 'no reading'})"
    body = _amount(entry["remaining"], entry.get("unit") or "")
    body += f", resets {until_text(entry.get('resets_at'))}"
    age = age_hours(entry.get("as_of"))
    if age is not None and age >= STALE_AFTER_H:
        body += f" (stale: as of {age_text(age)} ago)"
    return body


def format_lines(r: Runway) -> list[str]:
    """One line PER WINDOW (W-0179), not one per provider: Claude's 5-hour
    and weekly limits are two rows under one provider name. Banked credits
    ride on the first row, where a note column already existed."""
    if not r.known:
        return [f"{r.provider:<10} {'-':<8} {'unknown':<14} {'-':<14} {r.reason}"]
    rows = r.windows or [{"label": "balance", "remaining": r.remaining,
                          "unit": r.unit, "resets_at": r.resets_at}]
    note = credits_text(r.raw)
    return [f"{r.provider:<10} {w['label']:<8} "
            f"{_amount(w['remaining'], w['unit']):<14} "
            f"{until_text(w['resets_at']):<14} "
            f"{note if i == 0 and note else ''}".rstrip()
            for i, w in enumerate(rows)]
