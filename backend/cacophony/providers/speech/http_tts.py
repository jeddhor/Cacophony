"""HTTP text-to-speech adapters (design document sections 20, 85).

Section 20 names Piper and XTTS-class engines and then warns, in the same
breath, against coupling the core to any one of them. Two adapters honour that
by covering the two shapes almost every local TTS server exposes:

``piper``
    ``POST /synthesize`` with ``{"text": ...}``, returning WAV bytes - the
    interface of `piper1-gpl`'s web server and of the wrappers that copied it.

``openai_speech``
    ``POST /v1/audio/speech`` with ``{model, input, voice}``, the interface
    that has become the common denominator - spoken by openedai-speech,
    LocalAI, Kokoro-FastAPI and others, as well as by hosted services.

Both return audio bytes and a media type. Neither knows anything about
Cacophony's records, and Cacophony knows nothing about their internals: what
crosses the boundary is text, a voice name and audio.

One lesson from running these against real servers rather than documentation:
**the declared content type cannot be trusted.** Piper's Flask server labels
its WAV responses ``text/html``, which is Flask's default for a bare byte
response. Taking that at face value would file every recording as ``.bin``, so
the payload is sniffed and the header believed only when it says something
plausible.

Verified against piper1-gpl.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...assets.audio import duration_of, is_wav
from ...core.errors import ProviderError
from ...core.interfaces import Capability
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

        # What the bytes are beats what the header claims. Piper's server sends
        # WAV under `text/html`; believing it would save every recording as a
        # `.bin` nothing will play.
        wav = is_wav(data)
        if wav:
            resolved = "audio/wav"
        elif media_type and media_type.startswith("audio/"):
            resolved = media_type
        else:
            resolved = "audio/wav"

        # Duration is worth having in the record - a call-centre dataset with
        # no durations is missing the column everyone filters on - and it can
        # only be read out of formats we can parse.
        return AudioResult(
            data=data,
            media_type=resolved,
            duration_seconds=duration_of(data) if wav else None,
            sample_rate=self.sample_rate,
            voice=request.voice or self.default_voice,
            provider=self.id,
            raw={"bytes": len(data), "declared_media_type": media_type},
        )

    def capabilities(self) -> list[Capability]:
        return [Capability("text_to_speech")]


@register_adapter("piper", aliases=("piper_http",))
class PiperProvider(_HttpSpeechProvider):
    """Piper's web server: ``POST /synthesize``, receive WAV.

    A Piper server hosts one voice, chosen when it starts, so ``voice`` is
    passed through for the wrappers that accept it and is otherwise inert -
    which is why a schema that names voices should point each one at its own
    provider.
    """

    default_base_url = "http://localhost:5000"
    #: JSON, and it names the loaded voice - a better health probe than the
    #: HTML page at the root.
    health_path = "/info"
    synthesize_path = "/synthesize"

    async def synthesize(self, request: SpeechRequest) -> AudioResult:
        body: dict[str, Any] = {"text": request.text}
        voice = request.voice or self.default_voice
        if voice:
            body["voice"] = voice
        if request.speed:
            # Piper scales *length*, so it is the reciprocal of speed: 1.4 is
            # slower, not faster.
            body["length_scale"] = round(1.0 / max(0.1, request.speed), 4)
        body.update(request.options)

        data, media_type, _ms = await self.request_bytes(
            "POST", self.synthesize_path, json_body=body
        )
        return self._result(data, media_type, request)

    def _health_details(self, payload: Any) -> dict[str, Any]:
        voice = payload.get("voice") if isinstance(payload, dict) else None
        if isinstance(voice, dict):
            return {
                "voice": str(voice.get("name", "")),
                "language": str(voice.get("language", "")),
                "speakers": str(voice.get("num_speakers", "")),
            }
        return {}


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
