"""Detectors for data that looks real (design document section 61).

    Cacophony creates synthetic data, but models may accidentally reproduce
    real information.

Section 61 asks for optional detectors for real-looking government identifiers,
genuine card numbers, known domains and suspiciously realistic public
identities, and for a clear distinction between *synthetic representation* and
*real anonymised data*.

Three of those four are decidable from the value itself, and this module decides
them: a card number either passes a Luhn check or does not, a domain either sits
in a reserved range or does not. The fourth - a suspiciously realistic public
identity - is not decidable without a list of real people, which is a thing this
project should not build and should not ship. What is done instead is narrower
and honest: a field can be labelled, and a labelled field's findings are
reported under that label.

Nothing here claims the output is privacy-preserving in the mathematical sense.
Section 61 is explicit that such a claim needs an actual privacy technique, and
detection is not one; it catches leakage, which is a different and smaller thing.
"""

from __future__ import annotations

import ipaddress
import re
from typing import TYPE_CHECKING

from ..core.safe_identifiers import SAFE_DOMAINS, SAFE_TLDS
from .results import Severity, ValidationResult

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..core.record import GeneratedRecord
    from ..schema.models import PrivacySpec
    from ..schema.plan import CompiledEntity

__all__ = ["CHECKS", "PrivacyValidator", "looks_like_a_real_card"]

#: Every check, by name. A schema may ask for a subset.
CHECKS: tuple[str, ...] = (
    "card_numbers",
    "government_ids",
    "domains",
    "addresses",
    "phone_numbers",
)

