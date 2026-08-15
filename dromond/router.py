"""The staffing turn (W-0183): which profile runs a swept item.

Every swept item used to be staffed with the single ``[work] profile``, so a
trivial item and a hard one got the same model. Work cannot fix that: CONTRACT
§6 keeps backends, models and harnesses out of Work entirely, and a tag like
``profile:opus-high`` would leak exactly what the contract forbids. The
decision therefore belongs HERE.

On claim, one bounded turn on a CHEAP profile — ``[work] router`` — reads the
item and names the profile to staff, out of the profiles the project ENABLES
(W-0187), using the ``tier``/``priority`` metadata that exists for exactly
this (W-0181: 1 workhorse / 2 generalist / 3 heavy, priority ordering within a
tier). Same trade the spin observer makes: one cheap call, so the expensive
one is only paid when the item needs it.

**Routing must never block a dispatch.** No ``[work] router``, one profile
enabled, a router profile the project disabled, a dead process, a reply that
is not JSON, a name outside the enabled set — every one of them returns the
``[work] profile`` unchanged, with the reason it fell back. Nothing here
invents the judgement, which is ``conductor.parse_decision``'s posture:
anything that is not exactly a valid answer is a NON-answer.

The enabled set is enforced twice on purpose — ``parse_choice`` will not read
a name that is not in it, and the name it does read is still resolved through
``config.staff_profile``, the one gate a staffing moment goes through.
"""
from dromond import config, observer, profiles as profiles_mod, runway

# A claim waits on this turn, so it is deliberately shorter than the spin
# observer's 180s: the item is already `in_progress` on the board while it
# runs. ponytail: one turn per claimed item, taken serially inside the sweep
# pass. A pass claiming twenty items at once pays twenty times, in sequence;
# upgrade path is a thread pool here, when a board that wide exists.
TURN_TIMEOUT = 90

INSTRUCTIONS = """\
You are Dromond's staffing router. You have ONE job: pick which model profile \
runs the work item below.

You are not doing the work and you are not planning it. Read the item, judge \
how much model it actually needs, and name the CHEAPEST profile that can \
genuinely do it. Over-staffing a trivial item wastes the expensive model that \
a hard item will need later; under-staffing a hard one wastes the whole run.

How to read the list:
- tier 1 (workhorse) = well-defined, bounded work with the answer in sight.
- tier 2 (generalist) = ordinary work that needs judgement.
- tier 3 (heavy) = the hardest thinking: ambiguous, cross-cutting, or design.
- priority orders profiles WITHIN a tier — LOWER is more preferred.
- role and note are the owner's own words about what a profile is for.
- runway is that provider's measured headroom. A profile with almost none \
left is a bad choice even when its tier fits.

You may ONLY name a profile from the list. Any other name is refused and the \
project's default is staffed instead.

Reply with ONE JSON object and nothing else:
{"profile": "<exact name from the list>", "reason": "<one line: what about \
this item needs that profile>"}
"""


def profile_line(name: str, profile: dict, polls: dict) -> str:
    """One profile as the router reads it: tier, priority, role, headroom
    note and the measured runway of the provider it spends against."""
    backend = profile.get("backend", "opencode")
    bits = [f"- {name}: {backend}"]
    if profile.get("model"):
        bits.append(str(profile["model"]))
    tier = config.tier_of(profile.get("tier"))
    bits.append(f"tier {tier} ({config.TIERS[tier]})" if tier else "tier unset")
    bits.append(f"priority {config.priority_of(profile)}")
    if profile.get("role"):
        bits.append(f"role: {profile['role']}")
    if profile.get("note"):
        age = profiles_mod.note_age(profile.get("note_at"))
        bits.append(f"note: {profile['note']}" + (f" ({age})" if age else ""))
    poll = polls.get(runway.provider_of(backend, profile.get("model")))
    bits.append(f"runway: {runway.entry_text(poll) if poll else 'no reading'}")
    return " ".join(bits)


