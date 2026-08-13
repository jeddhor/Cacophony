"""Safe identifier generation (design document section 62).

Synthetic data has a habit of escaping into the world - into bug reports, test
fixtures, screenshots and demo environments. A synthetic email address that
happens to be someone's real mailbox, or a synthetic card number that passes a
Luhn check, is a genuine hazard.

Cacophony therefore defaults to identifiers drawn from ranges the relevant
standards bodies reserved for exactly this purpose, and only produces
realistic-valid identifiers when a schema explicitly asks for them (which is a
legitimate need when the point of the dataset is to test a validator).

Reserved ranges used here:

* ``example.com`` / ``example.org`` / ``.example``, ``.test``, ``.invalid`` - RFC 2606 / 6761
* ``192.0.2.0/24``, ``198.51.100.0/24``, ``203.0.113.0/24`` - RFC 5737 (IPv4 documentation)
* ``2001:db8::/32`` - RFC 3849 (IPv6 documentation)
* ``00:00:5E:00:53:00`` - ``00:00:5E:00:53:FF`` - RFC 7042 (documentation MAC)
* ``+1 555 0100`` - ``+1 555 0199`` - the North American fictitious range
* ``900-xx-xxxx`` SSN-shaped values - never issued by the SSA
"""

from __future__ import annotations

import random
from typing import Final

__all__ = [
    "SAFE_DOMAINS",
    "SAFE_TLDS",
    "safe_ipv4",
    "safe_ipv6",
    "safe_mac",
    "safe_phone_us",
    "safe_ssn_shaped",
    "sanitise_domain",
]

SAFE_DOMAINS: Final[tuple[str, ...]] = ("example.com", "example.org", "example.net")
SAFE_TLDS: Final[tuple[str, ...]] = (".example", ".test", ".invalid", ".localhost")

#: RFC 5737 documentation networks, as (first_octets, /24) pairs.
_DOC_NETWORKS_V4: Final[tuple[tuple[int, int, int], ...]] = (
    (192, 0, 2),
    (198, 51, 100),
    (203, 0, 113),
)


def sanitise_domain(domain: str) -> str:
    """Rewrite a domain onto a reserved TLD unless it already is one.

    >>> sanitise_domain("acme.com")
    'acme.example'
    >>> sanitise_domain("acme.example")
    'acme.example'
    """
    lowered = domain.lower().strip(".")
    if lowered in SAFE_DOMAINS or any(lowered.endswith(tld) for tld in SAFE_TLDS):
        return lowered
    label = lowered.rsplit(".", 1)[0] if "." in lowered else lowered
    return f"{label}.example"


def safe_ipv4(rng: random.Random) -> str:
    """An IPv4 address from an RFC 5737 documentation range."""
    a, b, c = rng.choice(_DOC_NETWORKS_V4)
    return f"{a}.{b}.{c}.{rng.randint(1, 254)}"


def safe_ipv6(rng: random.Random) -> str:
    """An IPv6 address from the RFC 3849 documentation prefix."""
    groups = ":".join(f"{rng.randint(0, 0xFFFF):x}" for _ in range(6))
    return f"2001:db8:{groups}"


def safe_mac(rng: random.Random) -> str:
    """A MAC address from the RFC 7042 documentation range."""
    return f"00:00:5e:00:53:{rng.randint(0, 255):02x}"


def safe_phone_us(rng: random.Random) -> str:
    """A North American number from the 555-0100..555-0199 fictitious block."""
    area = rng.randint(200, 989)
    return f"+1-{area}-555-{rng.randint(100, 199):04d}"


def safe_ssn_shaped(rng: random.Random) -> str:
    """An SSN-shaped value in the 900 area, which the SSA never issues."""
    return f"{rng.randint(900, 999)}-{rng.randint(10, 99):02d}-{rng.randint(1, 9999):04d}"
