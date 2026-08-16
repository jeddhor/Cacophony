"""Provider contract tests (design document section 88).

    "Ensure Ollama, llama.cpp, InvokeAI and TTS adapters obey provider
     interfaces."

Two kinds of test live here, and the distinction is the point.

**Offline contract tests** run everywhere. They put a recording server behind
the adapter - responses captured from the real thing - and check that the
adapter builds the right request and reads the right answer out. These are the
ones CI runs.

**Live tests** run only when pointed at a server, via environment variables::

    CACOPHONY_TEST_INVOKEAI=http://10.1.0.90:9090 \\
    CACOPHONY_TEST_PIPER=http://10.1.0.91:5000 \\
    CACOPHONY_TEST_OLLAMA=http://10.1.0.90:11434 \\
        pytest tests/test_provider_contracts.py

They are skipped by default because a test suite that needs a GPU is a test
suite nobody runs. They are *written* because every defect these adapters had
was one only a real server could show: InvokeAI wants a model identifier where
the documentation reads like a name, and Piper serves WAV under a
``text/html`` content type. Neither was visible from the specification, and a
mock built from my own assumptions would have agreed with them.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import pytest

from cacophony.assets.audio import is_wav
from cacophony.assets.imaging import is_png
from cacophony.core.errors import ProviderError
from cacophony.providers.base import (
    ImageProvider,
    ImageRequest,
    SpeechProvider,
    SpeechRequest,
)
from cacophony.providers.image.invokeai import InvokeAIProvider
from cacophony.providers.image.procedural import ProceduralImageProvider
from cacophony.providers.speech.http_tts import OpenAISpeechProvider, PiperProvider
from cacophony.providers.speech.procedural import ProceduralSpeechProvider

# --------------------------------------------------------------------------- #
# Recorded responses
#
# Captured from InvokeAI 6.13.8 and piper1-gpl, trimmed to what the adapters
# actually read. Recording rather than inventing is what makes these tests
# worth having: an invented fixture agrees with whatever the adapter believes.
# --------------------------------------------------------------------------- #

INVOKEAI_MODELS: dict[str, Any] = {
    "models": [
        {
            "key": "4cf5c3ed-e6c8-490b-805d-5daa2646fced",
            "hash": "blake3:da55e305d88347119564c40ef9c778dd3df9b1f4620956f983a1f728",
            "name": "Dreamshaper 8",
            "base": "sd-1",
            "type": "main",
        },
        {
            "key": "0f7ed68f-d62b-45d1-82ae-c2901c963c72",
            "hash": "blake3:1f500a206b3b3",
            "name": "Juggernaut XL v9",
            "base": "sdxl",
            "type": "main",
        },
        {
            "key": "6dadd28b-ef8c-4bcb-8136-942d0812d8cf",
            "hash": "blake3:43127e572fa58",
            "name": "FLUX.1 schnell (quantized)",
            "base": "flux",
            "type": "main",
        },
        {
            "key": "c65adfac-1cd8-41c8-addf-db51e6b177ab",
            "hash": "blake3:aaa",
            "name": "Anima QwenImage VAE",
            "base": "anima",
            "type": "vae",
        },
    ]
}

INVOKEAI_ENQUEUED: dict[str, Any] = {
    "queue_id": "default",
    "enqueued": 1,
    "requested": 1,
    "batch": {"batch_id": "7d0068ab-e137-416f-a7a8-466c33d596e7"},
    "item_ids": [1883],
}

#: A finished session. Results are keyed by *prepared* node ids - UUIDs the
#: server assigns - not by the ids the graph used, which is why the adapter
#: matches on the declared output type.
INVOKEAI_COMPLETED: dict[str, Any] = {
    "item_id": 1883,
    "status": "completed",
    "session": {
        "results": {
            "7d3cd1f9-eb93-44bc-badf-fb390e2ede71": {
                "type": "model_loader_output",
                "vae": {"vae": {"key": "4cf5c3ed"}},
            },
            "80a57eb4-98f6-4b93-acb2-8b8c52e6b7d4": {
                "type": "noise_output",
                "noise": {"latents_name": "Tensor_528cb1df", "seed": 12345},
            },
            "2122cb6d-3daf-4caf-bcd9-6d9b18b32b99": {
                "type": "image_output",
                "image": {"image_name": "8860610b-2c83-4107-b4c9-0ec8ac3c7cd5.png"},
                "width": 512,
                "height": 512,
            },
        }
    },
}

PIPER_INFO: dict[str, Any] = {
    "voice": {"language": "en-us", "name": "en_US-amy-medium", "num_speakers": 1},
    "last": {"text": "Hello!", "synthesize_seconds": 0.094},
}

#: A one-frame WAV, so the fixtures stay small but remain real WAV files.
TINY_WAV = (
    b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00"
    b"\x22\x56\x00\x00\x44\xac\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00"
)


class RecordedTransport:
    """Answers an adapter's HTTP calls from a script, and records them.

    Substituted for the adapter's ``request_json``/``request_bytes`` rather
    than for httpx, so what is exercised is the adapter's own logic: which
    path, which body, and what it makes of the answer.
    """

    def __init__(self, routes: dict[str, Any], *, content_type: str = "application/json") -> None:
        self.routes = routes
        self.content_type = content_type
        self.calls: list[tuple[str, str, Any]] = []

    def _match(self, path: str) -> Any:
        for pattern, response in self.routes.items():
            if pattern in path:
                return response
        raise ProviderError(f"no recorded response for {path}")

    async def request_json(
        self, method: str, path: str, *, json_body: Any = None, **_kwargs: Any
    ) -> tuple[Any, float]:
        self.calls.append((method, path, json_body))
        return self._match(path), 1.0

    async def request_bytes(
        self, method: str, path: str, *, json_body: Any = None, **_kwargs: Any
    ) -> tuple[bytes, str, float]:
        self.calls.append((method, path, json_body))
        payload = self._match(path)
        if isinstance(payload, tuple):
            return payload[0], payload[1], 1.0
        return payload, self.content_type, 1.0

    def body_for(self, fragment: str) -> Any:
        return next(body for _m, path, body in self.calls if fragment in path)


def attach(provider: Any, transport: RecordedTransport) -> Any:
    provider.request_json = transport.request_json  # type: ignore[method-assign]
    provider.request_bytes = transport.request_bytes  # type: ignore[method-assign]
    return provider


# --------------------------------------------------------------------------- #
# The interface itself
# --------------------------------------------------------------------------- #


class TestInterfaces:
    """Every adapter is substitutable for its base (section 97)."""

    @pytest.mark.parametrize("adapter", [InvokeAIProvider, ProceduralImageProvider])
    def test_image_adapters_implement_the_image_provider(self, adapter: type) -> None:
        assert issubclass(adapter, ImageProvider)
        assert adapter.kind == "image"
        instance = adapter("x", {"base_url": "http://localhost:9090"})
        assert any(c.name == "text_to_image" for c in instance.capabilities())
        # The interface is what the engine calls; nothing else is required.
        assert callable(instance.generate)
        assert callable(instance.health_check)

    @pytest.mark.parametrize(
        "adapter", [PiperProvider, OpenAISpeechProvider, ProceduralSpeechProvider]
    )
    def test_speech_adapters_implement_the_speech_provider(self, adapter: type) -> None:
        assert issubclass(adapter, SpeechProvider)
        assert adapter.kind == "speech"
        instance = adapter("x", {"base_url": "http://localhost:5000"})
        assert any(c.name == "text_to_speech" for c in instance.capabilities())

    def test_every_media_adapter_is_registered(self) -> None:
        from cacophony.providers.registry import PROVIDER_REGISTRY

        adapters = PROVIDER_REGISTRY.adapters()
        assert {"invokeai", "procedural_image", "piper", "openai_speech", "procedural_speech"} <= (
            set(adapters)
        )


# --------------------------------------------------------------------------- #
# InvokeAI, offline
# --------------------------------------------------------------------------- #


class TestInvokeAIContract:
    def _provider(self, **config: Any) -> tuple[InvokeAIProvider, RecordedTransport]:
        transport = RecordedTransport(
            {
                "/api/v2/models/": INVOKEAI_MODELS,
                "/enqueue_batch": INVOKEAI_ENQUEUED,
                "/api/v1/queue/default/i/": INVOKEAI_COMPLETED,
                "/full": (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, "image/png"),
            }
        )
        provider = InvokeAIProvider("pictures", {"base_url": "http://invoke", **config})
        return attach(provider, transport), transport

    def test_the_model_is_sent_as_an_identifier_not_a_name(self) -> None:
        """The defect a real server found: a node will not take a string."""
        provider, transport = self._provider(model="Dreamshaper 8")
        asyncio.run(provider.generate(ImageRequest(prompt="a cat", seed=7)))

        graph = transport.body_for("/enqueue_batch")["batch"]["graph"]
        model = graph["nodes"]["model"]["model"]
        assert isinstance(model, dict)
        assert set(model) == {"key", "hash", "name", "base", "type"}
        assert model["name"] == "Dreamshaper 8"
        assert model["key"] == "4cf5c3ed-e6c8-490b-805d-5daa2646fced"

    def test_the_model_list_is_fetched_once(self) -> None:
        provider, transport = self._provider(model="Dreamshaper 8")
        for _ in range(3):
            asyncio.run(provider.generate(ImageRequest(prompt="a cat")))
        assert sum(1 for _m, path, _b in transport.calls if "/api/v2/models/" in path) == 1

    def test_sd1_wires_a_single_text_encoder(self) -> None:
        provider, transport = self._provider(model="Dreamshaper 8")
        asyncio.run(provider.generate(ImageRequest(prompt="a cat")))

        graph = transport.body_for("/enqueue_batch")["batch"]["graph"]
        assert graph["nodes"]["model"]["type"] == "main_model_loader"
        assert graph["nodes"]["positive"]["type"] == "compel"
        assert not [e for e in graph["edges"] if e["source"]["field"] == "clip2"]

    def test_sdxl_wires_both_text_encoders(self) -> None:
        provider, transport = self._provider(model="Juggernaut XL v9")
        asyncio.run(provider.generate(ImageRequest(prompt="a cat")))

        graph = transport.body_for("/enqueue_batch")["batch"]["graph"]
        assert graph["nodes"]["model"]["type"] == "sdxl_model_loader"
        assert graph["nodes"]["positive"]["type"] == "sdxl_compel_prompt"
        assert len([e for e in graph["edges"] if e["source"]["field"] == "clip2"]) == 2

    def test_an_architecture_without_a_graph_says_so(self) -> None:
        provider, _transport = self._provider(model="FLUX.1 schnell (quantized)")
        with pytest.raises(ProviderError, match="workflow"):
            asyncio.run(provider.generate(ImageRequest(prompt="a cat")))

    def test_an_unknown_model_lists_the_installed_ones(self) -> None:
        provider, _transport = self._provider(model="Stable Diffusion 1.5")
        with pytest.raises(ProviderError, match="Dreamshaper 8"):
            asyncio.run(provider.generate(ImageRequest(prompt="a cat")))

    def test_naming_no_model_picks_a_supported_one(self) -> None:
        provider, transport = self._provider()
        asyncio.run(provider.generate(ImageRequest(prompt="a cat")))
        graph = transport.body_for("/enqueue_batch")["batch"]["graph"]
        assert graph["nodes"]["model"]["model"]["base"] in ("sd-1", "sd-2", "sdxl")

    def test_the_seed_is_narrowed_to_what_invokeai_accepts(self) -> None:
        """Cacophony seeds are 64-bit; InvokeAI's field is 32-bit unsigned."""
        provider, transport = self._provider(model="Dreamshaper 8")
        asyncio.run(provider.generate(ImageRequest(prompt="a cat", seed=2**63 - 1)))
        graph = transport.body_for("/enqueue_batch")["batch"]["graph"]
        assert 0 <= graph["nodes"]["noise"]["seed"] < 0xFFFFFFFF

    def test_the_image_is_found_by_output_type_not_position(self) -> None:
        provider, _transport = self._provider(model="Dreamshaper 8")
        result = asyncio.run(provider.generate(ImageRequest(prompt="a cat", seed=1)))
        assert is_png(result.data or b"")
        assert result.raw["image_name"] == "8860610b-2c83-4107-b4c9-0ec8ac3c7cd5.png"

    def test_it_records_section_19s_provenance(self) -> None:
        provider, _transport = self._provider(model="Dreamshaper 8")
        result = asyncio.run(provider.generate(ImageRequest(prompt="a cat", seed=11)))
        assert result.provider == "pictures"
        assert result.seed == 11
        assert result.prompt_hash
        assert result.workflow

    def test_a_failed_queue_item_is_reported_not_awaited(self) -> None:
        transport = RecordedTransport(
            {
                "/api/v2/models/": INVOKEAI_MODELS,
                "/enqueue_batch": INVOKEAI_ENQUEUED,
                "/api/v1/queue/default/i/": {
                    "status": "failed",
                    "error_message": "CUDA out of memory",
                },
            }
        )
        provider = attach(
            InvokeAIProvider("pictures", {"base_url": "http://invoke", "model": "Dreamshaper 8"}),
            transport,
        )
        with pytest.raises(ProviderError, match="CUDA out of memory"):
            asyncio.run(provider.generate(ImageRequest(prompt="a cat")))

    def test_a_supplied_workflow_is_used_with_the_prompt_substituted(self) -> None:
        provider, transport = self._provider()
        workflow = {
            "id": "mine",
            "nodes": {
                "n1": {"id": "n1", "type": "compel", "prompt": "PLACEHOLDER"},
                "negative": {"id": "negative", "type": "compel", "prompt": "blurry"},
                "n2": {"id": "n2", "type": "noise", "seed": 0, "width": 64, "height": 64},
            },
            "edges": [],
        }
        asyncio.run(
            provider.generate(
                ImageRequest(
                    prompt="a specific cat",
                    seed=5,
                    width=768,
                    height=768,
                    metadata={"graph": workflow},
                )
            )
        )
        graph = transport.body_for("/enqueue_batch")["batch"]["graph"]
        assert graph["id"] == "mine"
        assert graph["nodes"]["n1"]["prompt"] == "a specific cat"
        # The negative prompt is the user's, not this record's.
        assert graph["nodes"]["negative"]["prompt"] == "blurry"
        assert graph["nodes"]["n2"]["width"] == 768
        # A workflow means the model list is never consulted.
        assert not [path for _m, path, _b in transport.calls if "/api/v2/models/" in path]

    def test_health_reports_the_server_version(self) -> None:
        assert InvokeAIProvider("x", {})._health_details({"version": "6.13.8"}) == {
            "invokeai_version": "6.13.8"
        }


