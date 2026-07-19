"""`orchestra ui` — zero-dependency live web dashboard for a project's runs,
inboxes, findings feed, and teams.

The UI is read-mostly: normal refresh routes read project SQLite state, while
the details pane exposes a POST-only stop action that uses the same run
cancellation semantics as the CLI.

The HTML page lives in ui.html next to this module and is read from disk on
every request, so UI edits only need a browser refresh (no server restart).

Multi-project control plane
---------------------------
A single ``orchestra ui`` process serves every registered Orchestra project
root (see :mod:`orchestra_cli.projects`). Each request carries the selected
project via the ``X-Orchestra-Project`` header *or* the ``?project=<id>``
query parameter; the header wins when both are present. Unknown ids surface
as a 404 JSON error — they are never silently rerouted to the default. When
no selection is sent, requests fall through to the project the UI was
launched from (so single-project use is unchanged).
"""
import errno
import hashlib
import json
import mimetypes
import socket
import threading
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from orchestra_cli import cancel, db, ensemble, host, projects, tailscale
from orchestra_cli.usage import default_service

DEFAULT_UI_PORT = 4764

UI_FILE = Path(__file__).with_name("ui.html")
RUNWAY_FILE = Path(__file__).parent / "usage" / "web" / "runway.html"
RUNWAY_ASSETS_DIR = Path(__file__).parent / "usage" / "web" / "assets"

MAX_INPUT = 4000
MAX_OUTPUT = 12000

# Header is canonically lowercase (BaseHTTPRequestHandler lowercases header
# names). The query param covers browser fetches that cannot easily set a
# header (e.g. top-level document navigations).
PROJECT_HEADER = "x-orchestra-project"
PROJECT_QUERY = "project"


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


def _visible_text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def parse_transcript(text: str) -> list[dict]:
    """Best-effort JSONL -> ordered transcript items across the three backend
    event formats (opencode --format json, codex --json, claude stream-json).
    Streaming updates for the same part/item update in place (keyed), so the
    result reads like the tool's own transcript."""
    items: list[dict] = []
    index: dict = {}

    def add(key, item):
        if item.get("kind") in ("text", "thinking") and not _visible_text(item.get("body")):
            return
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


def teammate_transcript(session_id: str) -> tuple[list[dict], str]:
    """Teammate session parts via the orchestra host API -> transcript items."""
    u = host.url()
    if not u:
        return ([{"kind": "error", "body": "orchestra host is not running — teammate "
                  "transcripts are served from it (`orchestra host start`)"}], "nohost")
    try:
        raw = urllib.request.urlopen(f"{u}/session/{session_id}/message", timeout=6).read()
        msgs = json.loads(raw)
    except Exception as e:
        return ([{"kind": "error", "body": f"could not fetch session from host: {e}"}], "err")
    items = []
    for m in msgs:
        info = m.get("info") or {}
        for p in m.get("parts") or []:
            pt = p.get("type")
            if info.get("role") == "user":
                if pt == "text":
                    body = p.get("text", "")
                    items.append({"kind": "meta", "body": "» " + body[:400]
                                  + (" …" if len(body) > 400 else "")})
                continue
            if pt == "text":
                body = p.get("text", "")
                if _visible_text(body):
                    items.append({"kind": "text", "body": body})
            elif pt == "reasoning":
                body = p.get("text", "")
                if _visible_text(body):
                    items.append({"kind": "thinking", "body": body})
            elif pt == "tool":
                st = p.get("state") or {}
                items.append({"kind": "tool", "name": p.get("tool", "tool"),
                              "status": st.get("status", ""),
                              "input": _fmt(st.get("input"), MAX_INPUT),
                              "output": _fmt(st.get("output") or st.get("error") or "")})
    return items, hashlib.md5(raw).hexdigest()


