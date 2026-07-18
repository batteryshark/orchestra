"""`orchestra ui` — zero-dependency live web dashboard for a project's runs,
inboxes, findings feed, and teams. Reads the project SQLite; no writes.

The HTML page lives in ui.html next to this module and is read from disk on
every request, so UI edits only need a browser refresh (no server restart).
"""
import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from orchestra_cli import db

UI_FILE = Path(__file__).with_name("ui.html")

MAX_INPUT = 4000
MAX_OUTPUT = 12000


def _fmt(v, limit=MAX_OUTPUT) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        try:
            v = json.dumps(v, indent=2)
        except (TypeError, ValueError):
            v = str(v)
    v = str(v)
    return v if len(v) <= limit else v[:limit] + f"\n… [+{len(v) - limit} chars]"


def parse_transcript(text: str) -> list[dict]:
    """Best-effort JSONL -> ordered transcript items across the three backend
    event formats (opencode --format json, codex --json, claude stream-json).
    Streaming updates for the same part/item update in place (keyed), so the
    result reads like the tool's own transcript."""
    items: list[dict] = []
    index: dict = {}

    def add(key, item):
        if key is not None and key in index:
            index[key].update({k: v for k, v in item.items() if v not in (None, "")})
        else:
            items.append(item)
            if key is not None:
                index[key] = item

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if not line.startswith("{"):
            add(None, {"kind": "meta", "body": line[:300]})
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue

        # --- opencode: events carry a "part" ---
        part = obj.get("part")
        if isinstance(part, dict):
            pt, pid = part.get("type"), part.get("id")
            if pt == "text":
                add(("oc", pid), {"kind": "text", "body": part.get("text", "")})
            elif pt == "reasoning":
                add(("oc", pid), {"kind": "thinking", "body": part.get("text", "")})
            elif pt == "tool":
                st = part.get("state") or {}
                out = st.get("output") or st.get("error") or ""
                add(("oc", pid), {"kind": "tool", "name": part.get("tool", "tool"),
                                  "status": st.get("status", ""),
                                  "input": _fmt(st.get("input"), MAX_INPUT),
                                  "output": _fmt(out)})
            # step-start/step-finish/snapshot/patch: skip silently
            continue

        t = obj.get("type", "")

        # --- codex --json: item.* events ---
        if t.startswith("item."):
            it = obj.get("item") or {}
            k, typ = ("cx", it.get("id")), it.get("type")
            if typ == "agent_message":
                add(k, {"kind": "text", "body": it.get("text", "")})
            elif typ == "reasoning":
                add(k, {"kind": "thinking", "body": it.get("text", "") or it.get("summary", "")})
            elif typ == "command_execution":
                add(k, {"kind": "tool", "name": "shell", "status": it.get("status", ""),
                        "input": _fmt(it.get("command"), MAX_INPUT),
                        "output": _fmt(it.get("aggregated_output"))})
            elif typ in ("file_change", "patch"):
                add(k, {"kind": "tool", "name": "file_change", "status": it.get("status", ""),
                        "input": _fmt(it.get("changes") or {kk: vv for kk, vv in it.items()
                                                            if kk not in ("id", "type")}, MAX_INPUT),
                        "output": ""})
            elif typ == "mcp_tool_call":
                add(k, {"kind": "tool", "name": it.get("tool") or "mcp", "status": it.get("status", ""),
                        "input": _fmt(it.get("arguments"), MAX_INPUT), "output": _fmt(it.get("result"))})
            elif typ == "web_search":
                add(k, {"kind": "tool", "name": "web_search", "status": it.get("status", ""),
                        "input": _fmt(it.get("query"), MAX_INPUT), "output": ""})
            elif typ == "todo_list":
                pass
            elif typ == "error":
                add(k, {"kind": "error", "body": _fmt(it.get("message") or it)})
            continue
        if t == "thread.started":
            add(None, {"kind": "meta", "body": f"thread {obj.get('thread_id', '')}"})
            continue
        if t == "turn.failed":
            add(None, {"kind": "error", "body": _fmt(obj.get("error") or obj)})
            continue
        if t == "error":
            add(None, {"kind": "error", "body": _fmt(obj.get("message") or obj)})
            continue

        # --- claude -p stream-json ---
        if t == "assistant":
            m = obj.get("message") or {}
            mid = m.get("id", "")
            for i, c in enumerate(m.get("content") or []):
                if not isinstance(c, dict):
                    continue
                ct = c.get("type")
                if ct == "text":
                    add(("cl", mid, i), {"kind": "text", "body": c.get("text", "")})
                elif ct == "thinking":
                    add(("cl", mid, i), {"kind": "thinking", "body": c.get("thinking", "")})
                elif ct == "tool_use":
                    add(("cltool", c.get("id")), {"kind": "tool", "name": c.get("name", "tool"),
                                                  "status": "running",
                                                  "input": _fmt(c.get("input"), MAX_INPUT),
                                                  "output": ""})
            continue
        if t == "user":
            for c in ((obj.get("message") or {}).get("content") or []):
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    k = ("cltool", c.get("tool_use_id"))
                    out = c.get("content")
                    if isinstance(out, list):
                        out = "\n".join(x.get("text", "") for x in out if isinstance(x, dict))
                    if k in index:
                        index[k]["output"] = _fmt(out)
                        index[k]["status"] = "error" if c.get("is_error") else "completed"
            continue
        if t == "result":
            add(None, {"kind": "meta", "body": f"result · {_fmt(obj.get('result'), 300)}"})
            continue
        # unknown event: ignore quietly
    return items


