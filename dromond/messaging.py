"""The two messaging verbs (DESIGN §6): ``tell`` and ``ask``.

``tell`` is non-blocking and lands at a safe boundary. Its row kind stays
``interrupt`` because that IS the delivery machinery the supervisor already
runs (pending offset -> stop at a completed action boundary -> resume the
same backend session with the body embedded). ``dromond tell`` is the
DESIGN name for scheduling one; ``dromond interrupt --now`` is the
emergency variant of the same row.

``ask`` is blocking with a declared fallback. The worker files a question,
ends its turn, and the harness's Stop hook (``dromond hook``) holds the
session open while Dromond waits on a Nod decision request. The answer is
injected back through the hook as the next instruction. Nod's ``expires_at``
IS the declared fallback: when it passes, the worker is told to proceed on
its own judgement rather than being left hanging.

Both sides of an ``ask`` are mirrored into the Work item thread, because a
decision that only exists on a phone is not a record.

Undeliverable is a state, not a deletion. A message whose run ended before
delivery is marked with a reason and surfaced (``dromond show``,
``dromond traces messages``, the dashboard). It is never re-aimed at a later
run: a correction handed to a run that never saw the context it referred to
is worse than no correction.

Scope (DESIGN §6): human-to-run and parent-to-child. Arbitrary run-to-run
messaging is out, and ``ask`` targets the human only.
"""
import os
import sqlite3

from dromond import db, nod, traces, work_client

DELIVERY_KIND = "interrupt"   # a queued tell, delivered at a safe boundary
ASK_KIND = "ask"              # the run's question (outbound)
ANSWER_KIND = "answer"        # the human's answer (inbound, injected)

# A Stop hook cannot outlive its own harness timeout, so an ask waits for at
# most this long no matter what [nod] expires_after says.
MAX_ASK_SECONDS = 35_400      # hook timeout (36000s) minus a 10-minute margin
DEFAULT_ASK_SECONDS = 86_400


# --- tell -------------------------------------------------------------------

def queue_tell(con: sqlite3.Connection, run_id: int, sender: str, body: str,
               log_path: str | None, *, boundary: bool = True) -> int:
    """Record a message for delivery at the run's next safe boundary.

    ``boundary=False`` is the ACP transport (W-0104, DESIGN §6): the run has
    a live protocol channel, so there is no boundary to wait for — the
    supervisor steers it in mid-turn. The row then carries NO
    ``delivery_offset``, which is what makes the delivery state honest: the
    exec boundary machinery ignores it (``supervise._pending_delivery_offset``
    filters on that column) and ``traces.run_messages`` stops badging it
    ``pending_boundary``, because no boundary is pending.
    """
    offset = None
    if boundary:
        try:
            offset = os.path.getsize(log_path) if log_path else 0
        except OSError:
            offset = 0
    cur = con.execute(
        "INSERT INTO messages(run_id, sender, body, kind, created_at, delivery_offset) "
        "VALUES(?,?,?,?,?,?)",
        (run_id, sender, body, DELIVERY_KIND, db.now(), offset))
    con.commit()
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

    Called once, at finalization: the run is over, so nothing will deliver
    these. They stay visible with their reason instead of vanishing.
    """
    cur = con.execute(
        "UPDATE messages SET undeliverable_at=?, undeliverable_reason=? "
        "WHERE run_id=? AND kind IN (?,?) AND delivered_at IS NULL "
        "AND undeliverable_at IS NULL",
        (db.now(), reason[:500], run_id, DELIVERY_KIND, ANSWER_KIND))
    con.commit()
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
            "dromond: ask needs the human loop — set [nod] enabled = true and "
            "configure the decisions channel, or use `dromond tell` instead")
    run_id = int(run["id"])
    seconds = ask_seconds(cfg)
    title = f"run {run['slug'] or run_id} is asking"
    created = nod.blocked_run(
        channels, question, con=con, run_id=run_id, work_item=run["work_item"],
        title=title,
        summary=(run["title"] or "")[:200],
        expires_at=nod.expires_in(seconds),
        links=nod.links_for(work_url=work_url, run_url=run_url),
        # One open question per run: a second ask before the first is answered
        # replaces nothing and buzzes twice.
        dedupe_key=f"dromond:blocked:{run_id}",
    )
    con.execute(
        "INSERT INTO messages(run_id, sender, body, kind, created_at, delivered_at) "
        "VALUES(?,?,?,?,?,?)",
        (run_id, f"run {run_id}", question, ASK_KIND, db.now(), db.now()))
    con.commit()
    mirror_to_work(cfg, run["work_item"], f"Question for the human:\n\n{question}")
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
    if mirror_to_work(cfg, request["work_item"], answer):
        nod.mark_mirrored(con, request_id)
    return answer


def _fallback_text(why: str) -> str:
    return ("No answer arrived — " + why + ". This was the declared fallback: "
            "proceed on your own best judgement, and write down the assumption "
            "you made so the human can correct it afterwards.")


def mirror_to_work(cfg: dict, work_item: str | None, text: str) -> bool:
    """Put an ask or its answer into the Work item's thread. Best effort:
    Work being down must never break a live session."""
    if not work_item:
        return False
    client = work_client.from_cfg(cfg)
    if client is None:
        return False
    try:
        # Same split as sweeper.item_kind, inlined: sweeper imports supervise,
        # and supervise imports this module.
        if work_item.startswith("W-"):
            posted = client.log_task(work_item, text)
        else:
            posted = client.reply_issue(work_item, text)
    except work_client.WorkError:
        return False
    return posted is not None
