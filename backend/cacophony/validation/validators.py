"""Structural and constraint validators (design document section 57).

Section 57 names six categories. Two are implemented here:

``structural``  correct type and schema
``constraint``  ``age >= 18``

The other four - ``referential`` (foreign key exists), ``logical``
(``termination_date >= hire_date``), ``statistical`` (generated distributions
match target distributions) and ``semantic`` (optional LLM evaluation) - need
machinery from later phases, and are registered as recognised categories so
their results slot into the same report.

Structural validation *repairs* before it rejects. A lookup table that yields
``"42"`` for an integer field is a coercion problem, not a data problem;
section 13's pipeline is explicitly generate -> parse -> validate -> repair ->
retry -> accept.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from ..core.interfaces import Validator
from ..core.types import DataType, check_value, coerce_value
from .results import Severity, ValidationResult

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..core.context import GenerationContext
    from ..schema.models import ConstraintSpec, FieldSpec

__all__ = ["ConstraintValidator", "StructuralValidator"]


class StructuralValidator(Validator):
    """Check that a value matches its field's declared type, repairing if it can."""

    category = "structural"

    def __init__(self, field_spec: FieldSpec) -> None:
        self.field_spec = field_spec

    async def validate(self, value: Any, context: GenerationContext) -> ValidationResult:
        return self.validate_sync(value)

    def validate_sync(self, value: Any) -> ValidationResult:
        result = ValidationResult()
        name = self.field_spec.name
        data_type = self.field_spec.type

        if value is None:
            if not self.field_spec.nullable and self.field_spec.null_probability <= 0.0:
                result.add(
                    self.category,
                    "value is null but the field is not nullable",
                    field_name=name,
                )
            return result

        reason = check_value(value, data_type)
        if reason is None:
            return result

        repaired = coerce_value(value, data_type)
        if check_value(repaired, data_type) is None:
            result.repaired_value = repaired
            result.was_repaired = True
            result.add(
                self.category,
                f"{reason}; coerced to {data_type.value}",
                field_name=name,
                severity=Severity.WARNING,
                value=value,
            )
            return result

        result.add(self.category, reason, field_name=name, value=value)
        return result


class ConstraintValidator(Validator):
    """Check a value against the field's declared constraints."""

    category = "constraint"

    def __init__(self, field_spec: FieldSpec) -> None:
        self.field_spec = field_spec
        self.constraints: ConstraintSpec = field_spec.constraints
        self._pattern = re.compile(self.constraints.pattern) if self.constraints.pattern else None
        self._enum = set(self.constraints.enum) if self.constraints.enum else None
        self._forbidden = set(self.constraints.forbidden) if self.constraints.forbidden else None

    @property
    def is_noop(self) -> bool:
        return self.constraints.is_empty()

    async def validate(self, value: Any, context: GenerationContext) -> ValidationResult:
        return self.validate_sync(value)

    def validate_sync(self, value: Any) -> ValidationResult:
        result = ValidationResult()
        if value is None:
            return result

        name = self.field_spec.name
        constraints = self.constraints

        if constraints.min is not None and _less_than(value, constraints.min):
            result.add(
                self.category,
                f"{value!r} is below the minimum {constraints.min!r}",
                field_name=name,
            )
        if constraints.max is not None and _less_than(constraints.max, value):
            result.add(
                self.category,
                f"{value!r} is above the maximum {constraints.max!r}",
                field_name=name,
            )

        if constraints.min_length is not None or constraints.max_length is not None:
            length = _length_of(value)
            if length is None:
                result.add(
                    self.category,
                    f"length constraints do not apply to {type(value).__name__}",
                    field_name=name,
                    severity=Severity.WARNING,
                )
            else:
                if constraints.min_length is not None and length < constraints.min_length:
                    result.add(
                        self.category,
                        f"length {length} is below the minimum {constraints.min_length}",
                        field_name=name,
                    )
                if constraints.max_length is not None and length > constraints.max_length:
                    result.add(
                        self.category,
                        f"length {length} exceeds the maximum {constraints.max_length}",
                        field_name=name,
                    )

        if self._pattern is not None and not self._pattern.search(str(value)):
            result.add(
                self.category,
                f"{value!r} does not match pattern {constraints.pattern!r}",
                field_name=name,
            )

        if self._enum is not None and value not in self._enum:
            result.add(
                self.category, f"{value!r} is not one of the permitted values", field_name=name
            )

        if self._forbidden is not None and value in self._forbidden:
            result.add(self.category, f"{value!r} is a forbidden value", field_name=name)

        if constraints.multiple_of is not None and _is_number(value):
            remainder = float(value) % float(constraints.multiple_of)
            if abs(remainder) > 1e-9 and abs(remainder - float(constraints.multiple_of)) > 1e-9:
                result.add(
                    self.category,
                    f"{value!r} is not a multiple of {constraints.multiple_of}",
                    field_name=name,
                )

        return result


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _less_than(left: Any, right: Any) -> bool:
    """Order-compare two values, tolerating a numeric bound against a text value.

    A schema may reasonably write ``min: "2025-01-01"`` for a date field, so
    string and date comparisons both have to work. Anything genuinely
    incomparable is treated as satisfying the constraint - the structural
    validator has already reported the type mismatch, and reporting it twice
    just makes the report harder to read.
    """
    try:
        return bool(left < right)
    except TypeError:
        pass
    try:
        return float(left) < float(right)
    except (TypeError, ValueError):
        pass
    try:
        return str(left) < str(right)
    except TypeError:
        return False


def _length_of(value: Any) -> int | None:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, (list, tuple, dict, set, bytes)):
        return len(value)
    if isinstance(value, DataType):
        return None
    return None
