"""ACP transport: NDJSON JSON-RPC 2.0 over one persistent process (DESIGN §6).

The SECOND transport. Exec stays the default and is untouched: a profile
opts in with ``transport = "acp"``, and only Reasonix and OpenCode may —
they are the two backends that speak ACP (verified live, both report
``loadSession: true``).

What ACP buys over exec, and why this module exists:

* **live ``tell``** — Reasonix's vendor ``_reasonix.io/session/steer``
  injects a message mid-turn, so a correction lands while the worker is
  still working instead of waiting for a safe boundary. No other backend
  can do this. OpenCode has no steer, so its messages go as the next
  ``session/prompt`` on the SAME live session — still no kill, still no
  re-exec.
* **graceful ``session/cancel``** instead of kill-and-resume, which also
  preserves Reasonix's prefix cache: the session id never changes, so the
  next prompt continues where the cancelled turn stopped.
* **``session/request_permission``** arrives as a protocol message that
  Dromond can answer, rather than a TTY prompt nobody is watching.

**The raw log stays the source of truth** (DESIGN §7). Every frame in both
directions is appended to the run's own JSONL as one line, tagged ``_dir``
/ ``_ts`` / ``_method``, and the backend's stderr goes into the same file.
So ``traces.ingest`` (byte offsets, expand-in-place, retention),
``runners.parse_log`` (session ref, last text) and ``runners.parse_failure``
all work on an ACP run with no changes — the normalized events table is fed
through exactly the same path an exec run uses.

**Failure posture**: a dead peer or a failed handshake fails the run with a
reason. There is deliberately no mid-run fallback to exec — a run whose
transport changed halfway is a run nobody can reason about afterwards.
"""
import json
import os
import subprocess
import threading
import time

from dromond import config, db, messaging, observer, runners, traces

# Verified live (2026-08): `reasonix acp` and `opencode acp` both answer
# initialize + session/new with loadSession: true.
ACP_BACKENDS = ("reasonix", "opencode")
PROTOCOL_VERSION = 1
STEER_METHOD = "_reasonix.io/session/steer"

POLL_INTERVAL = 0.5
HANDSHAKE_TIMEOUT = 60.0

# Every transport failure reason starts with this, the same way a stall
# summary starts with "Stalled:". ``supervise`` keeps such a summary instead
# of overwriting it with the last line of a log that never got to the point.
FAILURE_PREFIX = "ACP "

# Every continuation prompt carries it, the same way the exec resume does —
# without it the handoff filing (DESIGN §9) reports a protocol failure on
# every run that took a message.
HANDOFF_REMINDER = "\n\nEnd with the usual handoff summary as your final message."

# ACP permission option kinds, most permissive first / least last. The
# posture picks which end to answer from.
_ALLOW_KINDS = ("allow_always", "allow_once")
_REJECT_KINDS = ("reject_once", "reject_always")


class AcpError(RuntimeError):
    """The peer failed, died, or answered an error. Always fails the run."""


# --- profile opt-in ----------------------------------------------------------

def transport_for(profile: dict) -> str:
    """``"acp"`` or ``"exec"``. Absent means exec, unchanged.

    An opt-in on a backend that cannot speak ACP is a configuration error,
    not a silent downgrade: quietly running exec under an ``acp`` profile is
    the same unreasonable-about run that the no-fallback rule forbids.
    """
    value = profile.get("transport") or "exec"
    if not isinstance(value, str):
        raise SystemExit("dromond: profile transport must be a string")
    value = value.strip().lower()
    if value not in ("exec", "acp"):
        raise SystemExit(
            f"dromond: unknown transport '{value}' for profile "
            f"{profile.get('name')} — use \"exec\" (default) or \"acp\"")
    if value == "acp" and profile.get("backend") not in ACP_BACKENDS:
        raise SystemExit(
            f"dromond: backend '{profile.get('backend')}' has no ACP mode "
            f"(profile {profile.get('name')}). Only "
            f"{', '.join(ACP_BACKENDS)} speak ACP; drop transport = \"acp\".")
    return value


