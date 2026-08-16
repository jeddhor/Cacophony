"""A procedural speech provider (design document sections 20, 21, 22).

Synthesises a voice-shaped signal rather than speech. It exists for the same
reason the procedural image provider does: an audio field changes the shape of
a project - durations, assets, transcripts, aligned metadata - and none of that
should require a TTS engine to develop or to test.

What it produces is deterministic, the right length for the text, and different
per voice, so a two-speaker call sounds like two speakers taking turns. It is
not intelligible and is never described as though it were: every result carries
``synthetic: True`` and a ``spoken_text`` field, so a dataset built with it
cannot be mistaken for a speech corpus.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from ...assets.audio import DEFAULT_SAMPLE_RATE, duration_of, silence, speech_like
from ...core.interfaces import Capability, HealthStatus
from ..base import AudioResult, SpeechProvider, SpeechRequest
from ..registry import register_adapter

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..secrets import SecretResolver

__all__ = ["ProceduralSpeechProvider"]

#: Named voices, so a schema can say ``voice: agent`` and mean something. The
#: number is a pitch offset in hertz; the range is roughly adult speaking pitch.
VOICES: dict[str, int] = {
    "agent": 118,
    "customer": 196,
    "narrator": 96,
    "operator": 142,
    "caller": 168,
}


@register_adapter("procedural_speech", aliases=("placeholder_speech", "tone"))
class ProceduralSpeechProvider(SpeechProvider):
    """Deterministic voice-shaped audio, synthesised locally."""

    def __init__(
        self,
        provider_id: str,
        config: dict[str, Any] | None = None,
        *,
        secrets: SecretResolver | None = None,
    ) -> None:
        super().__init__(provider_id, config)
        self.sample_rate = int(self.config.get("sample_rate", DEFAULT_SAMPLE_RATE))
        self.default_voice = str(self.config.get("voice") or "narrator")

    async def synthesize(self, request: SpeechRequest) -> AudioResult:
        text = request.text or ""
        if not text.strip():
            data = silence(0.4, sample_rate=self.sample_rate)
            return AudioResult(
                data=data,
                media_type="audio/wav",
                duration_seconds=duration_of(data),
                sample_rate=self.sample_rate,
                voice=request.voice or self.default_voice,
                provider=self.id,
                raw={"synthetic": True, "spoken_text": ""},
            )

        voice = request.voice or self.default_voice
        pitch = VOICES.get(voice.lower())
        if pitch is None:
            # An unknown voice still has to be stable and distinct, so derive
            # its pitch from the name rather than refusing it.
            digest = hashlib.blake2b(voice.encode("utf-8"), digest_size=2).digest()
            pitch = 85 + int.from_bytes(digest, "big") % 170

        data = speech_like(
            text,
            seed=pitch,
            sample_rate=self.sample_rate,
            speed=request.speed or 1.0,
            pitch_hz=float(pitch),
        )
        return AudioResult(
            data=data,
            media_type="audio/wav",
            duration_seconds=duration_of(data),
            sample_rate=self.sample_rate,
            voice=voice,
            provider=self.id,
            raw={"synthetic": True, "spoken_text": text, "pitch_hz": pitch},
        )

    async def health_check(self) -> HealthStatus:
        return HealthStatus.up(
            f"{self.id} synthesises placeholder audio in-process",
            latency_ms=0.0,
            details={"voices": ", ".join(sorted(VOICES)), "sample_rate": str(self.sample_rate)},
        )

    def capabilities(self) -> list[Capability]:
        return [Capability("text_to_speech"), Capability("placeholder")]

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "adapter": "procedural_speech", "voice": self.default_voice}