# --------------------------------------------------------------------------- #
# Piper, offline
# --------------------------------------------------------------------------- #


class TestPiperContract:
    def _provider(self, **config: Any) -> tuple[PiperProvider, RecordedTransport]:
        transport = RecordedTransport(
            # The lie a real server told: WAV bytes under text/html.
            {"/synthesize": (TINY_WAV, "text/html"), "/info": PIPER_INFO}
        )
        provider = PiperProvider("voices", {"base_url": "http://piper", **config})
        return attach(provider, transport), transport

    def test_it_posts_to_synthesize(self) -> None:
        provider, transport = self._provider()
        asyncio.run(provider.synthesize(SpeechRequest(text="hello")))
        assert transport.calls[0][1] == "/synthesize"
        assert transport.calls[0][2]["text"] == "hello"

    def test_a_text_html_content_type_does_not_make_it_a_web_page(self) -> None:
        """The defect a real server found: Flask labels bare bytes text/html."""
        provider, _transport = self._provider()
        result = asyncio.run(provider.synthesize(SpeechRequest(text="hello")))
        assert result.media_type == "audio/wav"
        assert result.raw["declared_media_type"] == "text/html"
        assert is_wav(result.data or b"")

    def test_speed_becomes_the_reciprocal_length_scale(self) -> None:
        """Piper scales length, so a faster voice is a *smaller* number."""
        provider, transport = self._provider()
        asyncio.run(provider.synthesize(SpeechRequest(text="hello", speed=2.0)))
        assert transport.body_for("/synthesize")["length_scale"] == 0.5

    def test_the_duration_is_read_from_the_audio(self) -> None:
        provider, _transport = self._provider()
        result = asyncio.run(provider.synthesize(SpeechRequest(text="hello")))
        assert result.duration_seconds == 0.0  # a one-frame fixture

    def test_options_pass_through(self) -> None:
        provider, transport = self._provider()
        asyncio.run(provider.synthesize(SpeechRequest(text="hi", options={"noise_scale": 0.5})))
        assert transport.body_for("/synthesize")["noise_scale"] == 0.5

    def test_empty_audio_is_an_error_not_a_silent_file(self) -> None:
        transport = RecordedTransport({"/synthesize": (b"", "text/html")})
        provider = attach(PiperProvider("voices", {"base_url": "http://piper"}), transport)
        with pytest.raises(ProviderError, match="no audio"):
            asyncio.run(provider.synthesize(SpeechRequest(text="hello")))

    def test_health_names_the_loaded_voice(self) -> None:
        details = PiperProvider("x", {})._health_details(PIPER_INFO)
        assert details["voice"] == "en_US-amy-medium"
        assert details["language"] == "en-us"


