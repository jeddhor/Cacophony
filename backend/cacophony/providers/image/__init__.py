"""Image adapters (design document section 18).

Importing this package registers every built-in image adapter with
:data:`cacophony.providers.registry.PROVIDER_REGISTRY`.

``InvokeAIProvider``
    Section 18's target. Submits a workflow graph to a local InvokeAI server -
    a default text-to-image graph, or one the project supplies - with model
    selection, dimensions, seed, steps, guidance and negative prompts.

``ProceduralImageProvider``
    Deterministic placeholder imagery drawn in-process. Not a diffusion model
    and never presented as one; it exists so that an image field can be
    designed, previewed, tested and exported by someone with no GPU.
"""

from .invokeai import InvokeAIProvider
from .procedural import ProceduralImageProvider

__all__ = ["InvokeAIProvider", "ProceduralImageProvider"]
