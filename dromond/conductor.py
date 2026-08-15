"""The conductor (DESIGN §10): a goal, watched by code, judged episodically.

A **goal** is a Work task tagged ``goal`` and ticked ``delegated`` (§1, the
epic pattern). The sweeper keeps claiming ordinary delegated items; this
module watches goals and nothing else.

- **The conductor is deterministic code.** It reads the board, maps runs to
  the goal and its children, decides whether an event worth judging has
  happened, assembles the packet, applies the decision and logs it. **Zero
  tokens while anything is merely in flight** — a run that is simply running
  produces no trigger, so no session is ever started for it.
- **A planner turn is episodic judgment.** On a trigger, one fresh stateless
  session gets one code-curated packet and returns one structured decision.
  The decision is logged and attributed; the session is discarded. Cost
  scales with events, not elapsed time.

**Triggers** (five, priority order): a new human comment on the goal, a run
blocked, a batch settled, provider runway low, nothing in flight. Each one
carries a *key* — the batch it settled, the run that blocked, the comment's
timestamp — and fires exactly once for that key, because a turn is recorded
with it. That is what keeps "nothing in flight" from waking a planner
forever on an idle goal: its key is the last settled run, so it fires once
per settle and then never again until something settles. The three quiet
triggers (blocked, settled, nothing-in-flight) describe one moment from
three angles, so the first of them to take its turn silences the other two
for that batch: one settle is one turn.

A **floor** of ``TURN_FLOOR_SECONDS`` per goal sits in front of all five. A
turn returning ``wait`` names the event it waits for, and until that event
arrives no other trigger wakes the goal.

**Nothing approves itself.** A planner judging a worker's proposal
(``findings.PLANNER``) is a fresh session that is never the worker's, and
``alignment_planner`` refuses outright if the proposing run turns out to be
a planner turn of this goal. A planner's own next step is ``dispatch``, not
a proposal.

``conductor_turns`` is Dromond's own log and the conductor's whole memory:
the floor, the wait gate, the once-per-settle guard and the delta watermark
are all queries against it. Only state-changing turns also post to the
goal's Work thread; a ``wait`` turn lives only here.
"""
import json
import math
import time
from datetime import datetime

from dromond import (config, db, findings, names, nod, observer, paths,
                         profiles as profiles_mod, project, runway as runway_mod,
                         supervise, sweeper, work_client)
from dromond.work_client import WorkError

# The packet is ~5x the 300-token worker brief: a planner deciding what to
# spend needs the state (DESIGN §10). The cap is HARD — oldest detail is
# dropped first, then the whole render is clamped, so no input can push a
# packet past it.
PACKET_TOKEN_CAP = 1500
CHARS_PER_TOKEN = 4      # ponytail: the usual English approximation, not a
# tokenizer. It over-counts code and under-counts CJK; the ceiling it
# enforces is therefore approximate in tokens and exact in characters.
# Upgrade path: a real tokenizer per backend, when one is worth a dependency.
PACKET_CHAR_CAP = PACKET_TOKEN_CAP * CHARS_PER_TOKEN
GOAL_TEXT_CHARS = 1500   # of that cap, the most the goal block may take
ENTRY_CHARS = 240        # one delta / child / finding / in-flight line

TURN_FLOOR_SECONDS = 120  # ~2 minutes per goal, in front of all five triggers
TURN_TIMEOUT = 300        # a wedged planner turn must not outlive its trigger

GOAL_TAG = "goal"
CLOSED_STATUSES = ("done", "closed", "archived", "cancelled")
ACTIONS = ("dispatch", "propose", "ask_human", "wait", "done")
TRIGGERS = ("comment", "blocked", "settled", "runway_low", "idle")
PLANNER_TIER = 2  # generalist (W-0181); named "mid" before the numbers
# A provider under this much of its window left is "budget low" — the one
# §10 trigger that outlived the budget machinery §7 deleted. It is a runway
# reading (§11), never a grant: nothing here blocks a dispatch.
RUNWAY_LOW_FRACTION = 0.10


class PlannerUnconfigured(Exception):
    """No planner profile. There are no default profiles (DESIGN §5)."""


class PlannerTurnError(Exception):
    """The planner's own model call failed. Never the goal's fault."""


# --- the planner profile -----------------------------------------------------

