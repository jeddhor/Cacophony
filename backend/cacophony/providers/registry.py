"""The provider registry (design document sections 43 and 85).

Providers advertise their capabilities and are addressed by URI. Cacophony
never owns models; it only knows how to talk to something that does.

Adapters register themselves by name. Instances are created per project from
:class:`~cacophony.schema.models.ProviderSpec` declarations, which is where the
logical secret id becomes a real credential (section 63).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..core.errors import ProviderNotFoundError
from ..core.interfaces import Provider
from .secrets import DEFAULT_RESOLVER, SecretResolver

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable

    from ..schema.models import ProjectSpec, ProviderSpec

__all__ = ["PROVIDER_REGISTRY", "ProviderRegistry", "register_adapter"]


class ProviderRegistry:
    """Maps adapter names to provider classes, and provider ids to instances."""

    def __init__(self) -> None:
        self._adapters: dict[str, type[Provider]] = {}
        self._aliases: dict[str, str] = {}
        self._instances: dict[str, Provider] = {}

    # -- registration ------------------------------------------------------- #

    def register_adapter(
        self,
        name: str,
        adapter: type[Provider],
        *,
        aliases: tuple[str, ...] = (),
        replace: bool = False,
    ) -> None:
        if name in self._adapters and not replace:
            raise ValueError(f"An adapter named '{name}' is already registered.")
        adapter.adapter_name = name  # type: ignore[attr-defined]
        self._adapters[name] = adapter
        for alias in aliases:
            if alias in self._aliases and not replace:
                raise ValueError(f"Adapter alias '{alias}' is already registered.")
            self._aliases[alias] = name

    def resolve_adapter(self, name: str) -> str:
        return self._aliases.get(name, name)

    def adapters(self) -> list[str]:
        return sorted(self._adapters)

    def adapter_aliases(self) -> dict[str, str]:
        return dict(self._aliases)

    def adapter_kinds(self) -> dict[str, str]:
        """What each adapter is for: a language model, images, or speech.

        Derived from the interface each one implements rather than declared
        beside it, so an adapter cannot be registered under the wrong heading -
        including one a plugin contributes (section 44).
        """
        from .base import ImageProvider, LanguageModelProvider, SpeechProvider

        kinds: dict[str, str] = {}
        for name, adapter in self._adapters.items():
            if issubclass(adapter, LanguageModelProvider):
                kinds[name] = "language_model"
            elif issubclass(adapter, ImageProvider):
                kinds[name] = "image"
            elif issubclass(adapter, SpeechProvider):
                kinds[name] = "speech"
            else:
                kinds[name] = "custom"
        return dict(sorted(kinds.items()))

    def adapter_class(self, name: str) -> type[Provider]:
        canonical = self.resolve_adapter(name)
        adapter = self._adapters.get(canonical)
        if adapter is None:
            known = ", ".join(self.adapters()) or "<none registered>"
            raise ProviderNotFoundError(f"No adapter named '{name}'. Available adapters: {known}")
        return adapter

    # -- instances ---------------------------------------------------------- #

    def create(self, spec: ProviderSpec, *, secrets: SecretResolver | None = None) -> Provider:
        """Instantiate a provider from its schema declaration."""
        adapter = self.adapter_class(spec.adapter)
        config: dict[str, Any] = {
            "base_url": spec.base_url,
            "model": spec.model,
            "secret": spec.secret,
            "concurrency": spec.concurrency,
            "timeout_seconds": spec.timeout_seconds,
            **spec.options,
        }
        try:
            instance = adapter(spec.id, config, secrets=secrets or DEFAULT_RESOLVER)  # type: ignore[call-arg]
        except TypeError:
            # Adapters that do not take a secret resolver (a plugin, or a test
            # double) still satisfy the base Provider signature.
            instance = adapter(spec.id, config)
        self._instances[spec.id] = instance
        return instance

    def create_all(
        self, project: ProjectSpec, *, secrets: SecretResolver | None = None
    ) -> dict[str, Provider]:
        """Instantiate every provider a project declares."""
        return {
            provider_id: self.create(spec, secrets=secrets)
            for provider_id, spec in project.providers.items()
        }

    def get(self, provider_id: str) -> Provider:
        try:
            return self._instances[provider_id]
        except KeyError as exc:
            known = ", ".join(sorted(self._instances)) or "<none configured>"
            raise ProviderNotFoundError(
                f"No provider '{provider_id}' has been created. Configured: {known}"
            ) from exc

    def add(self, provider: Provider) -> Provider:
        """Register an already-constructed provider, chiefly for tests."""
        self._instances[provider.id] = provider
        return provider

    def instances(self) -> Iterable[Provider]:
        return list(self._instances.values())

    def describe(self) -> list[dict[str, Any]]:
        return [instance.describe() for instance in self._instances.values()]

    async def aclose(self) -> None:
        """Release every instance's connections."""
        for instance in self._instances.values():
            closer = getattr(instance, "aclose", None)
            if closer is not None:
                await closer()

    def clear(self) -> None:
        self._instances.clear()

    def __contains__(self, provider_id: str) -> bool:
        return provider_id in self._instances

    def __len__(self) -> int:
        return len(self._instances)


PROVIDER_REGISTRY = ProviderRegistry()
"""The process-wide provider registry."""


def register_adapter(
    name: str,
    *,
    aliases: tuple[str, ...] = (),
    registry: ProviderRegistry | None = None,
) -> Any:
    """Class decorator registering a provider adapter under ``name``."""

    def decorator(adapter: type[Provider]) -> type[Provider]:
        (registry or PROVIDER_REGISTRY).register_adapter(name, adapter, aliases=aliases)
        return adapter

    return decorator
