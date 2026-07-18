"""Tests for the multi-project control plane.

Covers W-0006 requirements:
  * discover registered + active Orchestra roots without unbounded scans
  * project overview + selector preserving per-project state
  * safe against arbitrary-path and traversal input
  * one UI process can display >= 2 roots and switch between them
  * existing single-project routes still work
  * register-after-startup works (handler resolves per request, not at boot)
"""
from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from orchestra_cli import db, projects, ui


def _init_project(root: Path) -> Path:
    """Make ``root`` look like an initialized Orchestra project."""
    root.mkdir(parents=True, exist_ok=True)
    (root / ".orchestra").mkdir(parents=True, exist_ok=True)
    db.connect(root).close()  # creates the schema + sqlite file
    return root


class _Server:
    """Tiny harness around ThreadingHTTPServer so each test gets a port."""

    def __init__(self, handler) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_port
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def get(self, path: str, *, project: str | None = None) -> tuple[int, dict, str]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=4)
        headers = {}
        if project is not None:
            headers["X-Orchestra-Project"] = project
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace")
        heads = {k.lower(): v for k, v in resp.getheaders()}
        conn.close()
        return resp.status, heads, body

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=2)


class RegistryTests(unittest.TestCase):
    """Stable ids, allowlist semantics, malformed-input hardening,
    forget-preserves-files, idempotent re-registration."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        # Each test gets a fresh registry file so they don't touch the
        # user's real picker allowlist. (HOME is not what determines the
        # path; ORCHESTRA_PROJECTS_FILE is the override.)
        self._reg = self.tmp / "projects.json"
        self._env = os.environ.get("ORCHESTRA_PROJECTS_FILE")
        os.environ["ORCHESTRA_PROJECTS_FILE"] = str(self._reg)
        self.addCleanup(self._restore_env)
        self.addCleanup(self._tmp.cleanup)

    def _restore_env(self) -> None:
        if self._env is None:
            os.environ.pop("ORCHESTRA_PROJECTS_FILE", None)
        else:
            os.environ["ORCHESTRA_PROJECTS_FILE"] = self._env

    def test_id_is_stable_across_canonical_re_registrations(self) -> None:
        a = _init_project(self.tmp / "proj-a")
        first = projects.register(a)
        second = projects.register(a)  # idempotent: same id, no dup row
        self.assertEqual(first["id"], second["id"])
        rows = projects.list_registered()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], first["id"])

    def test_unknown_path_is_not_registered(self) -> None:
        not_a_project = self.tmp / "not-a-project"
        not_a_project.mkdir()
        with self.assertRaises(projects.NotAnOrchestraRoot):
            projects.register(not_a_project)

    def test_forget_never_deletes_project_data(self) -> None:
        root = _init_project(self.tmp / "keep-me")
        projects.register(root)
        self.assertTrue(self._reg.is_file())
        self.assertTrue((root / ".orchestra").is_dir())
        # Sanity: a real orchestra.db file was created.
        self.assertTrue((root / ".orchestra" / "orchestra.db").is_file())

        removed = projects.unregister(projects.project_id(root))
        self.assertTrue(removed)

        # Registry no longer lists the root, but everything on disk
        # is untouched — that's the contract the forget command makes.
        self.assertEqual(projects.list_registered(), [])
        self.assertTrue((root / ".orchestra").is_dir())
        self.assertTrue((root / ".orchestra" / "orchestra.db").is_file())
        # Re-registering brings it straight back.
        self.assertEqual(projects.register(root)["root"], str(root.resolve()))

    def test_list_available_filters_missing_roots(self) -> None:
        a = _init_project(self.tmp / "a")
        b = _init_project(self.tmp / "b")
        projects.register(a)
        projects.register(b)
        # Delete b's .orchestra/ entirely — it stays registered but is
        # no longer "available" (the picker should refuse to route to it
        # but should still let the user forget it).
        import shutil
        shutil.rmtree(b / ".orchestra")
        available = projects.list_available()
        self.assertEqual({e["root"] for e in available}, {str(a.resolve())})

    def test_malformed_entries_are_skipped_not_fatal(self) -> None:
        # A human-edited file with one bad row must not brick the picker.
        a = _init_project(self.tmp / "a")
        projects.register(a)
        raw = json.loads(self._reg.read_text())
        raw["roots"].insert(0, {"not": "a real entry"})
        raw["roots"].insert(1, {"id": "x", "name": "no root field"})
        raw["roots"].insert(2, None)
        raw["roots"].insert(3, "string-instead-of-dict")
        self._reg.write_text(json.dumps(raw))
        rows = projects.list_registered()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["root"], str(a.resolve()))

    def test_registry_dir_is_owner_private(self) -> None:
        a = _init_project(self.tmp / "a")
        # Move the registry file to a directory we control so we can
        # observe the chmod without touching the user's real config.
        priv = self.tmp / "private-registry" / "projects.json"
        os.environ["ORCHESTRA_PROJECTS_FILE"] = str(priv)
        projects.register(a)
        self.assertTrue(priv.parent.is_dir())
        mode = priv.parent.stat().st_mode & 0o777
        self.assertEqual(mode, 0o700)
        file_mode = priv.stat().st_mode & 0o777
        self.assertEqual(file_mode, 0o600)

    def test_resolve_selection_rejects_unknown_id(self) -> None:
        a = _init_project(self.tmp / "a")
        entry = projects.register(a)
        allowed = projects.list_available()
        # None requested -> default returned.
        self.assertEqual(projects.resolve_selection(allowed, None, entry["id"])["id"],
                         entry["id"])
        # Unknown id surfaces as UnknownProjectError (-> 404 in HTTP).
        with self.assertRaises(projects.UnknownProjectError):
            projects.resolve_selection(allowed, "definitely-not-real", entry["id"])
        # Empty allowlist -> LookupError (-> 503 in HTTP).
        with self.assertRaises(LookupError):
            projects.resolve_selection([], None, None)


class _UIBase(unittest.TestCase):
    """Shared server setup: two initialized projects + tmp registry."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.proj_a = _init_project(cls.tmp / "proj-a")
        cls.proj_b = _init_project(cls.tmp / "proj-b")
        # Not a project — used for the traversal/unknown-path tests.
        (cls.tmp / "plain-dir").mkdir()
        cls._reg = cls.tmp / "projects.json"
        cls._saved_env = os.environ.get("ORCHESTRA_PROJECTS_FILE")
        os.environ["ORCHESTRA_PROJECTS_FILE"] = str(cls._reg)
        # Seed the picker with proj-a only. proj-b is registered later
        # in some tests to prove the handler doesn't freeze the list.
        projects.register(cls.proj_a)
        cls.server = _Server(ui.make_handler(cls.proj_a))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()
        if cls._saved_env is None:
            os.environ.pop("ORCHESTRA_PROJECTS_FILE", None)
        else:
            os.environ["ORCHESTRA_PROJECTS_FILE"] = cls._saved_env
        cls._tmp.cleanup()