def profile_name(cfg: dict) -> str:
    """``[settings] planner_profile``, else the one profile marked mid-tier.

    Per project: settings merge per project, so a
    ``[project."<projectId>".settings]`` table gives that project's goals
    their own planner. ponytail: per-goal granularity has nowhere to live
    yet — DESIGN §10 says "per-goal configurable", but a Work item carries
    no Dromond settings, so the project is the finest key that exists.

    The tier scan runs over the profiles this project ENABLES (W-0187):
    picking a planner the project disabled would staff it anyway, by the
    back door. A ``planner_profile`` naming a disabled one is refused by
    ``planner_profile()`` below, with the message that names the project.
    """
    configured = cfg.get("profiles", {})
    profiles = config.enabled_profiles(cfg)
    named = str(cfg.get("settings", {}).get("planner_profile") or "").strip()
    if named:
        if named not in configured:
            raise PlannerUnconfigured(
                f"planner_profile '{named}' is not a configured profile "
                f"(configured: {', '.join(sorted(configured)) or 'none'}) — "
                f"fix it in {paths.global_config_path()}")
        return named
    mid = sorted(name for name, p in profiles.items()
                 if config.tier_of(p.get("tier")) == PLANNER_TIER)
    if len(mid) == 1:
        return mid[0]
    if not mid:
        raise PlannerUnconfigured(
            "no planner profile: the conductor needs a mid-tier model to take "
            "planner turns with, and there are no default profiles. Set "
            '[settings] planner_profile = "NAME" (or the same key in a '
            '[project."<projectId>".settings] table for one project), or mark '
            "one profile tier = 2 (generalist), in "
            + str(paths.global_config_path()))
    raise PlannerUnconfigured(
        f"several profiles are tier 2 (generalist) ({', '.join(mid)}) — set "
        '[settings] planner_profile = "NAME" to pick one')


def planner_profile(cfg: dict) -> dict:
    """Staffing (W-0187): the planner is a run the project pays for, so a
    disabled profile is refused here like any other."""
    try:
        return config.staff_profile(cfg, profile_name(cfg))
    except SystemExit as exc:
        raise PlannerUnconfigured(str(exc)) from exc


# --- the turn log (schema v10) ----------------------------------------------

def log_turn(con, goal_id: str, *, trigger: str, key: str, action: str,
             rationale: str = "", slug: str | None = None,
             profile: str | None = None, wait_event: str | None = None,
             comment_ts: str | None = None, packet_tokens: int | None = None,
             detail=None) -> int:
    """Record one turn BEFORE it is acted on, so its reasoning survives even
    if the action fails."""
    if detail is not None and not isinstance(detail, str):
        detail = json.dumps(detail, ensure_ascii=False, default=str)
    turn_id = int(con.execute(
        "INSERT INTO conductor_turns(goal_id, trigger_kind, trigger_key, slug, "
        "profile, action, rationale, wait_event, comment_ts, packet_tokens, "
        "detail, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (goal_id, trigger, key, slug, profile, action, rationale[:4000],
         wait_event, comment_ts, packet_tokens, detail, db.now())).lastrowid)
    con.commit()
    return turn_id


def set_detail(con, turn_id: int, detail) -> None:
    if not isinstance(detail, str):
        detail = json.dumps(detail, ensure_ascii=False, default=str)
    con.execute("UPDATE conductor_turns SET detail=? WHERE id=?", (detail, turn_id))
    con.commit()


def last_turn(con, goal_id: str):
    return con.execute("SELECT * FROM conductor_turns WHERE goal_id=? "
                       "ORDER BY id DESC LIMIT 1", (goal_id,)).fetchone()


def fired(con, goal_id: str, trigger: str, key: str) -> bool:
    """Has this exact event already had its turn? The once-per-settle rule
    and every other once-per-event rule are this one query."""
    return con.execute(
        "SELECT 1 FROM conductor_turns WHERE goal_id=? AND trigger_kind=? "
        "AND trigger_key=? LIMIT 1", (goal_id, trigger, key)).fetchone() is not None


def turn_slugs(con, goal_id: str) -> set:
    return {row["slug"] for row in con.execute(
        "SELECT slug FROM conductor_turns WHERE goal_id=? AND slug IS NOT NULL",
        (goal_id,))}


# --- goals and their state ---------------------------------------------------

def is_goal(item: dict) -> bool:
    """DESIGN §1: tagged ``goal`` and ticked ``delegated``. Both, or it is an
    ordinary item and the sweeper owns it."""
    tags = [str(t).strip().lower() for t in (item.get("tags") or [])]
    return GOAL_TAG in tags and bool(item.get("delegated"))


def open_goals(tasks) -> list[dict]:
    return [t for t in (tasks or [])
            if is_goal(t) and t.get("status") not in CLOSED_STATUSES]


def children_of(tasks, goal_id: str) -> list[dict]:
    return [t for t in (tasks or []) if t.get("parentId") == goal_id]


def _rows(con, sql: str, item_ids: list[str]):
    marks = ",".join("?" * len(item_ids))
    return list(con.execute(sql.format(marks=marks), item_ids))


def in_flight(con, item_ids: list[str]) -> list:
    return _rows(con, "SELECT * FROM runs WHERE work_item IN ({marks}) AND "
                      f"status NOT IN {db.TERMINAL_SQL} ORDER BY id", item_ids)


def settled_runs(con, item_ids: list[str], since_id: int = 0) -> list:
    return [r for r in _rows(
        con, "SELECT * FROM runs WHERE work_item IN ({marks}) AND status IN "
             f"{db.TERMINAL_SQL} ORDER BY id", item_ids) if r["id"] > since_id]


latest_runway = runway_mod.latest_polls  # the query moved next to its table


