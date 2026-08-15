"""Docker-style memorable run names (ported from Orchestra).

``silly_panda``-style display aliases for runs. The numeric ``runs.id``
stays authoritative; the slug is a friendly display name plus a stable
UNIQUE key so retries detect collisions deterministically.
"""

from __future__ import annotations

import secrets
import sqlite3

# 58 adjectives * 65 nouns = 3,770 combinations; each retry is an independent
# draw from a cryptographic RNG, so the birthday bound is generous for normal
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
    return f"{secrets.choice(ADJECTIVES)}_{secrets.choice(NOUNS)}"


def is_valid_slug(value: object) -> bool:
    """Format check only — exactly ``<adjective>_<noun>`` from the curated
    wordlists, so user input can never smuggle operator escapes into SQL or
    display surfaces."""
    if not isinstance(value, str):
        return False
    parts = value.split("_")
    if len(parts) != 2:
        return False
    adj, noun = parts
    if not (adj and noun and adj.isalpha() and noun.isalpha()):
        return False
    if not (adj.islower() and noun.islower()):
        return False
    return (adj in ADJECTIVES) and (noun in NOUNS)


def is_unique_violation(exc: sqlite3.IntegrityError) -> bool:
    """True iff ``exc`` looks like a UNIQUE-constraint violation."""
    msg = (str(exc) or "").lower()
    return "unique" in msg and "constraint failed" in msg


def assign_slug(con: sqlite3.Connection, *, max_attempts: int = MAX_ATTEMPTS) -> str:
    """Mint a slug unique against the runs table. Raises ``RuntimeError`` if
    every attempt collides.

    The in-process ``_MEMORY_SEEN`` short-circuit is an optimisation only;
    the DB UNIQUE constraint stays authoritative for concurrent processes.
    Callers writing the slug must catch ``sqlite3.IntegrityError`` and retry.
    """
    existing = _MEMORY_SEEN | {
        row["slug"]
        for row in con.execute("SELECT slug FROM runs WHERE slug IS NOT NULL")
    }
    for _ in range(max_attempts):
        slug = generate_slug()
        if slug in existing:
            continue
        existing.add(slug)
        _MEMORY_SEEN.add(slug)
        return slug
    raise RuntimeError(
        f"dromond: could not mint a unique run slug after {max_attempts} attempts"
    )


def reset_memory_cache() -> None:
    """Drop the in-process short-circuit cache (tests; collision retries)."""
    _MEMORY_SEEN.clear()


_MEMORY_SEEN: set[str] = set()
