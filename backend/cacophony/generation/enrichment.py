"""Language-model enrichment (design document sections 11, 13, 65, 66, 76).

Section 11 describes four modes. They differ only in how many fields and how
many records one call covers:

``per_field``   one call per field per record - highest control, most expensive
``per_record``  one call per record, covering every AI field in that layer
``batch``       one call covering many records - "much faster"
``expansion``   contextual expansion: deterministic fields first, then enrich

Section 11 calls contextual expansion "often the optimal strategy", and it is
what Cacophony does by default in every mode: the engine has already produced
every deterministic field before this module is called, so the model is asked
to enrich a partly-built record rather than invent one from nothing. That is
what keeps a generated biography consistent with a name and a hire date that
Faker and a distribution decided.

The retry ladder is section 66's, exactly:

    attempt 1   normal generation
    attempt 2   repair prompt, quoting what came back and what was wrong
    attempt 3   more explicit schema prompt, temperature lowered
    attempt 4   fallback generator, or mark failed

"Never permit infinite retry loops" - so the ladder has a fixed length and each
rung is tried once.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..core.errors import GenerationError, ProviderError, ProviderUnavailableError
from ..core.provenance import FieldProvenance
from ..providers.base import GenerationRequest
from ..providers.cache import cache_key
from .prompt import CompiledPrompt
from .structured import ParsedRecord, StructuredOutputError, parse_record, parse_records

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from ..core.context import GenerationContext
    from ..core.record import GeneratedRecord
    from ..providers.base import LanguageModelProvider
    from ..schema.plan import CompiledEntity, CompiledField
    from .runtime import GenerationRuntime

__all__ = ["Enricher", "EnrichmentGroup", "plan_enrichment"]

#: Rungs of section 66's ladder, in order.
_LADDER = ("normal", "repair", "explicit", "fallback")


@dataclass(slots=True)
class EnrichmentGroup:
    """Fields that can be produced by one kind of call to one provider."""

    provider_id: str | None
    mode: str
    fields: tuple[CompiledField, ...]
    prompt: CompiledPrompt
    context_fields: tuple[str, ...]
    on_unavailable: str = "error"
    temperature: float | None = None
    max_tokens: int | None = None

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(compiled.name for compiled in self.fields)

    def describe(self) -> str:
        where = self.provider_id or "<default>"
        return f"{self.mode}:{where}[{', '.join(self.names)}]"


def plan_enrichment(
    entity: CompiledEntity,
    fields: Sequence[CompiledField],
    runtime: GenerationRuntime,
    *,
    batch_size: int = 1,
) -> list[EnrichmentGroup]:
    """Group a layer's language-model fields into as few calls as possible.

    Fields in one layer are independent by construction, so any two that share
    a provider and a mode can travel in the same call. That is the difference
    between one request per record and one request per field per record.
    """
    buckets: dict[tuple[str | None, str], list[CompiledField]] = {}
    for compiled in fields:
        generator = compiled.generator
        key = (getattr(generator, "provider", None), getattr(generator, "mode", "per_record"))
        buckets.setdefault(key, []).append(compiled)

    groups: list[EnrichmentGroup] = []
    compiler = runtime.prompt_compiler

    for (provider_id, mode), members in buckets.items():
        # per_field means what it says: one call per field, so one group each.
        chunks = [[member] for member in members] if mode == "per_field" else [members]
        for chunk in chunks:
            context_fields = _context_for(entity, chunk)
            effective_mode = "batch" if mode == "batch" else "per_record"
            groups.append(
                EnrichmentGroup(
                    provider_id=provider_id,
                    mode=mode,
                    fields=tuple(chunk),
                    prompt=compiler.compile(
                        entity.spec,
                        chunk,
                        mode=effective_mode,
                        context_fields=context_fields,
                        batch_size=batch_size if mode == "batch" else 1,
                    ),
                    context_fields=context_fields,
                    on_unavailable=getattr(chunk[0].generator, "on_unavailable", "error"),
                    temperature=getattr(chunk[0].generator, "temperature", None),
                    max_tokens=getattr(chunk[0].generator, "max_tokens", None),
                )
            )
    return groups


def _context_for(entity: CompiledEntity, fields: Sequence[CompiledField]) -> tuple[str, ...]:
    """Which already-generated fields the model should be shown.

    A field's declared dependencies come first. When a field declares none, the
    whole already-generated record is offered instead: a model asked to write a
    biography with no context will invent one that contradicts the record it
    belongs to, which is precisely the failure section 14 exists to prevent.
    """
    declared: list[str] = []
    for compiled in fields:
        declared.extend(compiled.dependencies)

    if declared:
        return tuple(dict.fromkeys(declared))

    produced = {compiled.name for compiled in fields}
    generated_before: list[str] = []
    for compiled in entity.fields:
        if compiled.name in produced:
            continue
        if type(compiled.generator).requires_provider:
            continue  # not necessarily produced yet
        generated_before.append(compiled.name)
    return tuple(generated_before)


@dataclass(slots=True)
class _Attempt:
    """One rung of the ladder, and what it produced."""

    rung: str
    text: str = ""
    error: str = ""


class Enricher:
    """Executes enrichment groups against a language-model provider."""

    def __init__(self, runtime: GenerationRuntime, *, max_attempts: int = 3) -> None:
        self.runtime = runtime
        # The ladder's last rung is fallback/fail, so attempts are capped at
        # three model calls however large max_attempts is set (section 66).
        self.max_attempts = max(1, min(max_attempts, len(_LADDER) - 1))

    # -- entry point -------------------------------------------------------- #

    async def run(
        self,
        group: EnrichmentGroup,
        records: Sequence[GeneratedRecord],
        contexts: Sequence[GenerationContext],
    ) -> None:
        """Fill ``group``'s fields into ``records``, in place."""
        if not records:
            return

        try:
            provider = self.runtime.language_model(group.provider_id)
        except ProviderError as exc:
            self._degrade(group, records, contexts, reason=str(exc))
            return

        # A provider already known to be down in this run is not asked again.
        down = self.runtime.is_unavailable(provider.id)
        if down is not None:
            self._degrade(group, records, contexts, reason=down)
            return

        if group.mode == "batch":
            await self._run_batched(group, provider, records, contexts)
        else:
            await asyncio.gather(
                *(
                    self._run_single(group, provider, record, context)
                    for record, context in zip(records, contexts, strict=True)
                )
            )
        self.runtime.stats.records_enriched += len(records)

    # -- per-record and per-field ------------------------------------------- #

    async def _run_single(
        self,
        group: EnrichmentGroup,
        provider: LanguageModelProvider,
        record: GeneratedRecord,
        context: GenerationContext,
    ) -> None:
        known = self._known_values(group, record)
        seed = context.seeds.descend("llm", *group.names).seed

        parsed, provenance = await self._ask(
            group,
            provider,
            known=known,
            seed=seed,
            expected=1,
            location=f"{record.entity}#{context.record_index}",
        )

        if parsed is None:
            self._degrade(group, [record], [context], reason=provenance.extra.get("error", ""))
            return

        self._apply(group, record, parsed[0], provenance)

    # -- batch -------------------------------------------------------------- #

    async def _run_batched(
        self,
        group: EnrichmentGroup,
        provider: LanguageModelProvider,
        records: Sequence[GeneratedRecord],
        contexts: Sequence[GenerationContext],
    ) -> None:
        size = max(1, min(self.runtime.llm_batch_size, group.prompt.batch_size or 1))
        chunks = [
            (records[start : start + size], contexts[start : start + size])
            for start in range(0, len(records), size)
        ]
        await asyncio.gather(
            *(
                self._run_chunk(group, provider, chunk, chunk_contexts)
                for chunk, chunk_contexts in chunks
            )
        )

    async def _run_chunk(
        self,
        group: EnrichmentGroup,
        provider: LanguageModelProvider,
        records: Sequence[GeneratedRecord],
        contexts: Sequence[GenerationContext],
    ) -> None:
        known = [self._known_values(group, record) for record in records]
        seed = contexts[0].seeds.descend("llm-batch", *group.names, len(records)).seed

        parsed, provenance = await self._ask(
            group,
            provider,
            known=known,
            seed=seed,
            expected=len(records),
            location=f"{records[0].entity} batch of {len(records)}",
        )

        if parsed is None:
            self._degrade(group, records, contexts, reason=provenance.extra.get("error", ""))
            return

        for index, (record, context) in enumerate(zip(records, contexts, strict=True)):
            if index < len(parsed):
                self._apply(group, record, parsed[index], provenance)
            else:
                # A short batch is not a failure of the records that did arrive;
                # only the remainder falls through to the unavailability policy.
                self._degrade(group, [record], [context], reason="the model returned a short batch")

    # -- the ladder --------------------------------------------------------- #

    async def _ask(
        self,
        group: EnrichmentGroup,
        provider: LanguageModelProvider,
        *,
        known: Any,
        seed: int,
        expected: int,
        location: str,
    ) -> tuple[list[ParsedRecord] | None, FieldProvenance]:
        """Walk section 66's ladder until something parses, or it runs out."""
        prompt_text = group.prompt.render(known)
        provenance = FieldProvenance(
            generator="llm",
            seed=seed,
            provider=provider.id,
            model=getattr(provider, "model", None),
            prompt_version=group.prompt.version,
        )

        history: list[_Attempt] = []

        for attempt_index in range(self.max_attempts):
            # Re-checked each attempt: sibling records in the same gather may
            # have found the provider down since this one was scheduled.
            down = self.runtime.is_unavailable(provider.id)
            if down is not None:
                provenance.extra["error"] = down
                return None, provenance

            rung = _LADDER[attempt_index]
            provenance.attempts = attempt_index + 1

            request = self._build_request(
                group, provider, prompt_text, seed=seed, rung=rung, history=history
            )
            key = self._cache_key(provider, request, group)

            cached = self.runtime.cache.get(key)
            if cached is not None:
                text = str(cached.get("text", ""))
                provenance.cached = True
            else:
                provenance.cached = False
                try:
                    result = await provider.generate(request)
                except ProviderUnavailableError as exc:
                    # The server is not there. Rewording the prompt will not
                    # bring it back, so the ladder is abandoned immediately and
                    # the provider is taken out of circulation for this run.
                    self.runtime.stats.llm_failures += 1
                    self.runtime.mark_unavailable(provider.id, str(exc))
                    provenance.extra["error"] = str(exc)
                    return None, provenance
                except ProviderError as exc:
                    history.append(_Attempt(rung=rung, error=str(exc)))
                    self.runtime.stats.llm_failures += 1
                    if attempt_index + 1 < self.max_attempts:
                        self.runtime.stats.llm_retries += 1
                        continue
                    provenance.extra["error"] = str(exc)
                    return None, provenance

                self.runtime.stats.record_call(
                    duration_ms=result.duration_ms,
                    prompt=result.prompt_tokens,
                    completion=result.completion_tokens,
                )
                text = result.text
                provenance.model = result.model
                self.runtime.cache.put(
                    key, {"text": text}, provider=provider.id, model=result.model
                )

            provenance.prompt = request.prompt
            provenance.raw_response = text

            try:
                parsed = (
                    parse_records(text, group.fields, expected=expected)
                    if expected > 1
                    else [parse_record(text, group.fields)]
                )
            except StructuredOutputError as exc:
                self.runtime.stats.parse_failures += 1
                history.append(_Attempt(rung=rung, text=text, error=str(exc)))
                if attempt_index + 1 < self.max_attempts:
                    self.runtime.stats.llm_retries += 1
                    continue
                provenance.extra["error"] = f"{location}: {exc}"
                return None, provenance

            # A short batch is a wrong answer to a well-formed question, so it
            # goes back up the ladder like any other. Only when the rungs run
            # out does the caller deal with the remainder.
            short = len(parsed) < expected
            invalid = [record for record in parsed if not record.ok]

            if (short or invalid) and attempt_index + 1 < self.max_attempts:
                self.runtime.stats.llm_retries += 1
                problem = (
                    f"you returned {len(parsed)} records but exactly {expected} were required"
                    if short
                    else invalid[0].problems
                )
                history.append(_Attempt(rung=rung, text=text, error=problem))
                continue

            repairs = sum(len(record.repairs) for record in parsed)
            if repairs:
                self.runtime.stats.repairs += repairs
                provenance.extra["repairs"] = repairs
            if invalid:
                # Out of rungs, but the shape is right and validation will
                # report the specifics. Accepting beats discarding.
                provenance.extra["validation_issues"] = invalid[0].problems

            return parsed, provenance

        provenance.extra["error"] = f"{location}: exhausted the retry ladder"
        return None, provenance

    def _build_request(
        self,
        group: EnrichmentGroup,
        provider: LanguageModelProvider,
        prompt_text: str,
        *,
        seed: int,
        rung: str,
        history: Sequence[_Attempt],
    ) -> GenerationRequest:
        prompt = prompt_text
        temperature = group.temperature
        system = group.prompt.system

        if rung == "repair" and history:
            last = history[-1]
            prompt = (
                f"{prompt_text}\n\n"
                "Your previous answer could not be used.\n"
                f"What you returned:\n{_clip(last.text) or '(nothing)'}\n\n"
                f"What was wrong:\n{last.error}\n\n"
                "Return corrected STRICT JSON only. No commentary, no markdown fences."
            )
        elif rung == "explicit":
            # Last model attempt: restate the contract in full and lower the
            # temperature, trading variety for a parseable answer.
            prompt = (
                f"{prompt_text}\n\n"
                "Your answer must validate against this JSON Schema exactly:\n"
                f"{json.dumps(group.prompt.json_schema, indent=2)}\n\n"
                "Output the JSON document and nothing else. Do not add fields. "
                "Do not omit required fields. Do not wrap it in markdown."
            )
            temperature = 0.1
            system = group.prompt.system + "\nAccuracy of format matters more than variety."

        return GenerationRequest(
            prompt=prompt,
            model=getattr(provider, "model", None),
            system=system,
            max_tokens=group.max_tokens,
            temperature=temperature,
            seed=seed,
            json_schema=group.prompt.json_schema,
        )

    def _cache_key(
        self, provider: LanguageModelProvider, request: GenerationRequest, group: EnrichmentGroup
    ) -> str:
        """Section 76: provider, model, prompt, settings and seed."""
        return cache_key(
            provider=provider.id,
            model=getattr(provider, "model", None),
            prompt=request.prompt,
            settings={
                "system": request.system,
                "temperature": request.temperature,
                "max_tokens": request.max_tokens,
                "prompt_version": group.prompt.version,
                "schema": group.prompt.hash,
            },
            seed=request.seed,
        )

    # -- results ------------------------------------------------------------ #

    def _known_values(self, group: EnrichmentGroup, record: GeneratedRecord) -> dict[str, Any]:
        return {
            name: record.values[name]
            for name in group.context_fields
            if name in record.values and record.values[name] is not None
        }

    def _apply(
        self,
        group: EnrichmentGroup,
        record: GeneratedRecord,
        parsed: ParsedRecord,
        provenance: FieldProvenance,
    ) -> None:
        for compiled in group.fields:
            record.values[compiled.name] = parsed.values.get(compiled.name)
            if record.provenance is not None:
                record.provenance.fields[compiled.name] = provenance

    def _degrade(
        self,
        group: EnrichmentGroup,
        records: Sequence[GeneratedRecord],
        contexts: Sequence[GenerationContext],
        *,
        reason: str,
    ) -> None:
        """Apply the field's unavailability policy (section 65)."""
        if group.on_unavailable == "error":
            raise GenerationError(
                f"{records[0].entity}.{group.names[0]}: language-model generation failed "
                f"and 'on_unavailable' is 'error'. {reason}"
            )

        self.runtime.stats.fallbacks += len(records)
        for record, context in zip(records, contexts, strict=True):
            for compiled in group.fields:
                value = (
                    None
                    if group.on_unavailable == "null"
                    else compiled.generator.placeholder(context)  # type: ignore[attr-defined]
                )
                record.values[compiled.name] = value
                if record.provenance is not None:
                    record.provenance.fields[compiled.name] = FieldProvenance(
                        generator="llm",
                        seed=context.seed,
                        extra={"degraded": group.on_unavailable, "reason": reason[:200]},
                    )


def _clip(text: str, limit: int = 600) -> str:
    collapsed = text.strip()
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"
