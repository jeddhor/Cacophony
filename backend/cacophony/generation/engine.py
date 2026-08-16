"""The generation engine (design document sections 27, 31, 75 and 98).

The engine walks a compiled entity's fields in dependency *layers*, builds a
chunk of :class:`~cacophony.core.record.GeneratedRecord` objects in lockstep,
validates them, and yields records in bounded batches::

    Generate batch -> Validate batch -> Write batch -> Release memory

Nothing accumulates. ``stream`` is an async generator, so a run over ten
million records holds one batch in memory regardless of dataset size
(section 31).

Records are built together rather than one after another because that is what
lets a single language-model call cover several fields of several records
(section 11). Deterministic fields are unaffected - they are still produced per
record, in dependency order, at the same cost as before.

Records are addressed by absolute index. Because seeds are derived by hashing
that index rather than by advancing an RNG (section 75), record *n* is
identical whether it is the first record produced or resumed from a checkpoint
at 6,830,000 (section 32).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..core.context import GenerationContext
from ..core.errors import CacophonyError, GenerationError
from ..core.provenance import FieldProvenance, ProvenanceMode, RecordProvenance
from ..core.record import GeneratedRecord
from ..core.seeds import SeedChain
from ..core.types import coerce_value
from ..validation.pipeline import RecordValidator
from ..validation.results import ValidationResult
from .relations import EntityResolver

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..schema.plan import CompiledEntity, CompiledField, CompiledProject
    from .runtime import GenerationRuntime

__all__ = ["EntityStats", "FailurePolicy", "GenerationEngine"]

DEFAULT_BATCH_SIZE = 1_000


class FailurePolicy:
    """What to do when one field fails (design document section 65)."""

    RETRY = "retry"
    SKIP = "skip"
    PLACEHOLDER = "placeholder"
    INCOMPLETE = "incomplete"
    ABORT = "abort"

    ALL = (RETRY, SKIP, PLACEHOLDER, INCOMPLETE, ABORT)


@dataclass(slots=True)
class EntityStats:
    """Per-entity counters for the run inspector (section 56)."""

    entity: str
    generated: int = 0
    rejected: int = 0
    repaired: int = 0
    field_failures: int = 0
    retries: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity": self.entity,
            "generated": self.generated,
            "rejected": self.rejected,
            "repaired": self.repaired,
            "field_failures": self.field_failures,
            "retries": self.retries,
            "errors": self.errors[:20],
        }


class GenerationEngine:
    """Produces records for a compiled project."""

    def __init__(
        self,
        compiled: CompiledProject,
        *,
        validate: bool = True,
        drop_invalid: bool = False,
        provenance: ProvenanceMode | None = None,
        failure_policy: str = FailurePolicy.ABORT,
        max_attempts: int = 3,
        seed_namespace: str | None = None,
        run_id: str | None = None,
        runtime: GenerationRuntime | None = None,
        resolver: EntityResolver | None = None,
        reference_sample_every: int = 1,
        counts: dict[str, int] | None = None,
        assets: Any | None = None,
    ) -> None:
        if failure_policy not in FailurePolicy.ALL:
            raise ValueError(
                f"Unknown failure policy '{failure_policy}'. "
                f"Choose one of: {', '.join(FailurePolicy.ALL)}"
            )

        self.compiled = compiled
        self.validate_records = validate
        self.drop_invalid = drop_invalid
        self.provenance_mode = provenance or compiled.spec.project.provenance
        self.failure_policy = failure_policy
        # Section 66: never permit infinite retry loops.
        self.max_attempts = max(1, min(max_attempts, 10))
        self.seed_namespace = seed_namespace
        self.run_id = run_id
        self.runtime = runtime
        if self.runtime is None and compiled.spec.providers:
            # A project that declares providers means them to be used. Building
            # the runtime here creates provider objects but opens no
            # connections, so this costs nothing on a run that never calls one.
            from .runtime import GenerationRuntime

            self.runtime = GenerationRuntime.for_project(compiled.spec)

        #: Where media generators write their files (sections 19, 81). An
        #: :class:`cacophony.assets.store.AssetStore`, or None for a run that
        #: generates no media - in which case a media field degrades under its
        #: own failure policy rather than the engine growing a special case.
        self.assets = assets

        self.stats: dict[str, EntityStats] = {}
        self._validators: dict[str, RecordValidator] = {}
        self._enricher: Any = None
        self._group_cache: dict[tuple[str, int, int], list[Any]] = {}

        # Cross-record coherence (section 15). The resolver needs to generate
        # records and the engine needs the resolver, so they are introduced to
        # one another here rather than at construction.
        self.resolver = (
            resolver if resolver is not None else EntityResolver(compiled, counts=counts)
        )
        self.resolver.bind(self.generate_partial)
        #: Check one reference in every N. Verifying ten million foreign keys
        #: one at a time costs more than generating them did, so a large run
        #: checks a sample and says how large a sample it was.
        self.reference_sample_every = max(1, reference_sample_every)

    # -- provider services -------------------------------------------------- #

    @property
    def enricher(self) -> Any:
        """The language-model enricher, built once per engine."""
        if self._enricher is None and self.runtime is not None:
            from .enrichment import Enricher

            self._enricher = Enricher(self.runtime, max_attempts=self.max_attempts)
        return self._enricher

    def _enrichment_groups(
        self, entity: CompiledEntity, fields: Sequence[CompiledField], batch_size: int
    ) -> list[Any]:
        """Group a layer's model-backed fields, compiling prompts once.

        Prompts depend on the schema, not on the record, so they are compiled
        the first time a layer is seen and reused for every batch after it. On
        a ten-million-record run that is the difference between compiling a
        prompt once and compiling it ten million times.
        """
        from .enrichment import plan_enrichment

        key = (entity.name, id(fields[0]), batch_size)
        groups = self._group_cache.get(key)
        if groups is None:
            assert self.runtime is not None
            groups = plan_enrichment(entity, fields, self.runtime, batch_size=batch_size)
            self._group_cache[key] = groups
        return groups

    # -- seeds -------------------------------------------------------------- #

    def seed_chain_for(self, entity: CompiledEntity) -> SeedChain:
        """Root seed chain for an entity, honouring any sampling namespace.

        Sampling isolation (section 103) is structural here rather than
        stateful: seeds are hashes of the position in the hierarchy, so drawing
        a preview cannot consume randomness a production run would have used.
        ``seed_namespace`` exists on top of that, for when a caller explicitly
        wants a *different* sample rather than a faithful one.
        """
        chain = entity.seed_chain(self.compiled.seed)
        return chain.descend("sample", self.seed_namespace) if self.seed_namespace else chain

    # -- single record ------------------------------------------------------ #

    async def generate_record(
        self,
        entity: CompiledEntity,
        index: int,
        *,
        entity_seeds: SeedChain | None = None,
        related: dict[str, GeneratedRecord] | None = None,
    ) -> GeneratedRecord:
        """Build one record at absolute position ``index``."""
        records = await self.generate_chunk(
            entity, [index], entity_seeds=entity_seeds, related=related
        )
        return records[0]

    async def generate_chunk(
        self,
        entity: CompiledEntity,
        indices: Sequence[int],
        *,
        entity_seeds: SeedChain | None = None,
        related: dict[str, GeneratedRecord] | None = None,
    ) -> list[GeneratedRecord]:
        """Build several records together, layer by layer.

        Records are built in lockstep rather than one at a time, because that
        is what allows one language-model call to cover several fields and
        several records (section 11). Deterministic fields do not care either
        way - they are produced per record inside each layer exactly as before.

        Field order within a record is unaffected: the layers come from the
        dependency graph, so every field still sees everything it declared a
        dependency on.
        """
        seeds_root = entity_seeds or self.seed_chain_for(entity)
        stats = self._stats_for(entity.name)
        track_fields = self.provenance_mode.tracks_fields
        authored = entity.spec.field_names()

        records: list[GeneratedRecord] = []
        contexts: list[GenerationContext] = []
        record_seeds: list[SeedChain] = []

        for index in indices:
            seeds = seeds_root.record(index)
            # Values are pre-seeded in *authored* order so that the serialised
            # record reads like the schema, whatever order the dependency graph
            # decided to produce the fields in.
            record = GeneratedRecord(entity=entity.name, values=dict.fromkeys(authored))
            if self.provenance_mode.tracks_records:
                record.provenance = RecordProvenance(
                    entity=entity.name,
                    record_index=index,
                    seed=seeds.seed,
                    run_id=self.run_id,
                    schema_version=self.compiled.spec.project.version,
                )
            # One context per record, repointed at each field, rather than one
            # per field. Constructing a fresh context per value was measurably
            # the second-largest cost in the generation loop after the
            # generators themselves. Generators receive the context for the
            # duration of a single call and must not retain it; ``sub_context``
            # exists for the cases that need an independent one.
            contexts.append(
                GenerationContext(
                    project=self.compiled.spec,
                    entity=entity.spec,
                    record_index=index,
                    seeds=seeds,
                    current_record=record.values,
                    related_records=related or {},
                    run_id=self.run_id,
                    runtime=self.runtime,
                    resolver=self.resolver,
                    assets=self.assets,
                )
            )
            records.append(record)
            record_seeds.append(seeds)

        for layer in entity.layers():
            enrichable = [compiled for compiled in layer if self._is_enrichable(compiled)]
            direct = [compiled for compiled in layer if compiled not in enrichable]

            for record, context, seeds in zip(records, contexts, record_seeds, strict=True):
                for compiled_field in direct:
                    context.field = compiled_field.spec
                    context.seeds = (
                        seeds.labelled_field(compiled_field.name)
                        if self.provenance_mode.tracks_payloads
                        else seeds.field(compiled_field.name)
                    )
                    context.attempt = 1
                    if compiled_field.related_entities:
                        self._attach_related(compiled_field, context)
                    value, field_provenance, assets = await self._generate_field(
                        compiled_field, context, stats
                    )
                    record.values[compiled_field.name] = value
                    # A media generator produces a file as well as a value, and
                    # the record owns it (section 81).
                    if assets:
                        record.assets.extend(assets)
                    if track_fields and record.provenance is not None:
                        record.provenance.fields[compiled_field.name] = field_provenance

            if enrichable:
                await self._enrich_layer(entity, enrichable, records, contexts, stats)

        for record, index in zip(records, indices, strict=True):
            record.id = self._record_id(entity, record, index)
        return records

    def generate_partial(
        self, entity_name: str, index: int, fields: Sequence[str]
    ) -> dict[str, Any]:
        """Produce only ``fields`` of one record, for the resolver.

        This is what makes a foreign key cost no memory: to learn employee
        4,823,913's id, generate that one field of that one record rather than
        keeping five million of them.
        """
        entity = self.compiled.entity(entity_name)
        wanted = set(fields)
        seeds = self.seed_chain_for(entity).record(index)
        values: dict[str, Any] = {}

        context = GenerationContext(
            project=self.compiled.spec,
            entity=entity.spec,
            record_index=index,
            seeds=seeds,
            current_record=values,
            run_id=self.run_id,
            runtime=self.runtime,
            resolver=self.resolver,
            assets=self.assets,
        )

        for compiled_field in entity.fields:
            if compiled_field.name not in wanted:
                continue
            context.field = compiled_field.spec
            context.seeds = seeds.field(compiled_field.name)
            context.attempt = 1
            if compiled_field.related_entities:
                self._attach_related(compiled_field, context)
            try:
                if compiled_field.is_sync:
                    value = compiled_field.generator.generate_sync(context)  # type: ignore[attr-defined]
                else:
                    # A parent's model-written fields are never needed to
                    # resolve a reference, and calling one here would make a
                    # single child record cost a language-model request.
                    value = None
                values[compiled_field.name] = coerce_value(value, compiled_field.spec.type)
            except CacophonyError as exc:
                raise GenerationError(
                    f"could not derive {entity_name}.{compiled_field.name} at index {index}: {exc}"
                ) from exc

        return values

    def _attach_related(self, compiled_field: CompiledField, context: GenerationContext) -> None:
        """Give a field the parent records this row actually referenced.

        The reference generator recorded which index it chose; this turns that
        into a record. Resolving lazily matters: most fields never look at a
        parent, and materialising one for every reference would undo the point
        of deriving keys on demand.
        """
        from .generators.reference import LINKS_KEY

        links: dict[str, int] = context.extras.get(LINKS_KEY) or {}
        for target in compiled_field.related_entities:
            if target in context.related_records:
                continue
            index = links.get(target)
            if index is None:
                continue
            try:
                context.related_records[target] = self.resolver.record_at(target, index)
            except CacophonyError:
                # A parent that cannot be derived is reported when the field
                # that wanted it fails, with that field's name attached.
                continue

        # A field that reads `{agent.first_name}` named the reference field
        # rather than the entity, so the record has to answer to both.
        for alias, target in compiled_field.related_aliases.items():
            record = context.related_records.get(target)
            if record is not None:
                context.related_records[alias] = record

    def _is_enrichable(self, compiled_field: CompiledField) -> bool:
        """Whether this field should be produced by a grouped model call."""
        return (
            self.runtime is not None
            and type(compiled_field.generator).requires_provider == "language_model"
        )

    async def _enrich_layer(
        self,
        entity: CompiledEntity,
        fields: Sequence[CompiledField],
        records: Sequence[GeneratedRecord],
        contexts: Sequence[GenerationContext],
        stats: EntityStats,
    ) -> None:
        enricher = self.enricher
        assert enricher is not None

        for group in self._enrichment_groups(entity, fields, len(records)):
            try:
                await enricher.run(group, records, contexts)
            except GenerationError:
                if self.failure_policy == FailurePolicy.ABORT:
                    raise
                stats.field_failures += len(records)
                for record in records:
                    for compiled in group.fields:
                        record.values[compiled.name] = (
                            f"[FAILED:{compiled.name}]"
                            if self.failure_policy == FailurePolicy.PLACEHOLDER
                            else None
                        )

    async def _generate_field(
        self,
        compiled_field: CompiledField,
        context: GenerationContext,
        stats: EntityStats,
    ) -> tuple[Any, FieldProvenance, list[Any]]:
        """Produce one value, its provenance, and any files it wrote."""
        provenance = FieldProvenance(generator=compiled_field.generator_name, seed=context.seed)

        null_probability = compiled_field.null_probability
        if null_probability > 0.0 and context.seeds.sub("null").rng().random() < null_probability:
            provenance.extra["nulled"] = True
            return None, provenance, []

        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            context.attempt = attempt
            provenance.attempts = attempt
            assets: list[Any] = []
            try:
                if compiled_field.is_sync:
                    value = compiled_field.generator.generate_sync(context)  # type: ignore[attr-defined]
                else:
                    produced = await compiled_field.generator.generate(context)
                    value = produced.value
                    assets = list(produced.assets)
                    if produced.provenance is not None:
                        provenance = produced.provenance
                return coerce_value(value, compiled_field.spec.type), provenance, assets
            except Exception as exc:
                last_error = exc
                if self.failure_policy != FailurePolicy.RETRY or attempt == self.max_attempts:
                    break
                stats.retries += 1

        stats.field_failures += 1
        message = f"{context.location}: {last_error}"
        if len(stats.errors) < 100:
            stats.errors.append(message)

        if self.failure_policy == FailurePolicy.ABORT:
            raise GenerationError(message) from last_error
        if self.failure_policy == FailurePolicy.PLACEHOLDER:
            provenance.extra["placeholder"] = True
            return f"[FAILED:{compiled_field.name}]", provenance, []
        provenance.extra["failed"] = True
        return None, provenance, []

    def _record_id(self, entity: CompiledEntity, record: GeneratedRecord, index: int) -> str:
        primary_key = entity.spec.resolved_primary_key()
        if primary_key and record.values.get(primary_key) is not None:
            return str(record.values[primary_key])
        return f"{entity.name}#{index}"

    # -- streaming ---------------------------------------------------------- #

    async def stream(
        self,
        entity_name: str,
        *,
        count: int | None = None,
        offset: int = 0,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> AsyncIterator[list[GeneratedRecord]]:
        """Yield records in bounded batches.

        ``offset`` is what makes a run resumable: a checkpoint records how many
        records were completed, and resuming means starting the stream there.
        """
        entity = self.compiled.entity(entity_name)
        total = entity.count if count is None else count
        if total <= 0:
            return

        batch_size = max(1, batch_size)
        entity_seeds = self.seed_chain_for(entity)
        stats = self._stats_for(entity.name)
        validator = self._validator_for(entity) if self.validate_records else None

        # The write batch is also the generation chunk, so a batch-mode model
        # call can cover as many records as the batch holds. Memory stays
        # bounded by batch_size either way, which is section 31's requirement.
        for chunk_start in range(offset, offset + total, batch_size):
            indices = range(chunk_start, min(chunk_start + batch_size, offset + total))
            records = await self.generate_chunk(entity, list(indices), entity_seeds=entity_seeds)

            batch: list[GeneratedRecord] = []
            for record in records:
                if validator is not None:
                    result = validator.validate(record)
                    self._apply_result(result, stats)
                    if not result.ok and self.drop_invalid:
                        continue
                batch.append(record)
                stats.generated += 1

            if batch:
                yield batch
            # Give the event loop a turn so a long CPU-bound run stays
            # cancellable and, later, so writers can drain concurrently.
            await asyncio.sleep(0)

    async def generate_batch(
        self, entity_name: str, count: int, *, offset: int = 0
    ) -> list[GeneratedRecord]:
        """Materialise ``count`` records. Intended for previews, not for runs."""
        records: list[GeneratedRecord] = []
        async for batch in self.stream(
            entity_name, count=count, offset=offset, batch_size=max(count, 1)
        ):
            records.extend(batch)
        return records

    def preview(
        self, entity_name: str, count: int = 25, *, offset: int = 0
    ) -> list[GeneratedRecord]:
        """Synchronous convenience wrapper for sampling (section 103)."""
        return asyncio.run(self.generate_batch(entity_name, count, offset=offset))

    # -- bookkeeping -------------------------------------------------------- #

    def _stats_for(self, entity_name: str) -> EntityStats:
        stats = self.stats.get(entity_name)
        if stats is None:
            stats = EntityStats(entity=entity_name)
            self.stats[entity_name] = stats
        return stats

    def _validator_for(self, entity: CompiledEntity) -> RecordValidator:
        validator = self._validators.get(entity.name)
        if validator is None:
            validator = RecordValidator(
                entity,
                resolver=self.resolver,
                reference_sample_every=self.reference_sample_every,
            )
            self._validators[entity.name] = validator
        return validator

    @staticmethod
    def _apply_result(result: ValidationResult, stats: EntityStats) -> None:
        if result.was_repaired:
            stats.repaired += 1
        if not result.ok:
            stats.rejected += 1
            for issue in result.errors[:3]:
                if len(stats.errors) < 100:
                    stats.errors.append(issue.render())

    def validation_stats(self) -> dict[str, Any]:
        return {name: validator.summary() for name, validator in self._validators.items()}

    def summary(self) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "project": self.compiled.name,
            "seed": self.compiled.seed,
            "run_id": self.run_id,
            "entities": {name: stats.to_dict() for name, stats in self.stats.items()},
            "validation": self.validation_stats(),
        }
        if self.resolver.stats.key_lookups:
            summary["relations"] = self.resolver.describe()
        if self.runtime is not None:
            summary.update(self.runtime.summary())
        return summary

    async def aclose(self) -> None:
        """Release provider connections held for this run."""
        if self.runtime is not None:
            await self.runtime.aclose()

    def entity_order(self) -> Sequence[str]:
        return self.compiled.entity_order
