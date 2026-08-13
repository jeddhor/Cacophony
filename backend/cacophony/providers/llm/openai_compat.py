"""The OpenAI-compatible adapter (design document section 10).

    "Provide optional compatibility with any server exposing a standard
    chat/completions interface. This could indirectly support many inference
    systems without coupling Cacophony to them."

That is the whole point of this adapter, and it is also its difficulty: the
interface is a de facto standard, not a real one. vLLM, LM Studio, TGI,
llama.cpp's shim, LocalAI and the hosted services all accept
``/v1/chat/completions`` and all disagree about structured output.

So structured output is negotiated rather than assumed:

``auto``          try ``json_schema``, fall back to ``json_object``, then to
                  prompt-only instruction - decided once per provider, per run
``json_schema``   insist on it
``json_object``   ask only for valid JSON, leave the shape to validation
``none``          send no structured-output hint at all

``auto`` is the default because it is the only setting that works on a server
whose capabilities the user has not told us about.
"""

from __future__ import annotations

from typing import Any

from ...core.errors import ProviderError
from ...core.interfaces import Capability
from ..base import GenerationRequest, GenerationResult, LanguageModelProvider, ModelInfo
from ..http import HttpProvider
from ..registry import register_adapter

__all__ = ["OpenAICompatibleProvider"]

_MODES = ("auto", "json_schema", "json_object", "none")


@register_adapter("openai_compatible", aliases=("openai", "vllm", "lmstudio", "tgi"))
class OpenAICompatibleProvider(HttpProvider, LanguageModelProvider):
    """Connect to any server exposing a standard chat/completions interface."""

    default_base_url = "http://localhost:8000"
    health_path = "/v1/models"

    _PASSTHROUGH = ("top_k", "presence_penalty", "frequency_penalty", "repetition_penalty", "n")

    def __init__(
        self, provider_id: str, config: dict[str, Any] | None = None, **kwargs: Any
    ) -> None:
        super().__init__(provider_id, config, **kwargs)
        mode = str(self.config.get("structured_output", "auto"))
        if mode not in _MODES:
            raise ProviderError(
                f"provider '{self.id}': structured_output must be one of {', '.join(_MODES)}, "
                f"got {mode!r}"
            )
        self.structured_output = mode
        #: What we have learned actually works on this endpoint.
        self._negotiated: str | None = None if mode == "auto" else mode
        self.completions_path = str(self.config.get("completions_path", "/v1/chat/completions"))

    def capabilities(self) -> list[Capability]:
        capabilities = [Capability("text_generation"), Capability("model_listing")]
        if self.structured_output != "none":
            capabilities.append(
                Capability("structured_output", {"mechanism": self._negotiated or "negotiated"})
            )
        return capabilities

    # -- models ------------------------------------------------------------- #

    def list_models(self) -> list[ModelInfo]:
        import asyncio

        return asyncio.run(self.list_models_async())

    async def list_models_async(self) -> list[ModelInfo]:
        payload, _ = await self.request_json("GET", "/v1/models", limit_concurrency=False)
        entries = payload.get("data", []) if isinstance(payload, dict) else []
        return [
            ModelInfo(
                name=entry.get("id", "?"),
                details={"owned_by": entry.get("owned_by"), "created": entry.get("created")},
            )
            for entry in entries
        ]

    # -- generation --------------------------------------------------------- #

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        model = request.model or self.model
        if not model:
            raise ProviderError(
                f"provider '{self.id}' has no model configured. Set 'model:' on the "
                "provider or on the field's generator."
            )

        attempts = self._structured_output_ladder(request)
        last_error: ProviderError | None = None

        for mechanism in attempts:
            body = self._build_body(request, model, mechanism)
            try:
                payload, elapsed_ms = await self.request_json(
                    "POST", self.completions_path, json_body=body
                )
            except ProviderError as exc:
                # A 400 here usually means "I do not understand response_format",
                # which is a capability answer rather than a failure. Remember it
                # and drop down the ladder.
                if _looks_like_unsupported_format(exc) and mechanism != attempts[-1]:
                    last_error = exc
                    continue
                raise

            self._negotiated = mechanism
            return self._parse(payload, model, elapsed_ms)

        raise last_error or ProviderError(f"provider '{self.id}' produced no usable response")

    def _structured_output_ladder(self, request: GenerationRequest) -> list[str]:
        """Which structured-output mechanisms to try, in order."""
        if request.json_schema is None or self.structured_output == "none":
            return ["none"]
        if self._negotiated is not None:
            return [self._negotiated]
        return ["json_schema", "json_object", "none"]

    def _build_body(self, request: GenerationRequest, model: str, mechanism: str) -> dict[str, Any]:
        messages: list[dict[str, str]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.append({"role": "user", "content": request.prompt})

        body: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.top_p is not None:
            body["top_p"] = request.top_p
        if request.max_tokens is not None:
            body["max_tokens"] = request.max_tokens
        if request.seed is not None:
            body["seed"] = request.seed % (2**31)
        if request.stop:
            body["stop"] = list(request.stop)
        for key in self._PASSTHROUGH:
            if key in request.options:
                body[key] = request.options[key]

        if mechanism == "json_schema" and request.json_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "cacophony_record",
                    "schema": request.json_schema,
                    "strict": True,
                },
            }
        elif mechanism == "json_object":
            body["response_format"] = {"type": "json_object"}

        return body

    def _parse(self, payload: Any, model: str, elapsed_ms: float) -> GenerationResult:
        if not isinstance(payload, dict) or not payload.get("choices"):
            raise ProviderError(
                f"provider '{self.id}' returned no choices from {self.completions_path}"
            )
        choice = payload["choices"][0]
        message = choice.get("message") or {}
        usage = payload.get("usage") or {}

        return GenerationResult(
            text=message.get("content") or "",
            model=payload.get("model", model),
            provider=self.id,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            duration_ms=round(elapsed_ms, 2),
            finish_reason=choice.get("finish_reason"),
            raw={"structured_output": self._negotiated},
        )

    def _health_details(self, payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict):
            return {"models_available": len(payload.get("data") or [])}
        return {}


def _looks_like_unsupported_format(exc: ProviderError) -> bool:
    """Whether an error reads like "I do not support that response_format"."""
    text = str(exc).lower()
    if "http 400" not in text and "http 422" not in text and "http 501" not in text:
        return False
    return any(
        marker in text
        for marker in ("response_format", "json_schema", "not supported", "unsupported", "unknown")
    )
