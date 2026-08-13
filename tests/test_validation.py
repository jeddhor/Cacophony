"""Validation (design document sections 13, 57 and 58)."""

from __future__ import annotations

import datetime as dt

import pytest

from cacophony.core.record import GeneratedRecord
from cacophony.core.types import DataType
from cacophony.schema.models import ConstraintSpec, FieldSpec
from cacophony.validation.pipeline import RecordValidator
from cacophony.validation.results import Severity, ValidationResult, ValidationStats
from cacophony.validation.validators import ConstraintValidator, StructuralValidator
from helpers import compile_from


class TestStructural:
    def test_correct_type_passes(self) -> None:
        validator = StructuralValidator(FieldSpec(name="n", type=DataType.INTEGER))
        assert validator.validate_sync(5).ok

    def test_wrong_type_is_reported(self) -> None:
        validator = StructuralValidator(FieldSpec(name="n", type=DataType.INTEGER))
        result = validator.validate_sync(object())
        assert not result.ok
        assert result.errors[0].field == "n"

    def test_repairable_value_is_repaired_not_rejected(self) -> None:
        """Section 13: repair before retry, retry before reject."""
        validator = StructuralValidator(FieldSpec(name="n", type=DataType.INTEGER))
        result = validator.validate_sync("42")
        assert result.ok
        assert result.was_repaired
        assert result.repaired_value == 42
        assert result.warnings[0].severity is Severity.WARNING

    def test_null_in_a_non_nullable_field_is_an_error(self) -> None:
        validator = StructuralValidator(FieldSpec(name="n", type=DataType.STRING))
        assert not validator.validate_sync(None).ok

    def test_null_in_a_nullable_field_is_fine(self) -> None:
        validator = StructuralValidator(FieldSpec(name="n", type=DataType.STRING, nullable=True))
        assert validator.validate_sync(None).ok


class TestConstraints:
    def _validator(self, **constraints) -> ConstraintValidator:
        return ConstraintValidator(
            FieldSpec(name="v", type=DataType.INTEGER, constraints=ConstraintSpec(**constraints))
        )

    def test_section_57_minimum(self) -> None:
        """``age >= 18``."""
        validator = self._validator(min=18)
        assert validator.validate_sync(21).ok
        assert not validator.validate_sync(17).ok

    def test_maximum(self) -> None:
        assert not self._validator(max=10).validate_sync(11).ok

    def test_length_bounds(self) -> None:
        validator = ConstraintValidator(
            FieldSpec(
                name="s",
                type=DataType.STRING,
                constraints=ConstraintSpec(min_length=3, max_length=5),
            )
        )
        assert validator.validate_sync("abcd").ok
        assert not validator.validate_sync("ab").ok
        assert not validator.validate_sync("abcdef").ok

    def test_pattern(self) -> None:
        validator = ConstraintValidator(
            FieldSpec(name="s", type=DataType.STRING, constraints=ConstraintSpec(pattern=r"^A\d+$"))
        )
        assert validator.validate_sync("A12").ok
        assert not validator.validate_sync("B12").ok

    def test_enum(self) -> None:
        validator = ConstraintValidator(
            FieldSpec(name="s", type=DataType.STRING, constraints=ConstraintSpec(enum=["a", "b"]))
        )
        assert validator.validate_sync("a").ok
        assert not validator.validate_sync("z").ok

    def test_forbidden_values(self) -> None:
        validator = ConstraintValidator(
            FieldSpec(name="s", type=DataType.STRING, constraints=ConstraintSpec(forbidden=["x"]))
        )
        assert not validator.validate_sync("x").ok

    def test_multiple_of(self) -> None:
        validator = self._validator(multiple_of=5)
        assert validator.validate_sync(25).ok
        assert not validator.validate_sync(26).ok

    def test_date_bounds_written_as_strings(self) -> None:
        """A schema may reasonably spell a date bound as an ISO string."""
        validator = ConstraintValidator(
            FieldSpec(name="d", type=DataType.DATE, constraints=ConstraintSpec(min="2026-01-01"))
        )
        assert validator.validate_sync(dt.date(2026, 6, 1)).ok
        assert not validator.validate_sync(dt.date(2025, 6, 1)).ok

    def test_null_skips_constraints(self) -> None:
        assert self._validator(min=18).validate_sync(None).ok

    def test_a_constraintless_field_is_a_noop(self) -> None:
        assert ConstraintValidator(FieldSpec(name="v")).is_noop


