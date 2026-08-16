"""The compiled project and its generation plan (design document sections 28, 69, 101).

Compiling a project turns a declarative schema into an *explicit* plan: which
entities are built in which order, which fields within them, by which
generator, and roughly what that will cost. The plan is a first-class artifact
because the UI is meant to show it before anything runs (section 28), and
because a user who can read the plan can see why a value was produced
(section 4, *Inspectable*).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..core.interfaces import Generator, SyncGenerator
from ..core.seeds import SeedChain

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .models import EntitySpec, FieldSpec, ProjectSpec

__all__ = [
    "CompiledEntity",
    "CompiledField",
    "CompiledProject",
    "GenerationPlan",
    "PlanStep",
    "WorkloadEstimate",
]


@dataclass(slots=True)
class CompiledField:
    """A field with its generator resolved and its dependencies known."""

    name: str
    spec: FieldSpec
    generator: Generator
    dependencies: tuple[str, ...] = ()
    related_entities: tuple[str, ...] = ()
    inferred_generator: bool = False

    @property
    def is_sync(self) -> bool:
        """Whether the engine may take the synchronous fast path."""
        return isinstance(self.generator, SyncGenerator)

    @property
    def generator_name(self) -> str:
        return self.generator.name or type(self.generator).__name__

    @property
    def null_probability(self) -> float:
        return self.spec.effective_null_probability


@dataclass(slots=True)
class CompiledEntity:
    """An entity with its fields ordered topologically."""

    name: str
    spec: EntitySpec
    fields: list[CompiledField] = field(default_factory=list)
    depends_on: tuple[str, ...] = ()
    field_layers: list[list[str]] = field(default_factory=list)
    #: Referenced entity -> the field of this entity that points at it. This
    #: is what lets a field reading ``company.domain`` be given the company
    #: *this record* chose rather than an arbitrary one.
    reference_fields: dict[str, str] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return self.spec.count

    @property
    def field_order(self) -> list[str]:
        return [compiled.name for compiled in self.fields]

    def field(self, name: str) -> CompiledField:
        for compiled in self.fields:
            if compiled.name == name:
                return compiled
        raise KeyError(f"Entity '{self.name}' has no field '{name}'")

    def layers(self) -> list[list[CompiledField]]:
        """Fields grouped into levels that may be produced together.

        Layer 0 depends on nothing; layer *n* depends only on earlier layers.
        Within a layer the fields are independent, which is what allows several
        language-model fields to share one call (section 11).
        """
        by_name = {compiled.name: compiled for compiled in self.fields}
        if not self.field_layers:
            return [list(self.fields)]
        return [[by_name[name] for name in layer] for layer in self.field_layers]

    def seed_chain(self, project_seed: int) -> SeedChain:
        """The seed chain rooted at this entity.

        An entity may pin its own seed so that regenerating one entity does not
        disturb the others.
        """
        base = self.spec.seed if self.spec.seed is not None else project_seed
        return SeedChain.root(base).entity(self.name)


@dataclass(slots=True)
class WorkloadEstimate:
    """A deliberately approximate pre-flight estimate (sections 54 and 69).

    Section 69 is explicit that estimates must not pretend to be exact, so
    these are order-of-magnitude figures derived from record counts and field
    cost classes - never a promise.
    """

    records: int = 0
    fields: int = 0
    llm_calls: int = 0
    image_calls: int = 0
    speech_calls: int = 0
    estimated_bytes: int = 0

    def merge(self, other: WorkloadEstimate) -> WorkloadEstimate:
        return WorkloadEstimate(
            records=self.records + other.records,
            fields=self.fields + other.fields,
            llm_calls=self.llm_calls + other.llm_calls,
            image_calls=self.image_calls + other.image_calls,
            speech_calls=self.speech_calls + other.speech_calls,
            estimated_bytes=self.estimated_bytes + other.estimated_bytes,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "records": self.records,
            "fields": self.fields,
            "llm_calls": self.llm_calls,
            "image_calls": self.image_calls,
            "speech_calls": self.speech_calls,
            "estimated_bytes": self.estimated_bytes,
        }


@dataclass(slots=True)
class PlanStep:
    """One line of the human-readable plan."""

    entity: str
    count: int
    fields: list[str]
    generators: dict[str, str]
    depends_on: tuple[str, ...] = ()
    estimate: WorkloadEstimate = field(default_factory=WorkloadEstimate)

    def render(self, indent: str = "") -> list[str]:
        lines = [f"{indent}Generate {self.count:,} {self.entity}"]
        if self.depends_on:
            lines.append(f"{indent}  after: {', '.join(self.depends_on)}")
        for name in self.fields:
            lines.append(f"{indent}  {name:<28} {self.generators.get(name, '?')}")
        return lines


@dataclass(slots=True)
class GenerationPlan:
    """The full ordered plan for a run."""

    project_name: str
    seed: int
    steps: list[PlanStep] = field(default_factory=list)
    entity_order: tuple[str, ...] = ()
    estimate: WorkloadEstimate = field(default_factory=WorkloadEstimate)
    warnings: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            f"GENERATION PLAN - {self.project_name}",
            f"seed {self.seed}",
            "",
        ]
        for step in self.steps:
            lines.extend(step.render())
            lines.append("")
        lines.append(
            f"Totals: {self.estimate.records:,} records, "
            f"{self.estimate.fields:,} field values, "
            f"{self.estimate.llm_calls:,} language-model calls"
        )
        if self.warnings:
            lines.append("")
            lines.append("Warnings:")
            lines.extend(f"  - {warning}" for warning in self.warnings)
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project_name,
            "seed": self.seed,
            "entity_order": list(self.entity_order),
            "steps": [
                {
                    "entity": step.entity,
                    "count": step.count,
                    "fields": step.fields,
                    "generators": step.generators,
                    "depends_on": list(step.depends_on),
                    "estimate": step.estimate.to_dict(),
                }
                for step in self.steps
            ],
            "estimate": self.estimate.to_dict(),
            "warnings": list(self.warnings),
        }


@dataclass(slots=True)
class CompiledProject:
    """Everything the engine needs to execute a run."""

    spec: ProjectSpec
    entities: dict[str, CompiledEntity] = field(default_factory=dict)
    entity_order: tuple[str, ...] = ()
    plan: GenerationPlan | None = None

    @property
    def name(self) -> str:
        return self.spec.project.name

    @property
    def seed(self) -> int:
        return self.spec.project.seed

    def entity(self, name: str) -> CompiledEntity:
        try:
            return self.entities[name]
        except KeyError as exc:
            known = ", ".join(self.entity_order) or "<none>"
            raise KeyError(f"No compiled entity '{name}'. Known entities: {known}") from exc

    def ordered_entities(self) -> list[CompiledEntity]:
        return [self.entities[name] for name in self.entity_order]
