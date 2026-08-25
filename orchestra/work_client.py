"""Stdlib HTTP client for Work's sanctioned agent surface (CONTRACT §2).

Orchestra is a program, so it speaks HTTP to the local Work API with an
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


def retryable(error: WorkError) -> bool:
    """Whether Work asked us to retry rather than settle an authority refusal."""
    return error.status >= 500 or error.status in (408, 429)


class WorkClient:
    def __init__(self, api_url: str, identity: str = "orchestra",
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
            print(f"orchestra: work api unreachable ({method} {path}): {reason}",
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

    def decisions(self) -> list | None:
        got = self._call("GET", "/api/decisions")
        return None if got is None else got.get("decisions", [])

    def projects(self) -> list | None:
        """Work's project list: id, projectId, name, path, aliasPaths. Paths
        are relative to the workspace root."""
        got = self._call("GET", "/api/projects")
        return None if got is None else got.get("projects", [])

    def workspace_root(self) -> str | None:
        got = self._call("GET", "/api/health")
        return None if got is None else (got.get("workspace") or {}).get("root")

    # --- writeback: the five contract verbs (CONTRACT §3) ------------------

    def check_task_item(self, task_id: str, section: str, index: int, *,
                        checked: bool = True, reason: str | None = None):
        """Tick, untick, or decline one requirement or acceptance criterion."""
        body = ({"section": section, "index": index,
                 "declined": True, "reason": reason} if reason is not None
                else {"section": section, "index": index, "checked": checked})
        return self._call("POST", f"/api/tasks/{task_id}/checklist", body)

    def decline_unaccounted(self, task_id: str, reason: str) -> int | None:
        """Decline, on a run's behalf, every criterion it left open.

        CONTRACT §3 verb 2: no task leaves an agent's hands with a criterion
        unanswered. The worker owns that answer and is told to give it. A run
        that dies, is killed, or ignores the protocol gives none, so whoever
        reports for it answers the only way it honestly can: it declines what
        is left, saying who did not account for it. It never ticks anything; a
        tick is a claim that work was verified, and the reporter verified
        nothing. Work refuses a ``landed`` fact while anything is unaccounted,
        so this runs before that fact is appended.
        """
        task = self.task(task_id)
        if task is None:
            return None
        declined = 0
        for section, key in (("requirements", "requirements"),
                             ("acceptance", "acceptanceCriteria")):
            for index, entry in enumerate(task.get(key) or []):
                if entry.get("checked") or entry.get("declined"):
                    continue
                posted = self.check_task_item(task_id, section, index,
                                              reason=reason[:2000])
                if posted is None:
                    return None
                declined += 1
        return declined

    def log_task(self, task_id: str, message: str):
        return self._call("POST", f"/api/tasks/{task_id}/log", {"message": message})

    def claim_issue(self, issue_id: str):
        return self._call("POST", f"/api/agent/issues/{issue_id}/claim", {})

    def reply_issue(self, issue_id: str, body: str):
        return self._call("POST", f"/api/agent/issues/{issue_id}/replies",
                          {"body": body})

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

    def create_decision(self, title: str, *, recommendation_reason: str,
                        detail: str | None = None, options: list | None = None,
                        refs: list | None = None,
                        project_path: str | None = None,
                        recommended_option: str | None = None):
        """A choice only the human may make — lands in the needs-you queue.

        Work refuses an agent-filed decision without a
        ``recommendationReason`` (``decision_reason_required``): with a
        recommendation it says why that option, without one it says why no
        lean is possible. Every call this client makes carries the agent
        identity, so the reason is required here too."""
        payload = {"title": title,
                   "recommendationReason": recommendation_reason}
        if detail is not None:
            payload["detail"] = detail
        if options:
            payload["options"] = list(options)
        if recommended_option is not None:
            payload["recommendedOption"] = recommended_option
        if refs:
            payload["refs"] = list(refs)
        if project_path is not None:
            payload["projectPath"] = project_path
        return self._call("POST", "/api/decisions", payload)


def fact_line(tag: str, verb: str, **fields) -> str:
    """One run fact for a task's log or an issue's thread (CONTRACT §3 verb 1,
    Work docs/ARTIFACT-SCHEMA.md "Run facts").

    ``tag`` is the bracketed run identity; empty fields are dropped, and the
    last field's value runs to the end of the line, so pass free text last.

    ponytail: every value is flattened to one line. A task's progress log is
    line-oriented and a broken fact reads as prose, while an issue's
    ``summary=`` may legally span lines — the full text is in the report
    comment beside it either way. Split the flatten per-kind if a board ever
    wants the paragraphs back.
    """
    pairs = "".join(f" {key}={' '.join(str(value).split())}"
                    for key, value in fields.items()
                    if value is not None and str(value).strip())
    return f"{tag} fact: {verb}{pairs}"


def verifier_identity(slug: str) -> str:
    """Attribution for a sign-off run (W-0269): ``verify/`` then the run slug,
    never the worker's identity. A fact carries it as a prefix; Work reads the
    prefix as attribution, not authority."""
    return f"verify/{slug}"


def from_cfg(cfg: dict) -> WorkClient | None:
    """Client for the one configured Work server (DESIGN §2), or None when
    [work] is off."""
    w = cfg.get("work", {})
    if not w.get("enabled") or not w.get("api_url"):
        return None
    return WorkClient(w["api_url"], identity=w.get("agent_identity", "orchestra"))
