"""Faker-backed generators (design document section 8).

Faker supplies the conventional providers - names, addresses, phone numbers,
company names, postal codes - so Cacophony does not have to.

Two things need care:

**Reproducibility.** Faker keeps its own RNG. Left alone it would ignore
Cacophony's seed hierarchy entirely, so the instance is reseeded from the
field's derived seed before every call. Identical seed, identical value,
regardless of generation order (section 75).

**Safety.** Faker happily produces ``john.smith@gmail.com``. Section 62 says
built-in generators should avoid accidentally producing valid sensitive
identifiers, so domain-bearing values are rewritten onto reserved ranges
unless the schema explicitly asks otherwise with ``safe: false``.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, ClassVar

from ...core.interfaces import SyncGenerator
from ...core.safe_identifiers import sanitise_domain
from ..registry import register_generator
from .base import OptionsMixin

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ...core.context import GenerationContext

__all__ = ["FakerGenerator"]

#: Providers whose output contains a domain that must be made safe.
_DOMAIN_PROVIDERS = frozenset(
    {
        "email",
        "ascii_email",
        "safe_email",
        "free_email",
        "company_email",
        "ascii_company_email",
        "ascii_free_email",
        "ascii_safe_email",
        "domain_name",
        "domain_word",
        "hostname",
        "url",
        "uri",
    }
)

_EMAIL_SPLIT = re.compile(r"^(?P<local>[^@]+)@(?P<domain>.+)$")


@register_generator("faker", aliases=("fake",))
class FakerGenerator(OptionsMixin, SyncGenerator):
    """Call a Faker provider.

    Options:
        ``provider``  the Faker method to call, e.g. ``first_name``, ``company``
        ``locale``    a Faker locale (defaults to the project locale)
        ``safe``      rewrite domains onto reserved ranges (default ``true``)
        ``unique``    ask Faker for a distinct value each call

    Any other option is forwarded to the provider as a keyword argument, so
    ``provider: pyint`` with ``min_value: 10`` works as expected.
    """

    #: One Faker instance per locale, shared across every field that wants it.
    _instances: ClassVar[dict[str, Any]] = {}

    #: Options consumed by this generator rather than forwarded to Faker.
    _OWN_OPTIONS = frozenset({"provider", "locale", "safe", "unique", "type"})

    def prepare(self) -> None:
        provider = self.opt_str("provider", None, "method", "faker")
        if provider is None:
            raise self._fail(
                "option 'provider' is required, e.g. provider: first_name. "
                "Run 'cacophony generators --faker' to list what is available."
            )
        self.provider = provider
        self.safe = self.opt_bool("safe", True)
        self.unique = self.opt_bool("unique", False)

        locale = self.opt_str("locale", None)
        if locale is None and self.field is not None:
            locale = self.field.locale
        self.locale = locale or "en_US"

        self.kwargs = {
            key: value for key, value in self.options.items() if key not in self._OWN_OPTIONS
        }

        faker = self._faker()
        if not hasattr(faker, self.provider):
            raise self._fail(
                f"Faker locale '{self.locale}' has no provider named '{self.provider}'"
            )

    def _faker(self) -> Any:
        instance = self._instances.get(self.locale)
        if instance is None:
            from faker import Faker

            instance = Faker(self.locale)
            self._instances[self.locale] = instance
        return instance

    def generate_sync(self, context: GenerationContext) -> Any:
        faker = self._faker()
        # Reseed from the field's derived seed so Faker follows Cacophony's
        # hierarchy rather than its own global sequence.
        faker.seed_instance(context.seed)

        source = faker.unique if self.unique else faker
        try:
            value = getattr(source, self.provider)(**self.kwargs)
        except TypeError as exc:
            raise self._fail(
                f"provider '{self.provider}' rejected the supplied options: {exc}"
            ) from exc

        if self.safe and self.provider in _DOMAIN_PROVIDERS:
            return _make_safe(self.provider, value)
        return value

    def describe(self) -> str:
        return f"faker({self.provider})"


def _make_safe(provider: str, value: Any) -> Any:
    """Rewrite a Faker value so it cannot collide with a real internet resource."""
    if not isinstance(value, str):
        return value

    if "email" in provider:
        match = _EMAIL_SPLIT.match(value)
        if match:
            return f"{match.group('local')}@{sanitise_domain(match.group('domain'))}"
        return value

    if provider in ("url", "uri"):
        scheme, _, rest = value.partition("://")
        host, slash, path = rest.partition("/")
        return f"{scheme}://{sanitise_domain(host)}{slash}{path}"

    if provider == "domain_word":
        return value  # a bare label carries no TLD, so it is already harmless

    return sanitise_domain(value)