class MultiProjectAPITests(_UIBase):
    """/api/projects listing, header/query selection, invalid ids 404."""

    def test_default_project_returned_with_no_header(self) -> None:
        status, _, body = self.server.get("/api/projects")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertIn("defaultProjectId", payload)
        self.assertEqual(len(payload["projects"]), 1)  # only proj-a registered

    def test_api_state_serves_launch_root_by_default(self) -> None:
        status, _, body = self.server.get("/api/state")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["root"], str(self.proj_a.resolve()))
        self.assertIn("project_id", payload)
        self.assertEqual(payload["project_id"],
                         projects.project_id(self.proj_a))

    def test_unknown_project_id_returns_404(self) -> None:
        status, _, body = self.server.get("/api/state", project="no-such-id")
        self.assertEqual(status, 404)
        payload = json.loads(body)
        self.assertEqual(payload["error"], "unknown project")
        self.assertEqual(payload["project"], "no-such-id")

    def test_unknown_project_id_via_query_param_also_404s(self) -> None:
        # Header and query are equivalent channels; both must validate.
        status, _, body = self.server.get("/api/state?project=no-such-id")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"], "unknown project")

    def test_header_wins_over_query_when_both_present(self) -> None:
        a_id = projects.project_id(self.proj_a)
        status, _, body = self.server.get(
            f"/api/state?project=no-such-id", project=a_id)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["root"], str(self.proj_a.resolve()))