def run_transport(cfg: dict, profile_name: str) -> str:
    """Transport of a run, from the profile it was launched with.

    ponytail: derived, not stored — a profile edited mid-run would report
    the new value. Add a ``runs.transport`` column if a finished run ever
    needs to prove how it ran after its profile changed. The raw log still
    settles the question after the fact: ACP frames carry ``jsonrpc``.
    """
    try:
        return transport_for(config.profile_cfg(cfg, profile_name))
    except SystemExit:
        return "exec"


def build_acp_cmd(profile: dict) -> list[str]:
    """The persistent-peer command. One line per backend, like ``build_cmd``.

    Model/effort go over the protocol (``session/set_model``), not on the
    command line — verified: both agents validate ``modelId`` there, and
    ``opencode acp`` has no model flag at all.
    """
    backend = profile.get("backend")
    if backend == "reasonix":
        return ["reasonix", "acp"]
    if backend == "opencode":
        return ["opencode", "acp"]
    raise SystemExit(f"dromond: backend '{backend}' has no ACP mode")


# --- the peer ----------------------------------------------------------------

class Peer:
    """One persistent ACP process, spoken to in NDJSON JSON-RPC 2.0.

    Stdlib only: the protocol is one JSON object per line, so a reader
    thread and a dict of pending ids is the whole client. Requests the AGENT
    makes (permission) are answered from the reader thread, because the
    agent's turn is blocked until they are.
    """

    def __init__(self, cmd, *, cwd, env, log_path, on_request=None):
        self.cmd = list(cmd)
        self.cwd = cwd
        self.env = env
        self.log_path = log_path
        self.on_request = on_request
        self.proc = None
        self.dead = False
        self.death_reason = None
        self.last_rx = time.time()
        self._next_id = 0
        self._responses: dict[int, dict] = {}
        self._methods: dict[int, str] = {}
        self._lock = threading.Lock()      # guards stdin and the id counter
        self._log_lock = threading.Lock()  # guards the raw log file
        self._readers: list[threading.Thread] = []

    # -- lifecycle
    def start(self) -> None:
        try:
            self.proc = subprocess.Popen(
                self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, cwd=self.cwd, env=self.env,
                text=True, bufsize=1, start_new_session=True)
        except OSError as exc:
            raise AcpError(f"cannot start {self.cmd[0]}: {exc}") from exc
        self._readers = [
            threading.Thread(target=self._read_stdout, daemon=True),
            threading.Thread(target=self._read_stderr, daemon=True),
        ]
        for reader in self._readers:
            reader.start()

    def close(self) -> None:
        if self.proc is None:
            return
        proc = self.proc
        if proc.poll() is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        for reader in self._readers:
            reader.join(timeout=5)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()

    @property
    def alive(self) -> bool:
        return bool(self.proc) and self.proc.poll() is None and not self.dead

    # -- log (the raw file is the source of truth, DESIGN §7)
    def _log(self, frame: dict, direction: str, method: str | None = None) -> None:
        record = {"jsonrpc": "2.0", "_dir": direction,
                  "_ts": db.now(), **frame}
        if method:
            record["_method"] = method
        line = json.dumps(record, ensure_ascii=False, default=str)
        self._append(line)

    def _append(self, line: str) -> None:
        # Three threads write here (caller, stdout reader, stderr reader) and
        # a half-written line would break the byte offsets the trace stores.
        with self._log_lock:
            try:
                with open(self.log_path, "a", encoding="utf-8",
                          errors="replace") as handle:
                    handle.write(line.rstrip("\n") + "\n")
            except OSError:
                pass  # a log that cannot be written must not kill a live run

    # -- reading
    def _read_stdout(self) -> None:
        try:
            for line in self.proc.stdout:
                self.last_rx = time.time()
                line = line.strip()
                if not line:
                    continue
                try:
                    frame = json.loads(line)
                except ValueError:
                    self._append(line)   # not protocol; keep it for the human
                    continue
                if not isinstance(frame, dict):
                    continue
                self._dispatch(frame)
        except (OSError, ValueError):
            pass
        finally:
            self.dead = True
            code = self.proc.poll() if self.proc else None
            self.death_reason = self.death_reason or (
                f"the ACP peer exited (code {code})" if code is not None
                else "the ACP peer closed its output")

    def _read_stderr(self) -> None:
        """Backend stderr lands in the same log, exactly as exec's does —
        that is what ``runners.parse_failure`` reads when a run dies before
        the model ever speaks."""
        try:
            for line in self.proc.stderr:
                if line.strip():
                    self._append(line.rstrip("\n"))
        except (OSError, ValueError):
            pass

    def _dispatch(self, frame: dict) -> None:
        if "id" in frame and ("result" in frame or "error" in frame):
            with self._lock:
                method = self._methods.pop(frame["id"], None)
            self._log(frame, "in", method)
            self._responses[frame["id"]] = frame
            return
        method = frame.get("method")
        self._log(frame, "in")
        if method and "id" in frame:
            self._answer(frame)

    def _answer(self, frame: dict) -> None:
        """Answer an agent-to-client request. The agent is blocked on it."""
        try:
            result = self.on_request(frame.get("method"),
                                     frame.get("params") or {}) \
                if self.on_request else None
        except Exception as exc:      # never leave the agent hanging
            result = None
            self._send({"jsonrpc": "2.0", "id": frame["id"],
                        "error": {"code": -32603, "message": str(exc)[:200]}},
                       method=frame.get("method"))
            return
        if result is None:
            self._send({"jsonrpc": "2.0", "id": frame["id"],
                        "error": {"code": -32601,
                                  "message": f"method not found: {frame.get('method')}"}},
                       method=frame.get("method"))
        else:
            self._send({"jsonrpc": "2.0", "id": frame["id"], "result": result},
                       method=frame.get("method"))

    # -- writing
    def _send(self, frame: dict, method: str | None = None) -> None:
        line = json.dumps(frame, ensure_ascii=False, default=str)
        with self._lock:
            if not self.proc or self.proc.stdin is None or self.proc.poll() is not None:
                raise AcpError(self.death_reason or "the ACP peer is not running")
            try:
                self.proc.stdin.write(line + "\n")
                self.proc.stdin.flush()
            except (OSError, ValueError) as exc:
                self.dead = True
                self.death_reason = f"writing to the ACP peer failed: {exc}"
                raise AcpError(self.death_reason) from exc
        self._log(frame, "out", method)

    def request(self, method: str, params: dict) -> int:
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            self._methods[request_id] = method
        self._send({"jsonrpc": "2.0", "id": request_id,
                    "method": method, "params": params})
        return request_id

    def notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def response(self, request_id: int) -> dict | None:
        """The reply frame, or None while it is still outstanding."""
        frame = self._responses.pop(request_id, None)
        if frame is None and not self.alive:
            raise AcpError(self.death_reason or "the ACP peer died")
        return frame

    def call(self, method: str, params: dict, timeout: float = HANDSHAKE_TIMEOUT):
        """Blocking request. For the handshake, where there is nothing else
        to do; the prompt turn uses request()/response() so the supervisor
        keeps polling."""
        request_id = self.request(method, params)
        deadline = time.time() + timeout
        while time.time() < deadline:
            frame = self.response(request_id)
            if frame is not None:
                return _result(method, frame)
            time.sleep(0.02)
        raise AcpError(f"{method} timed out after {int(timeout)}s")


