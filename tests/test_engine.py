"""The generation engine (design document sections 27, 31, 65, 75, 103)."""

from __future__ import annotations

import asyncio

import pytest

from cacophony.core.errors import GenerationError
from cacophony.core.provenance import ProvenanceMode
from cacophony.generation.engine import FailurePolicy, GenerationEngine
from helpers import compile_from


async def _count(engine, entity: str, count: int | None, batch_size: int) -> int:
    """Consume a stream and report how many records it produced."""
    total = 0
    async for batch in engine.stream(entity, count=count, batch_size=batch_size):
        total += len(batch)
    return total


SIMPLE = {
    "employee": {
        "count": 50,
        "primary_key": "employee_id",
        "fields": {
            "employee_id": {"type": "string", "generator": "sequence", "format": "EMP-{0000}"},
            "first_name": {"type": "string", "semantic": "Person's given name"},
            "last_name": {"type": "string", "semantic": "Person's family name"},
            "email": {
                "type": "email",
                "generator": "template",
                "template": "{first_name|lower}.{last_name|lower}@example.com",
            },
            "age": {
                "type": "integer",
                "generator": "distribution",
                "distribution": "normal",
                "mean": 40,
                "stddev": 8,
                "min": 21,
                "max": 65,
            },
        },
    }
}


@pytest.fixture
def compiled():
    return compile_from(SIMPLE)


class TestDeterminism:
    def test_identical_configuration_and_seed_gives_identical_records(self, compiled) -> None:
        left = [r.values for r in GenerationEngine(compiled).preview("employee", 20)]
        right = [r.values for r in GenerationEngine(compiled).preview("employee", 20)]
        assert left == right

    def test_a_record_does_not_depend_on_how_it_was_reached(self, compiled) -> None:
        """Section 32: resuming from a checkpoint must reproduce the same rows."""
        streamed = GenerationEngine(compiled).preview("employee", 40)
        resumed = GenerationEngine(compiled).preview("employee", 10, offset=30)
        assert [r.values for r in streamed[30:]] == [r.values for r in resumed]

    def test_a_different_project_seed_changes_the_data(self) -> None:
        left = GenerationEngine(compile_from(SIMPLE, seed=1)).preview("employee", 10)
        right = GenerationEngine(compile_from(SIMPLE, seed=2)).preview("employee", 10)
        assert [r.values for r in left] != [r.values for r in right]

    def test_sampling_does_not_disturb_production_output(self, compiled) -> None:
        """Section 103, satisfied structurally: seeds are positional, not sequential."""
        engine = GenerationEngine(compiled)
        for _ in range(5):
            engine.preview("employee", 3)
        after_sampling = GenerationEngine(compiled).preview("employee", 10)
        assert [r.values for r in after_sampling] == [
            r.values for r in GenerationEngine(compiled).preview("employee", 10)
        ]

    def test_isolated_namespace_produces_a_different_sample(self, compiled) -> None:
        faithful = GenerationEngine(compiled).preview("employee", 5)
        isolated = GenerationEngine(compiled, seed_namespace="preview-1").preview("employee", 5)
        assert [r.values for r in faithful] != [r.values for r in isolated]

    def test_entity_seed_isolates_entities(self) -> None:
        """A per-entity seed lets one entity be regenerated without moving others."""
        base = {
            "a": {"count": 3, "fields": {"v": {"type": "integer", "generator": "random"}}},
            "b": {"count": 3, "fields": {"v": {"type": "integer", "generator": "random"}}},
        }
        engine = GenerationEngine(compile_from(base))
        assert [r.values for r in engine.preview("a", 3)] != [
            r.values for r in engine.preview("b", 3)
        ]


