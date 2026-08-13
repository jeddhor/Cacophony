"""The Ollama adapter (design document section 10).

Talks to an Ollama server's native API rather than its OpenAI-compatible shim,
because the native API exposes two things Cacophony wants and the shim does
not: `format` as a JSON schema, which constrains decoding so malformed JSON
becomes impossible rather than merely unlikely, and `options.seed`, which is
what makes a language-model field as reproducible as the model allows.
"""

from __future__ import annotations

from typing import Any

from ...core.errors import ProviderError
from ...core.interfaces import Capability
from ..base import GenerationRequest, GenerationResult, LanguageModelProvider, ModelInfo
from ..http import HttpProvider
from ..registry import register_adapter

__all__ = ["OllamaProvider"]


@register_adapter("ollama")
class OllamaProvider(HttpProvider, LanguageModelProvider):
    """Connect to an Ollama server."""

    default_base_url = "http://localhost:11434"
    health_path = "/api/tags"

    #: Ollama nests sampling parameters under ``options``.
    _OPTION_KEYS = (
        "top_k",
        "repeat_penalty",
        "presence_penalty",
        "frequency_penalty",
        "num_ctx",
        "num_gpu",
        "mirostat",
    )

    def capabilities(self) -> list[Capability]:
        return [
            Capability("text_generation"),
            Capability("structured_output", {"mechanism": "json_schema"}),
            Capability("seeded_generation"),
            Capability("model_listing"),
        ]

    # -- models ------------------------------------------------------------- #

    def list_models(self) -> list[ModelInfo]:
        """Synchronous by interface contract (section 10), so it blocks briefly."""
        import asyncio

        return asyncio.run(self.list_models_async())

    async def list_models_async(self) -> list[ModelInfo]:
        payload, _ = await self.request_json("GET", "/api/tags", limit_concurrency=False)
        models = payload.get("models", []) if isinstance(payload, dict) else []
        return [
            ModelInfo(
                name=entry.get("name", entry.get("model", "?")),
                family=(entry.get("details") or {}).get("family"),
                parameter_size=(entry.get("details") or {}).get("parameter_size"),
                quantization=(entry.get("details") or {}).get("quantization_level"),
                digest=entry.get("digest"),
                details={"size_bytes": entry.get("size")},
            )
            for entry in models
        ]

    # -- generation --------------------------------------------------------- #

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        model = request.model or self.model
        if not model:
            raise ProviderError(
                f"provider '{self.id}' has no model configured. Set 'model:' on the "
                "provider or on the field's generator."
            )

        options: dict[str, Any] = {}
        if request.temperature is not None:
            options["temperature"] = request.temperature
        if request.top_p is not None:
            options["top_p"] = request.top_p
        if request.max_tokens is not None:
            options["num_predict"] = request.max_tokens
        if request.seed is not None:
            # Ollama wants a 32-bit seed; Cacophony's are 64-bit.
            options["seed"] = request.seed % (2**31)
        if request.stop:
            options["stop"] = list(request.stop)
        for key in self._OPTION_KEYS:
            if key in request.options:
                options[key] = request.options[key]

        body: dict[str, Any] = {
            "model": model,
            "prompt": request.prompt,
            "stream": False,
            "options": options,
        }
        if request.system:
            body["system"] = request.system
        if request.json_schema is not None:
            # Constrained decoding: the server enforces the shape, so the
            # structured-output stage downstream is a safety net rather than
            # the only line of defence.
            body["format"] = request.json_schema
        if "keep_alive" in request.options:
            body["keep_alive"] = request.options["keep_alive"]

        payload, elapsed_ms = await self.request_json("POST", "/api/generate", json_body=body)

        if not isinstance(payload, dict) or "response" not in payload:
            raise ProviderError(
                f"provider '{self.id}' returned an unexpected payload shape from /api/generate"
            )

        return GenerationResult(
            text=payload.get("response", ""),
            model=payload.get("model", model),
            provider=self.id,
            prompt_tokens=payload.get("prompt_eval_count"),
            completion_tokens=payload.get("eval_count"),
            duration_ms=round(elapsed_ms, 2),
            finish_reason=payload.get("done_reason"),
            raw={"total_duration": payload.get("total_duration")},
        )

    def _health_details(self, payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict):
            models = payload.get("models") or []
            return {"models_available": len(models)}
        return {}
