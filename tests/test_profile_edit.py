"""Managed profile edits (DESIGN §5, W-0173).

Every test writes to a throwaway ``DROMOND_CONFIG`` inside a temp directory.
Nothing here reads the developer's real ``~/.config/dromond/config.toml`` —
it holds live credentials and ten real profiles — and no test shells out to
a harness CLI: discovery is fed from the same fixture text ``test_profiles``
parses.
"""
import json
import os
import stat
import tempfile
import time
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest import mock

from dromond import auth, config, db, profile_edit, profiles
from dromond import http as mhttp

# The same shapes the harnesses really print (see tests/test_profiles.py).
OPENCODE = "opencode/big-pickle\ndeepseek/deepseek-v4-pro\n"
CODEX = json.dumps({"models": [
    {"slug": "gpt-5.6-sol", "default_reasoning_level": "medium",
     "supported_reasoning_levels": [{"effort": e} for e in
                                    ("low", "medium", "high", "xhigh", "max", "ultra")]},
    {"slug": "gpt-5.6-luna",
     "supported_reasoning_levels": [{"effort": e} for e in
                                    ("low", "medium", "high", "xhigh", "max")]},
]})
REASONIX = """\
[[providers]]
name = "ds4"
models = ["deepseek-v4-flash"]
supported_efforts = ["high", "max"]
"""

CONFIG = """\
# Dromond profiles + settings. Hand-editable, and full of the comments that
# make it readable.

[settings]
timeout = 36000        # hard cap for a runaway worker

# --- profiles -------------------------------------------------------------
# Each entry is a reusable launch template, never a worker identity.

[profiles.thinker]
backend = "codex"      # the expensive one
model = "gpt-5.6-sol"
effort = "high"
# delegation allowlist below; keep it short
spawn_profiles = ["cheap"]

[profiles.cheap]
backend = "opencode"
model = "deepseek/deepseek-v4-pro"
"""


def fixture_options() -> dict:
    """Picker options from fixture text: no CLI is ever executed."""
    def runner(cmd):
        return ({"opencode": OPENCODE, "codex": CODEX}[cmd[0]], None)
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
        f.write(REASONIX)
    try:
        found = profiles.discover(runner=runner, reasonix_config=Path(f.name))
    finally:
        os.unlink(f.name)
    return profile_edit.picker_options(found)


class EditCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.toml"
        self.path.write_text(CONFIG)
        os.chmod(self.path, 0o600)
        self.env = mock.patch.dict(os.environ, {
            "DROMOND_CONFIG": str(self.path),
            "DROMOND_HOME": str(Path(self.tmp.name) / "home")})
        self.env.start()
        self.options = fixture_options()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def save(self, name, changes, **kw):
        kw.setdefault("options", self.options)
        return profile_edit.save(name, changes, **kw)

    def table(self, name) -> dict:
        import tomllib
        return tomllib.loads(self.path.read_text())["profiles"][name]


class DiscoveryPickerTests(EditCase):
    def test_real_lists_reach_the_picker(self) -> None:
        codex = {m["id"]: m["efforts"] for m in self.options["codex"]["models"]}
        self.assertIn("ultra", codex["gpt-5.6-sol"])
        self.assertNotIn("ultra", codex["gpt-5.6-luna"])
        self.assertEqual(codex["gpt-5.6-luna"][-1], "max")
        self.assertIn("deepseek/deepseek-v4-pro",
                      [m["id"] for m in self.options["opencode"]["models"]])
        self.assertEqual([m["id"] for m in self.options["reasonix"]["models"]],
                         ["ds4/deepseek-v4-flash"])

    def test_opencode_has_no_effort_control_at_all(self) -> None:
        """OpenCode takes no --effort flag, so the control disables itself
        rather than accepting a value the launch would silently drop."""
        self.assertIs(self.options["opencode"]["supports_effort"], False)
        self.assertTrue(self.options["codex"]["supports_effort"])
        result = self.save("cheap", {"effort": "high"})
        self.assertIn("takes no reasoning effort", result["error"])
        self.assertNotIn("effort", self.table("cheap"))

    def test_an_effort_the_model_does_not_declare_is_refused(self) -> None:
        result = self.save("thinker", {"model": "gpt-5.6-luna", "effort": "ultra"})
        self.assertIn("not 'ultra'", result["error"])
        self.assertEqual(self.table("thinker")["model"], "gpt-5.6-sol")

    def test_claude_is_the_one_typed_backend_and_says_so(self) -> None:
        self.assertTrue(self.options["claude"]["free_model"])
        self.assertTrue(self.options["claude"]["supports_effort"])


