"""Safe identifier generators (design document section 62).

Faker will happily produce a telephone number that rings someone's house and a
government identifier that belongs to a real person. Section 62 says built-in
generators should avoid accidentally producing valid sensitive identifiers
where practical, so these draw from the ranges reserved for fiction:

* telephone numbers from the North American ``555-0100`` - ``555-0199`` block,
  which carriers do not assign;
* government-identifier-shaped values from the ``900`` area, which the US
  Social Security Administration has never issued.

Both accept ``safe: false`` for the legitimate case where the point of the
dataset is to exercise a validator that rejects fictitious values.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...core.interfaces import SyncGenerator
from ...core.safe_identifiers import safe_phone_us, safe_ssn_shaped
from ..registry import register_generator
from .base import OptionsMixin

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ...core.context import GenerationContext

__all__ = ["GovernmentIdGenerator", "PhoneNumberGenerator"]


@register_generator("phone", aliases=("phone_number", "telephone"))
class PhoneNumberGenerator(OptionsMixin, SyncGenerator):
    """A telephone number from the fictitious 555-01xx block.

    Options:
        ``format``     ``e164`` (default), ``national`` or ``plain``
        ``area_code``  pin the area code instead of drawing one
        ``safe``       use the fictitious block (default ``true``)
    """

    def prepare(self) -> None:
        self.style = self.opt_choice("format", ("e164", "national", "plain"), "e164")
        self.area_code = self.opt_int("area_code", None)
        self.safe = self.opt_bool("safe", True)
        if self.area_code is not None and not 200 <= self.area_code <= 999:
            raise self._fail("option 'area_code' must be between 200 and 999")

    def generate_sync(self, context: GenerationContext) -> Any:
        rng = context.rng()
        if self.safe:
            number = safe_phone_us(rng)  # "+1-AAA-555-01XX"
            _, area, exchange, line = number.split("-")
        else:
            area = str(self.area_code or rng.randint(200, 989))
            exchange = str(rng.randint(200, 999))
            line = f"{rng.randint(0, 9999):04d}"

        if self.area_code is not None:
            area = str(self.area_code)

        if self.style == "e164":
            return f"+1{area}{exchange}{line}"
        if self.style == "national":
            return f"({area}) {exchange}-{line}"
        return f"{area}{exchange}{line}"

    def describe(self) -> str:
        return f"phone({self.style}" + (", fictitious block)" if self.safe else ")")


@register_generator("government_id", aliases=("ssn", "national_id"))
class GovernmentIdGenerator(OptionsMixin, SyncGenerator):
    """A government-identifier-shaped value that cannot belong to anyone.

    Options:
        ``masked``  render as ``***-**-1234`` (default ``false``)
        ``safe``    use the never-issued 900 area (default ``true``)

    With ``safe: false`` this produces a structurally valid identifier. That is
    occasionally the point - testing a validator - but such a value should be
    treated as sensitive even though it is synthetic, because it may
    coincidentally match a real one.
    """

    def prepare(self) -> None:
        self.masked = self.opt_bool("masked", False)
        self.safe = self.opt_bool("safe", True)

    def generate_sync(self, context: GenerationContext) -> Any:
        rng = context.rng()
        if self.safe:
            value = safe_ssn_shaped(rng)
        else:
            value = f"{rng.randint(1, 899):03d}-{rng.randint(1, 99):02d}-{rng.randint(1, 9999):04d}"
        return f"***-**-{value[-4:]}" if self.masked else value

    def describe(self) -> str:
        return "government_id(never-issued range)" if self.safe else "government_id"
