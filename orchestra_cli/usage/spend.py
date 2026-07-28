"""Project-local provider spend derived from completed OpenCode steps."""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path

from orchestra_cli import db


def tracked_opencode_spend(root: Path, model_prefix: str) -> float:
    con = db.connect(root)
    try:
        rows = list(
            con.execute(
                "SELECT log_path FROM runs "
                "WHERE backend='opencode' AND model LIKE ? AND log_path IS NOT NULL",
                (f"{model_prefix}%",),
            )
        )
    finally:
        con.close()

    total = 0.0
    for row in rows:
        path = Path(row["log_path"])
        if not path.is_file():
            continue
        try:
            with path.open(encoding="utf-8", errors="replace") as log:
                for line in log:
                    if not line.lstrip().startswith("{"):
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    part = event.get("part")
                    cost = part.get("cost") if isinstance(part, dict) else None
                    if (
                        isinstance(cost, (int, float))
                        and not isinstance(cost, bool)
                        and math.isfinite(cost)
                        and cost >= 0
                        and part.get("type") == "step-finish"
                    ):
                        total += float(cost)
        except OSError:
            continue
    return round(total, 4)


def with_project_spend(snapshot: dict, root: Path | None) -> dict:
    """Return a copy enriched with explicitly project-scoped spend."""
    if root is None:
        return snapshot
    result = copy.deepcopy(snapshot)
    for provider in result.get("providers") or []:
        if not isinstance(provider, dict) or provider.get("id") != "together":
            continue
        balance = provider.get("account_balance")
        if not isinstance(balance, dict):
            continue
        balance["spent"] = tracked_opencode_spend(root, "togetherai/")
        balance["spent_scope"] = f"{root.name} runs"
    return result
