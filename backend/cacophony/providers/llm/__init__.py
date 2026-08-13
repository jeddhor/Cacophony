"""Language-model adapters (design document section 10).

Importing this package registers every built-in language-model adapter with
:data:`cacophony.providers.registry.PROVIDER_REGISTRY`.

``OllamaProvider``
    Connect to an Ollama server.

``LlamaCppProvider``
    Connect to llama.cpp's native HTTP server.

``OpenAICompatibleProvider``
    Any server exposing a standard chat/completions interface, which
    indirectly supports many inference systems without coupling Cacophony to
    any of them.

``MockLanguageModelProvider``
    An in-process model used by the integration tests (section 88), and useful
    for rehearsing a run's shape before pointing it at real hardware.
"""

from .llamacpp import LlamaCppProvider
from .mock import MockLanguageModelProvider
from .ollama import OllamaProvider
from .openai_compat import OpenAICompatibleProvider

__all__ = [
    "LlamaCppProvider",
    "MockLanguageModelProvider",
    "OllamaProvider",
    "OpenAICompatibleProvider",
]
