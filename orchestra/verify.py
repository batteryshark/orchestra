"""Sign-off is a run (W-0269): verification earns done, or blocks with reasons.

When the sweeper's landed fact moves a swept item to ``review``, the runner
records a verification run — ON by default (W-0299), no human asks — on
``[verify] profile``, defaulting to the one tier-1 (workhorse) profile and
never the worker's profile or session, and executes each acceptance
criterion's stated method against landed main. All pass: it ticks them and appends ``fact: verified``, which
reads **done** on top of the worker's landing. Any fail: ``fact: halted``
naming those criteria, which reads blocked. Surface lane, no ring.

The facts belong to the WORKER's claim window: a sign-off never claims, so a
human move made in the meantime dismisses this run's narrative too.

ponytail: stated methods are mechanical (command / grep / test / read), so
code runs them. A prose method — one whose first word is no executable —
is NOT run and NOT a veto: it reports as needing judgment and the item
stays in review for the human. On 2026-08-25 "harness fixture test" and
"click through" were exec'd as commands, failed with ENOENT, and blocked
two items whose work was green — the exact damage a read-and-report
verifier must never do.

A criterion that needs judgment can get it (W-0307): when `[verify]
second_opinion` names a profile, the pass runs a capped dialogue between
the verify seat and that second voice — message cap, output budget, and
per-turn timeout enforced in code, the yeschef rooms lesson (W-0306) —
recorded as `dialogue` control turns. The verdict is advisory: it may
tick a criterion, never fail one, and a dialogue that settles every open
criterion completes the pass exactly as an all-mechanical one would.

Evidence this checkout does not hold is declined, never failed (W-0310):
a read or test method whose explicit target resolves outside the run
workdir — or a test file the checkout does not contain — is not run; the
criterion is declined through Work with the reason, and a criterion the
worker already declined stays as the worker recorded it. Run 54 executed
Orchestra test methods inside the Work checkout, read exit 5 as failure,
and blocked a verified item. Declined criteria join no dialogue (the
second voice cannot see another repository either) and, like unchecked
ones, leave the item in review with no fact: the pass certifies only
evidence it inspected. A bare ``test`` that collects zero tests is
judgment too (W-0311): an empty discovery proves nothing either way.
"""
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from orchestra import config, db, observer, project, supervise
from orchestra.work_client import WorkClient, fact_line, verifier_identity

# The full suite is a legitimate stated method and takes ~70s on this
# machine; 60s manufactured a false verification failure (W-0308, run 45).
METHOD_TIMEOUT = 600
REQUESTED_BY = "verify"
VERIFY_TIER = 1  # workhorse — the "cheaper model" the default promises (W-0299)
# The dialogue's hard caps (W-0307). Configurable via [verify]
# dialogue_messages / dialogue_budget; the per-turn ceiling is
# observer.TURN_TIMEOUT. Enforced in the loop, never hoped for.
DIALOGUE_MESSAGES = 4
DIALOGUE_BUDGET = 8000  # total reply characters across the whole dialogue


def _default_profile(pcfg: dict, item_id: str) -> str | None:
    """The one ENABLED tier-1 profile, else None with the fix printed.

    The same volunteer rule as ``observer.profile_name``: ambiguity is
    reported, never guessed — two tier-1 profiles is a config the owner
    resolves, not something to pick for them.
    """
    cheap = sorted(name for name, p in config.enabled_profiles(pcfg).items()
                   if config.tier_of(p.get("tier")) == VERIFY_TIER)
    if len(cheap) == 1:
        return cheap[0]
    fix = (f"several tier-1 profiles ({', '.join(cheap)}) — set "
           '[verify] profile = "NAME"' if cheap else
           'no verify profile — set [verify] profile = "NAME" or mark one '
           "profile tier = 1 (workhorse)")
    print(f"orchestra verify: {item_id} skipped — {fix}")
    return None


def after_report(con, cfg: dict, client: WorkClient, actions: list) -> None:
    """Sign off each item this pass just moved to review."""
    for action in list(actions):
        if action.get("action") != "report" or action.get("to") != "review":
            continue
        try:
            sign_off(con, cfg, client, action["item"], action["run"], actions)
        except Exception as exc:
            print(f"orchestra verify: {action['item']} failed: {exc}")