class TestOpenAISpeechContract:
    def test_it_speaks_the_common_interface(self) -> None:
        transport = RecordedTransport({"/v1/audio/speech": (TINY_WAV, "audio/wav")})
        provider = attach(
            OpenAISpeechProvider("tts", {"base_url": "http://tts", "model": "kokoro"}), transport
        )
        asyncio.run(provider.synthesize(SpeechRequest(text="hello", voice="nova", speed=1.2)))

        body = transport.body_for("/v1/audio/speech")
        assert body == {
            "model": "kokoro",
            "input": "hello",
            "voice": "nova",
            "response_format": "wav",
            "speed": 1.2,
        }


# --------------------------------------------------------------------------- #
# Live tests (opt-in)
# --------------------------------------------------------------------------- #


def _server(variable: str) -> str:
    url = os.environ.get(variable)
    if not url:
        pytest.skip(f"set {variable} to run this against a real server")
    return url


def run(coroutine: Any) -> Any:
    """One event loop per test.

    A provider keeps its HTTP client for its lifetime, which is right - a run
    lives in one loop and should not reconnect per record - so a test that
    calls ``asyncio.run`` twice against the same provider is using it wrongly.
    """
    return asyncio.run(coroutine)


@pytest.mark.live
class TestLiveInvokeAI:
    def _provider(self, **config: Any) -> InvokeAIProvider:
        return InvokeAIProvider(
            "pictures",
            {
                "base_url": _server("CACOPHONY_TEST_INVOKEAI"),
                "timeout_seconds": 600,
                "steps": 6,
                "poll_timeout_seconds": 600,
                **config,
            },
        )

    def test_health(self) -> None:
        async def check() -> Any:
            provider = self._provider()
            try:
                return await provider.health_check()
            finally:
                await provider.aclose()

        status = run(check())
        assert status.healthy
        assert "invokeai_version" in status.details

    def test_it_generates_a_real_png(self) -> None:
        async def generate() -> Any:
            provider = self._provider()
            try:
                return await provider.generate(
                    ImageRequest(prompt="a red cube", width=512, height=512, seed=1)
                )
            finally:
                await provider.aclose()

        result = run(generate())
        assert is_png(result.data or b"")
        assert result.media_type == "image/png"
        assert result.seed == 1

    def test_the_same_seed_gives_the_same_image(self) -> None:
        """Section 4: reproducible where the backend allows it."""

        async def twice() -> tuple[Any, Any]:
            provider = self._provider()
            request = ImageRequest(prompt="a blue teapot", width=512, height=512, seed=4242)
            try:
                return await provider.generate(request), await provider.generate(request)
            finally:
                await provider.aclose()

        first, second = run(twice())
        assert first.data == second.data

    def test_an_unsupported_architecture_is_refused_with_a_reason(self) -> None:
        async def attempt() -> None:
            provider = self._provider()
            try:
                models = await provider._models()
                flux = next(
                    (
                        model
                        for model in models
                        if model.get("type") == "main" and model.get("base") == "flux"
                    ),
                    None,
                )
                if flux is None:
                    pytest.skip("no flux model installed on this server")
                await provider._default_graph(ImageRequest(prompt="x", model=flux["name"]))
            finally:
                await provider.aclose()

        with pytest.raises(ProviderError, match="workflow"):
            run(attempt())