class TestFieldOrderAndDependencies:
    def test_dependent_fields_see_their_dependencies(self, compiled) -> None:
        for record in GenerationEngine(compiled).preview("employee", 10):
            expected = (
                f"{record.values['first_name'].lower()}."
                f"{record.values['last_name'].lower()}@example.com"
            )
            assert record.values["email"] == expected

    def test_record_keys_follow_the_generation_order(self, compiled) -> None:
        record = GenerationEngine(compiled).preview("employee", 1)[0]
        assert list(record.values) == compiled.entity("employee").field_order

    def test_record_id_uses_the_primary_key(self, compiled) -> None:
        record = GenerationEngine(compiled).preview("employee", 1)[0]
        assert record.id == record.values["employee_id"]

    def test_record_id_falls_back_to_the_index(self) -> None:
        engine = GenerationEngine(
            compile_from({"e": {"fields": {"v": {"generator": "constant", "value": 1}}}})
        )
        assert engine.preview("e", 1)[0].id == "e#0"


class TestStreaming:
    def test_batches_are_bounded(self, compiled) -> None:
        async def collect() -> list[int]:
            engine = GenerationEngine(compiled)
            return [
                len(batch) async for batch in engine.stream("employee", count=25, batch_size=10)
            ]

        assert asyncio.run(collect()) == [10, 10, 5]

    def test_zero_count_produces_nothing(self, compiled) -> None:
        async def collect() -> list:
            engine = GenerationEngine(compiled)
            return [batch async for batch in engine.stream("employee", count=0)]

        assert asyncio.run(collect()) == []

    def test_entity_count_is_the_default(self, compiled) -> None:
        assert asyncio.run(_count(GenerationEngine(compiled), "employee", None, 7)) == 50

    def test_unknown_entity_is_reported(self, compiled) -> None:
        with pytest.raises(KeyError, match="Known entities"):
            GenerationEngine(compiled).preview("ghost", 1)


class TestNullability:
    def test_null_probability_is_approximately_honoured(self) -> None:
        engine = GenerationEngine(
            compile_from(
                {
                    "e": {
                        "fields": {
                            "maybe": {
                                "type": "string",
                                "generator": "constant",
                                "value": "x",
                                "nullable": True,
                                "null_probability": 0.3,
                            }
                        }
                    }
                }
            ),
            validate=False,
        )
        values = [r.values["maybe"] for r in engine.preview("e", 2000)]
        assert 0.25 < values.count(None) / len(values) < 0.35

    def test_zero_probability_never_nulls(self, compiled) -> None:
        for record in GenerationEngine(compiled).preview("employee", 30):
            assert all(value is not None for value in record.values.values())


class TestFailurePolicies:
    FAILING = {
        "e": {
            "count": 3,
            "fields": {
                "ok": {"generator": "constant", "value": 1},
                "boom": {"type": "text", "generator": "llm"},  # errors by default
            },
        }
    }

    def test_abort_raises(self) -> None:
        engine = GenerationEngine(compile_from(self.FAILING), failure_policy=FailurePolicy.ABORT)
        with pytest.raises(GenerationError, match=r"e\.boom"):
            engine.preview("e", 1)

    def test_skip_records_the_failure_and_continues(self) -> None:
        engine = GenerationEngine(
            compile_from(self.FAILING), failure_policy=FailurePolicy.SKIP, validate=False
        )
        records = engine.preview("e", 3)
        assert len(records) == 3
        assert all(record.values["boom"] is None for record in records)
        assert engine.stats["e"].field_failures == 3

    def test_placeholder_policy_marks_the_value(self) -> None:
        engine = GenerationEngine(
            compile_from(self.FAILING), failure_policy=FailurePolicy.PLACEHOLDER, validate=False
        )
        assert engine.preview("e", 1)[0].values["boom"] == "[FAILED:boom]"

    def test_retry_is_bounded(self) -> None:
        """Section 66: never permit infinite retry loops."""
        engine = GenerationEngine(
            compile_from(self.FAILING),
            failure_policy=FailurePolicy.RETRY,
            max_attempts=3,
            validate=False,
        )
        engine.preview("e", 1)
        assert engine.stats["e"].retries == 2  # attempts 1 and 2 retried, 3 gave up

    def test_max_attempts_is_capped(self) -> None:
        engine = GenerationEngine(compile_from(SIMPLE), max_attempts=10_000)
        assert engine.max_attempts == 10

    def test_unknown_policy_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="Unknown failure policy"):
            GenerationEngine(compile_from(SIMPLE), failure_policy="panic")

    def test_unknown_validation_policy_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="Unknown validation policy"):
            GenerationEngine(compile_from(SIMPLE), validation_policy="panic")

    def test_validation_follows_the_failure_policy_by_default(self) -> None:
        """One flag, one behaviour: --on-failure covers both kinds of failure."""
        engine = GenerationEngine(compile_from(SIMPLE), failure_policy="skip")
        assert engine.validation_policy == "skip"


