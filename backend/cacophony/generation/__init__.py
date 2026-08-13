"""Generation layer: the generator registry, the built-in generators and the engine.

Importing this package registers every built-in generator.

``engine`` is deliberately *not* imported here. The engine depends on the
schema compiler, and the compiler resolves generators through this registry;
keeping the engine out of the package's eager imports means that cycle never
has to be discovered at import time. Import it explicitly::

    from cacophony.generation.engine import GenerationEngine
"""

from . import generators as generators
from .recommend import Recommendation, recommend_generator
from .registry import REGISTRY, GeneratorRegistry, register_generator

__all__ = [
    "REGISTRY",
    "GeneratorRegistry",
    "Recommendation",
    "recommend_generator",
    "register_generator",
]
