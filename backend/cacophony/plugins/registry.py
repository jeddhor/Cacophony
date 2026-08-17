"""Finding and loading plugins (design document section 44).

**Entry points only. Never a project directory. This is the decision the whole
phase turns on.**

A project file is something people share. It arrives by email, in a Git
repository, inside a ``.cacophony`` bundle. If Cacophony loaded Python from a
``plugins/`` directory beside a schema, then opening a schema somebody sent you
would be equivalent to running their code - and every other safety property in
the platform would be decoration. The expression evaluator's allow-list, the
bundle importer's refusal of path traversal, the linter's careful messages: all
of it pointless, because the file could simply ask for a shell.

So plugins are discovered from installed **entry points**:

    [project.entry-points."cacophony.plugins"]
    network_packets = "my_package:NetworkPackets"

which means somebody ran ``pip install`` deliberately. That is where the trust
decision belongs: with a person, at install time, about a package - not with a
program, at open time, about a file.

A project may *require* a plugin by name:

    requires:
      plugins:
        - network_packets

which fails loudly when it is absent, naming what to install. That is how a
project depends on code without carrying it.

**Loading is never fatal to a run.** A plugin that raises on import is recorded
with its error and skipped, because a broken plugin somebody installed last month
should not stop today's generation of a project that does not use it. A project
that *requires* the plugin does stop - immediately, at compile time, with the
name.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..core.errors import SchemaError
from .manifest import PluginError, PluginManifest
from .protocol import PluginHost

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable, Sequence

__all__ = [
    "ENTRY_POINT_GROUP",
    "PluginRegistry",
    "check_requirements",
    "load_plugins",
    "loaded_plugins",
]

#: Where an installed package declares itself.
ENTRY_POINT_GROUP = "cacophony.plugins"

#: Set to any of these to skip plugin loading entirely - for a run that must be
#: reproducible against the built-ins alone, or for bisecting a plugin problem.
_DISABLE = ("1", "true", "yes", "on")


@dataclass(slots=True)
class PluginRegistry:
    """Every plugin found, whether or not it loaded."""

    manifests: list[PluginManifest] = field(default_factory=list)
    #: True once discovery has run, so it runs once per process.
    loaded: bool = False
    disabled: bool = False

    def by_name(self, name: str) -> PluginManifest | None:
        for manifest in self.manifests:
            if manifest.name == name:
                return manifest
        return None

    def names(self) -> list[str]:
        return sorted(manifest.name for manifest in self.manifests)

    @property
    def working(self) -> list[PluginManifest]:
        return [manifest for manifest in self.manifests if manifest.ok]

    @property
    def broken(self) -> list[PluginManifest]:
        return [manifest for manifest in self.manifests if not manifest.ok]

    def contributions(self) -> dict[str, dict[str, str]]:
        """What each category gained, and from which plugin."""
        found: dict[str, dict[str, str]] = {}
        for manifest in self.manifests:
            for category, names in manifest.registered.items():
                for name in names:
                    found.setdefault(category, {})[name] = manifest.name
        return found

    def describe(self) -> dict[str, Any]:
        return {
            "loaded": self.loaded,
            "disabled": self.disabled,
            "entry_point_group": ENTRY_POINT_GROUP,
            "plugins": [manifest.to_dict() for manifest in self.manifests],
            "contributions": self.contributions(),
        }


#: The process-wide registry. One per process because the registries plugins
#: contribute to are process-wide too - registering a generator twice would
#: raise, and a second load pass has nothing to add.
REGISTRY = PluginRegistry()


def loaded_plugins() -> PluginRegistry:
    """The registry, loading it on first use."""
    if not REGISTRY.loaded:
        load_plugins()
    return REGISTRY


def load_plugins(*, force: bool = False, entries: Iterable[Any] | None = None) -> PluginRegistry:
    """Discover and register every installed plugin.

    ``entries`` exists for tests, which need to load a plugin that is not
    installed. Nothing in the product passes it: production discovery is
    entry points and only entry points.
    """
    if REGISTRY.loaded and not force:
        return REGISTRY

    REGISTRY.manifests = []
    REGISTRY.loaded = True
    REGISTRY.disabled = os.environ.get("CACOPHONY_NO_PLUGINS", "").lower() in _DISABLE
    if REGISTRY.disabled:
        return REGISTRY

    for entry in entries if entries is not None else _entry_points():
        manifest = _load_one(entry)
        if manifest is not None:
            REGISTRY.manifests.append(manifest)
    return REGISTRY


def _entry_points() -> list[Any]:
    from importlib.metadata import entry_points

    try:
        return list(entry_points(group=ENTRY_POINT_GROUP))
    except Exception:  # pragma: no cover - a broken metadata directory
        return []


def _load_one(entry: Any) -> PluginManifest | None:
    """Load one plugin, recording rather than raising on failure."""
    name = getattr(entry, "name", "<unnamed>")
    source = f"{ENTRY_POINT_GROUP}:{name}"

    try:
        factory = entry.load()
    except Exception as exc:
        return PluginManifest(
            name=name,
            source=source,
            provides={},
            error=f"could not import: {type(exc).__name__}: {exc}",
        )

    try:
        plugin = factory() if isinstance(factory, type) else factory
        manifest = _manifest_of(plugin, source=source)
    except PluginError as exc:
        return PluginManifest(name=name, source=source, provides={}, error=str(exc))
    except Exception as exc:
        return PluginManifest(
            name=name,
            source=source,
            provides={},
            error=f"could not read its manifest: {type(exc).__name__}: {exc}",
        )

    host = PluginHost(manifest, replace=bool(getattr(plugin, "replace", False)))
    try:
        plugin.register(host)
    except Exception as exc:
        manifest.error = f"register() failed: {type(exc).__name__}: {exc}"
        return manifest

    # What it declared and did not deliver. Reported rather than raised: the
    # plugin may be half-useful, and a project that needs the missing half will
    # fail on its own with a clearer message.
    for category, names in manifest.provides.items():
        delivered = set(manifest.registered.get(category, []))
        manifest.missing.extend(f"{category}.{item}" for item in names if item not in delivered)
    return manifest


def _manifest_of(plugin: Any, *, source: str) -> PluginManifest:
    raw = getattr(plugin, "manifest", None)
    if raw is None:
        raise PluginError(
            f"{source}: a plugin needs a 'manifest' (section 44). Give it a mapping with "
            "'name', 'version' and 'provides'."
        )
    if isinstance(raw, PluginManifest):
        raw.source = source
        return raw
    if callable(raw) and not isinstance(raw, dict):
        raw = raw()
    return PluginManifest.from_dict(raw, source=source)


def check_requirements(required: Sequence[str], *, registry: PluginRegistry | None = None) -> None:
    """Refuse to compile a project whose plugins are not installed.

    Loudly and early, naming what is missing. A project that quietly generated
    without the plugin it declared would produce a dataset missing whole fields,
    and the person reading it would have no way to know why.
    """
    if not required:
        return

    found = registry or loaded_plugins()
    if found.disabled:
        raise SchemaError(
            "this project requires plugins "
            f"({', '.join(required)}), but plugin loading is disabled by "
            "CACOPHONY_NO_PLUGINS."
        )

    available = set(found.names())
    missing = [name for name in required if name not in available]
    if missing:
        installed = ", ".join(sorted(available)) or "<none>"
        raise SchemaError(
            f"this project requires plugin(s) {', '.join(missing)}, which are not installed. "
            f"Installed: {installed}. Plugins are found through the "
            f"'{ENTRY_POINT_GROUP}' entry point, so install the package that provides them - "
            "Cacophony deliberately does not load code from a project directory."
        )

    faulty = [
        f"{name} ({manifest.error or 'manifest and code disagree'})"
        for name in required
        if (manifest := found.by_name(name)) is not None and not manifest.ok
    ]
    if faulty:
        raise SchemaError(
            f"this project requires plugin(s) that did not load cleanly: {', '.join(faulty)}. "
            "Run 'cacophony plugins' for the detail."
        )
