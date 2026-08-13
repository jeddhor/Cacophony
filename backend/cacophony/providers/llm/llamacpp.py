"""The llama.cpp adapter (design document section 10).

Targets ``llama-server``'s native ``/completion`` endpoint. Like the Ollama
adapter, this prefers the native API over the server's OpenAI-compatible shim,
because the native API accepts ``json_schema`` - which llama.cpp compiles to a
GBNF grammar and enforces during decoding - and a per-request ``seed``.

A llama.cpp server usually holds exactly one model, so ``model`` is optional
and reported rather than selected.
"""

from __future__ import annotations

from typing import Any

from ...core.errors import ProviderError
from ...core.interfaces import Capability, HealthStatus
from ..base import GenerationRequest, GenerationResult, LanguageModelProvider, ModelInfo
from ..http import HttpProvider
from ..registry import register_adapter

__all__ = ["LlamaCppProvider"]


@register_adapter("llamacpp", aliases=("llama_cpp", "llama.cpp"))
class LlamaCppProvider(HttpProvider, LanguageModelProvider):
    """Connect to llama.cpp's native HTTP server."""

    default_base_url = "http://localhost:8080"
    health_path = "/health"

    _PASSTHROUGH = (
        "top_k",
        "min_p",
        "typical_p",
        "repeat_penalty",
        "repeat_last_n",
        "presence_penalty",
        "frequency_penalty",
        "mirostat",
        "n_keep",
        "grammar",
    )

    def capabilities(self) -> list[Capability]:
        return [
            Capability("text_generation"),
            Capability("structured_output", {"mechanism": "gbnf_grammar"}),
            Capability("seeded_generation"),
        ]

    # -- models ------------------------------------------------------------- #

    def list_models(self) -> list[ModelInfo]:
        import asyncio

        return asyncio.run(self.list_models_async())

    async def list_models_async(self) -> list[ModelInfo]:
        """Ask ``/v1/models``, falling back to ``/props``.

        Older builds expose only ``/props``; newer ones expose both. Trying the
        richer endpoint first and degrading keeps one adapter working across
        the versions people actually have installed.
        """
        try:
            payload, _ = await self.request_json("GET", "/v1/models", limit_concurrency=False)
        except ProviderError:
            payload = None

        if isinstance(payload, dict) and payload.get("data"):
            return [
                ModelInfo(name=entry.get("id", "?"), details={"owned_by": entry.get("owned_by")})
                for entry in payload["data"]
            ]

        props, _ = await self.request_json("GET", "/props", limit_concurrency=False)
        if isinstance(props, dict):
            name = props.get("model_path") or props.get("default_generation_settings", {}).get(
                "model"
            )
            if name:
                return [
                    ModelInfo(
                        name=str(name).rsplit("/", 1)[-1],
                        context_length=props.get("n_ctx"),
                        details={"model_path": name},
                    )
                ]
        return []

    # -- generation --------------------------------------------------------- #

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        # llama.cpp has no chat-template step on /completion, so the system
        # prompt is prepended rather than sent separately.
        prompt = f"{request.system}\n\n{request.prompt}" if request.system else request.prompt

        body: dict[str, Any] = {
            "prompt": prompt,
            "stream": False,
            "cache_prompt": bool(request.options.get("cache_prompt", True)),
        }
        if request.max_tokens is not None:
            body["n_predict"] = request.max_tokens
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.top_p is not None:
            body["top_p"] = request.top_p
        if request.seed is not None:
            body["seed"] = request.seed % (2**31)
        if request.stop:
            body["stop"] = list(request.stop)
        if request.json_schema is not None:
            body["json_schema"] = request.json_schema
        for key in self._PASSTHROUGH:
            if key in request.options:
                body[key] = request.options[key]

        payload, elapsed_ms = await self.request_json("POST", "/completion", json_body=body)

        if not isinstance(payload, dict) or "content" not in payload:
            raise ProviderError(
                f"provider '{self.id}' returned an unexpected payload shape from /completion"
            )

        return GenerationResult(
            text=payload.get("content", ""),
            model=request.model or self.model or payload.get("model"),
            provider=self.id,
            prompt_tokens=payload.get("tokens_evaluated"),
            completion_tokens=payload.get("tokens_predicted"),
            duration_ms=round(elapsed_ms, 2),
            finish_reason=payload.get("stop_type")
            or ("length" if payload.get("truncated") else None),
            raw={"timings": payload.get("timings")},
        )

    async def health_check(self) -> HealthStatus:
        status = await super().health_check()
        # llama.cpp answers /health with 503 while a model is still loading,
        # which HttpProvider surfaces as an error. Say so in those words.
        if not status.healthy and "503" in status.message:
            return HealthStatus.down(f"{self.id} is starting up or loading a model")
        return status

    def _health_details(self, payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict) and "status" in payload:
            return {"status": payload["status"]}
        return {}
