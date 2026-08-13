"""Language-model adapters (design document section 10).

Planned for the provider phase, all implementing
:class:`cacophony.providers.base.LanguageModelProvider`:

``OllamaProvider``
    Connect to an Ollama server.

``LlamaCppProvider``
    Connect to llama.cpp's OpenAI-compatible or native HTTP server.

``OpenAICompatibleProvider``
    Any server exposing a standard chat/completions interface, which
    indirectly supports many inference systems without coupling Cacophony to
    any of them.
"""
