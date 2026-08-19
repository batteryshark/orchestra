"""In-process fake of Work's local agent API (stdlib http.server).

Mirrors the route shapes and server-side authority rules of
work-management's local-api.mjs that the sweeper depends on:

- agents may move tasks only to in_progress / review / blocked (403)
- agents may never PATCH a task (403)
- only a queued issue can be claimed; state changes require the claimant
- agents may set issue state only to in_progress / needs_human / resolved
  (resolved requires a resolutionSummary); done/closed are human verbs
- agent task creation (contract verb 5, W-0158) requires a ``parentId``
  naming a task the human delegated; top-level is rejected, and the
  human-only ``delegated`` flag is rejected from an agent
- ``updatedSince`` filters strictly greater-than

State lives in plain dicts; ``requests`` records (method, path) so tests
can assert that a pass performed no new mutations.
"""
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AGENT_TASK_STATUSES = {"in_progress", "review", "blocked"}
# Work collapsed "resolved" into "closed" (2026-08-14); the API still accepts
# "resolved" as an alias and stores "closed".
AGENT_ISSUE_STATES = {"in_progress", "needs_human", "closed"}


class FakeWork:
    def __init__(self, workspace_root="/tmp/workspace"):
        self.tasks: dict[str, dict] = {}
        self.issues: dict[str, dict] = {}
        self.decisions: dict[str, dict] = {}
        self.projects: list[dict] = []
        self.workspace_root = str(workspace_root)
        self.requests: list[tuple[str, str]] = []
        self._tick = 0
        self._server = None
        self._thread = None

    # Monotonic ISO timestamps so updatedSince ordering is deterministic.
    def now(self) -> str:
        self._tick += 1
        return f"2026-08-12T00:00:00.{self._tick:06d}Z"

    # --- state helpers (tests act as the human via these) -------------------

    def add_project(self, work_id, project_id, *, path=None, name=None,
                    alias_paths=()):
        """A Work project as /api/projects serves it: ``id`` is the human
        path-ish key an item's ``projectPath`` carries, ``projectId`` is the
        immutable UUID Dromond keys on. ``path`` is workspace-relative."""
        entry = {"id": work_id, "projectId": project_id,
                 "name": name or work_id, "path": path or work_id,
                 "aliasPaths": list(alias_paths)}
        self.projects.append(entry)
        return entry

    def add_task(self, task_id, title, *, status="ready", delegated=False,
                 agents=(), goal="", notes="", requirements="",
                 acceptance=(), project_path=None, depends_on=(),
                 blocked_by=(), parent_id=None, tags=()):
        # ``delegated`` is the CONTRACT v0.2 signal; ``agents`` models the
        # legacy shape older Work instances still serve (tolerant-read path).
        # ``dependsOn``/``blockedBy`` are the hard ordering constraints a
        # dispatcher must honor (DESIGN §4); Work always serves both.
        ts = self.now()
        self.tasks[task_id] = {
            "id": task_id, "title": title, "status": status,
            "projectPath": project_path or self._default_project(),
            "delegated": delegated, "agents": list(agents),
            "dependsOn": list(depends_on), "blockedBy": list(blocked_by),
            "parentId": parent_id, "tags": list(tags),
            "sections": {"goal": goal, "notes": notes,
                         "requirements": requirements,
                         "acceptanceCriteria": "\n".join(
                             f"- [ ] {text}" for text in acceptance)},
            # Work serves the parsed checklist alongside the markdown, with
            # three states per item: open, checked, or declined-with-a-reason.
            "requirements": [],
            "acceptanceCriteria": [
                {"checked": False, "declined": False, "reason": "", "text": text}
                for text in acceptance],
            "log": [], "createdAt": ts, "updatedAt": ts,
        }
        return self.tasks[task_id]

    def _default_project(self):
        return self.projects[0]["id"] if self.projects else None

    def add_issue(self, issue_id, title, *, state="queued", delegated=False,
                  agents=(), body="", project_path=None):
        ts = self.now()
        self.issues[issue_id] = {
            "id": issue_id, "title": title, "state": state, "body": body,
            "projectPath": project_path or self._default_project(),
            "delegated": delegated, "agents": list(agents),
            "claimedBy": None,
            "resolutionSummary": None, "messages": [],
            "createdAt": ts, "updatedAt": ts,
        }
        return self.issues[issue_id]

    def reorder_lane(self, *task_ids):
        """Put these tasks first, in this order — the human dragging the
        ready lane on their phone. Work has no priority field, so the order
        it serves the lane in IS the priority signal (DESIGN §4). Timestamps
        are untouched: reordering is not an update."""
        rest = [t for t in self.tasks if t not in task_ids]
        self.tasks = {tid: self.tasks[tid] for tid in list(task_ids) + rest}

    def human_log(self, task_id, message):
        task = self.tasks[task_id]
        ts = self.now()
        task["log"].append({"at": ts, "message": message})
        task["updatedAt"] = ts

    def human_move(self, task_id, status):
        task = self.tasks[task_id]
        ts = self.now()
        task["log"].append({"at": ts,
                            "message": f"Moved from {task['status']} to {status}."})
        task["status"] = status
        task["updatedAt"] = ts

    def human_reply(self, issue_id, body):
        issue = self.issues[issue_id]
        ts = self.now()
        issue["messages"].append({"id": f"message_{self._tick}", "body": body,
                                  "author": {"kind": "human"}, "createdAt": ts})
        if issue["state"] in ("needs_human", "closed"):
            issue["state"] = "queued"
            issue["claimedBy"] = None
            issue["resolutionSummary"] = None
        issue["updatedAt"] = ts

    def mutation_count(self) -> int:
        return sum(1 for method, _ in self.requests if method != "GET")

    # --- lifecycle ----------------------------------------------------------

    def start(self) -> str:
        handler = _make_handler(self)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)
        self._thread.start()
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=5)


