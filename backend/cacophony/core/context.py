"""The generation context (design document section 98).

Every generator receives one structured object rather than a long positional
signature. New capabilities - scenarios, timelines, world state, related
records - are added as fields here, so generators written today keep working
when later phases land.
"""

from __future__ import annotations

import random

# 'field' is aliased because these dataclasses have an attribute named 'field',
# which would otherwise shadow the dataclasses helper inside the class body.
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import TYPE_CHECKING, Any

from .seeds import SeedChain

if TYPE_CHECKING:  # pragma: no cover - typing only
    import numpy as np

    from ..schema.models import EntitySpec, FieldSpec, ProjectSpec
    from .record import GeneratedRecord

__all__ = ["GenerationContext"]


@dataclass(slots=True)
class GenerationContext:
    """Everything a generator is allowed to know about the value it is producing.

    ``current_record`` holds the values generated *so far* for this record. The
    engine walks fields in dependency order (section 101), so a generator may
    rely on every field it declared a dependency on already being present.
    """

    project: ProjectSpec
    entity: EntitySpec
    record_index: int
    seeds: SeedChain
    field: FieldSpec | None = None
    current_record: dict[str, Any] = dataclass_field(default_factory=dict)
    related_records: dict[str, GeneratedRecord] = dataclass_field(default_factory=dict)
    scenario: Any | None = None
    timeline: Any | None = None
    run_id: str | None = None
    attempt: int = 1
    extras: dict[str, Any] = dataclass_field(default_factory=dict)

    #: Lazily created RNG, reseeded when the context moves to another field.
    _rng: random.Random | None = None
    _rng_seed: int = -1

    # -- seeds ------------------------------------------------------------- #

    @property
    def seed(self) -> int:
        """The seed for this exact (entity, record, field) position."""
        return self.seeds.seed

    def rng(self) -> random.Random:
        """A stdlib RNG derived from this context's position in the hierarchy.

        Repeated calls within one field return the *same* generator, advanced
        by whatever has already been drawn from it. That is what a caller
        expects, and it also avoids allocating a Mersenne Twister per field -
        which was the single largest cost in the deterministic generation loop.
        """
        seed = self.seeds.seed
        if self._rng is None:
            self._rng = random.Random(seed)
        elif self._rng_seed != seed:
            self._rng.seed(seed)
        self._rng_seed = seed
        return self._rng

    def numpy(self) -> np.random.Generator:
        """A NumPy generator derived from this context's position."""
        return self.seeds.numpy()

    def sub_context(self, label: Any) -> GenerationContext:
        """Derive a nested context, e.g. for one element of an array field.

        The nested context gets its own seed so array elements differ from one
        another while remaining reproducible.
        """
        return GenerationContext(
            project=self.project,
            entity=self.entity,
            record_index=self.record_index,
            seeds=self.seeds.sub(label),
            field=self.field,
            current_record=self.current_record,
            related_records=self.related_records,
            scenario=self.scenario,
            timeline=self.timeline,
            run_id=self.run_id,
            attempt=self.attempt,
            extras=self.extras,
        )

    def for_field(self, field_spec: FieldSpec) -> GenerationContext:
        """Derive the context for a specific field of the current record."""
        return GenerationContext(
            project=self.project,
            entity=self.entity,
            record_index=self.record_index,
            seeds=self.seeds.field(field_spec.name),
            field=field_spec,
            current_record=self.current_record,
            related_records=self.related_records,
            scenario=self.scenario,
            timeline=self.timeline,
            run_id=self.run_id,
            attempt=self.attempt,
            extras=self.extras,
        )

    # -- lookups ----------------------------------------------------------- #

    def value(self, name: str, default: Any = None) -> Any:
        """Read a sibling field's already-generated value.

        Dotted names (``company.domain``) resolve against ``related_records``.
        """
        if "." in name:
            head, _, tail = name.partition(".")
            related = self.related_records.get(head)
            if related is None:
                return default
            return related.get(tail, default)
        return self.current_record.get(name, default)

    @property
    def location(self) -> str:
        """A ``entity.field`` label used in error messages and logs."""
        field_name = self.field.name if self.field is not None else "<record>"
        return f"{self.entity.name}.{field_name}"