class CommentPreservationTests(EditCase):
    def test_an_edit_keeps_every_comment_and_touches_one_line(self) -> None:
        before = self.path.read_text()
        result = self.save("thinker", {"effort": "max"})
        self.assertTrue(result["applied"], result)
        after = self.path.read_text()
        for comment in ("# Dromond profiles + settings.",
                        "# --- profiles ----",
                        "# the expensive one",
                        "# delegation allowlist below; keep it short",
                        "timeout = 36000        # hard cap for a runaway worker"):
            self.assertIn(comment, after)
        changed = [(a, b) for a, b in zip(before.splitlines(), after.splitlines())
                   if a != b]
        self.assertEqual(changed, [('effort = "high"', 'effort = "max"')])

    def test_a_trailing_comment_on_the_edited_line_survives(self) -> None:
        self.save("thinker", {"backend": "codex", "model": "gpt-5.6-luna"})
        self.assertIn('backend = "codex"      # the expensive one',
                      self.path.read_text())

    def test_a_new_profile_is_appended_and_nothing_else_moves(self) -> None:
        before = self.path.read_text()
        result = self.save("scout", {"backend": "codex", "model": "gpt-5.6-luna",
                                     "effort": "low"})
        self.assertTrue(result["applied"], result)
        after = self.path.read_text()
        self.assertTrue(after.startswith(before))
        self.assertEqual(self.table("scout"),
                         {"backend": "codex", "model": "gpt-5.6-luna",
                          "effort": "low"})

    def test_the_very_first_profile_lands_in_a_fresh_config(self) -> None:
        """A fresh install has no profiles at all (DESIGN §5), so the first
        add creates the [profiles.NAME] table and keeps the shipped comments."""
        self.path.unlink()
        result = self.save("first", {"backend": "codex", "model": "gpt-5.6-sol"})
        self.assertTrue(result["applied"], result)
        text = self.path.read_text()
        self.assertIn("# --- profiles ---", text)   # from the default config
        self.assertIn("[profiles.first]", text)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_removing_a_profile_removes_only_its_lines(self) -> None:
        self.save("thinker", {"spawn_profiles": []})
        result = self.save("cheap", {}, delete=True)
        self.assertTrue(result["applied"], result)
        after = self.path.read_text()
        self.assertNotIn("[profiles.cheap]", after)
        self.assertIn("# the expensive one", after)
        self.assertIn("# --- profiles ----", after)

    def test_a_profile_someone_still_delegates_to_is_not_silently_removed(self) -> None:
        result = self.save("cheap", {}, delete=True)
        self.assertIn("spawn_profiles of thinker", result["error"])
        self.assertIn("[profiles.cheap]", self.path.read_text())

    def test_the_write_is_atomic_and_stays_0600(self) -> None:
        self.save("thinker", {"note": "10% weekly left, resets Sunday 18:00"})
        mode = stat.S_IMODE(self.path.stat().st_mode)
        self.assertEqual(mode, 0o600)
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()),
                         ["config.toml"])  # no temp file left behind
        self.assertTrue(self.table("thinker")["note_at"].endswith("Z"))

    def test_a_multiline_array_is_refused_not_mangled(self) -> None:
        """ponytail ceiling: the surgery is line-based, so a key spread over
        several lines is rejected with the reason rather than half-rewritten."""
        self.path.write_text(
            '[profiles.cheap]\nbackend = "codex"\nmodel = "gpt-5.6-sol"\n\n'
            '[profiles.wide]\nbackend = "codex"\nmodel = "gpt-5.6-sol"\n'
            'spawn_profiles = [\n  "cheap",\n]\n')
        result = self.save("wide", {"spawn_profiles": []})
        self.assertIn("would not parse", result["error"])
        self.assertIn('"cheap",', self.path.read_text())


