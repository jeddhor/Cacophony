"""The provider registry (design document sections 43 and 85).

Providers advertise their capabilities and are addressed by URI. Cacophony
never owns models; it only knows how to talk to something that does.

Adapters register themselves here by name. The registry is populated with
concrete adapters - ``ollama``, ``llamacpp``, ``openai_compatible``,
``invokeai``, ``piper`` - in the provider phase; today it is the empty seam
they will slot into.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..core.errors import ProviderNotFoundError
from ..core.interfaces import Provider

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..schema.models import ProviderSpec

__all__ = ["PROVIDER_REGISTRY", "ProviderRegistry", "register_adapter"]


class ProviderRegistry:
    """Maps adapter names to provider classes, and provider ids to instances."""

    def __init__(self) -> None:
        self._adapters: dict[str, type[Provider]] = {}
        self._instances: dict[str, Provider] = {}

    def register_adapter(
        self, name: str, adapter: type[Provider], *, replace: bool = False
    ) -> None:
        if name in self._adapters and not replace:
            raise ValueError(f"An adapter named '{name}' is already registered.")
        self._adapters[name] = adapter

    def adapters(self) -> list[str]:
        return sorted(self._adapters)

    def create(self, spec: ProviderSpec) -> Provider:
        """Instantiate a provider from its schema declaration."""
        adapter = self._adapters.get(spec.adapter)
        if adapter is None:
            known = ", ".join(self.adapters()) or "<none registered yet>"
            raise ProviderNotFoundError(
                f"No adapter named '{spec.adapter}'. Available adapters: {known}"
            )
        config: dict[str, Any] = {
            "base_url": spec.base_url,
            "model": spec.model,
            "secret": spec.secret,
            "concurrency": spec.concurrency,
            "timeout_seconds": spec.timeout_seconds,
            **spec.options,
        }
        instance = adapter(spec.id, config)
        self._instances[spec.id] = instance
        return instance

    def get(self, provider_id: str) -> Provider:
        try:
            return self._instances[provider_id]
        except KeyError as exc:
            known = ", ".join(sorted(self._instances)) or "<none configured>"
            raise ProviderNotFoundError(
                f"No provider '{provider_id}' has been created. Configured: {known}"
            ) from exc

    def describe(self) -> list[dict[str, Any]]:
        return [instance.describe() for instance in self._instances.values()]

    def clear(self) -> None:
        self._instances.clear()

    def __contains__(self, provider_id: str) -> bool:
        return provider_id in self._instances


PROVIDER_REGISTRY = ProviderRegistry()
"""The process-wide provider registry."""


def register_adapter(name: str, *, registry: ProviderRegistry | None = None) -> Any:
    """Class decorator registering a provider adapter under ``name``."""

    def decorator(adapter: type[Provider]) -> type[Provider]:
        (registry or PROVIDER_REGISTRY).register_adapter(name, adapter)
        return adapter

    return decorator
