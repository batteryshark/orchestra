"""Per-run tokens and the route authority table (DESIGN §3/§5, W-0176).

The question these tests ask is the one the old declaration could not
answer: not "did the caller say it was an agent" but "what did the caller
prove, and what may that identity do". Every server here binds 127.0.0.1
on port 0 inside a throwaway DROMOND_HOME.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dromond import auth, brief, db, profile_edit
from dromond import http as mhttp
from tests.test_http import KEY, ServerCase


class TokenSecretTests(unittest.TestCase):
    """A token is a credential, so it lives in exactly one place."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {
            "DROMOND_HOME": str(Path(self.tmp.name) / "home"),
            "DROMOND_CONFIG": str(Path(self.tmp.name) / "config.toml")})
        self.env.start()
        self.con = db.connect()

    def tearDown(self) -> None:
        self.con.close()
        self.env.stop()
        self.tmp.cleanup()

    def make_run(self, status="running") -> int:
        run_id = int(self.con.execute(
            "INSERT INTO runs(profile, backend, requested_by, workdir, status, "
            "started_at) VALUES('codex','codex','human',?,?,?)",
            (self.tmp.name, status, db.now())).lastrowid)
        self.con.commit()
        return run_id

    def test_only_the_hash_is_stored(self) -> None:
        run_id = self.make_run()
        token = auth.mint(self.con, run_id)
        stored = self.con.execute("SELECT run_token_hash FROM runs WHERE id=?",
                                  (run_id,)).fetchone()["run_token_hash"]
        self.assertEqual(stored, auth.hashed(token))
        self.assertNotIn(token, stored)
        self.assertGreater(len(token), 30)

    def test_the_token_is_in_no_file_anyone_reads(self) -> None:
        """The database bytes, a rendered brief, and the snapshot."""
        run_id = self.make_run()
        token = auth.mint(self.con, run_id)
        from dromond import paths
        text = brief.compose(
            run_id=run_id, slug="calm_otter",
            profile={"name": "codex", "backend": "codex",
                     "spawn_profiles": ["cheap"]},
            mission="do the thing", requester="human",
            root=Path(self.tmp.name), workdir=self.tmp.name,
            extra_context="context", work_snapshot="snapshot")
        brief_path = paths.briefs_dir() / f"run-{run_id}.md"
        brief_path.parent.mkdir(parents=True, exist_ok=True)
        brief_path.write_text(text)
        self.con.commit()
        raw = token.encode()
        files = sorted(Path(paths.db_path()).parent.glob("dromond.db*"))
        self.assertTrue(files, "no database file to grep")
        for path in files:
            self.assertNotIn(raw, path.read_bytes(), f"{path.name} carries it")
        self.assertNotIn(token, brief_path.read_text())
        self.assertNotIn(token, json.dumps(mhttp.snapshot(self.con)))

    def test_a_terminal_run_revokes_its_own_token(self) -> None:
        run_id = self.make_run()
        token = auth.mint(self.con, run_id)
        self.assertEqual(auth.identify(self.con, token, None),
                         auth.Identity(auth.RUN, run_id))
        self.con.execute("UPDATE runs SET status='done' WHERE id=?", (run_id,))
        self.con.commit()
        self.assertIsNone(auth.identify(self.con, token, None))
        self.assertIsNone(self.con.execute(
            "SELECT run_token_hash FROM runs WHERE id=?",
            (run_id,)).fetchone()["run_token_hash"])

    def test_every_terminal_status_revokes(self) -> None:
        for status in db.RUN_TERMINAL:
            run_id = self.make_run()
            token = auth.mint(self.con, run_id)
            self.con.execute("UPDATE runs SET status=? WHERE id=?",
                             (status, run_id))
            self.con.commit()
            self.assertIsNone(auth.identify(self.con, token, None), status)

    def test_the_cli_reads_authority_from_the_token(self) -> None:
        from dromond import cli
        run_id = self.make_run()
        token = auth.mint(self.con, run_id)
        with mock.patch.dict(os.environ, {auth.TOKEN_ENV: token}, clear=False):
            os.environ.pop("DROMOND_RUN_ID", None)
            self.assertEqual(cli._authority(), "agent")
            self.con.execute("UPDATE runs SET status='done' WHERE id=?", (run_id,))
            self.con.commit()
            self.assertEqual(cli._authority(), "human")


