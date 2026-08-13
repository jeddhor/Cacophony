"""The schema compiler (design document section 100).

The compiler performs the seven steps the design document calls for:

1. parse project configuration  (done by :mod:`cacophony.schema.loader`)
2. validate schema
3. resolve generators
4. identify dependencies
5. detect cycles
6. calculate entity ordering
7. construct the generation plan

Resolving generators is the interesting step. A field may name its generator
explicitly, or it may say nothing but ``semantic: "Person's given name"`` - in
which case the recommendation engine (section 68) picks one. Making that
decision at compile time rather than at generation time means the choice is
visible in the plan, and means a bad option is reported by ``cacophony
validate`` instead of two million records into a run.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..core.errors import GeneratorConfigError, SchemaError, UnknownFieldReferenceError
from ..core.types import DataType
from .graph import DependencyGraph
from .models import EntitySpec, FieldSpec, ProjectSpec
from .plan import (
    CompiledEntity,
    CompiledField,
    CompiledProject,
    GenerationPlan,
    PlanStep,
    WorkloadEstimate,
)

__all__ = ["compile_project"]


#: Rough on-disk size per value, by type, used only for order-of-magnitude
#: estimates (section 69 - estimates must not pretend to be exact).
_BYTES_PER_VALUE: dict[DataType, int] = {
    DataType.TEXT: 400,
    DataType.JSON: 200,
    DataType.OBJECT: 200,
    DataType.ARRAY: 120,
    DataType.STRING: 24,
    DataType.UUID: 36,
    DataType.DATETIME: 24,
    DataType.DATE: 10,
    DataType.TIME: 8,
    DataType.INTEGER: 8,
    DataType.FLOAT: 8,
    DataType.DECIMAL: 16,
    DataType.BOOLEAN: 1,
}
_DEFAULT_BYTES_PER_VALUE = 32

#: Very rough per-call token cost, used for the pre-flight LLM estimate.
_TOKENS_PER_LLM_FIELD = 180


def compile_project(project: ProjectSpec) -> CompiledProject:
    """Compile a validated schema into an executable :class:`CompiledProject`."""
    # Imported here rather than at module scope: the generation package builds
    # on core, and the compiler builds on both. A local import keeps the
    # package-level dependency arrow pointing one way.
    from ..generation.recommend import recommend_generator
    from ..generation.registry import REGISTRY

    if not project.entities:
        raise SchemaError("The project defines no entities; there is nothing to generate.")

    compiled_entities: dict[str, CompiledEntity] = {}
    entity_graph = DependencyGraph(kind="entity")
    for entity_name in project.entities:
        entity_graph.add_node(entity_name)

    for entity_name, entity_spec in project.entities.items():
        compiled = _compile_entity(
            project=project,
            entity=entity_spec,
            registry=REGISTRY,
            recommend=recommend_generator,
        )
        compiled_entities[entity_name] = compiled
        for dependency in compiled.depends_on:
            entity_graph.add_dependency(entity_name, dependency)

    # Relationships imply ordering too: the "one" side must exist before the
    # "many" side can point at it.
    for relationship in project.relationships:
        _require_entity(project, relationship.from_entity, f"relationship '{relationship.name}'")
        _require_entity(project, relationship.to_entity, f"relationship '{relationship.name}'")
        if relationship.cardinality in ("one_to_many", "one_to_one"):
            entity_graph.add_dependency(relationship.to_entity, relationship.from_entity)
        else:
            entity_graph.add_dependency(relationship.from_entity, relationship.to_entity)

    entity_order = tuple(entity_graph.topological_order())

    compiled_project = CompiledProject(
        spec=project,
        entities=compiled_entities,
        entity_order=entity_order,
    )
    compiled_project.plan = build_plan(compiled_project)
    return compiled_project


# --------------------------------------------------------------------------- #
# Entity compilation
# --------------------------------------------------------------------------- #


def _compile_entity(
    *,
    project: ProjectSpec,
    entity: EntitySpec,
    registry: object,
    recommend: object,
) -> CompiledEntity:
    if not entity.fields:
        raise SchemaError(f"Entity '{entity.name}' defines no fields.")

    field_graph = DependencyGraph(kind="field")
    for field_name in entity.fields:
        field_graph.add_node(field_name)

    compiled_fields: dict[str, CompiledField] = {}
    related_entities: set[str] = set()

    for field_name, field_spec in entity.fields.items():
        compiled = _compile_field(
            project=project,
            entity=entity,
            field_spec=field_spec,
            registry=registry,
            recommend=recommend,
        )
        compiled_fields[field_name] = compiled

        for dependency in compiled.dependencies:
            if dependency not in entity.fields:
                raise UnknownFieldReferenceError(entity.name, field_name, dependency)
            field_graph.add_dependency(field_name, dependency)

        for other in compiled.related_entities:
            _require_entity(project, other, f"{entity.name}.{field_name}")
            if other != entity.name:
                related_entities.add(other)

    field_order = field_graph.topological_order()

    return CompiledEntity(
        name=entity.name,
        spec=entity,
        fields=[compiled_fields[name] for name in field_order],
        depends_on=tuple(sorted(related_entities)),
        field_layers=field_graph.layers(),
    )


def _compile_field(
    *,
    project: ProjectSpec,
    entity: EntitySpec,
    field_spec: FieldSpec,
    registry: object,
    recommend: object,
) -> CompiledField:
    inferred = False

    if field_spec.has_explicit_generator:
        assert field_spec.generator is not None  # narrowed by has_explicit_generator
        generator_name = field_spec.generator.type
        options = dict(field_spec.generator.options)
    else:
        recommendation = recommend(field_spec, entity=entity, project=project)  # type: ignore[operator]
        generator_name = recommendation.generator
        options = {**recommendation.options, **field_spec.pending_options}
        inferred = True

    try:
        generator = registry.create(  # type: ignore[attr-defined]
            generator_name, options, field=field_spec, entity=entity
        )
    except GeneratorConfigError as exc:
        raise GeneratorConfigError(
            generator_name,
            str(exc).split(": ", 1)[-1],
            location=f"{entity.name}.{field_spec.name}",
        ) from exc

    own_fields, cross_entity = _split_references(
        [*field_spec.depends_on, *generator.dependencies()]
    )
    related = set(cross_entity)

    # Section 49's field editor lists 'Context' as a mix of related entities
    # and sibling fields ("employee", "device", "ticket_category"), so each
    # name is resolved against both. A name that is neither is a typo, and the
    # error names both things it could have meant.
    for name in field_spec.context:
        if name in entity.fields:
            own_fields.append(name)
        elif name in project.entities:
            related.add(name)
        else:
            raise SchemaError(
                f"{entity.name}.{field_spec.name}: context lists '{name}', which is neither "
                f"a field of '{entity.name}' nor a known entity."
            )

    # A generator may name the entity it references, e.g. ``reference: employee``.
    referenced = options.get("entity") or options.get("references")
    if isinstance(referenced, str):
        related.add(referenced)

    return CompiledField(
        name=field_spec.name,
        spec=field_spec,
        generator=generator,
        dependencies=tuple(dict.fromkeys(own_fields)),
        related_entities=tuple(sorted(related)),
        inferred_generator=inferred,
    )


def _split_references(references: Iterable[str]) -> tuple[list[str], list[str]]:
    """Separate ``last_name`` (same entity) from ``company.domain`` (another entity)."""
    own: list[str] = []
    cross: list[str] = []
    for reference in references:
        if "." in reference:
            cross.append(reference.split(".", 1)[0])
        else:
            own.append(reference)
    return own, cross


def _require_entity(project: ProjectSpec, name: str, where: str) -> None:
    if name not in project.entities:
        known = ", ".join(sorted(project.entities)) or "<none>"
        raise SchemaError(f"{where} refers to unknown entity '{name}'. Known entities: {known}")


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #


def build_plan(compiled: CompiledProject) -> GenerationPlan:
    """Turn a compiled project into the inspectable plan of section 28."""
    steps: list[PlanStep] = []
    total = WorkloadEstimate()

    for entity in compiled.ordered_entities():
        estimate = _estimate_entity(entity)
        steps.append(
            PlanStep(
                entity=entity.name,
                count=entity.count,
                fields=entity.field_order,
                generators={
                    compiled_field.name: compiled_field.generator.describe()
                    for compiled_field in entity.fields
                },
                depends_on=entity.depends_on,
                estimate=estimate,
            )
        )
        total = total.merge(estimate)

    return GenerationPlan(
        project_name=compiled.name,
        seed=compiled.seed,
        steps=steps,
        entity_order=compiled.entity_order,
        estimate=total,
    )


def _estimate_entity(entity: CompiledEntity) -> WorkloadEstimate:
    count = entity.count
    bytes_per_record = 0
    llm_fields = 0
    image_fields = 0
    speech_fields = 0

    for compiled_field in entity.fields:
        bytes_per_record += _BYTES_PER_VALUE.get(compiled_field.spec.type, _DEFAULT_BYTES_PER_VALUE)
        provider_kind = type(compiled_field.generator).requires_provider
        if provider_kind == "language_model":
            llm_fields += 1
        elif provider_kind == "image":
            image_fields += 1
        elif provider_kind == "speech":
            speech_fields += 1

    return WorkloadEstimate(
        records=count,
        fields=count * len(entity.fields),
        llm_calls=count * llm_fields,
        image_calls=count * image_fields,
        speech_calls=count * speech_fields,
        estimated_bytes=count * bytes_per_record + count * llm_fields * _TOKENS_PER_LLM_FIELD * 4,
    )
