"""What a plugin looks like (design document section 44).

A plugin is a Python object with a manifest and a ``register`` method. That is
the whole protocol:

    class NetworkPackets:
        manifest = {
            "name": "Network Packets",
            "version": "1.0",
            "provides": {"generators": ["network_packet"]},
        }

        def register(self, host):
            host.add_generator("network_packet", NetworkPacketGenerator)

``host`` is a :class:`PluginHost`, and every method on it is a door into a
registry Cacophony already had. A plugin therefore cannot reach anything a
built-in generator could not - not because it is prevented from importing
Cacophony's internals (it can; it is installed Python code) but because there is
nothing to gain by doing so. The host exists to make the *declared* surface
obvious, and to refuse contributions the manifest did not mention.

**Every contribution is checked against the manifest.** ``add_generator`` for a
name the manifest did not declare is refused and recorded. Not a security
measure - a plugin is code the user chose to install, and the security decision
was made at ``pip install`` - but a correctness one: a manifest that has drifted
from its code produces a project that works on one machine and fails on another.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from .manifest import PluginError, PluginManifest

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

__all__ = ["Plugin", "PluginHost"]


@runtime_checkable
class Plugin(Protocol):
    """What Cacophony needs from a plugin."""

    #: A mapping, or a :class:`PluginManifest`.
    manifest: Any

    def register(self, host: PluginHost) -> None: ...


class PluginHost:
    """The doors a plugin may go through.

    One per plugin, so every contribution is attributable to the plugin that
    made it - which is what lets ``cacophony plugins`` say where a generator
    came from, and what lets a plugin be blamed by name when its manifest is
    wrong.
    """

    def __init__(self, manifest: PluginManifest, *, replace: bool = False) -> None:
        self.manifest = manifest
        #: Whether this plugin may take over a name that already exists. Off by
        #: default: a plugin silently replacing the built-in ``uuid`` generator
        #: would change every project on the machine.
        self.replace = replace

    # -- the eight categories --------------------------------------------------- #

    def add_generator(self, name: str, generator: type, *, aliases: tuple[str, ...] = ()) -> None:
        """Contribute a generation strategy (section 8)."""
        if not self._permitted("generators", name):
            return
        from ..generation.registry import REGISTRY

        self._guard("generators", name, name in REGISTRY.names())
        REGISTRY.register(name, generator, aliases=aliases, replace=self.replace)
        self._note("generators", name)

    def add_transform(self, name: str, operation: Callable[[Any, str | None], Any]) -> None:
        """Contribute a post-generation operation (section 105)."""
        if not self._permitted("transforms", name):
            return
        from ..transforms.operations import OPERATIONS

        self._guard("transforms", name, name in OPERATIONS)
        OPERATIONS[name] = operation
        self._note("transforms", name)

    def add_output(self, name: str, writer: type) -> None:
        """Contribute an output format (section 33)."""
        if not self._permitted("outputs", name):
            return
        from ..outputs import OUTPUT_FORMATS

        self._guard("outputs", name, name in OUTPUT_FORMATS)
        OUTPUT_FORMATS[name] = writer
        self._note("outputs", name)

    def add_validator(self, name: str, validator: type) -> None:
        """Contribute a validation category (section 57)."""
        if not self._permitted("validators", name):
            return
        from ..validation import extra_validators

        self._guard("validators", name, name in extra_validators())
        extra_validators()[name] = validator
        self._note("validators", name)

    def add_scenario(self, name: str, scenario: type) -> None:
        """Contribute a scenario behaviour (section 17)."""
        if not self._permitted("scenarios", name):
            return
        from ..scenarios import extra_scenarios

        self._guard("scenarios", name, name in extra_scenarios())
        extra_scenarios()[name] = scenario
        self._note("scenarios", name)

    def add_language_model(
        self, name: str, adapter: type, *, aliases: tuple[str, ...] = ()
    ) -> None:
        """Contribute a text-generation backend (section 43)."""
        self._add_provider("language_models", name, adapter, aliases)

    def add_image_provider(
        self, name: str, adapter: type, *, aliases: tuple[str, ...] = ()
    ) -> None:
        """Contribute an image backend (section 18)."""
        self._add_provider("images", name, adapter, aliases)

    def add_speech_provider(
        self, name: str, adapter: type, *, aliases: tuple[str, ...] = ()
    ) -> None:
        """Contribute a speech backend (section 20)."""
        self._add_provider("speech", name, adapter, aliases)

    # -- bookkeeping ------------------------------------------------------------- #

    def _add_provider(
        self, category: str, name: str, adapter: type, aliases: tuple[str, ...]
    ) -> None:
        if not self._permitted(category, name):
            return
        from ..providers.registry import PROVIDER_REGISTRY

        self._guard(category, name, name in PROVIDER_REGISTRY.adapters())
        PROVIDER_REGISTRY.register_adapter(name, adapter, aliases=aliases, replace=self.replace)
        self._note(category, name)

    def _permitted(self, category: str, name: str) -> bool:
        """Whether the manifest declared this contribution."""
        if self.manifest.declares(category, name):
            return True
        self.manifest.refused.append(f"{category}.{name}")
        return False

    def _guard(self, category: str, name: str, exists: bool) -> None:
        if exists and not self.replace:
            raise PluginError(
                f"{self.manifest.name}: '{name}' is already registered as a {category[:-1]}. "
                "A plugin that silently replaced a built-in would change every project on "
                "this machine; declare `replace: true` in the manifest if that is the intent."
            )

    def _note(self, category: str, name: str) -> None:
        self.manifest.registered.setdefault(category, []).append(name)
