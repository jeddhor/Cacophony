"""Speech adapters (design document section 20).

Importing this package registers every built-in speech adapter with
:data:`cacophony.providers.registry.PROVIDER_REGISTRY`.

Section 20 requires that no single engine be baked into the core, so the two
HTTP adapters cover the two shapes local TTS servers actually expose rather
than one engine each.

``PiperProvider``
    Piper's HTTP mode: POST text, receive WAV.

``OpenAISpeechProvider``
    ``POST /v1/audio/speech``, spoken by openedai-speech, LocalAI,
    Kokoro-FastAPI and others.

``ProceduralSpeechProvider``
    Voice-shaped audio synthesised in-process. Not speech; it exists so that
    an audio field can be designed, previewed, tested and exported without a
    TTS engine.
"""

from .http_tts import OpenAISpeechProvider, PiperProvider
from .procedural import ProceduralSpeechProvider

__all__ = ["OpenAISpeechProvider", "PiperProvider", "ProceduralSpeechProvider"]
