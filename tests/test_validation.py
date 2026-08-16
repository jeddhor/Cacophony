"""Validation (design document sections 13, 57 and 58)."""

from __future__ import annotations

import datetime as dt
from typing import Any

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


# --------------------------------------------------------------------------- #
# Referential and statistical validation (design document sections 57, 58)
# --------------------------------------------------------------------------- #


def _relational() -> Any:
    return compile_from(
        {
            "team": {
                "count": 5,
                "primary_key": "team_id",
                "fields": {"team_id": {"type": "integer", "generator": "sequence"}},
            },
            "player": {
                "count": 40,
                "primary_key": "player_id",
                "fields": {
                    "player_id": {"type": "integer", "generator": "sequence"},
                    "team": {"generator": "reference", "entity": "team"},
                    "position": {
                        "generator": "weighted",
                        "choices": {"forward": 40, "midfield": 35, "defence": 25},
                    },
                },
            },
        }
    )


class TestReferentialValidation:
    def test_a_generated_reference_is_accepted(self) -> None:
        import asyncio

        from cacophony.generation.engine import GenerationEngine

        compiled = _relational()
        engine = GenerationEngine(compiled)
        validator = RecordValidator(compiled.entity("player"), resolver=engine.resolver)

        for record in asyncio.run(engine.generate_batch("player", 40)):
            assert validator.validate(record).ok

        assert validator.summary()["referential"]["integrity"] == 1.0

    def test_a_key_outside_the_parent_range_is_reported(self) -> None:
        from cacophony.generation.engine import GenerationEngine

        compiled = _relational()
        engine = GenerationEngine(compiled)
        validator = RecordValidator(compiled.entity("player"), resolver=engine.resolver)

        record = GeneratedRecord(entity="player", values={"player_id": 1, "team": 9999})
        result = validator.validate(record)
        assert not result.ok
        assert any(issue.category == "referential" for issue in result.issues)

    def test_sampling_reports_how_much_it_looked_at(self) -> None:
        import asyncio

        from cacophony.generation.engine import GenerationEngine

        compiled = _relational()
        engine = GenerationEngine(compiled)
        validator = RecordValidator(
            compiled.entity("player"), resolver=engine.resolver, reference_sample_every=10
        )
        for record in asyncio.run(engine.generate_batch("player", 40)):
            validator.validate(record)

        referential = validator.summary()["referential"]
        assert referential["sample_every"] == 10
        assert referential["references_checked"] == 4

    def test_an_entity_with_no_references_says_nothing(self) -> None:
        compiled = _relational()
        validator = RecordValidator(compiled.entity("team"))
        assert "referential" not in validator.summary()


class TestStatisticalValidation:
    def test_a_faithful_distribution_scores_near_one(self) -> None:
        import asyncio

        from cacophony.generation.engine import GenerationEngine

        compiled = _relational()
        engine = GenerationEngine(compiled)
        validator = RecordValidator(compiled.entity("player"))
        for record in asyncio.run(engine.generate_batch("player", 2000)):
            validator.validate(record)

        statistical = validator.summary()["statistical"]
        assert statistical["distribution_match"] > 0.9
        check = statistical["checks"][0]
        assert check["field"] == "position"
        assert check["expected"]["forward"] == pytest.approx(0.4)

    def test_a_small_sample_is_labelled_as_one(self) -> None:
        from cacophony.validation.referential import DistributionCheck

        check = DistributionCheck(
            entity="player",
            field="position",
            expected={"a": 0.5, "b": 0.5},
            observed={"a": 0.5, "b": 0.5},
            samples=12,
            distance=0.0,
        )
        assert not check.confident
        assert check.match == 1.0

    def test_the_worst_offender_is_named(self) -> None:
        from cacophony.validation.referential import DistributionCheck

        check = DistributionCheck(
            entity="e",
            field="f",
            expected={"a": 0.6, "b": 0.3, "c": 0.1},
            observed={"a": 0.2, "b": 0.35, "c": 0.45},
            samples=1000,
            distance=0.4,
        )
        name, expected, observed = check.worst()
        assert (name, expected, observed) == ("a", 0.6, 0.2)

    def test_a_wrong_distribution_is_reported_as_a_warning(self) -> None:
        from cacophony.validation.referential import StatisticalValidator

        compiled = _relational()
        validator = StatisticalValidator(compiled.entity("player"))
        # Everything came out "forward" when 40% was declared.
        for _ in range(500):
            validator.observe(GeneratedRecord(entity="player", values={"position": "forward"}))
        result = validator.report()
        assert not result.ok or result.warnings
        assert "distribution" in result.render()

    def test_an_entity_with_no_declared_distribution_says_nothing(self) -> None:
        compiled = _relational()
        validator = RecordValidator(compiled.entity("team"))
        assert "statistical" not in validator.summary()


class TestQualityReport:
    def test_it_renders_section_58s_shape(self) -> None:
        from cacophony.validation.referential import QualityReport

        report = QualityReport(referential_integrity=0.998, distribution_match=0.91)
        rendered = report.render()
        assert "Referential Integrity" in rendered
        assert "99.80%" in rendered

    def test_sample_size_never_pretends_a_dozen_records_is_enough(self) -> None:
        from cacophony.validation.referential import sample_size_for

        assert sample_size_for(2) >= 100
        assert sample_size_for(50) > sample_size_for(5)