class RouteTableTests(unittest.TestCase):
    """The table is the audit surface, so it has to match the normalizer."""

    SAMPLES = {
        "GET /": "/",
        "GET /api/snapshot": "/api/snapshot",
        "GET /api/profiles/options": "/api/profiles/options",
        "GET /api/*/stream": "/api/log/stream",
        "GET /api/runs/{run}/stream": "/api/runs/4/stream",
        "GET /api/runs/{run}/brief": "/api/runs/4/brief",
        "GET /api/runs/{run}/diff": "/api/runs/4/diff",
        "POST /api/runs/{run}/stop": "/api/runs/4/stop",
        "POST /api/runs/{run}/tell": "/api/runs/4/tell",
        "POST /api/runs/{run}/check": "/api/runs/4/check",
        "POST /api/sweep": "/api/sweep",
        "POST /api/restart": "/api/restart",
        "POST /api/dispatch/pause": "/api/dispatch/pause",
        "POST /api/dispatch/resume": "/api/dispatch/resume",
        "POST /api/profiles/{name}": "/api/profiles/thinker",
    }

    def test_every_entry_is_a_path_the_normalizer_produces(self) -> None:
        self.assertEqual(set(self.SAMPLES), set(auth.ROUTES))
        for key, path in self.SAMPLES.items():
            method = key.split(" ", 1)[0]
            self.assertEqual(auth.route_key(method, path)[0], key)

    def test_an_unlisted_route_is_the_humans(self) -> None:
        run = auth.Identity(auth.RUN, 1)
        self.assertIsNone(auth.permit(auth.Identity(auth.HUMAN), "POST /api/new"))
        self.assertIn("human", auth.permit(run, "POST /api/new"))

    def test_head_is_gated_as_the_get_it_is(self) -> None:
        self.assertEqual(auth.route_key("HEAD", "/api/snapshot")[0],
                         "GET /api/snapshot")

    def test_self_means_this_run_and_no_other(self) -> None:
        run = auth.Identity(auth.RUN, 7)
        key = "POST /api/runs/{run}/stop"
        self.assertIsNone(auth.permit(run, key, 7))
        self.assertIn("not on run 8", auth.permit(run, key, 8))


class RunTokenRouteTests(ServerCase):
    """The same server the human uses, called with a run's own credential."""

    def setUp(self) -> None:
        super().setUp()
        self.run_id = self.make_run()
        self.sibling = self.make_run()
        self.token = auth.mint(self.con, self.run_id)

    def test_a_run_may_read(self) -> None:
        status, snap = self.json_request(path="/api/snapshot", key=self.token)
        self.assertEqual(status, 200)
        self.assertIn("runs", snap)

    def test_a_run_may_not_sweep_pause_or_resume(self) -> None:
        for path in ("/api/sweep", "/api/dispatch/pause", "/api/dispatch/resume"):
            status, text = self.request("POST", path, key=self.token)
            self.assertEqual(status, 403, path)
            self.assertIn("human", text)
        self.assertFalse(self.wake.is_set())

    def test_a_run_may_stop_itself(self) -> None:
        status, result = self.json_request(
            method="POST", path=f"/api/runs/{self.run_id}/stop", key=self.token)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["status"], "killed")

    def test_a_run_may_not_touch_a_sibling(self) -> None:
        for action, body in (("stop", None), ("tell", {"text": "hi"}),
                             ("check", None)):
            status, text = self.request(
                "POST", f"/api/runs/{self.sibling}/{action}", key=self.token,
                body=body)
            self.assertEqual(status, 403, action)
            self.assertIn(f"not on run {self.sibling}", text)
        row = self.con.execute("SELECT status FROM runs WHERE id=?",
                               (self.sibling,)).fetchone()
        self.assertEqual(row["status"], "running")

    def test_a_run_may_watch_its_own_trace_but_never_a_siblings(self) -> None:
        """W-0178: the run trace is a read of ONE run, so it is scoped like
        stop/tell/check rather than like the service-wide daemon log."""
        status, ctype, body = self.sse(f"/api/runs/{self.run_id}/stream",
                                       key=self.token, until="retry:")
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", ctype)
        self.assertIn("retry:", body)
        status, _, text = self.sse(f"/api/runs/{self.sibling}/stream",
                                   key=self.token)
        self.assertEqual(status, 403)
        self.assertIn(f"not on run {self.sibling}", text)
        self.assertNotIn(self.token, text)

    def test_a_run_may_read_its_own_brief_but_never_a_siblings(self) -> None:
        """W-0183: a brief is one run's mission, scoped like its trace."""
        path = self.tmp_path / "brief.md"
        path.write_text("## Mission\n\nmine alone\n")
        for run_id in (self.run_id, self.sibling):
            self.con.execute("UPDATE runs SET brief_path=? WHERE id=?",
                             (str(path), run_id))
        self.con.commit()
        status, payload = self.json_request(
            path=f"/api/runs/{self.run_id}/brief", key=self.token)
        self.assertEqual(status, 200)
        self.assertIn("mine alone", payload["text"])
        status, text = self.request(path=f"/api/runs/{self.sibling}/brief",
                                    key=self.token)
        self.assertEqual(status, 403)
        self.assertIn(f"not on run {self.sibling}", text)
        self.assertNotIn("mine alone", text)

    def test_the_daemon_log_stays_a_read_any_run_may_make(self) -> None:
        status, ctype, _ = self.sse("/api/log/stream", key=self.token,
                                    until="retry:")
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", ctype)

    def test_stopping_itself_revokes_the_token_it_used(self) -> None:
        self.json_request(method="POST", path=f"/api/runs/{self.run_id}/stop",
                          key=self.token)
        status, _ = self.request(path="/api/snapshot", key=self.token)
        self.assertEqual(status, 401)

    def test_a_token_is_never_taken_from_a_cookie_or_a_query(self) -> None:
        self.assertEqual(self.request(path="/api/snapshot", key=None,
                                      cookie=self.token)[0], 401)
        self.assertEqual(self.request(path=f"/?key={self.token}", key=None)[0], 401)

    def test_the_denial_never_echoes_the_credential(self) -> None:
        status, text = self.request("POST", "/api/sweep", key=self.token)
        self.assertEqual(status, 403)
        self.assertNotIn(self.token, text)
        self.assertNotIn(KEY, text)

    def test_the_human_keeps_the_whole_surface(self) -> None:
        self.assertEqual(self.request("POST", "/api/sweep")[0], 200)
        self.assertEqual(self.request("POST", "/api/dispatch/pause")[0], 200)
        self.assertEqual(self.request(
            "POST", f"/api/runs/{self.sibling}/stop")[0], 200)


