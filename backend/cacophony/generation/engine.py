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
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..core.context import GenerationContext
from ..core.errors import (
    CacophonyError,
    GenerationError,
    SchemaError,
    ValidationFailedError,
)
from ..core.provenance import FieldProvenance, ProvenanceMode, RecordProvenance
from ..core.record import GeneratedRecord
from ..core.seeds import SeedChain, mix_seed
from ..core.types import coerce_value
from ..validation.pipeline import RecordValidator
from ..validation.rejects import DEFAULT_KEEP as DEFAULT_KEEP_REJECTS
from ..validation.results import ValidationResult
from ..validation.uniqueness import DEFAULT_MEMORY_CEILING
from .relations import EntityResolver

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..schema.plan import CompiledEntity, CompiledField, CompiledProject
    from .runtime import GenerationRuntime

__all__ = ["EntityStats", "FailurePolicy", "GenerationEngine", "StreamChunk"]

DEFAULT_BATCH_SIZE = 1_000

#: Distinguishes the reject sampler's seed from every other derivation.
_REJECT_SALT = 0x52454A43

#: What ``profile: maximum_chaos`` sets ``--edge-cases`` to when nothing else
#: has. High enough to find something, low enough that the dataset is still
#: mostly the dataset you asked for.
PROFILE_EDGE_CASES = 0.05


class FailurePolicy:
    """What to do when generation or validation says no (design document section 65).

    Section 65 is written about a field that fails to generate, and the policy
    was applied only there for a long time: a record that generated cleanly and
    then failed *validation* was counted, written to the file anyway, and the
    run exited successfully. That is now one behaviour rather than two, because
    "abort" meaning "abort unless the data is merely invalid" is not a promise
    anybody can plan around.

    The mapping onto a validation failure:

    ``abort``
        Stop the run, naming the record and what was wrong with it.
    ``retry``
        Generate the record again, up to ``max_attempts``; if it still fails,
        drop it and count it, which is what an exhausted per-field retry does.
    ``skip``
        Drop the record and count it. What ``--drop-invalid`` asks for.
    ``placeholder``
        Mark the offending fields ``[FAILED:name]`` and keep the record.
    ``incomplete``
        Remove the offending fields and keep the record.
    ``report``
        Count it and write it anyway. The old behaviour, kept because a
        preview, a ``regenerate`` and the model benchmark all exist to *show*
        you the invalid record rather than to refuse it.
    """

    RETRY = "retry"
    SKIP = "skip"
    PLACEHOLDER = "placeholder"
    INCOMPLETE = "incomplete"
    ABORT = "abort"
    REPORT = "report"

    ALL = (RETRY, SKIP, PLACEHOLDER, INCOMPLETE, ABORT, REPORT)


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


