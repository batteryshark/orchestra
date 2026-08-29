"""TOML config: [settings], [worker_env], [profiles.NAME] launch profiles.

Merge order: built-in defaults <- ~/.config/orchestra/config.toml <-
that file's [project."<projectId>"] table (shallow, per table). Per-project
settings key on the central registry id — a local UUID, or the id a source
adapter cached. Settings live in Orchestra's config, never in the project
directory (DESIGN §2).

**Profiles are GLOBAL presets (W-0187).** A project does not change what a
profile IS; it chooses WHICH of them it may staff, with one key:

    [project."<projectId>"]
    enabled_profiles = ["sol-medium", "ds-flash"]

Absent means every profile is enabled — the honest default for an install
that already has ten of them. An explicit list means only those, and
``[project."<id>".profiles.NAME]`` override tables are gone: a config that
still carries one fails loudly (``LEGACY_PROJECT_PROFILES_ERROR``) rather
than reading as "no overrides".

Enablement binds when a run is staffed (``staff_profile`` below). A run already
in flight is not revalidated: ``profile_cfg`` is the unchecked read used by
relaunch and continuation paths.

Profiles are launch templates ONLY (DESIGN D4/D10): any number of
concurrent runs may share one, and nothing addresses a run by its profile
name. A profile may carry a freeform headroom ``note`` + ``note_at``
timestamp (D10) — routing intent for planners and humans, never injected
into worker briefs.
"""
import json
import tomllib
from pathlib import Path

from orchestra import paths

DEFAULT_RUN_TIMEOUT_SECONDS = 36000
DEFAULT_STALL_TIMEOUT_SECONDS = 1800

# --- routing metadata a planner reads (W-0181) ------------------------------
# ``tier`` is capability, ``priority`` is preference WITHIN a tier.
#
# The three tiers are Orchestra's, numbered rather than named so they sort:
# 1 workhorse (well-defined bounded tasks), 2 generalist, 3 heavy (the
# frontier model, for the hardest thinking).
TIERS = {1: "workhorse", 2: "generalist", 3: "heavy"}
# The names Orchestra accepted before the numbers, plus Orchestra's own. Read
# only: a legacy `tier = "cheap"` in a hand-written config keeps working, and
# the next write through ``profile_edit`` turns it into the number.
_TIER_ALIASES = {"workhorse": 1, "cheap": 1, "low": 1,
                 "generalist": 2, "mid": 2, "medium": 2,
                 "heavy": 3, "high": 3, "frontier": 3}
# Priority orders profiles of the same tier against each other, `nice`-style:
# 0-99, LOWER is more preferred. The default sits in the middle so a profile
# can be nudged either way without renumbering the others.
DEFAULT_PRIORITY = 50
PRIORITY_MIN, PRIORITY_MAX = 0, 99


def tier_of(value) -> int | None:
    """1, 2, 3 — or None when the value is absent or means nothing."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value in TIERS else None
    text = str(value).strip().lower()
    if text.isdigit():
        return int(text) if int(text) in TIERS else None
    return _TIER_ALIASES.get(text)


def priority_of(profile: dict) -> int:
    """A profile's priority, defaulted. Anything unreadable is the default."""
    try:
        value = int(profile.get("priority", DEFAULT_PRIORITY))
    except (TypeError, ValueError):
        return DEFAULT_PRIORITY
    return value if PRIORITY_MIN <= value <= PRIORITY_MAX else DEFAULT_PRIORITY