def make_handler(root: Path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlparse(self.path)
            path = url.path
            if path in ("/", "/index.html"):
                try:
                    body = UI_FILE.read_bytes()
                except OSError:
                    body = b"ui.html missing next to ui.py"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/state":
                con = db.connect(root)
                state = {
                    "root": str(root),
                    "runs": [dict(r) for r in con.execute(
                        "SELECT * FROM runs ORDER BY id DESC LIMIT 100")][::-1],
                    "messages": [dict(r) for r in con.execute(
                        "SELECT * FROM messages ORDER BY id DESC LIMIT 150")][::-1],
                    "feed": [dict(r) for r in con.execute(
                        "SELECT * FROM feed ORDER BY id DESC LIMIT 50")][::-1],
                    "teams": [{"name": t["name"],
                               "members": [m["agent"] for m in con.execute(
                                   "SELECT agent FROM members WHERE team_id=?", (t["id"],))]}
                              for t in con.execute("SELECT * FROM teams")],
                }
                con.close()
                self._json(state)
            elif path.startswith("/api/transcript/"):
                try:
                    run_id = int(path.rsplit("/", 1)[1])
                except ValueError:
                    return self._json({"error": "bad id"}, 400)
                con = db.connect(root)
                r = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
                con.close()
                if not r:
                    return self._json({"error": "no such run"}, 404)
                lp = Path(r["log_path"]) if r["log_path"] else None
                try:
                    st = lp.stat() if lp else None
                    etag = f"{r['status']}-{st.st_size}-{int(st.st_mtime)}" if st else r["status"]
                except OSError:
                    etag, st = r["status"], None
                client_etag = (parse_qs(url.query).get("etag") or [None])[0]
                if client_etag == etag:
                    return self._json({"etag": etag, "unchanged": True})
                items = []
                if st:
                    items = parse_transcript(lp.read_text(errors="replace"))
                self._json({"etag": etag, "run": dict(r), "items": items})
            elif path.startswith("/api/log/"):
                try:
                    run_id = int(path.rsplit("/", 1)[1])
                except ValueError:
                    return self._json({"error": "bad id"}, 400)
                con = db.connect(root)
                r = con.execute("SELECT log_path FROM runs WHERE id=?", (run_id,)).fetchone()
                con.close()
                text = ""
                if r and r["log_path"] and Path(r["log_path"]).is_file():
                    text = Path(r["log_path"]).read_text(errors="replace")[-40000:]
                self._json({"text": text})
            else:
                self._json({"error": "not found"}, 404)

    return Handler


def serve(root: Path, port: int = 4764, open_browser: bool = True) -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(root))
    url = f"http://127.0.0.1:{port}"
    print(f"orchestra ui: {url}  (ctrl-c to stop; ui.html edits apply on browser refresh)")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
