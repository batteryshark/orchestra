"""Small stdlib ACP client: NDJSON JSON-RPC over one persistent process."""
import json
import subprocess
import threading
import time

from orchestra import db, harnesses
from orchestra.proc import resolve_cmd, session_kwargs

# Verified live (2026-08): `reasonix acp` and `opencode acp` both answer
# initialize + session/new with loadSession: true.
ACP_BACKENDS = harnesses.supporting("transport", "acp")
PROTOCOL_VERSION = 1
HANDSHAKE_TIMEOUT = 60.0

# ACP permission option kinds, most permissive first / least last. The
# posture picks which end to answer from.
_ALLOW_KINDS = ("allow_always", "allow_once")
_REJECT_KINDS = ("reject_once", "reject_always")


class AcpError(RuntimeError):
    """The peer failed, died, or answered an error. Always fails the run."""


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
    raise SystemExit(f"orchestra: backend '{backend}' has no ACP mode")


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
                resolve_cmd(self.cmd), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=self.cwd, env=self.env, text=True, bufsize=1,
                **session_kwargs())
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

    # -- log (the raw file is the source of truth, DESIGN §13)
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


# --- permission (DESIGN §10: a protocol message, not an unanswerable prompt) --

def permission_answer(profile: dict, params: dict) -> dict:
    """Answer ``session/request_permission`` from the profile's posture.

    ponytail: a deterministic answer, defaulting to allow so an ACP run
    behaves like the exec run it replaces (``--permission-mode auto`` /
    ``--auto``). The request and this answer are both in the normalized
    trace, so a human can see every decision after the fact. A profile that
    must not grant a capability uses ``deny``; external decision routing is
    deliberately outside this transport helper.
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


# --- self-check --------------------------------------------------------------

def _demo() -> None:
    """Smallest runnable check of the parts with no database in them."""
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
