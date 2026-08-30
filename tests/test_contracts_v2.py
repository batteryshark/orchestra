import unittest

from orchestra.contracts import ContractError, RunRequest, child_tier_allowed


class RunRequestTests(unittest.TestCase):
    def test_minimum_request_has_neutral_defaults(self):
        request = RunRequest.from_mapping({
            "request_id": "mail-2026-08-29",
            "profile": "codex-fast",
            "context": "Triage my mail",
        })
        self.assertEqual(request.group, "general")
        self.assertIsNone(request.cwd)
        self.assertEqual(request.observer, "inherit")
        self.assertEqual(request.requested_by, "operator")

    def test_dependencies_are_explicit_and_unique(self):
        request = RunRequest.from_mapping({
            "request_id": "research-2",
            "profile": "claude",
            "context": "Synthesize results",
            "after": [{"run_id": 12, "condition": "terminal"}],
        })
        self.assertEqual(request.after[0].run_id, 12)
        self.assertEqual(request.after[0].condition, "terminal")
        with self.assertRaisesRegex(ContractError, "same run twice"):
            RunRequest.from_mapping({
                **request.as_dict(),
                "after": [{"run_id": 12}, {"run_id": 12}],
            })

    def test_unknown_work_shaped_fields_fail_loudly(self):
        with self.assertRaisesRegex(ContractError, "acceptance"):
            RunRequest.from_mapping({
                "request_id": "x", "profile": "p",
                "context": "m", "acceptance": ["tests pass"],
            })

    def test_delegation_never_moves_up_a_tier(self):
        self.assertTrue(child_tier_allowed(3, 3))
        self.assertTrue(child_tier_allowed(3, 1))
        self.assertFalse(child_tier_allowed(1, 2))


if __name__ == "__main__":
    unittest.main()
