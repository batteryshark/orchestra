"""Tailscale-only UI bind mode, modeled on Work's ``tailscale-network.mjs``.

Discovery uses the same two facts Work relies on:
  - Tailscale reserves 100.64.0.0/10 for tailnet addresses (octet 1 == 100,
    octet 2 in [64..127]).
  - ``tailscale ip -4`` reports exactly one such address per machine.

We also pin which bind hosts are acceptable for ``orchestra ui``. The CLI
defaults to loopback (no LAN exposure, no Tailnet exposure) and refuses
ordinary LAN hosts or the wildcard address. The ``--tailscale`` flag is the
only broader listener mode and is what the project's docs and this worker
brief prescribe.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Sequence

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

# Wildcard IPv4 / IPv6 — orchestra never binds an "all interfaces" socket.
WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", "[::]"})

# Tailnet IPv4 range is 100.64.0.0/10. Octet 1 MUST be 100; octet 2 in 64-127.
_TAILSCALE_OCTET_RE = re.compile(
    r"^(?P<a>\d{1,3})\.(?P<b>\d{1,3})\.(?P<c>\d{1,3})\.(?P<d>\d{1,3})$"
)


class TailscaleError(RuntimeError):
    """Raised when Tailscale bind mode cannot satisfy the request. The CLI
    surfaces this verbatim so operators see the actionable cause; the API
    layer maps it to a clear 4xx-style exit message."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def is_tailscale_ipv4(value: object) -> bool:
    """True iff ``value`` looks like a Tailscale tailnet IPv4 address."""
    if not isinstance(value, str):
        return False
    match = _TAILSCALE_OCTET_RE.match(value.strip())
    if not match:
        return False
    a, b, _c, _d = (int(match.group(name)) for name in ("a", "b", "c", "d"))
    if not all(0 <= int(match.group(name)) <= 255 for name in ("a", "b", "c", "d")):
        return False
    return a == 100 and 64 <= b <= 127


def discover_tailscale_ipv4(*, runner: callable = None) -> str:
    """Run ``tailscale ip -4`` and return the first tailnet IPv4 address.

    Pass ``runner`` for tests; production goes straight to ``subprocess.run``.
    Errors are surfaced with two distinct codes so operators know whether
    Tailscale is missing entirely (``tailscale_unavailable``) or just
    disconnected (``tailscale_address_unavailable``).
    """
    if runner is None:
        if shutil.which("tailscale") is None:
            raise TailscaleError(
                "Could not find the `tailscale` CLI. Install Tailscale and "
                "make sure `tailscale ip -4` works from this shell.",
                code="tailscale_unavailable",
            )
        try:
            completed = subprocess.run(
                ["tailscale", "ip", "-4"],
                capture_output=True, text=True, timeout=10,
            )
        except subprocess.TimeoutExpired as exc:
            raise TailscaleError(
                "Tailscale did not respond. Make sure it is running.",
                code="tailscale_unavailable",
            ) from exc
        except OSError as exc:
            raise TailscaleError(
                f"Could not run `tailscale ip -4`: {exc}.",
                code="tailscale_unavailable",
            ) from exc
        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip() or completed.stdout.strip()
            raise TailscaleError(
                "Could not ask Tailscale for this machine's IPv4 address. "
                f"Make sure Tailscale is installed, running, and connected. {stderr}",
                code="tailscale_unavailable",
            )
        stdout = completed.stdout
    else:
        try:
            payload = runner()
        except Exception as exc:  # test seams raise plain errors
            raise TailscaleError(
                "Could not ask Tailscale for this machine's IPv4 address. "
                f"Make sure Tailscale is installed, running, and connected. {exc}",
                code="tailscale_unavailable",
            ) from exc
        stdout = payload if isinstance(payload, str) else str(payload or "")

    for token in stdout.split():
        if is_tailscale_ipv4(token):
            return token.strip()
    raise TailscaleError(
        "Tailscale did not report a connected IPv4 address. "
        "Connect this machine to a tailnet and try again.",
        code="tailscale_address_unavailable",
    )


@dataclass(frozen=True)
class BindPlan:
    host: str
    tailscale: bool
    fallback_used: bool = False


def _normalise(host: str) -> str:
    return host.strip().rstrip("]")


def resolve_bind_host(
    *,
    explicit_host: str | None,
    tailscale: bool,
) -> BindPlan:
    """Resolve what ``orchestra ui`` should bind to.

    Rules (matching docs/LOCAL-WORKSPACE.md):
      * Loopback is the default and the only listener when ``tailscale`` is
        False.
      * ``tailscale=True`` is the only broader mode. We discover the machine's
        tailnet IPv4 and bind exactly that interface — never a wildcard or
        ordinary LAN address. If Tailscale is unavailable, raise
        ``TailscaleError`` so the CLI prints a clear failure.
      * Explicit ``--host`` accepts loopback OR a Tailscale IPv4; it rejects
        the wildcard address and ordinary LAN hosts because neither belongs
        on an Orchestra UI listener.
    """
    if tailscale:
        host = discover_tailscale_ipv4()
        return BindPlan(host=host, tailscale=True)

    if explicit_host is not None:
        normalised = _normalise(explicit_host)
        if normalised in WILDCARD_HOSTS or normalised.strip("[]") in WILDCARD_HOSTS:
            raise TailscaleError(
                "Refusing to bind a wildcard address. Use loopback "
                "(default) or pass --tailscale to bind only your Tailnet IPv4.",
                code="invalid_listen_host",
            )
        if normalised in LOOPBACK_HOSTS:
            return BindPlan(host="127.0.0.1", tailscale=False)
        if is_tailscale_ipv4(normalised):
            return BindPlan(host=normalised, tailscale=True)
        raise TailscaleError(
            f"Refusing to bind {explicit_host!r}: wildcards and ordinary LAN "
            "addresses are not allowed. Use loopback (default) or pass "
            "--tailscale to bind only your Tailnet IPv4.",
            code="invalid_listen_host",
        )

    # Default: bind loopback only. No Tailnet exposure, no LAN exposure.
    return BindPlan(host="127.0.0.1", tailscale=False)


def is_loopback_host(host: str) -> bool:
    """True iff ``host`` is one of the loopback addresses the CLI binds by default."""
    return _normalise(host) in LOOPBACK_HOSTS


def is_wildcard_host(host: str) -> bool:
    """True iff ``host`` is one of the wildcard addresses we always reject."""
    stripped = _normalise(host)
    return stripped in WILDCARD_HOSTS or stripped.strip("[]") in WILDCARD_HOSTS
