"""Who is calling: the human, or one specific run (DESIGN §3, §5).

Two credentials reach the HTTP surface, and they are not the same authority.

- **The shared secret** — ``X-Dromond-Key`` from the 0600 config, the
  dashboard's and the iOS app's credential. Full authority, unchanged.
- **A per-run token** (W-0176) — minted at dispatch into the worker's
  environment as ``DROMOND_RUN_TOKEN`` and sent as the *same* header. It says
  which run is calling, it cannot claim to be the human, it cannot act on a
  sibling run, and it stops working the moment its run ends.

Before this the split was a DECLARATION: an ``X-Dromond-Run`` header the
caller chose, or ``DROMOND_RUN_ID`` in its own environment. Every worker
holds the shared secret, so a worker that simply omitted the header was
treated as the human — auditable, never contained. A token inverts that:
omitting it does not promote you, it only fails.

Storage is the hash, never the value: ``runs.run_token_hash`` holds the
SHA-256, the raw token exists in the worker's environment and nowhere else.
Revocation is a database trigger (``db.SCHEMA``), so every path to a terminal
status revokes without a finalizer having to remember.

``ROUTES`` below is the entire authority table. It lives here, in one dict,
so the answer to "what may a run do" is read rather than reconstructed from
handlers — and an unlisted route is the human's by default, so adding a route
never accidentally hands a run authority.
"""
import hashlib
import os
import re
import secrets
from typing import NamedTuple

from dromond import db, paths

TOKEN_ENV = "DROMOND_RUN_TOKEN"

HUMAN = "human"   # the shared secret
RUN = "run"       # a live run's own token


class Identity(NamedTuple):
    kind: str
    run_id: int | None = None


# --- the authority table ----------------------------------------------------

BOTH = "both"        # a read: the human and any live run
SELF = "self"        # the human, or a run acting on ITSELF and no other
ONLY_HUMAN = "human"  # the shared secret alone
SPLIT = "split"      # both reach it; profile_edit splits by cost inside it

ROUTES: dict[str, str] = {
    "GET /":                       BOTH,
    "GET /api/snapshot":           BOTH,
    "GET /api/profiles/options":   BOTH,
    "GET /api/*/stream":           BOTH,   # the daemon log (W-0165)
    # A run's trace is run-scoped like stop/tell/check: a live run may watch
    # itself work and no sibling's (W-0178). The catch-all above is a READ of
    # the service, so it stays BOTH; this is a read of ONE run's transcript.
    "GET /api/runs/{run}/stream":  SELF,
    # A brief or diff is one run's mission/work, not the service's, so a run
    # token reads its own and no sibling's.
    "GET /api/runs/{run}/brief":   SELF,
    "GET /api/runs/{run}/diff":    SELF,
    "POST /api/runs/{run}/stop":   SELF,
    "POST /api/runs/{run}/tell":   SELF,
    "POST /api/runs/{run}/check":  SELF,
    "POST /api/sweep":             ONLY_HUMAN,
    "POST /api/restart":           ONLY_HUMAN,
    "POST /api/dispatch/pause":    ONLY_HUMAN,
    "POST /api/dispatch/resume":   ONLY_HUMAN,
    # note/effort-down for a run; everything dearer files a Work
    # decision — the split itself is in profile_edit.save.
    "POST /api/profiles/{name}":   SPLIT,
}
DEFAULT_LEVEL = ONLY_HUMAN

_RUN_PATH = re.compile(r"^/api/runs/(\d+)/(stop|tell|check|stream|brief|diff)$")
_PROFILE_PATH = re.compile(r"^/api/profiles/[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def route_key(method: str, path: str) -> tuple[str, int | None]:
    """``("POST /api/runs/{run}/stop", 12)`` — the table key and the run the
    request is aimed at (None when it names no run)."""
    method = "GET" if method == "HEAD" else method
    match = _RUN_PATH.match(path)
    if match:
        return f"{method} /api/runs/{{run}}/{match.group(2)}", int(match.group(1))
    if path.startswith("/api/") and path.endswith("/stream"):
        return f"{method} /api/*/stream", None
    if path != "/api/profiles/options" and _PROFILE_PATH.match(path):
        return f"{method} /api/profiles/{{name}}", None
    return f"{method} {path}", None


def permit(identity: Identity, key: str, target_run: int | None = None) -> str | None:
    """None when this identity may call this route, else the one-line reason."""
    if identity.kind == HUMAN:
        return None
    level = ROUTES.get(key, DEFAULT_LEVEL)
    if level in (BOTH, SPLIT):
        return None
    if level == SELF:
        if target_run is not None and target_run == identity.run_id:
            return None
        return (f"run {identity.run_id} may act on itself, not on run "
                f"{target_run}")
    return f"{key} is the human's — a run token carries no authority there"


# --- minting, hashing, identifying ------------------------------------------

def hashed(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def mint(con, run_id: int) -> str:
    """The run's own credential, returned once. Only its hash is stored."""
    raw = secrets.token_urlsafe(32)
    con.execute("UPDATE runs SET run_token_hash=? WHERE id=?", (hashed(raw), run_id))
    con.commit()
    return raw


def identify(con, supplied: str, human_key: str | None) -> Identity | None:
    """The caller behind a credential, or None when it matches nothing.

    A terminal run's token matches nothing: the revocation trigger nulled the
    hash, and this query would refuse it even if a row kept one.
    """
    supplied = (supplied or "").strip()
    if not supplied:
        return None
    if human_key and secrets.compare_digest(supplied, human_key):
        return Identity(HUMAN)
    row = con.execute(
        f"SELECT id FROM runs WHERE run_token_hash=? AND status NOT IN {db.TERMINAL_SQL}",
        (hashed(supplied),)).fetchone()
    return Identity(RUN, int(row["id"])) if row else None


def run_from_env(con) -> int | None:
    """The run whose token this process carries, if it carries a live one.

    The CLI's half of the same question. It is weaker than the HTTP half by
    construction — a worker can unset an environment variable, and the CLI
    talks to the database and the config file directly, so a worker that
    wants the human's authority locally was never stopped by a credential.
    Containment lives at the HTTP surface; this keeps the honest path honest.
    """
    raw = paths.env(TOKEN_ENV).strip()
    if not raw:
        return None
    identity = identify(con, raw, None)
    return identity.run_id if identity else None
