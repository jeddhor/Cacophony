"""The language-model generator (design document sections 8, 9, 11).

    biography:
      type: text
      generator: llm
      semantic: >
        A short fictional professional biography consistent with the
        employee's department, title, age and location.

There is no prompt in that declaration, and there should not be. The user says
what the field means; the prompt compiler (section 12) writes the instruction
from the meaning plus the field's type, constraints, dependencies and examples.

This class is mostly a *declaration*. The work happens in
:mod:`cacophony.generation.enrichment`, because the interesting modes cover
several fields or several records in one call and therefore cannot be driven
from a single field's ``generate``. The engine groups these fields and hands
them to the enricher; ``generate`` remains implemented so the generator still
works standalone, one field at a time, outside the engine.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...core.errors import GenerationError, ProviderError
from ...core.interfaces import GeneratedValue, Generator
from ..registry import register_generator
from .base import OptionsMixin
from .deferred import PlaceholderMixin

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from ...core.context import GenerationContext

__all__ = ["LanguageModelGenerator"]

#: Section 11's modes.
MODES = ("per_field", "per_record", "batch", "expansion")


@register_generator("llm", aliases=("language_model", "ai", "gpt"))
class LanguageModelGenerator(OptionsMixin, PlaceholderMixin, Generator):
    """Generate semantic content from field and record context.

    Options:
        ``provider``        id of a configured language-model provider; optional
                            when the project declares exactly one
        ``mode``            ``per_field``, ``per_record``, ``batch`` or
                            ``expansion`` (section 11)
        ``context``         fields the model may see; defaults to every
                            deterministic field of the record
        ``max_tokens``      completion length cap
        ``temperature``     sampling temperature
        ``on_unavailable``  ``error`` (default), ``placeholder`` or ``null``

    ``expansion`` is a synonym for ``per_record`` here rather than a separate
    code path, because contextual expansion is what every mode already does:
    the engine produces deterministic fields first and asks the model to enrich
    them (section 11, "This will often be the optimal strategy").
    """

    requires_provider = "language_model"
    cost_class = "llm"
    deterministic = False

    def prepare(self) -> None:
        self.provider = self.opt_str("provider", None, "provider_id")
        self.mode = self.opt_choice("mode", MODES, "per_record")
        if self.mode == "expansion":
            self.mode = "per_record"
        self.max_tokens = self.opt_int("max_tokens", 400)
        self.temperature = self.opt_float("temperature", 0.8)
        self.on_unavailable = self.opt_choice(
            "on_unavailable", ("error", "placeholder", "null"), "error"
        )
        self.context_fields = tuple(self.opt_list("context", [], "inputs"))

        if self.temperature is not None and not 0.0 <= self.temperature <= 2.0:
            raise self._fail("option 'temperature' must be between 0.0 and 2.0")
        if self.max_tokens is not None and self.max_tokens < 1:
            raise self._fail("option 'max_tokens' must be at least 1")

    def dependencies(self) -> Sequence[str]:
        return self.context_fields

    # -- standalone path ---------------------------------------------------- #

    async def generate(self, context: GenerationContext) -> GeneratedValue:
        """Produce this one field for this one record.

        The engine does not take this path - it batches instead - so this
        exists for direct use, for tests, and for any caller holding a single
        context.
        """
        runtime = context.runtime
        if runtime is None:
            return GeneratedValue.of(
                self._unavailable(
                    context,
                    "this field needs a language model, but the project declares no "
                    "providers. Add one under 'providers:' with type: language_model, "
                    "or set 'on_unavailable: placeholder' on the field.",
                )
            )

        from ..enrichment import Enricher, EnrichmentGroup

        compiled = self._compiled_field(context)
        try:
            prompt = runtime.prompt_compiler.compile(
                context.entity, [compiled], mode="per_record", context_fields=self._visible(context)
            )
            group = EnrichmentGroup(
                provider_id=self.provider,
                mode="per_record",
                fields=(compiled,),
                prompt=prompt,
                context_fields=self._visible(context),
                on_unavailable=self.on_unavailable,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )
            provider = runtime.language_model(self.provider)
        except ProviderError as exc:
            return GeneratedValue.of(self._unavailable(context, str(exc)))

        enricher = Enricher(runtime)
        parsed, provenance = await enricher._ask(
            group,
            provider,
            known={
                name: context.current_record.get(name)
                for name in self._visible(context)
                if context.current_record.get(name) is not None
            },
            seed=context.seed,
            expected=1,
            location=context.location,
        )
        if parsed is None:
            return GeneratedValue.of(
                self._unavailable(context, str(provenance.extra.get("error", "")))
            )
        return GeneratedValue(value=parsed[0].values.get(compiled.name), provenance=provenance)

    def _visible(self, context: GenerationContext) -> tuple[str, ...]:
        if self.context_fields:
            return self.context_fields
        return tuple(name for name, value in context.current_record.items() if value is not None)

    def _compiled_field(self, context: GenerationContext) -> Any:
        from ...schema.plan import CompiledField

        assert context.field is not None
        return CompiledField(
            name=context.field.name,
            spec=context.field,
            generator=self,
            dependencies=tuple(self.context_fields),
        )

    def _unavailable(self, context: GenerationContext, reason: str) -> Any:
        if self.on_unavailable == "null":
            return None
        if self.on_unavailable == "placeholder":
            return self.placeholder(context)
        # The engine prefixes the field location when it reports a field
        # failure, so adding it here produced "e.words: e.words: ...".
        raise GenerationError(reason)

    # -- presentation ------------------------------------------------------- #

    def raw_placeholder(self, context: GenerationContext) -> Any:
        meaning = (self.field.meaning if self.field else None) or "generated text"
        summary = " ".join(meaning.split())[:80]
        return f"[LLM PLACEHOLDER] {summary} (record {context.record_index})"

    def describe(self) -> str:
        where = f"@{self.provider}" if self.provider else ""
        return f"llm{where}({self.mode})"