DEFAULT_CONFIG = f"""\
# Orchestra profiles + settings. The only file: ~/.config/orchestra/config.toml
#
# Per-project overrides go in a [project."<projectId>"] table in THIS file.
# Use the id shown by `orchestra project list`; Work-backed projects retain
# Work's immutable projectId. Settings tables have the same shape as the top
# level and merge over it:
#
#   [project."53efe3c3-6def-4797-8560-3dce073d7d63".settings]
#   timeout = 7200
#
# Profiles are GLOBAL presets. A project does not change what one contains;
# it chooses which of them it may staff a run with. Absent = all of them:
#
#   [project."53efe3c3-6def-4797-8560-3dce073d7d63"]
#   enabled_profiles = ["sol-medium", "ds-flash"]

[settings]
timeout = {DEFAULT_RUN_TIMEOUT_SECONDS}           # hard cap for a runaway worker (10 hours)
stall_timeout = {DEFAULT_STALL_TIMEOUT_SECONDS}   # kill after no worker output (30 minutes); 0 disables
default_requester = "human"
# Additional directories passed through the harness's native access flag
# (DESIGN §12). Permissions vary by harness and may include writes. Declare
# them explicitly, usually in a [project."<projectId>".settings] table. A
# profile may override the list. OpenCode ignores this setting with a warning.
# add_dirs = ["~/Projects/reference-repo"]
# Raw backend JSONL is the full-detail record of every run and is KEPT
# FOREVER by default (0). A positive day count lets `orchestra traces prune`
# age out TERMINAL runs' logs after that many days. Normalized trace events
# are kept indefinitely either way; pruning only loses expand-in-place detail.
raw_log_retention_days = 0
# One shell command fired after a run finishes — its result, landing, and
# handoff are durable first. A callback for external listeners; Orchestra
# does not care who. ORCHESTRA_RUN_ID and ORCHESTRA_RUN_STATUS are set in
# its environment. A missed callback must be covered by the listener's own
# fallback poll.
# on_run_finished = "/path/to/notify-listener"
# Optional observer policy. There are no budgets and no run ceilings: a
# long run is a good run, and this is what catches a feral one instead.
# observer_profile picks the model that judges transcripts out of band;
# with none set, the one profile marked tier = 1 (workhorse) is used, and with
# neither the observer says so and stays off. Set it per project in a
# [project."<projectId>".settings] table to give that project's goals their
# own observer.
# observer_profile = "cheap"
# observer_first_look = 1800   # first out-of-band look, in seconds
# observer_interval = 3600     # and hourly after that
# Mechanical loop detection, zero tokens: how many identical tool calls in a
# row, and how many consecutive edits to one file, count as spinning.
# loop_repeats = 6
# loop_file_repeats = 8
# Non-secret environment values applied to every worker process.
# {{root}} expands to the project root.
[worker_env]

# --- Optional Nod notification adapter -----------------------------------
# Escalations are delivered as Nod request cards. Off unless enabled.
# A Nod issuer token is scoped to exactly ONE channel, so Orchestra holds two
# (decisions, alerts). Neither token, the base url, nor the channel ids live
# in THIS file: they go in secrets_file with mode 0600, as
#   base_url=...            (may include a proxy path prefix, e.g. /boop)
#   decisions_channel=...   decisions_token=...
#   alerts_channel=...      alerts_token=...
# Any of those keys can be overridden per-run by ORCHESTRA_NOD_<KEY>, e.g.
# ORCHESTRA_NOD_ALERTS_TOKEN. Configure one channel and not the other and only
# that other channel reports as unconfigured.
[nod]
enabled = false
secrets_file = "~/.config/orchestra/nod-secrets.env"
timeout = 15
# Set once the daemon has a reachable callback route (W-0163). Nod's callback
# is an unsigned wake-up hint; the decision is always re-read over the API.
callback_url = ""
# Default life of a decision card, in seconds; 0 means it never expires.
expires_after = 86400

# --- profiles -------------------------------------------------------------
# Each entry is a reusable launch template, never a worker identity (D4).
# The `backend` KEY names the HARNESS that runs the model — the word every
# surface says now; the key name stays `backend` so existing profiles keep
# working.
# backend: opencode | codex | claude | reasonix  (the harness)
# model:   harness-specific model id
# effort:  codex/claude/reasonix reasoning effort; opencode uses `variant` instead
# sandbox: codex execution sandbox (default: workspace-write)
# tier:    1 workhorse (well-defined bounded tasks) | 2 generalist |
#          3 heavy (frontier model, hardest thinking). Routing metadata a
#          planner reads. tier = 1 also volunteers a profile as the spin
#          observer when settings.observer_profile is unset.
# priority: 0-99, like a linux process `nice` value — LOWER is more preferred.
#          Orders profiles of the SAME tier against each other. Default 50.
# transport: "exec" (default, all supported harnesses) or "acp" — one persistent
#          Agent Client Protocol peer instead of one process per turn. Only
#          opencode and reasonix speak it. It buys mid-turn `tell` (Reasonix
#          steer), graceful session/cancel instead of kill-and-resume, and
#          permission requests as protocol messages. There is no fallback:
#          a failed ACP handshake fails the run.
# acp_permission: "allow" (default) or "deny" — how an ACP run answers
#          session/request_permission. Ignored by the exec transport.
# timeout / stall_timeout: per-profile overrides, in seconds
# extra_args: appended to the backend CLI invocation
# lane:    claude only. "quota" unsets ANTHROPIC_API_KEY so `-p` uses the
#          Max subscription. "api" keeps the key. Spent quota retries once
#          on the api lane when a key is present. The trace names the lane.
# env:     optional table of string keys to string values for THIS profile.
#          Wins over [worker_env] and the inherited process environment.
#          Unnamed variables stay put. Point a harness at a local endpoint:
#            env = {{ ANTHROPIC_BASE_URL = "http://127.0.0.1:8080" }}
# note / note_at: freeform headroom note + when it was written (D10);
#                 `orchestra profiles note NAME "..."` sets both

"""