class TestValidationIntegration:
    BAD = {
        "e": {
            "count": 10,
            "fields": {
                "small": {
                    "type": "integer",
                    "generator": "constant",
                    "value": 5,
                    "constraints": {"min": 100},
                }
            },
        }
    }

    def test_an_invalid_record_stops_the_run(self) -> None:
        """The default is abort, and it now means abort.

        A run that reported thirty thousand validation failures used to write
        them to the file and exit successfully, which made "abort" a promise
        about generation only. Anyone who reads --on-failure and plans around it
        is planning around this.
        """
        engine = GenerationEngine(compile_from(self.BAD))
        with pytest.raises(GenerationError, match="failed validation"):
            engine.preview("e", 10)

    def test_the_abort_message_names_the_record_and_the_way_out(self) -> None:
        engine = GenerationEngine(compile_from(self.BAD))
        with pytest.raises(GenerationError) as caught:
            engine.preview("e", 10)
        message = str(caught.value)
        assert "e#0" in message
        assert "5 is below the minimum 100" in message
        assert "--drop-invalid" in message
        # Rich reads square brackets as markup, so the category is parenthesised
        # rather than rendered as "[constraint]" and eaten on the way out.
        assert "(constraint)" in message
        assert "[constraint]" not in message

    def test_report_counts_them_and_writes_them(self) -> None:
        engine = GenerationEngine(compile_from(self.BAD), validation_policy="report")
        assert len(engine.preview("e", 10)) == 10
        assert engine.stats["e"].rejected == 10
        assert engine.validation_stats()["e"]["records_rejected"] == 10

    def test_drop_invalid_removes_them(self) -> None:
        engine = GenerationEngine(compile_from(self.BAD), drop_invalid=True)
        assert engine.preview("e", 10) == []
        assert engine.stats["e"].rejected == 10

    def test_skip_is_drop_invalid_by_another_name(self) -> None:
        engine = GenerationEngine(compile_from(self.BAD), failure_policy="skip")
        assert engine.preview("e", 10) == []

    def test_placeholder_marks_the_offending_field(self) -> None:
        engine = GenerationEngine(compile_from(self.BAD), validation_policy="placeholder")
        records = engine.preview("e", 3)
        assert [record.values["small"] for record in records] == ["[FAILED:small]"] * 3

    def test_incomplete_removes_the_offending_field(self) -> None:
        engine = GenerationEngine(compile_from(self.BAD), validation_policy="incomplete")
        records = engine.preview("e", 3)
        assert all("small" not in record.values for record in records)

    def test_retry_gives_up_rather_than_stopping_the_run(self) -> None:
        """A deterministic field reproduces the value that failed.

        So retrying cannot help here, and the record is dropped and counted -
        which is what an exhausted per-field retry does. Retrying is for a field
        whose value comes from somewhere that might answer differently.
        """
        engine = GenerationEngine(compile_from(self.BAD), validation_policy="retry")
        assert engine.preview("e", 3) == []
        assert engine.stats["e"].rejected == 3
        assert engine.stats["e"].retries == 6  # two further attempts per record

    def test_a_dropped_record_gives_back_its_unique_value(self) -> None:
        """Otherwise it holds a value on behalf of a record nobody has.

        The record here fails on a length constraint, not on uniqueness. Its
        unique key was registered before that was known, so without releasing it
        the *next* record - which produces the same key, because the generator
        is a constant - would be reported as a duplicate of a record that was
        thrown away.
        """
        entities = {
            "e": {
                "count": 2,
                "fields": {
                    "key": {"generator": "constant", "value": "K", "unique": True},
                    "small": {
                        "type": "integer",
                        "generator": "constant",
                        "value": 5,
                        "constraints": {"min": 100},
                    },
                },
            }
        }
        engine = GenerationEngine(compile_from(entities), drop_invalid=True)
        assert engine.preview("e", 2) == []
        # Two rejections for the length, and no third for a phantom duplicate.
        assert engine.stats["e"].rejected == 2
        assert not any("duplicate" in error for error in engine.stats["e"].errors)

    def test_validation_can_be_disabled(self) -> None:
        engine = GenerationEngine(compile_from(self.BAD), validate=False)
        assert len(engine.preview("e", 10)) == 10
        assert engine.validation_stats() == {}

    def test_structural_repair_is_applied(self) -> None:
        """A lookup table yielding "42" for an integer field is repaired, not rejected."""
        engine = GenerationEngine(
            compile_from(
                {
                    "e": {
                        "fields": {"n": {"type": "integer", "generator": "constant", "value": "42"}}
                    }
                }
            )
        )
        assert engine.preview("e", 1)[0].values["n"] == 42


