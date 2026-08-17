"""Assurance: duplicate detection, the model benchmark, edge cases.

Design document sections 59, 67 and 79 — three ways of asking whether what came
out is any good.

    duplication   how much of this is the same thing twice?
    benchmark     which model produces records this schema accepts?
    edge cases    what does an application do with valid but awkward data?

Two properties carry most of these tests.

**Duplicate detection is bounded and honest about it.** A Bloom filter has
false positives and no false negatives, so a report of zero is exact and a
report of some comes with the filter's own error rate. The near-duplicate window
holds a fixed number of signatures whatever the dataset size.

**An edge case is valid data.** Every value the edge-case catalogue produces
must satisfy the field that holds it, or the feature has silently become a
second copy of chaos and its findings stop meaning anything. That is asserted
here at a 50% injection rate, which is far higher than anyone would run.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from cacophony.core.errors import SchemaError
from cacophony.core.record import GeneratedRecord
from cacophony.generation.engine import GenerationEngine
from cacophony.schema.compiler import compile_project
from cacophony.schema.models import DuplicationSpec
from cacophony.simulation.edges import (
    CATEGORIES,
    EDGE_MARK,
    EdgeCaseInjector,
    cases_for,
)
from cacophony.validation.duplication import (
    METHODS,
    BloomFilter,
    DuplicateDetector,
    MinHashIndex,
    normalise,
    shingles,
)
from cacophony.validation.pipeline import RecordValidator
from helpers import compile_from, make_project

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

#: One biography, the way a repetitive model hands it back: the same paragraph
#: with the name swapped. Every string is unique; every biography is the same.
BIOGRAPHY = (
    "{name} is a seasoned engineering manager with over twelve years of experience "
    "building distributed systems. Before joining the company, {name} led the platform "
    "team at a mid-sized fintech, where they were responsible for the migration of the "
    "core ledger to a service-oriented architecture."
)

NAMES = (
    "Amara Okonkwo",
    "Devin Halvorsen",
    "Priya Raman",
    "Tomas Ek",
    "Lena Fischer",
    "Rafael Sosa",
    "Yuki Tanaka",
    "Nadia Haddad",
)

PROSE_ENTITIES: dict[str, Any] = {
    "profile": {
        "count": 40,
        "primary_key": "profile_id",
        "fields": {
            "profile_id": {"type": "string", "generator": "sequence", "format": "P-{0000}"},
            "name": {"generator": "faker", "provider": "name"},
            "biography": {"type": "text", "generator": "constant", "value": "placeholder"},
        },
    }
}


def prose_project():
    return compile_from(PROSE_ENTITIES)


def record(text: str, index: int = 0, entity: str = "profile") -> GeneratedRecord:
    from cacophony.core.provenance import RecordProvenance

    return GeneratedRecord(
        entity=entity,
        values={"profile_id": f"P-{index:04d}", "name": "x", "biography": text},
        provenance=RecordProvenance(entity=entity, record_index=index),
    )


def detector(**options: Any) -> DuplicateDetector:
    compiled = prose_project()
    spec = DuplicationSpec(enabled=True, fields=["biography"], **options)
    return DuplicateDetector(compiled.entity("profile"), spec, expected_records=1_000)


# --------------------------------------------------------------------------- #
# Section 59 - the primitives
# --------------------------------------------------------------------------- #


class TestNormalisation:
    def test_the_same_repetition_wearing_a_hat(self) -> None:
        assert normalise("Dr. Amara Okonkwo!!  ") == normalise("dr amara okonkwo")

    def test_unicode_is_folded_before_punctuation_is_stripped(self) -> None:
        """NFKC first, or fancy typography looks like a different sentence."""
        assert normalise("Ｈｅｌｌｏ， ｗｏｒｌｄ") == normalise("hello world")

    def test_shingles_are_phrases_not_spellings(self) -> None:
        grams = shingles("the quick brown fox jumps", 3)
        assert "the quick brown" in grams
        assert len(grams) == 3

    def test_a_short_text_is_still_comparable(self) -> None:
        assert shingles("two words", 5) == {"two words"}

    def test_empty_text_has_no_shingles(self) -> None:
        assert shingles("   ") == set()
        assert shingles("!!!") == set()


class TestBloomFilter:
    def test_it_has_no_false_negatives(self) -> None:
        """The property that makes a report of zero duplicates exact."""
        filter_ = BloomFilter(1_000, 0.01)
        values = [f"value-{index}" for index in range(500)]
        for value in values:
            filter_.add(value)
        assert all(value in filter_ for value in values)

    def test_the_false_positive_rate_is_reported_not_assumed(self) -> None:
        filter_ = BloomFilter(1_000, 0.01)
        for index in range(500):
            filter_.add(f"value-{index}")

        described = filter_.describe()
        seen = sum(1 for index in range(1_000, 20_000) if f"value-{index}" in filter_)
        measured = seen / 19_000
        # The reported figure is the prediction; the measured one should be
        # close to it. A structure that guesses is fine; one that lies is not.
        assert described["false_positive_rate"] == pytest.approx(measured, abs=0.02)

    def test_ten_million_items_fit_in_tens_of_megabytes(self) -> None:
        """The reason this is a filter rather than a set."""
        filter_ = BloomFilter(10_000_000, 0.001)
        assert filter_.size_bytes < 40_000_000

    def test_overfilling_degrades_and_says_so(self) -> None:
        filter_ = BloomFilter(100, 0.001)
        for index in range(10_000):
            filter_.add(f"v{index}")
        assert filter_.load > 1.0
        assert filter_.effective_error_rate > 0.5

    def test_add_reports_whether_it_had_seen_the_value(self) -> None:
        filter_ = BloomFilter(1_000)
        assert filter_.add("first") is False
        assert filter_.add("first") is True


class TestMinHash:
    def test_it_finds_the_repetition_a_model_actually_produces(self) -> None:
        """A name-swapped biography, which is the canonical failure."""
        index = MinHashIndex()
        first = index.signature(BIOGRAPHY.format(name="Amara Okonkwo"))
        second = index.signature(BIOGRAPHY.format(name="Devin Halvorsen"))
        assert first is not None and second is not None
        assert index.estimate(first, second) >= index.similarity

    def test_unrelated_text_scores_nothing(self) -> None:
        index = MinHashIndex()
        first = index.signature(BIOGRAPHY.format(name="Amara Okonkwo"))
        other = index.signature(
            "Priya joined last spring from a robotics startup and works on the mobile client."
        )
        assert first is not None and other is not None
        assert index.estimate(first, other) < 0.2

    def test_lsh_surfaces_every_repeat_and_no_stranger(self) -> None:
        """Recall is what the band layout is chosen for."""
        index = MinHashIndex()
        surfaced = 0
        for position, name in enumerate(NAMES):
            text = BIOGRAPHY.format(name=name)
            signature = index.signature(text)
            assert signature is not None
            if position and index.candidates(signature):
                surfaced += 1
            index.add(text, signature)
        assert surfaced == len(NAMES) - 1

        strangers = MinHashIndex()
        false = 0
        texts = [
            f"Employee {number} joined in {2015 + number} and works on {area}."
            for number, area in enumerate(["mobile", "billing", "search", "infra", "ml"])
        ]
        for position, text in enumerate(texts):
            signature = strangers.signature(text)
            assert signature is not None
            if position and strangers.candidates(signature):
                false += 1
            strangers.add(text, signature)
        assert false == 0

    def test_the_band_threshold_never_sits_above_the_target(self) -> None:
        """A threshold above the target is a silent false negative."""
        for similarity in (0.5, 0.6, 0.7, 0.8, 0.9):
            index = MinHashIndex(signature_size=64, similarity=similarity)
            threshold = (1.0 / index.bands) ** (1.0 / index.rows)
            assert threshold <= similarity + 1e-9
            assert index.bands * index.rows == 64

    def test_the_window_is_bounded(self) -> None:
        """A stream of a million records must not become a million signatures."""
        index = MinHashIndex(window=10, signature_size=32)
        for number in range(500):
            text = f"text number {number} with padding words so shingles exist here"
            signature = index.signature(text)
            assert signature is not None
            index.add(text, signature)
        assert len(index) == 10
        # Eviction removes the band keys too, or the index leaks.
        assert len(index._buckets) <= 10 * index.bands

    def test_signatures_are_derived_not_random(self) -> None:
        """Two indexes must compare two texts the same way."""
        text = BIOGRAPHY.format(name="Amara Okonkwo")
        assert MinHashIndex().signature(text) == MinHashIndex().signature(text)

    def test_no_signature_for_empty_text(self) -> None:
        assert MinHashIndex().signature("   ") is None


# --------------------------------------------------------------------------- #
# Section 59 - the detector
# --------------------------------------------------------------------------- #


class TestDuplicateDetector:
    def test_exact_repeats_are_counted_once_each(self) -> None:
        found = detector(methods=["exact"])
        for index in range(5):
            found.observe(record("the very same sentence every time", index))
        report = found.finish()
        assert report.values == 5
        assert report.exact == 4  # the first is not a repeat of anything

    def test_normalisation_catches_the_same_text_dressed_differently(self) -> None:
        found = detector(methods=["normalized"])
        found.observe(record("Dr. Amara Okonkwo", 0))
        found.observe(record("dr amara okonkwo!!", 1))
        report = found.finish()
        assert report.normalized == 1

    def test_near_duplicates_are_the_ones_that_matter(self) -> None:
        """Unique strings, identical content. Nothing else can see this."""
        found = detector(methods=["exact", "minhash"])
        for index, name in enumerate(NAMES):
            found.observe(record(BIOGRAPHY.format(name=name), index))
        report = found.finish()
        assert report.exact == 0
        assert report.near == len(NAMES) - 1
        assert report.uniqueness < 0.2

    def test_fuzzy_confirms_what_lsh_proposed(self) -> None:
        """And confirms it rather than rejecting it.

        `SequenceMatcher`'s autojunk heuristic treats any character appearing
        in more than 1% of a sequence over 200 characters as noise - the vowels,
        for prose. With it on, two biographies differing only in the name scored
        0.014 against a true 0.956, so the confirmation step threw away real
        duplicates. It is switched off.
        """
        found = detector(methods=["fuzzy"])
        for index, name in enumerate(NAMES):
            found.observe(record(BIOGRAPHY.format(name=name), index))
        assert found.finish().near == len(NAMES) - 1

    def test_genuinely_varied_text_is_left_alone(self) -> None:
        found = detector(methods=["exact", "normalized", "minhash"])
        texts = [
            "She runs the observability group and spends her time on incident review.",
            "He rewrote the offline sync layer for the mobile client last quarter.",
            "They manage vendor relationships and the annual procurement cycle.",
            "A former teacher who moved into technical writing eight years ago.",
            "Leads the data platform team and is unreasonably fond of Parquet.",
        ]
        for index, text in enumerate(texts):
            found.observe(record(text, index))
        report = found.finish()
        assert report.exact == 0
        assert report.near == 0
        assert report.uniqueness == 1.0

    def test_deliberate_duplicates_are_exempt(self) -> None:
        """Reporting these would be reporting the feature (section 24)."""
        from cacophony.simulation.chaos import DUPLICATE_MARK

        found = detector(methods=["exact"])
        first = record("identical text here", 0)
        found.observe(first)

        copy = record("identical text here", 0)
        copy.damage[DUPLICATE_MARK] = "duplicates"
        found.observe(copy)

        report = found.finish()
        assert report.exact == 0
        assert report.exempt == 1

    def test_thresholds_are_breached_in_sentences(self) -> None:
        found = detector(methods=["exact"], max_exact=0.1)
        for index in range(10):
            found.observe(record("same", index))
        report = found.finish()
        assert not report.ok
        assert "above the 10.00% allowed" in report.breaches[0]

    def test_a_dataset_within_its_thresholds_is_ok(self) -> None:
        found = detector(methods=["exact"], max_exact=0.5, max_near=0.5)
        for index in range(10):
            found.observe(record(f"sentence number {index} which is different", index))
        assert found.finish().ok

    def test_examples_are_kept_so_a_report_can_show_rather_than_assert(self) -> None:
        found = detector(methods=["exact"])
        for index in range(4):
            found.observe(record("a repeated paragraph of some length", index))
        report = found.finish()
        assert report.examples
        assert report.examples[0].kind == "exact"
        assert "repeated paragraph" in report.examples[0].excerpt

    def test_the_report_says_how_much_to_trust_it(self) -> None:
        found = detector(methods=["exact", "minhash"])
        found.observe(record("something", 0))
        payload = found.finish().to_dict()
        assert payload["bloom"]["false_positive_rate"] >= 0.0
        assert payload["index"]["window"] > 0

    def test_whole_records_can_be_compared(self) -> None:
        compiled = prose_project()
        spec = DuplicationSpec(enabled=True, fields=["*"], methods=["exact"])
        found = DuplicateDetector(compiled.entity("profile"), spec)
        one = record("text", 0)
        two = record("text", 0)
        found.observe(one)
        found.observe(two)
        assert found.finish().exact == 1

    def test_embeddings_are_refused_with_a_reason(self) -> None:
        """Named in section 59, and needing a provider nothing offers."""
        compiled = prose_project()
        spec = DuplicationSpec(enabled=True, fields=["biography"], methods=["embeddings"])
        with pytest.raises(SchemaError, match="embedding provider"):
            DuplicateDetector(compiled.entity("profile"), spec)

    def test_an_unknown_method_is_refused(self) -> None:
        compiled = prose_project()
        spec = DuplicationSpec(enabled=True, methods=["telepathy"])
        with pytest.raises(SchemaError, match="telepathy"):
            DuplicateDetector(compiled.entity("profile"), spec)

    def test_a_field_that_does_not_exist_is_refused(self) -> None:
        compiled = prose_project()
        spec = DuplicationSpec(enabled=True, fields=["nonexistent"])
        with pytest.raises(SchemaError, match="nonexistent"):
            DuplicateDetector(compiled.entity("profile"), spec)

    def test_fields_default_to_where_repetition_happens(self) -> None:
        """Not the ids, and not the weighted choices.

        Comparing every employee id against every other one finds nothing and
        costs a great deal; reporting that a weighted choice recurs would be
        reporting what a weighted choice is for.
        """
        compiled = compile_from(
            {
                "person": {
                    "count": 5,
                    "fields": {
                        "id": {"type": "integer", "generator": "sequence"},
                        "status": {
                            "type": "enum",
                            "generator": "weighted",
                            "choices": ["active", "left"],
                        },
                        "postcode": {
                            "type": "string",
                            "generator": "pattern",
                            "pattern": "{A-Z:2}",
                        },
                        "notes": {"type": "text", "generator": "constant", "value": "x"},
                    },
                }
            }
        )
        found = DuplicateDetector(compiled.entity("person"), DuplicationSpec(enabled=True))
        assert found.fields == ["notes"]

    def test_all_declared_methods_are_implementable(self) -> None:
        compiled = prose_project()
        for method in METHODS:
            DuplicateDetector(
                compiled.entity("profile"),
                DuplicationSpec(enabled=True, fields=["biography"], methods=[method]),
            )


class TestDuplicationSpec:
    def test_a_threshold_is_a_request_to_measure(self) -> None:
        assert DuplicationSpec(max_near=0.02).is_enabled()
        assert DuplicationSpec(max_exact=0.0).is_enabled()
        assert DuplicationSpec(fields=["summary"]).is_enabled()

    def test_off_by_default(self) -> None:
        """Nothing is measured that nobody asked for."""
        assert not DuplicationSpec().is_enabled()

    def test_enabled_wins_over_inference(self) -> None:
        assert not DuplicationSpec(enabled=False, max_near=0.01).is_enabled()


class TestDuplicationThroughTheEngine:
    def test_a_repetitive_project_is_reported(self) -> None:
        project = make_project(
            {
                "profile": {
                    "count": 60,
                    "fields": {
                        "id": {"type": "integer", "generator": "sequence"},
                        "biography": {
                            "type": "text",
                            "generator": "lookup",
                            "mode": "random",
                            "values": [
                                "A seasoned engineering manager who builds distributed systems.",
                                "An experienced designer who has shipped consumer applications.",
                                "A data engineer focused on streaming pipelines and warehouses.",
                            ],
                        },
                    },
                }
            },
            quality={"duplication": {"max_exact": 0.01}},
        )
        compiled = compile_project(project)
        engine = GenerationEngine(compiled)
        asyncio.run(_drain(engine, "profile", 60))

        reports = engine.duplication_reports()
        assert "profile" in reports
        report = reports["profile"]
        # Sixty records drawn from three biographies.
        assert report["exact"] >= 55
        assert not report["ok"]

    def test_nothing_is_measured_when_nothing_was_asked(self) -> None:
        compiled = prose_project()
        engine = GenerationEngine(compiled)
        asyncio.run(_drain(engine, "profile", 10))
        assert engine.duplication_reports() == {}


async def _drain(engine: GenerationEngine, entity: str, count: int) -> int:
    total = 0
    async for batch in engine.stream(entity, count=count, batch_size=10):
        total += len(batch)
    return total


# --------------------------------------------------------------------------- #
# Section 79 - edge cases
# --------------------------------------------------------------------------- #


EDGE_ENTITIES: dict[str, Any] = {
    "person": {
        "count": 200,
        "primary_key": "person_id",
        "fields": {
            "person_id": {"type": "string", "generator": "sequence", "format": "P-{00000}"},
            "first_name": {"generator": "faker", "provider": "first_name"},
            "surname": {"generator": "faker", "provider": "last_name"},
            "age": {"type": "integer", "generator": "random", "min": 18, "max": 90},
            "salary": {"type": "decimal", "generator": "random", "min": 20000, "max": 200000},
            "joined": {"type": "date", "generator": "datetime"},
            "note": {
                "type": "string",
                "generator": "constant",
                "value": "ok",
                "constraints": {"max_length": 60},
            },
            "tiny": {
                "type": "string",
                "generator": "constant",
                "value": "abc",
                "constraints": {"min_length": 3, "max_length": 4},
            },
        },
    }
}


class TestEdgeCaseCatalogue:
    def test_every_category_is_produced_by_something(self) -> None:
        """A category nothing can generate is a category that lies in the help."""
        compiled = compile_from(EDGE_ENTITIES)
        produced = {
            category
            for compiled_field in compiled.entity("person").fields
            for category, _value in cases_for(compiled_field.spec, compiled_field.generator)
        }
        # Coordinates need a geo_point, which this fixture has not got.
        assert produced >= set(CATEGORIES) - {"extreme_coordinates"}

        geo = compile_from(
            {"place": {"count": 1, "fields": {"at": {"type": "geo_point", "generator": "random"}}}}
        )
        assert any(
            category == "extreme_coordinates"
            for category, _value in cases_for(geo.entity("place").fields[0].spec)
        )

    def test_the_empty_string_only_where_it_is_legal(self) -> None:
        """A field with a minimum length given "" is chaos, not an edge case."""
        compiled = compile_from(EDGE_ENTITIES)
        note = next(f for f in compiled.entity("person").fields if f.name == "note")
        tiny = next(f for f in compiled.entity("person").fields if f.name == "tiny")
        assert ("boundary_length", "") in cases_for(note.spec)
        assert ("boundary_length", "") not in cases_for(tiny.spec)

    def test_numeric_edges_respect_the_generators_bounds(self) -> None:
        """`min: 18` is a generator option, not a constraint.

        A version of this that read only `constraints` saw an unbounded integer
        and proposed 2^63 as an age. Nothing rejected it - no constraint was
        violated - and the dataset held a person nine quadrillion years old.
        """
        compiled = compile_from(EDGE_ENTITIES)
        age = next(f for f in compiled.entity("person").fields if f.name == "age")
        assert age.spec.constraints.min is None  # the bound is on the generator
        values = [value for _category, value in cases_for(age.spec, age.generator)]
        assert 18 in values and 90 in values
        assert all(18 <= value <= 90 for value in values)

    def test_an_unbounded_integer_does_get_the_huge_ones(self) -> None:
        """Section 79 asks for huge integers, where they are legal."""
        compiled = compile_from(
            {"row": {"count": 1, "fields": {"n": {"type": "integer", "generator": "random"}}}}
        )
        field_ = compiled.entity("row").fields[0]
        # This generator defaults to a bounded range, so declare the field
        # without one to get the genuinely unbounded case.
        values = [value for _category, value in cases_for(field_.spec, None)]
        assert 2**63 - 1 in values
        assert -(2**31) in values

    def test_temporal_edges_include_a_leap_day_and_a_dst_boundary(self) -> None:
        compiled = compile_from(EDGE_ENTITIES)
        joined = next(f for f in compiled.entity("person").fields if f.name == "joined")
        values = {value.isoformat() for _category, value in cases_for(joined.spec)}
        assert "2024-02-29" in values
        # 2026-03-08 in US/Eastern is the spring-forward day.
        assert "2026-03-08" in values


class TestEdgeCaseInjector:
    def _injector(self, **options: Any) -> tuple[EdgeCaseInjector, RecordValidator]:
        compiled = compile_from(EDGE_ENTITIES)
        entity = compiled.entity("person")
        return EdgeCaseInjector(entity, seed=99, **options), RecordValidator(entity)

    def test_keys_and_references_are_never_touched(self) -> None:
        """An emoji primary key is a broken fixture, not a robustness test."""
        compiled = compile_from(
            {
                "team": {
                    "count": 2,
                    "fields": {"id": {"type": "integer", "generator": "sequence"}},
                },
                "member": {
                    "count": 10,
                    "primary_key": "member_id",
                    "fields": {
                        "member_id": {"type": "string", "generator": "sequence"},
                        "team": {"generator": "reference", "entity": "team"},
                        "nickname": {"generator": "faker", "provider": "first_name"},
                    },
                },
            }
        )
        injector = EdgeCaseInjector(compiled.entity("member"), fraction=1.0)
        assert "member_id" in injector.protected
        assert "team" in injector.protected
        assert set(injector.candidates) == {"nickname"}

    def test_a_fraction_of_records_is_marked(self) -> None:
        injector, validator = self._injector(fraction=0.3)
        marked = 0
        for index in range(600):
            row = GeneratedRecord(entity="person", values=_person(index))
            if _apply_plan(injector, row, index, validator):
                marked += 1
                assert row.damage[EDGE_MARK] in CATEGORIES
        assert 0.2 < marked / 600 < 0.4

    def test_zero_is_off(self) -> None:
        injector, validator = self._injector(fraction=0.0)
        assert injector.is_noop
        assert injector.plan(0) is None
        row = GeneratedRecord(entity="person", values=_person(0))
        assert _apply_plan(injector, row, 0, validator) is False

    def test_the_choice_is_derived_from_the_index(self) -> None:
        """A bug found once has to be findable again."""
        first, validator = self._injector(fraction=0.5)
        second, _ = self._injector(fraction=0.5)
        for index in range(200):
            left = GeneratedRecord(entity="person", values=_person(index))
            right = GeneratedRecord(entity="person", values=_person(index))
            assert first.plan(index) == second.plan(index)
            assert _apply_plan(first, left, index, validator) == _apply_plan(
                second, right, index, validator
            )
            assert left.values == right.values

    def test_a_candidate_the_field_cannot_hold_is_refused(self) -> None:
        """The property that keeps this from being a second copy of chaos."""
        compiled = compile_from(
            {
                "thing": {
                    "count": 10,
                    "fields": {
                        "code": {
                            "type": "string",
                            "generator": "constant",
                            "value": "abcd",
                            "constraints": {"min_length": 4, "max_length": 4},
                        }
                    },
                }
            }
        )
        entity = compiled.entity("thing")
        injector = EdgeCaseInjector(entity, fraction=1.0, seed=7)
        validator = RecordValidator(entity)

        for index in range(300):
            row = GeneratedRecord(entity="thing", values={"code": "abcd"})
            _apply_plan(injector, row, index, validator)
            # Whatever happened, the value still fits the field.
            assert len(str(row.values["code"])) == 4

        # And the refusals were counted rather than hidden.
        assert injector.stats.rejected > 0

    def test_categories_can_be_narrowed(self) -> None:
        injector, validator = self._injector(fraction=1.0, categories=["emoji"])
        for index in range(200):
            row = GeneratedRecord(entity="person", values=_person(index))
            _apply_plan(injector, row, index, validator)
        assert set(injector.stats.by_category) <= {"emoji"}

    def test_the_report_says_what_it_did(self) -> None:
        injector, validator = self._injector(fraction=0.5)
        for index in range(200):
            _apply_plan(
                injector, GeneratedRecord(entity="person", values=_person(index)), index, validator
            )
        described = injector.describe()
        assert described["entity"] == "person"
        assert described["records_marked"] > 0
        assert described["by_field"]


def _apply_plan(
    injector: EdgeCaseInjector, row: GeneratedRecord, index: int, validator: Any
) -> bool:
    """Drive the injector the way the engine does: field by field.

    The engine calls ``apply_to_field`` as each value is produced, so anything
    derived from an awkward value derives from the awkward value. This mirrors
    that without a whole engine.
    """
    injector.note_record()
    return any(
        injector.apply_to_field(row, index, name, validator) for name in sorted(injector.candidates)
    )


def _person(index: int) -> dict[str, Any]:
    from datetime import date
    from decimal import Decimal

    return {
        "person_id": f"P-{index:05d}",
        "first_name": "Alex",
        "surname": "Smith",
        "age": 40,
        "salary": Decimal("50000"),
        "joined": date(2024, 6, 1),
        "note": "ok",
        "tiny": "abc",
    }


class TestEdgeCasesThroughTheEngine:
    @pytest.mark.parametrize("fraction", [0.2, 0.5])
    def test_every_edge_case_still_passes_validation(self, fraction: float) -> None:
        """The claim the whole feature rests on.

        An edge case that fails validation has told you nothing about your
        application and everything about the catalogue's bugs.
        """
        compiled = compile_from(EDGE_ENTITIES)
        engine = GenerationEngine(compiled, edge_cases=fraction)
        asyncio.run(_drain(engine, "person", 400))

        stats = engine.stats["person"]
        assert stats.rejected == 0, stats.errors[:3]
        reports = engine.edge_case_reports()
        assert reports["person"]["records_marked"] > 0

    def test_off_by_default(self) -> None:
        compiled = compile_from(EDGE_ENTITIES)
        engine = GenerationEngine(compiled)
        asyncio.run(_drain(engine, "person", 50))
        assert engine.edge_case_reports() == {}

    def test_the_records_really_do_carry_awkward_values(self) -> None:
        compiled = compile_from(EDGE_ENTITIES)
        engine = GenerationEngine(compiled, edge_cases=1.0)
        rows = asyncio.run(engine.generate_batch("person", 60))

        marked = [row for row in rows if EDGE_MARK in row.damage]
        assert marked
        # At least one value somewhere is not plain ASCII, which is the point.
        assert any(
            any(ord(character) > 127 for character in str(value))
            for row in marked
            for key, value in row.values.items()
            if key != "person_id"
        )

    def test_a_primary_key_is_never_made_awkward(self) -> None:
        compiled = compile_from(EDGE_ENTITIES)
        engine = GenerationEngine(compiled, edge_cases=1.0)
        rows = asyncio.run(engine.generate_batch("person", 40))
        assert all(row.values["person_id"].startswith("P-") for row in rows)


class TestEdgeCasesAreNotChaos:
    """The distinction the feature would be worthless without."""

    def test_chaos_produces_invalid_data_and_edge_cases_do_not(self) -> None:
        compiled = compile_from(EDGE_ENTITIES)

        edges = GenerationEngine(compiled, edge_cases=1.0, chaos=False)
        asyncio.run(_drain(edges, "person", 200))
        assert edges.stats["person"].rejected == 0

        # The same project with chaos instead: the validator now has plenty to
        # say, and skips what was damaged on purpose rather than reporting it.
        chaotic_project = make_project(EDGE_ENTITIES, chaos={"preset": "messy"})
        chaotic = GenerationEngine(compile_project(chaotic_project))
        asyncio.run(_drain(chaotic, "person", 200))
        damage = sum(injector.stats.records_damaged for injector in chaotic._injectors.values())
        assert damage > 0


# --------------------------------------------------------------------------- #
# Section 67 - the model benchmark
# --------------------------------------------------------------------------- #


BENCH_ENTITIES: dict[str, Any] = {
    "plain": {
        "count": 5,
        "fields": {"id": {"type": "integer", "generator": "sequence"}},
    },
    "written": {
        "count": 5,
        "fields": {
            "id": {"type": "integer", "generator": "sequence"},
            "summary": {
                "type": "string",
                "semantic": "A one-line summary of an incident.",
                "generator": "llm",
                "constraints": {"max_length": 120},
            },
        },
    },
}


def bench_project():
    return compile_project(
        make_project(
            BENCH_ENTITIES,
            providers={
                "local": {
                    "type": "language_model",
                    "adapter": "mock",
                    "base_url": "mock://",
                    "model": "mock-a",
                }
            },
        )
    )


class TestBenchmarkSelection:
    def test_it_picks_the_entity_that_exercises_the_model_hardest(self) -> None:
        from cacophony.generation.benchmark import default_entity, model_backed_fields

        compiled = bench_project()
        assert default_entity(compiled) == "written"
        assert model_backed_fields(compiled, "written") == ["summary"]
        assert model_backed_fields(compiled, "plain") == []

    def test_a_project_with_no_model_fields_is_refused(self) -> None:
        from cacophony.generation.benchmark import default_entity

        compiled = compile_from({"plain": BENCH_ENTITIES["plain"]})
        with pytest.raises(SchemaError, match="no entity"):
            default_entity(compiled)

    def test_benchmarking_an_entity_with_no_model_fields_is_refused(self) -> None:
        from cacophony.generation.benchmark import benchmark_models

        compiled = bench_project()
        with pytest.raises(SchemaError, match="measure nothing"):
            asyncio.run(benchmark_models(compiled, ["mock-a"], entity="plain"))

    def test_a_project_with_no_language_provider_is_refused(self) -> None:
        from cacophony.generation.benchmark import benchmark_models

        compiled = compile_project(make_project(BENCH_ENTITIES))
        with pytest.raises(SchemaError, match="no language-model provider"):
            asyncio.run(benchmark_models(compiled, ["whatever"]))


class TestBenchmarkScoring:
    def test_it_scores_a_model_against_the_schema(self) -> None:
        from cacophony.generation.benchmark import benchmark_models

        compiled = bench_project()
        result = asyncio.run(benchmark_models(compiled, ["mock-a", "mock-b"], records=6))

        assert result.entity == "written"
        assert result.fields == ["summary"]
        assert len(result.scores) == 2
        for score in result.scores:
            assert score.records == 6
            assert score.values == 6
            assert 0.0 <= score.json_validity <= 1.0
            assert 0.0 <= score.usable <= 1.0

    def test_a_model_that_cannot_be_reached_is_a_result_not_a_crash(self) -> None:
        from cacophony.generation.benchmark import benchmark_models

        project = make_project(
            BENCH_ENTITIES,
            providers={
                "local": {
                    "type": "language_model",
                    "adapter": "ollama",
                    # Nothing listens here.
                    "base_url": "http://127.0.0.1:1",
                    "model": "absent",
                    "timeout_seconds": 1.0,
                }
            },
        )
        result = asyncio.run(benchmark_models(compile_project(project), ["absent"], records=2))
        assert result.scores[0].error
        assert not result.ok

    def test_ranking_puts_failures_last(self) -> None:
        from cacophony.generation.benchmark import BenchmarkResult, ModelScore

        good = ModelScore(model="good", provider="p", records=10, calls=10)
        better = ModelScore(model="better", provider="p", records=10, calls=10, repairs=0)
        worse = ModelScore(model="worse", provider="p", records=10, calls=10, repairs=5)
        broken = ModelScore(model="broken", provider="p", error="unreachable")

        result = BenchmarkResult(project="p", entity="e", records=10, seed=1)
        result.scores = [worse, broken, good, better]
        assert [score.model for score in result.ranked()][-1] == "broken"
        assert result.ranked()[-2].model == "worse"

    def test_repairs_count_against_validity(self) -> None:
        from cacophony.generation.benchmark import ModelScore

        clean = ModelScore(model="a", provider="p", records=20, calls=20)
        repaired = ModelScore(model="b", provider="p", records=20, calls=20, repairs=5)
        assert clean.json_validity == 1.0
        assert repaired.json_validity == 0.75

    def test_a_clipped_value_is_not_usable(self) -> None:
        """Found in real output: a constrained decoder cuts mid-word."""
        from cacophony.generation.benchmark import _is_clipped

        assert _is_clipped(
            "Server overload due to high volume; failed to handle queueing, led 3", 69
        )
        assert not _is_clipped("Short and finished.", 90)
        assert not _is_clipped("Ends on a full stop at the limit.", 33)

    def test_a_refusal_is_not_usable(self) -> None:
        from cacophony.generation.benchmark import _REFUSALS

        assert _REFUSALS.search("As an AI language model, I cannot invent an incident.")
        assert _REFUSALS.search("I'm sorry, I do not have access to that information.")
        assert not _REFUSALS.search("Checkout latency rose after the cache node was drained.")

    def test_the_table_has_a_row_per_model(self) -> None:
        from cacophony.generation.benchmark import BenchmarkResult, ModelScore, render_table

        result = BenchmarkResult(project="p", entity="e", records=5, seed=1)
        result.scores = [
            ModelScore(model="a", provider="p", records=5, values=5, calls=5),
            ModelScore(model="b", provider="p", error="down"),
        ]
        rows = render_table(result)
        assert rows[0][0] == "MODEL"
        assert len(rows) == 3
        assert rows[-1][1] == "failed"
        assert all(len(row) == len(rows[0]) for row in rows)


class TestBenchmarkFairness:
    def test_the_cache_is_forced_off(self) -> None:
        """Or the second model is scored on the first model's answers."""
        source = Path("backend/cacophony/generation/benchmark.py").read_text(encoding="utf-8")
        assert "CacheMode.DISABLED" in source

    def test_every_model_generates_the_same_indices(self) -> None:
        from cacophony.generation.benchmark import benchmark_models

        compiled = bench_project()
        result = asyncio.run(benchmark_models(compiled, ["mock-a", "mock-b"], records=5))
        # Same count, same entity, same seed - which is what makes the numbers
        # comparable at all.
        assert {score.records for score in result.scores} == {5}
        assert result.seed == compiled.seed
