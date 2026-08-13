"""The generation engine (design document sections 27, 31, 75 and 98).

The engine walks a compiled entity's fields in dependency order, builds one
:class:`~cacophony.core.record.GeneratedRecord` at a time, validates it, and
yields records in bounded batches::

    Generate batch -> Validate batch -> Write batch -> Release memory

Nothing accumulates. ``stream`` is an async generator, so a run over ten
million records holds one batch in memory regardless of dataset size
(section 31).

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
from ..core.errors import GenerationError
from ..core.provenance import FieldProvenance, ProvenanceMode, RecordProvenance
from ..core.record import GeneratedRecord
from ..core.seeds import SeedChain
from ..core.types import coerce_value
from ..validation.pipeline import RecordValidator
from ..validation.results import ValidationResult

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..schema.plan import CompiledEntity, CompiledField, CompiledProject

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

        self.stats: dict[str, EntityStats] = {}
        self._validators: dict[str, RecordValidator] = {}

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
        seeds = (entity_seeds or self.seed_chain_for(entity)).record(index)
        stats = self._stats_for(entity.name)

        record = GeneratedRecord(entity=entity.name, values={})
        provenance = (
            RecordProvenance(
                entity=entity.name,
                record_index=index,
                seed=seeds.seed,
                run_id=self.run_id,
                schema_version=self.compiled.spec.project.version,
            )
            if self.provenance_mode.tracks_records
            else None
        )

        # One context per record, repointed at each field, rather than one per
        # field. Constructing a fresh context per value was measurably the
        # second-largest cost in the generation loop after the generators
        # themselves. Generators receive the context for the duration of a
        # single call and must not retain it; ``sub_context`` exists for the
        # cases that need an independent one.
        context = GenerationContext(
            project=self.compiled.spec,
            entity=entity.spec,
            record_index=index,
            seeds=seeds,
            current_record=record.values,
            related_records=related or {},
            run_id=self.run_id,
        )

        track_fields = provenance is not None and self.provenance_mode.tracks_fields
        for compiled_field in entity.fields:
            context.field = compiled_field.spec
            context.seeds = (
                seeds.labelled_field(compiled_field.name)
                if self.provenance_mode.tracks_payloads
                else seeds.field(compiled_field.name)
            )
            context.attempt = 1

            value, field_provenance = await self._generate_field(compiled_field, context, stats)
            record.values[compiled_field.name] = value
            if track_fields:
                provenance.fields[compiled_field.name] = field_provenance  # type: ignore[union-attr]

        record.provenance = provenance
        record.id = self._record_id(entity, record, index)
        return record

    async def _generate_field(
        self,
        compiled_field: CompiledField,
        context: GenerationContext,
        stats: EntityStats,
    ) -> tuple[Any, FieldProvenance]:
        provenance = FieldProvenance(generator=compiled_field.generator_name, seed=context.seed)

        null_probability = compiled_field.null_probability
        if null_probability > 0.0 and context.seeds.sub("null").rng().random() < null_probability:
            provenance.extra["nulled"] = True
            return None, provenance

        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            context.attempt = attempt
            provenance.attempts = attempt
            try:
                if compiled_field.is_sync:
                    value = compiled_field.generator.generate_sync(context)  # type: ignore[attr-defined]
                else:
                    produced = await compiled_field.generator.generate(context)
                    value = produced.value
                    if produced.provenance is not None:
                        provenance = produced.provenance
                return coerce_value(value, compiled_field.spec.type), provenance
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
            return f"[FAILED:{compiled_field.name}]", provenance
        provenance.extra["failed"] = True
        return None, provenance

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

        batch: list[GeneratedRecord] = []
        for index in range(offset, offset + total):
            record = await self.generate_record(entity, index, entity_seeds=entity_seeds)

            if validator is not None:
                result = validator.validate(record)
                self._apply_result(result, stats)
                if not result.ok and self.drop_invalid:
                    continue

            batch.append(record)
            stats.generated += 1

            if len(batch) >= batch_size:
                yield batch
                batch = []
                # Give the event loop a turn so a long CPU-bound run stays
                # cancellable and, later, so writers can drain concurrently.
                await asyncio.sleep(0)

        if batch:
            yield batch

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
            validator = RecordValidator(entity)
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
        return {name: validator.stats.to_dict() for name, validator in self._validators.items()}

    def summary(self) -> dict[str, Any]:
        return {
            "project": self.compiled.name,
            "seed": self.compiled.seed,
            "run_id": self.run_id,
            "entities": {name: stats.to_dict() for name, stats in self.stats.items()},
            "validation": self.validation_stats(),
        }

    def entity_order(self) -> Sequence[str]:
        return self.compiled.entity_order
