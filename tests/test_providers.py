"""The provider layer (design document sections 10, 43, 63, 76, 85, 88).

Adapter tests drive real request/response cycles through an httpx mock
transport rather than a hand-written double, so the body Cacophony actually
sends is what gets asserted on. Section 88 calls these provider contract tests:
their job is to prove each adapter obeys the provider interface and speaks its
server's dialect.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from cacophony.core.errors import ProviderError, ProviderNotFoundError, ProviderUnavailableError
from cacophony.providers.base import GenerationRequest
from cacophony.providers.cache import CacheMode, GenerationCache, cache_key
from cacophony.providers.llm.llamacpp import LlamaCppProvider
from cacophony.providers.llm.mock import MockLanguageModelProvider
from cacophony.providers.llm.ollama import OllamaProvider
from cacophony.providers.llm.openai_compat import OpenAICompatibleProvider
from cacophony.providers.registry import PROVIDER_REGISTRY, ProviderRegistry
from cacophony.providers.secrets import SecretResolver, redact, secret_env_var
from cacophony.schema.models import ProviderSpec


class Recorder:
    """Captures the requests an adapter makes and replies with a script."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            reply = self.routes.get(request.url.path)
            if reply is None:
                return httpx.Response(404, json={"error": "no route"})
            if isinstance(reply, httpx.Response):
                return reply
            if callable(reply):
                return reply(request)
            return httpx.Response(200, json=reply)

        return httpx.MockTransport(handle)

    @property
    def bodies(self) -> list[Any]:
        return [json.loads(request.content) for request in self.requests if request.content]


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Ollama
# --------------------------------------------------------------------------- #


class TestOllama:
    def _provider(self, recorder: Recorder, **config: Any) -> OllamaProvider:
        return OllamaProvider(
            "ollama", {"transport": recorder.transport(), "model": "llama3.1:8b", **config}
        )

    def test_generate_uses_the_native_api(self) -> None:
        recorder = Recorder(
            {
                "/api/generate": {
                    "response": '{"bio": "words"}',
                    "model": "llama3.1:8b",
                    "prompt_eval_count": 30,
                    "eval_count": 12,
                    "done_reason": "stop",
                }
            }
        )
        provider = self._provider(recorder)
        result = run(
            provider.generate(
                GenerationRequest(
                    prompt="write a bio",
                    system="be brief",
                    temperature=0.4,
                    max_tokens=128,
                    seed=99,
                    json_schema={"type": "object"},
                )
            )
        )
        assert result.text == '{"bio": "words"}'
        assert result.prompt_tokens == 30 and result.completion_tokens == 12
        assert result.provider == "ollama"

        body = recorder.bodies[0]
        assert body["model"] == "llama3.1:8b"
        assert body["stream"] is False
        assert body["system"] == "be brief"
        # Sampling parameters live under 'options' in Ollama's native API.
        assert body["options"]["temperature"] == 0.4
        assert body["options"]["num_predict"] == 128
        assert body["options"]["seed"] == 99
        # A JSON schema is sent as 'format' so decoding is constrained.
        assert body["format"] == {"type": "object"}

    def test_seed_is_narrowed_to_32_bits(self) -> None:
        """Cacophony's seeds are 64-bit; Ollama wants something smaller."""
        recorder = Recorder({"/api/generate": {"response": "x"}})
        run(self._provider(recorder).generate(GenerationRequest(prompt="p", seed=2**63 - 1)))
        assert 0 <= recorder.bodies[0]["options"]["seed"] < 2**31

    def test_list_models(self) -> None:
        recorder = Recorder(
            {
                "/api/tags": {
                    "models": [
                        {
                            "name": "llama3.1:8b",
                            "digest": "abc",
                            "size": 100,
                            "details": {
                                "family": "llama",
                                "parameter_size": "8B",
                                "quantization_level": "Q4_K_M",
                            },
                        }
                    ]
                }
            }
        )
        models = run(self._provider(recorder).list_models_async())
        assert models[0].name == "llama3.1:8b"
        assert models[0].parameter_size == "8B"
        assert models[0].quantization == "Q4_K_M"

    def test_health_check_counts_models(self) -> None:
        recorder = Recorder({"/api/tags": {"models": [{"name": "a"}, {"name": "b"}]}})
        status = run(self._provider(recorder).health_check())
        assert status.healthy
        assert status.details["models_available"] == 2

    def test_unreachable_server_is_reported_as_unavailable(self) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        provider = OllamaProvider(
            "ollama",
            {"transport": httpx.MockTransport(refuse), "model": "m", "transport_retries": 0},
        )
        status = run(provider.health_check())
        assert not status.healthy and "unreachable" in status.message

    def test_missing_model_is_reported(self) -> None:
        recorder = Recorder({"/api/generate": {"response": "x"}})
        provider = OllamaProvider("ollama", {"transport": recorder.transport()})
        with pytest.raises(ProviderError, match="no model configured"):
            run(provider.generate(GenerationRequest(prompt="p")))

    def test_http_error_names_the_provider(self) -> None:
        recorder = Recorder({"/api/generate": httpx.Response(500, text="boom")})
        with pytest.raises(ProviderError, match="ollama"):
            run(self._provider(recorder).generate(GenerationRequest(prompt="p")))

    def test_capabilities_include_structured_output(self) -> None:
        recorder = Recorder({})
        names = {c.name for c in self._provider(recorder).capabilities()}
        assert "structured_output" in names and "text_generation" in names


