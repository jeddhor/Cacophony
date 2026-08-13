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

from .results import Severity, ValidationResult, ValidationStats
from .validators import ConstraintValidator, StructuralValidator

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..core.record import GeneratedRecord
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

    def __init__(self, entity: CompiledEntity, *, track_unique: bool = True) -> None:
        self.entity = entity
        self.stats = ValidationStats()

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

    def validate(self, record: GeneratedRecord, *, repair: bool = True) -> ValidationResult:
        """Validate one record, optionally applying repairs in place."""
        result = ValidationResult()

        for name, validator in self._structural:
            field_result = validator.validate_sync(record.values.get(name))
            if field_result.was_repaired and repair:
                record.values[name] = field_result.repaired_value
            result.issues.extend(field_result.issues)
            if field_result.was_repaired:
                result.was_repaired = True

        for name, constraint_validator in self._constraint:
            result.issues.extend(constraint_validator.validate_sync(record.values.get(name)).issues)

        for name in self._unique_fields:
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