class SpawnChecklistTests(EditCase):
    def test_an_allowlist_naming_a_profile_that_does_not_exist_is_rejected(self) -> None:
        result = self.save("cheap", {"spawn_profiles": ["ghost"]})
        self.assertIn("'ghost'", result["error"])
        self.assertIn("not a configured profile", result["error"])
        self.assertNotIn("spawn_profiles", self.table("cheap"))

    def test_an_allowlist_of_existing_names_is_written(self) -> None:
        result = self.save("cheap", {"spawn_profiles": ["thinker"]})
        self.assertTrue(result["applied"], result)
        self.assertEqual(self.table("cheap")["spawn_profiles"], ["thinker"])


class RoutingMetadataTests(EditCase):
    """tier + priority (W-0181): what a planner routes on."""

    def test_a_tier_outside_one_to_three_is_refused(self) -> None:
        for bad in (0, 4, "frontierish"):
            result = self.save("cheap", {"tier": bad})
            self.assertIn("tier must be 1", result["error"], bad)
        self.assertNotIn("tier", self.table("cheap"))

    def test_a_named_tier_is_written_as_its_number(self) -> None:
        """Ten real profiles say tier = "cheap"; a save migrates the value
        instead of rejecting the config the human already has."""
        self.assertTrue(self.save("cheap", {"tier": "workhorse"})["applied"])
        self.assertEqual(self.table("cheap")["tier"], 1)
        self.assertTrue(self.save("thinker", {"tier": "mid"})["applied"])
        self.assertEqual(self.table("thinker")["tier"], 2)

    def test_a_priority_outside_zero_to_ninetynine_is_refused(self) -> None:
        self.assertIn("priority must be 0-99",
                      self.save("cheap", {"priority": 100})["error"])
        self.assertIn("must not be negative",
                      self.save("cheap", {"priority": -1})["error"])
        self.assertNotIn("priority", self.table("cheap"))

    def test_priority_says_lower_is_more_preferred(self) -> None:
        self.assertIn("LOWER is more preferred",
                      self.save("cheap", {"priority": 100})["error"])

    def test_a_priority_in_range_is_written(self) -> None:
        self.assertTrue(self.save("cheap", {"priority": 10})["applied"])
        self.assertEqual(self.table("cheap")["priority"], 10)

    def test_an_edit_beside_a_legacy_named_tier_still_saves(self) -> None:
        """The merged profile carries the file's own `tier = "cheap"`, and
        validation must map it rather than refuse an unrelated note edit."""
        self.path.write_text(self.path.read_text() + '\ntier = "cheap"\n')
        self.assertTrue(self.save("cheap", {"note": "plenty"})["applied"])

    def test_max_steps_is_no_longer_a_profile_field(self) -> None:
        self.assertIn("not an editable profile key",
                      self.save("thinker", {"max_steps": 40})["error"])