def runway_low(entries: list[dict]) -> list[dict]:
    """Stale readings are reported to humans but never trigger (W-0179):
    since adapters now surface an old snapshot instead of withholding it, a
    number from a window that has already refilled would otherwise stop
    dispatch on history — the exact failure DESIGN §11 forbids."""
    low = []
    for e in entries:
        remaining, limit = e.get("remaining"), e.get("limit_value")
        if remaining is None:
            continue          # unknown means available (DESIGN §11)
        age = runway_mod.age_hours(e.get("as_of"))
        if (age is not None and age >= runway_mod.STALE_AFTER_H) or \
                runway_mod.expired(e.get("resets_at")):
            continue          # stale means available, for the same reason
        fraction = (remaining / 100.0 if (e.get("unit") or "") == "percent"
                    else (remaining / limit if limit else None))
        if fraction is not None and fraction <= RUNWAY_LOW_FRACTION:
            low.append(e)
    return low


# --- triggers ----------------------------------------------------------------

def _epoch(ts: str | None) -> float:
    try:
        return datetime.fromisoformat((ts or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _minutes_since(ts: str | None) -> int:
    started = _epoch(ts)
    return int((time.time() - started) // 60) if started else 0


def candidates(con, goal: dict, item_ids: list[str], comments: list[dict],
               runway_entries: list[dict]) -> list[tuple[str, str, str]]:
    """Every (trigger, key, detail) that could fire now, in priority order.

    A key is what makes a trigger fire once and only once: the settled run
    that ends a batch, the run that blocked, the comment's timestamp, the
    provider's reset window. ``fired()`` filters the ones already taken.
    """
    flight = in_flight(con, item_ids)
    done = settled_runs(con, item_ids)
    newest = done[-1]["id"] if done else 0
    out: list[tuple[str, str, str]] = []
    if comments:
        newest_comment = max(c["at"] for c in comments)
        out.append(("comment", newest_comment,
                    f"{len(comments)} new human comment(s) on {goal['id']}"))
    for run in done:
        if run["status"] != "done":
            out.append(("blocked", f"run:{run['id']}",
                        f"run {run['id']} ended {run['status']}"))
    # One settle is ONE turn. The three quiet triggers describe the same
    # moment from different angles, so once any of them has had its turn for
    # this batch the others stay silent — otherwise a batch that ends badly
    # buys a blocked turn, a settled turn and an idle turn in a row, all
    # reading the same state.
    blocked_taken = fired(con, goal["id"], "blocked", f"run:{newest}")
    settled_taken = fired(con, goal["id"], "settled", f"settle:{newest}")
    if newest and not flight and not blocked_taken:
        out.append(("settled", f"settle:{newest}",
                    f"the batch settled; {len(done)} run(s) finished"))
    for entry in runway_low(runway_entries):
        out.append(("runway_low",
                    f"{entry['provider']}:{entry.get('resets_at') or entry['id']}",
                    f"{entry['provider']} runway is low"))
    if not flight and not (blocked_taken or settled_taken):
        # Fires ONCE per settle: the key is the batch that settled, so an
        # idle goal cannot wake a planner a second time until something else
        # finishes. A goal with no runs at all has key idle:0 — one turn to
        # get it started, then silence.
        out.append(("idle", f"idle:{newest}", "nothing is in flight"))
    return out


def pick(con, goal_id: str, last, cands: list[tuple[str, str, str]]):
    """The one trigger this pass acts on, honouring the wait gate.

    A ``wait`` turn named the event it waits for; until that event appears
    no other trigger wakes the goal. A wait turn that named nothing is not a
    gate — any trigger may wake it, which costs at most one extra turn and
    never strands the goal.
    """
    gate = last["wait_event"] if last and last["action"] == "wait" else None
    for trigger, key, detail in cands:
        if gate and trigger != gate:
            continue
        if fired(con, goal_id, trigger, key):
            continue
        return {"trigger": trigger, "key": key, "detail": detail}
    return None


# --- the packet --------------------------------------------------------------

def est_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def _entry(order: str, text: str) -> tuple[str, str]:
    return (order or "", " ".join(str(text).split())[:ENTRY_CHARS])


def _block(title: str, entries: list, *, droppable: bool = True,
           empty: str = "(none)") -> dict:
    return {"title": title, "entries": list(entries), "droppable": droppable,
            "dropped": 0, "empty": empty}


def _render(blocks: list[dict]) -> str:
    parts = []
    for b in blocks:
        lines = [f"## {b['title']}"]
        if b["dropped"]:
            lines.append(f"… {b['dropped']} older entries truncated")
        lines += [text for _, text in b["entries"]]
        if not b["entries"] and not b["dropped"]:
            lines.append(b["empty"])
        parts.append("\n".join(lines))
    return "\n\n".join(parts) + "\n"


def fit(blocks: list[dict], cap: int = PACKET_CHAR_CAP) -> str:
    """Render inside the cap, oldest detail first out.

    Entries sort by their own timestamp, so the drop order is chronological
    across blocks. State entries (profiles, runway, in-flight) carry a
    sentinel key and go last: they are current state, not old detail. The
    final clamp is what makes the cap HARD — no input can beat it.
    """
    # ponytail: re-renders per dropped line, O(n²) over a few hundred short
    # lines. Measurable only if a packet ever carries thousands of entries.
    while len(_render(blocks)) > cap:
        droppable = [b for b in blocks if b["droppable"] and b["entries"]]
        if not droppable:
            break
        block = min(droppable, key=lambda b: b["entries"][0][0])
        block["entries"].pop(0)
        block["dropped"] += 1
    return _render(blocks)[:cap]


def build_packet(goal: dict, *, delta: list, children: list, issues: list,
                 profiles: list, runway_entries: list, flight: list,
                 cap: int = PACKET_CHAR_CAP) -> str:
    """The six blocks of DESIGN §10, inside the hard cap."""
    sections = goal.get("sections") or {}
    head = [f"{goal['id']} · {goal.get('title', '')} [{goal.get('status', '')}]"]
    for label in ("goal", "acceptanceCriteria"):
        text = (sections.get(label) or "").strip()
        if text:
            head.append(f"{label}: {text}")
    goal_text = "\n".join(head)[:GOAL_TEXT_CHARS]

    blocks = [
        _block("goal and acceptance", [_entry("", goal_text)], droppable=False),
        _block("delta since the last turn", delta,
               empty="(nothing since the last turn)"),
        _block("open child items", children, empty="(no open children)"),
        _block("open findings", issues, empty="(no open findings)"),
        _block("profiles and runway", profiles + runway_entries,
               empty="(no profiles configured)"),
        _block("in flight now", flight, empty="(nothing in flight)"),
    ]
    return fit(blocks, cap)


def delta_entries(runs: list, comments: list[dict]) -> list:
    """Runs that finished with their outcome, and new human comments."""
    out = [_entry(r["finished_at"] or r["started_at"],
                  f"- run {r['id']} {r['status']}: "
                  f"{(r['summary'] or '(no summary)').strip()}") for r in runs]
    out += [_entry(c["at"], f"- human comment {c['at']}: {c['text']}")
            for c in comments]
    return sorted(out)


def child_entries(children: list[dict]) -> list:
    return sorted(_entry(c.get("updatedAt") or "",
                         f"- {c['id']} [{c.get('status', '')}] {c.get('title', '')}")
                  for c in children if c.get("status") not in CLOSED_STATUSES)


def issue_entries(issues: list[dict]) -> list:
    return sorted(_entry(i.get("updatedAt") or "",
                         f"- {i['id']} [{i.get('state', '')}] {i.get('title', '')}")
                  for i in issues)


def profile_entries(cfg: dict) -> list:
    """Profiles with their headroom notes (D10) — routing intent, never a
    number this module invents.

    Listed in routing order: tier is capability, priority orders equals
    within it, `nice`-style (lower is more preferred) — W-0181.
    """
    out = []
    entries = sorted((cfg.get("profiles") or {}).items(),
                     key=lambda kv: (config.priority_of(kv[1]), kv[0]))
    for name, p in entries:
        bits = [f"- {name}: {p.get('backend', '?')}"]
        if p.get("model"):
            bits.append(str(p["model"]))
        tier = config.tier_of(p.get("tier"))
        if tier:
            bits.append(f"tier {tier} ({config.TIERS[tier]})")
        bits.append(f"priority {config.priority_of(p)}")
        if p.get("note"):
            age = profiles_mod.note_age(p.get("note_at"))
            bits.append(f"note: {p['note']}" + (f" ({age})" if age else ""))
        out.append(_entry("~", " ".join(bits)))
    return out


def runway_entries_for(entries: list[dict]) -> list:
    return [_entry("~", f"- runway {e['provider']}: {runway_mod.entry_text(e)}")
            for e in entries]


def flight_entries(runs: list) -> list:
    return [_entry("~", f"- run {r['id']} {r['status']} for "
                        f"{_minutes_since(r['started_at'])}m on "
                        f"{r['work_item']}: {r['title'] or ''}") for r in runs]


# --- one planner turn --------------------------------------------------------

INSTRUCTIONS = """\
You are Dromond's conductor planner. You hold ONE goal and decide its NEXT \
STEP. This session is stateless and ends with your reply: everything you are \
given is below, and nothing you remember carries over.

Code does all the mechanics — dispatching, watching, filing, ferrying. You \
supply judgment only, and you are asked for it only when something happened.

Choose exactly ONE action:
  dispatch   — start a run now. This is your normal next step. Name the item \
(the goal, or one of its open children) and write the mission.
  propose    — add a durable child task under the goal, for work the human \
should see and reprioritize. Not your next step; dispatch is.
  ask_human  — a question only the human can answer. It wakes their phone.
  wait       — nothing to do until a named event. You MUST name it.
  done       — the acceptance criteria are met. You never close the item; the \
human closes it.

Reply with ONE JSON object and nothing else:
{"action": "dispatch|propose|ask_human|wait|done",
 "rationale": "<one or two sentences of evidence from the packet>",
 "item": "<W-#### for dispatch>", "mission": "<for dispatch>",
 "title": "<for propose>", "question": "<for ask_human>",
 "await": "comment|blocked|settled|runway_low|idle"}

"await" is required for wait and is one of those five events; only that \
event wakes you again.
"""

ALIGNMENT_INSTRUCTIONS = """\
You are Dromond's conductor planner, judging ONE proposal a worker run made \
against the goal you hold. You are a different session from the worker: it \
cannot see you and you do not inherit its reasoning.

Answer only whether the proposal serves THIS goal. Reply with ONE JSON object:
{"verdict": "aligned|pivot", "rationale": "<one sentence>"}

"aligned" = it belongs under this goal and code may file it as a child. \
"pivot" = it changes direction; the human rules on it. When in doubt, pivot.
"""

JUDGMENT_INSTRUCTIONS = """\
You are Dromond's conductor planner. A run FINISHED but produced work that is \
not right. Code will not retry it: the same brief through the same model \
produces the same bad work.

Choose exactly ONE action:
  dispatch   — re-brief the item with a corrected mission (name item and mission).
  propose    — split it into a smaller child item under the goal (name title).
  ask_human  — hand it to the human with a question.

Reply with ONE JSON object and nothing else:
{"action": "dispatch|propose|ask_human", "rationale": "<one sentence>",
 "item": "<W-####>", "mission": "<for dispatch>", "title": "<for propose>",
 "question": "<for ask_human>"}
"""


def model_turn(profile: dict, prompt: str, *, timeout: int = TURN_TIMEOUT) -> str:
    """One fresh stateless session. Reuses the observer's out-of-band caller
    (§7): a separate process, its own prompt, no session ever resumed."""
    try:
        return observer.model_turn(profile, prompt, timeout=timeout)
    except observer.ObserverTurnError as exc:
        raise PlannerTurnError(str(exc)) from exc


def parse_decision(text: str, *, actions=ACTIONS, key: str = "action") -> dict:
    """The last JSON object in the reply. Garble is not a decision.

    An unreadable planner must never change state, so anything unparsed
    becomes a ``wait`` that names no event: logged, ungated, and free. The
    alignment seam speaks ``verdict`` rather than ``action`` (``key``), and
    a reply that fails to be one of ``actions`` lands in the same safe place.
    """
    raw = (text or "").strip()
    found = observer.last_json_object(raw, key)
    if not found:
        return {"action": "wait", "await": None, "unparsed": raw[:500],
                "rationale": "the planner's reply was not JSON; nothing was "
                             "changed and any event will wake it again"}
    action = str(found.get(key, "")).strip().lower()
    if action not in actions:
        return {"action": "wait", "await": None, "unparsed": raw[:500],
                "rationale": f"the planner asked for {action!r}, which is not "
                             f"one of {', '.join(actions)}; nothing was changed"}
    awaited = str(found.get("await") or "").strip().lower() or None
    if awaited not in TRIGGERS:
        awaited = None
    out = {"action": action, "rationale": str(found.get("rationale") or "").strip()[:2000],
           "await": awaited}
    for key in ("item", "mission", "title", "question"):
        value = str(found.get(key) or "").strip()
        if value:
            out[key] = value[:4000]
    return out


def take_turn(profile: dict, packet: str, *, slug: str, instructions=INSTRUCTIONS,
              turn=None, actions=ACTIONS, key: str = "action") -> dict:
    """Invoke one session and take one decision back."""
    prompt = (f"{instructions}\nYou are session dromond/{slug}. It exists for "
              f"this one decision.\n\n{packet}")
    return parse_decision((turn or model_turn)(profile, prompt),
                          actions=actions, key=key)


# --- acting on a decision ----------------------------------------------------

def _tag(client, slug: str) -> str:
    """Attribution: ``dromond/<run-slug>`` (DESIGN §10)."""
    return f"[{client.identity}/{slug}]"


def _post(client, goal_id: str, body: str) -> bool:
    try:
        return client.log_task(goal_id, body[:19000]) is not None
    except WorkError as exc:
        print(f"dromond conductor: post to {goal_id} rejected: {exc}")
        return False


def _dispatch(con, cfg: dict, client, goal: dict, board: dict, decision: dict,
              slug: str, launcher) -> dict:
    """Start one run for the goal or one of its open children.

    Same row shape and same launch path as a swept dispatch — the conductor
    is not a second dispatcher, only another caller of the one there is.
    """
    item = board.get(decision.get("item") or "") or goal
    if item["id"] != goal["id"] and item.get("parentId") != goal["id"]:
        item = goal  # a planner may only dispatch its own goal or its children
    item_id = item["id"]
    if con.execute(f"SELECT 1 FROM runs WHERE work_item=? AND status NOT IN "
                   f"{db.TERMINAL_SQL} LIMIT 1", (item_id,)).fetchone():
        return {"action": "skipped", "item": item_id,
                "reason": "a run for this item is already in flight"}
    proj = project.by_work_path(con, item.get("projectPath"))
    if proj is None and project.refresh(con, cfg):
        proj = project.by_work_path(con, item.get("projectPath"))
    if proj is None:
        return {"action": "skipped", "item": item_id,
                "reason": f"no known project for {item.get('projectPath')!r}"}
    pcfg = config.load(proj.project_id)
    worker = sweeper.work_cfg(pcfg).get("profile", "claude")
    try:
        # Staffing (W-0187): the project's enabled set decides, and a
        # refusal is reported as a skip with its reason — never a fallback
        # to whichever other profile happens to be enabled.
        profile = config.staff_profile(pcfg, worker)
    except SystemExit as exc:
        return {"action": "skipped", "item": item_id, "reason": str(exc)}
    mission = decision.get("mission") or (
        f"Work task {item_id}: {item.get('title', '')}\n\n"
        "The Work item snapshot below carries the details.")
    run_id, run_slug = sweeper.insert_run(con, proj, worker, profile,
                                          (item.get("title") or item_id)[:80],
                                          item_id, item.get("updatedAt"))
    run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    isolate = bool(sweeper.work_cfg(pcfg).get("worktree", True))
    snapshot = sweeper.render_snapshot(item, "task")
    try:
        supervise.prepare_launch(con, proj.path, pcfg, run, mission=mission,
                                 use_worktree=isolate, work_snapshot=snapshot)
    except (Exception, SystemExit) as exc:
        # ponytail: the same shared-checkout fallback the sweeper does, said
        # twice. Fold both into one helper when supervise.py is free to edit.
        if not isolate:
            raise
        print(f"dromond conductor: shared checkout for run {run_id} ({exc})")
        supervise.prepare_launch(con, proj.path, pcfg, run, mission=mission,
                                 use_worktree=False, work_snapshot=snapshot)
    con.commit()
    _post(client, goal["id"], f"{_tag(client, slug)} dispatched run {run_id} "
                              f"on {item_id} — {decision.get('rationale', '')}")
    launcher(proj.path, run_id)
    return {"action": "dispatch", "item": item_id, "run": run_id,
            "run_slug": run_slug}


def _propose(client, goal: dict, decision: dict, slug: str) -> dict:
    """Contract verb 5: a durable child under the goal, never top-level."""
    title = decision.get("title") or decision.get("rationale") or "follow-on work"
    tag = _tag(client, slug)
    try:
        created = client.create_task(
            title=title[:300], parent_id=goal["id"],
            project_path=goal.get("projectPath"),
            description=f"{tag} proposed by the conductor under {goal['id']}.\n\n"
                        f"{decision.get('rationale', '')}"[:19000])
    except WorkError as exc:
        print(f"dromond conductor: proposal on {goal['id']} rejected: {exc}")
        return {"action": "rejected", "stage": "create_task", "error": exc.code}
    if created is None:
        return {"action": "deferred", "stage": "create_task"}
    child_id = created.get("id") if isinstance(created, dict) else None
    _post(client, goal["id"], f"{tag} proposed child {child_id or ''} — {title}\n\n"
                              f"{decision.get('rationale', '')}")
    return {"action": "propose", "task": child_id}


def _ask_human(con, cfg: dict, client, goal: dict, decision: dict,
               slug: str) -> dict:
    """A Nod card, mirrored into the goal's thread (DESIGN §8 + §10)."""
    question = decision.get("question") or decision.get("rationale") or \
        "The conductor needs a decision."
    tag = _tag(client, slug)
    out = {"action": "ask_human", "nod": None}
    try:
        target = nod.from_cfg(cfg)
        if target is None:
            out["nod_error"] = "the human loop is off; the question is in Work only"
        else:
            filed = nod.blocked_run(
                target, question, title=f"{goal['id']}: {goal.get('title', '')}"[:200],
                summary=decision.get("rationale", "")[:500], con=con,
                work_item=goal["id"])
            out["nod"] = filed.get("request_id")
    except Exception as exc:  # a dead Nod must not swallow the question
        out["nod_error"] = f"{exc.__class__.__name__}: {exc}"
        print(f"dromond conductor: could not file a Nod card for {goal['id']}: {exc!r}")
    _post(client, goal["id"], f"{tag} needs you: {question}\n\n"
                              f"{decision.get('rationale', '')}"
                              + (f"\n\n(Nod card {out['nod']})" if out["nod"] else ""))
    return out


def _done(client, goal: dict, decision: dict, slug: str) -> dict:
    """Says so; never closes. The human closes (CONTRACT §3 verb 2)."""
    _post(client, goal["id"],
          f"{_tag(client, slug)} believes {goal['id']} is met — "
          f"{decision.get('rationale', '')}\n\nDromond never closes a goal; "
          "close it yourself when you agree.")
    return {"action": "done"}


def apply_decision(con, cfg: dict, client, goal: dict, board: dict,
                   decision: dict, slug: str, launcher) -> dict:
    """State-changing turns post to the goal's thread; ``wait`` does not."""
    action = decision["action"]
    if action == "dispatch":
        return _dispatch(con, cfg, client, goal, board, decision, slug, launcher)
    if action == "propose":
        return _propose(client, goal, decision, slug)
    if action == "ask_human":
        return _ask_human(con, cfg, client, goal, decision, slug)
    if action == "done":
        return _done(client, goal, decision, slug)
    # wait: Dromond's own log only, or an overnight goal leaves fifty
    # "still waiting" comments on the board.
    return {"action": "wait", "await": decision.get("await")}


def wait_event_for(decision: dict) -> str | None:
    """What re-wakes this goal.

    ``ask_human`` and ``done`` both hand the goal to the human, and a human
    answer reaches Dromond as a comment on the thread (a Nod decision is
    mirrored there too), so both gate on ``comment``. Anything else that is
    not a wait leaves the gate open.
    """
    action = decision["action"]
    if action == "wait":
        return decision.get("await")
    if action in ("ask_human", "done"):
        return "comment"
    return None


# --- one pass ----------------------------------------------------------------

def open_issues_for(issues, project_path: str | None) -> list[dict]:
    """Findings land as Work issues (DESIGN §9); the open ones for this
    project are the goal's open findings."""
    return [i for i in (issues or [])
            if i.get("state") not in ("resolved", "closed")
            and (project_path is None or i.get("projectPath") == project_path)]


def conduct_goal(con, cfg: dict, client, goal: dict, board: dict, issues: list,
                 *, turn=None, launcher=supervise.spawn_supervisor,
                 floor: int = TURN_FLOOR_SECONDS) -> dict | None:
    """Zero or one planner turn for one goal. ``None`` means nothing fired —
    which is the common case, and it costs nothing."""
    goal_id = goal["id"]
    children = children_of(board.values(), goal_id)
    item_ids = [goal_id] + [c["id"] for c in children]
    last = last_turn(con, goal_id)
    if last is not None and (time.time() - _epoch(last["created_at"])) < floor:
        return None
    comments = sweeper.human_comments(goal, "task",
                                      last["comment_ts"] if last else None,
                                      client.identity)
    runway_now = latest_runway(con)
    picked = pick(con, goal_id, last,
                  candidates(con, goal, item_ids, comments, runway_now))
    if picked is None:
        return None

    pcfg = config.load(_project_id(con, goal))
    profile = planner_profile(pcfg)        # raises PlannerUnconfigured
    flight = in_flight(con, item_ids)
    finished_since = [r for r in settled_runs(con, item_ids)
                      if not last or (r["finished_at"] or "") > (last["created_at"] or "")]
    packet = build_packet(
        goal,
        delta=delta_entries(finished_since, comments),
        children=child_entries(children),
        issues=issue_entries(open_issues_for(issues, goal.get("projectPath"))),
        profiles=profile_entries(pcfg),
        runway_entries=runway_entries_for(runway_now),
        flight=flight_entries(flight))
    slug = names.generate_slug()
    decision = take_turn(profile, packet, slug=slug, turn=turn)
    # The comment watermark only ever moves forward, so a turn taken for some
    # other trigger cannot swallow a comment it never read.
    seen = [c["at"] for c in comments] + \
        ([last["comment_ts"]] if last and last["comment_ts"] else [])
    comment_ts = max(seen) if seen else None
    turn_id = log_turn(con, goal_id, trigger=picked["trigger"], key=picked["key"],
                       action=decision["action"], rationale=decision.get("rationale", ""),
                       slug=slug, profile=profile["name"],
                       wait_event=wait_event_for(decision), comment_ts=comment_ts,
                       packet_tokens=est_tokens(packet))
    result = apply_decision(con, cfg, client, goal, board, decision, slug, launcher)
    set_detail(con, turn_id, result)
    return {"goal": goal_id, "trigger": picked["trigger"], "key": picked["key"],
            "turn": turn_id, "slug": slug, "packet_tokens": est_tokens(packet),
            **result}


def _project_id(con, item: dict) -> str | None:
    hit = project.by_work_path(con, item.get("projectPath"))
    return hit.project_id if hit else None


def pass_once(cfg: dict, client, *, turn=None,
              launcher=supervise.spawn_supervisor,
              floor: int = TURN_FLOOR_SECONDS) -> list[dict]:
    """One conductor pass over every open goal. Never raises for one goal."""
    actions: list[dict] = []
    tasks = client.tasks()
    if tasks is None:
        return actions
    board = {t["id"]: t for t in tasks}
    issues = client.issues() or []
    con = db.connect()
    try:
        for goal in open_goals(tasks):
            try:
                took = conduct_goal(con, cfg, client, goal, board, issues,
                                    turn=turn, launcher=launcher, floor=floor)
            except PlannerUnconfigured as exc:
                print(f"dromond conductor: {goal['id']} has no planner: {exc}")
                actions.append({"goal": goal["id"], "action": "unconfigured",
                                "error": str(exc)})
                continue
            except PlannerTurnError as exc:
                print(f"dromond conductor: planner turn for {goal['id']} failed: {exc}")
                actions.append({"goal": goal["id"], "action": "turn_failed",
                                "error": str(exc)})
                continue
            if took is not None:
                actions.append(took)
    finally:
        con.close()
    return actions


# --- seam: findings.PLANNER (proposal alignment) -----------------------------

def alignment_planner(*, goal: dict, proposal: dict, run, cfg: dict | None = None,
                      turn=None) -> dict | None:
    """Judge one worker proposal against the goal, in a FRESH session.

    ``None`` means unevaluated, and an unevaluated proposal goes to the
    human — which is what happens for a planner that is unconfigured, a turn
    that fails, and, deliberately, a proposal this goal's own planner raised.
    Nothing approves itself.
    """
    goal_id = goal.get("id") or ""
    con = db.connect()
    try:
        raised_by = {run["slug"], run["session_ref"]} if run is not None else set()
        if raised_by & turn_slugs(con, goal_id):
            print(f"dromond conductor: {goal_id} proposal came from a planner "
                  "session; a planner may not judge its own proposal")
            return None
        cfg = config.load(run["project_id"] if run is not None else None) \
            if cfg is None else cfg
        profile = planner_profile(cfg)     # PlannerUnconfigured => unevaluated
        packet = build_packet(
            goal,
            delta=[_entry("", f"- proposal from run {run['id'] if run else '?'}: "
                              f"{proposal.get('title', '')} — "
                              f"{proposal.get('why') or '(no rationale given)'}")],
            children=[], issues=[], profiles=[], runway_entries=[],
            flight=[])
        slug = names.generate_slug()
        decision = take_turn(profile, packet, slug=slug,
                             instructions=ALIGNMENT_INSTRUCTIONS, turn=turn,
                             actions=("aligned", "pivot"), key="verdict")
        # take_turn speaks {action}; the seam's contract speaks {verdict}.
        verdict = decision["action"] if decision["action"] in ("aligned", "pivot") \
            else None
        log_turn(con, goal_id, trigger="proposal",
                 key=f"run:{run['id'] if run else '?'}:{proposal.get('title', '')}"[:200],
                 action=f"align:{verdict or 'unevaluated'}",
                 rationale=decision.get("rationale", ""), slug=slug,
                 profile=profile["name"], packet_tokens=est_tokens(packet))
        if verdict is None:
            return None
        return {"verdict": verdict, "rationale": decision.get("rationale", "")}
    finally:
        con.close()


# --- seam: observer.planner_review (judgment failures) -----------------------

_DEFERRED_REVIEW = observer.planner_review


def judgment_turn(con, run_id: int, reason: str, *, detail: str | None = None,
                  cfg: dict | None = None, turn=None,
                  launcher=supervise.spawn_supervisor) -> dict:
    """A run finished and the work is not right (DESIGN §7 → §10).

    One planner turn decides: re-brief (dispatch), split (propose), or hand
    it to the human (ask_human). Anything the turn cannot supply — no goal,
    no planner, Work unreachable, a failed turn — falls back to the original
    seam, which records the request and escalates. A judgment failure is
    never silently absorbed.
    """
    run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    cfg = config.load(run["project_id"]) if (cfg is None and run is not None) else cfg
    client = work_client.from_cfg(cfg or {})
    goal = None
    board: dict = {}
    if run is not None and client is not None and (run["work_item"] or "").startswith("W-"):
        tasks = client.tasks() or []
        board = {t["id"]: t for t in tasks}
        item = board.get(run["work_item"])
        goal = item if item and is_goal(item) else board.get((item or {}).get("parentId") or "")
        if goal is not None and not is_goal(goal):
            goal = None
    if goal is None:
        return _DEFERRED_REVIEW(con, run_id, reason, detail=detail, cfg=cfg)
    pcfg = config.load(_project_id(con, goal))
    try:
        profile = planner_profile(pcfg)
        children = children_of(board.values(), goal["id"])
        packet = build_packet(
            goal,
            delta=[_entry(run["finished_at"] or "",
                          f"- run {run_id} {run['status']} but the work is not "
                          f"right: {reason} {detail or ''}")],
            children=child_entries(children),
            issues=[], profiles=profile_entries(pcfg),
            runway_entries=runway_entries_for(latest_runway(con)),
            flight=flight_entries(in_flight(con, [goal["id"]] +
                                            [c["id"] for c in children])))
        slug = names.generate_slug()
        decision = take_turn(profile, packet, slug=slug,
                             instructions=JUDGMENT_INSTRUCTIONS, turn=turn,
                             actions=("dispatch", "propose", "ask_human"))
    except (PlannerUnconfigured, PlannerTurnError) as exc:
        print(f"dromond conductor: judgment turn for run {run_id} unavailable: {exc}")
        return _DEFERRED_REVIEW(con, run_id, reason, detail=detail, cfg=cfg)
    if decision["action"] not in ("dispatch", "propose", "ask_human"):
        return _DEFERRED_REVIEW(con, run_id, reason, detail=detail, cfg=cfg)
    observer.record(con, run_id, "planner", decision["action"],
                    decision.get("rationale", reason), {"goal": goal["id"]})
    turn_id = log_turn(con, goal["id"], trigger="judgment", key=f"run:{run_id}",
                       action=decision["action"],
                       rationale=decision.get("rationale", ""), slug=slug,
                       profile=profile["name"], wait_event=wait_event_for(decision),
                       packet_tokens=est_tokens(packet))
    result = apply_decision(con, cfg, client, goal, board, decision, slug, launcher)
    set_detail(con, turn_id, result)
    return {"action": decision["action"], "run": result.get("run"),
            "reason": decision.get("rationale", reason), "goal": goal["id"],
            "turn": turn_id}


def attach() -> None:
    """Fill both planner seams in this process (W-0099). Idempotent.

    Called by the detached supervisor entry point, which is the process where
    a completion files proposals and where a judgment failure is noticed.
    """
    findings.PLANNER = alignment_planner
    observer.planner_review = judgment_turn
