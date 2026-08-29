"""In-process fake of Work's local agent API (stdlib http.server).

Mirrors the route shapes and server-side authority rules of
Work's local-api.mjs that the sweeper depends on:

- **stored status has one writer class: humans** (CONTRACT 0.8). An agent
  identity appends a run FACT — a task progress line, an issue message — and
  every read serves a status DERIVED from those facts plus the human's stored
  value. A legacy agent transition is bridged into its fact and writes no
  status, exactly as Work's own bridge does.
- agents may bridge a task move only from in_progress / review / blocked
  (403); a verifier identity (``verify/`` prefix, W-0269) may also send done
- agents may never PATCH a task (403) — except while the item carries the
  human's ``refine`` tag, which allows the six sections plus one ``tags``
  update whose only change is dropping that tag (W-0309)
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

# The transition an old client sends, and the fact it really means.
AGENT_MOVE_FACT = {"in_progress": "claimed", "review": "landed",
                   "blocked": "halted"}
# The tag-scoped edit allowance (W-0309): the human's `refine` tag is the
# request, and it is the only state in which an agent identity may PATCH a
# task. It buys the six sections and one tags update that drops the tag.
REFINE_TAG = "refine"
REFINE_FIELDS = {"description", "goal", "requirements", "acceptanceCriteria",
                 "plan", "notes"}
# Work collapsed "resolved" into "closed" (2026-08-14); the API still accepts
# "resolved" as an alias and stores "closed".
AGENT_ISSUE_STATES = {"in_progress", "needs_human", "closed"}
AGENT_ISSUE_FACT = {"in_progress": "claimed", "needs_human": "needs_human",
                    "closed": "resolved"}

# --- run facts (Work docs/ARTIFACT-SCHEMA.md § Run facts) --------------------
# A compact mirror of Work's parser and precedence rule; the authoritative one
# is lib/local-workspace.mjs. Tolerant read: the identity prefix is optional,
# unknown verbs and keys are ignored, `run` and `sha` stop at their first
# token, every other value runs to the next `key=` or the end of the fact.
FACT_LINE = re.compile(r"^(?:\[[^\]\n]*\]\s*)?fact:\s*([a-z_]+)\b[ \t]*([\s\S]*)$",
                       re.I)
FACT_KEY = re.compile(r"(?:^|\s)([a-z][a-z0-9_]*)=", re.I)
TASK_FACT_STATUS = {"claimed": "in_progress", "landed": "review",
                    "halted": "blocked", "failed": "blocked"}
ISSUE_FACT_STATE = {"claimed": "in_progress", "resolved": "closed",
                    "needs_human": "needs_human"}


def parse_fact(source: str, at: str) -> dict | None:
    match = FACT_LINE.match((source or "").strip())
    if not match:
        return None
    verb, tail = match.group(1).lower(), match.group(2)
    keys = list(FACT_KEY.finditer(tail))
    fields, free = {}, [tail if not keys else tail[:keys[0].start()]]
    for position, key in enumerate(keys):
        end = keys[position + 1].start() if position + 1 < len(keys) else len(tail)
        value = tail[key.end():end].strip()
        name = key.group(1).lower()
        if name in ("run", "sha"):
            first, _, rest = value.partition(" ")
            value, _ = first, free.append(rest)
        fields.setdefault(name, value)
    return {"verb": verb, "fields": fields, "text": " ".join(free).strip(),
            "at": at}


def live_run_facts(entries: list[tuple[str, str]], human_move_at) -> list[dict]:
    """The facts that govern the item now: those from the last ``claimed``,
    and only while that claim is newer than the human's last move. Ties go to
    the human — a human move dismisses every earlier run's narrative."""
    facts = [f for f in (parse_fact(text, at) for text, at in entries) if f]
    claims = [i for i, f in enumerate(facts) if f["verb"] == "claimed"]
    if not claims or facts[claims[-1]]["at"] <= (human_move_at or ""):
        return []
    return facts[claims[-1]:]


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
                    alias_paths=(), archived=False):
        """A Work project as /api/projects serves it: ``id`` is the human
        path-ish key an item's ``projectPath`` carries, ``projectId`` is the
        immutable UUID Orchestra keys on. ``path`` is workspace-relative.
        ``archived`` is Work's own flag, on every record it serves."""
        entry = {"id": work_id, "projectId": project_id,
                 "name": name or work_id, "path": path or work_id,
                 "aliasPaths": list(alias_paths), "archived": bool(archived)}
        self.projects.append(entry)
        return entry

    def add_task(self, task_id, title, *, status="ready", delegated=False,
                 agents=(), goal="", notes="", requirements="",
                 acceptance=(), project_path=None, depends_on=(),
                 blocked_by=(), parent_id=None, tags=()):
        # ``dependsOn`` is the single ordering edge a dispatcher must honor
        # (DESIGN §4). Work's read path folds a legacy ``blockedBy`` record
        # into ``dependsOn`` and never serves the key itself
        # (Work, lib/local-workspace.mjs) — mirror that here.
        ts = self.now()
        # First-occurrence order, deduped — exactly Work's read fold.
        folded = list(dict.fromkeys(list(depends_on) + list(blocked_by)))
        self.tasks[task_id] = {
            # `status` is what every read serves — DERIVED. `storedStatus` is
            # the human's own value, and `statusAt` is when they last set it.
            "id": task_id, "title": title, "status": status,
            "storedStatus": status, "statusAt": ts,
            "projectPath": project_path or self._default_project(),
            "delegated": delegated, "agents": list(agents),
            "dependsOn": folded,
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
            "storedState": state, "stateHistory": [],
            "projectPath": project_path or self._default_project(),
            "delegated": delegated, "agents": list(agents),
            "claimedBy": None,
            "resolutionSummary": None, "storedResolutionSummary": None,
            "messages": [], "createdAt": ts, "updatedAt": ts,
        }
        return self.issues[issue_id]

    # --- derivation (the read path every route ends with) -------------------

    def _rederive_task(self, task) -> dict:
        status, landed = task["storedStatus"], False
        for fact in live_run_facts([(e["message"], e["at"]) for e in task["log"]],
                                   task["statusAt"]):
            if fact["verb"] == "landed":
                landed = True
            if fact["verb"] == "verified":
                # Sign-off means done only on top of a landing.
                status = "done" if landed else status
                continue
            status = TASK_FACT_STATUS.get(fact["verb"], status)
        task["status"] = status
        return task

    def _rederive_issue(self, issue) -> dict:
        human = [e["at"] for e in issue["stateHistory"] if e["actor"] == "human"]
        state = issue["storedState"]
        summary = issue["storedResolutionSummary"]
        for fact in live_run_facts(
                [(m["body"], m["createdAt"]) for m in issue["messages"]
                 if (m.get("author") or {}).get("kind") == "agent"],
                human[-1] if human else None):
            nxt = ISSUE_FACT_STATE.get(fact["verb"])
            if not nxt:
                continue
            state = nxt
            if nxt == "closed":
                summary = fact["fields"].get("summary") or fact["text"] or summary
        issue["state"], issue["resolutionSummary"] = state, summary
        return issue

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
        """The one writer of stored status — and the move that dismisses every
        earlier run's narrative."""
        task = self.tasks[task_id]
        ts = self.now()
        task["log"].append({
            "at": ts,
            "message": f"Moved from {task['storedStatus']} to {status}."})
        task["storedStatus"], task["statusAt"] = status, ts
        task["updatedAt"] = ts
        self._rederive_task(task)

    def agent_claim(self, task_id, run=1, agent="orchestra"):
        """A run opening its window on a task the test did not sweep."""
        task = self.tasks[task_id]
        ts = self.now()
        task["log"].append({"at": ts,
                            "message": f"[{agent}] fact: claimed run={run}"})
        task["updatedAt"] = ts
        return self._rederive_task(task)

    def human_close_issue(self, issue_id, summary="closed by human"):
        """A human closes an issue: settled, with a resolution summary."""
        issue = self.issues[issue_id]
        self._human_issue_state(issue, "closed", summary=summary)

    def _human_issue_state(self, issue, state, summary=None):
        ts = self.now()
        issue["stateHistory"].append({"from": issue["storedState"], "to": state,
                                      "actor": "human", "at": ts})
        issue["storedState"], issue["storedResolutionSummary"] = state, summary
        issue["claimedBy"] = None
        issue["updatedAt"] = ts
        self._rederive_issue(issue)

    def human_reply(self, issue_id, body):
        issue = self.issues[issue_id]
        ts = self.now()
        issue["messages"].append({"id": f"message_{self._tick}", "body": body,
                                  "author": {"kind": "human"}, "createdAt": ts})
        issue["updatedAt"] = ts
        if issue["state"] in ("needs_human", "closed"):
            # Answering takes the item back: a human state event, which is
            # what dismisses the run's facts.
            self._human_issue_state(issue, "queued")
        else:
            self._rederive_issue(issue)

    def mutation_count(self) -> int:
        return sum(1 for method, _ in self.requests if method != "GET")

    # --- lifecycle ----------------------------------------------------------

    def start(self) -> str:
        handler = _make_handler(self)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        args=(0.01,), daemon=True)
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

        def _append_message(self, issue, body, author):
            ts = state.now()
            issue["messages"].append({"id": f"message_{ts}", "body": body,
                                      "author": author, "createdAt": ts})
            issue["updatedAt"] = ts

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
            if path == "/api/decisions":
                return self._send(200, {"decisions": list(state.decisions.values())})
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
            m = re.fullmatch(r"/api/tasks/([^/]+)", self.path)
            if m and self._agent():
                task = state.tasks.get(m.group(1))
                if not task:
                    return self._error(404, "not_found")
                return self._refine_edit(task, self._body())
            self._error(404, "not_found")

        def _refine_edit(self, task, body):
            """Work's one carve-out (W-0309): while an item carries the
            `refine` tag, an agent identity may rewrite the six sections and
            send `tags` once with only `refine` dropped. Every other field,
            and every edit once the tag is gone, is refused as before."""
            if REFINE_TAG not in (task.get("tags") or []):
                return self._error(403, "agent_task_edit_forbidden",
                                   "Agents cannot edit tasks.")
            if set(body) - REFINE_FIELDS - {"tags"}:
                return self._error(403, "agent_task_edit_forbidden",
                                   "Refining may not touch these fields.")
            if "tags" in body and set(body["tags"]) != \
                    set(task["tags"]) - {REFINE_TAG}:
                return self._error(403, "agent_task_edit_forbidden",
                                   "A refining tags update may only drop "
                                   "`refine`.")
            for field in REFINE_FIELDS & set(body):
                if field in ("requirements", "acceptanceCriteria"):
                    texts = [str(text) for text in body[field]]
                    # A replaced checklist loses its ticks — Work's rule, and
                    # the reason the brief says to resend every kept item.
                    task[field] = [{"checked": False, "declined": False,
                                    "reason": "", "text": text}
                                   for text in texts]
                    task["sections"][field] = "\n".join(
                        f"- [ ] {text}" for text in texts)
                else:
                    task["sections"][field] = body[field]
            if "tags" in body:
                task["tags"] = list(body["tags"])
            task["updatedAt"] = state.now()
            return self._send(200, state._rederive_task(task))

        def do_POST(self):
            state.requests.append(("POST", self.path))
            path = self.path
            m = re.fullmatch(r"/api/tasks/([^/]+)/move", path)
            if m:
                task = state.tasks.get(m.group(1))
                if not task:
                    return self._error(404, "not_found")
                body = self._body()
                status, agent = body.get("status"), self._agent()
                if not agent:
                    state.human_move(task["id"], status)
                    return self._send(200, task)
                # CONTRACT 0.8 bridge: a legacy transition becomes the fact it
                # really meant, and writes no status.
                verb = ("verified" if status == "done" and agent.startswith("verify/")
                        else AGENT_MOVE_FACT.get(status))
                if not verb:
                    return self._error(403, "task_status_forbidden",
                                       "Agents may only move tasks to "
                                       "in_progress, review, or blocked.")
                if verb in ("landed", "verified"):
                    # Handing work back still means answering for every item.
                    # Halting is never refused: a stopped run must be able to
                    # say so, or nobody is left to move the item.
                    open_items = [i for section in ("requirements",
                                                    "acceptanceCriteria")
                                  for i in task.get(section) or []
                                  if not i["checked"] and not i["declined"]]
                    if open_items:
                        return self._error(
                            409, "review_checklist_incomplete",
                            f"Cannot move {task['id']} to {status} with "
                            f"{len(open_items)} unaccounted checklist items.")
                note = body.get("note") or ""
                run = re.search(r"\brun[ =#]?(\d+)", note, re.I)
                fields = (f" run={run.group(1)}" if verb == "claimed" and run
                          else f" reason={note}" if verb == "halted" and note else "")
                ts = state.now()
                task["log"].append({
                    "at": ts, "message": f"[{agent}] fact: {verb}{fields}"})
                task["updatedAt"] = ts
                return self._send(200, state._rederive_task(task))
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
                # A fact IS a log line, so this route derives like any other.
                return self._send(200, state._rederive_task(task))
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
                # Claiming is a fact, not a transition. Ownership is not
                # status, so claimedBy is still written: the reply and state
                # gates run off it.
                issue["claimedBy"] = {"kind": "agent", "name": agent}
                self._append_message(issue, f"[{agent}] fact: claimed",
                                     {"kind": "agent", "name": agent})
                return self._send(200, state._rederive_issue(issue))
            m = re.fullmatch(r"/api/agent/issues/([^/]+)/replies", path)
            if m:
                issue, agent = state.issues.get(m.group(1)), self._agent()
                if not issue:
                    return self._error(404, "not_found")
                if issue["state"] == "closed":
                    return self._error(409, "issue_closed")
                if not issue["claimedBy"] or issue["claimedBy"]["name"] != agent:
                    return self._error(409, "issue_not_claimed")
                self._append_message(issue, self._body()["body"],
                                     {"kind": "agent", "name": agent})
                # A fact IS a message, so this route derives like any other.
                return self._send(200, state._rederive_issue(issue))
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
                # CONTRACT 0.8 bridge: the transition becomes its fact.
                verb = AGENT_ISSUE_FACT[target]
                value = (body.get("resolutionSummary") if target == "closed"
                         else body.get("reason"))
                key = "summary" if target == "closed" else "reason"
                self._append_message(
                    issue, f"[{agent}] fact: {verb}"
                           + (f" {key}={value}" if value else ""),
                    {"kind": "agent", "name": agent})
                return self._send(200, state._rederive_issue(issue))
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
                # Contract verb 5 + its gate (W-0158, amended 0.13): a child
                # of a delegated task stays direct; a TOP-LEVEL ask files an
                # adopt proposal decision — the human's approve click is the
                # create.
                body = self._body()
                if self._agent():
                    if body.get("delegated"):
                        return self._error(403, "agent_delegation_forbidden",
                                           "Agents cannot set delegated.")
                    parent = state.tasks.get(body.get("parentId") or "")
                    if not body.get("parentId"):
                        state._tick += 1
                        decision_id = f"decision_{state._tick}"
                        decision = {
                            "id": decision_id,
                            "title": f"Adopt task proposal from "
                                     f"{self._agent()}: "
                                     f"{body.get('title') or ''}"[:500],
                            "status": "open",
                            "options": ["Create the task", "Decline"],
                        }
                        state.decisions[decision_id] = decision
                        return self._send(200, {"proposed": True,
                                                "decision": decision})
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
