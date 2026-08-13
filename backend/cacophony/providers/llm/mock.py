"""A deterministic mock language model (design document section 88).

    "Integration Tests ... Use mock providers."

This is not a toy. It is the only way to test the whole
schema -> prompt -> generation -> parse -> validate -> export path without a
GPU in the loop, and it is what lets the test suite assert on *behaviour*
rather than on whether a real model happened to co-operate that afternoon.

It reads the JSON schema it is handed and synthesises a conforming document,
seeded from the request, so:

* the structured-output stage gets realistic input rather than a canned string;
* the same request produces the same answer, so tests are not flaky;
* ``failure_rate`` and ``malformed_rate`` let the retry ladder of section 66 be
  exercised deliberately rather than hoped for.

It is registered as a normal adapter, so a project can point at it to rehearse
a run's shape and cost before pointing at a real model.
"""

from __future__ import annotations

import json
import random
from typing import Any

from ...core.errors import ProviderError, ProviderUnavailableError
from ...core.interfaces import Capability, HealthStatus
from ...core.seeds import derive_seed
from ..base import GenerationRequest, GenerationResult, LanguageModelProvider, ModelInfo
from ..registry import register_adapter

__all__ = ["MockLanguageModelProvider"]

_LOREM = (
    "resolved the issue after confirming the configuration",
    "escalated to the platform team for further review",
    "restarted the affected service and verified recovery",
    "applied the documented workaround and monitored overnight",
    "reproduced the fault in staging before rolling back",
    "updated the access policy and confirmed with the requester",
    "replaced the failing component under warranty",
    "cleared the cache and validated the result end to end",
)

_SUBJECTS = (
    "Northwind",
    "Blue Harbor",
    "Ridgeline",
    "Copperfield",
    "Silverbrook",
    "Fairmount",
    "Westgate",
    "Alder Creek",
)