def _result(method: str, frame: dict):
    if "error" in frame:
        error = frame["error"] or {}
        raise AcpError(f"{method}: {error.get('message') or error}")
    return frame.get("result")


# --- permission (DESIGN §6: a protocol message, not an unanswerable prompt) ---

def permission_answer(profile: dict, params: dict) -> dict:
    """Answer ``session/request_permission`` from the profile's posture.

    ponytail: a deterministic answer, defaulting to allow so an ACP run
    behaves like the exec run it replaces (``--permission-mode auto`` /
    ``--auto``). The request and this answer are both in the normalized
    trace, so a human can see every decision after the fact. The upgrade is
    routing it to Nod (§8) as a real ``ask`` when the profile says a human
    must decide — the protocol already supports waiting.
    """
    posture = str(profile.get("acp_permission") or "allow").strip().lower()
    options = [o for o in (params.get("options") or []) if isinstance(o, dict)]
    wanted = _REJECT_KINDS if posture == "deny" else _ALLOW_KINDS
    for kind in wanted:
        for option in options:
            if option.get("kind") == kind:
                return {"outcome": {"outcome": "selected",
                                    "optionId": option.get("optionId")}}
    if options and posture != "deny":
        return {"outcome": {"outcome": "selected",
                            "optionId": options[0].get("optionId")}}
    return {"outcome": {"outcome": "cancelled"}}


