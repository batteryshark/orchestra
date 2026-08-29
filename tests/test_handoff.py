"""The completion-handoff protocol (DESIGN §9): parsing and enforcement."""
import unittest

from orchestra import handoff as protocol

FINDING = ('{"claim": "retry loop drops the last error", "where": "runners.py:88", '
           '"confidence": "observed", "why_not_fixed": "outside this mission"}')
PROPOSAL = '{"title": "Add a retry test", "why": "the loop is untested"}'

HANDOFF = """Work complete.

```json
{"findings": [%s], "proposals": [%s]}
```
"""


class ParseTests(unittest.TestCase):
    def test_both_fields_present_and_empty_is_valid(self) -> None:
        handoff, problems = protocol.parse_handoff(HANDOFF % ("", ""))
        self.assertEqual(problems, [])
        self.assertEqual(handoff, {"findings": [], "proposals": []})

    def test_absent_field_is_a_protocol_problem(self) -> None:
        _, problems = protocol.parse_handoff('```json\n{"findings": []}\n```')
        self.assertEqual(len(problems), 1)
        self.assertIn("`proposals`", problems[0])

    def test_no_block_at_all_is_a_protocol_problem(self) -> None:
        handoff, problems = protocol.parse_handoff("I finished. Nothing to report.")
        self.assertEqual(handoff, {"findings": [], "proposals": []})
        self.assertIn("no handoff block", problems[0])

    def test_last_block_wins_and_bare_fence_is_accepted(self) -> None:
        text = ('```json\n{"findings": [], "proposals": []}\n```\n'
                'then more work\n```\n{"findings": [%s], "proposals": []}\n```' % FINDING)
        handoff, problems = protocol.parse_handoff(text)
        self.assertEqual(problems, [])
        self.assertEqual(len(handoff["findings"]), 1)

    def test_halt_reason_reads_the_handoff_marker(self) -> None:
        text = '```json\n{"findings": [], "proposals": [], "halt": "api gone"}\n```'
        self.assertEqual(protocol.halt_reason(text), "api gone")
        self.assertEqual(protocol.halt_reason(
            '```json\n{"halt": "doomed without a handoff"}\n```'),
            "doomed without a handoff")
        self.assertIsNone(protocol.halt_reason(HANDOFF % ("", "")))
        self.assertIsNone(protocol.halt_reason('```json\n{"halt": "  "}\n```'))
        self.assertIsNone(protocol.halt_reason("no block here"))

    def test_non_list_field_does_not_lose_the_sibling(self) -> None:
        handoff, problems = protocol.parse_handoff(
            '```json\n{"findings": "none", "proposals": [%s]}\n```' % PROPOSAL)
        self.assertEqual(handoff["findings"], [])
        self.assertEqual(len(handoff["proposals"]), 1)
        self.assertIn("not a list", problems[0])

    def test_bad_confidence_is_recorded_as_suspected_not_dropped(self) -> None:
        problems: list[str] = []
        cleaned = protocol.clean_findings(
            [{"claim": "c", "where": "w", "confidence": "pretty sure"}], problems)
        self.assertEqual(cleaned[0]["confidence"], "suspected")
        self.assertEqual(cleaned[0]["why_not_fixed"], "not stated")
        self.assertEqual(len(problems), 2)

    def test_claimless_finding_is_dropped(self) -> None:
        problems: list[str] = []
        self.assertEqual(protocol.clean_findings([{"where": "w"}], problems), [])
        self.assertIn("no `claim`", problems[0])


if __name__ == "__main__":
    unittest.main()