# --------------------------------------------------------------------------- #
# llama.cpp
# --------------------------------------------------------------------------- #


class TestLlamaCpp:
    def _provider(self, recorder: Recorder, **config: Any) -> LlamaCppProvider:
        return LlamaCppProvider("lcpp", {"transport": recorder.transport(), **config})

    def test_generate_uses_the_completion_endpoint(self) -> None:
        recorder = Recorder(
            {
                "/completion": {
                    "content": '{"a": 1}',
                    "tokens_predicted": 8,
                    "tokens_evaluated": 40,
                    "stop_type": "eos",
                }
            }
        )
        result = run(
            self._provider(recorder).generate(
                GenerationRequest(
                    prompt="hello",
                    system="be terse",
                    max_tokens=64,
                    temperature=0.2,
                    seed=5,
                    json_schema={"type": "object"},
                )
            )
        )
        assert result.text == '{"a": 1}'
        assert result.completion_tokens == 8

        body = recorder.bodies[0]
        assert body["n_predict"] == 64
        assert body["temperature"] == 0.2
        # llama.cpp compiles json_schema into a GBNF grammar.
        assert body["json_schema"] == {"type": "object"}
        # /completion has no chat template, so the system prompt is prepended.
        assert body["prompt"].startswith("be terse")
        assert "hello" in body["prompt"]

    def test_models_fall_back_to_props(self) -> None:
        """Older builds expose /props but not /v1/models."""
        recorder = Recorder(
            {
                "/v1/models": httpx.Response(404, json={"error": "nope"}),
                "/props": {"model_path": "/models/qwen2.5-7b.gguf", "n_ctx": 8192},
            }
        )
        models = run(self._provider(recorder).list_models_async())
        assert models[0].name == "qwen2.5-7b.gguf"
        assert models[0].context_length == 8192

    def test_models_prefer_v1(self) -> None:
        recorder = Recorder({"/v1/models": {"data": [{"id": "local-model"}]}})
        assert run(self._provider(recorder).list_models_async())[0].name == "local-model"

    def test_loading_server_reports_a_useful_message(self) -> None:
        recorder = Recorder({"/health": httpx.Response(503, json={"status": "loading model"})})
        status = run(self._provider(recorder).health_check())
        assert not status.healthy
        assert "loading" in status.message


# --------------------------------------------------------------------------- #
# OpenAI-compatible
# --------------------------------------------------------------------------- #


def _chat_reply(content: str = '{"a": 1}') -> dict[str, Any]:
    return {
        "model": "gpt-local",
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 3},
    }