# --- the run -----------------------------------------------------------------

def supervise_run(con, run, profile: dict, *, prompt: str, run_id: int,
                  deadline: float, env: dict | None = None,
                  stall_timeout: int | None = None,
                  peer_factory=None) -> tuple[str, int | None]:
    """Run one worker to completion over ACP. Returns (status, exit_code).

    Called from ``supervise.supervise`` in place of the exec loop; every
    finalization step after it (traces, summary, checkpoint, usage,
    findings, retry, dependency release) is shared and unchanged.

    ponytail: usage is the one thing that degrades. ACP has no usage event
    in the base protocol, and ``runners.parse_usage`` keys on each backend's
    own exec-mode ``result``/``step-finish`` line, so an ACP run records
    NULL tokens and NULL cost — "not captured", which is what DESIGN §11
    already means by null. Add an ACP usage reader once a paid run shows
    where (probably Reasonix's ``_meta``) the numbers actually arrive.
    """
    log_path = run["log_path"]
    backend = run["backend"]
    peer = (peer_factory or Peer)(
        build_acp_cmd(profile), cwd=run["workdir"],
        env=dict(env if env is not None else os.environ),
        log_path=log_path,
        on_request=lambda method, params: (
            permission_answer(profile, params)
            if method == "session/request_permission" else None))
    try:
        session_id = _handshake(con, peer, run, profile, run_id)
    except AcpError as exc:
        # DESIGN §6 / W-0104: no silent fallback to exec. The run fails and
        # says why, so its transport is never in doubt.
        return _fail(con, run_id, f"{FAILURE_PREFIX}handshake failed: {exc}",
                     peer, log_path)
    try:
        return _turns(con, peer, run, profile, session_id, prompt=prompt,
                      run_id=run_id, backend=backend, log_path=log_path,
                      deadline=deadline, stall_timeout=stall_timeout)
    except AcpError as exc:
        # A human's `dromond kill` takes the peer's process group down with
        # it, which surfaces here as a dead peer. That is a kill, not a
        # transport failure.
        latest = con.execute("SELECT status FROM runs WHERE id=?",
                             (run_id,)).fetchone()
        if latest and latest["status"] in db.RUN_TERMINAL:
            return latest["status"], None
        return _fail(con, run_id, f"{FAILURE_PREFIX}transport failed: {exc}",
                     peer, log_path)
    finally:
        peer.close()


def _fail(con, run_id: int, reason: str, peer,
          log_path: str | None = None) -> tuple[str, int | None]:
    """Fail the run WITH A REASON. Deliberately never falls back to exec: a
    run whose transport changed halfway is a run nobody can reason about.

    The peer's own stderr is appended when it said anything — a backend that
    dies on a revoked token explains itself there and nowhere else.
    """
    detail = runners.parse_failure(log_path) if log_path else None
    if detail and detail not in reason:
        reason = f"{reason}\n{detail}"
    con.execute("UPDATE runs SET summary=? WHERE id=?", (reason[:2000], run_id))
    con.commit()
    peer.close()
    return "failed", None


