"""The plugin architecture (design document section 44).

    Plugin categories: GeneratorPlugin, ValidatorPlugin, TransformPlugin,
    OutputPlugin, LanguageModelPlugin, ImagePlugin, SpeechPlugin,
    ScenarioPlugin.

    Plugins should provide manifests describing their capabilities.

All eight categories reach a registry that already existed - the generator
registry since phase one, the providers since phase two, the output formats since
phase one, the transform operations since phase twelve. A plugin is a door into
an extension point, not a new mechanism beside one.

**Discovery is by installed entry point, never from a project directory**, and
that is the decision the phase turns on. A project file is something people
share; if opening one could load its own Python, every other safety property in
the platform would be decoration. The trust decision belongs to a person running
``pip install``, not to a program opening a file.

    [project.entry-points."cacophony.plugins"]
    network_packets = "my_package:NetworkPackets"

A project depends on a plugin by requiring it, which fails loudly when absent::

    requires:
      plugins: [network_packets]
"""

from __future__ import annotations

from .manifest import CATEGORIES, PluginError, PluginManifest
from .protocol import Plugin, PluginHost
from .registry import (
    ENTRY_POINT_GROUP,
    REGISTRY,
    PluginRegistry,
    check_requirements,
    load_plugins,
    loaded_plugins,
)

__all__ = [
    "CATEGORIES",
    "ENTRY_POINT_GROUP",
    "REGISTRY",
    "Plugin",
    "PluginError",
    "PluginHost",
    "PluginManifest",
    "PluginRegistry",
    "check_requirements",
    "load_plugins",
    "loaded_plugins",
]