LEGACY_AGENTS_ERROR = """\
orchestra: {path} still uses the legacy [agents.NAME] config table.
Rename every [agents.NAME] table to [profiles.NAME] (DESIGN D10 — profiles,
not a roster), including inside any [project."<projectId>"] table."""

LEGACY_PROJECT_PROFILES_ERROR = """\
orchestra: {path} still carries per-project profile overrides:
{tables}
Profiles are GLOBAL presets now (W-0187) — a project chooses WHICH profiles
it may staff a run with, never what one of them contains. Fold each override
into its own top-level [profiles.NAME] table (add a second global profile if
two projects really wanted different values), then say which ones the
project enables:

  [project."{first}"]
  enabled_profiles = ["name-a", "name-b"]

Leave enabled_profiles out entirely and every profile is enabled."""

ENABLED_TYPE_ERROR = """\
orchestra: enabled_profiles in [project."{project_id}"] ({path}) must be a list
of profile names, e.g. enabled_profiles = ["sol-medium", "ds-flash"].
Leave it out entirely and every profile is enabled."""


def _load_toml(p: Path) -> dict:
    if not p.is_file():
        return {}
    with open(p, "rb") as f:
        return tomllib.load(f)


def check(text: str) -> dict:
    """Parse TOML and refuse legacy shapes. Raises ValueError, never SystemExit."""
    try:
        top = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"not valid TOML: {exc}") from exc
    try:
        _reject_legacy(paths.global_config_path(), top)
    except SystemExit as exc:
        raise ValueError(str(exc)) from exc
    return top


def _reject_legacy(path: Path, top: dict) -> None:
    """Loud failure for a config shape that no longer means what it says.

    Both checks scan EVERY project table, not just the one being loaded: a
    stale copy of the config must not read as "no overrides" merely because
    the caller happened to ask for a different project.
    """
    projects = top.get("project") or {}
    tables = [t for t in [top] + [v for v in projects.values()
                                  if isinstance(v, dict)] if t.get("agents")]
    if tables:
        raise SystemExit(LEGACY_AGENTS_ERROR.format(path=path))
    stale = [f'  [project."{pid}".profiles.{name}]'
             for pid, table in projects.items() if isinstance(table, dict)
             for name in (table.get("profiles") or {})]
    if stale:
        raise SystemExit(LEGACY_PROJECT_PROFILES_ERROR.format(
            path=path, tables="\n".join(sorted(stale)),
            first=sorted(pid for pid, t in projects.items()
                         if isinstance(t, dict) and t.get("profiles"))[0]))


def _enabled_list(path: Path, project_id: str | None, table: dict):
    """The project's enabled set as written, or None for "all of them"."""
    listed = table.get("enabled_profiles")
    if listed is None:
        return None
    if not isinstance(listed, list) or not all(isinstance(n, str) for n in listed):
        raise SystemExit(ENABLED_TYPE_ERROR.format(project_id=project_id,
                                                   path=path))
    return [n.strip() for n in listed if n.strip()]