def _handshake(con, peer, run, profile: dict, run_id: int) -> str:
    """initialize + session/new (or session/load on a resume). Raises on any
    failure — a half-open peer is not a run."""
    peer.start()
    init = peer.call("initialize", {
        "protocolVersion": PROTOCOL_VERSION,
        # ponytail: no client fs/terminal capabilities, so the agent uses its
        # own tools and Dromond implements none of fs/read_text_file,
        # fs/write_text_file or terminal/*. Declare them if a backend ever
        # needs the client to be its filesystem.
        "clientCapabilities": {"fs": {"readTextFile": False,
                                      "writeTextFile": False},
                               "terminal": False},
        "clientInfo": {"name": "dromond", "version": "1"},
    }) or {}
    version = init.get("protocolVersion")
    if version != PROTOCOL_VERSION:
        raise AcpError(f"peer speaks ACP protocolVersion {version!r}, "
                       f"this client speaks {PROTOCOL_VERSION}")
    capabilities = init.get("agentCapabilities") or {}
    resume_ref = run["session_ref"] if run["parent_run"] else None
    if resume_ref and capabilities.get("loadSession"):
        peer.call("session/load", {"sessionId": resume_ref,
                                   "cwd": run["workdir"], "mcpServers": []})
        session_id = resume_ref
    else:
        created = peer.call("session/new", {"cwd": run["workdir"],
                                            "mcpServers": []}) or {}
        session_id = created.get("sessionId")
    if not session_id:
        raise AcpError("the peer created no session")
    con.execute("UPDATE runs SET session_ref=? WHERE id=?", (session_id, run_id))
    con.commit()
    if profile.get("model"):
        # Not fatal: a rejected model id is a configuration problem the trace
        # already records, and the session's own default still works.
        try:
            peer.call("session/set_model", {"sessionId": session_id,
                                            "modelId": profile["model"]})
        except AcpError:
            pass
    return session_id


def _text_prompt(text: str) -> list[dict]:
    return [{"type": "text", "text": text}]


def _turns(con, peer, run, profile, session_id: str, *, prompt: str,
           run_id: int, backend: str, log_path: str, deadline: float,
           stall_timeout: int | None) -> tuple[str, int | None]:
    """Prompt, watch, deliver, repeat until the mission ends."""
    spin = observer.Watcher(run_id, run["project_id"])
    # The peer's pid on the row keeps `dromond kill` and the daemon's orphan
    # reap working unchanged. A kill is still a kill; `tell --now` takes the
    # graceful session/cancel path instead.
    con.execute(
        "UPDATE runs SET pid=?, status='running' "
        f"WHERE id=? AND status NOT IN {db.TERMINAL_SQL}",
        (peer.proc.pid if peer.proc else None, run_id))
    con.commit()
    text = prompt
    while True:
        turn = peer.request("session/prompt",
                            {"sessionId": session_id, "prompt": _text_prompt(text)})
        outcome, frame = _watch_turn(
            con, peer, profile, session_id, turn, run_id=run_id, backend=backend,
            log_path=log_path, deadline=deadline, stall_timeout=stall_timeout,
            spin=spin)
        traces.ingest(con, run_id, log_path, backend)
        if outcome == "timeout":
            return "timeout", None
        if outcome == "killed":
            return "killed", None
        if outcome == "ended":
            _result("session/prompt", frame)      # an error frame fails the run
            if not _has_pending(con, run_id):
                return "done", 0
        # A cancelled turn leaves the session RESUMABLE: same session id, so
        # the next prompt continues where it stopped and keeps Reasonix's
        # prefix cache, instead of the kill-and-resume exec has to do.
        claimed = con.execute(
            "UPDATE runs SET status='running' WHERE id=? AND status IN "
            "('interrupt','running')", (run_id,))
        con.commit()
        if claimed.rowcount != 1:      # killed while we were deciding
            latest = con.execute("SELECT status FROM runs WHERE id=?",
                                 (run_id,)).fetchone()
            return (latest["status"] if latest
                    and latest["status"] in db.RUN_TERMINAL else "failed"), None
        delivered = messaging.claim_pending(con, run_id)
        text = (messaging.render_delivery(delivered) if delivered else
                "Continue the original mission from where the cancelled turn "
                "stopped.") + HANDOFF_REMINDER


def _has_pending(con, run_id: int) -> bool:
    return con.execute(
        "SELECT 1 FROM messages WHERE run_id=? AND kind=? AND delivered_at IS NULL "
        "AND undeliverable_at IS NULL LIMIT 1",
        (run_id, messaging.DELIVERY_KIND)).fetchone() is not None


