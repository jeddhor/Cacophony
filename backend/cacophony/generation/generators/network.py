"""Computing and network generators (design document sections 62 and 106).

Section 106 lists hostnames, IPs, MACs and OS names among the recipes worth
supporting early - they appear in nearly every security, telemetry and asset
dataset. By default these draw from documentation ranges (section 62) so a
synthetic log can never point at a real host.
"""

from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING, Any

from ...core.interfaces import SyncGenerator
from ...core.safe_identifiers import safe_ipv4, safe_ipv6, safe_mac
from ..registry import register_generator
from .base import OptionsMixin

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ...core.context import GenerationContext

__all__ = ["IpAddressGenerator", "MacAddressGenerator"]


@register_generator("ip", aliases=("ip_address", "ipv4"))
class IpAddressGenerator(OptionsMixin, SyncGenerator):
    """An IP address.

    Options:
        ``network``  a CIDR block to draw from, e.g. ``10.20.0.0/16``
        ``version``  ``4`` (default) or ``6``
        ``safe``     draw from documentation ranges when no network is given
                     (default ``true``; see section 62)

    Supplying ``network`` is the normal way to model a fictional corporate
    estate: private RFC 1918 space is not routable, so it is safe to use
    directly and produces far more realistic-looking logs.
    """

    def prepare(self) -> None:
        self.version = self.opt_int("version", 4) or 4
        if self.version not in (4, 6):
            raise self._fail("option 'version' must be 4 or 6")
        self.safe = self.opt_bool("safe", True)

        network = self.opt_str("network", None, "cidr", "subnet")
        self._network: Any = None
        if network is not None:
            try:
                self._network = ipaddress.ip_network(network, strict=False)
            except ValueError as exc:
                raise self._fail(f"option 'network' is not a valid CIDR block: {exc}") from exc
            self._first = int(self._network.network_address)
            self._last = int(self._network.broadcast_address)
            if self._last - self._first > 2:
                # Skip the network and broadcast addresses.
                self._first += 1
                self._last -= 1

    def generate_sync(self, context: GenerationContext) -> Any:
        rng = context.rng()
        if self._network is not None:
            return str(ipaddress.ip_address(rng.randint(self._first, self._last)))
        if self.version == 6:
            return safe_ipv6(rng)
        if self.safe:
            return safe_ipv4(rng)
        return ".".join(str(rng.randint(1, 254)) for _ in range(4))

    def describe(self) -> str:
        if self._network is not None:
            return f"ip({self._network})"
        return f"ip(v{self.version}, documentation range)"


@register_generator("mac", aliases=("mac_address",))
class MacAddressGenerator(OptionsMixin, SyncGenerator):
    """A MAC address.

    Options:
        ``oui``        a three-octet prefix, e.g. ``00:1a:2b``
        ``separator``  ``:`` (default) or ``-``
        ``upper``      render in upper case
        ``safe``       use the RFC 7042 documentation range (default ``true``)
    """

    def prepare(self) -> None:
        self.safe = self.opt_bool("safe", True)
        self.separator = self.opt_str("separator", ":") or ":"
        self.upper = self.opt_bool("upper", False)

        oui = self.opt_str("oui", None, "prefix", "vendor")
        self._oui: list[int] | None = None
        if oui is not None:
            parts = [part for part in oui.replace("-", ":").split(":") if part]
            if len(parts) != 3:
                raise self._fail(f"option 'oui' must be three octets, got {oui!r}")
            try:
                self._oui = [int(part, 16) for part in parts]
            except ValueError as exc:
                raise self._fail(f"option 'oui' contains a non-hex octet: {oui!r}") from exc

    def generate_sync(self, context: GenerationContext) -> Any:
        rng = context.rng()
        if self._oui is not None:
            octets = [*self._oui, *(rng.randint(0, 255) for _ in range(3))]
            value = self.separator.join(f"{octet:02x}" for octet in octets)
        elif self.safe:
            value = safe_mac(rng).replace(":", self.separator)
        else:
            value = self.separator.join(f"{rng.randint(0, 255):02x}" for _ in range(6))
        return value.upper() if self.upper else value

    def describe(self) -> str:
        return "mac(documentation range)" if self._oui is None and self.safe else "mac"
