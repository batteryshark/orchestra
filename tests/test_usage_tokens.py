from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orchestra_cli.usage.tokens import token_usage


class TokenUsageTests(unittest.TestCase):
    def test_reads_each_supported_provider_without_estimating(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "run.jsonl"
            events = [
                {"part": {"type": "step-finish", "tokens": {
                    "total": 19, "input": 2, "output": 3, "reasoning": 4,
                    "cache": {"read": 10, "write": 0}}, "cost": 0.25}},
                {"type": "turn.completed", "usage": {
                    "input_tokens": 20, "cached_input_tokens": 12,
                    "output_tokens": 5, "reasoning_output_tokens": 2}},
                {"type": "result", "usage": {
                    "input_tokens": 7, "output_tokens": 8,
                    "cache_read_input_tokens": 9,
                    "cache_creation_input_tokens": 6}, "total_cost_usd": 0.5},
                {"type": "result", "usage": "unavailable"},
                [],
            ]
            log.write_text("not json\n" + "\n".join(map(json.dumps, events)) + "\n")

            usage = token_usage(log)

        self.assertEqual(usage["total"], 74)
        self.assertEqual(usage["input"], 29)
        self.assertEqual(usage["output"], 16)
        self.assertEqual(usage["reasoning"], 6)
        self.assertEqual(usage["cache_read"], 31)
        self.assertEqual(usage["cache_write"], 6)
        self.assertEqual(usage["cost"], 0.75)
        self.assertEqual(usage["events"], 3)

    def test_missing_log_has_zero_usage(self) -> None:
        self.assertEqual(token_usage(None)["events"], 0)
        self.assertEqual(token_usage("/definitely/missing")["total"], 0)


if __name__ == "__main__":
    unittest.main()