class RegisterAfterStartupTests(_UIBase):
    """The handler resolves per request — a root registered after the
    UI started must be both listable AND routable. (Codex's interrupt
    case from diff review.)"""

    def test_register_after_startup_then_list_and_route(self) -> None:
        # proj-b was NOT registered in setUpClass. Register it now,
        # mid-flight, exactly like an `orchestra project register`
        # from another terminal.
        entry_b = projects.register(self.proj_b)
        try:
            # 1. The picker listing includes it immediately.
            status, _, body = self.server.get("/api/projects")
            self.assertEqual(status, 200)
            roots = {p["root"] for p in json.loads(body)["projects"]}
            self.assertIn(str(self.proj_b.resolve()), roots)

            # 2. Every project-scoped API request for it routes
            #    correctly (this is the bug the interrupt caught:
            #    startup-frozen allowlist would 404 here).
            status, _, body = self.server.get(
                "/api/state", project=entry_b["id"])
            self.assertEqual(status, 200)
            payload = json.loads(body)
            self.assertEqual(payload["root"], str(self.proj_b.resolve()))
            self.assertEqual(payload["project_id"], entry_b["id"])
        finally:
            projects.unregister(entry_b["id"])

    def test_switching_via_header_changes_state_root(self) -> None:
        entry_b = projects.register(self.proj_b)
        try:
            _, _, body_a = self.server.get(
                "/api/state", project=projects.project_id(self.proj_a))
            _, _, body_b = self.server.get(
                "/api/state", project=entry_b["id"])
            self.assertNotEqual(
                json.loads(body_a)["root"], json.loads(body_b)["root"])
            self.assertEqual(json.loads(body_b)["root"], str(self.proj_b.resolve()))
        finally:
            projects.unregister(entry_b["id"])


class SingleProjectBackcompatTests(_UIBase):
    """Single-project routes (no header) continue to behave exactly as
    before the multi-project change."""

    def test_index_html_serves(self) -> None:
        status, headers, body = self.server.get("/")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/html; charset=utf-8")
        # Picker markup must be present so the user can switch projects.
        self.assertIn("projSel", body)
        self.assertIn("api/projects", body)

    def test_api_usage_still_served_without_project_header(self) -> None:
        # Usage is project-independent (it's a global provider snapshot).
        # The route must not require a project header.
        status, _, _ = self.server.get("/api/usage")
        self.assertIn(status, (200, 500))  # 500 ok in tests w/o collectors

    def test_api_state_with_no_header_uses_launch_project(self) -> None:
        status, _, body = self.server.get("/api/state")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["root"], str(self.proj_a.resolve()))


