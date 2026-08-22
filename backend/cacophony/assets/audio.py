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
    "SOUND_KINDS",
    "duration_of",
    "encode_wav",
    "is_wav",
    "silence",
    "sound_like",
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


# --------------------------------------------------------------------------- #
# Section 22: the audio that is not a voice
# --------------------------------------------------------------------------- #

#: What ``sound_like`` can synthesise, and what each one is for. Section 22
#: names these; they are built from the same oscillators and noise as the
#: voice above, because a dataset of alarms should not need a GPU either.
SOUND_KINDS: dict[str, str] = {
    "alarm": "A two-tone siren, the pattern a klaxon or an alert makes.",
    "ambience": "Broadband room tone: the sound of a place with nothing happening.",
    "machine": "A periodic hum with harmonics - a pump, a fan, a conveyor.",
    "notification": "A short rising chime, the shape of a device asking for attention.",
    "beep": "One flat tone, the length of a button press.",
}


def _noise(index: int, seed: int) -> float:
    """A deterministic pseudo-random sample in [-1, 1].

    Derived from the index rather than drawn from a stream, for the same reason
    every other value in this program is: a sample must not depend on how many
    samples were produced before it (section 75).
    """
    mixed = (index * 6364136223846793005 + seed * 1442695040888963407) & 0xFFFFFFFFFFFFFFFF
    mixed ^= mixed >> 33
    mixed = (mixed * 0xFF51AFD7ED558CCD) & 0xFFFFFFFFFFFFFFFF
    mixed ^= mixed >> 33
    return (mixed / 0xFFFFFFFFFFFFFFFF) * 2.0 - 1.0


def _low_passed(index: int, seed: int, previous: float, amount: float) -> float:
    """One pole of low-pass filtering, which is what turns hiss into rumble."""
    return previous + amount * (_noise(index, seed) - previous)


def sound_like(
    kind: str,
    *,
    seconds: float = 2.0,
    seed: int = 0,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    level: float = 0.6,
    distortion: float = 0.0,
) -> bytes:
    """Synthesise one of section 22's non-speech sounds.

    Deterministic in ``seed``: the same seed produces the same bytes, so a
    record's audio is reproducible in the way every other value is. ``level``
    scales the result and ``distortion`` drives it into soft clipping, which is
    what a cheap microphone or a saturated line does to any of these.
    """
    if kind not in SOUND_KINDS:
        known = ", ".join(sorted(SOUND_KINDS))
        raise ValueError(f"unknown sound '{kind}'. Available: {known}")

    total = int(max(0.05, seconds) * sample_rate)
    level = max(0.0, min(1.0, level))
    samples: list[float] = []
    rumble = 0.0

    # A pitch derived from the seed, so two alarms are not the same alarm.
    base = 220.0 + (seed % 660)

    for index in range(total):
        moment = index / sample_rate
        fade = min(1.0, moment / 0.02, max(0.0, (total / sample_rate) - moment) / 0.02)

        if kind == "alarm":
            # Two tones, alternating twice a second: the interval is what makes
            # it read as a warning rather than as a note.
            high = (moment * 2.0) % 1.0 < 0.5
            pitch = base * (1.0 if high else 0.75)
            value = math.sin(2 * math.pi * pitch * moment)
            value += 0.3 * math.sin(2 * math.pi * pitch * 2 * moment)
        elif kind == "ambience":
            rumble = _low_passed(index, seed, rumble, 0.02)
            value = rumble * 3.0 + 0.05 * _noise(index, seed ^ 0x5EED)
        elif kind == "machine":
            # A fundamental with its harmonics, wobbling slightly - a motor
            # that never wobbles sounds like a sine wave, because it is one.
            wobble = 1.0 + 0.004 * math.sin(2 * math.pi * 0.7 * moment)
            pitch = (base / 4.0) * wobble
            value = (
                math.sin(2 * math.pi * pitch * moment)
                + 0.4 * math.sin(2 * math.pi * pitch * 2 * moment)
                + 0.2 * math.sin(2 * math.pi * pitch * 3 * moment)
            ) / 1.6
            rumble = _low_passed(index, seed, rumble, 0.05)
            value += 0.08 * rumble
        elif kind == "notification":
            # Two rising notes in the first third, then silence: the shape of
            # every device that has ever wanted something.
            step = 0 if moment < 0.12 else 1
            pitch = base * (1.0 if step == 0 else 1.5)
            decay = math.exp(-6.0 * (moment % 0.12))
            value = math.sin(2 * math.pi * pitch * moment) * decay
            if moment > 0.34:
                value = 0.0
        else:  # beep
            value = math.sin(2 * math.pi * base * moment)

        if distortion:
            drive = 1.0 + 9.0 * max(0.0, min(1.0, distortion))
            value = math.tanh(value * drive) / math.tanh(drive)

        samples.append(value * fade * level)

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
