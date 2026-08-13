"""Provider interfaces (design document sections 10, 18, 20, 43 and 97).

Section 111 requires that the architectural interfaces for image, speech,
scenario and plugin providers already exist, even where their implementations
are initially empty, so that later multimodal work extends the platform rather
than forcing a rewrite. These are those interfaces.

The request and result types matter more than the methods. They are what
downstream code - the prompt compiler, the cache, the provenance recorder, the
run inspector - is written against, so getting them roughly right now is what
buys the extensibility.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..core.interfaces import Capability, Provider

__all__ = [
    "AudioResult",
    "GenerationRequest",
    "GenerationResult",
    "ImageProvider",
    "ImageRequest",
    "ImageResult",
    "LanguageModelProvider",
    "ModelInfo",
    "SpeechProvider",
    "SpeechRequest",
]


# --------------------------------------------------------------------------- #
# Language models (section 10)
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class ModelInfo:
    """A model a provider can serve."""

    name: str
    family: str | None = None
    parameter_size: str | None = None
    quantization: str | None = None
    context_length: int | None = None
    digest: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class GenerationRequest:
    """One text-generation call.

    ``json_schema`` carries the structured-output contract of section 13.
    Providers that can enforce it natively should; those that cannot leave the
    enforcement to the parsing and repair stages.
    """

    prompt: str
    model: str | None = None
    system: str | None = None
    max_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    stop: list[str] = field(default_factory=list)
    seed: int | None = None
    json_schema: dict[str, Any] | None = None
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class GenerationResult:
    """What came back, plus everything reproducibility needs (section 4)."""

    text: str
    model: str | None = None
    provider: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    duration_ms: float | None = None
    finish_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class LanguageModelProvider(Provider):
    """A text-generation backend (section 10)."""

    kind = "language_model"

    @abstractmethod
    def list_models(self) -> list[ModelInfo]:
        raise NotImplementedError

    @abstractmethod
    async def generate(self, request: GenerationRequest) -> GenerationResult:
        raise NotImplementedError

    def capabilities(self) -> list[Capability]:
        return [Capability("text_generation")]


# --------------------------------------------------------------------------- #
# Images (section 18)
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class ImageRequest:
    prompt: str
    width: int = 512
    height: int = 512
    seed: int | None = None
    steps: int | None = None
    guidance: float | None = None
    negative_prompt: str | None = None
    model: str | None = None
    workflow: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ImageResult:
    """A generated image plus the provenance block of section 19."""

    data: bytes | None = None
    path: str | None = None
    width: int = 0
    height: int = 0
    media_type: str = "image/png"
    provider: str | None = None
    workflow: str | None = None
    seed: int | None = None
    prompt_hash: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class ImageProvider(Provider):
    """An image-generation backend, initially InvokeAI (section 18)."""

    kind = "image"

    @abstractmethod
    async def generate(self, request: ImageRequest) -> ImageResult:
        raise NotImplementedError

    def capabilities(self) -> list[Capability]:
        return [Capability("text_to_image")]


# --------------------------------------------------------------------------- #
# Speech (section 20)
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class SpeechRequest:
    text: str
    voice: str | None = None
    language: str | None = None
    speed: float | None = None
    sample_rate: int | None = None
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AudioResult:
    data: bytes | None = None
    path: str | None = None
    media_type: str = "audio/wav"
    duration_seconds: float | None = None
    sample_rate: int | None = None
    voice: str | None = None
    provider: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class SpeechProvider(Provider):
    """A text-to-speech backend (section 20).

    Section 20 is explicit that the core architecture must not be tightly
    coupled to any single TTS engine, which is why this interface knows about
    text, a voice and options - and nothing about Piper or XTTS.
    """

    kind = "speech"

    @abstractmethod
    async def synthesize(self, request: SpeechRequest) -> AudioResult:
        raise NotImplementedError

    def capabilities(self) -> list[Capability]:
        return [Capability("text_to_speech")]