_DIGITS = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_SSN = re.compile(r"(?<!\d)(\d{3})-(\d{2})-(\d{4})(?!\d)")
_DOMAIN = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})|https?://([A-Za-z0-9.-]+)")
#: A value that is nothing but a hostname. Only trusted where the field says it
#: holds one: `config.yaml` is this shape too, and reporting it as a leaked
#: domain is how a detector earns its way into somebody's ignore list.
_BARE_DOMAIN = re.compile(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\.?")
_US_PHONE = re.compile(r"(?<!\d)(?:\+?1[ .-]?)?\(?(\d{3})\)?[ .-]?(\d{3})[ .-]?(\d{4})(?!\d)")
_IPV4 = re.compile(r"(?<![\d.])((?:\d{1,3}\.){3}\d{1,3})(?![\d.])")
#: Anything colon-separated that might be IPv6. Deliberately loose: every hit is
#: handed to `ipaddress` before it counts, so the regex only has to find
#: candidates, not to know the address format.
_IPV6 = re.compile(r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![0-9A-Fa-f:])")


def looks_like_a_real_card(digits: str) -> bool:
    """Whether a run of digits passes the Luhn check a card issuer would apply.

    A generated card number that happens to be valid is the one identifier in
    section 62's list that a system downstream might actually try to charge.
    """
    cleaned = [int(character) for character in digits if character.isdigit()]
    if not 13 <= len(cleaned) <= 19:
        return False
    total = 0
    for position, digit in enumerate(reversed(cleaned)):
        if position % 2:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _safe_domain(domain: str) -> bool:
    lowered = domain.lower().rstrip(".")
    return lowered in SAFE_DOMAINS or any(lowered.endswith(tld) for tld in SAFE_TLDS)


def _is_an_address(text: str) -> bool:
    """Whether this candidate is an IP address at all.

    The IPv6 pattern matches things that merely look like one - `12:34:56` is a
    duration - so parsing decides, not the regex.
    """
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


def _safe_address(text: str) -> bool:
    """Documentation ranges, and the private ranges nobody can route to."""
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return True
    return (
        address.is_private
        or address.is_loopback
        or address.is_reserved
        or address in ipaddress.ip_network("192.0.2.0/24")
        or address in ipaddress.ip_network("198.51.100.0/24")
        or address in ipaddress.ip_network("203.0.113.0/24")
        or address in ipaddress.ip_network("2001:db8::/32")
    )


#: Field types whose whole point is to carry an address or a number, where a
#: match inside a longer string is still worth reporting.
_NETWORK_TYPES = frozenset({"ip_address", "cidr", "hostname", "uri", "phone"})

#: Field types that hold a name of a host, where the whole value being a domain
#: is the ordinary case rather than a coincidence.
_HOSTNAME_TYPES = frozenset({"hostname", "uri", "email", "domain"})


def findings(
    value: object, checks: frozenset[str], *, field_type: str | None = None
) -> list[tuple[str, str]]:
    """What looks real in one value, as (check, what) pairs.

    Addresses and telephone numbers are matched against the *whole* value unless
    the field is one that carries them by definition. Scanning free prose for
    dotted quads reports every version string ever written - `rv:1.9.6.20` in a
    browser user agent is four octets in range - and a detector that cries wolf
    is a detector people turn off. Domains and card numbers are still scanned
    inside longer text, because neither has a plausible innocent double.
    """
    if not isinstance(value, str) or not value:
        return []

    whole = value.strip()
    scan_everywhere = (field_type or "") in _NETWORK_TYPES

    found: list[tuple[str, str]] = []

    if "card_numbers" in checks:
        for match in _DIGITS.finditer(value):
            if looks_like_a_real_card(match.group(0)):
                found.append(("card_numbers", "a card number that passes a Luhn check"))
                break

    if "government_ids" in checks:
        for area, group, serial in _SSN.findall(value):
            # 900-999 is never issued, which is what the generator draws from;
            # 000 and 666 are also impossible, so neither is a leak.
            if not (900 <= int(area) <= 999 or area in ("000", "666") or group == "00"):
                found.append(
                    ("government_ids", f"an issuable SSN-shaped value ({area}-{group}-{serial})")
                )
                break

    if "domains" in checks:
        candidates = [mailbox or url for mailbox, url in _DOMAIN.findall(value) if mailbox or url]
        # A field declared to hold a hostname, holding one: no address and no
        # scheme to find it inside, and nowhere else it could be hiding.
        if (field_type or "") in _HOSTNAME_TYPES and _BARE_DOMAIN.fullmatch(whole):
            candidates.append(whole.rstrip("."))
        for domain in candidates:
            if not _safe_domain(domain):
                found.append(("domains", f"a domain outside the reserved ranges ({domain})"))
                break

    if "addresses" in checks:
        if scan_everywhere:
            addresses = [*_IPV4.findall(value), *_IPV6.findall(value)]
        else:
            # Version strings are dotted quads too, so outside a field that
            # says it holds an address the whole value has to be one. An IPv6
            # address has no innocent double of that kind, so it counts
            # wherever it appears.
            addresses = ([whole] if _IPV4.fullmatch(whole) else []) + _IPV6.findall(value)
        for candidate in addresses:
            if _is_an_address(candidate) and not _safe_address(candidate):
                found.append(("addresses", f"a routable IP address ({candidate})"))
                break

    if "phone_numbers" in checks:
        source = value if scan_everywhere else (whole if _US_PHONE.fullmatch(whole) else "")
        for area, exchange, line in _US_PHONE.findall(source):
            if not (exchange == "555" and line.startswith("01")):
                found.append(
                    ("phone_numbers", f"a dialable telephone number ({area}) {exchange}-{line}")
                )
                break

    return found


class PrivacyValidator:
    """Runs the configured detectors over a record's values."""

    category = "privacy"

    def __init__(self, entity: CompiledEntity, spec: PrivacySpec) -> None:
        self.entity = entity
        self.spec = spec
        self.checks = frozenset(spec.checks or CHECKS)
        #: Fields that are meant to hold realistic values, and so are exempt.
        #: A schema that says `safe: false` has already made this choice once,
        #: and saying it twice would be a trap.
        self.exempt = {
            field.name
            for field in entity.fields
            if str(field.spec.privacy or "").lower() in ("allow", "allow_real", "real")
            # `safe: false` is a schema saying it wants realistic values here.
            # Flagging them afterwards would be arguing with an instruction.
            or getattr(field.generator, "options", {}).get("safe") is False
        }
        #: What a field was labelled, for the report.
        self.labels = {
            field.name: str(field.spec.privacy)
            for field in entity.fields
            if field.spec.privacy and field.name not in self.exempt
        }
        #: What each field says it holds, so an address in a field that is about
        #: addresses is read differently from four numbers in a sentence.
        self.types = {field.name: str(field.spec.type) for field in entity.fields}
        self.counts: dict[str, int] = {}

    @property
    def is_noop(self) -> bool:
        return not self.spec.is_enabled() or not self.checks

    def validate(
        self, record: GeneratedRecord, *, skip: set[str] | None = None
    ) -> ValidationResult:
        result = ValidationResult()
        if self.is_noop:
            return result

        damaged = skip or set()
        severity = Severity.ERROR if self.spec.policy == "block" else Severity.WARNING

        for name, value in record.values.items():
            if name in self.exempt or name in damaged:
                continue
            declared = self.types.get(name)
            for check, what in findings(value, self.checks, field_type=declared):
                self.counts[check] = self.counts.get(check, 0) + 1
                label = f" [{self.labels[name]}]" if name in self.labels else ""
                result.add(
                    self.category,
                    f"{what}{label}",
                    field_name=name,
                    severity=severity,
                )
        return result

    def summary(self) -> dict[str, object]:
        return {
            "policy": self.spec.policy,
            "checks": sorted(self.checks),
            "exempt_fields": sorted(self.exempt),
            "findings": dict(sorted(self.counts.items())),
        }