def sign_off(con, cfg: dict, client: WorkClient, item_id: str, worker_id: int,
             actions: list) -> None:
    worker = con.execute("SELECT * FROM runs WHERE id=?", (worker_id,)).fetchone()
    if worker is None or worker["requested_by"] == REQUESTED_BY:
        return
    already = con.execute(
        "SELECT id FROM runs WHERE work_item=? AND requested_by=?",
        (item_id, REQUESTED_BY)).fetchone()
    if already:
        return
    pcfg = config.load(worker["project_id"]) if worker["project_id"] else cfg
    vcfg = dict(pcfg.get("verify") or {})
    work = dict(pcfg.get("work") or {})
    # W-0299: on by default — every landing gets this pass without a human
    # asking. An explicit [verify] enabled wins; the legacy [work] verify key
    # is still honored, so a pre-W-0299 config keeps meaning what it said.
    if not vcfg.get("enabled", work.get("verify", True)):
        return
    profile_name = (vcfg.get("profile") or work.get("verify_profile")
                    or "").strip()
    if not profile_name:
        profile_name = _default_profile(pcfg, item_id)
        if not profile_name:
            return
    if profile_name == worker["profile"]:
        print(f"orchestra verify: {item_id} skipped — verify_profile is the "
              f"worker's profile ({profile_name})")
        return
    try:
        profile = config.staff_profile(pcfg, profile_name)
    except SystemExit as exc:
        print(f"orchestra verify: {item_id} not staffed: {exc}")
        return
    root = project.root_for(con, worker)
    run, _blocked = _insert(con, worker, root, profile_name, profile, item_id)
    if run is None:
        return
    run_id, slug = int(run["id"]), run["slug"]
    verifier = WorkClient(client.api_url, identity=verifier_identity(slug),
                          timeout=client.timeout)
    try:
        results = _execute(verifier, item_id, root)
        failed = [r for r in results if r["ok"] is False]
        declined = [r for r in results if r.get("declined")]
        unchecked = [r for r in results
                     if r["ok"] is None and not r.get("declined")]
        dialogue_reason = None
        # A mechanical failure already halts; spending model turns arguing
        # about the rest would decorate a blocked item. Judgment only when
        # judgment is the only thing missing.
        if unchecked and not failed:
            seats = _dialogue_seats(pcfg, worker["profile"], profile_name,
                                    profile)
            if seats:
                item = verifier.task(item_id) or {"id": item_id}
                met, dialogue_reason = _dialogue(
                    con, pcfg, item, unchecked, worker, seats,
                    worker["project_id"])
                for r in unchecked:
                    if met.get(r["index"]):
                        verifier.check_task_item(item_id, "acceptance",
                                                 r["index"], checked=True)
                        verifier.log_task(
                            item_id, f"{r['text']}: judged met by the "
                                     "second-opinion dialogue")
                        r["ok"] = True
                        r["note"] = "judged met by the second-opinion dialogue"
                unchecked = [r for r in results
                             if r["ok"] is None and not r.get("declined")]
        _writeback(verifier, item_id, run_id, results, failed, unchecked,
                   declined, dialogue_reason)
        # What code cannot check, code must not veto: unchecked and declined
        # criteria leave the item in review for the human, with no fact.
        if failed:
            status, target = "failed", "blocked"
        elif unchecked or declined:
            status, target = "done", None
        else:
            status, target = "done", "done"
        summary = _summary(item_id, results, failed, unchecked, declined)
    except Exception as exc:
        status, summary, target = "failed", str(exc)[:300], None
        print(f"orchestra verify: {item_id} aborted: {exc}")
    _finalize(con, run_id, status, summary)
    actions.append({"action": "verify", "item": item_id, "run": run_id,
                    "to": target})


def stated_method(text: str) -> str:
    """The method a criterion names: the clause after an em dash, else all of it."""
    for sep in (" — ", " – ", " -- "):
        if sep in text:
            return text.rsplit(sep, 1)[1].strip()
    return text.strip()