def make_handler(root: Path, registry: list[dict] | None = None):
    """Build a request handler.

    ``root`` is the project the UI process was launched from and stays
    the default selection when a request does not name one. ``registry``
    is only the *startup* snapshot of the allowlist (purely advisory);
    every request re-reads the live registry on disk so a root
    registered from another terminal after the UI started is selectable
    immediately. The launch root is always merged into the live
    allowlist so `orchestra ui` works even before `orchestra init` /
    `orchestra project register` has run.

    The default project id is whatever ``projects.project_id(root)``
    resolves to (canonical, not whatever ``root`` was passed in as) so
    requests with no header and requests with the canonical id both
    land on the same project row in the registry.
    """
    default_root = projects._canonical(root)
    default_id = projects.project_id(default_root)
    # Snapshot the startup registry ONLY to surface startup problems
    # (corrupt file, etc.) in the server log. The live view is read on
    # every request below.
    if registry is not None:
        try:
            # Touch the live registry path so an early error shows up
            # at startup rather than on the first request.
            projects.list_registered()
        except Exception as exc:  # pragma: no cover - diagnostic only
            import sys
            print(f"orchestra ui: projects registry unreadable at startup: {exc}",
                  file=sys.stderr)

    def _live_allowlist() -> list[dict]:
        """Merge the live registry with the launch root.

        Read fresh on every call (cheap: a tiny JSON file) so the
        picker and the request routing share ONE source of truth. The
        launch root is always present, even if the user has not
        registered it yet — `orchestra ui` should never refuse to
        serve the project it was started from.
        """
        try:
            live = projects.list_available()
        except Exception:
            live = []
        if not any(e["id"] == default_id for e in live) \
                and projects.is_orchestra_root(default_root):
            live = list(live) + [{
                "id": default_id,
                "name": default_root.name,
                "root": str(default_root),
                "available": True,
            }]
        return live

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_static(self, path: Path, *, content_type: str, no_store: bool) -> None:
            try:
                body = path.read_bytes()
            except OSError:
                return self._json({"error": "static asset unavailable"}, 500)
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store" if no_store else "max-age=300")
            self.end_headers()
            self.wfile.write(body)

        def _read_json_post(self) -> tuple[bool, dict | None]:
            ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if ctype != "application/json":
                self._json({"error": "Content-Type must be application/json"}, 415)
                return False, None
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                self._json({"error": "invalid Content-Length"}, 400)
                return False, None
            if length < 0:
                self._json({"error": "invalid Content-Length"}, 400)
                return False, None
            if length > 2048:
                self._json({"error": "request body too large"}, 413)
                return False, None
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return True, {}
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                self._json({"error": "invalid JSON body"}, 400)
                return False, None
            if not isinstance(payload, dict):
                self._json({"error": "JSON body must be an object"}, 400)
                return False, None
            return True, payload

        # --- project selection ------------------------------------------
        def _requested_project(self, url) -> str | None:
            """The client's project id, from header or query. Header wins."""
            header = self.headers.get(PROJECT_HEADER)
            if header and header.strip():
                return header.strip()
            q = parse_qs(url.query).get(PROJECT_QUERY)
            if q and q[0].strip():
                return q[0].strip()
            return None

        def _resolve_project(self, url, *, required: bool = False) -> Path | None:
            """Return the canonical project root this request is for.

            Reads the allowlist fresh on every request so a root
            registered after the UI process started routes correctly
            (the picker already lists it via /api/projects). The launch
            root is always in the allowlist. Unknown explicit ids
            surface as a 404 *side effect* (the response is already
            written) and return ``None`` so the caller bails.
            """
            allowed = _live_allowlist()
            requested = self._requested_project(url)
            if not requested and not required:
                # Fast path: default selection. Still validated against
                # the live allowlist in case the launch root has been
                # deleted from disk out from under us.
                by_id = {e["id"]: e for e in allowed}
                sel = by_id.get(default_id)
                if sel is None and allowed:
                    sel = allowed[0]
                if sel is None:
                    self._json({"error": "no projects available"}, 503)
                    return None
                return Path(sel["root"])
            try:
                sel = projects.resolve_selection(allowed, requested, default_id)
            except projects.UnknownProjectError as exc:
                self._json({"error": "unknown project",
                            "project": exc.project_id}, 404)
                return None
            except LookupError:
                self._json({"error": "no projects available"}, 503)
                return None
            return Path(sel["root"])

        def _projects_listing(self) -> dict:
            """Snapshot of the picker: live allowlist + which one is default."""
            return {
                "defaultProjectId": default_id,
                "projects": _live_allowlist(),
            }

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
            elif path == "/runway":
                if not RUNWAY_FILE.is_file():
                    return self._json({"error": "runway page unavailable"}, 500)
                try:
                    body = RUNWAY_FILE.read_bytes()
                except OSError:
                    return self._json({"error": "runway page read error"}, 500)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path.startswith("/runway-assets/"):
                asset = (RUNWAY_ASSETS_DIR / path[len("/runway-assets/"):]).resolve()
                # confine to the assets dir — refuse path traversal
                if RUNWAY_ASSETS_DIR.resolve() not in asset.parents and asset.parent != RUNWAY_ASSETS_DIR.resolve():
                    return self._json({"error": "bad asset path"}, 400)
                if not asset.is_file():
                    return self._json({"error": "asset not found"}, 404)
                ctype = mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
                self._send_static(asset, content_type=ctype, no_store=True)
            elif path == "/api/projects":
                # The picker source of truth. Always served off the
                # global registry; no project header needed.
                self._json(self._projects_listing())
            elif path == "/api/state":
                project = self._resolve_project(url)
                if project is None:
                    return
                con = db.connect(project)
                state = {
                    "root": str(project),
                    "project_id": projects.project_id(project),
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
                    "ensemble": ensemble.store.teams(project),
                }
                con.close()
                self._json(state)
            elif path.startswith("/api/teammate/"):
                project = self._resolve_project(url)
                if project is None:
                    return
                sid = path.rsplit("/", 1)[1]
                q = parse_qs(url.query)
                items, etag = teammate_transcript(sid)
                if (q.get("etag") or [None])[0] == etag:
                    return self._json({"etag": etag, "unchanged": True})
                team_id = (q.get("team") or [None])[0]
                self._json({"etag": etag, "items": items,
                            "messages": ensemble.store.messages(team_id) if team_id else []})
            elif path.startswith("/api/transcript/"):
                try:
                    run_id = int(path.rsplit("/", 1)[1])
                except ValueError:
                    return self._json({"error": "bad id"}, 400)
                project = self._resolve_project(url)
                if project is None:
                    return
                con = db.connect(project)
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
                project = self._resolve_project(url)
                if project is None:
                    return
                con = db.connect(project)
                r = con.execute("SELECT log_path FROM runs WHERE id=?", (run_id,)).fetchone()
                con.close()
                text = ""
                if r and r["log_path"] and Path(r["log_path"]).is_file():
                    text = Path(r["log_path"]).read_text(errors="replace")[-40000:]
                self._json({"text": text})
            elif path == "/api/usage":
                # Honor ?refresh=1 — the runway page's Refresh button sends it.
                force = (parse_qs(url.query).get("refresh") or ["0"])[0] in {"1", "true", "yes"}
                snap = default_service().snapshot(force=force)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                body = json.dumps(snap).encode()
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            url = urlparse(self.path)
            path = url.path
            if path.startswith("/api/runs/") and path.endswith("/stop"):
                ok, _payload = self._read_json_post()
                if not ok:
                    return
                parts = path.strip("/").split("/")
                if len(parts) != 4:
                    return self._json({"error": "not found"}, 404)
                try:
                    run_id = int(parts[2])
                except ValueError:
                    return self._json({"error": "bad id"}, 400)
                project = self._resolve_project(url)
                if project is None:
                    return
                con = db.connect(project)
                try:
                    result = cancel.stop_run(con, run_id)
                finally:
                    con.close()
                if not result:
                    return self._json({"error": "no such run"}, 404)
                return self._json({
                    **result.as_dict(),
                    "label": "stopped by user" if result.status == "killed" else result.status,
                })
            self.send_error(501, "Unsupported method ('POST')")

    return Handler