def _watch_turn(con, peer, profile, session_id: str, turn: int, *, run_id: int,
                backend: str, log_path: str, deadline: float,
                stall_timeout: int | None, spin) -> tuple[str, dict | None]:
    """Poll one live turn. Returns (outcome, response frame).

    Outcomes: ``ended`` (the agent finished the turn), ``cancelled`` (we
    asked it to stop and it did), ``killed``, ``timeout``.
    """
    cancelled = False
    last_progress = time.time()
    while True:
        frame = peer.response(turn)       # raises AcpError if the peer died
        if frame is not None:
            return ("cancelled" if cancelled else "ended"), frame
        now = time.time()
        latest = con.execute("SELECT status FROM runs WHERE id=?",
                             (run_id,)).fetchone()
        status = latest["status"] if latest else None
        if not cancelled and (status == "interrupt" or status in db.RUN_TERMINAL):
            # DESIGN §6: graceful cancel, never a kill. session/cancel is a
            # NOTIFICATION — verified live: both agents answer "method not
            # found" to a request-shaped one.
            peer.notify("session/cancel", {"sessionId": session_id})
            cancelled = True
            if status in db.RUN_TERMINAL:
                _drain(peer, turn)
                return "killed", None
        if not cancelled:
            _steer(con, peer, profile, session_id, run_id)
        if peer.last_rx > last_progress:
            last_progress = peer.last_rx
            traces.ingest(con, run_id, log_path, backend)
        spin.poll(con)
        # W-0098: an open `ask` is not a stall — the human is thinking.
        if stall_timeout and now - last_progress >= stall_timeout \
                and messaging.open_ask(con, run_id) is None:
            con.execute("UPDATE runs SET summary=? WHERE id=?",
                        (f"Stalled: no worker output for {int(now - last_progress)}s "
                         "(stall_timeout)", run_id))
            con.commit()
            peer.notify("session/cancel", {"sessionId": session_id})
            _drain(peer, turn)
            return "timeout", None
        if now > deadline:
            peer.notify("session/cancel", {"sessionId": session_id})
            _drain(peer, turn)
            return "timeout", None
        time.sleep(POLL_INTERVAL)


def _steer(con, peer, profile, session_id: str, run_id: int) -> None:
    """Deliver queued messages MID-TURN where the backend allows it.

    Reasonix only: ``_reasonix.io/session/steer``, whose parameter shape is
    verified live (``{sessionId, prompt}``; an empty prompt is rejected with
    "empty prompt"). OpenCode has no equivalent, so its messages wait for
    the turn to end and go as the next prompt on the same live session —
    still no kill and no re-exec.
    """
    if profile.get("backend") != "reasonix":
        return
    rows = list(con.execute(
        "SELECT id, sender, body FROM messages WHERE run_id=? AND kind=? "
        "AND delivered_at IS NULL AND undeliverable_at IS NULL ORDER BY id",
        (run_id, messaging.DELIVERY_KIND)))
    if not rows:
        return
    body = messaging.render_delivery(rows)
    try:
        peer.call(STEER_METHOD,
                  {"sessionId": session_id, "prompt": _text_prompt(body)},
                  timeout=30)
    except AcpError:
        # "session has no active prompt" is the ordinary race — the turn
        # ended between the poll and the steer. The rows stay queued and go
        # out as the next prompt.
        return
    # Two injection events land for one steer, and both belong: the frame
    # records what the model was actually sent, claim_pending records the
    # human's own words and the sender they came from.
    messaging.claim_pending(con, run_id)


def _drain(peer, turn: int, timeout: float = 10.0) -> None:
    """Give a cancelled turn a moment to answer, so the peer is not killed
    mid-write and the trace keeps its final frames."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            if peer.response(turn) is not None:
                return
        except AcpError:
            return
        time.sleep(0.05)


# --- self-check --------------------------------------------------------------

def _demo() -> None:
    """Smallest runnable check of the parts with no database in them."""
    assert transport_for({"backend": "reasonix"}) == "exec"
    assert transport_for({"backend": "opencode", "transport": "ACP"}) == "acp"
    assert build_acp_cmd({"backend": "reasonix"}) == ["reasonix", "acp"]
    allow = permission_answer({}, {"options": [{"optionId": "n", "kind": "reject_once"},
                                               {"optionId": "y", "kind": "allow_once"}]})
    assert allow == {"outcome": {"outcome": "selected", "optionId": "y"}}
    deny = permission_answer({"acp_permission": "deny"},
                             {"options": [{"optionId": "n", "kind": "reject_once"}]})
    assert deny["outcome"]["optionId"] == "n"
    assert permission_answer({}, {"options": []})["outcome"]["outcome"] == "cancelled"
    print("acp: ok")


if __name__ == "__main__":
    _demo()