@register_adapter("mock", aliases=("mock_llm", "fake"))
class MockLanguageModelProvider(LanguageModelProvider):
    """A language model that never leaves the process.

    Options:
        ``failure_rate``    fraction of calls that raise (default ``0.0``)
        ``malformed_rate``  fraction that return unparseable text
        ``latency_ms``      simulated per-call delay
        ``healthy``         whether ``health_check`` succeeds
    """

    adapter_name = "mock"

    def __init__(self, provider_id: str, config: dict[str, Any] | None = None, **_: Any) -> None:
        super().__init__(provider_id, config)
        self.model = self.config.get("model") or "mock-1"
        self.failure_rate = float(self.config.get("failure_rate") or 0.0)
        self.malformed_rate = float(self.config.get("malformed_rate") or 0.0)
        self.latency_ms = float(self.config.get("latency_ms") or 0.0)
        self.healthy = bool(self.config.get("healthy", True))
        #: Literal responses to return in order, cycling. Used by tests that
        #: need an exact script rather than a probability.
        self.responses: list[str] = list(self.config.get("responses") or [])
        #: Every request seen, so tests can assert on prompts and batching.
        self.calls: list[GenerationRequest] = []

    def capabilities(self) -> list[Capability]:
        return [
            Capability("text_generation"),
            Capability("structured_output", {"mechanism": "synthesised"}),
            Capability("seeded_generation"),
        ]

    async def health_check(self) -> HealthStatus:
        if not self.healthy:
            return HealthStatus.down(f"{self.id} is configured as unhealthy")
        return HealthStatus.up(f"{self.id} is a mock provider", latency_ms=0.0, version="mock")

    def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(name=self.model, family="mock", parameter_size="0B")]

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        self.calls.append(request)

        if self.latency_ms:
            import asyncio

            await asyncio.sleep(self.latency_ms / 1000.0)

        # The answer depends on the prompt as well as the seed. A real model
        # given a repair prompt answers differently; a mock keyed only on the
        # seed would return the identical broken output forever, and the retry
        # ladder could never be shown to work.
        seed = derive_seed(request.seed or 0, request.prompt)
        rng = random.Random(seed)

        # Failure and malformation are drawn from a *separate* stream, so that
        # turning them on does not change the content the model would otherwise
        # have produced. Tests can then isolate one variable at a time.
        fault_rng = random.Random(derive_seed(seed, "faults"))
        if self.failure_rate and fault_rng.random() < self.failure_rate:
            raise ProviderUnavailableError(f"provider '{self.id}': simulated failure")

        if self.responses:
            text = self.responses[(len(self.calls) - 1) % len(self.responses)]
        elif self.malformed_rate and fault_rng.random() < self.malformed_rate:
            text = "Certainly! Here is the record you asked for:\n{ this is not, valid json "
        elif request.json_schema is not None:
            text = json.dumps(_synthesise(request.json_schema, rng))
        else:
            text = f"{rng.choice(_SUBJECTS)} {rng.choice(_LOREM)}."

        return GenerationResult(
            text=text,
            model=request.model or self.model,
            provider=self.id,
            prompt_tokens=max(1, len(request.prompt) // 4),
            completion_tokens=max(1, len(text) // 4),
            duration_ms=self.latency_ms,
            finish_reason="stop",
        )

    def reset(self) -> None:
        self.calls.clear()


def _synthesise(schema: dict[str, Any], rng: random.Random) -> Any:
    """Build a value conforming to a (subset of) JSON Schema."""
    if schema.get("enum"):
        return rng.choice(list(schema["enum"]))

    schema_type = schema.get("type", "object")

    if schema_type == "object":
        properties: dict[str, Any] = schema.get("properties") or {}
        return {name: _synthesise(subschema, rng) for name, subschema in properties.items()}

    if schema_type == "array":
        items = schema.get("items") or {"type": "string"}
        low = int(schema.get("minItems", 1))
        high = max(low, int(schema.get("maxItems", low + 2)))
        return [_synthesise(items, rng) for _ in range(rng.randint(low, high))]

    if schema_type == "integer":
        low_int = int(schema.get("minimum", 0))
        high_int = int(schema.get("maximum", low_int + 100))
        return rng.randint(low_int, max(low_int, high_int))

    if schema_type == "number":
        low_float = float(schema.get("minimum", 0.0))
        high_float = float(schema.get("maximum", low_float + 100.0))
        return round(rng.uniform(low_float, max(low_float, high_float)), 2)

    if schema_type == "boolean":
        return rng.random() < 0.5

    if schema_type == "null":
        return None

    return _synthesise_string(schema, rng)


def _synthesise_string(schema: dict[str, Any], rng: random.Random) -> str:
    """Produce a string that satisfies the schema's format and length bounds."""
    fmt = schema.get("format")
    if fmt == "date":
        return f"20{rng.randint(20, 26):02d}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
    if fmt == "date-time":
        return (
            f"20{rng.randint(20, 26):02d}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
            f"T{rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d}"
        )
    if fmt == "email":
        return f"{rng.choice(_SUBJECTS).lower().replace(' ', '.')}@example.com"
    if fmt == "uuid":
        return str(__import__("uuid").UUID(int=rng.getrandbits(128), version=4))

    text = f"{rng.choice(_SUBJECTS)} {rng.choice(_LOREM)}."
    minimum = int(schema.get("minLength", 0))
    maximum = schema.get("maxLength")

    while len(text) < minimum:
        text = f"{text} {rng.choice(_LOREM)}."
    if maximum is not None and len(text) > int(maximum):
        text = text[: int(maximum)].rstrip(" ,.") or "x" * max(minimum, 1)
        # Trimming must not push us back under the minimum.
        if len(text) < minimum:
            text = text.ljust(minimum, ".")
    return text


def unreachable_provider(provider_id: str = "unreachable") -> MockLanguageModelProvider:
    """A mock that always fails, for exercising failure paths."""
    provider = MockLanguageModelProvider(provider_id, {"failure_rate": 1.0, "healthy": False})
    return provider


def _raise_unreachable() -> None:  # pragma: no cover - documentation of intent
    raise ProviderError("mock providers never reach the network")
