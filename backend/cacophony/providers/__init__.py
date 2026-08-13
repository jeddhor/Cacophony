"""Provider layer (design document sections 10, 18, 20, 43 and 85)."""

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
from .registry import PROVIDER_REGISTRY, ProviderRegistry, register_adapter

__all__ = [
    "PROVIDER_REGISTRY",
    "AudioResult",
    "GenerationRequest",
    "GenerationResult",
    "ImageProvider",
    "ImageRequest",
    "ImageResult",
    "LanguageModelProvider",
    "ModelInfo",
    "ProviderRegistry",
    "SpeechProvider",
    "SpeechRequest",
    "register_adapter",
]
