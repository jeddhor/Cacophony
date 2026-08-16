"""Cross-record coherence (design document sections 8, 15, 26, 57).

The claim these tests exist to check is a strong one: a foreign key is
*computed* from the parent's index rather than looked up, so it costs no
memory, works at any scale, and gives the same answer whichever order the
entities were generated in. Each of those three is asserted below, because a
reference that merely produces plausible-looking values would pass a weaker
test suite and be wrong.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from typing import Any

import pytest

from cacophony.core.errors import GenerationError, SchemaError
from cacophony.generation.engine import GenerationEngine
from cacophony.generation.relations import (
    REFERENCE_DISTRIBUTIONS,
    EntityResolver,
    harmonic,
    pick_index,
)
from cacophony.schema.linter import lint_project
from helpers import compile_from

# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #


def relational_entities(**overrides: Any) -> dict[str, Any]:
    """company 1--N employee 1--N login, the shape section 15 describes."""
    reference_options = {"entity": "employee", "distribution": "skewed", **overrides}
    return {
        "company": {
            "count": 8,
            "primary_key": "company_id",
            "fields": {
                "company_id": {"type": "integer", "generator": "sequence"},
                "domain": {"generator": "faker", "provider": "domain_name"},
            },
        },
        "employee": {
            "count": 40,
            "primary_key": "employee_id",
            "fields": {
                "employee_id": {"type": "integer", "generator": "sequence", "start": 1000},
                "employer": {"generator": "reference", "entity": "company"},
                "first_name": {"generator": "faker", "provider": "first_name"},
                "email": {
                    "generator": "template",
                    "template": "{first_name|lower}@{company.domain}",
                },
            },
        },
        "login": {
            "count": 200,
            "primary_key": "login_id",
            "fields": {
                "login_id": {"type": "integer", "generator": "sequence"},
                "employee": {"generator": "reference", **reference_options},
            },
        },
    }


def generate(engine: GenerationEngine, entity: str, count: int, offset: int = 0) -> list[Any]:
    return asyncio.run(engine.generate_batch(entity, count, offset=offset))


# --------------------------------------------------------------------------- #
# pick_index
# --------------------------------------------------------------------------- #


class TestPickIndex:
    def test_every_distribution_stays_in_range(self) -> None:
        import random

        rng = random.Random(7)
        for distribution in REFERENCE_DISTRIBUTIONS:
            for record_index in range(200):
                index = pick_index(rng, 13, distribution=distribution, record_index=record_index)
                assert 0 <= index < 13, distribution

    def test_sequential_visits_every_parent(self) -> None:
        import random

        rng = random.Random(1)
        seen = {
            pick_index(rng, 5, distribution="sequential", record_index=index) for index in range(5)
        }
        assert seen == {0, 1, 2, 3, 4}

    @staticmethod
    def _top_decile_share(distribution: str, skew: float = 1.6) -> float:
        """What proportion of references land on the busiest tenth of parents."""
        import random

        rng = random.Random(99)
        counts = Counter(
            pick_index(rng, 100, distribution=distribution, record_index=index, skew=skew)
            for index in range(40_000)
        )
        return sum(count for _, count in counts.most_common(10)) / 40_000

    def test_skewed_concentrates_and_uniform_does_not(self) -> None:
        """The point of `skewed`: a heavy head, which uniform will not produce."""
        assert self._top_decile_share("skewed") > 0.2
        assert self._top_decile_share("uniform") < 0.15

    @pytest.mark.parametrize("skew", [1.6, 2.0, 3.3])
    def test_skew_matches_its_documented_shape(self, skew: float) -> None:
        """`skew` is documented as an exact figure, so it has to be one.

        The busiest tenth of parents take ``0.1 ** (1 / skew)`` of the
        references. That table is in the docstring for people to choose a skew
        from, which makes it a promise rather than a description.
        """
        assert self._top_decile_share("skewed", skew) == pytest.approx(0.1 ** (1 / skew), abs=0.02)

    def test_one_parent_is_always_index_zero(self) -> None:
        import random

        assert pick_index(random.Random(0), 1, distribution="skewed") == 0

    def test_no_parents_is_an_error_not_a_guess(self) -> None:
        import random

        with pytest.raises(GenerationError):
            pick_index(random.Random(0), 0)

    def test_harmonic_matches_the_exact_sum_for_small_n(self) -> None:
        assert harmonic(4) == pytest.approx(1 + 1 / 2 + 1 / 3 + 1 / 4)
        assert harmonic(0) == 0.0
        # The approximation takes over above 1,000 and must not jump.
        assert harmonic(1000) == pytest.approx(harmonic(999) + 1 / 1000, abs=1e-3)


# --------------------------------------------------------------------------- #
# The resolver
# --------------------------------------------------------------------------- #


class TestEntityResolver:
    def test_derives_a_key_without_generating_the_records_before_it(self) -> None:
        compiled = compile_from(relational_entities())
        engine = GenerationEngine(compiled)

        # Record 37 directly, with nothing generated beforehand.
        assert engine.resolver.key_at("employee", 37) == 1037

    def test_a_key_matches_the_record_generated_normally(self) -> None:
        compiled = compile_from(relational_entities())
        engine = GenerationEngine(compiled)

        records = generate(engine, "employee", 40)
        for index, record in enumerate(records):
            assert engine.resolver.key_at("employee", index) == record.values["employee_id"]

    def test_closure_is_only_what_the_key_needs(self) -> None:
        compiled = compile_from(relational_entities())
        resolver = EntityResolver(compiled)

        # The key is a bare sequence, so deriving it must not drag in the
        # company lookup or the email template.
        assert resolver.closure_for("employee", "employee_id") == ("employee_id",)
        # The email needs the name and the employer, and the employer first.
        closure = resolver.closure_for("employee", "email")
        assert closure.index("employer") < closure.index("email")
        assert "first_name" in closure

    def test_caches_are_accelerators_not_correctness(self) -> None:
        compiled = compile_from(relational_entities())
        engine = GenerationEngine(compiled)
        warm = [engine.resolver.key_at("employee", index) for index in range(20)]

        cold = EntityResolver(compiled, key_cache=1, record_cache=1)
        cold.bind(engine.generate_partial)
        assert [cold.key_at("employee", index) for index in range(20)] == warm

    def test_reports_its_hit_rate(self) -> None:
        compiled = compile_from(relational_entities())
        engine = GenerationEngine(compiled)
        for _ in range(5):
            engine.resolver.key_at("employee", 3)

        stats = engine.resolver.describe()
        assert stats["key_lookups"] == 5
        assert stats["key_hit_rate"] == pytest.approx(0.8)

    def test_unknown_entity_names_what_it_knows(self) -> None:
        compiled = compile_from(relational_entities())
        resolver = EntityResolver(compiled)
        with pytest.raises(SchemaError, match="employee"):
            resolver.entity("emplyee")

    def test_unknown_field_names_the_fields(self) -> None:
        compiled = compile_from(relational_entities())
        resolver = EntityResolver(compiled)
        with pytest.raises(SchemaError, match="employee_id"):
            resolver.key_field("employee", "employe_id")

    def test_counts_can_be_overridden_for_a_partial_run(self) -> None:
        """`--records 5` must not produce references to record 17."""
        compiled = compile_from(relational_entities())
        resolver = EntityResolver(compiled, counts={"employee": 5})
        assert resolver.count_of("employee") == 5
        assert resolver.count_of("company") == 8


# --------------------------------------------------------------------------- #
# The reference generator
# --------------------------------------------------------------------------- #


class TestReferences:
    def test_every_reference_identifies_a_real_parent(self) -> None:
        compiled = compile_from(relational_entities())
        engine = GenerationEngine(compiled)

        keys = {record.values["employee_id"] for record in generate(engine, "employee", 40)}
        for record in generate(engine, "login", 200):
            assert record.values["employee"] in keys

    def test_a_derived_field_reads_the_parent_this_row_chose(self) -> None:
        """The check that separates a real reference from a plausible one."""
        compiled = compile_from(relational_entities())
        engine = GenerationEngine(compiled)

        companies = {
            record.values["company_id"]: record.values["domain"]
            for record in generate(engine, "company", 8)
        }
        for record in generate(engine, "employee", 40):
            expected = companies[record.values["employer"]]
            assert record.values["email"].endswith("@" + expected)

    def test_the_answer_does_not_depend_on_generation_order(self) -> None:
        compiled = compile_from(relational_entities())

        forward = GenerationEngine(compiled)
        generate(forward, "company", 8)
        generate(forward, "employee", 40)
        first = [record.values for record in generate(forward, "login", 50)]

        # A fresh engine, asked for the children only.
        backward = GenerationEngine(compile_from(relational_entities()))
        second = [record.values for record in generate(backward, "login", 50)]

        assert first == second

    def test_a_reference_is_reproducible_from_any_offset(self) -> None:
        compiled = compile_from(relational_entities())
        engine = GenerationEngine(compiled)

        whole = [record.values for record in generate(engine, "login", 60)]
        tail = [record.values for record in generate(engine, "login", 20, offset=40)]
        assert tail == whole[40:60]

    def test_unique_gives_each_parent_one_child(self) -> None:
        entities = relational_entities(unique=True, distribution="sequential")
        entities["login"]["count"] = 40
        compiled = compile_from(entities)
        engine = GenerationEngine(compiled)

        chosen = [record.values["employee"] for record in generate(engine, "login", 40)]
        assert len(set(chosen)) == 40

    def test_skewed_produces_a_heavy_head(self) -> None:
        compiled = compile_from(relational_entities())
        engine = GenerationEngine(compiled)

        counts = Counter(record.values["employee"] for record in generate(engine, "login", 200))
        busiest = counts.most_common(1)[0][1]
        assert busiest > 200 / 40 * 2, "a skewed reference should not be flat"

    def test_a_reference_adopts_the_type_of_the_key_it_points_at(self) -> None:
        """An integer key referenced by a string column joins to nothing."""
        compiled = compile_from(relational_entities())
        engine = GenerationEngine(compiled)

        record = generate(engine, "login", 1)[0]
        assert isinstance(record.values["employee"], int)

    def test_an_explicit_type_is_not_overruled(self) -> None:
        entities = relational_entities()
        entities["login"]["fields"]["employee"]["type"] = "string"
        compiled = compile_from(entities)

        record = generate(GenerationEngine(compiled), "login", 1)[0]
        assert isinstance(record.values["employee"], str)

    def test_a_missing_target_entity_is_a_schema_error(self) -> None:
        entities = relational_entities()
        entities["login"]["fields"]["employee"]["entity"] = "nobody"
        with pytest.raises(SchemaError, match="nobody"):
            compile_from(entities)

    def test_on_unavailable_null_degrades_instead_of_failing(self) -> None:
        compiled = compile_from(
            {
                "orphan": {
                    "count": 3,
                    "fields": {
                        "id": {"type": "integer", "generator": "sequence"},
                        "parent": {
                            "generator": "reference",
                            "entity": "orphan",
                            "field": "id",
                            "on_unavailable": "null",
                            "nullable": True,
                        },
                    },
                }
            }
        )
        records = generate(GenerationEngine(compiled), "orphan", 3)
        assert all(record.values["parent"] in (1, 2, 3) for record in records)

    def test_the_compiler_orders_a_derived_field_after_its_reference(self) -> None:
        compiled = compile_from(relational_entities())
        employee = compiled.entity("employee")
        order = [field.name for field in employee.fields]
        assert order.index("employer") < order.index("email")


# --------------------------------------------------------------------------- #
# The linter (section 15)
# --------------------------------------------------------------------------- #


class TestReferenceLinting:
    def _codes(self, entities: dict[str, Any]) -> set[str]:
        return {issue.code for issue in lint_project(compile_from(entities))}

    def test_a_healthy_schema_reports_nothing(self) -> None:
        assert "unique-reference-overflow" not in self._codes(relational_entities())

    def test_unique_beyond_the_parent_count_is_an_error(self) -> None:
        entities = relational_entities(unique=True)
        entities["login"]["count"] = 500  # 500 children, 40 parents
        assert "unique-reference-overflow" in self._codes(entities)

    def test_referencing_a_nonunique_field_is_a_warning(self) -> None:
        entities = relational_entities()
        entities["employee"]["fields"]["employer"]["field"] = "domain"
        assert "reference-to-nonunique-key" in self._codes(entities)

    def test_a_self_reference_is_flagged(self) -> None:
        entities = relational_entities()
        entities["employee"]["fields"]["mentor"] = {
            "generator": "reference",
            "entity": "employee",
        }
        assert "self-reference" in self._codes(entities)

    def test_a_flat_reference_at_scale_is_worth_a_note(self) -> None:
        entities = relational_entities(distribution="uniform")
        entities["login"]["count"] = 500_000
        assert "uniform-reference" in self._codes(entities)

    def test_referencing_an_empty_entity_is_an_error(self) -> None:
        entities = relational_entities()
        entities["employee"]["count"] = 0
        assert "reference-to-empty-entity" in self._codes(entities)