class AuthorityTests(EditCase):
    class FakeWork:
        def __init__(self):
            self.filed = []

        def create_decision(self, **kw):
            self.filed.append(kw)
            return {"id": "W-9001"}

    def test_an_agent_may_retune_the_cheap_knobs(self) -> None:
        for changes in ({"note": "burn the weekly allowance"},
                        {"effort": "low"}):
            result = self.save("thinker", changes, authority="agent")
            self.assertTrue(result["applied"], result)
        table = self.table("thinker")
        self.assertEqual(table["effort"], "low")
        self.assertEqual(table["note"], "burn the weekly allowance")

    def test_an_agent_may_not_reroute_itself_with_tier_or_priority(self) -> None:
        """W-0181: tier and priority are what a planner routes on, so an
        agent promoting itself is exactly the self-grant principle 5 forbids."""
        work = self.FakeWork()
        for changes in ({"tier": 3}, {"priority": 0}):
            result = self.save("thinker", changes, authority="agent", work=work)
            self.assertFalse(result["applied"], result)
        self.assertEqual([r for r in self.table("thinker")
                          if r in ("tier", "priority")], [])
        self.assertEqual(len(work.filed), 2)

    def test_an_agent_adding_a_model_gets_a_decision_not_a_write(self) -> None:
        work = self.FakeWork()
        before = self.path.read_text()
        result = self.save("thinker", {"model": "gpt-5.6-luna"},
                           authority="agent", work=work)
        self.assertFalse(result["applied"])
        self.assertEqual(result["decision"], "W-9001")
        self.assertEqual(result["needs"], ["model"])
        self.assertEqual(self.path.read_text(), before)
        self.assertIn("thinker", work.filed[0]["title"])
        self.assertIn("commits spend", work.filed[0]["detail"])

    def test_an_agent_may_not_add_or_remove_a_profile(self) -> None:
        work = self.FakeWork()
        before = self.path.read_text()
        made = self.save("newone", {"backend": "codex", "model": "gpt-5.6-sol"},
                         authority="agent", work=work)
        gone = self.save("cheap", {}, delete=True, authority="agent", work=work)
        self.assertFalse(made["applied"])
        self.assertFalse(gone["applied"])
        self.assertEqual(self.path.read_text(), before)
        self.assertEqual(len(work.filed), 2)

    def test_a_human_change_needs_no_decision(self) -> None:
        work = self.FakeWork()
        result = self.save("thinker", {"model": "gpt-5.6-luna", "effort": "max"},
                           authority="human", work=work)
        self.assertTrue(result["applied"], result)
        self.assertEqual(work.filed, [])

    def test_an_unknown_key_and_a_bogus_name_are_refused(self) -> None:
        self.assertIn("not an editable profile key",
                      self.save("thinker", {"tokens": 5})["error"])
        self.assertIn("not a usable profile name",
                      self.save("../etc/passwd", {"backend": "codex"})["error"])

    def test_a_note_alone_never_conjures_a_profile(self) -> None:
        result = self.save("ghost", {"note": "n/a"})
        self.assertIn("needs a harness", result["error"])
        self.assertNotIn("[profiles.ghost]", self.path.read_text())


PROJECT_ID = "53efe3c3-6def-4797-8560-3dce073d7d63"


class EnabledSetWriteTests(EditCase):
    """W-0187: the enabled set is a key in the project's own table, written
    by the same targeted surgery every profile edit uses."""

    def project_table(self) -> dict:
        import tomllib
        return (tomllib.loads(self.path.read_text()).get("project")
                or {}).get(PROJECT_ID, {})

    def test_writing_the_set_keeps_every_comment(self) -> None:
        before = self.path.read_text()
        result = profile_edit.set_enabled(PROJECT_ID, ["cheap"])
        self.assertTrue(result["applied"], result)
        after = self.path.read_text()
        self.assertEqual(self.project_table()["enabled_profiles"], ["cheap"])
        for comment in ("# Dromond profiles + settings.",
                        "# --- profiles ----",
                        "# the expensive one",
                        "# delegation allowlist below; keep it short"):
            self.assertIn(comment, after)
        # only lines were ADDED; nothing that existed changed
        self.assertEqual([l for l in before.splitlines()
                          if l not in after.splitlines()], [])

    def test_the_set_is_replaced_in_place_on_a_second_write(self) -> None:
        profile_edit.set_enabled(PROJECT_ID, ["cheap"])
        first = self.path.read_text()
        profile_edit.set_enabled(PROJECT_ID, ["thinker", "cheap"])
        self.assertEqual(self.project_table()["enabled_profiles"],
                         ["thinker", "cheap"])
        # one table, not two: the header was reused
        self.assertEqual(self.path.read_text().count(f'[project."{PROJECT_ID}"]'), 1)
        self.assertEqual(first.count("enabled_profiles"), 1)

    def test_none_removes_the_key_rather_than_listing_everything(self) -> None:
        """"All enabled" is the ABSENCE of the key. Writing out every current
        name would silently disable the next profile someone adds."""
        profile_edit.set_enabled(PROJECT_ID, ["cheap"])
        result = profile_edit.set_enabled(PROJECT_ID, None)
        self.assertTrue(result["applied"], result)
        self.assertNotIn("enabled_profiles", self.path.read_text())
        self.assertIsNone(config.load(PROJECT_ID)["enabled_profiles"])

    def test_the_set_lands_beside_an_existing_settings_table(self) -> None:
        self.path.write_text(self.path.read_text()
                             + f'\n[project."{PROJECT_ID}".settings]\ntimeout = 99\n')
        self.assertTrue(profile_edit.set_enabled(PROJECT_ID, ["cheap"])["applied"])
        cfg = config.load(PROJECT_ID)
        self.assertEqual(cfg["settings"]["timeout"], 99)
        self.assertEqual(cfg["enabled_profiles"], ["cheap"])

    def test_a_name_that_is_not_a_profile_is_refused_before_any_write(self) -> None:
        before = self.path.read_text()
        result = profile_edit.set_enabled(PROJECT_ID, ["ghost"])
        self.assertFalse(result["applied"])
        self.assertIn("ghost", result["error"])
        self.assertEqual(self.path.read_text(), before)

    def test_the_write_stays_0600(self) -> None:
        profile_edit.set_enabled(PROJECT_ID, ["cheap"])
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), "0o600")