class TestOpenAICompatible:
    def _provider(self, recorder: Recorder, **config: Any) -> OpenAICompatibleProvider:
        return OpenAICompatibleProvider(
            "compat", {"transport": recorder.transport(), "model": "gpt-local", **config}
        )

    def test_chat_completions_shape(self) -> None:
        recorder = Recorder({"/v1/chat/completions": _chat_reply()})
        result = run(
            self._provider(recorder).generate(
                GenerationRequest(prompt="hi", system="sys", max_tokens=50, seed=3)
            )
        )
        assert result.text == '{"a": 1}'
        assert result.prompt_tokens == 11

        body = recorder.bodies[0]
        assert body["messages"] == [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ]
        assert body["max_tokens"] == 50

    def test_structured_output_negotiation_falls_back(self) -> None:
        """A server that rejects json_schema should still be usable."""
        calls: list[dict[str, Any]] = []

        def handle(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            calls.append(body)
            fmt = (body.get("response_format") or {}).get("type")
            if fmt == "json_schema":
                return httpx.Response(400, json={"error": "response_format not supported"})
            return httpx.Response(200, json=_chat_reply())

        provider = OpenAICompatibleProvider(
            "compat", {"transport": httpx.MockTransport(handle), "model": "gpt-local"}
        )
        result = run(
            provider.generate(GenerationRequest(prompt="p", json_schema={"type": "object"}))
        )
        assert result.text == '{"a": 1}'
        assert [(c.get("response_format") or {}).get("type") for c in calls] == [
            "json_schema",
            "json_object",
        ]

    def test_negotiation_is_remembered(self) -> None:
        """The capability answer is learned once, not re-tested per record."""
        calls: list[dict[str, Any]] = []

        def handle(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            calls.append(body)
            if (body.get("response_format") or {}).get("type") == "json_schema":
                return httpx.Response(400, json={"error": "unsupported response_format"})
            return httpx.Response(200, json=_chat_reply())

        provider = OpenAICompatibleProvider(
            "compat", {"transport": httpx.MockTransport(handle), "model": "gpt-local"}
        )
        for _ in range(3):
            run(provider.generate(GenerationRequest(prompt="p", json_schema={"type": "object"})))
        # Two calls for the first request, then one each - not two each.
        assert len(calls) == 4

    def test_explicit_mode_is_not_negotiated(self) -> None:
        recorder = Recorder({"/v1/chat/completions": _chat_reply()})
        provider = self._provider(recorder, structured_output="json_object")
        run(provider.generate(GenerationRequest(prompt="p", json_schema={"type": "object"})))
        assert recorder.bodies[0]["response_format"] == {"type": "json_object"}

    def test_structured_output_none(self) -> None:
        recorder = Recorder({"/v1/chat/completions": _chat_reply()})
        provider = self._provider(recorder, structured_output="none")
        run(provider.generate(GenerationRequest(prompt="p", json_schema={"type": "object"})))
        assert "response_format" not in recorder.bodies[0]

    def test_bad_structured_output_setting_is_rejected(self) -> None:
        with pytest.raises(ProviderError, match="structured_output"):
            OpenAICompatibleProvider("compat", {"structured_output": "telepathy"})

    def test_bearer_token_is_sent(self) -> None:
        recorder = Recorder({"/v1/chat/completions": _chat_reply()})
        resolver = SecretResolver(overrides={"my-key": "s3cret"})
        provider = OpenAICompatibleProvider(
            "compat",
            {"transport": recorder.transport(), "model": "m", "secret": "my-key"},
            secrets=resolver,
        )
        run(provider.generate(GenerationRequest(prompt="p")))
        assert recorder.requests[0].headers["authorization"] == "Bearer s3cret"


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


def test_provider_concurrency_is_enforced() -> None:
    """Section 30: each provider limits its own in-flight requests."""
    in_flight = 0
    peak = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return httpx.Response(200, json={"response": "ok"})

    provider = OllamaProvider(
        "ollama",
        {"transport": httpx.MockTransport(handle), "model": "m", "concurrency": 2},
    )

    async def drive() -> None:
        await asyncio.gather(
            *(provider.generate(GenerationRequest(prompt=f"p{i}")) for i in range(8))
        )

    run(drive())
    assert peak <= 2


def test_transport_errors_are_retried_then_reported() -> None:
    attempts = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("refused")

    provider = OllamaProvider(
        "ollama",
        {"transport": httpx.MockTransport(handle), "model": "m", "transport_retries": 2},
    )
    with pytest.raises(ProviderUnavailableError):
        run(provider.generate(GenerationRequest(prompt="p")))
    assert attempts == 3  # the first try plus two retries


# --------------------------------------------------------------------------- #
# Mock provider
# --------------------------------------------------------------------------- #


class TestMockProvider:
    def test_synthesises_against_the_schema(self) -> None:
        provider = MockLanguageModelProvider("m")
        schema = {
            "type": "object",
            "properties": {
                "title": {"type": "string", "maxLength": 30},
                "score": {"type": "integer", "minimum": 1, "maximum": 5},
                "tags": {"type": "array", "items": {"type": "string"}},
                "active": {"type": "boolean"},
            },
            "required": ["title", "score"],
        }
        result = run(provider.generate(GenerationRequest(prompt="p", json_schema=schema, seed=1)))
        payload = json.loads(result.text)
        assert len(payload["title"]) <= 30
        assert 1 <= payload["score"] <= 5
        assert isinstance(payload["tags"], list)
        assert isinstance(payload["active"], bool)

    def test_honours_enums(self) -> None:
        provider = MockLanguageModelProvider("m")
        schema = {
            "type": "object",
            "properties": {"colour": {"type": "string", "enum": ["red", "blue"]}},
        }
        for seed in range(20):
            payload = json.loads(
                run(
                    provider.generate(GenerationRequest(prompt="p", json_schema=schema, seed=seed))
                ).text
            )
            assert payload["colour"] in {"red", "blue"}

    def test_is_deterministic_for_a_seed(self) -> None:
        provider = MockLanguageModelProvider("m")
        request = GenerationRequest(prompt="p", seed=42)
        assert run(provider.generate(request)).text == run(provider.generate(request)).text

    def test_simulated_failures(self) -> None:
        provider = MockLanguageModelProvider("m", {"failure_rate": 1.0})
        with pytest.raises(ProviderUnavailableError):
            run(provider.generate(GenerationRequest(prompt="p", seed=1)))

    def test_simulated_malformed_output(self) -> None:
        provider = MockLanguageModelProvider("m", {"malformed_rate": 1.0})
        text = run(provider.generate(GenerationRequest(prompt="p", seed=1))).text
        with pytest.raises(json.JSONDecodeError):
            json.loads(text)

    def test_records_its_calls(self) -> None:
        provider = MockLanguageModelProvider("m")
        run(provider.generate(GenerationRequest(prompt="one")))
        run(provider.generate(GenerationRequest(prompt="two")))
        assert [call.prompt for call in provider.calls] == ["one", "two"]


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


class TestRegistry:
    def test_built_in_adapters_are_registered(self) -> None:
        for name in ("ollama", "llamacpp", "openai_compatible", "mock"):
            assert name in PROVIDER_REGISTRY.adapters()

    def test_aliases_resolve(self) -> None:
        assert PROVIDER_REGISTRY.resolve_adapter("llama.cpp") == "llamacpp"
        assert PROVIDER_REGISTRY.resolve_adapter("vllm") == "openai_compatible"

    def test_unknown_adapter_lists_alternatives(self) -> None:
        registry = ProviderRegistry()
        registry.register_adapter("mock", MockLanguageModelProvider)
        with pytest.raises(ProviderNotFoundError, match="Available adapters"):
            registry.create(ProviderSpec(id="x", adapter="telepathy"))

    def test_create_from_spec(self) -> None:
        registry = ProviderRegistry()
        registry.register_adapter("ollama", OllamaProvider)
        provider = registry.create(
            ProviderSpec(id="local", adapter="ollama", base_url="http://gpu-box:11434", model="m")
        )
        assert provider.base_url == "http://gpu-box:11434"
        assert registry.get("local") is provider

    def test_unknown_instance_lists_configured(self) -> None:
        with pytest.raises(ProviderNotFoundError, match="Configured"):
            ProviderRegistry().get("ghost")

    def test_adapter_name_is_stamped(self) -> None:
        assert OllamaProvider.adapter_name == "ollama"


# --------------------------------------------------------------------------- #
# Secrets (section 63)
# --------------------------------------------------------------------------- #


class TestSecrets:
    def test_environment_variable_convention(self) -> None:
        assert secret_env_var("openai-main") == "CACOPHONY_SECRET_OPENAI_MAIN"

    def test_resolution_from_the_environment(self) -> None:
        resolver = SecretResolver(environ={"CACOPHONY_SECRET_MY_KEY": "abc"}, use_keyring=False)
        assert resolver.resolve("my-key") == "abc"

    def test_resolution_from_a_bare_variable_name(self) -> None:
        resolver = SecretResolver(environ={"MY_KEY": "abc"}, use_keyring=False)
        assert resolver.resolve("my_key") == "abc"

    def test_overrides_win(self) -> None:
        resolver = SecretResolver(
            overrides={"k": "override"},
            environ={"CACOPHONY_SECRET_K": "env"},
            use_keyring=False,
        )
        assert resolver.resolve("k") == "override"

    def test_missing_secret_returns_none(self) -> None:
        assert SecretResolver(environ={}, use_keyring=False).resolve("nope") is None

    def test_require_explains_how_to_supply_it(self) -> None:
        resolver = SecretResolver(environ={}, use_keyring=False)
        with pytest.raises(LookupError, match="CACOPHONY_SECRET_NOPE"):
            resolver.require("nope")

    def test_redaction_keeps_only_a_tail(self) -> None:
        assert redact("supersecretvalue") == "************alue"
        assert redact(None) == "<unset>"

    def test_no_secret_id_resolves_to_none(self) -> None:
        assert SecretResolver().resolve(None) is None


# --------------------------------------------------------------------------- #
# Cache (section 76)
# --------------------------------------------------------------------------- #


class TestCache:
    def test_key_covers_everything_that_changes_the_answer(self) -> None:
        base = {
            "provider": "p",
            "model": "m",
            "prompt": "hello",
            "settings": {"temperature": 0.5},
            "seed": 1,
        }
        key = cache_key(**base)
        assert key == cache_key(**base)
        for field, value in (
            ("provider", "other"),
            ("model", "other"),
            ("prompt", "goodbye"),
            ("settings", {"temperature": 0.9}),
            ("seed", 2),
        ):
            assert cache_key(**{**base, field: value}) != key, field

    def test_setting_order_does_not_change_the_key(self) -> None:
        left = cache_key(provider="p", model="m", prompt="x", settings={"a": 1, "b": 2})
        right = cache_key(provider="p", model="m", prompt="x", settings={"b": 2, "a": 1})
        assert left == right

    def test_disabled_never_serves(self) -> None:
        cache = GenerationCache(mode=CacheMode.DISABLED)
        cache.put("k", {"text": "v"})
        assert cache.get("k") is None

    def test_read_only_serves_but_does_not_record(self) -> None:
        cache = GenerationCache(mode=CacheMode.READ_WRITE)
        cache.put("k", {"text": "v"})
        cache.mode = CacheMode.READ_ONLY
        assert cache.get("k") == {"text": "v"}
        cache.put("k2", {"text": "v2"})
        assert cache.get("k2") is None

    def test_read_write_round_trip(self) -> None:
        cache = GenerationCache(mode=CacheMode.READ_WRITE)
        cache.put("k", {"text": "v"})
        assert cache.get("k") == {"text": "v"}
        assert cache.stats.hits == 1 and cache.stats.writes == 1

    def test_misses_are_counted(self) -> None:
        cache = GenerationCache(mode=CacheMode.READ_WRITE)
        assert cache.get("nothing") is None
        assert cache.stats.misses == 1 and cache.stats.hit_rate == 0.0

    def test_persists_across_instances(self, tmp_path) -> None:
        path = tmp_path / "cache.db"
        with GenerationCache(path, mode=CacheMode.READ_WRITE) as cache:
            cache.put("k", {"text": "durable"}, provider="p", model="m")
        with GenerationCache(path, mode=CacheMode.READ_WRITE) as reopened:
            assert reopened.get("k") == {"text": "durable"}
            assert len(reopened) == 1

    def test_clear(self, tmp_path) -> None:
        cache = GenerationCache(tmp_path / "c.db", mode=CacheMode.READ_WRITE)
        cache.put("k", {"text": "v"})
        cache.clear()
        assert len(cache) == 0 and cache.get("k") is None
        cache.close()

    def test_corrupt_entry_is_dropped_rather_than_raised(self, tmp_path) -> None:
        cache = GenerationCache(tmp_path / "c.db", mode=CacheMode.READ_WRITE)
        cache.put("k", {"text": "v"})
        cache._memory["k"] = "{not json"
        assert cache.get("k") is None
        cache.close()