def run_method(root: Path, method: str) -> tuple[bool | None, str]:
    """Execute one stated method against landed main.

    Returns ``(ok, one-line note)`` — ``ok`` is None for a prose method,
    which code can neither pass nor fail."""
    kind, arg = _classify(method)
    if kind == "prose":
        return None, f"{method}: not a mechanical method — needs judgment"
    root = Path(root).resolve()
    if kind == "read":
        path = (root / arg).resolve()
        if not path.is_relative_to(root):
            return False, f"read {arg}: path outside project"
        if path.is_file():
            return True, f"read {arg}: present"
        return False, f"read {arg}: not a file"
    if kind == "grep":
        r = subprocess.run(
            ["grep", "-R", "-n", "-I", "--exclude-dir=.git", "-e", arg, "."],
            cwd=str(root), capture_output=True, text=True, timeout=METHOD_TIMEOUT)
        note = f"grep {arg}: " + ("matched" if r.returncode == 0 else "no match")
        return r.returncode == 0, note
    if kind == "test":
        cmd = [sys.executable, "-m", "unittest"] + (shlex.split(arg) if arg else ["discover"])
        ok, note = _command(root, cmd, method)
        # A bare test that collects nothing proves nothing: unittest exits 5
        # (NO TESTS RAN) in a checkout with no Python tests, and run 54 read
        # that as failure (W-0311). Targeted tests keep their verdicts.
        if not arg and note.endswith("exit 5"):
            return None, f"{method}: no tests ran in this checkout — needs judgment"
        return ok, note
    return _command(root, shlex.split(method), method)


def _classify(method: str) -> tuple[str, str]:
    low = method.lower()
    if low.startswith("read "):
        return "read", method.split(None, 1)[1]
    if low.startswith("grep "):
        return "grep", method.split(None, 1)[1]
    if low == "test" or low.startswith("test "):
        return "test", method.split(None, 1)[1] if " " in method else ""
    # A method is a command only when its first word IS one; anything else
    # is prose and gets judgment, not execution.
    try:
        head = shlex.split(method)[0] if method.strip() else ""
    except ValueError:
        return "prose", method
    if head and (shutil.which(head) or Path(head).is_file()):
        return "command", method
    return "prose", method


def _command(root: Path, cmd: list[str], label: str) -> tuple[bool, str]:
    if not cmd:
        return False, f"{label}: empty command"
    try:
        r = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True,
                           timeout=METHOD_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, f"{label}: timed out after {METHOD_TIMEOUT}s"
    except OSError as exc:
        return False, f"{label}: {exc}"
    return r.returncode == 0, f"{label}: exit {r.returncode}"


def _external_target(root: Path, method: str) -> str | None:
    """The decline reason when the method's explicit evidence target is not
    under ``root``, else None (W-0310).

    Only read and test methods name a target code can resolve; grep
    patterns, bare ``test``, and generic commands stay ambiguous and run as
    before — a decline is reserved for evidence provably elsewhere. A read
    target that is inside but absent stays a failure: presence IS what a
    read asserts. A test target that is absent is declined: the criterion
    asserts the tests pass, and this checkout does not hold them.
    """
    kind, arg = _classify(method)
    if kind not in ("read", "test") or not arg:
        return None
    try:
        target = shlex.split(arg)[0] if kind == "test" else arg
    except (ValueError, IndexError):
        return None
    if kind == "test" and target == "discover":
        return None
    rel = (target if kind == "read" or "/" in target or target.endswith(".py")
           else target.replace(".", "/"))
    root = Path(root).resolve()
    path = (root / rel).resolve()
    if not path.is_relative_to(root):
        return f"{method}: {target} resolves outside this checkout — not attempted"
    if kind == "test" and not (path.exists() or path.with_suffix(".py").exists()):
        return (f"{method}: {target} is not in this checkout — "
                "the evidence lives in another repository")
    return None