@dataclass(slots=True)
class StreamChunk:
    """One batch, and where in the source it came from (sections 31, 32).

    The records and the position are *different numbers*, and conflating them
    was a real defect: validation can drop a record and entropy injection can
    duplicate one, so "rows written" is not "source records consumed". A
    checkpoint that stores the first and resumes from the second skips or
    repeats work - measurably, once a schema drops anything.

    ``next_index`` is where a resumed run must start. It is reported even for a
    chunk that produced no records at all, so an all-dropped batch is still a
    checkpoint and still a place a run can be cancelled.
    """

    records: list[GeneratedRecord]
    #: First source index this chunk covered.
    first_index: int
    #: Source index a continuation should start from.
    next_index: int

    def __len__(self) -> int:
        return len(self.records)

    def __bool__(self) -> bool:
        return bool(self.records)

    def __iter__(self) -> Iterator[GeneratedRecord]:
        return iter(self.records)


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
        validation_policy: str | None = None,
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
        unique_memory_ceiling: int = DEFAULT_MEMORY_CEILING,
        keep_rejects: int = DEFAULT_KEEP_REJECTS,
    ) -> None:
        for name, policy in (("failure", failure_policy), ("validation", validation_policy)):
            if policy is not None and policy not in FailurePolicy.ALL:
                raise ValueError(
                    f"Unknown {name} policy '{policy}'. "
                    f"Choose one of: {', '.join(FailurePolicy.ALL)}"
                )

        self.compiled = compiled
        self.validate_records = validate
        self.drop_invalid = drop_invalid
        self.provenance_mode = provenance or compiled.spec.project.provenance
        self.failure_policy = failure_policy
        #: What to do about a record that generated cleanly and then failed
        #: validation. The same policy by default, which is the point: one flag,
        #: one behaviour. It is separate so that the three commands whose job is
        #: to *show* you a bad record - preview, regenerate, benchmark - can
        #: report an invalid record while still aborting on a generator that
        #: raises, which is a different kind of problem and still theirs to
        #: surface.
        self.validation_policy = validation_policy or failure_policy
        #: How many rejected records each entity keeps for the inspector
        #: (section 56). Bounded, because rejections scale with the dataset
        #: and section 31 says nothing here may.
        self.keep_rejects = max(0, keep_rejects)
        self._rejects: dict[str, Any] = {}
        #: Fields that decided for themselves, and what they decided
        #: (section 65). Reported, so a run that says `abort` and did not is
        #: explicable from its own summary.
        self._policy_overrides: dict[str, str] = {}
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
        self.unique_memory_ceiling = max(1, unique_memory_ceiling)
        self._detectors: dict[str, Any] = {}
        self._judges: dict[str, Any] = {}
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
        # The other half of section 77's `maximum_chaos`: a fraction of records
        # get a legal-but-awkward value as well as the damage. Resolved here
        # rather than in the CLI so that the API, the coordinator and a
        # distributed worker all mean the same thing by the profile.
        if edge_cases <= 0 and compiled.spec.project.profile == "maximum_chaos":
            edge_cases = PROFILE_EDGE_CASES
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
                # One call produced several fields, and they may not agree
                # about what a failure means. A field that says `abort` aborts
                # the run; the others fall back to what each of them asked for.
                policies = {compiled.name: self._policy_for(compiled) for compiled in group.fields}
                if FailurePolicy.ABORT in policies.values():
                    raise
                stats.field_failures += len(records)
                for record in records:
                    for compiled in group.fields:
                        policy = policies[compiled.name]
                        if policy != self.failure_policy:
                            self._policy_overrides[f"{entity.name}.{compiled.name}"] = policy
                        record.values[compiled.name] = (
                            f"[FAILED:{compiled.name}]"
                            if policy == FailurePolicy.PLACEHOLDER
                            else None
                        )

    def _policy_for(self, compiled_field: CompiledField) -> str:
        """The failure policy governing this field (design document section 65).

        A field may state its own; otherwise the run's applies. Section 65 asks
        for the policy to be configurable per generator, and a generator is
        what produces a field - so this is where "per generator" lives.
        """
        return compiled_field.spec.on_failure or self.failure_policy

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

        policy = self._policy_for(compiled_field)
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
                if policy != FailurePolicy.RETRY or attempt == self.max_attempts:
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

        if policy != self.failure_policy:
            # Recorded so the run report can say which fields decided for
            # themselves: a run that says "abort" and did not is otherwise a
            # mystery in the log.
            self._policy_overrides[f"{context.entity.name}.{compiled_field.name}"] = policy

        if policy == FailurePolicy.ABORT:
            raise GenerationError(message) from last_error
        if policy == FailurePolicy.PLACEHOLDER:
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
    ) -> AsyncIterator[StreamChunk]:
        """Yield records in bounded batches, each carrying its source position.

        ``offset`` is what makes a run resumable, and the position is what makes
        it *correct*: a checkpoint stores where the source got to rather than
        how many rows came out, because those two numbers differ the moment a
        record is dropped or duplicated (see :class:`StreamChunk`).
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
            indices = list(range(chunk_start, min(chunk_start + batch_size, offset + total)))
            prepared = await self._prepare(entity, indices, entity_seeds)

            detector = self._detector_for(entity)
            judge = self._judge_for(entity)
            batch: list[GeneratedRecord] = []
            for index, record in prepared:
                if validator is not None:
                    kept = await self._validated(
                        entity, index, record, validator, stats, entity_seeds
                    )
                    if kept is None:
                        continue
                    record = kept
                # After the drop, so a dataset's duplication rate describes the
                # dataset somebody gets rather than one including records that
                # were thrown away.
                if detector is not None:
                    detector.observe(record)
                if judge is not None:
                    judge.observe(record)
                batch.append(record)
                stats.generated += 1

            # Yielded even when empty: an all-dropped batch has still consumed
            # source records, and the caller needs to be told so - both to
            # checkpoint the position and to notice a cancellation.
            yield StreamChunk(
                records=batch,
                first_index=indices[0] if indices else chunk_start,
                next_index=(indices[-1] + 1) if indices else chunk_start,
            )
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
        prepared: list[tuple[int, GeneratedRecord]],
    ) -> list[tuple[int, GeneratedRecord]]:
        """Apply entropy injection to a batch (sections 24, 78).

        A duplicate is emitted immediately after its original, which is what a
        retried insert looks like in a real system, and it carries the index of
        the record it duplicates - so a record stays traceable to the position
        it came from however many of them a batch ends up holding.
        """
        injector = self._injector_for(entity)
        if injector.is_noop:
            return prepared

        damaged: list[tuple[int, GeneratedRecord]] = []
        for index, record in prepared:
            duplicate = injector.apply(record, index)
            damaged.append((index, record))
            if duplicate is not None:
                damaged.append((index, duplicate))
        return damaged

    async def _prepare(
        self,
        entity: CompiledEntity,
        indices: list[int],
        entity_seeds: Any,
    ) -> list[tuple[int, GeneratedRecord]]:
        """Generate a chunk and put it through everything that precedes validation.

        Chaos first, so the validator can be told which defects were asked for
        (sections 24, 78). Patch rules last, because a rule is the final word on
        what a record contains - somebody masking a column means the column
        masked, not masked and then damaged - and before validation, because
        what reaches the file is what should be checked (section 104).

        Kept as one method so that regenerating a single record for a retry puts
        it through the same steps as the batch it came from. A retried record
        that skipped chaos or patches would be a different kind of record.
        """
        records = await self.generate_chunk(entity, indices, entity_seeds=entity_seeds)
        prepared = list(zip(indices, records, strict=True))

        if self.inject_chaos:
            prepared = self._damage(entity, prepared)

        patch_set = self._patches_for(entity)
        if patch_set is not None:
            prepared = [
                (index, patched)
                for index, patched in (
                    (index, self._patch(patch_set, record)) for index, record in prepared
                )
                if patched is not None
            ]
        return prepared

    async def _validated(
        self,
        entity: CompiledEntity,
        index: int,
        record: GeneratedRecord,
        validator: RecordValidator,
        stats: EntityStats,
        entity_seeds: Any,
    ) -> GeneratedRecord | None:
        """Validate one record and apply the failure policy (sections 57, 65).

        Returns the record to write, or None to drop it; raises under ``abort``.
        """
        result = validator.validate(record)
        if result.was_repaired:
            stats.repaired += 1
        if result.ok:
            return record

        policy = FailurePolicy.SKIP if self.drop_invalid else self.validation_policy

        if policy == FailurePolicy.RETRY:
            for _ in range(self.max_attempts - 1):
                stats.retries += 1
                # The failed record gives back whatever unique values it took,
                # or its own replacement collides with it.
                validator.forget_last()
                candidates = await self._prepare(entity, [index], entity_seeds)
                if not candidates:  # a patch rule filtered the record out
                    self._reject(entity, index, record, result, stats)
                    return None
                candidate = candidates[0][1]
                retried = validator.validate(candidate)
                if retried.was_repaired:
                    stats.repaired += 1
                if retried.ok:
                    return candidate
                record, result = candidate, retried
            # Out of attempts, and the record is dropped rather than the run
            # stopped - which is what an exhausted per-field retry does. Note
            # that a deterministic field reproduces the value that failed, so
            # retrying only ever helps where a provider is involved.
            self._reject(entity, index, record, result, stats)
            validator.forget_last()
            return None

        self._reject(entity, index, record, result, stats)

        if policy == FailurePolicy.ABORT:
            raise ValidationFailedError(self._invalid_message(entity, index, record, result))
        if policy == FailurePolicy.SKIP:
            validator.forget_last()
            return None
        if policy in (FailurePolicy.PLACEHOLDER, FailurePolicy.INCOMPLETE):
            for name in self._offending_fields(result):
                if policy == FailurePolicy.PLACEHOLDER:
                    record.values[name] = f"[FAILED:{name}]"
                else:
                    record.values.pop(name, None)
            return record
        # REPORT: counted, and written as it is.
        return record

    @staticmethod
    def _offending_fields(result: ValidationResult) -> list[str]:
        """Which fields the errors name, in order, without repeats."""
        names: list[str] = []
        for issue in result.errors:
            if issue.field and issue.field not in names:
                names.append(issue.field)
        return names

    def _invalid_message(
        self,
        entity: CompiledEntity,
        index: int,
        record: GeneratedRecord,
        result: ValidationResult,
    ) -> str:
        """An abort message that says what to do next.

        A validation failure is usually a schema problem rather than a run
        problem, so the message names the record, quotes the first issues, and
        lists the three flags that mean "I know, carry on".
        """
        # Rendered here rather than with ``issue.render()``: that form leads with
        # "[category]", and the CLI prints an error through rich, which reads
        # square brackets as markup and eats the word.
        issues = "; ".join(
            f"{issue.field}: {issue.message} ({issue.category})"
            if issue.field
            else f"{issue.message} ({issue.category})"
            for issue in result.errors[:3]
        )
        more = len(result.errors) - 3
        if more > 0:
            issues += f" (and {more} more)"
        return (
            f"{self._record_id(entity, record, index)} failed validation: {issues}. "
            "Pass --drop-invalid to discard records like this one, "
            "--on-failure report to write them and count them, "
            "or --no-validate to stop checking."
        )

    async def audit(
        self,
        entity: CompiledEntity,
        records: Sequence[GeneratedRecord],
        indices: Sequence[int] | None = None,
    ) -> int:
        """Validate records that were produced outside :meth:`stream`.

        The live stream generates chunks directly - it decides for itself how
        many records are due this tick - so this is where its records meet the
        validator, and it returns how many failed.

        Always reporting, never enforcing, whatever the failure policy says: a
        workload generator that stopped mid-load because one record in a million
        was invalid would have failed the test it was running.
        """
        if not self.validate_records:
            return 0

        validator = self._validator_for(entity)
        stats = self._stats_for(entity.name)
        positions = list(indices or range(len(records)))
        failed = 0
        for position, record in zip(positions, records, strict=False):
            result = validator.validate(record)
            if result.was_repaired:
                stats.repaired += 1
            if not result.ok:
                failed += 1
                self._reject(entity, position, record, result, stats)
        return failed

    async def generate_batch(
        self, entity_name: str, count: int, *, offset: int = 0
    ) -> list[GeneratedRecord]:
        """Materialise ``count`` records. Intended for previews, not for runs."""
        records: list[GeneratedRecord] = []
        async for chunk in self.stream(
            entity_name, count=count, offset=offset, batch_size=max(count, 1)
        ):
            records.extend(chunk.records)
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

    def _judge_for(self, entity: CompiledEntity) -> Any:
        """The semantic evaluator for an entity, or None (section 57).

        Off unless the schema asks. It samples during the run and judges at the
        end, so a model is called a bounded number of times rather than once per
        record - which is the cost section 57 is careful about.
        """
        if entity.name in self._judges:
            return self._judges[entity.name]

        from ..validation.semantic import SemanticEvaluator

        judge = None
        if self.runtime is not None:
            judge = SemanticEvaluator.for_entity(entity, self.compiled.spec.quality.semantic)
        self._judges[entity.name] = judge
        return judge

    async def semantic_reports(self) -> dict[str, Any]:
        """Ask the judge about what it collected. Called once, at the end."""
        if self.runtime is None:
            return {}
        reports: dict[str, Any] = {}
        for name, judge in self._judges.items():
            if judge is None:
                continue
            verdict = await judge.evaluate(self.runtime)
            if verdict is not None:
                reports[name] = verdict
        return reports

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
                privacy=self.compiled.spec.privacy,
                unique_memory_ceiling=self.unique_memory_ceiling,
            )
            self._validators[entity.name] = validator
        return validator

    def _reject(
        self,
        entity: CompiledEntity,
        index: int,
        record: GeneratedRecord,
        result: ValidationResult,
        stats: EntityStats,
    ) -> None:
        """Count one rejected record, once, whatever happens to it next.

        Counted here rather than per validation attempt, so a retried record is
        one rejection rather than three.
        """
        stats.rejected += 1
        where = self._record_id(entity, record, index)
        for issue in result.errors[:3]:
            if len(stats.errors) < 100:
                stats.errors.append(f"{where}: {issue.render()}")

        # And keep some of them, so section 56's inspector has records to show
        # rather than a count and three sentences.
        if self.keep_rejects > 0:
            from ..validation.rejects import RejectionSample, describe

            sample = self._rejects.get(entity.name)
            if sample is None:
                sample = RejectionSample(
                    entity=entity.name,
                    keep=self.keep_rejects,
                    seed=mix_seed(self.compiled.seed, _REJECT_SALT, len(entity.name)),
                )
                self._rejects[entity.name] = sample
            sample.observe(describe(entity.name, index, where, record, result))

    def validation_stats(self) -> dict[str, Any]:
        return {name: validator.summary() for name, validator in self._validators.items()}

    def rejected_records(self) -> dict[str, list[dict[str, Any]]]:
        """The sample of rejected records, per entity (section 56)."""
        return {
            name: [rejected.to_dict() for rejected in sample.kept]
            for name, sample in self._rejects.items()
            if sample.kept
        }

    def rejection_summary(self) -> dict[str, dict[str, Any]]:
        """How many were rejected, how many kept, and whether that is a sample."""
        return {name: sample.summary() for name, sample in self._rejects.items() if sample.seen}

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

    def unique_fields(self, entity_name: str) -> list[str]:
        """Which of an entity's fields are being checked for uniqueness."""
        if not self.validate_records:
            return []
        entity = self.compiled.entity(entity_name)
        return self._validator_for(entity).unique_fields

    def remember_written(self, entity_name: str, values: dict[str, list[Any]]) -> int:
        """Seed the uniqueness trackers with what an earlier attempt wrote."""
        if not self.validate_records:
            return 0
        entity = self.compiled.entity(entity_name)
        return self._validator_for(entity).remember_written(values)

    async def aclose(self) -> None:
        """Release what this run is holding: connections, and files on disk.

        The validators are closed as well as the providers. Their summaries
        survive - they are read from counters, not from the spill file - so a
        finished run still reports everything it measured.
        """
        if self.runtime is not None:
            await self.runtime.aclose()
        for validator in self._validators.values():
            validator.close()

    def policy_overrides(self) -> dict[str, str]:
        """Fields whose own failure policy was used instead of the run's."""
        return dict(self._policy_overrides)

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

    from ..simulation.timeline import (
        _PER_SUBJECT,
        SHAPES,
        ShapeOverrides,
        Timeline,
        Zoning,
        parse_moment,
    )

    zoning = Zoning.from_spec(spec.timezone, seed=compiled.seed)
    # What a stated offset in a bound may mean, which depends on whether the
    # subjects share a clock.
    bound_zone = None if zoning is None else (_PER_SUBJECT if zoning.per_subject else zoning.zone)

    start = parse_moment(spec.start, what="the timeline's start", zone=bound_zone)
    end = parse_moment(spec.end, what="the timeline's end", zone=bound_zone) if spec.end else None
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
    return Timeline(start, end, shape, zoning=zoning)


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
