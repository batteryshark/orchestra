"""The two messaging verbs (DESIGN §6): ``tell`` and ``ask``.

``tell`` is non-blocking. Exec runs receive it at a completed action boundary
after stop and resume. ACP runs use their live protocol channel. Its row kind
stays ``interrupt`` because both transports share the same delivery record.
``orchestra interrupt --now`` is the emergency variant.

``ask`` is blocking with a declared fallback. The worker files a question,
ends its turn, and the harness's Stop hook (``orchestra hook``) holds the
session open while Orchestra waits on a Nod decision request. The answer is
injected back through the hook as the next instruction. Nod's ``expires_at``
IS the declared fallback: when it passes, the worker is told to proceed on
its own judgement rather than being left hanging.

Both sides of an ``ask`` belong in the source item's thread, because a
decision that only exists on a phone is not a record. This module does not
put them there: it records the question and the answer as ``ask``/``answer``
rows on the run, and the source's ADAPTER carries them (CONTRACT §7
Enforcement). Neither row is deleted when it is carried, so the mirror is a
read, not a queue.

Undeliverable is a state, not a deletion. A message whose run ended before
delivery is marked with a reason and surfaced (``orchestra show``,
``orchestra traces messages``, the dashboard). It is never re-aimed at a later
run: a correction handed to a run that never saw the context it referred to
is worse than no correction.

Scope (DESIGN §6): human-to-run and run-to-human. Arbitrary run-to-run
messaging is out, child launch is not implemented, and ``ask`` targets the
human only.
"""
import os
import sqlite3

from orchestra import db, nod, traces

DELIVERY_KIND = "interrupt"   # a tell, delivered by exec boundary or live ACP
ASK_KIND = "ask"              # the run's question (outbound)
ANSWER_KIND = "answer"        # the human's answer (inbound, injected)

# A Stop hook cannot outlive its own harness timeout, so an ask waits for at
# most this long no matter what [nod] expires_after says.
MAX_ASK_SECONDS = 35_400      # hook timeout (36000s) minus a 10-minute margin
DEFAULT_ASK_SECONDS = 86_400


class RunClosed(RuntimeError):
    """A delivery target became terminal before message admission."""


# --- tell -------------------------------------------------------------------

def queue_tell(con: sqlite3.Connection, run_id: int, sender: str, body: str,
               log_path: str | None, *, boundary: bool = True,
               commit: bool = True) -> int:
    """Record a message for delivery at the run's next safe boundary.

    ``boundary=False`` is the ACP transport (W-0104, DESIGN §6): the run has
    a live protocol channel, so there is no boundary to wait for — the
    supervisor steers it in mid-turn. The row then carries NO
    ``delivery_offset``, which is what makes the delivery state honest: the
    exec boundary machinery ignores it (``supervise._pending_delivery_offset``
    filters on that column) and ``traces.run_messages`` stops badging it
    ``pending_boundary``, because no boundary is pending.

    ``commit=False`` leaves this function's transaction open so a caller can
    attach its own row to the same admission — the pattern ``create_run``
    already uses. The Work adapter's ferry needs it: its thread watermark and
    the message that explains it must land together or not at all, and after
    schema v21 that watermark is the adapter's row to write, not this one's.
    """
    if con.in_transaction:
        raise RuntimeError("message admission requires a clean transaction")
    offset = None
    if boundary:
        try:
            offset = os.path.getsize(log_path) if log_path else 0
        except OSError:
            offset = 0
    con.execute("BEGIN IMMEDIATE")
    try:
        run = con.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        if run is None or run["status"] in db.RUN_TERMINAL:
            con.rollback()
            state = run["status"] if run else "missing"
            raise RunClosed(f"run {run_id} is {state}")
        cur = con.execute(
            "INSERT INTO messages(run_id, sender, body, kind, created_at, "
            "delivery_offset) VALUES(?,?,?,?,?,?)",
            (run_id, sender, body, DELIVERY_KIND, db.now(), offset))
        if commit:
            con.commit()
    except BaseException:
        if con.in_transaction:
            con.rollback()
        raise
    return int(cur.lastrowid)


def claim_pending(con: sqlite3.Connection, run_id: int) -> list:
    """Atomically take every undelivered message for a run and mark it
    delivered. Both delivery paths (the supervisor's resume and the Stop
    hook) call this, so a message is handed over exactly once."""
    con.commit()  # no implicit transaction may be open under BEGIN IMMEDIATE
    con.execute("BEGIN IMMEDIATE")
    rows = list(con.execute(
        "SELECT id, sender, body FROM messages WHERE run_id=? AND kind=? "
        "AND delivered_at IS NULL AND undeliverable_at IS NULL ORDER BY id",
        (run_id, DELIVERY_KIND)))
    if rows:
        con.execute(
            "UPDATE messages SET delivered_at=? WHERE run_id=? AND kind=? "
            "AND delivered_at IS NULL AND undeliverable_at IS NULL",
            (db.now(), run_id, DELIVERY_KIND))
    con.execute("COMMIT")
    for row in rows:  # the injection belongs in the trace (DESIGN §7)
        traces.record_injection(con, run_id, row["sender"], row["body"])
    return rows


def mark_undeliverable(con: sqlite3.Connection, run_id: int, reason: str) -> int:
    """Mark every still-queued message for a run. Returns how many.

    Called during finalization: the run is over, so nothing will deliver
    these. Replays are harmless because already-marked rows do not match.
    The caller owns the transaction so the run result and delivery state can
    be committed together.
    """
    cur = con.execute(
        "UPDATE messages SET undeliverable_at=?, undeliverable_reason=? "
        "WHERE run_id=? AND kind IN (?,?) AND delivered_at IS NULL "
        "AND undeliverable_at IS NULL",
        (db.now(), reason[:500], run_id, DELIVERY_KIND, ANSWER_KIND))
    return cur.rowcount