def load(project_id: str | None = None) -> dict:
    """Merged config: defaults <- global file <- its [project."<id>"] table.

    Profiles come from the global file ALONE (W-0187). What the project table
    contributes is ``enabled_profiles``: the names it may staff, carried on
    the returned config as ``enabled_profiles`` (None = every profile) beside
    the ``project_id`` it was loaded for, so a refusal can name both.
    """
    cfg = tomllib.loads(DEFAULT_CONFIG)
    path = paths.global_config_path()
    top = _load_toml(path)
    _reject_legacy(path, top)
    per_project = (top.get("project") or {}).get(project_id or "")
    per_project = per_project if isinstance(per_project, dict) else {}
    cfg["project_id"] = project_id or None
    cfg["enabled_profiles"] = _enabled_list(path, project_id, per_project)
    for overlay in (top, per_project):
        cfg.setdefault("settings", {}).update(overlay.get("settings", {}))
        cfg.setdefault("nod", {}).update(overlay.get("nod", {}))
        # [merge] is the verification gate (DESIGN §9): base branch, declared
        # checks, tripwire limits. Without this line merge_cfg only ever saw
        # its defaults, so a configured `test` check never ran — for the
        # supervisor OR for `orchestra merge`.
        cfg.setdefault("merge", {}).update(overlay.get("merge", {}))
        # [http] trust_local = true makes an unauthenticated request FROM THIS MACHINE
# the human, so a browser on localhost never pastes the key. Off by default:
# workers run on this machine too, and loopback cannot tell them apart, so
# turning it on hands every run the authority per-run tokens exist to withhold.
# [http] holds the dashboard/API shared secret, port, bind and host
        # allowlist (DESIGN §3). `orchestra init` writes the table; there is no
        # default, because a default secret is not a secret.
        cfg.setdefault("http", {}).update(overlay.get("http", {}))
        # [runway] names providers with no active plan. Every table has to be
        # listed here to survive the load -- the same omission once meant a
        # configured [merge] check never ran.
        cfg.setdefault("runway", {}).update(overlay.get("runway", {}))
        worker_env = overlay.get("worker_env", {})
        if not isinstance(worker_env, dict):
            raise SystemExit(f"orchestra: [worker_env] in {path} must be a TOML table")
        cfg.setdefault("worker_env", {}).update(worker_env)
    # Profiles are global: only the top-level file contributes them. A
    # per-project [profiles.NAME] table cannot reach here — _reject_legacy
    # refused the whole load before this point.
    for name, profile in top.get("profiles", {}).items():
        cfg.setdefault("profiles", {}).setdefault(name, {}).update(profile)
    for name, entry in load_profile_notes().items():
        if name in cfg.get("profiles", {}) and entry.get("note"):
            cfg["profiles"][name]["note"] = entry["note"]
            cfg["profiles"][name]["note_at"] = entry.get("note_at")
    return cfg


# --- the enabled set (W-0187) ------------------------------------------------
# A project's ``enabled_profiles`` is a FILTER over the global profiles. It
# binds when staffing a run. Relaunches, continuations, and ACP transport
# lookups read through ``profile_cfg``, unchecked, because a run in flight
# keeps the preset it launched with even when that preset has since been
# disabled.

def enabled_profiles(cfg: dict) -> dict:
    """The profiles this project may staff, ``name -> table``.

    No ``enabled_profiles`` key means every configured profile, which is what
    an install with ten of them and no project tables already has.
    """
    listed = cfg.get("enabled_profiles")
    profiles = cfg.get("profiles", {})
    if listed is None:
        return dict(profiles)
    return {name: p for name, p in profiles.items() if name in listed}


def is_enabled(cfg: dict, name: str) -> bool:
    listed = cfg.get("enabled_profiles")
    return listed is None or name in listed


def not_enabled_error(cfg: dict, name: str) -> str:
    """The refusal. It names the project and the enabled set, because the fix
    is one of two edits and the reader has to be able to pick which."""
    enabled = sorted(enabled_profiles(cfg))
    project_id = cfg.get("project_id") or "(no project)"
    return (
        f"orchestra: project {project_id} has not enabled profile '{name}'.\n"
        f"Enabled there: {', '.join(enabled) or 'none'}.\n"
        f'Either add "{name}" to enabled_profiles in [project."{project_id}"] '
        f"in {paths.global_config_path()}, or staff one of the enabled "
        "profiles instead. Removing enabled_profiles enables every profile.")


