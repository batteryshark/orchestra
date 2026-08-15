"""A stub ACP peer: NDJSON JSON-RPC 2.0 on stdio, no model, no cost.

Run as a subprocess in place of ``reasonix acp`` / ``opencode acp``. It
speaks the shapes verified live against both real agents (2026-08):

* ``initialize`` -> ``protocolVersion: 1`` + ``loadSession: true``
* ``session/new`` -> ``{"sessionId": ...}``; ``session/load`` re-opens one
* ``session/prompt`` -> streamed ``session/update`` notifications, then
  ``{"stopReason": ...}``
* ``session/cancel`` is a NOTIFICATION (both real agents answer "method not
  found" to a request-shaped one) and ends the live turn as ``cancelled``
  WITHOUT ending the session — the next prompt keeps the same session id
* ``_reasonix.io/session/steer`` injects mid-turn; with no active turn it
  errors exactly as Reasonix does ("session has no active prompt")
* ``session/request_permission`` is asked of the CLIENT, and the turn does
  not finish until the client answers

Knobs, all through the environment so a test can pick a behaviour without
touching this file:

  STUB_ACP_TURN        seconds one turn takes                 (default 1)
  STUB_ACP_PERMISSION  ask for permission during the turn     (default 0)
  STUB_ACP_DIE         "start" | "handshake" | "turn"         (default "")
  STUB_ACP_BAD_VERSION report an unsupported protocolVersion  (default 0)
"""
import json
import os
import sys
import threading
import time

SESSION_ID = "stub-acp-session-1"


def _env(name, default=""):
    return os.environ.get(name, default)


class Stub:
    def __init__(self, out=None):
        self.out = out or sys.stdout
        self.lock = threading.Lock()
        self.turn = None            # {"id":..., "cancelled": bool, "steers": [...]}
        self.session = None
        self.permission_answer = None
        self.permission_waiter = threading.Event()

    # -- wire
    def send(self, obj):
        with self.lock:
            self.out.write(json.dumps(obj) + "\n")
            self.out.flush()

    def reply(self, request_id, result):
        self.send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def error(self, request_id, code, message):
        self.send({"jsonrpc": "2.0", "id": request_id,
                   "error": {"code": code, "message": message}})

    def update(self, payload):
        self.send({"jsonrpc": "2.0", "method": "session/update",
                   "params": {"sessionId": self.session, "update": payload}})

    # -- methods
    def handle(self, frame):
        method, params = frame.get("method"), frame.get("params") or {}
        request_id = frame.get("id")
        if method is None and request_id is not None:
            self.permission_answer = frame.get("result")
            self.permission_waiter.set()
            return
        if method == "initialize":
            version = 99 if _env("STUB_ACP_BAD_VERSION") == "1" else 1
            self.reply(request_id, {
                "protocolVersion": version,
                "agentCapabilities": {"loadSession": True,
                                      "promptCapabilities": {"embeddedContext": True}},
                "agentInfo": {"name": "stub-acp", "version": "1"}})
            if _env("STUB_ACP_DIE") == "handshake":
                os._exit(3)
            return
        if method == "session/new":
            self.session = SESSION_ID
            self.reply(request_id, {"sessionId": self.session})
            return
        if method == "session/load":
            self.session = params.get("sessionId")
            self.reply(request_id, {})
            return
        if method in ("session/set_model", "session/set_mode"):
            self.reply(request_id, {})
            return
        if method == "session/prompt":
            threading.Thread(target=self._turn, args=(request_id, params),
                             daemon=True).start()
            return
        if method == "_reasonix.io/session/steer":
            turn = self.turn
            if not (params.get("prompt") or []):
                self.error(request_id, -32602,
                           "_reasonix.io/session/steer: empty prompt")
            elif turn is None:
                self.error(request_id, -32600,
                           "_reasonix.io/session/steer: session has no active prompt")
            else:
                turn["steers"].append(_text(params.get("prompt")))
                self.update({"sessionUpdate": "user_message_chunk",
                             "content": {"type": "text",
                                         "text": _text(params.get("prompt"))}})
                self.reply(request_id, {})
            return
        if method == "session/cancel":
            if self.turn is not None:
                self.turn["cancelled"] = True     # session stays open and loadable
            return
        if request_id is not None:
            self.error(request_id, -32601, f"method not found: {method}")

    # -- one turn
    def _turn(self, request_id, params):
        turn = {"id": request_id, "cancelled": False, "steers": []}
        self.turn = turn
        prompt = _text(params.get("prompt"))
        self.update({"sessionUpdate": "agent_thought_chunk",
                     "content": {"type": "text", "text": "considering the mission"}})
        self.update({"sessionUpdate": "plan", "entries": [
            {"content": "read the code", "priority": "high", "status": "pending"}]})
        self.update({"sessionUpdate": "tool_call", "toolCallId": "call-1",
                     "title": "read README.md", "kind": "read", "status": "pending",
                     "rawInput": {"path": "README.md"}})
        if _env("STUB_ACP_PERMISSION") == "1":
            self.send({"jsonrpc": "2.0", "id": 9001,
                       "method": "session/request_permission",
                       "params": {"sessionId": self.session,
                                  "toolCall": {"toolCallId": "call-1",
                                               "title": "write to README.md",
                                               "kind": "edit"},
                                  "options": [
                                      {"optionId": "no", "name": "Reject",
                                       "kind": "reject_once"},
                                      {"optionId": "yes", "name": "Allow",
                                       "kind": "allow_once"}]}})
            self.permission_waiter.wait(timeout=30)
        self.update({"sessionUpdate": "tool_call_update", "toolCallId": "call-1",
                     "status": "completed",
                     "content": [{"type": "text", "text": "the file says hello"}]})
        end = time.time() + float(_env("STUB_ACP_TURN", "1"))
        while time.time() < end and not turn["cancelled"]:
            time.sleep(0.05)
        if _env("STUB_ACP_DIE") == "turn":
            os._exit(4)
        self.turn = None
        if turn["cancelled"]:
            self.update({"sessionUpdate": "agent_message_chunk",
                         "content": {"type": "text", "text": "stopped on request"}})
            self.reply(request_id, {"stopReason": "cancelled"})
            return
        said = "stub acp turn complete: " + prompt[:200]
        for steer in turn["steers"]:
            said += f" | steered mid-turn: {steer[:200]}"
        self.update({"sessionUpdate": "agent_message_chunk",
                     "content": {"type": "text", "text": said}})
        self.reply(request_id, {"stopReason": "end_turn"})


def _text(blocks) -> str:
    if isinstance(blocks, list):
        return "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
    return str(blocks or "")


def main() -> None:
    if _env("STUB_ACP_DIE") == "start":
        sys.stderr.write("stub-acp ERROR refusing to start\n")
        sys.exit(2)
    stub = Stub()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            frame = json.loads(line)
        except ValueError:
            continue
        if isinstance(frame, dict):
            stub.handle(frame)


if __name__ == "__main__":
    main()