def undeliverable(con: sqlite3.Connection, run_id: int | None = None) -> list:
    """Marked-undelivered messages, newest last. The surfacing query."""
    if run_id is None:
        return list(con.execute(
            "SELECT * FROM messages WHERE undeliverable_at IS NOT NULL ORDER BY id"))
    return list(con.execute(
        "SELECT * FROM messages WHERE run_id=? AND undeliverable_at IS NOT NULL "
        "ORDER BY id", (run_id,)))


def render_delivery(rows: list) -> str:
    """What a live session is told when queued messages arrive at a boundary."""
    joined = "\n\n".join(f"[message from {r['sender']}]\n{r['body']}" for r in rows)
    return ("Apply the following delivered message(s) now, then continue the "
            f"original mission.\n\n{joined}")


# --- ask --------------------------------------------------------------------

def ask_seconds(cfg: dict) -> int:
    """How long an ask may hold a session open, and the card's expires_at."""
    try:
        configured = int(nod.nod_cfg(cfg).get("expires_after") or DEFAULT_ASK_SECONDS)
    except (TypeError, ValueError):
        configured = DEFAULT_ASK_SECONDS
    if configured <= 0:
        configured = DEFAULT_ASK_SECONDS
    return min(configured, MAX_ASK_SECONDS)


def open_ask(con: sqlite3.Connection, run_id: int):
    """The run's unanswered question, if it has one."""
    return con.execute(
        "SELECT * FROM nod_requests WHERE run_id=? AND kind='blocked' "
        "AND status='pending' ORDER BY created_at DESC LIMIT 1", (run_id,)).fetchone()


def file_question(con: sqlite3.Connection, cfg: dict, run, question: str,
                  *, run_url: str | None = None,
                  work_url: str | None = None) -> tuple[str, int]:
    """File the question as a Nod decision request. Returns (request_id, seconds).

    Raises ``SystemExit`` when the human loop is off: a worker that thinks it
    asked and never will be answered is worse than a loud failure.
    """
    channels = nod.from_cfg(cfg)
    if channels is None:
        raise SystemExit(
            "orchestra: ask needs the human loop — set [nod] enabled = true and "
            "configure the decisions channel, or use `orchestra tell` instead")
    run_id = int(run["id"])
    seconds = ask_seconds(cfg)
    title = f"run {run['slug'] or run_id} is asking"
    created = nod.blocked_run(
        channels, question, con=con, run_id=run_id, ref=run["ref"],
        title=title,
        summary=(run["title"] or "")[:200],
        expires_at=nod.expires_in(seconds),
        links=nod.links_for(work_url=work_url, run_url=run_url),
        # One open question per run: a second ask before the first is answered
        # replaces nothing and buzzes twice.
        dedupe_key=f"orchestra:blocked:{run_id}",
    )
    con.execute(
        "INSERT INTO messages(run_id, sender, body, kind, created_at, delivered_at) "
        "VALUES(?,?,?,?,?,?)",
        (run_id, f"run {run_id}", question, ASK_KIND, db.now(), db.now()))
    con.commit()   # the adapter reads this row and mirrors it (CONTRACT §7)
    return created["request_id"], seconds


def await_answer(con: sqlite3.Connection, cfg: dict, request, *,
                 sleep=None) -> str:
    """Hold until the human answers, or until the card expires.

    Returns the text to inject back into the held session. An expiry is not
    an error: it is the declared fallback, and the worker is told so.
    """
    channels = nod.from_cfg(cfg)
    request_id = request["request_id"]
    if channels is None:  # configuration disappeared under a live ask
        return _fallback_text("the human loop is not configured any more")
    client = channels.for_request(con, request_id)
    seconds = ask_seconds(cfg)
    waited, view = 0, None
    while waited < seconds:
        chunk = min(nod.WAIT_MAX, seconds - waited)
        try:
            view = client.wait(request_id, timeout_seconds=chunk)
        except nod.NodError as exc:
            return _fallback_text(f"Nod is not answering ({exc})")
        waited += chunk
        if view.get("status", "pending") != "pending":
            break   # decided (or cancelled/expired server-side); stop waiting
        if sleep is not None:
            sleep(0)
    if view is None or view.get("status", "pending") == "pending":
        return _fallback_text("nobody answered before the card expired")
    nod.save_decision(con, request_id, view)
    decision = view.get("decision") or {}
    if not decision:  # cancelled or expired server-side: no answer exists
        return _fallback_text(f"the request ended as {view.get('status')}")
    text = (decision.get("text") or "").strip()
    if decision.get("option_kind") == "reject":  # the "Stop the run" option
        answer = ("The human ended this question with STOP" +
                  (f": {text}" if text else "") +
                  ". Do not continue the current line of work — write up where "
                  "you got to and finish.")
    elif text:
        answer = f"Answer from the human:\n\n{text}"
    else:
        answer = ("The human answered without text "
                  f"({decision.get('option_id') or 'no option'}). Use your best "
                  "judgement and record the assumption you made.")
    con.execute(
        "INSERT INTO messages(run_id, sender, body, kind, created_at, delivered_at) "
        "VALUES(?,?,?,?,?,?)",
        (request["run_id"], "human", answer, ANSWER_KIND, db.now(), db.now()))
    con.commit()
    if request["run_id"] is not None:
        traces.record_injection(con, int(request["run_id"]), "human", answer)
    return answer   # the ``answer`` row above is what the adapter mirrors


def _fallback_text(why: str) -> str:
    return ("No answer arrived — " + why + ". This was the declared fallback: "
            "proceed on your own best judgement, and write down the assumption "
            "you made so the human can correct it afterwards.")
