"""Plugin manifests (design document section 44).

    Plugins should provide manifests describing their capabilities.

        name: My Custom Generator
        version: 1.0

        provides:
          generators:
            - network_packet_generator

**A manifest is a contract, checked in both directions.** A plugin that declares
one generator and registers three has the other two refused; a plugin that
declares a generator and registers none is reported as incomplete. Neither is
about hostile plugins - it is about a plugin whose manifest has drifted from its
code, which happens constantly and produces a project that works on one machine
and fails on another with no clue why.

The eight categories are section 44's, exactly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.errors import CacophonyError

__all__ = ["CATEGORIES", "PluginError", "PluginManifest"]


class PluginError(CacophonyError):
    """A plugin that cannot be loaded, or that broke its own manifest."""


#: Section 44's categories, mapped to the registry each one contributes to.
#:
#: Every registry already existed - the generator registry since phase one, the
#: provider registry since phase two, the output formats since phase one, the
#: transform operations since phase twelve. A plugin is therefore a way of
#: reaching an extension point that was always there, not a new mechanism
#: bolted alongside one.
CATEGORIES: dict[str, str] = {
    "generators": "cacophony.generation.registry.REGISTRY",
    "validators": "cacophony.validation.pipeline",
    "transforms": "cacophony.transforms.operations.OPERATIONS",
    "outputs": "cacophony.outputs.OUTPUT_FORMATS",
    "language_models": "cacophony.providers.registry.PROVIDER_REGISTRY",
    "images": "cacophony.providers.registry.PROVIDER_REGISTRY",
    "speech": "cacophony.providers.registry.PROVIDER_REGISTRY",
    "scenarios": "cacophony.scenarios",
}


@dataclass(slots=True)
class PluginManifest:
    """What a plugin says it provides."""

    name: str
    version: str = "0"
    description: str = ""
    homepage: str = ""
    author: str = ""
    #: Category to the names it contributes, exactly as section 44's example.
    provides: dict[str, list[str]] = field(default_factory=dict)
    #: Where it was loaded from, for the listing. Set by the loader.
    source: str = ""
    #: Populated after loading: what it actually registered, and what it did not.
    registered: dict[str, list[str]] = field(default_factory=dict)
    refused: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and not self.refused and not self.missing

    @property
    def total_declared(self) -> int:
        return sum(len(names) for names in self.provides.values())

    def declares(self, category: str, name: str) -> bool:
        return name in self.provides.get(category, [])

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "homepage": self.homepage,
            "author": self.author,
            "source": self.source,
            "provides": {key: list(value) for key, value in sorted(self.provides.items())},
            "registered": {key: list(value) for key, value in sorted(self.registered.items())},
            "refused": list(self.refused),
            "missing": list(self.missing),
            "error": self.error,
            "ok": self.ok,
        }

    @classmethod
    def from_dict(cls, data: Any, *, source: str = "") -> PluginManifest:
        if not isinstance(data, dict):
            raise PluginError(f"{source or 'a plugin'}: the manifest must be a mapping")

        name = str(data.get("name") or "").strip()
        if not name:
            raise PluginError(f"{source or 'a plugin'}: the manifest needs a 'name'")

        raw = data.get("provides") or {}
        if not isinstance(raw, dict):
            raise PluginError(f"{name}: 'provides' must be a mapping of category to names")

        unknown = set(raw) - set(CATEGORIES)
        if unknown:
            raise PluginError(
                f"{name}: unknown plugin categor{'y' if len(unknown) == 1 else 'ies'} "
                f"{', '.join(sorted(unknown))}. Section 44's categories are: "
                f"{', '.join(sorted(CATEGORIES))}"
            )

        provides: dict[str, list[str]] = {}
        for category, names in raw.items():
            if isinstance(names, str):
                names = [names]
            if not isinstance(names, list):
                raise PluginError(f"{name}: provides.{category} must be a list of names")
            provides[str(category)] = [str(item) for item in names]

        if not provides or not any(provides.values()):
            raise PluginError(
                f"{name}: the manifest declares nothing. A plugin that provides nothing "
                "cannot be checked against what it registers."
            )

        # Version is stringified rather than parsed: section 44's example writes
        # `1.0`, which YAML reads as a float, and refusing that would be
        # pedantry about the document's own example.
        return cls(
            name=name,
            version=str(data.get("version") or "0"),
            description=str(data.get("description") or "").strip(),
            homepage=str(data.get("homepage") or "").strip(),
            author=str(data.get("author") or "").strip(),
            provides=provides,
            source=source,
        )
