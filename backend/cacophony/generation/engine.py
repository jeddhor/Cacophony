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
from ..core.errors import CacophonyError, GenerationError, SchemaError
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
        simulate: bool = True,
        chaos: bool = True,
        detect_duplicates: bool = True,
        edge_cases: float = 0.0,
        edge_categories: list[str] | None = None,
        patches: bool = True,
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
        #: Duplicate detectors, one per entity (section 59). Built lazily and
        #: kept for the whole run: a repeat is only interesting relative to
        #: everything generated before it, so a per-batch detector would find
        #: almost nothing.
        self._detectors: dict[str, Any] = {}
        self.detect_duplicates = detect_duplicates
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

        # Synthetic worlds (sections 17, 24, 25, 26). Built once per run and
        # shared by every record; an entity that declares nothing costs nothing.
        self.counts = dict(counts or {})
        self.timeline = _build_timeline(compiled) if simulate else None
        self.scenarios = _build_scenarios(compiled, self.timeline) if simulate else None
        self.simulations: dict[str, Any] = {}
        if simulate:
            from .simulation import build_simulations, evaluator

            self.simulations = build_simulations(
                compiled,
                timeline=self.timeline,
                scenarios=self.scenarios,
                evaluate=evaluator(),
                counts=self.counts,
            )
        self._injectors: dict[str, Any] = {}
        self.inject_chaos = chaos and compiled.spec.chaos.is_enabled()

        # Edge-case generation (section 79). A fraction of records get a legal
        # but awkward value. Distinct from chaos, which produces illegal ones:
        # an application that mishandles an edge case has a bug, while one that
        # rejects chaos is doing its job.
        self.edge_cases = min(1.0, max(0.0, float(edge_cases)))
        self.edge_categories = list(edge_categories or [])
        self._edge_injectors: dict[str, Any] = {}

        # Patch rules (section 104). Part of the project, so a patched dataset
        # is still a pure function of the schema and the seed - which is what
        # lets record 4,823,913 be regenerated next year with the same edit
        # applied. An edit made to an output file has neither property.
        self.apply_patches = patches
        self._patch_sets: dict[str, Any] = {}

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

        # What the simulation knows before any field exists: whose event this
        # is, where it falls, and whether a scenario has it (sections 17, 25).
        simulation = self.simulations.get(entity.name)
        if simulation is not None:
            from .generators.simulated import SIMULATION_KEY

            replay = self._replay_for(simulation, entity) if simulation.has_state else None
            for record, context, index in zip(records, contexts, indices, strict=True):
                frame = simulation.frame_for(index)
                if simulation.has_state:
                    frame.fold = _folder(simulation, frame, record, replay)
                context.extras[SIMULATION_KEY] = frame

        # Edge-case generation (section 79). Applied as each field is produced
        # rather than to the finished record, so anything derived from an
        # awkward value derives from the awkward value.
        edges = self._edges_for(entity)
        if edges is not None:
            for _record in records:
                edges.note_record()

        for layer in entity.layers():
            enrichable = [compiled for compiled in layer if self._is_enrichable(compiled)]
            direct = [compiled for compiled in layer if compiled not in enrichable]

            for record, context, seeds, index in zip(
                records, contexts, record_seeds, indices, strict=True
            ):
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
                    if edges is not None:
                        edges.apply_to_field(
                            record, index, compiled_field.name, self._validator_for(entity)
                        )

            if enrichable:
                await self._enrich_layer(entity, enrichable, records, contexts, stats)
                if edges is not None:
                    for record, index in zip(records, indices, strict=True):
                        for compiled_field in enrichable:
                            edges.apply_to_field(
                                record, index, compiled_field.name, self._validator_for(entity)
                            )

        if simulation is not None and self.scenarios is not None:
            self._apply_scenarios(entity, records, contexts)

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

        # A partially-derived record still needs to know whose event it is:
        # replaying an earlier event to rebuild a subject's state (section 26)
        # goes through here, and that event's own `subject` and `event_time`
        # fields are part of what has to be regenerated. No fold is attached -
        # the state machine is the caller, and a fold here would recurse.
        simulation = self.simulations.get(entity_name)
        if simulation is not None:
            from .generators.simulated import SIMULATION_KEY

            context.extras[SIMULATION_KEY] = simulation.frame_for(index)

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

    # -- simulation ---------------------------------------------------------- #

    def _replay_for(self, simulation: Any, entity: CompiledEntity) -> Any:
        """Regenerate an earlier event of the same subject, for a resumed fold.

        Only called when the state machine is asked for an ordinal it has not
        reached - a run resumed mid-block, or a preview starting in the middle.
        The cost is bounded by one subject's block, not by the dataset.
        """
        needed = [
            compiled.name
            for compiled in entity.fields
            if compiled.generator.name not in ("state",)
            and type(compiled.generator).requires_provider is None
        ]

        def replay(subject: int, ordinal: int) -> dict[str, Any]:
            index = simulation.allocation.start_of(subject) + ordinal
            return self.generate_partial(entity.name, index, needed)

        return replay

    def _apply_scenarios(
        self,
        entity: CompiledEntity,
        records: list[GeneratedRecord],
        contexts: list[GenerationContext],
    ) -> None:
        """Overwrite fields for records a scenario has hold of (section 17).

        Applied after generation rather than during it, so a scenario composes
        with every generator instead of having to be understood by each one,
        and so the 98% of records it does not touch cost nothing.
        """
        from .generators.simulated import SIMULATION_KEY

        assert self.scenarios is not None
        for record, context in zip(records, contexts, strict=True):
            frame = context.extras.get(SIMULATION_KEY)
            if frame is None or not frame.effects:
                continue

            for name, effect in frame.effects.items():
                if name not in record.values:
                    continue
                record.values[name] = _resolve_effect(effect, record.values, context)

            self.scenarios.record_applied(frame.involvement.scenario)
            if record.provenance is not None and frame.involvement is not None:
                record.provenance.extra["scenario"] = frame.involvement.to_dict()

    def _injector_for(self, entity: CompiledEntity) -> Any:
        """The chaos injector for an entity, built on first use (section 78)."""
        injector = self._injectors.get(entity.name)
        if injector is None:
            from ..simulation.chaos import ChaosInjector

            primary = entity.spec.resolved_primary_key()
            # A scenario label is Cacophony's own annotation, not generated
            # data: damaging it would corrupt the record of what the generator
            # did, which is the one thing a chaotic dataset still has to be
            # able to tell you.
            protected = [primary] if primary else []
            protected += [
                compiled.name for compiled in entity.fields if compiled.generator.name == "scenario"
            ]
            injector = ChaosInjector(
                self.compiled.spec.chaos,
                seed=self.compiled.seed,
                entity=entity.name,
                fields=entity.spec.field_names(),
                protected=protected,
            )
            self._injectors[entity.name] = injector
        return injector

    def _patches_for(self, entity: CompiledEntity) -> Any:
        """The patch rules that apply to an entity, or None (section 104)."""
        if not self.apply_patches:
            return None
        if entity.name in self._patch_sets:
            return self._patch_sets[entity.name]

        from ..transforms.rules import PatchRule, PatchSet

        declared = self.compiled.spec.patches
        rules = [
            PatchRule.from_spec(name, spec)
            for name, spec in declared.items()
            if not spec.entity or spec.entity == entity.name
        ]
        patch_set = PatchSet(rules, entity=entity.name) if rules else None
        self._patch_sets[entity.name] = patch_set
        return patch_set

    def patch_reports(self) -> dict[str, Any]:
        """What the patch rules did, per entity."""
        return {
            name: patch_set.describe()
            for name, patch_set in self._patch_sets.items()
            if patch_set is not None and patch_set.stats.records_seen
        }

    def _edges_for(self, entity: CompiledEntity) -> Any:
        """The edge-case injector for an entity, or None (section 79)."""
        if self.edge_cases <= 0:
            return None
        if entity.name in self._edge_injectors:
            return self._edge_injectors[entity.name]

        from ..simulation.edges import EdgeCaseInjector

        protected = [
            compiled.name for compiled in entity.fields if compiled.generator.name == "scenario"
        ]
        injector = EdgeCaseInjector(
            entity,
            fraction=self.edge_cases,
            seed=self.compiled.seed,
            categories=self.edge_categories or None,
            protected=protected,
        )
        self._edge_injectors[entity.name] = None if injector.is_noop else injector
        return self._edge_injectors[entity.name]

    def edge_case_reports(self) -> dict[str, Any]:
        """What edge-case generation did, per entity."""
        return {
            name: injector.describe()
            for name, injector in self._edge_injectors.items()
            if injector is not None and injector.stats.records_seen
        }

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
        # The location once, not twice. A generator that already knows where it
        # is - and several do, because a good message needs the field name -
        # would otherwise be reported as "t.x: t.x: ...".
        detail = str(last_error)
        prefix = f"{context.location}: "
        message = detail if detail.startswith(prefix) else prefix + detail
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

            # Deliberate damage, before validation so the validator can be told
            # which defects were asked for (sections 24, 78).
            if self.inject_chaos:
                records = self._damage(entity, records, list(indices))

            # Patch rules last, and before validation (section 104). Last
            # because a rule is the final word on what a record contains -
            # somebody masking a column means the column masked, not masked and
            # then damaged. Before validation because what reaches the file is
            # what should be checked.
            patch_set = self._patches_for(entity)
            if patch_set is not None:
                records = [
                    patched
                    for patched in (self._patch(patch_set, record) for record in records)
                    if patched is not None
                ]

            detector = self._detector_for(entity)
            batch: list[GeneratedRecord] = []
            for record in records:
                if validator is not None:
                    result = validator.validate(record)
                    self._apply_result(result, stats)
                    if not result.ok and self.drop_invalid:
                        continue
                # After the drop, so a dataset's duplication rate describes the
                # dataset somebody gets rather than one including records that
                # were thrown away.
                if detector is not None:
                    detector.observe(record)
                batch.append(record)
                stats.generated += 1

            if batch:
                yield batch
            # Give the event loop a turn so a long CPU-bound run stays
            # cancellable and, later, so writers can drain concurrently.
            await asyncio.sleep(0)

    @staticmethod
    def _patch(patch_set: Any, record: GeneratedRecord) -> GeneratedRecord | None:
        """Apply the rules to one record, in place. None means filtered out."""
        result = patch_set.apply(record.values)
        return None if result is None else record

    def _damage(
        self,
        entity: CompiledEntity,
        records: list[GeneratedRecord],
        indices: list[int],
    ) -> list[GeneratedRecord]:
        """Apply entropy injection to a batch (sections 24, 78).

        A duplicate is emitted immediately after its original, which is what a
        retried insert looks like in a real system.
        """
        injector = self._injector_for(entity)
        if injector.is_noop:
            return records

        damaged: list[GeneratedRecord] = []
        for record, index in zip(records, indices, strict=True):
            duplicate = injector.apply(record, index)
            damaged.append(record)
            if duplicate is not None:
                damaged.append(duplicate)
        return damaged

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

    def _detector_for(self, entity: CompiledEntity) -> Any:
        """The duplicate detector for an entity, or None (section 59).

        One per entity per engine, so its Bloom filter and window span the
        whole run rather than a batch - a duplicate is only interesting
        relative to everything before it.
        """
        if not self.detect_duplicates:
            return None
        if entity.name in self._detectors:
            return self._detectors[entity.name]

        from ..validation.duplication import DuplicateDetector

        spec = self.compiled.spec.quality.duplication
        detector: Any = None
        if spec.is_enabled():
            detector = DuplicateDetector(
                entity, spec, expected_records=self.counts.get(entity.name, entity.count)
            )
            # An entity with no comparable fields is not an error: a table of
            # ids and timestamps has no prose to repeat.
            if not detector.fields:
                detector = None
        self._detectors[entity.name] = detector
        return detector

    def duplication_reports(self) -> dict[str, Any]:
        """Closed reports for every entity that was checked."""
        return {
            name: detector.finish().to_dict()
            for name, detector in self._detectors.items()
            if detector is not None
        }

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


