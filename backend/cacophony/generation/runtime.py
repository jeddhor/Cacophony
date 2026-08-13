"""Run-time services shared by generators (design document sections 43, 63, 76, 86).

A deterministic generator needs nothing but its options and a seed. A generator
that calls a model needs a provider, a prompt compiler, a cache and somewhere
to record what it cost. Rather than thread four arguments through every
interface, they are bundled here and reached through
``GenerationContext.runtime``.

This is also where the numbers section 86 asks for are accumulated: provider
latency, token counts, retry rate, cache hit rate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..core.errors import ProviderNotFoundError
from ..providers.cache import CacheMode, GenerationCache
from ..providers.registry import PROVIDER_REGISTRY, ProviderRegistry
from ..providers.secrets import DEFAULT_RESOLVER, SecretResolver
from .prompt import PromptCompiler

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..providers.base import LanguageModelProvider
    from ..schema.models import ProjectSpec

__all__ = ["GenerationRuntime", "RuntimeStats"]


@dataclass(slots=True)
class RuntimeStats:
    """Provider-side counters for the run inspector (sections 56, 58, 86)."""

    llm_calls: int = 0
    llm_failures: int = 0
    llm_retries: int = 0
    parse_failures: int = 0
    repairs: int = 0
    fallbacks: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    provider_ms: float = 0.0
    records_enriched: int = 0

    def record_call(
        self, *, duration_ms: float | None, prompt: int | None, completion: int | None
    ) -> None:
        self.llm_calls += 1
        if duration_ms:
            self.provider_ms += duration_ms
        if prompt:
            self.prompt_tokens += prompt
        if completion:
            self.completion_tokens += completion

    @property
    def mean_latency_ms(self) -> float:
        return self.provider_ms / self.llm_calls if self.llm_calls else 0.0

    @property
    def retry_rate(self) -> float:
        return self.llm_retries / self.llm_calls if self.llm_calls else 0.0

    @property
    def parse_success_rate(self) -> float:
        """Section 58's "LLM Parse Success" metric."""
        if not self.llm_calls:
            return 1.0
        return max(0.0, (self.llm_calls - self.parse_failures) / self.llm_calls)

    def to_dict(self) -> dict[str, Any]:
        return {
            "llm_calls": self.llm_calls,
            "llm_failures": self.llm_failures,
            "llm_retries": self.llm_retries,
            "parse_failures": self.parse_failures,
            "repairs": self.repairs,
            "fallbacks": self.fallbacks,
            "records_enriched": self.records_enriched,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "mean_latency_ms": round(self.mean_latency_ms, 2),
            "retry_rate": round(self.retry_rate, 4),
            "parse_success_rate": round(self.parse_success_rate, 4),
        }


@dataclass(slots=True)
class GenerationRuntime:
    """Everything a provider-backed generator needs at run time."""

    project: ProjectSpec
    providers: ProviderRegistry = field(default_factory=lambda: PROVIDER_REGISTRY)
    cache: GenerationCache = field(default_factory=lambda: GenerationCache(mode=CacheMode.DISABLED))
    secrets: SecretResolver = field(default_factory=lambda: DEFAULT_RESOLVER)
    prompts: PromptCompiler | None = None
    stats: RuntimeStats = field(default_factory=RuntimeStats)
    #: Bound on how many records one batch-mode call may cover (section 11).
    llm_batch_size: int = 20
    #: Providers found to be unreachable during this run, and why.
    unavailable: dict[str, str] = field(default_factory=dict)

    @classmethod
    def for_project(
        cls,
        project: ProjectSpec,
        *,
        providers: ProviderRegistry | None = None,
        cache: GenerationCache | None = None,
        secrets: SecretResolver | None = None,
        llm_batch_size: int = 20,
        create_providers: bool = True,
    ) -> GenerationRuntime:
        """Build a runtime, instantiating the project's declared providers."""
        registry = providers if providers is not None else ProviderRegistry()
        if providers is None:
            # A fresh registry needs the adapter classes, which register on import.
            registry._adapters = dict(PROVIDER_REGISTRY._adapters)
            registry._aliases = dict(PROVIDER_REGISTRY._aliases)

        resolver = secrets or DEFAULT_RESOLVER
        runtime = cls(
            project=project,
            providers=registry,
            cache=cache if cache is not None else GenerationCache(mode=CacheMode.DISABLED),
            secrets=resolver,
            prompts=PromptCompiler(project),
            llm_batch_size=max(1, llm_batch_size),
        )

        if create_providers:
            for provider_id, spec in project.providers.items():
                if provider_id in registry:
                    continue
                try:
                    registry.create(spec, secrets=resolver)
                except ProviderNotFoundError as exc:
                    # A project may legitimately declare an image or speech
                    # provider whose adapter is not built yet. That must not
                    # stop the language-model fields, or the deterministic ones,
                    # from generating - the field's own failure policy decides
                    # what happens when something actually needs it.
                    runtime.mark_unavailable(provider_id, str(exc))
        return runtime

    # -- availability ------------------------------------------------------- #

    def mark_unavailable(self, provider_id: str, reason: str) -> None:
        """Record that a provider is unreachable, and stop calling it.

        Without this, a ten-million-record run against a server that is down
        would make ten million connection attempts, each with its own retry
        ladder. The first refusal is enough: the field's failure policy takes
        over for every record after it, and the run either degrades cleanly or
        aborts once instead of thirty million times.
        """
        self.unavailable.setdefault(provider_id, reason)

    def is_unavailable(self, provider_id: str) -> str | None:
        return self.unavailable.get(provider_id)

    def reset_availability(self) -> None:
        self.unavailable.clear()

    # -- lookups ------------------------------------------------------------ #

    def language_model(self, provider_id: str | None) -> LanguageModelProvider:
        """Resolve a language-model provider, or explain what is missing.

        When a field names no provider and the project declares exactly one
        language model, that one is used - requiring every field to repeat the
        same provider id would be noise.
        """
        from ..providers.base import LanguageModelProvider as LanguageModel

        if provider_id:
            provider = self.providers.get(provider_id)
            if not isinstance(provider, LanguageModel):
                raise ProviderNotFoundError(
                    f"provider '{provider_id}' is a {provider.kind} provider, but this "
                    "field needs a language model."
                )
            return provider

        candidates = [p for p in self.providers.instances() if isinstance(p, LanguageModel)]
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            raise ProviderNotFoundError(
                "No language-model provider is configured. Add one under 'providers:' "
                "with type: language_model, or set 'on_unavailable: placeholder' on the field."
            )
        names = ", ".join(sorted(p.id for p in candidates))
        raise ProviderNotFoundError(
            f"This project configures several language models ({names}), so the field "
            "must name one with 'provider:'."
        )

    @property
    def prompt_compiler(self) -> PromptCompiler:
        if self.prompts is None:
            self.prompts = PromptCompiler(self.project)
        return self.prompts

    async def aclose(self) -> None:
        await self.providers.aclose()
        self.cache.close()

    def summary(self) -> dict[str, Any]:
        return {
            "providers": self.stats.to_dict(),
            "cache": self.cache.describe(),
            "unavailable": dict(self.unavailable),
        }
