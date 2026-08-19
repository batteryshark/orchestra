"""The spin observer and the retry rule (DESIGN §7, W-0166).

**There are no budgets and no run ceilings.** Runs go three hours and do
good work, so nothing here may punish a run for being long. What gets
stopped is a run that has gone *feral*. Three layers, cheapest first:

(a) **process stall detection** — already in ``supervise._run_proc``: a
    growing log is progress, silence past ``stall_timeout`` kills. Zero
    tokens, reused as-is.
(b) **mechanical loop detection** over the NORMALIZED events table: the same
    tool call repeated back to back, or the same file edited N times
    running. Zero tokens, and it reads ``events`` — never raw logs.
(c) a cheap **out-of-band observer turn** for long runs — first look at ~30
    minutes, then every half hour. It reads the transcript from OUTSIDE, in its own
    process, with its own cheap model. Nothing is injected into the run, so
    the worker never knows it happened and pays nothing for it. This is the
    whole difference from the periodic check-in Orchestra killed.

Exactly three outcomes: do nothing, ``tell`` a correction, or stop and
escalate. **A stop always escalates with its reasoning** — recorded in
``observations``, posted to the run's messages, and filed to Nod when the
human loop is on. Nothing here may silently kill a run.

Also on demand: ``dromond check <run>`` / ``POST /api/runs/N/check`` run the
same judgement immediately, through ``check()``.

**Retry**: an infrastructure-shaped terminal state is retried ONCE,
automatically, reusing the same brief. Two consecutive infrastructure
failures on the same item stop and escalate; nothing spends a third. A run
that FINISHED but produced bad work is a judgment failure, not an
infrastructure one: it goes to a planner turn, whose seam is
``planner_review`` at the bottom of this file.
"""
import json
import os
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from dromond import config, db, dispatch, nod, paths, runners, traces

# Layer (c) cadence: first look five minutes in, then every half hour (§7).
# A workhorse (tier 1) gets looked at much sooner: a heavy model thinking for
# half an hour is normal, a cheap one silent for half an hour is how run 24
# burned 21 minutes and 119k prompt tokens re-reading stale worktrees with
# nobody watching. Length is still never a fault on its own — this only
# changes WHEN the judgement happens, never what it judges.
# Five minutes to the first look, then every half hour. A run that is already
# lost is lost inside five minutes — that is when stopping it is still cheap —
# and after that a half-hourly glance is enough to catch a run that wanders
# later without pestering one that is simply working (owner, 2026-08-14).
FIRST_LOOK = 300
INTERVAL = 1800
POLL_EVERY = 30          # seconds between watcher passes; layer (b) is SQL only
TURN_TIMEOUT = 180       # a wedged observer must never outlive its usefulness

# ponytail: loop thresholds are a heuristic with a real false-positive cost
# (a `tell` the worker did not need), so they are calibration knobs, not
# constants — [settings] loop_repeats / loop_file_repeats override them.
# Raise them if honest work trips the detector; the ceiling is that neither
# rule understands intent, only shape.
TOOL_REPEATS = 6         # identical tool call, back to back
FILE_REPEATS = 8         # consecutive edits to one path with nothing between

DIGEST_EVENTS = 60       # transcript tail handed to the observer turn
DIGEST_CHARS = 400       # per event
MISSION_CHARS = 1200
MIN_EVENTS = 5           # below this there is no transcript worth paying for
OBSERVER_TIER = 1        # workhorse (W-0181); named "cheap" before the numbers

INFRA_TERMINAL = ("failed", "timeout")
# ponytail: DESIGN §7 also lists `killed` as infrastructure-shaped, but in
# this codebase NOTHING sets `killed` except a human (`dromond kill`,
# dashboard stop) and this module's own stop verdict. Auto-retrying a
# deliberate stop is worse than missing a retry, so `killed` is excluded
# until something mechanical can produce it.

_EDIT_TOOLS = {"edit", "write", "multiedit", "notebookedit", "str_replace_editor",
               "apply_patch", "patch", "file_change", "create_file", "write_file"}
_PATH_KEYS = ("file_path", "filepath", "path", "file", "target_file", "abs_path")


class ObserverUnconfigured(Exception):
    """No observer profile is configured. There are no default profiles."""


class ObserverTurnError(Exception):
    """The observer's own model call failed. Never the run's fault."""


# --- observations (schema v9) ----------------------------------------------