def staff_profile(cfg: dict, name: str) -> dict:
    """``profile_cfg``, gated by the project's enabled set. STAFFING ONLY.

    Call this where a run is being STAFFED — the sweeper, ``orchestra
    dispatch``, the conductor's dispatch action, the observer and planner
    picking their own model. Never on a relaunch or a continuation: those
    carry a preset the project once enabled, and revalidating mid-run is the
    one thing W-0187 rules out.
    """
    if not is_enabled(cfg, name):
        raise SystemExit(not_enabled_error(cfg, name))
    return profile_cfg(cfg, name)


def profile_cfg(cfg: dict, name: str) -> dict:
    """Resolve one launch template by name. NOT gated by the enabled set —
    see ``staff_profile`` for the staffing boundary."""
    profiles = cfg.get("profiles", {})
    if name not in profiles:
        if not profiles:
            raise SystemExit(
                "orchestra: no profiles configured. A profile is backend + model +\n"
                "effort, picked from what the harnesses report — run\n"
                "`orchestra profiles discover` to see the real lists, then add a\n"
                f"[profiles.NAME] table to {paths.global_config_path()}"
            )
        raise SystemExit(
            f"orchestra: unknown profile '{name}'. Configured: {', '.join(sorted(profiles))}\n"
            f"Add it to {paths.global_config_path()}"
        )
    profile = dict(profiles[name])
    profile["name"] = name
    profile.setdefault("backend", "opencode")
    profile.setdefault("extra_args", [])
    # DESIGN §12: extra directories are declared, never discovered. Declared
    # per project in [project."<id>".settings], or on the profile itself.
    raw = profile.get("add_dirs", cfg.get("settings", {}).get("add_dirs", []))
    if not isinstance(raw, list) or not all(isinstance(d, str) and d for d in raw):
        raise SystemExit(
            f"orchestra: add_dirs must be a list of directory paths "
            f"(profile '{name}', {paths.global_config_path()})")
    profile["add_dirs"] = [str(Path(d).expanduser()) for d in raw]
    if "env" in profile:
        _check_env_table(profile["env"], kind="profile environment")
    return profile


# --- headroom notes (D10) ---------------------------------------------------
# Notes used to live in a JSON sidecar, because TOML has no stdlib writer.
# W-0173 gave the config file a targeted writer (``profile_edit``), so a note
# is now a key in the profile's own table like every other field. The sidecar
# is still READ, so a note written before the move is not lost, and a write
# through ``profile_edit`` drops that name's stale sidecar entry so the file
# stops being shadowed.

def profile_notes_path() -> Path:
    return paths.global_config_path().with_name("profile-notes.json")


def load_profile_notes() -> dict:
    try:
        data = json.loads(profile_notes_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def forget_profile_note(name: str) -> None:
    """Drop a legacy sidecar note so the config file wins for that profile."""
    notes = load_profile_notes()
    if notes.pop(name, None) is None:
        return
    profile_notes_path().write_text(json.dumps(notes, indent=2), encoding="utf-8")


def _check_env_table(values, *, kind: str) -> None:
    if not isinstance(values, dict):
        raise SystemExit(f"orchestra: {kind} must be a TOML table")
    for name, value in values.items():
        if not isinstance(name, str) or not name or "=" in name or "\0" in name:
            raise SystemExit(f"orchestra: invalid {kind} name {name!r}")
        if not isinstance(value, str) or "\0" in value:
            raise SystemExit(f"orchestra: {kind} value for {name} must be a string")


def apply_worker_env(cfg: dict, env: dict[str, str], root: Path) -> dict[str, str]:
    """Apply non-secret project values to every worker process."""
    values = cfg.get("worker_env", {})
    _check_env_table(values, kind="worker environment")
    updated = dict(env)
    for name, value in values.items():
        updated[name] = value.replace("{root}", str(root))
    return updated


def apply_profile_env(profile: dict, env: dict[str, str]) -> dict[str, str]:
    """Profile env wins over inherited env. Unnamed variables stay put."""
    values = profile.get("env")
    if values is None:
        return env
    _check_env_table(values, kind="profile environment")
    # ponytail: no {root} expansion; add if a profile env path needs the project root
    updated = dict(env)
    updated.update(values)
    return updated


def ensure_global_config() -> Path:
    p = paths.global_config_path()
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(DEFAULT_CONFIG, encoding="utf-8")
    return p