def build_packet(snapshot: str, enabled: dict, polls: dict) -> str:
    """Instructions, the item, and the profiles it may be staffed with.

    The item arrives as the sweeper's own ``render_snapshot`` — title, goal,
    requirements, acceptance criteria and the recent thread, already capped —
    so the router reads exactly what the worker's brief will carry.

    Profiles are listed in routing order (priority first, `nice`-style), the
    same order the dashboard and the conductor's packet use.
    """
    entries = sorted(enabled.items(),
                     key=lambda kv: (config.priority_of(kv[1]), kv[0]))
    lines = "\n".join(profile_line(name, p, polls) for name, p in entries)
    return (f"{INSTRUCTIONS}\n--- the work item ---\n{snapshot}\n\n"
            f"--- profiles this project has enabled ---\n{lines}\n")


def parse_choice(text: str, enabled: dict) -> tuple[str | None, str]:
    """``(profile name, reason)``; the name is None when the reply is not an
    answer, and the reason then says why it was not.

    Bounded by the enabled set, case-insensitively: a name outside it is a
    non-answer, never a name to go looking for.
    """
    by_lower = {name.lower(): name for name in enabled}
    found = observer.last_json_object(text, "profile")
    if not found:
        return None, "the staffing turn's reply named no profile"
    named = str(found.get("profile") or "").strip()
    reason = str(found.get("reason") or found.get("rationale") or "").strip()
    if named.lower() not in by_lower:
        return None, (f"the staffing turn named {named!r}, which this project "
                      f"has not enabled ({', '.join(sorted(enabled)) or 'none'})")
    return by_lower[named.lower()], reason[:2000] or "(no reason given)"


def _one_line(exc) -> str:
    return (str(exc).strip().splitlines() or [""])[0]


def _choose(con, cfg: dict, snapshot: str, name: str, profile: dict, *,
            turn=None) -> tuple[str, dict, str | None]:
    """The staffing decision. ``choose`` owns its fail-safe boundary."""
    router_name = str((cfg.get("work") or {}).get("router") or "").strip()
    if not router_name:
        return name, profile, None
    enabled = config.enabled_profiles(cfg)
    if len(enabled) < 2:
        return name, profile, (
            f"skipped the staffing turn: {len(enabled)} profile enabled, "
            f"so there is nothing to decide; staffed {name}")
    try:
        router = config.staff_profile(cfg, router_name)
    except SystemExit as exc:
        return name, profile, (f"the router profile {router_name!r} is not "
                               f"staffable ({_one_line(exc)}); staffed {name}")
    packet = build_packet(snapshot, enabled, {p["provider"]: p
                                              for p in runway.latest_polls(con)})
    try:
        text = (turn or observer.model_turn)(router, packet, timeout=TURN_TIMEOUT)
    # A dead binary, a wedged process, a backend that changed its output: the
    # dispatch goes either way, so nothing this turn does may propagate.
    # SystemExit counts too — `runners.build_cmd` exits for CLI use.
    except (Exception, SystemExit) as exc:
        return name, profile, (f"the staffing turn could not run "
                               f"({_one_line(exc)}); staffed {name}")
    chosen, reason = parse_choice(text, enabled)
    if chosen is None:
        return name, profile, f"{reason}; staffed {name}"
    try:
        # The gate, always: `parse_choice` bounded the NAME, and this is what
        # actually resolves a staffed profile anywhere in Dromond.
        picked = config.staff_profile(cfg, chosen)
    except SystemExit as exc:
        return name, profile, (f"{chosen!r} is not staffable "
                               f"({_one_line(exc)}); staffed {name}")
    if chosen == name:
        return name, profile, f"kept {name}: {reason}"
    return chosen, picked, f"staffed {chosen} over {name}: {reason}"


def choose(con, cfg: dict, snapshot: str, name: str, profile: dict, *,
           turn=None) -> tuple[str, dict, str | None]:
    """SEAM (W-0183), called by ``sweeper._claim``.

    Returns ``(profile_name, profile, reason)``. ``reason`` is None only when
    ``[work] router`` is unset — routing is off, so there is no decision to
    put on the board. Every other outcome carries its one line, whether it
    routed or fell back to the ``[work] profile`` it was handed.

    Never raises. A staffing turn that fails is the router's problem, never
    the item's, so the caller always gets a profile it can dispatch.
    """
    try:
        return _choose(con, cfg, snapshot, name, profile, turn=turn)
    except (Exception, SystemExit) as exc:
        detail = _one_line(exc) or type(exc).__name__
        return name, profile, (f"the staffing turn failed ({detail}); "
                               f"staffed {name}")
