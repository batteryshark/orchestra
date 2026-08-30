import unittest

from orchestra import brief


def compose(**values):
    defaults = {
        "run_id": 1,
        "display_number": "General #1",
        "profile_name": "Quick",
        "runtime_name": "Codex",
        "request": "organize the inbox",
        "requester": "ios",
        "group_name": "Personal",
        "workdir": "/tmp/personal",
    }
    defaults.update(values)
    return brief.compose(**defaults)


class NeutralBriefTests(unittest.TestCase):
    def test_brief_contains_only_frozen_mission_context_and_small_protocol(self):
        text = compose(context="Only messages from this week")
        self.assertIn("General #1", text)
        self.assertIn("organize the inbox", text)
        self.assertIn("Only messages from this week", text)
        self.assertIn("## Orchestra protocol", text)
        self.assertIn("orchestra artifact PATH", text)
        self.assertLessEqual(len([line for line in brief.PROTOCOL.splitlines()
                                  if line.strip()]), 8)

    def test_delegation_is_explicit_and_profile_bounded(self):
        self.assertNotIn("## Delegation", compose())
        delegated = compose(may_delegate=True)
        self.assertIn("orchestra child --profile PROFILE", delegated)
        self.assertIn("your tier or a lower tier", " ".join(delegated.split()))

    def test_brief_has_no_work_tracker_contract(self):
        text = compose().lower()
        for term in ("handoff", "writeback", "source item", "acceptancecriteria",
                     "findings json", "proposals json"):
            self.assertNotIn(term, text)

    def test_resume_calls_out_replay_risk_and_preserves_new_direction(self):
        text = brief.resume_message(
            reason="interrupted", messages=["Use the other account"],
            replay_risk=True)
        self.assertIn("replay", text.lower())
        self.assertIn("Use the other account", text)


if __name__ == "__main__":
    unittest.main()
