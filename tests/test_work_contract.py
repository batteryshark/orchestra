"""Conformance suite against the REAL Work server (CONTRACT §2–§3).

Every other test module talks to tests/fake_work.py, a hand-written mirror
of Work's authority rules — which is exactly how drift ships: the fake and
the prose both said one thing while lib/local-workspace.mjs said another
(the legacy ``agents`` list read as delegation, ffac070; "four contract
verbs" vs five, 0fa36a8). This module boots the actual ``work`` server from
the sibling checkout, pulls its live capability catalog, and asserts every
claim Dromond depends on. When either side changes, this fails BEFORE a
live sweep misbehaves.

The whole module skips cleanly when node or the checkout is absent, so CI
without the sibling stays green. The Work checkout is found next to this
repo (or via WORK_CHECKOUT); the server runs against a throwaway root with
its own registry file (WORK_REGISTRY_FILE), so the developer's real
~/.work/roots.json is never touched.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from dromond import sweeper
from dromond.work_client import WorkClient, WorkError, fact_line

NODE = shutil.which("node")


def _find_work_checkout() -> Path | None:
    candidates = [
        os.environ.get("WORK_CHECKOUT"),
        # Work's checkout moved out of work-management; both names are tried,
        # because a skip here reads as green while testing nothing.
        Path(__file__).resolve().parents[2] / "work",
        Path(__file__).resolve().parents[2] / "work-management" / "project-manager-thing",
    ]
    for candidate in candidates:
        if candidate and (Path(candidate) / "bin" / "work.mjs").is_file():
            return Path(candidate)
    return None


WORK_CHECKOUT = _find_work_checkout()

# Skip is for machines that legitimately lack the sibling checkout. On the
# machine that OWNS the contract, a silent skip reads as green while testing
# nothing — set WORK_CONTRACT_REQUIRED=1 there so a moved checkout FAILS.
if os.environ.get("WORK_CONTRACT_REQUIRED") and not (NODE and WORK_CHECKOUT):
    raise RuntimeError(
        "WORK_CONTRACT_REQUIRED is set but node or the Work checkout is "
        "missing (set WORK_CHECKOUT to the project-manager-thing path)")

# What dromond/work_client.py actually sends, statically: one row per verb,
# with every body shape the client can produce. Conformance means (a) the
# operation exists in the live catalog at this method/path, (b) no schema-
# required key is missing from any client body, and (c) no client key is
# outside the schema's properties (Work's schemas set additionalProperties
# false, so an unknown key is a rejected request, not a warning).
CLIENT_CALLS = [
    # CONTRACT §3 verb 1 — comment.
    ("tasks.log", "POST", "/api/tasks/{id}/log", [{"message"}]),
    ("issues.reply", "POST", "/api/agent/issues/{id}/replies", [{"body"}]),
    # Lifecycle: an issue claim is ownership. A run's own lifecycle events are
    # facts on the two comment routes above (CONTRACT 0.8) — it never
    # transitions an item, so tasks.move and issues.update-state are not
    # client calls any more.
    ("issues.claim", "POST", "/api/agent/issues/{id}/claim", [set()]),
    # Verb 2 — account for checklist items.
    ("tasks.checklist", "POST", "/api/tasks/{id}/checklist",
     [{"section", "index", "checked"},
      {"section", "index", "declined", "reason"}]),
    # Verb 4 — file findings.
    ("issues.create", "POST", "/api/agent/issues",
     [{"body"}, {"body", "title", "projectPath"}]),
    ("decisions.create", "POST", "/api/decisions",
     [{"title", "recommendationReason"},
      {"title", "recommendationReason", "detail", "options",
       "recommendedOption", "refs", "projectPath"}]),
    # Verb 5 — propose follow-on work.
    ("tasks.create", "POST", "/api/tasks",
     [{"title", "parentId"},
      {"title", "parentId", "projectPath", "description", "tags"}]),
]


def _request(origin, method, path, body=None, agent=None):
    """One raw HTTP exchange; returns (status, parsed json). Unlike
    WorkClient this can speak as the human (no X-Work-Agent header)."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if agent:
        headers["X-Work-Agent"] = agent
    req = urllib.request.Request(origin + path, data=data, method=method,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


@unittest.skipUnless(NODE and WORK_CHECKOUT,
                     "needs node and the sibling Work checkout")
class WorkContract(unittest.TestCase):
    """One real server for the class; each test creates its own records."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="work-contract-")
        cls.root = Path(cls.tmp) / "workspace"
        cls.root.mkdir()
        work_mjs = str(WORK_CHECKOUT / "bin" / "work.mjs")
        env = {**os.environ,
               # Temp roots are refused by default (they vanish on reboot);
               # Work's own tests set the same override.
               "WORK_ALLOW_TEMP_ROOTS": "1",
               # Never the developer's real ~/.work/roots.json.
               "WORK_REGISTRY_FILE": str(Path(cls.tmp) / "roots.json")}
        subprocess.run([NODE, work_mjs, "init", str(cls.root)], env=env,
                       check=True, capture_output=True, text=True, timeout=60)
        cls.proc = subprocess.Popen(
            [NODE, work_mjs, "serve", str(cls.root), "--no-ui",
             "--api-port", "0"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True)
        cls.origin = cls._await_origin()
        cls.client = WorkClient(cls.origin, identity="dromond")
        cls.tasks_dir = cls.root / ".work" / "tasks"

    @classmethod
    def _await_origin(cls) -> str:
        """Scan stdout on a thread (select+readline lose lines to the stream
        buffer). The thread keeps draining for the server's lifetime, so a
        chatty server can never block on a full pipe."""
        seen, origin, found = [], [], threading.Event()
        def scan():
            for line in cls.proc.stdout:
                seen.append(line)
                match = re.search(r"API ready at (http://[\d.]+:\d+)", line)
                if match and not found.is_set():
                    origin.append(match.group(1))
                    found.set()
            found.set()  # EOF: the server exited
        threading.Thread(target=scan, daemon=True).start()
        found.wait(timeout=30)
        if origin:
            return origin[0]
        cls.proc.terminate()
        raise RuntimeError("work serve never became ready:\n" + "".join(seen))

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # --- helpers -------------------------------------------------------------

    def human(self, method, path, body=None):
        return _request(self.origin, method, path, body)

    def agent_raw(self, method, path, body=None):
        return _request(self.origin, method, path, body, agent="dromond")

    def new_task(self, title, **fields):
        status, task = self.human("POST", "/api/tasks",
                                  {"title": title, **fields})
        self.assertEqual(status, 201, task)
        return task

    # --- CONTRACT §2: reads and the item schema ------------------------------

    def test_read_surface_answers_the_client(self):
        # The class shares one server, so assert shape, not emptiness.
        task = self.new_task("A task the reads can see")
        self.assertIn(task["id"], [t["id"] for t in self.client.tasks()])
        self.assertIsInstance(self.client.issues(), list)
        self.assertIsInstance(self.client.needs_you(), list)
        self.assertIsInstance(self.client.projects(), list)
        self.assertEqual(Path(self.client.workspace_root()).resolve(),
                         self.root.resolve())
        # The agent issue surface refuses anonymous callers.
        status, body = self.human("GET", "/api/agent/issues")
        self.assertEqual((status, body["error"]["code"]),
                         (400, "agent_identity_required"))

    def test_legacy_agents_list_is_history_not_delegation(self):
        """CONTRACT §2 (0.2): the ``delegated`` boolean, and nothing else,
        hands an item to automation. The legacy ``agents`` name list recorded
        who did the work in an older system; one reading of it as delegation
        offered 96 finished records to the runner (ffac070)."""
        task = self.new_task("Legacy record")
        pathname = self.tasks_dir / f"{task['id']}.md"
        source = pathname.read_text()
        self.assertIn("delegated: false", source)
        pathname.write_text(source.replace(
            "delegated: false", 'agents: ["claude","codex"]', 1))

        served = self.client.task(task["id"])
        self.assertIs(served["delegated"], False)
        self.assertNotIn("agents", served)  # Work no longer emits the key
        self.assertFalse(sweeper.is_delegated(served, "dromond"))
        self.assertFalse(sweeper.is_delegated(served, "claude"))

        delegated = self.new_task("Explicit tick", delegated=True)
        self.assertTrue(sweeper.is_delegated(self.client.task(delegated["id"]),
                                             "dromond"))

    def test_agent_cannot_flip_delegation_or_edit_tasks(self):
        task = self.new_task("Human-owned card")
        status, body = self.agent_raw("PATCH", f"/api/tasks/{task['id']}",
                                      {"delegated": True})
        self.assertEqual((status, body["error"]["code"]),
                         (403, "agent_delegation_forbidden"))
        status, body = self.agent_raw("PATCH", f"/api/tasks/{task['id']}",
                                      {"title": "renamed"})
        self.assertEqual((status, body["error"]["code"]),
                         (403, "agent_task_edit_forbidden"))

    # --- CONTRACT §3: the five verbs against the live catalog ----------------

    def test_catalog_covers_every_client_call(self):
        status, catalog = self.human("GET", "/api/agent/operations")
        self.assertEqual(status, 200)
        listed = {op["id"] for op in catalog["operations"]}
        for op_id, method, path, variants in CLIENT_CALLS:
            with self.subTest(operation=op_id):
                self.assertIn(op_id, listed)
                status, got = self.human("GET",
                                         f"/api/agent/operations/{op_id}")
                self.assertEqual(status, 200)
                operation = got["operation"]
                self.assertEqual(operation["transport"]["api"],
                                 {"method": method, "path": path})
                schema = operation["inputSchema"]
                properties = schema.get("properties", {})
                for required in schema.get("required", []):
                    for sent in variants:
                        self.assertIn(
                            required, sent,
                            f"{op_id}: schema requires {required!r} but the "
                            f"client can send a body without it")
                if schema.get("additionalProperties", True) is False:
                    for sent in variants:
                        for key in sent:
                            self.assertIn(
                                key, properties,
                                f"{op_id}: client sends {key!r}, which the "
                                f"schema rejects (additionalProperties false)")

    def test_catalog_vocabulary_covers_what_the_sweeper_sends(self):
        _, check_op = self.human("GET", "/api/agent/operations/tasks.checklist")
        sections = check_op["operation"]["inputSchema"]["properties"]["section"]
        self.assertLessEqual({"requirements", "acceptance"},
                             set(sections["enum"]))

    # --- CONTRACT 0.8: facts derive the board, and never write status --------

    def test_the_facts_the_sweeper_appends_derive_the_board(self):
        """Exactly the lines dromond now emits, against the real deriver."""
        task = self.new_task("Fact grammar", delegated=True, status="ready")
        item, tag = task["id"], "[dromond/brisk_otter]"

        self.client.log_task(item, fact_line(tag, "claimed", run=41))
        self.assertEqual(self.client.task(item)["status"], "in_progress")
        self.client.log_task(item, fact_line(
            tag, "landed", sha="9c1f2ab", revert="git revert -m 1 9c1f2ab"))
        landed = self.client.task(item)
        self.assertEqual(landed["status"], "review")
        self.assertEqual(landed["storedStatus"], "ready")
        self.client.log_task(item, fact_line("[verify/quiet_owl]", "verified"))
        self.assertEqual(self.client.task(item)["status"], "done")

        # The human takes it back, and every earlier fact becomes history.
        self.human("POST", f"/api/tasks/{item}/move", {"status": "blocked"})
        self.client.log_task(item, fact_line(tag, "landed"))
        settled = self.client.task(item)
        self.assertEqual((settled["status"], settled["storedStatus"]),
                         ("blocked", "blocked"))

    def test_a_halt_reason_reaches_the_board(self):
        task = self.new_task("Halting", delegated=True, status="ready")
        item, tag = task["id"], "[dromond/brisk_otter]"
        self.client.log_task(item, fact_line(tag, "claimed", run=42))
        self.client.log_task(item, fact_line(tag, "halted",
                                             reason="the staging box is down"))
        halted = self.client.task(item)
        self.assertEqual(halted["status"], "blocked")
        self.assertEqual(halted["blockedReason"], "the staging box is down")
        self.assertIn(item, [entry["id"] for entry in self.client.needs_you()])

    def test_the_log_route_gates_nothing_so_orchestra_accounts_first(self):
        """The checklist gate lives on the legacy move bridge, not on the log
        route a fact is appended through. Work therefore does NOT refuse a
        `landed` fact with criteria open — dromond/sweeper.py declines them on
        the run's behalf BEFORE appending the fact, and that ordering is the
        only thing keeping CONTRACT §3 verb 2 true."""
        task = self.new_task("Ungated append", delegated=True, status="ready",
                             acceptanceCriteria=["tests pass"])
        item, tag = task["id"], "[dromond/brisk_otter]"
        self.client.log_task(item, fact_line(tag, "claimed", run=43))
        self.client.log_task(item, fact_line(tag, "landed"))
        self.assertEqual(self.client.task(item)["status"], "review")
        self.assertFalse(self.client.task(item)["acceptanceCriteria"][0]["checked"])

    # --- verb 5 and its gate --------------------------------------------------

    def test_agent_task_creation_gate(self):
        status, body = self.agent_raw("POST", "/api/tasks",
                                      {"title": "unparented"})
        self.assertEqual((status, body["error"]["code"]),
                         (403, "agent_task_parent_required"))

        plain = self.new_task("Plain human task")
        with self.assertRaises(WorkError) as caught:
            self.client.create_task("child of a plain task", plain["id"])
        self.assertEqual(caught.exception.code,
                         "agent_task_parent_not_delegated")

        goal = self.new_task("Delegated goal", delegated=True)
        status, body = self.agent_raw(
            "POST", "/api/tasks",
            {"title": "self-delegating", "parentId": goal["id"],
             "delegated": True})
        self.assertEqual((status, body["error"]["code"]),
                         (403, "agent_delegation_forbidden"))

        child = self.client.create_task("Proposed follow-on", goal["id"],
                                        description="found while sweeping",
                                        tags=["dromond"])
        self.assertEqual(child["parentId"], goal["id"])
        self.assertIs(child["delegated"], False)

    # --- issue lifecycle vocabulary -------------------------------------------

    def test_issue_lifecycle_round_trips(self):
        status, issue = self.human("POST", "/api/issues",
                                   {"body": "the sweeper found a leak"})
        self.assertEqual(status, 201)
        self.assertEqual(issue["state"], "queued")
        issue_id = issue["id"]

        # Claiming is ownership AND the run's first fact; everything after it
        # is a fact in the thread (CONTRACT 0.8).
        self.assertEqual(self.client.claim_issue(issue_id)["state"],
                         "in_progress")
        tag = "[dromond/brisk_otter]"
        self.assertEqual(
            self.client.reply_issue(issue_id, fact_line(
                tag, "needs_human", reason="which fix?"))["state"],
            "needs_human")
        closed = self.client.reply_issue(issue_id, fact_line(
            tag, "resolved", summary="plugged it"))
        self.assertEqual(closed["state"], "closed")
        self.assertEqual(closed["resolutionSummary"], "plugged it")
        self.assertEqual(closed["storedState"], "queued")
        # A run cannot talk its way past the close it derived, human or not.
        with self.assertRaises(WorkError) as caught:
            self.client.reply_issue(issue_id, "one more thing")
        self.assertEqual(caught.exception.code, "issue_closed")

    def test_agent_filed_issue_starts_queued_and_not_delegated(self):
        created = self.client.create_issue("retry loop leaks connections",
                                           title="Connection leak")
        self.assertEqual(created["state"], "queued")
        self.assertIs(created["delegated"], False)

    # --- decision filing --------------------------------------------------------

    def test_agent_decision_requires_a_recommendation_reason(self):
        status, body = self.agent_raw(
            "POST", "/api/decisions",
            {"title": "Which way?", "options": ["A", "B"]})
        self.assertEqual((status, body["error"]["code"]),
                         (403, "decision_reason_required"))
        created = self.client.create_decision(
            "Which way?", detail="two viable routes",
            options=["A", "B"],
            recommendation_reason="No lean: both routes pass the same tests.")
        self.assertTrue(created["id"].startswith("decision_"))
        entries = self.client.needs_you()
        self.assertIn(created["id"], [entry["id"] for entry in entries])

    # --- checklist gate ---------------------------------------------------------

    def test_declining_is_always_available_and_only_silence_is_refused(self):
        task = self.new_task("Gated goal", delegated=True, status="ready",
                             requirements=["keep the API stable"],
                             acceptanceCriteria=["tests pass", "docs updated"])
        item_id = task["id"]

        # A decline without a reason is refused, not silently accounted for.
        with self.assertRaises(WorkError):
            self.client.check_task_item(item_id, "acceptance", 1, reason="")

        self.client.check_task_item(item_id, "requirements", 0, checked=True)
        self.client.check_task_item(item_id, "acceptance", 0, checked=True)
        accounted = self.client.check_task_item(
            item_id, "acceptance", 1, reason="blocked on the docs freeze")
        declined = accounted["acceptanceCriteria"][1]
        self.assertEqual((declined["declined"], declined["reason"]),
                         (True, "blocked on the docs freeze"))


if __name__ == "__main__":
    unittest.main()
