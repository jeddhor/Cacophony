"""Afterwards: transforms, patch rules and regeneration.

Design document sections 104 and 105.

    operations   lowercase … hash … mask … add noise … compress
    patch rules  an edit recorded in the project, not made to a file
    transform    the same rules applied to a file that already exists
    regenerate   record 4,823,913, on its own, from nothing

**The claim that matters is reproducibility.** A Cacophony dataset is a pure
function of its schema and its seed; editing a row in an output file breaks that
silently. So a patch rule lives in the project and applies during generation, and
the test that earns the feature is this: transform a file with a rule, put the
same rule in the schema, regenerate, and the two must be byte-identical.

**Every operation is deterministic**, including ``add_noise`` — which is why it
derives its jitter by hashing rather than drawing. An operation that reached for
a random number would change the file every time it ran.

**Nothing is destroyed.** A transform writes beside its target and swaps at the
end, so a rule that raises halfway through leaves the original untouched and no
partial file behind.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from cacophony.core.errors import OutputError, SchemaError
from cacophony.generation.engine import GenerationEngine
from cacophony.schema.compiler import compile_project
from cacophony.transforms import (
    OPERATIONS,
    PatchRule,
    PatchSet,
    RecordExpression,
    TransformError,
    apply_operations,
    parse_step,
    transform_file,
)
from helpers import make_project

# --------------------------------------------------------------------------- #
# Section 105 - the operations
# --------------------------------------------------------------------------- #


def run(spec: str, value: Any) -> Any:
    return apply_operations(value, [parse_step(spec)])


class TestOperations:
    def test_section_105_lists_are_all_implemented(self) -> None:
        """The list in section 105, checked name by name."""
        wanted = {
            "lowercase",
            "uppercase",
            "truncate",
            "hash",
            "format_date",
            "encode",
            "mask",
            "normalize",
            "add_noise",
            "round",
            "compress",
        }
        assert wanted <= set(OPERATIONS)

    @pytest.mark.parametrize(
        ("spec", "value", "expected"),
        [
            ("lowercase", "MiXeD", "mixed"),
            ("uppercase", "MiXeD", "MIXED"),
            ("title", "amara okonkwo", "Amara Okonkwo"),
            ("strip", "  padded  ", "padded"),
            ("truncate:5", "abcdefghij", "abcde"),
            ("mask:4", "4111111111111111", "************1111"),
            ("mask:0", "secret", "******"),
            ("normalize", "double  space\ttab", "double space tab"),
            ("slug", "Hello, World!", "hello-world"),
            ("round:1", 3.14159, 3.1),
            ("round", 3.7, 4),
            ("encode:base64", "hello", "aGVsbG8="),
            ("encode:hex", "hi", "6869"),
            ("encode:url", "a b&c", "a%20b%26c"),
            ("format_date:%Y-%m", "2026-03-14T09:12:00", "2026-03"),
            ("nullify", "anything", None),
        ],
    )
    def test_each_operation(self, spec: str, value: Any, expected: Any) -> None:
        assert run(spec, value) == expected

    def test_aliases_are_accepted(self) -> None:
        assert run("lower", "ABC") == "abc"
        assert run("redact:2", "abcdef") == "****ef"
        assert run("noise:1", 100) == run("add_noise:1", 100)

    def test_hash_is_stable_and_the_right_width(self) -> None:
        first = run("hash:16", "alice@example.com")
        assert first == run("hash:16", "alice@example.com")
        assert len(first) == 16
        assert first != run("hash:16", "bob@example.com")

    def test_hash_keeps_a_join_working(self) -> None:
        """The reason to hash rather than nullify: the column still joins."""
        left = [run("hash", value) for value in ("a", "b", "a")]
        assert left[0] == left[2] != left[1]

    def test_a_decimal_stays_a_decimal(self) -> None:
        """Money is a decimal, and a transform must not quietly make it a float."""
        assert isinstance(run("round:2", Decimal("10.005")), Decimal)
        assert isinstance(run("add_noise:5", Decimal("100.00")), Decimal)

    def test_compress_round_trips(self) -> None:
        text = "the same sentence over and over " * 20
        squashed = run("compress", text)
        assert len(squashed) < len(text)
        assert run("decompress", squashed) == text

    def test_format_date_accepts_dates_and_datetimes(self) -> None:
        assert run("format_date:%Y", date(2026, 5, 1)) == "2026"
        assert run("format_date:%H:%M", datetime(2026, 5, 1, 14, 30)) == "14:30"

    def test_a_pipeline_threads_the_value(self) -> None:
        steps = [parse_step("lowercase"), parse_step("slug"), parse_step("truncate:9")]
        assert apply_operations("Amara Okonkwo Ltd", steps) == "amara-oko"

    def test_null_short_circuits_except_for_nullify(self) -> None:
        """Real data has nulls in it, deliberately (section 24)."""
        assert apply_operations(None, [parse_step("mask:4")]) is None
        assert apply_operations(None, [parse_step("nullify")]) is None

    def test_an_unknown_operation_is_refused(self) -> None:
        """A pipeline that skipped `masc:4` would report an unmasked column."""
        with pytest.raises(TransformError, match="unknown transformation 'masc:4'"):
            parse_step("masc:4")

    @pytest.mark.parametrize(
        ("spec", "value"),
        [("round:2", "Courtney"), ("format_date", "not a date"), ("add_noise:5", "text")],
    )
    def test_an_inapplicable_operation_raises(self, spec: str, value: Any) -> None:
        """Rather than passing the value through, which hides a broken pass."""
        with pytest.raises(TransformError):
            run(spec, value)

    def test_bad_arguments_are_refused(self) -> None:
        with pytest.raises(TransformError, match="between 4 and 128"):
            run("hash:2", "x")
        with pytest.raises(TransformError, match="between 1 and 9"):
            run("compress:99", "x")
        with pytest.raises(TransformError, match="above 0 and at most 100"):
            run("add_noise:0", 1)
        with pytest.raises(TransformError, match="unknown encoding"):
            run("encode:rot13", "x")


class TestNoiseIsDeterministic:
    """The property that keeps a transformed dataset reproducible."""

    def test_the_same_value_always_moves_the_same_way(self) -> None:
        assert run("add_noise:10", 1000.0) == run("add_noise:10", 1000.0)

    def test_different_values_move_differently(self) -> None:
        moved = {run("add_noise:10", value) for value in (100.0, 200.0, 300.0)}
        assert len(moved) == 3

    def test_it_stays_within_the_percentage_asked_for(self) -> None:
        for value in range(1, 400):
            jittered = run("add_noise:5", float(value))
            assert abs(jittered - value) <= value * 0.05 + 1e-9

    def test_an_integer_stays_an_integer(self) -> None:
        result = run("add_noise:5", 1000)
        assert isinstance(result, int)

    def test_re_running_a_transform_changes_nothing_further(self, tmp_path: Path) -> None:
        """A pipeline step must be idempotent under re-running, or a re-run
        after a failure silently doubles the jitter."""
        source = tmp_path / "in.jsonl"
        source.write_text('{"amount": 1000.0}\n', encoding="utf-8")

        rule = PatchRule.parse("noise", {"set": {"amount": "add_noise:5"}})
        first = tmp_path / "one.jsonl"
        transform_file(source, [rule], destination=first, write_sidecar=False)
        once = first.read_text(encoding="utf-8")

        second = tmp_path / "two.jsonl"
        transform_file(source, [rule], destination=second, write_sidecar=False)
        assert second.read_text(encoding="utf-8") == once


# --------------------------------------------------------------------------- #
# Expressions over a record
# --------------------------------------------------------------------------- #


class TestRecordExpression:
    def test_it_reads_fields(self) -> None:
        expression = RecordExpression("upper(name) + '!'")
        assert expression.evaluate({"name": "amara"}) == "AMARA!"

    def test_it_compares(self) -> None:
        expression = RecordExpression("age >= 40 and department == 'Finance'")
        assert expression.matches({"age": 45, "department": "Finance"})
        assert not expression.matches({"age": 30, "department": "Finance"})

    def test_a_missing_field_names_itself(self) -> None:
        expression = RecordExpression("nonexistent == 1")
        with pytest.raises(SchemaError, match="references 'nonexistent'"):
            expression.evaluate({"age": 1})

    def test_the_names_it_reads_are_known(self) -> None:
        assert RecordExpression("a + b").names == {"a", "b"}

    def test_a_syntax_error_is_reported_when_the_rule_is_read(self) -> None:
        with pytest.raises(SchemaError, match="could not parse"):
            RecordExpression("age >=")

    def test_an_empty_expression_is_refused(self) -> None:
        with pytest.raises(SchemaError, match="empty"):
            RecordExpression("   ")

    @pytest.mark.parametrize(
        "source",
        [
            "__import__('os').system('true')",
            "[x for x in (1, 2)]",
            "(lambda: 1)()",
            "open('/etc/passwd').read()",
        ],
    )
    def test_it_cannot_reach_outside_the_record(self, source: str) -> None:
        """A `where:` clause in a project somebody sent you is untrusted."""
        with pytest.raises(SchemaError):
            RecordExpression(source).evaluate({})

    def test_the_allow_lists_are_shared_with_the_generator(self) -> None:
        """One copy of a security boundary, not two."""
        from cacophony.generation.generators.text import ExpressionGenerator

        expression = RecordExpression("lower(name)")
        assert set(expression._functions) == set(ExpressionGenerator.FUNCTIONS)


# --------------------------------------------------------------------------- #
# Section 104 - patch rules
# --------------------------------------------------------------------------- #


def employee(**overrides: Any) -> dict[str, Any]:
    record = {
        "employee_id": "EMP-000001",
        "first_name": "Amara",
        "last_name": "Okonkwo",
        "email": "amara.okonkwo@example.com",
        "department": "Finance",
        "salary": Decimal("82000.00"),
        "age": 41,
    }
    record.update(overrides)
    return record


class TestPatchRuleParsing:
    def test_an_operation_pipeline(self) -> None:
        rule = PatchRule.parse("m", {"set": {"email": "mask:4"}})
        assert rule.edits[0].operations == [("mask", "4")]

    def test_several_operations_joined_by_a_pipe(self) -> None:
        rule = PatchRule.parse("m", {"set": {"name": "lowercase | slug"}})
        assert [name for name, _ in rule.edits[0].operations] == ["lowercase", "slug"]

    def test_a_bare_string_that_is_not_an_operation_is_an_expression(self) -> None:
        rule = PatchRule.parse("m", {"set": {"email": "lower(email)"}})
        assert rule.edits[0].expression is not None
        assert rule.edits[0].apply(employee()) == "amara.okonkwo@example.com"

    def test_an_explicit_expression(self) -> None:
        rule = PatchRule.parse("m", {"set": {"tag": {"expression": "upper(department)"}}})
        assert rule.edits[0].apply(employee()) == "FINANCE"

    def test_a_literal_value(self) -> None:
        rule = PatchRule.parse("m", {"set": {"email": {"value": "redacted"}}})
        assert rule.edits[0].apply(employee()) == "redacted"

    def test_a_bare_number_is_a_literal(self) -> None:
        rule = PatchRule.parse("m", {"set": {"age": 0}})
        assert rule.edits[0].apply(employee()) == 0

    def test_a_rule_that_does_nothing_is_refused(self) -> None:
        with pytest.raises(SchemaError, match="does nothing"):
            PatchRule.parse("empty", {"description": "nothing"})

    def test_a_rule_cannot_both_drop_and_keep(self) -> None:
        with pytest.raises(SchemaError, match="cannot both drop and keep"):
            PatchRule.parse("m", {"drop": True, "keep": True})

    def test_a_filter_with_a_set_block_is_refused(self) -> None:
        """Or the order in which the two happen is unguessable from the YAML."""
        with pytest.raises(SchemaError, match="nothing to set"):
            PatchRule.parse("m", {"drop": True, "set": {"a": "mask"}})

    def test_a_set_entry_needing_nothing_is_refused(self) -> None:
        with pytest.raises(SchemaError, match="needs 'operations'"):
            PatchRule.parse("m", {"set": {"a": {"nonsense": 1}}})

    def test_a_bad_where_clause_is_reported_with_its_location(self) -> None:
        with pytest.raises(SchemaError, match=r"patches\.m\.where"):
            PatchRule.parse("m", {"where": "age >=", "set": {"a": "mask"}})


class TestPatchSet:
    def test_it_edits_matching_records(self) -> None:
        rules = [
            PatchRule.parse(
                "mask", {"where": "department == 'Finance'", "set": {"email": "mask:8"}}
            )
        ]
        patches = PatchSet(rules)

        finance = patches.apply(employee())
        assert finance is not None
        assert finance["email"].startswith("*")

        other = patches.apply(employee(department="Sales"))
        assert other is not None
        assert other["email"] == "amara.okonkwo@example.com"

        assert patches.stats.records_edited == 1
        assert patches.stats.values_changed == 1

    def test_drop_filters_records_out(self) -> None:
        patches = PatchSet([PatchRule.parse("d", {"where": "age > 40", "drop": True})])
        assert patches.apply(employee(age=41)) is None
        assert patches.apply(employee(age=30)) is not None
        assert patches.stats.records_dropped == 1

    def test_keep_is_the_same_filter_the_other_way(self) -> None:
        patches = PatchSet([PatchRule.parse("k", {"where": "age > 40", "keep": True})])
        assert patches.apply(employee(age=41)) is not None
        assert patches.apply(employee(age=30)) is None

    def test_rules_apply_in_authored_order(self) -> None:
        """Masking then hashing is a different thing from hashing then masking."""
        forwards = PatchSet(
            [
                PatchRule.parse("a", {"set": {"email": "mask:4"}}),
                PatchRule.parse("b", {"set": {"email": "hash:8"}}),
            ]
        )
        backwards = PatchSet(
            [
                PatchRule.parse("b", {"set": {"email": "hash:8"}}),
                PatchRule.parse("a", {"set": {"email": "mask:4"}}),
            ]
        )
        first = forwards.apply(employee())
        second = backwards.apply(employee())
        assert first is not None and second is not None
        assert first["email"] != second["email"]

    def test_rules_are_filtered_by_entity(self) -> None:
        rules = [
            PatchRule.parse("a", {"entity": "employee", "set": {"email": "nullify"}}),
            PatchRule.parse("b", {"entity": "device", "set": {"email": "mask"}}),
        ]
        assert [rule.name for rule in PatchSet(rules, entity="employee").rules] == ["a"]

    def test_a_rule_with_no_entity_applies_everywhere(self) -> None:
        rules = [PatchRule.parse("a", {"set": {"email": "nullify"}})]
        assert PatchSet(rules, entity="anything").rules


# --------------------------------------------------------------------------- #
# Patches during generation
# --------------------------------------------------------------------------- #


PEOPLE: dict[str, Any] = {
    "person": {
        "count": 200,
        "primary_key": "person_id",
        "fields": {
            "person_id": {"type": "string", "generator": "sequence", "format": "P-{00000}"},
            "first_name": {"generator": "faker", "provider": "first_name"},
            "email": {
                "type": "string",
                "generator": "template",
                "template": "{first_name|lower}@example.com",
            },
            "department": {
                "type": "enum",
                "generator": "weighted",
                "choices": {"Finance": 30, "Sales": 40, "Engineering": 30},
            },
            "salary": {
                "type": "decimal",
                "generator": "random",
                "min": 30000,
                "max": 200000,
                "precision": 2,
            },
        },
    }
}


def generate(project_keys: dict[str, Any], count: int = 200) -> list[Any]:
    compiled = compile_project(make_project(PEOPLE, **project_keys))
    return asyncio.run(GenerationEngine(compiled).generate_batch("person", count))


class TestPatchesDuringGeneration:
    def test_a_rule_in_the_project_is_applied(self) -> None:
        records = generate(
            {
                "patches": {
                    "mask_finance": {
                        "entity": "person",
                        "where": "department == 'Finance'",
                        "set": {"email": "mask:8"},
                    }
                }
            }
        )
        finance = [r for r in records if r.values["department"] == "Finance"]
        others = [r for r in records if r.values["department"] != "Finance"]
        assert finance and others
        assert all(r.values["email"].startswith("*") for r in finance)
        assert all(not r.values["email"].startswith("*") for r in others)

    def test_a_dropping_rule_removes_records(self) -> None:
        records = generate(
            {"patches": {"no_sales": {"where": "department == 'Sales'", "drop": True}}}
        )
        assert records
        assert all(r.values["department"] != "Sales" for r in records)
        assert len(records) < 200

    def test_the_engine_reports_what_it_did(self) -> None:
        compiled = compile_project(
            make_project(
                PEOPLE,
                patches={"mask": {"where": "department == 'Finance'", "set": {"email": "mask:4"}}},
            )
        )
        engine = GenerationEngine(compiled)
        asyncio.run(engine.generate_batch("person", 200))
        report = engine.patch_reports()["person"]
        assert report["records_edited"] > 0
        assert report["by_rule"]["mask"] == report["records_edited"]

    def test_patches_can_be_switched_off(self) -> None:
        compiled = compile_project(
            make_project(PEOPLE, patches={"m": {"set": {"email": "nullify"}}})
        )
        records = asyncio.run(GenerationEngine(compiled, patches=False).generate_batch("person", 5))
        assert all(r.values["email"] is not None for r in records)

    def test_a_patched_dataset_is_still_reproducible(self) -> None:
        """The property the whole design protects."""
        keys = {"patches": {"m": {"set": {"salary": "add_noise:5"}}}}
        first = [r.values["salary"] for r in generate(keys, 50)]
        second = [r.values["salary"] for r in generate(keys, 50)]
        assert first == second

    def test_a_patch_is_applied_before_validation(self) -> None:
        """What reaches the file is what should be checked."""
        compiled = compile_project(
            make_project(
                {
                    "row": {
                        "count": 20,
                        "fields": {
                            "code": {
                                "type": "string",
                                "generator": "constant",
                                "value": "abcdefghij",
                                "constraints": {"max_length": 10},
                            }
                        },
                    }
                },
                # Truncating to 4 keeps it legal; a rule that made it illegal
                # would be caught, which is the point of the ordering.
                patches={"shorten": {"set": {"code": "truncate:4"}}},
            )
        )
        engine = GenerationEngine(compiled)
        records = asyncio.run(engine.generate_batch("row", 20))
        assert all(record.values["code"] == "abcd" for record in records)
        assert engine.stats["row"].rejected == 0


# --------------------------------------------------------------------------- #
# Section 105 - transforming a file
# --------------------------------------------------------------------------- #


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> Path:
    path.write_text(
        "".join(json.dumps(record, default=str) + "\n" for record in records), encoding="utf-8"
    )
    return path


class TestTransformFile:
    def test_it_rewrites_matching_records(self, tmp_path: Path) -> None:
        source = write_jsonl(
            tmp_path / "in.jsonl",
            [employee(), employee(department="Sales", email="b@example.com")],
        )
        rule = PatchRule.parse(
            "m", {"where": "department == 'Finance'", "set": {"email": "mask:4"}}
        )
        result = transform_file(source, [rule], destination=tmp_path / "out.jsonl")

        rows = [json.loads(line) for line in (tmp_path / "out.jsonl").read_text().splitlines()]
        assert result.read == 2
        assert result.written == 2
        assert result.edited == 1
        assert rows[0]["email"].startswith("*")
        assert rows[1]["email"] == "b@example.com"

    def test_filtering_writes_fewer_records(self, tmp_path: Path) -> None:
        source = write_jsonl(tmp_path / "in.jsonl", [employee(age=age) for age in (20, 30, 40, 50)])
        rule = PatchRule.parse("k", {"where": "age >= 40", "keep": True})
        result = transform_file(source, [rule], destination=tmp_path / "out.jsonl")
        assert result.written == 2
        assert result.dropped == 2

    def test_csv_round_trips(self, tmp_path: Path) -> None:
        source = tmp_path / "in.csv"
        source.write_text(
            "name,email\nAmara,a@example.com\nBrett,b@example.com\n", encoding="utf-8"
        )
        rule = PatchRule.parse("m", {"set": {"email": "mask:4"}})
        transform_file(source, [rule], destination=tmp_path / "out.csv")

        lines = (tmp_path / "out.csv").read_text(encoding="utf-8").splitlines()
        assert lines[0] == "name,email"
        assert lines[1].endswith(".com") and "*" in lines[1]

    def test_a_csv_cannot_gain_a_column(self, tmp_path: Path) -> None:
        """CSV has a fixed header; saying so beats dropping the value."""
        source = tmp_path / "in.csv"
        source.write_text("name\nAmara\n", encoding="utf-8")
        rule = PatchRule.parse("m", {"set": {"added": {"value": 1}}})
        with pytest.raises(OutputError, match="fixed header"):
            transform_file(source, [rule], destination=tmp_path / "out.csv")

    def test_a_json_array_round_trips(self, tmp_path: Path) -> None:
        source = tmp_path / "in.json"
        source.write_text(json.dumps([employee(), employee(age=20)], default=str), encoding="utf-8")
        rule = PatchRule.parse("k", {"where": "age > 30", "keep": True})
        transform_file(source, [rule], destination=tmp_path / "out.json")
        assert len(json.loads((tmp_path / "out.json").read_text())) == 1

    def test_parquet_is_refused_with_a_reason(self, tmp_path: Path) -> None:
        source = tmp_path / "in.parquet"
        source.write_bytes(b"PAR1")
        rule = PatchRule.parse("m", {"set": {"a": "mask"}})
        with pytest.raises(OutputError, match="Convert to jsonl"):
            transform_file(source, [rule], destination=tmp_path / "out.parquet", fmt="parquet")

    def test_an_existing_destination_is_not_replaced_silently(self, tmp_path: Path) -> None:
        source = write_jsonl(tmp_path / "in.jsonl", [employee()])
        target = tmp_path / "out.jsonl"
        target.write_text("keep me\n", encoding="utf-8")
        rule = PatchRule.parse("m", {"set": {"email": "nullify"}})

        with pytest.raises(OutputError, match="already exists"):
            transform_file(source, [rule], destination=target)
        assert target.read_text() == "keep me\n"

        transform_file(source, [rule], destination=target, overwrite=True)
        assert "keep me" not in target.read_text()

    def test_writing_over_the_source_needs_in_place(self, tmp_path: Path) -> None:
        source = write_jsonl(tmp_path / "in.jsonl", [employee()])
        rule = PatchRule.parse("m", {"set": {"email": "nullify"}})
        with pytest.raises(OutputError, match="Pass --in-place"):
            transform_file(source, [rule], destination=source, overwrite=True)

    def test_in_place_rewrites_the_file(self, tmp_path: Path) -> None:
        source = write_jsonl(tmp_path / "in.jsonl", [employee()])
        rule = PatchRule.parse("m", {"set": {"email": "mask:4"}})
        transform_file(source, [rule], in_place=True, write_sidecar=False)
        assert json.loads(source.read_text().splitlines()[0])["email"].startswith("*")

    def test_a_failure_leaves_the_original_and_no_partial(self, tmp_path: Path) -> None:
        """The reason it writes beside and swaps at the end."""
        source = write_jsonl(tmp_path / "in.jsonl", [employee(), employee(first_name="Bo")])
        before = source.read_text(encoding="utf-8")
        rule = PatchRule.parse("bad", {"set": {"first_name": "round:2"}})

        with pytest.raises(TransformError):
            transform_file(source, [rule], in_place=True)

        assert source.read_text(encoding="utf-8") == before
        assert not list(tmp_path.glob("*.partial"))

    def test_it_needs_somewhere_to_write(self, tmp_path: Path) -> None:
        source = write_jsonl(tmp_path / "in.jsonl", [employee()])
        rule = PatchRule.parse("m", {"set": {"email": "nullify"}})
        with pytest.raises(OutputError, match="somewhere to write"):
            transform_file(source, [rule])
        with pytest.raises(OutputError, match="not both"):
            transform_file(source, [rule], destination=tmp_path / "o.jsonl", in_place=True)

    def test_a_missing_file_says_so(self, tmp_path: Path) -> None:
        rule = PatchRule.parse("m", {"set": {"a": "mask"}})
        with pytest.raises(OutputError, match="no such file"):
            transform_file(tmp_path / "nope.jsonl", [rule], destination=tmp_path / "o.jsonl")

    def test_malformed_input_names_the_line(self, tmp_path: Path) -> None:
        source = tmp_path / "in.jsonl"
        source.write_text('{"a": 1}\nnot json\n', encoding="utf-8")
        rule = PatchRule.parse("m", {"set": {"a": "nullify"}})
        with pytest.raises(OutputError, match="line 2"):
            transform_file(source, [rule], destination=tmp_path / "out.jsonl")

    def test_it_records_what_it_did(self, tmp_path: Path) -> None:
        """A masked column looks exactly like one that was always masked."""
        source = write_jsonl(tmp_path / "in.jsonl", [employee()])
        rule = PatchRule.parse("m", {"set": {"email": "mask:4"}})
        result = transform_file(source, [rule], destination=tmp_path / "out.jsonl")

        assert result.sidecar is not None
        payload = json.loads(result.sidecar.read_text(encoding="utf-8"))
        assert payload["rules"] == ["m"]
        assert payload["records_edited"] == 1
        assert "transformed_at" in payload

    def test_it_streams(self, tmp_path: Path) -> None:
        """Bounded by one record, whatever the file size."""
        source = write_jsonl(
            tmp_path / "in.jsonl", [employee(person=index) for index in range(20_000)]
        )
        rule = PatchRule.parse("m", {"set": {"email": "hash:16"}})
        result = transform_file(
            source, [rule], destination=tmp_path / "out.jsonl", write_sidecar=False
        )
        assert result.written == 20_000


# --------------------------------------------------------------------------- #
# The claim
# --------------------------------------------------------------------------- #


class TestARuleSurvivesRegeneration:
    """Transform a file, put the rule in the schema, regenerate. Identical.

    This is what makes section 104's "patch rules" a real answer rather than a
    euphemism for editing the output. If the two disagreed, the rule would be a
    second implementation of the same intent, and one of them would be wrong.
    """

    def _rule(self) -> dict[str, Any]:
        return {"where": "department == 'Finance'", "set": {"email": "mask:8"}}

    def test_the_transformed_file_and_the_regenerated_dataset_agree(self, tmp_path: Path) -> None:
        # Generate without the rule, then transform the file.
        plain = generate({}, 300)
        source = write_jsonl(tmp_path / "plain.jsonl", [record.values for record in plain])
        transform_file(
            source,
            [PatchRule.parse("mask_finance", self._rule())],
            destination=tmp_path / "transformed.jsonl",
            write_sidecar=False,
        )
        transformed = [
            json.loads(line) for line in (tmp_path / "transformed.jsonl").read_text().splitlines()
        ]

        # Now put the same rule in the project and regenerate.
        patched = generate({"patches": {"mask_finance": self._rule()}}, 300)
        regenerated = [json.loads(json.dumps(r.values, default=str)) for r in patched]

        assert transformed == regenerated

    def test_regenerating_one_record_reproduces_it(self) -> None:
        """No run, no file, no state - just the index (section 75)."""
        keys = {"patches": {"mask_finance": self._rule()}}
        whole = generate(keys, 200)

        compiled = compile_project(make_project(PEOPLE, **keys))
        engine = GenerationEngine(compiled, counts={"person": 200})
        one = asyncio.run(engine.generate_batch("person", 1, offset=137))
        assert one[0].values == whole[137].values


# --------------------------------------------------------------------------- #
# The CLI
# --------------------------------------------------------------------------- #


class TestCli:
    def _dataset(self, tmp_path: Path) -> Path:
        return write_jsonl(
            tmp_path / "employee.jsonl",
            [employee(department="Finance"), employee(department="Sales", email="b@example.com")],
        )

    def test_transform_masks_and_reports(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from cacophony.cli.main import app

        source = self._dataset(tmp_path)
        result = CliRunner().invoke(
            app,
            [
                "transform",
                str(source),
                "--set",
                "email=mask:4",
                "--where",
                "department == 'Finance'",
                "-o",
                str(tmp_path / "out.jsonl"),
            ],
        )
        assert result.exit_code == 0, result.stdout
        assert "edited" in result.stdout
        # The warning that matters: a file changed outside its schema.
        assert "--record-as" in result.stdout

    def test_record_as_prints_a_paste_ready_rule(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from cacophony.cli.main import app

        source = self._dataset(tmp_path)
        result = CliRunner().invoke(
            app,
            [
                "transform",
                str(source),
                "--set",
                "email=mask:4",
                "--where",
                "department == 'Finance'",
                "-o",
                str(tmp_path / "out.jsonl"),
                "--record-as",
                "mask_finance",
            ],
        )
        assert result.exit_code == 0, result.stdout
        assert "patches:" in result.stdout
        assert "mask_finance" in result.stdout
        assert "mask:4" in result.stdout

    def test_a_bad_set_argument_explains_itself(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from cacophony.cli.main import app

        result = CliRunner().invoke(
            app,
            ["transform", str(self._dataset(tmp_path)), "--set", "nonsense", "-o", "x.jsonl"],
        )
        assert result.exit_code == 2
        assert "FIELD=OPERATION" in result.stderr

    def test_nothing_to_do_is_refused(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from cacophony.cli.main import app

        result = CliRunner().invoke(
            app, ["transform", str(self._dataset(tmp_path)), "-o", str(tmp_path / "o.jsonl")]
        )
        assert result.exit_code == 2
        assert "nothing to do" in result.stderr

    def test_regenerate_derives_records_without_a_run(self, tmp_path: Path) -> None:
        import yaml
        from typer.testing import CliRunner

        from cacophony.cli.main import app

        project = tmp_path / "project.yaml"
        project.write_text(
            yaml.safe_dump({"project": {"name": "R", "seed": 7}, "entities": PEOPLE}),
            encoding="utf-8",
        )
        result = CliRunner().invoke(
            app, ["regenerate", str(project), "-e", "person", "-r", "10-14", "--json"]
        )
        assert result.exit_code == 0, result.stdout
        payload = json.loads(result.stdout)
        assert [row["index"] for row in payload["records"]] == [10, 11, 12, 13, 14]

    def test_regenerate_refuses_a_whole_dataset(self, tmp_path: Path) -> None:
        import yaml
        from typer.testing import CliRunner

        from cacophony.cli.main import app

        project = tmp_path / "project.yaml"
        project.write_text(
            yaml.safe_dump({"project": {"name": "R"}, "entities": PEOPLE}), encoding="utf-8"
        )
        result = CliRunner().invoke(app, ["regenerate", str(project), "-r", "0-100000"])
        assert result.exit_code == 2
        assert "is a generate, not a regenerate" in result.stderr
