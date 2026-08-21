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
from .logical import LogicalValidator
from .privacy import PrivacyValidator
from .referential import ReferentialValidator, StatisticalValidator
from .results import Severity, ValidationResult, ValidationStats
from .uniqueness import DEFAULT_MEMORY_CEILING, UniqueTracker
from .validators import ConstraintValidator, StructuralValidator

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..core.record import GeneratedRecord
    from ..generation.relations import EntityResolver
    from ..schema.models import PrivacySpec
    from ..schema.plan import CompiledEntity

__all__ = ["RecordValidator"]


class RecordValidator:
    """Validates whole records for one entity.

    Also enforces field-level ``unique: true`` by remembering the values it has
    seen. That memory used to grow with the dataset - a ten-million-row run held
    ten million values for its whole duration, which is exactly the shape section
    31 says nothing here should have. It is now bounded: see
    :class:`~cacophony.validation.uniqueness.UniqueTracker`, which holds values
    in a set up to a stated ceiling and spills to a disk-backed index after that,
    without giving up exactness.
    """

    def __init__(
        self,
        entity: CompiledEntity,
        *,
        track_unique: bool = True,
        resolver: EntityResolver | None = None,
        reference_sample_every: int = 1,
        privacy: PrivacySpec | None = None,
        unique_memory_ceiling: int = DEFAULT_MEMORY_CEILING,
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

        # Section 57's logical category: rules about a record rather than a
        # value, so they need every field before they can be asked.
        self.logical = LogicalValidator(entity)

        # Section 61's detectors, which a project has to ask for.
        privacy_spec = getattr(entity, "privacy", None) or privacy
        self.privacy = PrivacyValidator(entity, privacy_spec) if privacy_spec else None

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
        self._seen: dict[str, UniqueTracker] = {
            name: UniqueTracker(name, memory_ceiling=unique_memory_ceiling)
            for name in self._unique_fields
        }

        #: What the most recently validated record added to ``_seen``, so that a
        #: record which is then discarded can give its values back.
        self._last_added: list[tuple[str, Any]] = []

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
        self._last_added = []

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

        if not self.logical.is_noop:
            result.issues.extend(self.logical.validate(record, skip=set(damaged)).issues)

        if self.privacy is not None and not self.privacy.is_noop:
            result.issues.extend(self.privacy.validate(record, skip=set(damaged)).issues)

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
            if not self._seen[name].add(value):
                result.add(
                    "uniqueness",
                    f"duplicate value {value!r} for a field declared unique",
                    field_name=name,
                    severity=Severity.ERROR,
                    value=value,
                )
            else:
                self._last_added.append((name, value))

        self.stats.record(result)
        return result

    def forget_last(self) -> None:
        """Give back the unique values the last validated record introduced.

        Called when that record is discarded or regenerated. Without it, a
        record dropped for a length violation would still be holding its email
        address, and a later record that produced the same address would be
        rejected as a duplicate of a record nobody has.
        """
        for name, value in self._last_added:
            self._seen[name].forget([value])
        self._last_added = []

    def reset(self) -> None:
        """Clear per-run state so the validator can be reused for another pass."""
        self.stats = ValidationStats()
        for tracker in self._seen.values():
            tracker.reset()
        self._last_added = []
        self.statistical = StatisticalValidator(self.entity)

    def close(self) -> None:
        """Release what uniqueness tracking is holding on disk.

        Nothing collected these before: the API keeps a finished run's
        validator around so its metrics stay readable, so a spilled tracker's
        database and temporary directory outlived every run that made one.
        """
        for tracker in self._seen.values():
            tracker.close()

    def summary(self) -> dict[str, Any]:
        """Everything this validator learned, for the run report (section 58)."""
        data: dict[str, Any] = self.stats.to_dict()
        if self.referential is not None and not self.referential.is_noop:
            data["referential"] = self.referential.to_dict()
        if self.privacy is not None and not self.privacy.is_noop:
            data["privacy"] = self.privacy.summary()
        spilled = [
            tracker.summary() for tracker in self._seen.values() if tracker.summary()["spilled"]
        ]
        if spilled:
            # Worth saying: it is the one thing in a run whose memory profile
            # changed shape, and somebody reading a slow run should know why.
            data["uniqueness_spilled"] = spilled
        if not self.statistical.is_noop:
            data["statistical"] = self.statistical.to_dict()
        return data
