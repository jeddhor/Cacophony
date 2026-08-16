"""WAV writing and simple synthesis (design document sections 20, 21, 22).

Section 20 asks for a generic speech provider and warns against coupling the
core to any single TTS engine. That cuts both ways: the core also should not
depend on one to be *testable*. A synthetic call-centre dataset has a shape -
a conversation, two speakers, alternating turns, a duration, an aligned
transcript - and all of that can be exercised with audio Cacophony synthesises
itself.

So this module writes WAV from the standard library and can synthesise a
voice-like signal: a fundamental with a couple of harmonics, shaped by an
envelope, at a pitch derived from a seed. It is not speech. It is the right
length, the right format, deterministic, and distinguishable between speakers -
which is what a pipeline test needs, and what a real TTS provider replaces.

Section 22's non-speech audio (alarms, machine noise, ambience) uses the same
primitives.
"""

from __future__ import annotations

import io
import math
import struct
import wave

__all__ = [
    "DEFAULT_SAMPLE_RATE",
    "duration_of",
    "encode_wav",
    "is_wav",
    "silence",
    "speech_like",
]

#: 22.05 kHz mono: the rate small TTS engines emit, and half the bytes of 44.1.
DEFAULT_SAMPLE_RATE = 22_050

#: Roughly how long a person takes to say one character aloud.
_SECONDS_PER_CHARACTER = 0.06


def is_wav(data: bytes) -> bool:
    return data[:4] == b"RIFF" and data[8:12] == b"WAVE"


def encode_wav(
    samples: list[float],
    *,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    channels: int = 1,
) -> bytes:
    """Encode floats in [-1, 1] as 16-bit PCM WAV."""
    frames = bytearray()
    for sample in samples:
        clamped = -1.0 if sample < -1.0 else (1.0 if sample > 1.0 else sample)
        frames += struct.pack("<h", int(clamped * 32767))

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(bytes(frames))
    return buffer.getvalue()


def duration_of(data: bytes) -> float:
    """How long a WAV runs, in seconds."""
    with wave.open(io.BytesIO(data), "rb") as handle:
        rate = handle.getframerate()
        return handle.getnframes() / rate if rate else 0.0


def silence(seconds: float, *, sample_rate: int = DEFAULT_SAMPLE_RATE) -> bytes:
    return encode_wav([0.0] * int(max(0.0, seconds) * sample_rate), sample_rate=sample_rate)


def estimate_seconds(text: str, *, speed: float = 1.0) -> float:
    """How long this text would take to say.

    A crude reading-rate model, and deliberately so - the point is that a
    hundred-character line produces roughly six seconds of audio rather than
    the same fixed blip as a paragraph, so durations in a generated dataset
    correlate with the text they came from.
    """
    seconds = max(0.4, len(text) * _SECONDS_PER_CHARACTER)
    return seconds / max(0.1, speed)


def speech_like(
    text: str,
    *,
    seed: int = 0,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    speed: float = 1.0,
    pitch_hz: float | None = None,
) -> bytes:
    """Synthesise a voice-shaped signal for ``text``.

    Not speech, and never presented as speech: a fundamental plus two
    harmonics, amplitude-modulated at syllable rate and gated into words, at a
    pitch derived from ``seed`` so two speakers are audibly different. Length
    tracks the text.
    """
    seconds = estimate_seconds(text, speed=speed)
    total = int(seconds * sample_rate)
    if total <= 0:
        return silence(0.4, sample_rate=sample_rate)

    # 85-255 Hz spans the usual range of adult speaking pitch.
    fundamental = pitch_hz if pitch_hz else 85.0 + (seed % 170)
    syllable_rate = 4.2 * max(0.1, speed)

    samples: list[float] = []
    for index in range(total):
        moment = index / sample_rate
        # Word gating: brief silences, so it is not one continuous drone.
        gate = 1.0 if (moment * 1.7) % 1.0 < 0.82 else 0.0
        envelope = 0.5 + 0.5 * math.sin(2 * math.pi * syllable_rate * moment)
        # A gentle fade at both ends; a hard edge is an audible click.
        fade = min(1.0, moment / 0.05, max(0.0, seconds - moment) / 0.05)

        value = (
            math.sin(2 * math.pi * fundamental * moment)
            + 0.5 * math.sin(2 * math.pi * fundamental * 2 * moment)
            + 0.25 * math.sin(2 * math.pi * fundamental * 3 * moment)
        ) / 1.75
        samples.append(value * envelope * gate * fade * 0.6)

    return encode_wav(samples, sample_rate=sample_rate)


def concatenate(clips: list[bytes], *, gap_seconds: float = 0.35) -> bytes:
    """Join WAV clips with a pause between them (section 21's audio composer).

    Every clip must share a sample rate; the first one's wins, and a clip that
    disagrees is an error rather than something silently resampled into a
    chipmunk.
    """
    if not clips:
        return silence(0.0)

    rate = 0
    frames = bytearray()
    gap = b""
    for clip in clips:
        with wave.open(io.BytesIO(clip), "rb") as handle:
            if rate == 0:
                rate = handle.getframerate()
                gap = b"\x00\x00" * int(max(0.0, gap_seconds) * rate)
            elif handle.getframerate() != rate:
                raise ValueError(
                    f"cannot join audio at {handle.getframerate()} Hz with audio at {rate} Hz"
                )
            if frames:
                frames += gap
            frames += handle.readframes(handle.getnframes())

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate or DEFAULT_SAMPLE_RATE)
        handle.writeframes(bytes(frames))
    return buffer.getvalue()