class HttpProfileRouteTests(unittest.TestCase):
    """The dashboard's half: pickers over HTTP, writes to the config file."""

    KEY = "test-secret-value"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.toml"
        self.path.write_text(CONFIG)
        self.env = mock.patch.dict(os.environ, {
            "DROMOND_CONFIG": str(self.path),
            "DROMOND_HOME": str(Path(self.tmp.name) / "home")})
        self.env.start()
        os.environ.pop(mhttp.KEY_ENV, None)
        # Warm the discovery cache from fixtures: no harness CLI is run, and
        # the routes see exactly what a real `codex debug models` would give.
        profile_edit._CACHE.clear()
        profile_edit._CACHE.update(at=time.time(), options=fixture_options())
        self.srv = mhttp.serve(addr="127.0.0.1", port=0,
                               cfg={"http": {"key": self.KEY}})
        self.assertIsNotNone(self.srv)

    def tearDown(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()
        profile_edit._CACHE.clear()
        self.env.stop()
        self.tmp.cleanup()

    def token(self, run_id: int) -> str:
        """A live run row and a freshly minted token for it."""
        con = db.connect()
        try:
            con.execute(
                "INSERT OR IGNORE INTO runs(id, profile, backend, requested_by, "
                "workdir, status, started_at) "
                "VALUES(?, 'codex', 'codex', 'human', ?, 'running', ?)",
                (run_id, str(self.path.parent), db.now()))
            con.commit()
            return auth.mint(con, run_id)
        finally:
            con.close()

    def call(self, method, path, body=None, run=None):
        """``run`` calls as that run: its own token, not the shared secret.

        W-0176: there is no header a caller can set to *say* it is an agent.
        The credential is the whole answer, so the agent path in these tests
        is a real minted token for a real live run.
        """
        headers = {mhttp.HEADER: self.KEY if run is None else self.token(run)}
        payload = json.dumps(body).encode() if body is not None else None
        if payload:
            headers["Content-Type"] = "application/json"
        conn = HTTPConnection("127.0.0.1", self.srv.server_port, timeout=10)
        try:
            conn.request(method, path, body=payload, headers=headers)
            res = conn.getresponse()
            text = res.read().decode()
            return res.status, (json.loads(text) if text.strip() else {})
        finally:
            conn.close()

    def test_a_profile_is_created_from_the_pickers_without_typing_a_model(self) -> None:
        """The DESIGN §5 acceptance: backend → model → effort, all picked."""
        status, options = self.call("GET", "/api/profiles/options")
        self.assertEqual(status, 200)
        backend = "codex"
        model = options[backend]["models"][0]          # picked, not typed
        effort = model["efforts"][-1]                  # from that model's list
        status, result = self.call("POST", "/api/profiles/scout", {
            "profile": {"backend": backend, "model": model["id"], "effort": effort}})
        self.assertEqual(status, 200, result)
        self.assertTrue(result["applied"], result)
        text = self.path.read_text()
        self.assertIn(f'model = "{model["id"]}"', text)
        self.assertIn("# the expensive one", text)  # comments intact
        status, snap = self.call("GET", "/api/snapshot")
        entry = [p for p in snap["profiles"] if p["name"] == "scout"][0]
        self.assertEqual(entry["effort"], effort)

    def test_the_effort_control_is_unavailable_for_opencode(self) -> None:
        _, options = self.call("GET", "/api/profiles/options")
        self.assertIs(options["opencode"]["supports_effort"], False)
        self.assertIn("no --effort", options["opencode"]["effort_note"])
        status, result = self.call("POST", "/api/profiles/cheap",
                                   {"profile": {"effort": "high"}})
        self.assertEqual(status, 400)
        self.assertIn("takes no reasoning effort", result["error"])

    def test_a_spawn_entry_naming_a_missing_profile_cannot_be_saved(self) -> None:
        status, result = self.call("POST", "/api/profiles/cheap",
                                   {"profile": {"spawn_profiles": ["ghost"]}})
        self.assertEqual(status, 400)
        self.assertIn("not a configured profile", result["error"])

    def test_the_snapshot_carries_routing_metadata_in_routing_order(self) -> None:
        """W-0181: a planner reads tier and priority off the same snapshot the
        dashboard draws, and the list arrives sorted — lowest priority first."""
        self.assertTrue(self.call("POST", "/api/profiles/thinker",
                                  {"profile": {"priority": 5, "tier": 3}})[1])
        self.assertTrue(self.call("POST", "/api/profiles/cheap",
                                  {"profile": {"priority": 80, "tier": "cheap"}})[1])
        _, snap = self.call("GET", "/api/snapshot")
        self.assertEqual([p["name"] for p in snap["profiles"]],
                         ["thinker", "cheap"])
        by_name = {p["name"]: p for p in snap["profiles"]}
        self.assertEqual(by_name["thinker"]["tier"], 3)
        self.assertEqual(by_name["thinker"]["tier_name"], "heavy")
        self.assertEqual(by_name["cheap"]["tier"], 1)          # named, stored 1
        self.assertEqual(by_name["cheap"]["tier_name"], "workhorse")
        self.assertNotIn("max_steps", by_name["cheap"])

    def test_a_profile_with_no_priority_reports_the_default(self) -> None:
        _, snap = self.call("GET", "/api/snapshot")
        self.assertEqual({p["priority"] for p in snap["profiles"]}, {50})
        self.assertEqual([p["tier"] for p in snap["profiles"]], [None, None])

    def test_the_note_round_trips_and_carries_its_age(self) -> None:
        status, result = self.call("POST", "/api/profiles/cheap",
                                   {"profile": {"note": "resets Sunday 18:00"}})
        self.assertEqual(status, 200, result)
        _, snap = self.call("GET", "/api/snapshot")
        entry = [p for p in snap["profiles"] if p["name"] == "cheap"][0]
        self.assertEqual(entry["note"], "resets Sunday 18:00")
        self.assertEqual(entry["note_age"], "just now")

    def test_the_agent_path_files_a_decision_instead_of_writing(self) -> None:
        before = self.path.read_text()
        with mock.patch.object(profile_edit.work_client, "from_cfg",
                               return_value=AuthorityTests.FakeWork()):
            status, result = self.call("POST", "/api/profiles/thinker",
                                       {"profile": {"model": "gpt-5.6-luna"}}, run=7)
            self.assertEqual(status, 200, result)
            self.assertFalse(result["applied"])
            self.assertEqual(result["decision"], "W-9001")
            self.assertEqual(self.path.read_text(), before)
            # …while the cheap knobs still go straight through.
            status, result = self.call("POST", "/api/profiles/thinker",
                                       {"profile": {"note": "lean on it"}}, run=7)
        self.assertTrue(result["applied"], result)

    def test_removing_a_profile_over_http(self) -> None:
        self.call("POST", "/api/profiles/thinker", {"profile": {"spawn_profiles": []}})
        status, result = self.call("POST", "/api/profiles/cheap", {"delete": True})
        self.assertEqual(status, 200, result)
        self.assertNotIn("[profiles.cheap]", self.path.read_text())


class CliParityTests(EditCase):
    def cli(self, argv: list[str]) -> str:
        import contextlib
        import io

        from dromond import cli as mcli
        out = io.StringIO()
        with mock.patch.object(mcli.profile_edit, "discovery_options",
                               lambda force=False: self.options), \
             mock.patch.object(mcli.sys, "argv", ["dromond"] + argv), \
             contextlib.redirect_stdout(out):
            mcli.main()
        return out.getvalue()

    def test_set_edits_the_config_file(self) -> None:
        text = self.cli(["profiles", "set", "thinker", "--effort", "ultra",
                         "--note", "10% weekly left"])
        self.assertIn("effort", text)
        table = self.table("thinker")
        self.assertEqual(table["effort"], "ultra")
        self.assertEqual(table["note"], "10% weekly left")
        self.assertIn("# the expensive one", self.path.read_text())

    def test_set_refuses_an_effort_on_opencode(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            self.cli(["profiles", "set", "cheap", "--effort", "high"])
        self.assertIn("no --effort", str(ctx.exception))

    def test_set_refuses_a_model_no_harness_reports(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            self.cli(["profiles", "set", "thinker", "--model", "gpt-9-imaginary"])
        self.assertIn("discovery lists", str(ctx.exception))

    def test_a_bare_model_flag_offers_the_real_list(self) -> None:
        with mock.patch("builtins.input", side_effect=["2"]):
            self.cli(["profiles", "set", "thinker", "--model"])
        self.assertEqual(self.table("thinker")["model"], "gpt-5.6-luna")

    def test_rm_removes_it(self) -> None:
        self.cli(["profiles", "set", "thinker", "--spawn", ""])
        self.cli(["profiles", "rm", "cheap"])
        self.assertNotIn("[profiles.cheap]", self.path.read_text())

    def test_the_run_environment_is_the_agent_path(self) -> None:
        """A worker's shell carries DROMOND_RUN_ID, so the CLI enforces the
        same split the HTTP surface does."""
        work = AuthorityTests.FakeWork()
        with mock.patch.dict(os.environ, {"DROMOND_RUN_ID": "12"}), \
             mock.patch.object(profile_edit.work_client, "from_cfg",
                               return_value=work):
            text = self.cli(["profiles", "set", "thinker",
                             "--model", "gpt-5.6-luna"])
        self.assertIn("Work decision", text)
        self.assertEqual(self.table("thinker")["model"], "gpt-5.6-sol")
        self.assertEqual(len(work.filed), 1)

    def test_a_legacy_sidecar_note_stops_shadowing_the_file(self) -> None:
        config.profile_notes_path().parent.mkdir(parents=True, exist_ok=True)
        config.profile_notes_path().write_text(json.dumps(
            {"thinker": {"note": "stale", "note_at": "2026-01-01T00:00:00Z"}}))
        self.assertEqual(config.load()["profiles"]["thinker"]["note"], "stale")
        self.cli(["profiles", "note", "thinker", "fresh", "note"])
        self.assertEqual(config.load()["profiles"]["thinker"]["note"], "fresh note")


class SuiteIsolationTests(unittest.TestCase):
    """The suite is often run BY a supervised run, whose shell exports its own
    identity. cli._authority reads DROMOND_RUN_ID, so those four CliParityTests
    failures were decided by the shell the suite was launched from rather than
    by anything under test (I-0008, I-0009)."""

    def test_a_runs_identity_never_reaches_the_suite(self) -> None:
        for name in ("DROMOND_RUN_ID", "DROMOND_RUN_TOKEN", "DROMOND_ROOT"):
            self.assertIsNone(os.environ.get(name), name)

    def test_authority_is_human_inside_the_suite(self) -> None:
        from dromond import cli
        self.assertEqual("human", cli._authority())


if __name__ == "__main__":
    unittest.main()
