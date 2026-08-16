"""Provider layer (design document sections 10, 18, 20, 43, 63, 76 and 85).

Importing this package registers every built-in adapter.
"""

from . import image as image
from . import llm as llm
from . import speech as speech
from .base import (
    AudioResult,
    GenerationRequest,
    GenerationResult,
    ImageProvider,
    ImageRequest,
    ImageResult,
    LanguageModelProvider,
    ModelInfo,
    SpeechProvider,
    SpeechRequest,
)
from .cache import CacheMode, CacheStats, GenerationCache, cache_key
from .http import HttpProvider
from .registry import PROVIDER_REGISTRY, ProviderRegistry, register_adapter
from .secrets import SecretResolver, redact, secret_env_var

__all__ = [
    "PROVIDER_REGISTRY",
    "AudioResult",
    "CacheMode",
    "CacheStats",
    "GenerationCache",
    "GenerationRequest",
    "GenerationResult",
    "HttpProvider",
    "ImageProvider",
    "ImageRequest",
    "ImageResult",
    "LanguageModelProvider",
    "ModelInfo",
    "ProviderRegistry",
    "SecretResolver",
    "SpeechProvider",
    "SpeechRequest",
    "cache_key",
    "redact",
    "register_adapter",
    "secret_env_var",
]