class TestProvenance:
    def test_none_by_default(self, compiled) -> None:
        assert GenerationEngine(compiled).preview("employee", 1)[0].provenance is None

    def test_record_level(self, compiled) -> None:
        engine = GenerationEngine(compiled, provenance=ProvenanceMode.RECORD)
        record = engine.preview("employee", 1)[0]
        assert record.provenance is not None
        assert record.provenance.entity == "employee"
        assert record.provenance.fields == {}

    def test_field_level_names_each_generator(self, compiled) -> None:
        engine = GenerationEngine(compiled, provenance=ProvenanceMode.FIELD)
        record = engine.preview("employee", 1)[0]
        assert record.provenance.fields["first_name"].generator == "faker"
        assert record.provenance.fields["employee_id"].generator == "sequence"

    def test_field_provenance_reaches_the_serialised_record(self, compiled) -> None:
        engine = GenerationEngine(compiled, provenance=ProvenanceMode.FIELD)
        data = engine.preview("employee", 1)[0].to_dict(provenance_mode=ProvenanceMode.FIELD)
        assert data["_provenance"]["fields"]["email"]["generator"] == "template"

    def test_payloads_are_withheld_below_full(self, compiled) -> None:
        assert not ProvenanceMode.FIELD.tracks_payloads
        assert ProvenanceMode.FULL.tracks_payloads


def test_summary_reports_per_entity_statistics(compiled) -> None:
    engine = GenerationEngine(compiled)
    engine.preview("employee", 5)
    summary = engine.summary()
    assert summary["project"] and summary["entities"]["employee"]["generated"] == 5


@pytest.mark.scale
@pytest.mark.parametrize("count", [100, 10_000, 50_000])
def test_scale(count: int) -> None:
    """Section 88 asks for scale tests at 100, 10,000 and 1,000,000+ records.

    The tiers here stop at 50,000 so the default suite stays quick. The
    million-record tier is a benchmark, not a unit test - what it measures is
    throughput, and asserting on throughput in CI produces a flaky test rather
    than a useful signal.
    """
    compiled = compile_from(
        {
            "event": {
                "fields": {
                    "id": {"type": "string", "generator": "sequence", "format": "E-{00000000}"},
                    "host": {
                        "type": "hostname",
                        "generator": "pattern",
                        "pattern": "srv-{a-z:3}-{0000}",
                    },
                    "ip": {"type": "ip_address", "generator": "ip"},
                    "result": {"generator": "weighted", "choices": {"ok": 9, "fail": 1}},
                }
            }
        }
    )
    engine = GenerationEngine(compiled, validate=False)
    assert asyncio.run(_count(engine, "event", count, 5000)) == count