def record(con, run_id: int, layer: str, action: str, reason: str = "",
           detail=None) -> int:
    """Durable record of one judgement. Written BEFORE anything is acted on,
    so a stop's reasoning survives even if the kill races the writer."""
    if detail is not None and not isinstance(detail, str):
        detail = json.dumps(detail, ensure_ascii=False, default=str)
    return int(con.execute(
        "INSERT INTO observations(run_id, layer, action, reason, detail, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (run_id, layer, action, reason[:2000], detail, db.now())).lastrowid)


def observations(con, run_id: int, layer: str | None = None) -> list[dict]:
    sql = "SELECT * FROM observations WHERE run_id=?"
    args = [run_id]
    if layer:
        sql += " AND layer=?"
        args.append(layer)
    return [dict(r) for r in con.execute(sql + " ORDER BY id", args)]


def _last(con, run_id: int, layer: str, action: str | None = None):
    sql = "SELECT * FROM observations WHERE run_id=? AND layer=?"
    args = [run_id, layer]
    if action:
        sql += " AND action=?"
        args.append(action)
    return con.execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()


def _epoch(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


# --- the observer profile (per-goal configurable, cheap tier by default) ----

def profile_name(cfg: dict) -> str:
    """``[settings] observer_profile``, else the one profile marked cheap.

    Per-goal configurable: settings merge per project, so a
    ``[project."<id>".settings]`` table picks a different observer for that
    project's goals. ponytail: per-Work-item granularity has nowhere to live
    yet — add it when an item carries its own settings.

    There are no default profiles (DESIGN §5), so an unconfigured install is
    told exactly what to add rather than guessing a model.

    The tier scan runs over the profiles this project ENABLES (W-0187), so
    an observer is never volunteered out of a profile the project disabled.
    An ``observer_profile`` naming a disabled one is refused by
    ``observer_profile()`` below, with the message that names the project.
    """
    configured = cfg.get("profiles", {})
    profiles = config.enabled_profiles(cfg)
    named = str(cfg.get("settings", {}).get("observer_profile") or "").strip()
    if named:
        if named not in configured:
            raise ObserverUnconfigured(
                f"observer_profile '{named}' is not a configured profile "
                f"(configured: {', '.join(sorted(configured)) or 'none'})")
        return named
    cheap = sorted(name for name, p in profiles.items()
                   if config.tier_of(p.get("tier")) == OBSERVER_TIER)
    if len(cheap) == 1:
        return cheap[0]
    if not cheap:
        raise ObserverUnconfigured(
            "no observer profile: the spin observer needs a cheap model to "
            "judge transcripts with, and there are no default profiles. Set "
            '[settings] observer_profile = "NAME", or mark one profile '
            "tier = 1 (workhorse), in " + str(paths.global_config_path()))
    raise ObserverUnconfigured(
        f"several profiles are tier 1 (workhorse) ({', '.join(cheap)}) — set "
        '[settings] observer_profile = "NAME" to pick one')


def observer_profile(cfg: dict) -> dict:
    """Staffing (W-0187): the observer is a model turn the project pays for,
    so a disabled profile is refused here like any other."""
    try:
        return config.staff_profile(cfg, profile_name(cfg))
    except SystemExit as exc:
        raise ObserverUnconfigured(str(exc)) from exc


def status(cfg: dict | None = None) -> dict:
    """Can layer (c) actually run, and if not, what is the fix? (W-0189)

    An unconfigured observer used to fail silently INSIDE the watcher thread:
    nothing was looking at any run and no surface said so. This is the one
    answer `dromond doctor`, the daemon's startup line and daemon health all
    read, so "nothing is watching" can never again be invisible.

    Ambiguity is REPORTED, never guessed — two tier-1 profiles is a config
    the owner has to resolve, not something to pick for them.
    """
    cfg = config.load() if cfg is None else cfg
    try:
        profile = observer_profile(cfg)
    except ObserverUnconfigured as exc:
        return {"enabled": False, "profile": None, "problem": str(exc),
                "first_look": None, "interval": None}
    return {"enabled": True, "profile": profile["name"], "problem": None,
            "first_look": int(first_look(cfg)),
            "interval": int(interval(cfg))}


def status_report(cfg: dict | None = None) -> list[str]:
    """What `dromond doctor` and the daemon's startup line print."""
    state = status(cfg)
    if state["enabled"]:
        return [f"  observer: {state['profile']} — first look after "
                f"{int(state['first_look']) // 60}m, then every "
                f"{int(state.get('interval') or INTERVAL) // 60}m"]
    return ["  observer: DISABLED — no run is being watched for spin",
            f"            {state['problem']}"]


def first_look(cfg: dict) -> float:
    """Seconds before the FIRST observer turn on a run.

    The same for every run. It used to key on the OBSERVER's tier, which
    measured the wrong thing entirely — naming a stronger observer silently
    pushed the first look from five minutes back to thirty, so improving the
    judge made the watch worse.
    """
    setting = cfg.get("settings", {}).get("observer_first_look")
    return float(setting) if setting is not None else float(FIRST_LOOK)


def interval(cfg: dict) -> float:
    """Seconds between looks after the first."""
    setting = cfg.get("settings", {}).get("observer_interval")
    return float(setting) if setting is not None else float(INTERVAL)


# --- layer (b): mechanical loop detection over the normalized events --------

def _dig_path(value) -> str | None:
    if isinstance(value, dict):
        for key, inner in value.items():
            if str(key).lower() in _PATH_KEYS and isinstance(inner, str) and inner:
                return inner
        for inner in value.values():
            found = _dig_path(inner)
            if found:
                return found
    elif isinstance(value, list):
        for inner in value:
            found = _dig_path(inner)
            if found:
                return found
    return None


def edited_path(name: str | None, payload: str) -> str | None:
    """The file this tool call edits, or None when it is not an edit."""
    if not name or name.strip().lower().lstrip("_") not in _EDIT_TOOLS:
        return None
    try:
        obj = json.loads(payload)
    except (TypeError, ValueError):
        return None
    return _dig_path(obj)


def _tool_calls(con, run_id: int, limit: int) -> list:
    """The run's last ``limit`` tool calls, oldest first."""
    rows = list(con.execute(
        "SELECT seq, name, payload FROM events WHERE run_id=? AND kind='tool_call' "
        "ORDER BY seq DESC LIMIT ?", (run_id, limit)))
    return rows[::-1]


def loop_reason(con, run_id: int, *, tool_repeats: int = TOOL_REPEATS,
                file_repeats: int = FILE_REPEATS) -> tuple[str, int] | None:
    """(reason, last_seq) when the tail of the trace is spinning, else None.

    Two shapes, both read from ``events`` — the raw log is never re-parsed:
    the identical tool call repeated back to back, and one path edited N
    times running with no other tool call in between.
    """
    rows = _tool_calls(con, run_id, max(tool_repeats, file_repeats))
    if not rows:
        return None
    last_seq = int(rows[-1]["seq"])
    tail = rows[-tool_repeats:]
    if len(tail) >= tool_repeats >= 2 and len({
            (r["name"], r["payload"]) for r in tail}) == 1:
        name = tail[-1]["name"] or "tool"
        return (f"the same {name} call ran {tool_repeats} times in a row with "
                "identical arguments", last_seq)
    tail = rows[-file_repeats:]
    if len(tail) >= file_repeats >= 2:
        paths = {edited_path(r["name"], r["payload"]) for r in tail}
        if len(paths) == 1 and None not in paths:
            return (f"{paths.pop()} was edited {file_repeats} times in a row "
                    "with no other tool call in between", last_seq)
    return None


def _mechanical_seen(con, run_id: int, seq: int) -> bool:
    """Has this exact trip already been acted on? Keeps a still-spinning tail
    from producing one correction per poll."""
    row = _last(con, run_id, "mechanical")
    if not row or not row["detail"]:
        return False
    try:
        return int(json.loads(row["detail"]).get("seq", -1)) == seq
    except (ValueError, AttributeError, TypeError):
        return False


CORRECTION = (
    "Dromond's supervisor is watching this run from outside and sees {reason}. "
    "That is a loop, not progress. Stop repeating the action. If you are "
    "blocked, say plainly what is blocking you and finish with your handoff "
    "summary instead of trying again."
)


def mechanical(con, run_id: int, cfg: dict | None = None) -> dict | None:
    """Layer (b). First trip corrects; a second, different trip stops and
    escalates — a run that keeps spinning after being told is feral."""
    settings = (cfg or {}).get("settings", {})
    found = loop_reason(
        con, run_id,
        tool_repeats=int(settings.get("loop_repeats", TOOL_REPEATS)),
        file_repeats=int(settings.get("loop_file_repeats", FILE_REPEATS)))
    if not found:
        return None
    reason, seq = found
    if _mechanical_seen(con, run_id, seq):
        return None
    told = _last(con, run_id, "mechanical", "tell")
    action = "stop" if told else "tell"
    verdict = {"action": action, "reason": reason,
               "message": CORRECTION.format(reason=reason), "source": "mechanical"}
    record(con, run_id, "mechanical", action, reason, {"seq": seq})
    con.commit()
    return apply_verdict(con, run_id, verdict, cfg, layer="mechanical",
                         recorded=True)


# --- layer (c): the out-of-band observer turn -------------------------------

INSTRUCTIONS = """\
You are Dromond's spin observer. You are reading a worker run's transcript \
from OUTSIDE the run. The worker cannot see you and is not paying for this.

Judge ONE thing: is this run CONVERGING on its mission, or is it exploring \
without progress?

Converging looks like: narrowing on the real files, making changes, checking \
them, closing in on a handoff. Exploring without progress looks like: reading \
and searching on and on with nothing produced, re-deriving what the \
transcript already established, wandering into code the mission never named, \
or working on something already finished. The activity line below is \
evidence: many tool calls with NO file edits is a run that has been reading \
for a long time and changing nothing, which is a signal — weigh it against \
the mission (a review or investigation legitimately edits nothing).

LENGTH IS NOT A FAULT. There are no budgets and no run ceilings here. A run \
that has worked for hours and is still moving forward is a GOOD run — leave \
it alone. Stop a run only for genuinely feral behaviour: repeating an action \
that keeps failing, thrashing over the same file, drifting off the mission, \
exploring endlessly with nothing to show, or waiting on something that will \
never arrive.

Reply with ONE JSON object and nothing else:
{"action": "ok" | "tell" | "stop", "reason": "<one sentence of evidence from \
the transcript>", "message": "<only for tell: the correction to deliver to \
the worker>"}

"ok" = do nothing. "tell" = deliver a correction and let the run continue. \
"stop" = stop the run and wake a human. When in doubt, answer "ok".
"""


def digest(con, run_id: int, limit: int = DIGEST_EVENTS,
           chars: int = DIGEST_CHARS) -> str:
    rows = list(con.execute(
        "SELECT seq, kind, name, payload FROM events WHERE run_id=? "
        "ORDER BY seq DESC LIMIT ?", (run_id, limit)))
    lines = []
    for row in rows[::-1]:
        body = " ".join((row["payload"] or "").split())[:chars]
        head = f"{row['kind']}"
        if row["name"]:
            head += f" {row['name']}"
        lines.append(f"[{row['seq']}] {head}: {body}")
    return "\n".join(lines) or "(the trace is empty)"


def activity(con, run_id: int) -> str:
    """One line of shape the transcript tail cannot show (W-0189).

    Run 24 tripped neither mechanical rule: it never repeated a call and never
    edited a file, it just READ for 21 minutes. The tail of a trace looks
    busy either way, so the counts over the WHOLE run go in the prompt.

    ponytail: edits are counted by tool NAME only, not by parsing every
    payload — a run that edits through `bash sed` reads as zero edits here.
    Parse payloads if that shape ever shows up honestly.
    """
    names = ", ".join("?" * len(_EDIT_TOOLS))
    row = con.execute(
        "SELECT COUNT(*) AS calls, "
        "SUM(CASE WHEN lower(name) IN (" + names + ") THEN 1 ELSE 0 END) AS edits "
        "FROM events WHERE run_id=? AND kind='tool_call'",
        (*sorted(_EDIT_TOOLS), run_id)).fetchone()
    calls, edits = int(row["calls"] or 0), int(row["edits"] or 0)
    return (f"{calls} tool calls so far, {edits} of them file edits"
            + (" — this run has changed nothing" if calls and not edits else ""))


def too_thin(con, run_id: int) -> bool:
    """A trace with almost nothing in it has nothing to judge, and a model
    call to say so is the one cost this design refuses to pay. Silence with
    no transcript is layer (a)'s business, not the observer's."""
    return int(con.execute(
        "SELECT COUNT(*) AS n FROM (SELECT 1 FROM events WHERE run_id=? "
        "LIMIT ?)", (run_id, MIN_EVENTS)).fetchone()["n"]) < MIN_EVENTS


def prompt_for(con, run, *, limit: int = DIGEST_EVENTS) -> str:
    mission = ""
    try:
        if run["brief_path"]:
            mission = Path(run["brief_path"]).read_text(
                encoding="utf-8", errors="replace")[:MISSION_CHARS]
    except OSError:
        mission = ""
    started = _epoch(run["started_at"])
    elapsed = int(time.time() - started) // 60 if started else 0
    return (f"{INSTRUCTIONS}\n--- mission (run {run['id']}, running for "
            f"{elapsed} minutes) ---\n{mission or run['title'] or '(no brief)'}\n"
            f"\n--- activity ---\n{activity(con, run['id'])}\n"
            f"\n--- last {limit} trace events ---\n{digest(con, run['id'], limit)}\n")


def record_turn(con, layer: str, profile: dict, log_path: str, ok: bool,
                note: str = "", meta: dict | None = None) -> int | None:
    """File a control turn as a terminal runs row and ingest its transcript.

    The transcript lands in the logs directory and stays there: it ages out
    under the same ``prune_raw_logs`` rule as any terminal run. The row is
    terminal at birth and carries ``layer`` and no work_item/branch, so every
    fleet query either skips it by status or never matches it.

    Never raises and never masks the turn's own outcome: a turn that cannot
    be recorded is a lost trace, not a failed dispatch.
    """
    try:
        backend = profile.get("backend", "opencode")
        now = db.now()
        run_id = int(con.execute(
            "INSERT INTO runs(profile, backend, model, title, requested_by, "
            "log_path, workdir, status, exit_code, summary, started_at, "
            "finished_at, layer) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (profile.get("name") or layer, backend, profile.get("model"),
             f"{layer} turn", "dromond", log_path,
             tempfile.gettempdir(), "done" if ok else "failed",
             0 if ok else 1, (note or "")[:500], now, now, layer)).lastrowid)
        traces.ingest(con, run_id, log_path, backend)
        con.commit()
        if meta is not None:
            meta["turn_id"] = run_id
        return run_id
    except Exception as exc:
        print(f"dromond observer: could not record the {layer} turn: {exc!r}",
              flush=True)
        return None


def _write_transcript(layer: str, stdout: str) -> str:
    """The retained transcript, in the logs directory. Written BEFORE the
    outcome is judged, so a failed turn leaves the same readable log a good
    one does; retention prunes it under the run-log rule."""
    path = paths.logs_dir() / f"turn-{layer}-{time.time_ns()}.jsonl"
    path.write_text(stdout or "", encoding="utf-8", errors="replace")
    return str(path)


def note_turn(con, turn_id: int | None, summary: str) -> None:
    """The decision one-liner on the turn's row — what the pinned entry shows.

    This is also where the two-way link lives: the summary names the run the
    turn acted on and the Nod card it produced, and the card's detail names
    this turn (``apply_verdict`` / ``merge`` write that half). ``con=None``
    opens a short-lived connection (the merge judge is never handed one).
    """
    if not turn_id:
        return
    own = con is None
    con = db.connect() if own else con
    try:
        con.execute("UPDATE runs SET summary=? WHERE id=? AND layer IS NOT NULL",
                    (summary[:500], turn_id))
        con.commit()
    except Exception as exc:
        print(f"dromond observer: could not note turn {turn_id}: {exc!r}",
              flush=True)
    finally:
        if own:
            con.close()


def model_turn(profile: dict, prompt: str, *, timeout: int = TURN_TIMEOUT,
               workdir: str | None = None, layer: str | None = None,
               con=None, meta: dict | None = None) -> str:
    """One cheap model call, out of band. Returns the model's last text.

    A separate process with its own prompt: the worker's session is never
    resumed, never interrupted, and never billed for this.

    With ``layer`` set (router / merge / observer / conductor) the turn is a
    CONTROL turn (W-0214): its transcript is retained instead of unlinked,
    and the turn is recorded as a terminal runs row whose events normalize
    through ``traces.ingest`` — including a FAILED turn, which used to leave
    no trace at all. ``con`` is borrowed when given, opened for the recording
    otherwise. ``meta`` receives ``turn_id`` so the caller can link the
    decision back to it.
    """
    cmd = runners.build_cmd(profile, workdir=workdir or tempfile.gettempdir(),
                            title="dromond-observer", prompt=prompt)
    own = layer is not None and con is None
    con = db.connect() if own else con
    try:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                                  stdin=subprocess.DEVNULL)
            stdout = proc.stdout
        except FileNotFoundError as exc:
            if layer:
                record_turn(con, layer, profile, _write_transcript(layer, ""),
                            False, f"{cmd[0]} is not installed", meta)
            raise ObserverTurnError(f"{cmd[0]} is not installed") from exc
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout or ""
            if isinstance(out, bytes):
                out = out.decode("utf-8", "replace")
            if layer:
                record_turn(con, layer, profile, _write_transcript(layer, out),
                            False, f"the turn timed out after {timeout}s", meta)
            raise ObserverTurnError(f"the observer turn timed out after {timeout}s") from exc
        log_path = _write_transcript(layer, stdout) if layer else None
        if proc.returncode != 0 and not stdout.strip():
            detail = (proc.stderr or "").strip().splitlines()
            note = f"{cmd[0]} exited {proc.returncode}: {detail[-1][:200] if detail else ''}"
            if layer:
                record_turn(con, layer, profile, log_path, False, note, meta)
            raise ObserverTurnError(note)
        # The backends speak JSONL on stdout; reuse the same tolerant reader the
        # supervisor uses rather than a second parser.
        if layer:
            _, text = runners.parse_log(log_path)
        else:
            handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
            try:
                handle.write(stdout)
                handle.close()
                _, text = runners.parse_log(handle.name)
            finally:
                os.unlink(handle.name)
        if not (text or "").strip():
            if layer:
                record_turn(con, layer, profile, log_path, False,
                            "the turn produced no text", meta)
            raise ObserverTurnError("the observer turn produced no text")
        if layer:
            record_turn(con, layer, profile, log_path, True,
                        (text or "").strip().splitlines()[0][:200], meta)
        return text
    finally:
        if own:
            con.close()


def last_json_object(text: str, key: str) -> dict | None:
    """The last JSON object in a model's reply that carries ``key``, or None.

    Every out-of-band turn in Dromond asks for "one JSON object and nothing
    else" and gets prose around it anyway, so all of them scan the reply the
    same way: from the end, so a model that reconsiders is read on its final
    answer. This is the one copy — ``parse_verdict`` here,
    ``conductor.parse_decision`` and ``router.parse_choice`` all use it.
    """
    raw = (text or "").strip()
    end = raw.rfind("}")
    while end >= 0:
        start = raw.rfind("{", 0, end)
        while start >= 0:
            try:
                candidate = json.loads(raw[start:end + 1])
            except ValueError:
                start = raw.rfind("{", 0, start)
                continue
            if isinstance(candidate, dict) and candidate.get(key):
                return candidate
            break
        end = raw.rfind("}", 0, end)
    return None


def parse_verdict(text: str) -> dict:
    """The last JSON object in the model's reply. Garble reads as 'ok'.

    An observer that cannot be understood must never cost a run its life.
    """
    raw = (text or "").strip()
    found = last_json_object(raw, "action")
    if not found:
        return {"action": "ok", "reason": "the observer's reply was not JSON; "
                                          "reading it as 'no action'",
                "message": "", "source": "observer", "unparsed": raw[:500]}
    action = str(found.get("action", "ok")).strip().lower()
    if action not in ("ok", "tell", "stop"):
        action = "ok"
    return {"action": action, "reason": str(found.get("reason") or "")[:2000],
            "message": str(found.get("message") or ""), "source": "observer"}


def judge(con, run_id: int, cfg: dict | None = None, *, turn=None) -> dict:
    """Run the observer turn and return its verdict. Does not act on it."""
    run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if run is None:
        raise KeyError(f"no run {run_id}")
    cfg = config.load(run["project_id"]) if cfg is None else cfg
    profile = observer_profile(cfg)            # raises ObserverUnconfigured
    meta: dict = {}
    if turn is not None:
        text = turn(profile, prompt_for(con, run))
    else:
        text = model_turn(profile, prompt_for(con, run),
                          con=con, layer="observer", meta=meta)
    verdict = parse_verdict(text)
    verdict["profile"] = profile["name"]
    if meta.get("turn_id"):
        verdict["turn_id"] = meta["turn_id"]
    return verdict


# --- the three outcomes ------------------------------------------------------

def apply_verdict(con, run_id: int, verdict: dict, cfg: dict | None = None, *,
                  layer: str = "observer", recorded: bool = False) -> dict:
    """Do nothing, tell a correction, or stop and escalate. Nothing else.

    ``recorded`` says the caller already wrote the observation row (the
    reasoning must exist before a stop touches the process).
    """
    from dromond import http  # imported here: http imports this module
    action = verdict.get("action", "ok")
    reason = verdict.get("reason") or ""
    if not recorded:
        record(con, run_id, layer, action, reason,
               {k: v for k, v in verdict.items() if k not in ("reason", "action")})
        con.commit()
    result = {"run": run_id, "action": action, "reason": reason, "layer": layer}
    turn_id = verdict.get("turn_id")
    if action == "tell":
        message = verdict.get("message") or CORRECTION.format(reason=reason)
        told = http.tell_run(con, run_id, message)
        result["tell"] = told
        note_turn(con, turn_id, f"tell run {run_id}: {reason}")
        return result
    if action == "stop":
        run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        # Escalate FIRST: a stop that reached nobody is the silent kill
        # DESIGN §7 forbids. The kill is what may fail; the reasoning is not.
        result["escalation"] = escalate(
            con, run, title=f"Run {run_id} stopped by the spin observer",
            detail=f"The {layer} layer stopped run {run_id}.\n\n{reason}\n\n"
                   "Nothing retries this automatically: the observer stopped "
                   "it on judgement, not on an infrastructure fault."
                   + (f"\n\nThe turn that decided this is recorded as run "
                      f"#{turn_id}." if turn_id else ""),
            cfg=cfg)
        nod_id = (result["escalation"] or {}).get("nod")
        note_turn(con, turn_id,
                  f"stop run {run_id}: {reason}"
                  + (f" · nod {nod_id}" if nod_id else ""))
        result["stopped"] = http.stop_run(con, run_id)
        return result
    note_turn(con, turn_id, f"{action}: {reason}" if reason else action)
    return result


def escalate(con, run, *, title: str, detail: str, cfg: dict | None = None) -> dict:
    """Put the reasoning where a human sees it: the run's message thread
    always, plus a Nod card when the human loop is configured.

    Always a ``failure`` card (Retry / Abandon) — every escalation from this
    module is "this run stopped and a human has to choose what happens now".

    Never raises. An escalation that cannot be delivered still has to be
    recorded, or the run stops for no visible reason.
    """
    run_id = int(run["id"]) if run is not None else None
    con.execute(
        "INSERT INTO messages(run_id, sender, body, kind, created_at) "
        "VALUES(?, 'dromond', ?, 'escalation', ?)",
        (run_id, f"{title}\n\n{detail}", db.now()))
    con.commit()
    out = {"title": title, "nod": None}
    try:
        cfg = config.load(run["project_id"]) if cfg is None else cfg
        target = nod.from_cfg(cfg)
        if target is None:
            out["nod_error"] = "the human loop is off; escalation recorded only"
            return out
        filed = nod.failure(target, detail, title=title, con=con, run_id=run_id,
                            work_item=run["work_item"] if run is not None else None)
        out["nod"] = filed.get("request_id")
    except Exception as exc:  # a dead Nod must not swallow the reasoning
        out["nod_error"] = f"{exc.__class__.__name__}: {exc}"
        print(f"dromond observer: could not file escalation for run {run_id}: "
              f"{exc!r}", flush=True)
    return out


# --- the supervisor seam -----------------------------------------------------

class Watcher:
    """SEAM (W-0166): the spin observer, attached to the supervisor's loop.

    ``supervise._run_proc`` calls ``poll()`` on its existing 0.5s tick. Layer
    (b) is rate-limited SQL. Layer (c) runs on its own thread with its own
    connection, so the supervisor never blocks on a model call — a kill or a
    message delivery stays as responsive as it was before.
    """

    def __init__(self, run_id: int, project_id: str | None = None, *, turn=None,
                 clock=time.time):
        self.run_id = run_id
        self.project_id = project_id
        self.turn = turn
        self.clock = clock
        self._next = 0.0
        self._thread: threading.Thread | None = None

    def poll(self, con) -> None:
        now = self.clock()
        if now < self._next:
            return
        self._next = now + POLL_EVERY
        cfg = config.load(self.project_id)
        try:
            mechanical(con, self.run_id, cfg)
        except Exception as exc:  # a check must never take supervision down
            print(f"dromond observer: loop check failed for run {self.run_id}: "
                  f"{exc!r}", flush=True)
        if self._due(con, cfg) and not (self._thread and self._thread.is_alive()):
            self._thread = threading.Thread(target=self._pass, daemon=True)
            self._thread.start()

    def _due(self, con, cfg: dict) -> bool:
        row = con.execute("SELECT started_at, status FROM runs WHERE id=?",
                          (self.run_id,)).fetchone()
        if not row or row["status"] in db.RUN_TERMINAL:
            return False
        first = first_look(cfg)
        every = float(cfg.get("settings", {}).get("observer_interval", INTERVAL))
        last = _last(con, self.run_id, "observer")
        anchor = _epoch(last["created_at"]) if last else _epoch(row["started_at"])
        if anchor is None:
            return False
        return self.clock() >= anchor + (every if last else first)

    def _pass(self) -> None:
        """One observer turn, off the supervisor's thread."""
        con = db.connect()
        try:
            row = con.execute("SELECT status FROM runs WHERE id=?",
                              (self.run_id,)).fetchone()
            if not row or row["status"] in db.RUN_TERMINAL:
                return
            if too_thin(con, self.run_id):
                record(con, self.run_id, "observer", "ok",
                       "nothing in the trace to judge yet")
                con.commit()
                return
            try:
                verdict = judge(con, self.run_id, turn=self.turn)
            except (ObserverUnconfigured, ObserverTurnError) as exc:
                # The observer failing is the observer's problem, never the
                # run's. Recorded so the next look is an hour away, not 30s.
                record(con, self.run_id, "observer", "ok",
                       f"the observer turn could not run: {exc}")
                con.commit()
                return
            apply_verdict(con, self.run_id, verdict)
        except Exception as exc:
            print(f"dromond observer: pass failed for run {self.run_id}: {exc!r}",
                  flush=True)
        finally:
            con.close()

    def wait(self, timeout: float = 30) -> None:
        """Join an in-flight observer turn (tests, and orderly shutdown)."""
        if self._thread:
            self._thread.join(timeout)


# --- on demand: `dromond check <run>` / POST /api/runs/N/check --------------

def check(con, run, result: dict, *, model: bool = True, turn=None) -> dict:
    """SEAM (W-0166) called by ``http.check_run``: layers (b) and (c) now.

    ``http.check_run`` has already done layer (a) — process liveness and log
    silence — and put its sentence in ``result["verdict"]``. This adds the
    mechanical loop check and, for a live run, the observer turn, and lets
    their verdict replace it. Same three outcomes as the scheduled look.
    """
    run_id = int(run["id"])
    cfg = config.load(run["project_id"])
    outcome = mechanical(con, run_id, cfg)
    if outcome:
        result["loop"] = outcome
        result["verdict"] = f"{outcome['action']}: {outcome['reason']}"
        return result
    if not model:
        result["observer"] = {"skipped": "asked for the mechanical layers only"}
        return result
    if run["status"] in db.RUN_TERMINAL:
        # Nothing to correct and nothing to stop. Whether the finished work
        # is GOOD is a judgment call and belongs to `planner_review`.
        result["observer"] = {"skipped": "the run is already terminal"}
        return result
    if too_thin(con, run_id):
        result["observer"] = {"skipped": "nothing in the trace to judge yet"}
        return result
    try:
        verdict = judge(con, run_id, cfg, turn=turn)
    except (ObserverUnconfigured, ObserverTurnError) as exc:
        result["observer"] = {"error": str(exc)}
        return result
    result["observer"] = apply_verdict(con, run_id, verdict, cfg)
    if verdict["action"] != "ok" or verdict.get("reason"):
        result["verdict"] = f"{verdict['action']}: {verdict['reason']}"
    return result


# --- retry (DESIGN §7) -------------------------------------------------------

def _stopped_deliberately(con, run_id: int) -> bool:
    return _last(con, run_id, "observer", "stop") is not None or \
        _last(con, run_id, "mechanical", "stop") is not None


def infra_streak(con, run) -> int:
    """Consecutive infrastructure-shaped failures on the same item, this one
    included. The item is the Work item when there is one, else the retry
    lineage — a run dispatched by hand is still 'the same item' to itself."""
    if run["work_item"]:
        rows = con.execute(
            f"SELECT status FROM runs WHERE work_item=? AND id<=? "
            f"AND status IN {db.TERMINAL_SQL} ORDER BY id DESC",
            (run["work_item"], run["id"]))
        streak = 0
        for row in rows:
            if row["status"] not in INFRA_TERMINAL:
                break
            streak += 1
        return streak
    streak, current = 0, run
    while current is not None and current["status"] in INFRA_TERMINAL:
        streak += 1
        previous = current["retry_of"]
        current = con.execute("SELECT * FROM runs WHERE id=?",
                              (previous,)).fetchone() if previous else None
    return streak


def _retry_row(con, run, root) -> int:
    """A fresh run of the SAME brief. Not a resume: a new process, new log.

    The failed run's workdir may already be gone — a terminal run gives its
    worktree back (DESIGN §2) and the merge deletes its branch (§9) — so the
    retry re-homes through the shared seam instead of copying two dead paths
    (live run 28).

    ponytail: the brief is copied verbatim, so the run id printed inside it
    is the first attempt's. Rendering it fresh needs the mission text, which
    only the brief itself still has.
    """
    from dromond import supervise  # supervise imports this module
    workdir, branch, base_commit = supervise.rehome(con, root, run)
    run_id = int(con.execute(
        "INSERT INTO runs(profile, backend, model, title, requested_by, workdir, "
        "project_id, branch, base_commit, work_item, retry_of, status, started_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?, 'spawning', ?)",
        (run["profile"], run["backend"], run["model"], run["title"],
         run["requested_by"], workdir, run["project_id"], branch,
         base_commit, run["work_item"], run["id"], db.now())).lastrowid)
    brief = paths.briefs_dir() / f"run-{run_id}.md"
    try:
        text = Path(run["brief_path"]).read_text(encoding="utf-8", errors="replace")
    except (OSError, TypeError):
        text = run["title"] or "Continue the original mission."
    brief.write_text(text, encoding="utf-8")
    log = paths.logs_dir() / f"run-{run_id}.jsonl"
    log.touch()
    con.execute("UPDATE runs SET brief_path=?, log_path=? WHERE id=?",
                (str(brief), str(log), run_id))
    con.commit()
    return run_id


def after_terminal(con, run_id: int, *, cfg: dict | None = None,
                   launcher=None) -> dict:
    """SEAM (W-0166): the supervisor calls this once, after finalization.

    Applies the §7 retry rule and nothing else. Retry ONCE for an
    infrastructure-shaped terminal state, reusing the same brief; a second
    consecutive one on the same item stops and escalates. A run that
    finished is never retried here — bad work is a judgment failure and goes
    to ``planner_review``.
    """
    run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if run is None:
        return {"action": "none", "reason": f"no run {run_id}"}
    stop = _last(con, run_id, "observer", "stop") or _last(con, run_id, "mechanical", "stop")
    if stop is not None:
        # The finalizer overwrote `summary` with the transcript's last words;
        # put the reasoning back on the row a human reads first.
        note = f"Stopped by the spin observer: {stop['reason']}"
        con.execute("UPDATE runs SET summary=substr(COALESCE(summary || char(10), '') "
                    "|| ?, 1, 2000) WHERE id=?", (note, run_id))
        con.commit()
    if run["status"] not in INFRA_TERMINAL:
        return {"action": "none", "reason": f"status {run['status']} is not "
                                            "infrastructure-shaped"}
    if _stopped_deliberately(con, run_id):
        return {"action": "none", "reason": "the run was stopped on judgement"}
    if dispatch.paused(con):
        record(con, run_id, "retry", "deferred", "dispatch is paused")
        con.commit()
        return {"action": "none", "reason": "dispatch is paused"}
    cfg = config.load(run["project_id"]) if cfg is None else cfg
    streak = infra_streak(con, run)
    if streak >= 2:
        reason = (f"{streak} consecutive infrastructure failures on "
                  f"{run['work_item'] or f'run {run_id}'} — nothing spends a third")
        record(con, run_id, "retry", "escalate", reason, {"streak": streak})
        con.commit()
        result = {"action": "escalate", "reason": reason, "streak": streak}
        result["escalation"] = escalate(
            con, run, title=f"Two infrastructure failures on "
                            f"{run['work_item'] or f'run {run_id}'}",
            detail=f"Run {run_id} ended `{run['status']}` after an automatic "
                   f"retry of the same brief also failed.\n\n"
                   f"{(run['summary'] or '(no summary)')[:1000]}\n\n"
                   "Dromond will not retry a third time.", cfg=cfg)
        return result
    from dromond import project
    root = project.root_for(con, run)
    retry_id = _retry_row(con, run, root)
    # Anything waiting on the failed run waits on its retry instead —
    # otherwise the dependency release declines the whole chain a second
    # later, while the retry that may well succeed is still starting.
    con.execute("UPDATE OR IGNORE dispatch_dependencies SET depends_on_run=? "
                "WHERE depends_on_run=?", (retry_id, run_id))
    record(con, run_id, "retry", "retry",
           f"retrying run {run_id} ({run['status']}) once, same brief",
           {"retry_run": retry_id})
    con.commit()
    if launcher is None:
        from dromond import supervise  # supervise imports this module
        launcher = supervise.spawn_supervisor
    launcher(root, retry_id)
    return {"action": "retry", "run": retry_id, "reason": f"run {run_id} "
            f"ended {run['status']}; retried once with the same brief"}


# --- the planner seam --------------------------------------------------------

def planner_review(con, run_id: int, reason: str, *, detail: str | None = None,
                   cfg: dict | None = None) -> dict:
    """SEAM: a JUDGMENT failure — the run FINISHED and produced bad work.

    Code must not retry this: the same brief through the same model produces
    the same bad work, which is why DESIGN §7 sends it to a planner turn
    instead. That turn does not exist yet (DESIGN §10, the conductor).

    A planner turn attaching here is expected to:
      * receive ``(run_id, reason)`` plus, from the database, the run row,
        its ``observations``, its normalized ``events``, and its branch diff;
      * decide ONE of: re-brief the item with a corrected mission, split it
        into smaller items, or hand it to the human;
      * dispatch whatever it decided itself, and return
        ``{"action": ..., "run": <new run id or None>, "reason": ...}``.

    Until then this records the request and escalates, so a judgment failure
    is visible instead of silently absorbed.
    """
    run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    record(con, run_id, "planner", "deferred", reason, {"detail": detail})
    con.commit()
    out = {"action": "deferred", "run": run_id, "reason": reason}
    out["escalation"] = escalate(
        con, run, title=f"Run {run_id} finished but the work is not right",
        detail=f"{reason}\n\n{detail or ''}\n\nThis is a judgment failure, not "
               "an infrastructure one, so it is NOT retried automatically. "
               "The planner turn that would re-brief it does not exist yet "
               "(DESIGN §10).", cfg=cfg)
    return out