def _execute(client: WorkClient, item_id: str, root: Path) -> list[dict]:
    task = client.task(item_id) or {}
    results = []
    for index, entry in enumerate(task.get("acceptanceCriteria") or []):
        text = (entry.get("text") or "").strip()
        method = stated_method(text)
        declined = False
        if entry.get("declined"):
            # The worker answered this one on the record: declined, with a
            # reason. Run 54 re-ran such a criterion in the wrong checkout
            # and failed it — a decline stays the answer (W-0310).
            declined = True
            ok, note = None, (f"{text}: declined by the worker "
                              f"({entry.get('reason') or 'no reason'}) — left as recorded")
        elif not method:
            ok, note = None, f"{text or '(empty)'}: no stated method — needs judgment"
        elif reason := _external_target(root, method):
            declined = True
            ok, note = None, reason
            client.check_task_item(item_id, "acceptance", index,
                                   reason=reason[:1000])
        else:
            try:
                ok, note = run_method(root, method)
            except subprocess.TimeoutExpired:
                ok, note = False, f"{method}: timed out after {METHOD_TIMEOUT}s"
        client.log_task(item_id, note[:19000])
        if ok:
            client.check_task_item(item_id, "acceptance", index, checked=True)
        results.append({"index": index, "text": text, "ok": ok, "note": note,
                        "declined": declined})
    return results


# --- the second-opinion dialogue (W-0307) ------------------------------------

def _dialogue_seats(pcfg: dict, worker_profile: str, verify_name: str,
                    verify_profile: dict):
    """The two voices, or None: an unset second seat means the dialogue is
    off and the pass behaves exactly as before. The second voice opens —
    the verify seat already spoke through the mechanical pass."""
    vcfg = dict(pcfg.get("verify") or {})
    name = str(vcfg.get("second_opinion") or "").strip()
    if not name:
        return None
    if name in (worker_profile, verify_name):
        print(f"orchestra verify: second opinion skipped — {name} is "
              "already at the table")
        return None
    try:
        return [(name, config.staff_profile(pcfg, name)),
                (verify_name, verify_profile)]
    except SystemExit as exc:
        print(f"orchestra verify: second opinion not staffed: {exc}")
        return None


def _dialogue_prompt(packet: str, transcript: list, speaker: str,
                     last: bool) -> str:
    said = "\n\n".join(f"[{who}] {text}" for who, text in transcript)
    ask = ("Reply ONLY with the verdict JSON now: "
           '{"criteria": [{"index": N, "verdict": "met" or "unclear"}]} '
           "with one entry per numbered criterion." if last else
           "Answer in a few sentences. When you are already satisfied, end "
           "early by emitting the verdict JSON "
           '{"criteria": [{"index": N, "verdict": "met" or "unclear"}]}.')
    role = ("Assess each criterion against the evidence and say what would "
            "make you doubt it." if not transcript else
            "Answer the doubts raised so far with evidence, not reassurance.")
    return (f"You are {speaker}, one of two reviewers in a bounded dialogue.\n"
            f"{packet}\n\n"
            + (f"The dialogue so far:\n{said}\n\n" if said else "")
            + f"{role}\n{ask}")


