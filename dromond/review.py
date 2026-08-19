"""Performance review of runners (W-0130): how each profile actually did.

``dromond stats`` answers "how much did we run"; this answers "how well did
each profile do" — outcomes, not volume — so staffing decisions (tier,
priority, ``dromond profiles note``, all of which the router reads) can come
from evidence instead of impression. Read-only over the runs table: a run
row is stamped at finalization (status, usage), and this never writes.
"""

from dromond import db, http, runway


def performance(con) -> list[dict]:
    """One row per (profile, model) over TERMINAL runs, worst first.

    A live run is not a review subject, so it is excluded. Neither is a
    control turn (W-0214): a router turn is not the profile's work. Success is
    ``done / terminal runs``; tokens and cost are sums of what was captured,
    with ``uncaptured`` counting the runs that carry no usage (DESIGN §11 —
    null is "not captured", never a zero). A plan-backed run has no price
    (W-0179), so its cost contributes nothing and ``plan_runs`` says why.
    """
    rows: dict[tuple, dict] = {}
    for r in con.execute(
            "SELECT profile, backend, model, status, started_at, finished_at, "
            "tokens_total, cost_usd FROM runs WHERE layer IS NULL AND status IN "
            f"({','.join('?' * len(db.RUN_TERMINAL))})", db.RUN_TERMINAL):
        key = (r["profile"], r["model"] or "")
        entry = rows.setdefault(key, {
            "profile": r["profile"], "model": r["model"], "runs": 0,
            "done": 0, "failed": 0, "timeout": 0, "killed": 0,
            "seconds": 0.0, "tokens": None, "cost": None,
            "plan_runs": 0, "uncaptured": 0})
        entry["runs"] += 1
        entry[r["status"]] += 1
        entry["seconds"] += http._seconds(r["started_at"], r["finished_at"]) or 0.0
        if r["tokens_total"] is None:
            entry["uncaptured"] += 1
        else:
            entry["tokens"] = (entry["tokens"] or 0) + r["tokens_total"]
        if runway.kind_of(runway.provider_of(r["backend"], r["model"])) == "plan":
            entry["plan_runs"] += 1
        elif r["cost_usd"] is not None:
            entry["cost"] = round((entry["cost"] or 0) + r["cost_usd"], 6)
    for entry in rows.values():
        entry["success"] = round(entry["done"] / entry["runs"], 3)
        entry["avg_seconds"] = round(entry["seconds"] / entry["runs"], 1)
        del entry["seconds"]
    return sorted(rows.values(), key=lambda e: (e["success"], -e["runs"],
                                                e["profile"]))
