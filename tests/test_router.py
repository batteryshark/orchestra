"""The staffing turn (W-0183), unit and end-to-end through the sweeper.

NO TEST HERE DISPATCHES A REAL MODEL. Every path either passes an explicit
``turn`` stub or patches ``dromond.observer.model_turn``, which is the one
function that would start a backend process. ``DROMOND_HOME``/``DROMOND_CONFIG``
are sandboxed by the sweeper fixture, so the real config and database are never
touched.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dromond import config, db, router, sweeper
from tests.test_sweeper import SweeperFixture

# Three profiles across the three tiers, so "picked a different one" is a real
# choice and not the only one available.
ROUTED_CONFIG = """\
[settings]
timeout = 60

[profiles.stub]
backend = "opencode"
tier = 2
priority = 50
role = "the default worker"

[profiles.cheap]
backend = "opencode"
tier = 1
priority = 10

[profiles.big]
backend = "opencode"
model = "anthropic/opus"
tier = 3
priority = 20
role = "the hardest thinking"
note = "plenty of headroom this week"

[work]
enabled = true
agent_identity = "dromond"
profile = "stub"
router = "cheap"
poll_interval = 7
"""


def reply(name, reason="because"):
    """A well-formed router reply, wrapped in the prose models add anyway."""
    return ("Thinking about it...\n"
            + json.dumps({"profile": name, "reason": reason})
            + "\nHope that helps.")


class RouterUnitTestCase(unittest.TestCase):
    """``choose`` on its own: the fallback ladder, rung by rung."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / "global.toml"
        path.write_text(ROUTED_CONFIG)
        self.env = mock.patch.dict(os.environ, {
            "DROMOND_CONFIG": str(path),
            "DROMOND_HOME": str(Path(self.tmp.name) / "home")})
        self.env.start()
        self.con = db.connect()
        self.cfg = config.load()
        self.calls: list[tuple[dict, str]] = []

    def tearDown(self) -> None:
        self.con.close()
        self.env.stop()
        self.tmp.cleanup()

    def turn(self, text):
        def take(profile, prompt, **kw):
            self.calls.append((profile, prompt))
            return text
        return take

    def choose(self, cfg=None, snapshot="W-1 · Rewrite the scheduler", turn=None):
        cfg = self.cfg if cfg is None else cfg
        name = cfg["work"]["profile"]
        return router.choose(self.con, cfg, snapshot, name,
                             config.staff_profile(cfg, name), turn=turn)

    # --- the packet ---------------------------------------------------------

    def test_packet_carries_the_item_and_every_enabled_profile(self) -> None:
        self.choose(turn=self.turn(reply("big")))
        (_profile, prompt), = self.calls
        self.assertIn("Rewrite the scheduler", prompt)
        for name in ("stub", "cheap", "big"):
            self.assertIn(f"- {name}:", prompt)
        self.assertIn("tier 3 (heavy)", prompt)
        self.assertIn("priority 10", prompt)
        self.assertIn("role: the hardest thinking", prompt)
        self.assertIn("note: plenty of headroom this week", prompt)
        self.assertIn("runway: no reading", prompt)

    def test_packet_carries_measured_runway_when_a_poll_exists(self) -> None:
        self.con.execute(
            "INSERT INTO runway_polls(provider, remaining, unit, resets_at, "
            "as_of, polled_at) VALUES('anthropic', 12, 'percent', NULL, NULL, ?)",
            (db.now(),))
        self.con.commit()
        self.choose(turn=self.turn(reply("big")))
        self.assertIn("runway: 12% left", self.calls[0][1])

    def test_the_router_turn_runs_on_the_configured_cheap_profile(self) -> None:
        self.choose(turn=self.turn(reply("big")))
        self.assertEqual(self.calls[0][0]["name"], "cheap")

    # --- the answer ---------------------------------------------------------

    def test_routes_to_a_different_profile_than_the_work_profile(self) -> None:
        name, profile, reason = self.choose(
            turn=self.turn(reply("big", "the scheduler rewrite is cross-cutting")))
        self.assertEqual(name, "big")
        self.assertEqual(profile["model"], "anthropic/opus")
        self.assertIn("staffed big over stub", reason)
        self.assertIn("cross-cutting", reason)

    def test_keeping_the_default_still_records_its_reason(self) -> None:
        name, _p, reason = self.choose(turn=self.turn(reply("stub", "routine")))
        self.assertEqual(name, "stub")
        self.assertEqual(reason, "kept stub: routine")

    def test_a_name_outside_the_enabled_set_is_refused(self) -> None:
        cfg = dict(self.cfg, enabled_profiles=["stub", "cheap"])
        name, profile, reason = self.choose(
            cfg=cfg, turn=self.turn(reply("big", "looks hard")))
        self.assertEqual(name, "stub")            # never honoured
        self.assertIsNone(profile.get("model"))
        self.assertIn("has not enabled", reason)
        self.assertIn("'big'", reason)
        self.assertIn("staffed stub", reason)
        # And a disabled profile is not even offered in the packet.
        self.assertNotIn("- big:", self.calls[0][1])

    # --- the fallback ladder ------------------------------------------------

    def test_no_router_configured_takes_no_turn_and_records_nothing(self) -> None:
        cfg = dict(self.cfg, work=dict(self.cfg["work"], router=""))
        name, _p, reason = self.choose(cfg=cfg, turn=self.turn(reply("big")))
        self.assertEqual(name, "stub")
        self.assertIsNone(reason)
        self.assertEqual(self.calls, [])

    def test_one_enabled_profile_skips_the_call(self) -> None:
        cfg = dict(self.cfg, enabled_profiles=["stub"])
        name, _p, reason = self.choose(cfg=cfg, turn=self.turn(reply("big")))
        self.assertEqual(name, "stub")
        self.assertIn("nothing to decide", reason)
        self.assertEqual(self.calls, [])

    def test_a_disabled_router_profile_falls_back(self) -> None:
        cfg = dict(self.cfg, enabled_profiles=["stub", "big"])
        name, _p, reason = self.choose(cfg=cfg, turn=self.turn(reply("big")))
        self.assertEqual(name, "stub")
        self.assertIn("router profile 'cheap' is not staffable", reason)
        self.assertEqual(self.calls, [])

    def test_an_unknown_router_profile_falls_back(self) -> None:
        cfg = dict(self.cfg, work=dict(self.cfg["work"], router="ghost"))
        name, _p, reason = self.choose(cfg=cfg, turn=self.turn(reply("big")))
        self.assertEqual(name, "stub")
        self.assertIn("not staffable", reason)
        self.assertIn("unknown profile 'ghost'", reason)

    def test_a_dead_process_falls_back(self) -> None:
        def dies(profile, prompt, **kw):
            raise FileNotFoundError("opencode is not installed")
        name, _p, reason = self.choose(turn=dies)
        self.assertEqual(name, "stub")
        self.assertIn("could not run", reason)
        self.assertIn("opencode is not installed", reason)

    def test_a_systemexit_from_the_turn_falls_back(self) -> None:
        def exits(profile, prompt, **kw):
            raise SystemExit("dromond: that backend has no such model")
        name, _p, reason = self.choose(turn=exits)
        self.assertEqual(name, "stub")
        self.assertIn("could not run", reason)

    def test_an_unparsable_reply_falls_back(self) -> None:
        name, _p, reason = self.choose(turn=self.turn("I would use the big one."))
        self.assertEqual(name, "stub")
        self.assertIn("named no profile", reason)
        self.assertIn("staffed stub", reason)

    def test_an_empty_reply_falls_back(self) -> None:
        name, _p, reason = self.choose(turn=self.turn(""))
        self.assertEqual(name, "stub")
        self.assertIn("named no profile", reason)

    def test_a_failure_while_building_the_packet_falls_back(self) -> None:
        with mock.patch("dromond.router.runway.latest_polls",
                        side_effect=RuntimeError("runway database is unavailable")):
            name, _p, reason = self.choose(turn=self.turn(reply("big")))
        self.assertEqual(name, "stub")
        self.assertIn("runway database is unavailable", reason)
        self.assertIn("staffed stub", reason)
        self.assertEqual(self.calls, [])

    def test_the_last_object_wins_and_case_does_not_matter(self) -> None:
        name, _p, _r = self.choose(turn=self.turn(
            '{"profile": "cheap"} on reflection {"profile": "BIG", "reason": "hard"}'))
        self.assertEqual(name, "big")


