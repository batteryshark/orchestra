"""Conservative automatic retry policy.

This module classifies terminal outcomes only.  Admission of the separately
numbered retry run belongs to the scheduler, which can copy the frozen request
and lineage without hiding the failed attempt.
"""
from __future__ import annotations

AUTH_MARKERS = (
    "authentication failed", "failed to authenticate", "unauthorized",
    "invalid api key", "invalid token", "token expired",
)
TRANSIENT_MARKERS = (
    "connection reset", "connection refused", "connection timed out",
    "temporarily unavailable", "service unavailable", "gateway timeout",
    "rate limit", "rate-limit", "too many requests", "overloaded",
    "provider capacity", "capacity exhausted", "network is unreachable",
    "broken pipe", "tls handshake timeout",
)


def classify(status: str, detail: str | None = None) -> str:
    """Return intentional, success, auth, transient, or unknown."""
    state = (status or "").strip().lower()
    if state == "completed":
        return "success"
    if state in {"stopped", "skipped"}:
        return "intentional"
    if state == "timed_out":
        return "transient"
    if state != "failed":
        return "unknown"
    text = (detail or "").casefold()
    if any(marker in text for marker in AUTH_MARKERS):
        return "auth"
    if any(marker in text for marker in TRANSIENT_MARKERS):
        return "transient"
    return "unknown"


def decide(status: str, detail: str | None = None, *,
           automatic_retries: int = 0) -> dict:
    """Choose retry once, alert, or do nothing.

    Unknown and authentication failures are intentionally not retried.  The
    scheduler should create a new lineage run only for ``action == 'retry'``.
    """
    if automatic_retries < 0:
        raise ValueError("automatic_retries cannot be negative")
    failure = classify(status, detail)
    if failure in {"success", "intentional"}:
        return {"action": "none", "classification": failure}
    if failure == "transient" and automatic_retries == 0:
        return {"action": "retry", "classification": failure}
    reason = ("automatic retry already used" if failure == "transient" else
              "credentials require operator attention" if failure == "auth" else
              "failure is not known to be transient")
    return {"action": "alert", "classification": failure, "reason": reason}