def parse_port(value: int | None, *, fallback: int = DEFAULT_UI_PORT,
                label: str = "--port") -> int:
    """Validate a port number coming off argparse. None means "no value
    supplied" (caller wants the default preference with safe fallback); any
    other int must be a legal TCP port."""
    if value is None:
        return fallback
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    if value < 0 or value > 65_535:
        raise ValueError(f"{label} must be between 0 and 65535")
    return value


def _port_in_use(host: str, port: int) -> bool:
    """Non-connecting probe: ask the OS to bind a fresh socket to
    ``(host, port)``. ``EADDRINUSE`` is the only signal that matters; we
    don't open a connection."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        return False
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            return True
        # In case the host isn't bindable (e.g. tailscale down by race),
        # surface anything else rather than claim the port is free.
        raise
    finally:
        s.close()


def _pick_free_port(host: str) -> int:
    """Ask the OS for a free port on ``host``. Pure helper; never raises
    for "port taken" — only for OS-level bind failures we cannot recover
    from (the caller can then decide)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, 0))
        return s.getsockname()[1]
    finally:
        s.close()


def tailscale_warning(bind_host: str) -> str:
    """The exact one-line warning orchestra prints when Tailscale is the
    bind mode. Returned (not printed) so tests can assert on it without
    spinning up a real server against a non-routable interface. The string is
    explicit that Tailnet viewers can read dashboard data and stop active runs,
    so operators do not mistake the POST stop action for a passive view."""
    return (
        "[orchestra] Tailnet access is enabled. Members permitted by "
        "your Tailscale ACLs can view this Orchestra dashboard and stop "
        "active runs from your Tailnet."
    )