def _make_handler(state: FakeWork):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status, obj):
            payload = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _error(self, status, code, message=""):
            self._send(status, {"error": {"code": code, "message": message or code}})

        def _agent(self):
            return self.headers.get("X-Work-Agent")

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length)) if length else {}

        def _filtered(self, records):
            query = self.path.partition("?")[2]
            match = re.search(r"updatedSince=([^&]+)", query)
            if not match:
                return list(records)
            from urllib.parse import unquote
            cutoff = unquote(match.group(1))
            return [r for r in records if r["updatedAt"] > cutoff]

        def do_GET(self):
            state.requests.append(("GET", self.path))
            path = self.path.partition("?")[0]
            if path == "/api/projects":
                return self._send(200, {"projects": state.projects})
            if path == "/api/health":
                return self._send(200, {"ok": True, "workspace": {
                    "name": "Tests", "root": state.workspace_root}})
            if path == "/api/tasks":
                return self._send(200, {"tasks": self._filtered(state.tasks.values())})
            if path == "/api/agent/issues":
                if not self._agent():
                    return self._error(400, "agent_identity_required")
                return self._send(200, {"issues": self._filtered(state.issues.values())})
            if path == "/api/needs-you":
                entries = (
                    [{"type": "task", "id": t["id"], "title": t["title"],
                      "updatedAt": t["updatedAt"]}
                     for t in state.tasks.values() if t["status"] == "blocked"]
                    + [{"type": "issue", "id": i["id"], "title": i["title"],
                        "updatedAt": i["updatedAt"]}
                       for i in state.issues.values() if i["state"] == "needs_human"]
                    + [{"type": "decision", "id": d["id"], "title": d["title"],
                        "updatedAt": d["updatedAt"]}
                       for d in state.decisions.values() if d["status"] == "open"])
                return self._send(200, {"entries": entries})
            m = re.fullmatch(r"/api/tasks/([^/]+)", path)
            if m:
                task = state.tasks.get(m.group(1))
                return self._send(200, task) if task else self._error(404, "not_found")
            m = re.fullmatch(r"/api/agent/issues/([^/]+)", path)
            if m:
                issue = state.issues.get(m.group(1))
                return self._send(200, issue) if issue else self._error(404, "not_found")
            self._error(404, "not_found")

        def do_PATCH(self):
            state.requests.append(("PATCH", self.path))
            if re.fullmatch(r"/api/tasks/[^/]+", self.path) and self._agent():
                return self._error(403, "agent_task_edit_forbidden",
                                   "Agents cannot edit tasks.")
            self._error(404, "not_found")

        def do_POST(self):
            state.requests.append(("POST", self.path))
            path = self.path
            m = re.fullmatch(r"/api/tasks/([^/]+)/move", path)
            if m:
                task = state.tasks.get(m.group(1))
                if not task:
                    return self._error(404, "not_found")
                body = self._body()
                status = body.get("status")
                if self._agent() and status not in AGENT_TASK_STATUSES:
                    return self._error(403, "task_status_forbidden",
                                       "Agents may only move tasks to "
                                       "in_progress, review, or blocked.")
                if status in ("review", "blocked"):
                    open_items = [i for section in ("requirements",
                                                    "acceptanceCriteria")
                                  for i in task.get(section) or []
                                  if not i["checked"] and not i["declined"]]
                    if open_items:
                        return self._error(
                            409, "review_checklist_incomplete",
                            f"Cannot move {task['id']} to {status} with "
                            f"{len(open_items)} unaccounted checklist items.")
                ts = state.now()
                note = body.get("note") or ""
                task["log"].append({
                    "at": ts,
                    "message": f"Moved from {task['status']} to {status}."
                               + (f" {note}" if note else "")})
                task["status"] = status
                task["updatedAt"] = ts
                return self._send(200, task)
            m = re.fullmatch(r"/api/tasks/([^/]+)/checklist", path)
            if m:
                task = state.tasks.get(m.group(1))
                if not task:
                    return self._error(404, "not_found")
                body = self._body()
                key = ("requirements" if body.get("section") == "requirements"
                       else "acceptanceCriteria")
                items = task.get(key) or []
                index = body.get("index")
                if not isinstance(index, int) or not 0 <= index < len(items):
                    return self._error(404, "not_found")
                item = items[index]
                if body.get("declined"):
                    if not (body.get("reason") or "").strip():
                        return self._error(400, "invalid_input",
                                           "reason is required.")
                    item.update(checked=False, declined=True,
                                reason=body["reason"])
                    entry = (f"Declined acceptance criterion: {item['text']}"
                             f" — {item['reason']}.")
                else:
                    item.update(checked=bool(body.get("checked")),
                                declined=False, reason="")
                    entry = (f"{'Completed' if item['checked'] else 'Reopened'}"
                             f" acceptance criterion: {item['text']}.")
                ts = state.now()
                task["log"].append({"at": ts, "message": entry})
                task["updatedAt"] = ts
                return self._send(200, task)
            m = re.fullmatch(r"/api/tasks/([^/]+)/log", path)
            if m:
                task = state.tasks.get(m.group(1))
                if not task:
                    return self._error(404, "not_found")
                ts = state.now()
                task["log"].append({"at": ts, "message": self._body()["message"]})
                task["updatedAt"] = ts
                return self._send(200, task)
            m = re.fullmatch(r"/api/agent/issues/([^/]+)/claim", path)
            if m:
                issue, agent = state.issues.get(m.group(1)), self._agent()
                if not issue:
                    return self._error(404, "not_found")
                if not agent:
                    return self._error(400, "agent_identity_required")
                if issue["state"] != "queued":
                    return self._error(409, "issue_not_queued")
                if issue["claimedBy"] and issue["claimedBy"]["name"] != agent:
                    return self._error(409, "issue_claimed_by_other")
                issue["claimedBy"] = {"kind": "agent", "name": agent}
                issue["state"] = "in_progress"
                issue["updatedAt"] = state.now()
                return self._send(200, issue)
            m = re.fullmatch(r"/api/agent/issues/([^/]+)/replies", path)
            if m:
                issue, agent = state.issues.get(m.group(1)), self._agent()
                if not issue:
                    return self._error(404, "not_found")
                if issue["state"] == "closed":
                    return self._error(409, "issue_closed")
                if not issue["claimedBy"] or issue["claimedBy"]["name"] != agent:
                    return self._error(409, "issue_not_claimed")
                ts = state.now()
                issue["messages"].append({
                    "id": f"message_{ts}", "body": self._body()["body"],
                    "author": {"kind": "agent", "name": agent}, "createdAt": ts})
                issue["updatedAt"] = ts
                return self._send(200, issue)
            m = re.fullmatch(r"/api/agent/issues/([^/]+)/state", path)
            if m:
                issue, agent = state.issues.get(m.group(1)), self._agent()
                if not issue:
                    return self._error(404, "not_found")
                body = self._body()
                target = body.get("state")
                if target == "resolved":  # legacy alias for closed
                    target = "closed"
                if not issue["claimedBy"] or issue["claimedBy"]["name"] != agent:
                    return self._error(409, "issue_not_claimed")
                if target not in AGENT_ISSUE_STATES:
                    return self._error(403, "agent_issue_state_forbidden",
                                       "Agents may only mark issues in progress, "
                                       "needing human input, or closed with a "
                                       "resolution summary.")
                if issue["state"] == "closed":
                    return self._error(409, "issue_reopen_forbidden")
                if target == "closed" and not (body.get("resolutionSummary") or "").strip():
                    return self._error(400, "invalid_input",
                                       "resolutionSummary is required.")
                issue["state"] = target
                if target == "closed":
                    issue["resolutionSummary"] = body["resolutionSummary"]
                issue["updatedAt"] = state.now()
                return self._send(200, issue)
            if path == "/api/agent/issues":
                if not self._agent():
                    return self._error(400, "agent_identity_required")
                body = self._body()
                issue_id = f"issue_created_{state._tick}"
                issue = state.add_issue(issue_id, body.get("title") or "untitled",
                                        body=body.get("body", ""),
                                        project_path=body.get("projectPath"))
                return self._send(201, issue)
            if path == "/api/tasks":
                # Contract verb 5 + its gate (W-0158): an agent-created task
                # must be a child of a task the human delegated.
                body = self._body()
                if self._agent():
                    if body.get("delegated"):
                        return self._error(403, "agent_delegation_forbidden",
                                           "Agents cannot set delegated.")
                    parent = state.tasks.get(body.get("parentId") or "")
                    if not body.get("parentId"):
                        return self._error(403, "agent_task_parent_required",
                                           "Agent-created tasks must be parented "
                                           "to a delegated goal.")
                    if not parent or not parent.get("delegated"):
                        return self._error(403, "agent_task_parent_not_delegated",
                                           "The parent must be a delegated goal.")
                if not (body.get("title") or "").strip():
                    return self._error(400, "invalid_input", "title is required.")
                task_id = f"W-9{state._tick:03d}"
                task = state.add_task(
                    task_id, body["title"], status=body.get("status") or "ready",
                    delegated=bool(body.get("delegated")),
                    project_path=body.get("projectPath"),
                    parent_id=body.get("parentId"), tags=body.get("tags") or (),
                    notes=body.get("description") or "")
                return self._send(201, task)
            if path == "/api/decisions":
                body = self._body()
                if not (body.get("title") or "").strip():
                    return self._error(400, "invalid_input", "title is required.")
                # Work's rule: a recommendation carries its reason, and an
                # agent-filed decision carries one either way — with a
                # recommendation it says why that option, without one it says
                # why no lean is possible. Only silence is refused.
                recommended = body.get("recommendedOption")
                reason = (body.get("recommendationReason") or "").strip()
                if recommended and recommended not in (body.get("options") or []):
                    return self._error(400, "invalid_input",
                                       "recommendedOption must exactly match "
                                       "one of the recorded options.")
                if recommended and not reason:
                    return self._error(400, "decision_reason_required",
                                       "A recommendation must carry a "
                                       "recommendationReason.")
                if self._agent() and not reason:
                    return self._error(403, "decision_reason_required",
                                       "Agent-filed decisions state a "
                                       "recommendationReason.")
                ts = state.now()
                decision_id = f"decision_{state._tick}"
                state.decisions[decision_id] = {
                    "id": decision_id, "title": body["title"],
                    "detail": body.get("detail", ""),
                    "options": body.get("options") or [], "refs": body.get("refs") or [],
                    "recommendedOption": recommended,
                    "recommendationReason": reason or None,
                    "projectPath": body.get("projectPath"), "status": "open",
                    "createdAt": ts, "updatedAt": ts}
                return self._send(201, state.decisions[decision_id])
            self._error(404, "not_found")

    return Handler
