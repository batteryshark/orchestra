"""Docker-style memorable run names.

Generates short, human-recognisable identifiers like ``silly_panda`` for each
dispatched run. The numeric ``runs.id`` stays authoritative; the slug is only
a friendly display name + a stable UNIQUE key (so retries can detect
collisions deterministically).

Naming scheme: <adjective>_<noun>. Words are deliberately short, lowercase,
ASCII-safe; no digits in the slug itself (matches the Docker container-naming
pattern operators already expect).

Collision handling: a caller passes the conflict-checking callable; on collision
we retry up to ``max_attempts`` times, then raise — that signals the dispatcher
to surface the failure rather than silently mint a duplicate.
"""

from __future__ import annotations

import secrets
import sqlite3

# 64 adjectives * 64 nouns = 4,096 combinations. Each retry is independent
# (cryptographic RNG), so the birthday bound here is generous for normal
# dispatch volumes.
ADJECTIVES = (
    "admired", "adored", "afraid", "amused", "annoyed", "anxious", "ardent",
    "artful", "astonished", "avid", "bashful", "berserk", "blissful", "bold",
    "bouncy", "brave", "bright", "brisk", "bubbly", "calm", "cheeky", "cheery",
    "chilly", "clever", "cloudy", "cocky", "cozy", "crispy", "curious",
    "dapper", "daring", "dewy", "diligent", "dreamy", "eager", "elated",
    "fancy", "fearless", "feisty", "fierce", "fluffy", "focused", "frosty",
    "gallant", "gentle", "giddy", "glimmer", "goofy", "graceful", "happy",
    "humble", "icy", "jolly", "joyful", "keen", "kindly", "lively", "lucky",
)

NOUNS = (
    "albatross", "badger", "bear", "beaver", "buffalo", "camel", "chameleon",
    "cheetah", "chipmunk", "cobra", "corgi", "coyote", "crane", "dolphin",
    "dove", "dragonfly", "elephant", "ferret", "fox", "gazelle", "gecko",
    "gorilla", "hamster", "heron", "iguana", "jaguar", "kitten", "lemur",
    "leopard", "lion", "llama", "lynx", "mantis", "monkey", "narwhal",
    "ocelot", "octopus", "otter", "owl", "panda", "panther", "puffin",
    "puma", "puppy", "quail", "rabbit", "raven", "sable", "salmon",
    "seal", "shark", "sparrow", "squid", "starling", "tiger", "toucan",
    "turkey", "turtle", "viper", "walrus", "weasel", "wolf", "wombat",
    "yak", "zebra",
)

MAX_ATTEMPTS = 32


def generate_slug() -> str:
    """Return a freshly-minted adjective_noun slug. Pure; does not consult DB."""
    adj = secrets.choice(ADJECTIVES)
    noun = secrets.choice(NOUNS)
    return f"{adj}_{noun}"


def is_valid_slug(value: object) -> bool:
    """Format check only — does not query the DB. Slugs are ASCII, lowercased,
    exactly ``<adjective>_<noun>`` from the curated wordlists. Reject anything
    else so user input can never smuggle newlines or operator escapes into
    SQL or the HTML pane."""
    if not isinstance(value, str):
        return False
    parts = value.split("_")
    if len(parts) != 2:
        return False
    adj, noun = parts
    if not adj or not noun:
        return False
    if not (adj.isalpha() and noun.isalpha()):
        return False
    if not (adj.islower() and noun.islower()):
        return False
    return (adj in ADJECTIVES) and (noun in NOUNS)


def is_unique_violation(exc: sqlite3.IntegrityError) -> bool:
    """True iff ``exc`` looks like a UNIQUE-constraint violation on the
    runs table. SQLite reports it as e.g. ``UNIQUE constraint failed:
    runs.slug``; we sniff the message so we don't have to introspect
    internals."""
    msg = (str(exc) or "").lower()
    return "unique" in msg and "constraint failed" in msg


def assign_slug(con: sqlite3.Connection, *, max_attempts: int = MAX_ATTEMPTS) -> str:
    """Mint a unique slug against the runs table. Raises ``RuntimeError`` if
    every attempt collides (effectively unreachable at expected volumes but
    keeps callers honest).

    Optimisation (not a correctness tool): before consulting the DB we
    short-circuit against the in-process ``_MEMORY_SEEN`` set so a busy
    dispatch loop doesn't reread the table for every insert. THIS SET DOES
    NOT REPLACE the partial UNIQUE index in db.py — the DB is still
    authoritative for concurrent processes and for persisted state.

    Callers that actually write the slug to the table must catch
    ``sqlite3.IntegrityError`` (UNIQUE on slug) and retry, in case a parallel
    process beats them to the slug between read and write."""
    existing = _MEMORY_SEEN | {
        row["slug"]
        for row in con.execute(
            "SELECT slug FROM runs WHERE slug IS NOT NULL"
        )
    }
    for _ in range(max_attempts):
        slug = generate_slug()
        if slug in existing:
            continue
        existing.add(slug)
        _MEMORY_SEEN.add(slug)
        return slug
    raise RuntimeError(
        f"orchestra: could not mint a unique run slug after {max_attempts} attempts"
    )


def reset_memory_cache() -> None:
    """Drop the in-process ``assign_slug`` short-circuit cache. Tests use this
    to keep each scenario isolated; the cache repopulates from the DB on
    the next call."""
    _MEMORY_SEEN.clear()


_MEMORY_SEEN: set[str] = set()
