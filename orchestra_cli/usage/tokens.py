"""Provider-reported token usage from Orchestra worker logs."""
from __future__ import annotations

import json
import math
from pathlib import Path


def _number(value) -> int | float:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if isinstance(value, float) and math.isfinite(value) and value >= 0:
        return value
    return 0


def token_usage(log_path: str | Path | None) -> dict:
    usage = {
        "total": 0, "input": 0, "output": 0, "reasoning": 0,
        "cache_read": 0, "cache_write": 0, "cost": 0.0, "events": 0,
    }
    if not log_path:
        return usage
    path = Path(log_path)
    if not path.is_file():
        return usage

    try:
        with path.open(encoding="utf-8", errors="replace") as log:
            for line in log:
                if not line.lstrip().startswith("{"):
                    continue
                try:
                    event = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if not isinstance(event, dict):
                    continue

                part = event.get("part")
                if isinstance(part, dict) and part.get("type") == "step-finish":
                    tokens = part.get("tokens") or {}
                    if not isinstance(tokens, dict):
                        continue
                    cache = tokens.get("cache") or {}
                    cache = cache if isinstance(cache, dict) else {}
                    values = {
                        "input": _number(tokens.get("input")),
                        "output": _number(tokens.get("output")),
                        "reasoning": _number(tokens.get("reasoning")),
                        "cache_read": _number(cache.get("read")),
                        "cache_write": _number(cache.get("write")),
                    }
                    usage["total"] += _number(tokens.get("total")) or sum(values.values())
                    usage["cost"] += _number(part.get("cost"))
                elif event.get("type") == "turn.completed":
                    tokens = event.get("usage") or {}
                    if not isinstance(tokens, dict):
                        continue
                    values = {
                        "input": _number(tokens.get("input_tokens")),
                        "output": _number(tokens.get("output_tokens")),
                        "reasoning": _number(tokens.get("reasoning_output_tokens")),
                        "cache_read": _number(tokens.get("cached_input_tokens")),
                        "cache_write": 0,
                    }
                    # Codex input includes cached input and output includes reasoning.
                    usage["total"] += values["input"] + values["output"]
                elif event.get("type") == "result":
                    tokens = event.get("usage") or {}
                    if not isinstance(tokens, dict):
                        continue
                    values = {
                        "input": _number(tokens.get("input_tokens")),
                        "output": _number(tokens.get("output_tokens")),
                        "reasoning": 0,
                        "cache_read": _number(tokens.get("cache_read_input_tokens")),
                        "cache_write": _number(tokens.get("cache_creation_input_tokens")),
                    }
                    usage["total"] += sum(values.values())
                    usage["cost"] += _number(event.get("total_cost_usd"))
                else:
                    continue

                for name, value in values.items():
                    usage[name] += value
                usage["events"] += 1
    except OSError:
        pass
    usage["cost"] = round(usage["cost"], 4)
    return usage
