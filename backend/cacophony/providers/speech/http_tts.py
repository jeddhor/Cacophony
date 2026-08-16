"""HTTP text-to-speech adapters (design document sections 20, 85).

Section 20 names Piper and XTTS-class engines and then warns, in the same
breath, against coupling the core to any one of them. Two adapters honour that
by covering the two shapes almost every local TTS server exposes:

``piper``
    Piper's HTTP mode: POST the text, receive WAV bytes. Also fits the many
    small wrappers that copied the same interface.

``openai_speech``
    ``POST /v1/audio/speech`` with ``{model, input, voice}``, the interface
    that has become the common denominator - spoken by openedai-speech,
    LocalAI, Kokoro-FastAPI and others, as well as by hosted services.

Both return audio bytes and a media type. Neither knows anything about
Cacophony's records, and Cacophony knows nothing about their internals: what
crosses the boundary is text, a voice name and audio.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...assets.audio import duration_of, is_wav
from ...core.errors import ProviderError
from ...core.interfaces import Capability, HealthStatus
from ..base import AudioResult, SpeechProvider, SpeechRequest
from ..http import HttpProvider
from ..registry import register_adapter

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..secrets import SecretResolver

__all__ = ["OpenAISpeechProvider", "PiperProvider"]


class _HttpSpeechProvider(HttpProvider, SpeechProvider):
    """Shared plumbing for a speech backend reached over HTTP."""

    kind = "speech"

    def __init__(
        self,
        provider_id: str,
        config: dict[str, Any] | None = None,
        *,
        secrets: SecretResolver | None = None,
    ) -> None:
        super().__init__(provider_id, config, secrets=secrets)
        self.default_voice = self.config.get("voice")
        self.sample_rate = self.config.get("sample_rate")

    def _result(self, data: bytes, media_type: str, request: SpeechRequest) -> AudioResult:
        if not data:
            raise ProviderError(f"provider '{self.id}' returned no audio for a non-empty request")

        # Duration is worth having in the record - a call-centre dataset with
        # no durations is missing the column everyone filters on - and it can
        # only be read out of formats we can parse.
        duration = duration_of(data) if is_wav(data) else None
        return AudioResult(
            data=data,
            media_type=media_type or "audio/wav",
            duration_seconds=duration,
            sample_rate=self.sample_rate,
            voice=request.voice or self.default_voice,
            provider=self.id,
            raw={"bytes": len(data)},
        )

    def capabilities(self) -> list[Capability]:
        return [Capability("text_to_speech")]


@register_adapter("piper", aliases=("piper_http",))
class PiperProvider(_HttpSpeechProvider):
    """Piper's HTTP server: POST text, receive WAV."""

    default_base_url = "http://localhost:5000"
    health_path = "/"

    async def synthesize(self, request: SpeechRequest) -> AudioResult:
        body: dict[str, Any] = {"text": request.text}
        voice = request.voice or self.default_voice
        if voice:
            body["voice"] = voice
        if request.speed:
            body["length_scale"] = round(1.0 / max(0.1, request.speed), 4)
        body.update(request.options)

        data, media_type, _ms = await self.request_bytes("POST", "/", json_body=body)
        return self._result(data, media_type, request)

    async def health_check(self) -> HealthStatus:
        # Piper's root serves a page, not JSON, so the JSON health check the
        # base class uses would report a working server as broken.
        try:
            _data, _media_type, elapsed = await self.request_bytes(
                "GET", self.health_path, limit_concurrency=False
            )
        except ProviderError as exc:
            return HealthStatus.down(str(exc))
        return HealthStatus.up(f"{self.id} is reachable", latency_ms=round(elapsed, 2))


@register_adapter("openai_speech", aliases=("openai_tts", "speech_api"))
class OpenAISpeechProvider(_HttpSpeechProvider):
    """``POST /v1/audio/speech``, the common local-TTS interface."""

    default_base_url = "http://localhost:8000"
    health_path = "/v1/models"

    def __init__(
        self,
        provider_id: str,
        config: dict[str, Any] | None = None,
        *,
        secrets: SecretResolver | None = None,
    ) -> None:
        super().__init__(provider_id, config, secrets=secrets)
        self.response_format = str(self.config.get("format") or "wav")

    async def synthesize(self, request: SpeechRequest) -> AudioResult:
        body: dict[str, Any] = {
            "model": self.model or "tts-1",
            "input": request.text,
            "voice": request.voice or self.default_voice or "alloy",
            "response_format": self.response_format,
        }
        if request.speed:
            body["speed"] = request.speed
        body.update(request.options)

        data, media_type, _ms = await self.request_bytes("POST", "/v1/audio/speech", json_body=body)
        return self._result(data, media_type, request)
