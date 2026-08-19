"""Stdlib HTTP client for Work's sanctioned agent surface (CONTRACT §2).

Dromond is a program, so it speaks HTTP to the local Work API with an
``X-Work-Agent`` identity header — never the ``work`` CLI, never Work's
files. Only the calls the sweeper needs exist here.

Failure model:
- Transport failure (Work down, timeout): the method logs one line and
  returns ``None``. Work being unreachable must never crash a supervisor.
- HTTP rejection (authority rules, 4xx/5xx): raises ``WorkError`` carrying
  the server's error code, so the sweeper can skip that item and continue.
"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_TIMEOUT = 10


class WorkError(Exception):
    """The Work API rejected a request (its authority rules are server-side)."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"work api {status} {code}: {message}")
        self.status = status
        self.code = code


class WorkClient:
    def __init__(self, api_url: str, identity: str = "dromond",
                 timeout: float = DEFAULT_TIMEOUT):
        self.api_url = api_url.rstrip("/")
        self.identity = identity
        self.timeout = timeout

    def _call(self, method: str, path: str, body: dict | None = None,
              params: dict | None = None):
        """One request. JSON in/out; ``None`` on transport failure."""
        url = self.api_url + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "X-Work-Agent": self.identity,
            **({"Content-Type": "application/json"} if data is not None else {}),
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            try:
                err = json.loads(exc.read()).get("error", {})
            except (ValueError, OSError):
                err = {}
            raise WorkError(exc.code, err.get("code", "http_error"),
                            err.get("message", str(exc))) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            print(f"dromond: work api unreachable ({method} {path}): {reason}",
                  file=sys.stderr)
            return None
        return json.loads(raw) if raw.strip() else {}

    # --- reads --------------------------------------------------------------

    def tasks(self, updated_since: str | None = None) -> list | None:
        got = self._call("GET", "/api/tasks", params={"updatedSince": updated_since})
        return None if got is None else got.get("tasks", [])

    def issues(self, updated_since: str | None = None) -> list | None:
        got = self._call("GET", "/api/agent/issues",
                         params={"updatedSince": updated_since})
        return None if got is None else got.get("issues", [])

    def task(self, task_id: str) -> dict | None:
        return self._call("GET", f"/api/tasks/{task_id}")

    def issue(self, issue_id: str) -> dict | None:
        return self._call("GET", f"/api/agent/issues/{issue_id}")

    def needs_you(self) -> list | None:
        got = self._call("GET", "/api/needs-you")
        return None if got is None else got.get("entries", [])

    def projects(self) -> list | None:
        """Work's project list: id, projectId, name, path, aliasPaths. Paths
        are relative to the workspace root."""
        got = self._call("GET", "/api/projects")
        return None if got is None else got.get("projects", [])

    def workspace_root(self) -> str | None:
        got = self._call("GET", "/api/health")
        return None if got is None else (got.get("workspace") or {}).get("root")

    # --- writeback: the five contract verbs (CONTRACT §3) ------------------

    def move_task(self, task_id: str, status: str, note: str | None = None):
        body = {"status": status, **({"note": note} if note else {})}
        return self._call("POST", f"/api/tasks/{task_id}/move", body)

    def check_task_item(self, task_id: str, section: str, index: int, *,
                        checked: bool = True, reason: str | None = None):
        """Tick, untick, or decline one requirement or acceptance criterion."""
        body = ({"section": section, "index": index,
                 "declined": True, "reason": reason} if reason is not None
                else {"section": section, "index": index, "checked": checked})
        return self._call("POST", f"/api/tasks/{task_id}/checklist", body)

    def log_task(self, task_id: str, message: str):
        return self._call("POST", f"/api/tasks/{task_id}/log", {"message": message})

    def claim_issue(self, issue_id: str):
        return self._call("POST", f"/api/agent/issues/{issue_id}/claim", {})

    def reply_issue(self, issue_id: str, body: str):
        return self._call("POST", f"/api/agent/issues/{issue_id}/replies",
                          {"body": body})

    def set_issue_state(self, issue_id: str, state: str, reason: str | None = None,
                        resolution_summary: str | None = None):
        body = {"state": state}
        if reason is not None:
            body["reason"] = reason
        if resolution_summary is not None:
            body["resolutionSummary"] = resolution_summary
        return self._call("POST", f"/api/agent/issues/{issue_id}/state", body)

    def create_issue(self, body: str, title: str | None = None,
                     project_path: str | None = None):
        """CONTRACT §3 verb 4 — the findings filer (DESIGN §9). No
        ``delegated`` field exists on issue create: not-delegated is the
        server default and the flag is human-only, so triage is guaranteed
        by sending nothing."""
        payload = {"body": body}
        if title is not None:
            payload["title"] = title
        if project_path is not None:
            payload["projectPath"] = project_path
        return self._call("POST", "/api/agent/issues", payload)

    def create_task(self, title: str, parent_id: str,
                    project_path: str | None = None,
                    description: str | None = None, tags: list | None = None):
        """CONTRACT §3 verb 5 — propose follow-on work (Work side: W-0158).

        Always parented, never top-level. ``delegated`` is deliberately not
        sent: Work rejects the flag from any agent identity, and its default
        is false, which is exactly the required value.
        """
        payload = {"title": title, "parentId": parent_id}
        if project_path is not None:
            payload["projectPath"] = project_path
        if description is not None:
            payload["description"] = description
        if tags:
            payload["tags"] = list(tags)
        return self._call("POST", "/api/tasks", payload)

    def create_decision(self, title: str, detail: str | None = None,
                        options: list | None = None, refs: list | None = None,
                        project_path: str | None = None):
        """A choice only the human may make — lands in the needs-you queue."""
        payload = {"title": title}
        if detail is not None:
            payload["detail"] = detail
        if options:
            payload["options"] = list(options)
        if refs:
            payload["refs"] = list(refs)
        if project_path is not None:
            payload["projectPath"] = project_path
        return self._call("POST", "/api/decisions", payload)


def from_cfg(cfg: dict) -> WorkClient | None:
    """Client for the one configured Work server (DESIGN §2), or None when
    [work] is off."""
    w = cfg.get("work", {})
    if not w.get("enabled") or not w.get("api_url"):
        return None
    return WorkClient(w["api_url"], identity=w.get("agent_identity", "dromond"))