class EffortDirectionTests(unittest.TestCase):
    """An agent may spend less on its own say-so, never more (W-0176)."""

    CONFIG = ('[profiles.thinker]\nbackend = "codex"\nmodel = "gpt-5.6-sol"\n'
              'effort = "high"\n')

    class FakeWork:
        def __init__(self):
            self.filed = []

        def create_decision(self, **kw):
            self.filed.append(kw)
            return {"id": "W-9001"}

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.toml"
        self.path.write_text(self.CONFIG)
        self.env = mock.patch.dict(os.environ, {
            "DROMOND_CONFIG": str(self.path),
            "DROMOND_HOME": str(Path(self.tmp.name) / "home")})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def save(self, effort, authority="agent"):
        work = self.FakeWork()
        result = profile_edit.save("thinker", {"effort": effort},
                                   authority=authority, work=work,
                                   options=None)
        return result, work

    def test_lowering_is_the_agents_to_make(self) -> None:
        result, work = self.save("low")
        self.assertTrue(result["applied"], result)
        self.assertIn('effort = "low"', self.path.read_text())
        self.assertEqual(work.filed, [])

    def test_raising_files_a_decision_and_writes_nothing(self) -> None:
        result, work = self.save("ultra")
        self.assertFalse(result["applied"])
        self.assertEqual(result["needs"], ["effort"])
        self.assertEqual(result["decision"], "W-9001")
        self.assertIn('effort = "high"', self.path.read_text())
        self.assertIn("raising a reasoning effort", work.filed[0]["detail"])

    def test_an_effort_that_cannot_be_ranked_goes_to_the_human(self) -> None:
        result, _ = self.save("hyper")
        self.assertFalse(result["applied"])

    def test_the_human_may_raise_it(self) -> None:
        result, work = self.save("ultra", authority="human")
        self.assertTrue(result["applied"], result)
        self.assertEqual(work.filed, [])

    def test_the_ranking_is_ordered_and_conservative(self) -> None:
        self.assertFalse(profile_edit.raises_effort("high", "low"))
        self.assertFalse(profile_edit.raises_effort("ultra", "max"))
        self.assertFalse(profile_edit.raises_effort("high", None))
        self.assertTrue(profile_edit.raises_effort("max", "ultra"))
        self.assertTrue(profile_edit.raises_effort(None, "low"))


if __name__ == "__main__":
    unittest.main()