# --------------------------------------------------------------------------- #
# Simulation setup (sections 17, 25)
# --------------------------------------------------------------------------- #


def _build_timeline(compiled: CompiledProject) -> Any:
    """The project's period, if it declares one."""
    spec = getattr(compiled.spec, "timeline", None)
    if spec is None or not spec.is_enabled():
        return None

    from ..simulation.timeline import SHAPES, ShapeOverrides, Timeline, parse_moment

    start = parse_moment(spec.start, what="the timeline's start")
    end = parse_moment(spec.end, what="the timeline's end") if spec.end else None
    if end is None:
        import datetime as _dt

        end = start + _dt.timedelta(days=365)

    base = SHAPES.get(str(spec.shape).lower())
    if base is None:
        raise SchemaError(
            f"unknown timeline shape '{spec.shape}'. Available: {', '.join(sorted(SHAPES))}"
        )
    shape = ShapeOverrides(
        holidays=list(spec.holidays),
        holiday_weight=spec.holiday_weight,
        months=dict(spec.months),
        spikes=list(spec.spikes),
        growth=spec.growth,
    ).apply(base)
    return Timeline(start, end, shape)


def _build_scenarios(compiled: CompiledProject, timeline: Any) -> Any:
    """The scenario engine, if the project declares any (section 17)."""
    declared = list(compiled.spec.scenarios.values())
    if not declared:
        return None

    from ..simulation.scenarios import ScenarioEngine, compile_scenarios

    return ScenarioEngine(
        compile_scenarios(declared, entities=list(compiled.entity_order)),
        seed=compiled.seed,
        timeline=timeline,
    )


def _resolve_effect(effect: Any, values: dict[str, Any], context: GenerationContext) -> Any:
    """What a scenario effect evaluates to for one record.

    A constant is used as written. A mapping is a weighted choice, so an
    incident can say "mostly failures, sometimes a success" rather than
    flipping every record to the same value and making the scenario trivially
    detectable. A string beginning with ``=`` is an expression over the record.
    """
    if isinstance(effect, dict):
        rng = context.rng()
        total = sum(float(weight) for weight in effect.values()) or 1.0
        draw = rng.random() * total
        running = 0.0
        for value, weight in effect.items():
            running += float(weight)
            if draw <= running:
                return value
        return next(iter(effect))
    if isinstance(effect, list) and effect:
        return context.rng().choice(effect)
    if isinstance(effect, str) and effect.startswith("="):
        from .simulation import evaluator

        return evaluator()(effect[1:], values)
    return effect


def _folder(simulation: Any, frame: Any, record: GeneratedRecord, replay: Any) -> Any:
    """Bind one record's fold, for the frame to run when it is first needed."""

    def fold() -> None:
        simulation.fold_state(frame, record.values, replay=replay)

    return fold