class RoutedSweepTestCase(SweeperFixture, unittest.TestCase):
    """End to end: a claim routes, and the board is told why."""

    def setUp(self) -> None:
        super().setUp()
        url = self.cfg["work"]["api_url"]
        self.global_config.write_text(ROUTED_CONFIG + f'api_url = "{url}"\n')
        self.cfg = config.load()
        self.client = sweeper.client_from_cfg(self.cfg)
        # The one function that would start a real backend process. Patched
        # for every test in this case; a test that expects no turn asserts on
        # the call list rather than relying on it being unpatched.
        self.turns: list[str] = []
        self.answer = reply("big", "an ambiguous rewrite needs the heavy model")
        patcher = mock.patch("dromond.observer.model_turn",
                             side_effect=self._turn)
        self.model_turn = patcher.start()
        self.addCleanup(patcher.stop)

    def _turn(self, profile, prompt, **kw):
        self.turns.append(prompt)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def test_a_routed_dispatch_lands_on_the_row_and_the_thread(self) -> None:
        self.work.add_task("W-0001", "Rewrite the scheduler", delegated=True,
                           goal="Nobody knows what correct looks like yet.")
        self.assertEqual([a["action"] for a in self.sweep()], ["dispatch"])
        run = self.db_run()
        self.assertEqual(run["profile"], "big")
        self.assertEqual(run["model"], "anthropic/opus")
        self.assertIn("staffed big over stub", run["routed_reason"])
        log = "\n".join(e["message"] for e in self.work.tasks["W-0001"]["log"])
        self.assertIn("staffing: staffed big over stub", log)
        self.assertIn("an ambiguous rewrite needs the heavy model", log)
        # The turn saw the item the worker will see.
        self.assertIn("Nobody knows what correct looks like yet.", self.turns[0])

    def test_a_trivial_item_can_keep_the_work_profile(self) -> None:
        self.answer = reply("cheap", "a one-line typo fix")
        self.work.add_task("W-0002", "Fix a typo", delegated=True)
        self.sweep()
        run = self.db_run()
        self.assertEqual(run["profile"], "cheap")
        self.assertIn("staffed cheap over stub", run["routed_reason"])

    def test_a_failed_turn_still_dispatches_on_the_work_profile(self) -> None:
        self.answer = RuntimeError("the staffing turn timed out after 90s")
        self.work.add_task("W-0003", "Rewrite the scheduler", delegated=True)
        self.assertEqual([a["action"] for a in self.sweep()], ["dispatch"])
        run = self.db_run()
        self.assertEqual(run["profile"], "stub")
        self.assertIn("could not run", run["routed_reason"])
        log = "\n".join(e["message"] for e in self.work.tasks["W-0003"]["log"])
        self.assertIn("staffing: the staffing turn could not run", log)

    def test_routing_off_takes_no_turn_and_says_nothing(self) -> None:
        url = self.cfg["work"]["api_url"]
        self.global_config.write_text(
            ROUTED_CONFIG.replace('router = "cheap"\n', "") + f'api_url = "{url}"\n')
        self.cfg = config.load()
        self.work.add_task("W-0004", "Fix a typo", delegated=True)
        self.sweep()
        run = self.db_run()
        self.assertEqual(run["profile"], "stub")
        self.assertIsNone(run["routed_reason"])
        self.assertEqual(self.turns, [])
        log = "\n".join(e["message"] for e in self.work.tasks["W-0004"]["log"])
        self.assertNotIn("staffing:", log)

    def test_a_continuation_keeps_its_lineage_profile_and_takes_no_turn(self) -> None:
        self.work.add_task("W-0005", "Rewrite the scheduler", delegated=True)
        self.sweep()
        first = self.db_run()
        self.assertEqual(first["profile"], "big")
        self.finish_run(first["id"], status="done", session_ref="session-1")
        self.sweep()                       # reports, moves to review
        self.work.human_move("W-0005", "ready")
        self.work.human_log("W-0005", "one more pass please")
        self.turns.clear()
        self.assertEqual([a["action"] for a in self.sweep()], ["dispatch"])
        second = self.db_run()
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(second["profile"], "big")
        self.assertEqual(self.turns, [])   # a resume is not a staffing moment
        self.assertIsNone(second["routed_reason"])


if __name__ == "__main__":
    unittest.main()