def serve(root: Path, *, port: int | None = None,
          open_browser: bool = True,
          host: str | None = None, tailscale_mode: bool = False) -> int:
    """Start the dashboard, bind exactly the resolved host.

    ``port`` semantics:
      * ``None`` (caller passed no ``--port``): we prefer 4764 and, only in
        that case, fall back to an OS-chosen free port if 4764 is busy.
      * Any explicit int (including 0) is PINNED: the caller chose it, so a
        busy port fails clearly with ``EADDRINUSE``.

    Returns the actual port the server is bound to.
    """
    # Keep the launch root in the explicit allowlist so it appears in every
    # long-running dashboard. Routing still merges the launch root as a safe
    # fallback if the registry is temporarily unreadable.
    try:
        projects.register(root)
    except projects.NotAnOrchestraRoot:
        pass
    except Exception as exc:
        print(f"orchestra ui: could not register launch project: {exc}")

    plan = tailscale.resolve_bind_host(explicit_host=host, tailscale=tailscale_mode)
    bind_host = plan.host

    if port is None:
        # Default preference: 4764, with safe fallback to OS-chosen when 4764
        # is busy on this host.
        try:
            if _port_in_use(bind_host, DEFAULT_UI_PORT):
                chosen = _pick_free_port(bind_host)
                fallback_used = True
            else:
                chosen = DEFAULT_UI_PORT
                fallback_used = False
        except OSError:
            chosen = _pick_free_port(bind_host)
            fallback_used = True
        pin_port = False
    else:
        chosen = port
        pin_port = True
        fallback_used = False

    try:
        httpd = ThreadingHTTPServer((bind_host, chosen), make_handler(root))
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            raise SystemExit(
                f"orchestra: port {chosen} is already in use on {bind_host}. "
                "Pick a free port with --port, or omit --port to let orchestra "
                "fall back to an OS-chosen free port."
            ) from exc
        raise

    actual_port = httpd.server_address[1]
    url = f"http://{bind_host}:{actual_port}"
    print(f"orchestra ui: {url}  (ctrl-c to stop; ui.html edits apply on browser refresh)")
    if plan.tailscale:
        print(tailscale_warning(bind_host))
    if fallback_used:
        print(f"[orchestra] Preferred port {DEFAULT_UI_PORT} was occupied on "
              f"{bind_host}; using {actual_port} instead.")
    elif pin_port and actual_port != port:
        # Pinned port that wasn't free should have raised already; this
        # guard exists only for paranoia.
        print(f"[orchestra] Bound to {actual_port} (pinned to {port}).")

    if open_browser and bind_host == "127.0.0.1":
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return actual_port