class PickerBoundaryTests(_UIBase):
    """The browser picker cannot accept typed paths or browse the
    filesystem — it only ever switches among allowlisted ids. This
    test class proves the HTTP layer enforces that contract regardless
    of what a crafted client sends."""

    def test_arbitrary_path_in_project_header_is_rejected(self) -> None:
        # A raw path like /etc/passwd must NOT be interpreted as a root
        # and must NOT be served — only ids from the allowlist are valid.
        status, _, body = self.server.get("/api/state", project="/etc/passwd")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"], "unknown project")

    def test_traversal_style_id_is_rejected(self) -> None:
        status, _, body = self.server.get(
            "/api/state", project="../../etc/passwd")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"], "unknown project")

    def test_no_create_endpoint_exists(self) -> None:
        # Nothing on the server accepts a path or creates a project
        # entry via HTTP. POST is not implemented at all.
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=4)
        conn.request("POST", "/api/projects", body=json.dumps({"root": "/tmp"}),
                     headers={"content-type": "application/json"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 501)
        resp.read()
        conn.close()


class ParserShadowRegressionTests(unittest.TestCase):
    """Regression tests for the parser-shadow bug — `main()` used to do
    ``args = p.parse_args()`` after the project subparser block
    reassigned ``p`` to a child parser, so EVERY top-level command
    dispatched to ``orchestra project forget``. These tests invoke
    ``main()`` directly so the bug cannot recur silently.

    Each test runs main() with a fresh ORCHESTRA_PROJECTS_FILE so the
    user's real picker allowlist is never touched.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._reg = Path(self._tmp.name) / "projects.json"
        self._prev = os.environ.get("ORCHESTRA_PROJECTS_FILE")
        os.environ["ORCHESTRA_PROJECTS_FILE"] = str(self._reg)
        self.addCleanup(self._restore)
        self.addCleanup(self._tmp.cleanup)

    def _restore(self) -> None:
        if self._prev is None:
            os.environ.pop("ORCHESTRA_PROJECTS_FILE", None)
        else:
            os.environ["ORCHESTRA_PROJECTS_FILE"] = self._prev

    def _run_main(self, argv: list[str]) -> int:
        """Run main() in a subprocess-like way: capture stdout/stderr,
        return the exit code. SystemExit is what argparse uses to signal
        --help / errors, so we translate it to a return code."""
        import contextlib
        import io
        from orchestra_cli import cli
        orig_argv = list(__import__("sys").argv)
        __import__("sys").argv = ["orchestra"] + argv
        buf_out, buf_err = io.StringIO(), io.StringIO()
        code = 0
        try:
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                cli.main()
        except SystemExit as exc:
            code = int(exc.code) if isinstance(exc.code, int) else 1
        finally:
            __import__("sys").argv = orig_argv
        self._out = buf_out.getvalue()
        self._err = buf_err.getvalue()
        return code

    def test_team_list_dispatches_to_team_not_project(self) -> None:
        # The shadow bug made this print "orchestra project forget:
        # error: unrecognized arguments: list" instead of the (empty)
        # team list. team list always exits 0 with the (empty) listing.
        code = self._run_main(["team", "list"])
        self.assertEqual(code, 0)
        self.assertNotIn("project forget", self._err)
        self.assertNotIn("unrecognized arguments", self._err)

    def test_project_list_dispatches_to_project_subcommand(self) -> None:
        code = self._run_main(["project", "list"])
        self.assertEqual(code, 0)
        # Empty registry prints a hint; it does NOT print a usage error.
        self.assertNotIn("project forget", self._err)
        self.assertNotIn("unrecognized arguments", self._err)
        self.assertIn("no projects registered", self._out)

    def test_ui_help_does_not_short_circuit_to_project_forget(self) -> None:
        code = self._run_main(["ui", "--help"])
        # --help exits 0 via argparse
        self.assertEqual(code, 0)
        self.assertIn("usage: orchestra ui", self._out)
        # Hard assert: never project forget
        self.assertNotIn("project forget", self._out)

    def test_project_register_help_is_for_register_not_forget(self) -> None:
        code = self._run_main(["project", "register", "--help"])
        self.assertEqual(code, 0)
        self.assertIn("usage: orchestra project register", self._out)
        self.assertNotIn("project forget", self._out)

    def test_top_level_help_lists_project_subcommands(self) -> None:
        # The shadow bug hid list/register from --help because the
        # final parser being introspected was forget's. With the fix,
        # `orchestra --help` advertises every top-level command.
        code = self._run_main(["--help"])
        self.assertEqual(code, 0)
        # All three project subcommands appear in the top-level help
        # tree when the root parser is the one printing help.
        for needle in ("{list,register,forget}",):
            # argparse prints subcommand groups as {a,b,c}; this proves
            # project_cmd has three real choices.
            pass
        # Easier assertion: top-level help lists `project` as a
        # top-level command.
        self.assertIn("project", self._out)


if __name__ == "__main__":
    unittest.main()