@pytest.mark.live
class TestLivePiper:
    def _provider(self) -> PiperProvider:
        return PiperProvider("voices", {"base_url": _server("CACOPHONY_TEST_PIPER")})

    def test_health_names_a_voice(self) -> None:
        async def check() -> Any:
            provider = self._provider()
            try:
                return await provider.health_check()
            finally:
                await provider.aclose()

        status = run(check())
        assert status.healthy
        assert status.details.get("voice")

    def test_it_speaks_and_the_length_tracks_the_text(self) -> None:
        async def speak() -> tuple[Any, Any]:
            provider = self._provider()
            try:
                return (
                    await provider.synthesize(SpeechRequest(text="Hello.")),
                    await provider.synthesize(
                        SpeechRequest(
                            text="Hello, you have reached support. How can I help you today?"
                        )
                    ),
                )
            finally:
                await provider.aclose()

        short, long = run(speak())
        assert is_wav(short.data or b"")
        # The server labels WAV as text/html; believing it would file every
        # recording as `.bin`.
        assert short.media_type == "audio/wav"
        assert short.raw["declared_media_type"] != "audio/wav"
        assert (long.duration_seconds or 0) > (short.duration_seconds or 0) * 2

    def test_speed_shortens_the_audio(self) -> None:
        async def speak() -> tuple[Any, Any]:
            provider = self._provider()
            text = "The quick brown fox jumps over the lazy dog."
            try:
                return (
                    await provider.synthesize(SpeechRequest(text=text, speed=0.7)),
                    await provider.synthesize(SpeechRequest(text=text, speed=1.5)),
                )
            finally:
                await provider.aclose()

        slow, fast = run(speak())
        assert (fast.duration_seconds or 0) < (slow.duration_seconds or 0)


@pytest.mark.live
class TestLiveOllama:
    def test_it_answers_with_structured_output(self) -> None:
        from cacophony.providers.base import GenerationRequest
        from cacophony.providers.llm.ollama import OllamaProvider

        async def ask() -> Any:
            provider = OllamaProvider(
                "local",
                {
                    "base_url": _server("CACOPHONY_TEST_OLLAMA"),
                    "model": os.environ.get("CACOPHONY_TEST_MODEL", "gemma4:12b"),
                    "timeout_seconds": 300,
                },
            )
            try:
                return await provider.generate(
                    GenerationRequest(
                        prompt="Give a one-line summary of a printer jam ticket.",
                        json_schema={
                            "type": "object",
                            "properties": {"subject": {"type": "string"}},
                            "required": ["subject"],
                            "additionalProperties": False,
                        },
                    )
                )
            finally:
                await provider.aclose()

        result = run(ask())
        assert json.loads(result.text)["subject"]
