"""The record validation pipeline (design document section 13).

    Generate -> Parse -> Validate -> Repair if possible -> Retry if necessary -> Accept

This module owns the validate/repair/accept portion. Retry belongs to the
provider phase, where a failed language-model call can be reissued with a
repair prompt; the pipeline reports enough for that decision to be made.

Validators are built once per entity at compile time. Building a compiled
regex per record per field would dominate the cost of a large run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..simulation.chaos import DUPLICATE_MARK
from .referential import ReferentialValidator, StatisticalValidator
from .results import Severity, ValidationResult, ValidationStats
from .validators import ConstraintValidator, StructuralValidator

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..core.record import GeneratedRecord
    from ..generation.relations import EntityResolver
    from ..schema.plan import CompiledEntity

__all__ = ["RecordValidator"]


class RecordValidator:
    """Validates whole records for one entity.

    Also enforces field-level ``unique: true`` by remembering the values it has
    seen. That set is bounded by the number of unique fields, not by the size
    of the dataset, but it is still per-entity state - so a ten-million-row run
    with a unique field will hold ten million hashes. The linter warns when a
    generator provably cannot satisfy uniqueness; enforcing it at scale without
    that memory is a job for the relational phase's key pools.
    """

    def __init__(
        self,
        entity: CompiledEntity,
        *,
        track_unique: bool = True,
        resolver: EntityResolver | None = None,
        reference_sample_every: int = 1,
    ) -> None:
        self.entity = entity
        self.stats = ValidationStats()

        # Section 57's referential and statistical categories. Both need to
        # know something about the dataset rather than only about the value,
        # so both are optional: without a resolver there is nothing to check a
        # foreign key against.
        self.referential = (
            ReferentialValidator(entity, resolver, sample_every=reference_sample_every)
            if resolver is not None
            else None
        )
        self.statistical = StatisticalValidator(entity)

        self._structural: list[tuple[str, StructuralValidator]] = []
        self._constraint: list[tuple[str, ConstraintValidator]] = []
        for compiled_field in entity.fields:
            self._structural.append((compiled_field.name, StructuralValidator(compiled_field.spec)))
            constraint = ConstraintValidator(compiled_field.spec)
            if not constraint.is_noop:
                self._constraint.append((compiled_field.name, constraint))

        self._unique_fields = (
            [compiled_field.name for compiled_field in entity.fields if compiled_field.spec.unique]
            if track_unique
            else []
        )
        self._seen: dict[str, set[Any]] = {name: set() for name in self._unique_fields}

    def validate_field(self, name: str, value: Any) -> ValidationResult:
        """Check one value against one field, with no side effects.

        Deliberately not part of validating a record: nothing is repaired,
        nothing is remembered for uniqueness, and no distribution is observed.
        It exists so a caller can ask "would this value be legal here?" without
        the asking changing what a later record is checked against.

        Edge-case generation (section 79) is the caller: a candidate that would
        make the record invalid is not an edge case, it is chaos, and the two
        must not be confused.
        """
        result = ValidationResult()
        for field_name, validator in self._structural:
            if field_name == name:
                result.issues.extend(validator.validate_sync(value).issues)
        for field_name, constraint_validator in self._constraint:
            if field_name == name:
                result.issues.extend(constraint_validator.validate_sync(value).issues)
        return result

    def validate(self, record: GeneratedRecord, *, repair: bool = True) -> ValidationResult:
        """Validate one record, optionally applying repairs in place.

        Fields that entropy injection damaged on purpose (section 24) are
        skipped. Validation exists to catch a *generator* producing an invalid
        value; chaos produces invalid values because it was asked to, and
        reporting those would drown the real findings - and, with
        ``--drop-invalid``, would silently discard exactly the records the user
        wanted. The rest of a damaged record is still checked.
        """
        result = ValidationResult()
        damaged = record.damage

        for name, validator in self._structural:
            if name in damaged:
                continue
            field_result = validator.validate_sync(record.values.get(name))
            if field_result.was_repaired and repair:
                record.values[name] = field_result.repaired_value
            result.issues.extend(field_result.issues)
            if field_result.was_repaired:
                result.was_repaired = True

        for name, constraint_validator in self._constraint:
            if name in damaged:
                continue
            result.issues.extend(constraint_validator.validate_sync(record.values.get(name)).issues)

        if self.referential is not None and not self.referential.is_noop:
            result.issues.extend(self.referential.validate(record, skip=damaged).issues)

        if not self.statistical.is_noop:
            self.statistical.observe(record)

        # A record chaos duplicated on purpose carries a duplicate key by
        # construction; reporting it would be reporting the feature.
        deliberate_duplicate = DUPLICATE_MARK in damaged
        for name in self._unique_fields:
            if name in damaged or deliberate_duplicate:
                continue
            value = record.values.get(name)
            if value is None:
                continue
            key = _hashable(value)
            if key in self._seen[name]:
                result.add(
                    "uniqueness",
                    f"duplicate value {value!r} for a field declared unique",
                    field_name=name,
                    severity=Severity.ERROR,
                    value=value,
                )
            else:
                self._seen[name].add(key)

        self.stats.record(result)
        return result

    def reset(self) -> None:
        """Clear per-run state so the validator can be reused for another pass."""
        self.stats = ValidationStats()
        self._seen = {name: set() for name in self._unique_fields}
        self.statistical = StatisticalValidator(self.entity)

    def summary(self) -> dict[str, Any]:
        """Everything this validator learned, for the run report (section 58)."""
        data: dict[str, Any] = self.stats.to_dict()
        if self.referential is not None and not self.referential.is_noop:
            data["referential"] = self.referential.to_dict()
        if not self.statistical.is_noop:
            data["statistical"] = self.statistical.to_dict()
        return data


def _hashable(value: Any) -> Any:
    """Make a generated value usable as a set member."""
    if isinstance(value, (list, tuple)):
        return tuple(_hashable(item) for item in value)
    if isinstance(value, dict):
        return tuple(sorted((key, _hashable(item)) for key, item in value.items()))
    if isinstance(value, set):
        return frozenset(_hashable(item) for item in value)
    try:
        hash(value)
    except TypeError:
        return repr(value)
    return value