def _dialogue(con, pcfg: dict, item: dict, unchecked: list[dict], worker,
              seats: list, project_id: str | None):
    """Run the capped exchange. Returns (met, reason): ``met`` maps a
    criterion index to True when the dialogue judged it met, and ``reason``
    says how the dialogue ended. Advisory only — the caller ticks, never
    fails, and every cap breach is a recorded ending, not an exception."""
    vcfg = dict(pcfg.get("verify") or {})
    cap = max(1, int(vcfg.get("dialogue_messages", DIALOGUE_MESSAGES) or 0))
    budget = max(1, int(vcfg.get("dialogue_budget", DIALOGUE_BUDGET) or 0))
    named = "\n".join(f"{r['index']}. {r['text']}" for r in unchecked)
    packet = (f"Work item {item.get('id')}: {item.get('title') or ''}\n"
              f"The worker's handoff:\n{(worker['summary'] or '')[:2000]}\n\n"
              "These acceptance criteria carry no mechanical method; judge "
              f"each against the handoff and the landed work:\n{named}")
    transcript, spent = [], 0
    for turn in range(cap):
        speaker, profile = seats[turn % 2]
        prompt = _dialogue_prompt(packet, transcript, speaker, turn == cap - 1)
        try:
            text = observer.model_turn(profile, prompt, layer="dialogue",
                                       con=con, project_id=project_id)
        except observer.ObserverTurnError as exc:
            return {}, f"dialogue ended on a failed turn: {exc}"
        transcript.append((speaker, text.strip()))
        spent += len(text)
        verdict = observer.last_json_object(text, "criteria")
        if verdict:
            met = {}
            for entry in verdict.get("criteria") or []:
                if not isinstance(entry, dict):
                    continue
                try:
                    index = int(entry.get("index"))
                except (TypeError, ValueError):
                    continue
                if entry.get("verdict") == "met":
                    met[index] = True
            return met, (f"second-opinion dialogue: verdict after "
                         f"{turn + 1} message(s)")
        if spent >= budget:
            return {}, (f"second-opinion dialogue ended: output budget "
                        f"({budget} chars) reached without a verdict")
    return {}, (f"second-opinion dialogue ended: message cap ({cap}) "
                "reached without a verdict")


def _summary(item_id: str, results: list[dict], failed: list[dict],
             unchecked: list[dict], declined: list[dict]) -> str:
    if failed:
        names_ = ", ".join(r["text"] or f"AC{r['index']}" for r in failed)
        return f"Blocked. {item_id} failed: {names_}"[:300]
    if unchecked or declined:
        parts = []
        if unchecked:
            parts.append(f"{len(unchecked)} criteria have no mechanical method")
        if declined:
            parts.append(f"{len(declined)} criteria's evidence is not in "
                         "this checkout")
        return (f"Inconclusive. {item_id}: " + "; ".join(parts)
                + "; left in review for judgment.")[:300]
    return f"Verified. {item_id} is done."


def _writeback(client: WorkClient, item_id: str, run_id: int,
               results: list[dict], failed: list[dict],
               unchecked: list[dict], declined: list[dict],
               dialogue_reason: str | None = None) -> None:
    tag = f"[{client.identity}/{run_id}]"
    if failed:
        named = "\n".join(f"- {r['text']}: {r['note']}" for r in failed)
        body = (f"{tag} Blocked. {item_id} failed verification.\n\n"
                f"{named}\n\nOpen the blocked lane.")
        fact = fact_line(tag, "halted", reason=(
            f"{item_id} failed verification: " + ", ".join(
                r["text"] or f"AC{r['index']}" for r in failed))[:300])
    elif unchecked or declined:
        named = "\n".join(f"- {r['text']}: {r['note']}"
                          for r in unchecked + declined)
        judged = f"\n\n{dialogue_reason}." if dialogue_reason else ""
        body = (f"{tag} Inconclusive. {item_id}: this pass could not judge "
                f"these criteria.\n\n"
                f"{named}{judged}\n\nThe item stays in review; sign-off is yours.")
        fact = None
    else:
        body = (f"{tag} Verified. {item_id} is done.\n\n"
                + ("\n".join(r["note"] for r in results) or "No acceptance criteria.")
                + "\n\nNothing waits.")
        fact = fact_line(tag, "verified")
    client.log_task(item_id, body[:19000])
    if fact:
        client.log_task(item_id, fact)


def _insert(con, worker, root: Path, profile_name: str, profile: dict,
            item_id: str):
    title = f"Verify {item_id}"[:80]
    return supervise.create_run(
        con, profile=profile_name, backend=profile["backend"],
        model=profile.get("model"), title=title, requested_by=REQUESTED_BY,
        workdir=str(root), project_id=worker["project_id"], status="running",
        work_item=item_id, parent_run=int(worker["id"]), pause_gate=False)


def _finalize(con, run_id: int, status: str, summary: str) -> None:
    con.execute(
        "UPDATE runs SET status=?, summary=?, finished_at=?, work_reported_at=? "
        "WHERE id=?", (status, summary, db.now(), db.now(), run_id))
    con.commit()
