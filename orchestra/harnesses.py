"""The supported harnesses and the capabilities Orchestra relies on.

This is static product data, not a plugin API. Harness-specific commands and
parsers stay next to the code that uses them.
"""

CAPABILITIES = {
    "claude": {
        "discovery": "manual",
        "launch": True,
        "resume": True,
        "trace": True,
        "usage": True,
        "add_directory": True,
        "transport": ("exec",),
    },
    "codex": {
        "discovery": "cli",
        "launch": True,
        "resume": True,
        "trace": True,
        "usage": True,
        "add_directory": True,
        "transport": ("exec",),
    },
    "opencode": {
        "discovery": "cli",
        "launch": True,
        "resume": True,
        "trace": True,
        "usage": True,
        "add_directory": False,
        "transport": ("exec", "acp"),
    },
    "reasonix": {
        "discovery": "config",
        "launch": True,
        "resume": True,
        "trace": True,
        "usage": True,
        "add_directory": True,
        "transport": ("exec", "acp"),
    },
}

SUPPORTED = tuple(CAPABILITIES)


def supporting(capability: str, value=True) -> tuple[str, ...]:
    """Harnesses with this capability, optionally narrowed to one value."""
    found = []
    for name, facts in CAPABILITIES.items():
        fact = facts[capability]
        match = bool(fact) if value is True else (
            fact == value or isinstance(fact, tuple) and value in fact)
        if match:
            found.append(name)
    return tuple(found)