class TestRecordValidator:
    def test_repairs_are_written_back_to_the_record(self) -> None:
        compiled = compile_from(
            {"e": {"fields": {"n": {"type": "integer", "generator": "constant", "value": "7"}}}}
        )
        validator = RecordValidator(compiled.entity("e"))
        record = GeneratedRecord(entity="e", values={"n": "7"})
        assert validator.validate(record).ok
        assert record.values["n"] == 7

    def test_uniqueness_is_enforced(self) -> None:
        compiled = compile_from(
            {
                "e": {
                    "fields": {
                        "k": {
                            "type": "string",
                            "generator": "constant",
                            "value": "same",
                            "unique": True,
                        }
                    }
                }
            }
        )
        validator = RecordValidator(compiled.entity("e"))
        assert validator.validate(GeneratedRecord(entity="e", values={"k": "same"})).ok
        second = validator.validate(GeneratedRecord(entity="e", values={"k": "same"}))
        assert not second.ok
        assert second.errors[0].category == "uniqueness"

    def test_unhashable_values_do_not_break_uniqueness(self) -> None:
        compiled = compile_from(
            {
                "e": {
                    "fields": {
                        "k": {
                            "type": "object",
                            "generator": "constant",
                            "value": {"a": 1},
                            "unique": True,
                        }
                    }
                }
            }
        )
        validator = RecordValidator(compiled.entity("e"))
        assert validator.validate(GeneratedRecord(entity="e", values={"k": {"a": 1}})).ok
        assert not validator.validate(GeneratedRecord(entity="e", values={"k": {"a": 1}})).ok

    def test_reset_clears_state(self) -> None:
        compiled = compile_from(
            {
                "e": {
                    "fields": {
                        "k": {
                            "type": "string",
                            "generator": "constant",
                            "value": "x",
                            "unique": True,
                        }
                    }
                }
            }
        )
        validator = RecordValidator(compiled.entity("e"))
        validator.validate(GeneratedRecord(entity="e", values={"k": "x"}))
        validator.reset()
        assert validator.validate(GeneratedRecord(entity="e", values={"k": "x"})).ok

    def test_statistics_feed_the_quality_metrics(self) -> None:
        """Section 58: schema validity, constraint validity, and so on."""
        compiled = compile_from(
            {
                "e": {
                    "fields": {
                        "n": {
                            "type": "integer",
                            "generator": "constant",
                            "value": 1,
                            "constraints": {"min": 10},
                        }
                    }
                }
            }
        )
        validator = RecordValidator(compiled.entity("e"))
        for _ in range(4):
            validator.validate(GeneratedRecord(entity="e", values={"n": 1}))
        stats = validator.stats.to_dict()
        assert stats["records_checked"] == 4
        assert stats["records_rejected"] == 4
        assert stats["validity_rate"] == 0.0
        assert stats["issues_by_category"]["constraint"] == 4


class TestResults:
    def test_merge(self) -> None:
        left, right = ValidationResult(), ValidationResult()
        right.add("constraint", "bad")
        left.merge(right)
        assert not left.ok

    def test_warnings_do_not_fail_a_result(self) -> None:
        result = ValidationResult()
        result.add("structural", "coerced", severity=Severity.WARNING)
        assert result.ok and result.warnings

    def test_render(self) -> None:
        result = ValidationResult()
        result.add("constraint", "too small", field_name="age")
        assert "age" in result.render()

    def test_empty_stats_report_full_validity(self) -> None:
        assert ValidationStats().validity_rate == 1.0


@pytest.mark.parametrize("mode", ["repair", "no-repair"])
def test_repair_can_be_switched_off(mode: str) -> None:
    compiled = compile_from(
        {"e": {"fields": {"n": {"type": "integer", "generator": "constant", "value": "7"}}}}
    )
    validator = RecordValidator(compiled.entity("e"))
    record = GeneratedRecord(entity="e", values={"n": "7"})
    validator.validate(record, repair=(mode == "repair"))
    assert record.values["n"] == (7 if mode == "repair" else "7")
