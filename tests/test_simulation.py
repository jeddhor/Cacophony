"""Synthetic worlds (design document sections 16, 17, 24, 25, 26, 78, 93).

Phase 7 asks for things that pull against Cacophony's central property - that
record *n* is a pure function of *n*. Events have an order, balances depend on
what came before, incidents span records. So most of what is checked here is
not "does it produce plausible values" but "does it still hold the guarantees":

* an entity's events come out in chronological order without being sorted
* a resumed run computes the same balances as an uninterrupted one
* the same subjects are compromised on every run, in any order, at any scale
* deliberate damage is reported as deliberate rather than as a defect

A generator that produced pretty timestamps but broke resume would pass a
weaker suite and be useless.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import random
from collections import Counter
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

from cacophony.core.errors import GenerationError, SchemaError
from cacophony.generation.engine import GenerationEngine
from cacophony.schema.compiler import compile_project
from cacophony.schema.loader import load_project
from cacophony.simulation.allocation import Allocation
from cacophony.simulation.chaos import CHAOS_PRESETS, DUPLICATE_MARK, ChaosInjector
from cacophony.simulation.scenarios import Phase, Scenario, ScenarioEngine, compile_scenarios
from cacophony.simulation.timeline import SHAPES, Timeline, TimelineShape, parse_moment
from cacophony.simulation.world import World, WorldStore
from helpers import TEMPLATES, make_project

SECURITY = TEMPLATES / "security-operations.yaml"


# --------------------------------------------------------------------------- #
# Timeline (section 25)
# --------------------------------------------------------------------------- #


def year(shape: str = "flat") -> Timeline:
    return Timeline(dt.datetime(2026, 1, 1), dt.datetime(2026, 12, 31), shape)


class TestTimeline:
    def test_quantiles_span_the_period(self) -> None:
        line = year()
        assert line.at(0.0) == line.start
        assert abs((line.at(1.0) - line.end).total_seconds()) < 3600

    def test_at_is_monotonic(self) -> None:
        """The property that makes ordered events free."""
        line = year("business_hours")
        moments = [line.at(index / 200) for index in range(201)]
        assert moments == sorted(moments)

    def test_business_hours_empties_the_weekend(self) -> None:
        line = year("business_hours")
        rng = random.Random(1)
        days = Counter(line.sample(rng).strftime("%a") for _ in range(8000))
        weekend = (days["Sat"] + days["Sun"]) / 8000
        assert weekend < 0.10, "a uniform draw would put 28.6% at the weekend"

    def test_business_hours_concentrates_on_the_working_day(self) -> None:
        line = year("business_hours")
        rng = random.Random(2)
        hours = Counter(line.sample(rng).hour for _ in range(8000))
        working = sum(count for hour, count in hours.items() if 8 <= hour <= 17)
        assert working / 8000 > 0.75

    def test_retail_is_evening_heavy(self) -> None:
        line = year("retail")
        rng = random.Random(3)
        hours = [line.sample(rng).hour for _ in range(6000)]
        evening = sum(1 for hour in hours if 17 <= hour <= 22)
        morning = sum(1 for hour in hours if 5 <= hour <= 10)
        assert evening > morning

    def test_holidays_are_silent(self) -> None:
        closed = dt.date(2026, 7, 4)
        shape = TimelineShape(holidays=frozenset({closed}))
        line = Timeline(dt.datetime(2026, 7, 1), dt.datetime(2026, 7, 8), shape)
        rng = random.Random(4)
        assert not any(line.sample(rng).date() == closed for _ in range(2000))

    def test_a_holiday_can_keep_some_activity(self) -> None:
        quiet = dt.date(2026, 7, 4)
        shape = TimelineShape(holidays=frozenset({quiet}), holiday_weight=0.5)
        line = Timeline(dt.datetime(2026, 7, 1), dt.datetime(2026, 7, 8), shape)
        rng = random.Random(5)
        hits = sum(1 for _ in range(2000) if line.sample(rng).date() == quiet)
        assert 0 < hits < 2000 / 7

    def test_a_spike_concentrates_activity(self) -> None:
        shape = TimelineShape(spikes=((dt.date(2026, 6, 1), dt.date(2026, 6, 7), 20.0),))
        line = year("flat")
        line = Timeline(line.start, line.end, shape)
        rng = random.Random(6)
        during = sum(
            1
            for _ in range(4000)
            if dt.date(2026, 6, 1) <= line.sample(rng).date() <= dt.date(2026, 6, 7)
        )
        assert during / 4000 > 0.15, "one week in fifty-two, at twenty times the rate"

    def test_seasonality(self) -> None:
        shape = TimelineShape(months=tuple(10.0 if m == 11 else 1.0 for m in range(12)))
        line = Timeline(dt.datetime(2026, 1, 1), dt.datetime(2026, 12, 31), shape)
        rng = random.Random(7)
        december = sum(1 for _ in range(4000) if line.sample(rng).month == 12)
        assert december / 4000 > 0.35

    def test_ordered_events_come_out_in_order(self) -> None:
        line = year("business_hours")
        moments = [line.ordered(index, 60, jitter=0.9) for index in range(60)]
        assert moments == sorted(moments)

    def test_a_long_period_falls_back_to_daily_buckets(self) -> None:
        line = Timeline(dt.datetime(2000, 1, 1), dt.datetime(2026, 1, 1), "flat")
        assert line.bucket_seconds == 86_400
        assert line.describe()["resolution"] == "daily"

    def test_a_backwards_period_is_refused(self) -> None:
        with pytest.raises(SchemaError, match="ends before it starts"):
            Timeline(dt.datetime(2026, 2, 1), dt.datetime(2026, 1, 1))

    def test_an_unknown_shape_lists_the_known_ones(self) -> None:
        with pytest.raises(SchemaError, match="business_hours"):
            Timeline(dt.datetime(2026, 1, 1), dt.datetime(2026, 2, 1), "sideways")

    @pytest.mark.parametrize("name", sorted(SHAPES))
    def test_every_named_shape_produces_moments_inside_the_period(self, name: str) -> None:
        line = year(name)
        rng = random.Random(8)
        assert all(line.start <= line.sample(rng) <= line.end for _ in range(200))

    def test_parse_moment_accepts_what_a_schema_writes(self) -> None:
        assert parse_moment("2026-03-04").date() == dt.date(2026, 3, 4)
        assert parse_moment("2026-03-04T10:30:00Z").hour == 10
        with pytest.raises(SchemaError, match="ISO-8601"):
            parse_moment("the fourth of March")


# --------------------------------------------------------------------------- #
# Allocation (sections 15, 25)
# --------------------------------------------------------------------------- #


class TestAllocation:
    def test_every_event_is_placed_exactly_once(self) -> None:
        allocation = Allocation(10_000, 137, distribution="skewed", seed=3)
        assert sum(allocation.counts()) == 10_000

    def test_index_maps_into_its_own_block(self) -> None:
        allocation = Allocation(5_000, 61, distribution="skewed", seed=3)
        for index in range(0, 5_000, 37):
            placement = allocation.locate(index)
            assert allocation.start_of(placement.subject) + placement.ordinal == index
            assert placement.total == allocation.count_for(placement.subject)

    def test_a_subject_s_events_are_contiguous(self) -> None:
        """What makes a stateful fold linear rather than quadratic."""
        allocation = Allocation(2_000, 20, seed=1)
        subjects = [allocation.locate(index).subject for index in range(2_000)]
        assert subjects == sorted(subjects)

    def test_ordinals_run_from_zero(self) -> None:
        allocation = Allocation(1_000, 10, seed=1)
        first = allocation.locate(allocation.start_of(4))
        assert first.ordinal == 0 and first.is_first

    @pytest.mark.parametrize("skew", [1.6, 2.0, 3.3])
    def test_skew_means_what_it_means_for_a_reference(self, skew: float) -> None:
        """`skew: 1.9` must not mean two different things in one schema."""
        allocation = Allocation(200_000, 1_000, distribution="skewed", skew=skew, seed=7)
        share = allocation.describe()["top_decile_share"]
        assert share == pytest.approx(0.1 ** (1 / skew), abs=0.05)

    def test_uniform_is_uniform(self) -> None:
        counts = set(Allocation(1_000, 10, seed=1).counts())
        assert counts == {100}

    def test_zipf_has_a_long_tail(self) -> None:
        counts = sorted(Allocation(10_000, 50, distribution="zipf", seed=1).counts(), reverse=True)
        assert counts[0] > counts[-1] * 20

    def test_a_minimum_gives_everyone_something(self) -> None:
        allocation = Allocation(1_000, 100, distribution="skewed", skew=3.0, seed=2, minimum=3)
        assert min(allocation.counts()) >= 3

    def test_the_busy_subjects_are_scattered_not_the_first_few(self) -> None:
        counts = Allocation(50_000, 40, distribution="skewed", skew=2.5, seed=3).counts()
        busiest = counts.index(max(counts))
        assert busiest > 2, "employee 1 should not be inherently the busiest person"

    def test_it_is_deterministic(self) -> None:
        first = Allocation(9_999, 71, distribution="skewed", seed=11).counts()
        second = Allocation(9_999, 71, distribution="skewed", seed=11).counts()
        assert first == second

    def test_no_subjects_is_refused(self) -> None:
        with pytest.raises(SchemaError, match="no subjects"):
            Allocation(100, 0)

    def test_an_index_outside_the_allocation_is_refused(self) -> None:
        with pytest.raises(SchemaError, match="outside"):
            Allocation(10, 2).locate(10)


# --------------------------------------------------------------------------- #
# Scenarios (section 17)
# --------------------------------------------------------------------------- #


class TestScenarioSelection:
    @pytest.mark.parametrize("fraction", [0.001, 0.01, 0.1])
    def test_selection_is_calibrated(self, fraction: float) -> None:
        scenario = Scenario(name="s", affects_fraction=fraction)
        engine = ScenarioEngine([scenario], seed=42)
        hits = sum(1 for subject in range(50_000) if engine.selects(scenario, subject))
        assert hits / 50_000 == pytest.approx(fraction, rel=0.2)

    def test_the_same_subjects_every_time(self) -> None:
        scenario = Scenario(name="ransomware", affects_fraction=0.01)
        first = ScenarioEngine([scenario], seed=7).subjects_for(scenario, 5_000)
        second = ScenarioEngine([scenario], seed=7).subjects_for(scenario, 5_000)
        assert first == second and first

    def test_selection_does_not_depend_on_population_size(self) -> None:
        """Whether employee 300 is compromised cannot depend on how many
        employees were generated, or two datasets of the same world disagree."""
        scenario = Scenario(name="s", affects_fraction=0.05)
        engine = ScenarioEngine([scenario], seed=3)
        small = set(engine.subjects_for(scenario, 500))
        large = {s for s in engine.subjects_for(scenario, 5_000) if s < 500}
        assert small == large

    def test_different_scenarios_select_different_subjects(self) -> None:
        a = Scenario(name="phishing", affects_fraction=0.02)
        b = Scenario(name="ransomware", affects_fraction=0.02)
        engine = ScenarioEngine([a, b], seed=1)
        chosen_a = set(engine.subjects_for(a, 20_000))
        chosen_b = set(engine.subjects_for(b, 20_000))
        assert len(chosen_a & chosen_b) < len(chosen_a) * 0.2

    def test_a_zero_fraction_selects_nobody(self) -> None:
        scenario = Scenario(name="s", affects_fraction=0.0)
        assert not ScenarioEngine([scenario], seed=1).subjects_for(scenario, 1_000)

    def test_a_full_fraction_selects_everybody(self) -> None:
        scenario = Scenario(name="s", affects_fraction=1.0)
        assert len(ScenarioEngine([scenario], seed=1).subjects_for(scenario, 100)) == 100


class TestScenarioApplication:
    def _engine(self, **kwargs: Any) -> tuple[ScenarioEngine, Scenario]:
        scenario = Scenario(
            name="incident",
            applies_to=("login",),
            affects_fraction=1.0,
            window=(0.4, 0.6),
            effects={"result": "failure"},
            phases=(
                Phase("access", 0.0, 0.5, {"stage": "in"}),
                Phase("exfil", 0.5, 1.0, {"stage": "out"}),
            ),
            **kwargs,
        )
        return ScenarioEngine([scenario], seed=1), scenario

    def test_records_outside_the_window_are_untouched(self) -> None:
        engine, _ = self._engine()
        assert engine.involvement("login", 1, position=0.1) is None
        assert engine.involvement("login", 1, position=0.9) is None

    def test_records_inside_the_window_are_caught(self) -> None:
        engine, _ = self._engine()
        found = engine.involvement("login", 1, position=0.5)
        assert found is not None and found[1].scenario == "incident"

    def test_phases_divide_the_window_in_order(self) -> None:
        engine, _ = self._engine()
        early = engine.involvement("login", 1, position=0.45)
        late = engine.involvement("login", 1, position=0.58)
        assert early[1].phase == "access"
        assert late[1].phase == "exfil"

    def test_a_phase_effect_beats_the_scenario_effect(self) -> None:
        engine, scenario = self._engine()
        _, involvement = engine.involvement("login", 1, position=0.45)
        effects = engine.effects_for(scenario, involvement)
        assert effects["result"] == "failure"
        assert effects["stage"] == "in"

    def test_an_entity_the_scenario_does_not_apply_to_is_untouched(self) -> None:
        engine, _ = self._engine()
        assert engine.involvement("payment", 1, position=0.5) is None

    def test_a_disabled_scenario_does_nothing(self) -> None:
        scenario = Scenario(name="s", affects_fraction=1.0, enabled=False)
        assert ScenarioEngine([scenario], seed=1).is_noop


class TestScenarioCompilation:
    def _spec(self, **parameters: Any) -> Any:
        from cacophony.schema.models import ScenarioSpec

        return ScenarioSpec(
            name="incident", applies_to=["login"], affects_fraction=0.1, parameters=parameters
        )

    def test_a_window_as_at_and_duration(self) -> None:
        [scenario] = compile_scenarios(
            [self._spec(subject="user", window={"at": 0.4, "duration": 0.2})],
            entities=["login", "user"],
        )
        assert scenario.window == (0.4, pytest.approx(0.6))

    def test_a_window_as_a_pair(self) -> None:
        [scenario] = compile_scenarios([self._spec(window=[0.1, 0.3])], entities=["login"])
        assert scenario.window == (0.1, 0.3)

    def test_phases_share_the_window_when_they_do_not_say(self) -> None:
        [scenario] = compile_scenarios(
            [self._spec(phases=[{"name": "a"}, {"name": "b"}, {"name": "c"}])],
            entities=["login"],
        )
        assert [(p.start, p.end) for p in scenario.phases] == [
            (0.0, pytest.approx(1 / 3)),
            (pytest.approx(1 / 3), pytest.approx(2 / 3)),
            (pytest.approx(2 / 3), 1.0),
        ]

    def test_an_unknown_entity_is_refused(self) -> None:
        with pytest.raises(SchemaError, match="not an entity"):
            compile_scenarios([self._spec()], entities=["payment"])

    def test_an_unknown_subject_is_refused(self) -> None:
        with pytest.raises(SchemaError, match="subject"):
            compile_scenarios([self._spec(subject="ghost")], entities=["login"])


# --------------------------------------------------------------------------- #
# Chaos (sections 24, 78)
# --------------------------------------------------------------------------- #


def damaged_records(preset: str, count: int = 400) -> tuple[list[Any], ChaosInjector]:
    from cacophony.core.record import GeneratedRecord
    from cacophony.schema.models import ChaosSpec

    injector = ChaosInjector(
        ChaosSpec(preset=preset),
        seed=99,
        entity="thing",
        fields=["name", "amount", "when", "parent"],
        protected=["thing_id"],
    )
    out: list[Any] = []
    for index in range(count):
        record = GeneratedRecord(
            entity="thing",
            values={
                "thing_id": index,
                "name": f"Widget {index}",
                "amount": 100 + index,
                "when": dt.datetime(2026, 3, 1, 9, 0),
                "parent": f"P-{index:04d}",
            },
        )
        duplicate = injector.apply(record, index)
        out.append(record)
        if duplicate is not None:
            out.append(duplicate)
    return out, injector


class TestChaos:
    def test_pristine_does_nothing(self) -> None:
        records, injector = damaged_records("pristine")
        assert injector.is_noop
        assert not any(record.damage for record in records)

    def test_realistic_damages_a_small_fraction(self) -> None:
        _records, injector = damaged_records("realistic", 2000)
        assert 0.01 < injector.stats.damage_rate < 0.12

    def test_hostile_damages_much_more(self) -> None:
        _quiet, gentle = damaged_records("realistic", 1000)
        _loud, harsh = damaged_records("hostile_qa", 1000)
        assert harsh.stats.damage_rate > gentle.stats.damage_rate * 3

    def test_every_defect_is_recorded(self) -> None:
        records, _injector = damaged_records("messy", 800)
        for record in records:
            for name, kind in record.damage.items():
                assert kind in CHAOS_PRESETS["messy"] or name == DUPLICATE_MARK

    def test_the_primary_key_is_never_damaged(self) -> None:
        records, _injector = damaged_records("absolute", 500)
        assert not any("thing_id" in record.damage for record in records)
        assert all(record.values["thing_id"] is not None for record in records)

    def test_duplicates_are_emitted_and_marked(self) -> None:
        records, injector = damaged_records("messy", 1000)
        duplicates = [r for r in records if DUPLICATE_MARK in r.damage]
        assert duplicates and len(duplicates) == injector.stats.duplicates_emitted

    def test_it_is_deterministic(self) -> None:
        first, _ = damaged_records("messy", 300)
        second, _ = damaged_records("messy", 300)
        assert [r.values for r in first] == [r.values for r in second]

    def test_damage_does_not_appear_as_a_column(self) -> None:
        """`_chaos` is metadata; a schema's own fields are what a reader loads."""
        records, _ = damaged_records("messy", 200)
        damaged = next(r for r in records if r.damage)
        assert "_chaos" not in damaged.values
        assert "_chaos" in damaged.to_dict()

    def test_explicit_rates_override_the_preset(self) -> None:
        from cacophony.schema.models import ChaosSpec

        injector = ChaosInjector(
            ChaosSpec(preset="pristine", missing_data=1.0), seed=1, fields=["a"]
        )
        assert not injector.is_noop
        assert injector.rates["missing_data"] == 1.0


class TestChaosAndValidation:
    """The interaction that is easy to get wrong (section 24)."""

    def _project(self, preset: str) -> Any:
        return make_project(
            {
                "thing": {
                    "count": 400,
                    "primary_key": "thing_id",
                    "fields": {
                        "thing_id": {"type": "integer", "generator": "sequence"},
                        "label": {"generator": "faker", "provider": "word"},
                        "score": {
                            "type": "integer",
                            "generator": "random",
                            "min": 1,
                            "max": 10,
                            "constraints": {"min": 1, "max": 10},
                        },
                    },
                }
            },
            chaos={"preset": preset},
        )

    def test_deliberate_damage_is_not_a_validation_failure(self) -> None:
        engine = GenerationEngine(compile_project(self._project("hostile_qa")))
        records = asyncio.run(engine.generate_batch("thing", 400))

        assert any(record.damage for record in records), "the test needs damage to exist"
        stats = engine.validation_stats()["thing"]
        assert stats["records_rejected"] == 0

    def test_the_undamaged_part_of_a_record_is_still_checked(self) -> None:
        from cacophony.core.record import GeneratedRecord
        from cacophony.validation.pipeline import RecordValidator

        compiled = compile_project(self._project("pristine"))
        validator = RecordValidator(compiled.entity("thing"))
        record = GeneratedRecord(
            entity="thing",
            values={"thing_id": 1, "label": "x", "score": 99},
            damage={"label": "malformed_text"},
        )
        result = validator.validate(record)
        assert not result.ok, "score is out of bounds and was not damaged"

    def test_a_deliberate_duplicate_does_not_fail_uniqueness(self) -> None:
        project = make_project(
            {
                "thing": {
                    "count": 300,
                    "primary_key": "thing_id",
                    "fields": {
                        "thing_id": {"type": "integer", "generator": "sequence", "unique": True}
                    },
                }
            },
            chaos={"duplicates": 0.05},
        )
        engine = GenerationEngine(compile_project(project))
        records = asyncio.run(engine.generate_batch("thing", 300))
        assert len(records) > 300, "duplicates should have been emitted"
        assert engine.validation_stats()["thing"]["records_rejected"] == 0


# --------------------------------------------------------------------------- #
# End to end (sections 25, 26)
# --------------------------------------------------------------------------- #


LEDGER: dict[str, Any] = {
    "account": {
        "count": 25,
        "primary_key": "account_id",
        "fields": {"account_id": {"type": "integer", "generator": "sequence", "start": 1000}},
    },
    "transaction": {
        "count": 600,
        "primary_key": "txn_id",
        "simulation": {
            "subject": "account",
            "distribution": "skewed",
            "minimum": 4,
            "state": {
                "balance": {
                    "initial": "500",
                    "update": "balance + amount",
                    "min": 0,
                    "precision": 2,
                }
            },
        },
        "fields": {
            "txn_id": {"type": "integer", "generator": "sequence"},
            "account": {"generator": "subject"},
            "occurred_at": {"type": "datetime", "generator": "event_time"},
            "amount": {
                "type": "integer",
                "generator": "expression",
                "expression": "(txn_id * 37) % 300 - 140",
            },
            "balance": {"type": "decimal", "generator": "state"},
        },
    },
}


def ledger_project() -> Any:
    return make_project(LEDGER, timeline={"start": "2026-01-01", "end": "2026-06-30"})


class TestSimulatedGeneration:
    def _records(self, offset: int = 0, count: int = 600) -> list[Any]:
        engine = GenerationEngine(compile_project(ledger_project()))
        return [
            r.values
            for r in asyncio.run(engine.generate_batch("transaction", count, offset=offset))
        ]

    def test_events_belong_to_subjects_and_are_ordered(self) -> None:
        rows = self._records()
        by_account: dict[Any, list[Any]] = {}
        for row in rows:
            by_account.setdefault(row["account"], []).append(row["occurred_at"])
        assert len(by_account) == 25
        for moments in by_account.values():
            assert moments == sorted(moments), "a subject's events must be chronological"

    def test_the_subject_key_takes_the_type_it_points_at(self) -> None:
        rows = self._records(count=5)
        assert isinstance(rows[0]["account"], int)

    def test_the_balance_is_a_running_total(self) -> None:
        rows = self._records()
        by_account: dict[Any, list[Any]] = {}
        for row in rows:
            by_account.setdefault(row["account"], []).append(row)
        for txns in by_account.values():
            for previous, current in pairwise(txns):
                expected = max(0, float(previous["balance"]) + float(current["amount"]))
                assert float(current["balance"]) == pytest.approx(expected, abs=0.011)

    def test_the_minimum_clamps_a_decimal(self) -> None:
        """`min: 0` has to hold for the type money is generated as."""
        assert all(float(row["balance"]) >= 0 for row in self._records())

    def test_a_resumed_run_computes_the_same_state(self) -> None:
        """Section 26's central claim."""
        whole = self._records()
        for offset in (37, 150, 401):
            resumed = self._records(offset=offset, count=20)
            assert [str(r["balance"]) for r in resumed] == [
                str(r["balance"]) for r in whole[offset : offset + 20]
            ]

    def test_resuming_replays_only_one_subject_s_block(self) -> None:
        """Not the whole dataset - which is what makes resume affordable."""
        engine = GenerationEngine(compile_project(ledger_project()))
        asyncio.run(engine.generate_batch("transaction", 5, offset=400))
        machine = engine.simulations["transaction"].machine
        assert 0 < machine.replays < 200

    def test_generating_in_pieces_equals_one_pass(self) -> None:
        whole = self._records()
        pieces: list[Any] = []
        for start in range(0, 600, 100):
            pieces.extend(self._records(offset=start, count=100))
        assert [str(r["balance"]) for r in pieces] == [str(r["balance"]) for r in whole]

    def test_an_unknown_subject_entity_is_refused(self) -> None:
        entities = json.loads(json.dumps(LEDGER))
        entities["transaction"]["simulation"]["subject"] = "ghost"
        with pytest.raises(SchemaError, match="ghost"):
            compile_project(make_project(entities))

    def test_event_time_needs_a_timeline(self) -> None:
        engine = GenerationEngine(compile_project(make_project(LEDGER)))
        with pytest.raises(GenerationError, match="timeline"):
            asyncio.run(engine.generate_batch("transaction", 5))

    def test_a_state_field_needs_a_simulation(self) -> None:
        entities = json.loads(json.dumps(LEDGER))
        del entities["transaction"]["simulation"]
        with pytest.raises((GenerationError, SchemaError)):
            engine = GenerationEngine(compile_project(make_project(entities)))
            asyncio.run(engine.generate_batch("transaction", 5))


# --------------------------------------------------------------------------- #
# Worlds (section 16)
# --------------------------------------------------------------------------- #


class TestWorlds:
    def _compiled(self, seed: int = 42, count: int = 100) -> Any:
        return compile_project(
            make_project(
                {
                    "person": {
                        "count": count,
                        "fields": {"name": {"generator": "faker", "provider": "name"}},
                    }
                },
                seed=seed,
            )
        )

    def test_a_world_records_the_seed_and_populations(self, tmp_path: Path) -> None:
        world = World.of("acme", self._compiled())
        assert world.seed == 42
        assert world.populations == {"person": 100}
        assert world.schema_hash

    def test_it_survives_a_round_trip(self, tmp_path: Path) -> None:
        store = WorldStore(tmp_path)
        store.save(World.of("acme", self._compiled()))
        loaded = WorldStore(tmp_path).get("acme")
        assert loaded is not None and loaded.seed == 42

    def test_the_same_world_generates_the_same_people(self) -> None:
        """The whole point (section 16)."""
        world = World.of("acme", self._compiled())

        other = self._compiled(seed=999, count=20)
        world.apply_to(other)
        engine = GenerationEngine(other)
        second = [r.values["name"] for r in asyncio.run(engine.generate_batch("person", 20))]

        original = GenerationEngine(self._compiled())
        first = [r.values["name"] for r in asyncio.run(original.generate_batch("person", 20))]
        assert first == second

    def test_a_changed_seed_is_reported(self) -> None:
        world = World.of("acme", self._compiled())
        problems = world.conflicts_with(self._compiled(seed=777))
        assert any("seed" in problem for problem in problems)

    def test_a_changed_population_is_not_a_conflict(self) -> None:
        """Generating a smaller dataset from the same world is the normal case.

        The first twenty people of a population are the same twenty people
        whether the population is a hundred or five thousand, so a count is not
        part of a world's identity.
        """
        world = World.of("acme", self._compiled(count=100))
        assert world.conflicts_with(self._compiled(count=5000)) == []

    def test_a_changed_field_is_reported(self) -> None:
        changed = compile_project(
            make_project(
                {
                    "person": {
                        "count": 100,
                        "fields": {"name": {"generator": "faker", "provider": "first_name"}},
                    }
                },
                seed=42,
            )
        )
        world = World.of("acme", self._compiled())
        assert world.conflicts_with(changed)

    def test_an_unchanged_project_reports_nothing(self) -> None:
        world = World.of("acme", self._compiled())
        assert world.conflicts_with(self._compiled()) == []

    def test_runs_are_remembered(self, tmp_path: Path) -> None:
        store = WorldStore(tmp_path)
        store.save(World.of("acme", self._compiled()))
        store.record_run("acme", "run-1")
        store.record_run("acme", "run-2")
        assert WorldStore(tmp_path).get("acme").runs == ["run-1", "run-2"]

    def test_deleting(self, tmp_path: Path) -> None:
        store = WorldStore(tmp_path)
        store.save(World.of("acme", self._compiled()))
        assert store.delete("acme") is True
        assert store.delete("acme") is False


# --------------------------------------------------------------------------- #
# The shipped template (section 71)
# --------------------------------------------------------------------------- #


class TestSecurityTemplate:
    def test_it_compiles_with_its_scenarios(self) -> None:
        compiled = compile_project(load_project(SECURITY))
        enabled = [s for s in compiled.spec.scenarios.values() if s.enabled]
        assert len(enabled) >= 4

    def test_it_produces_a_correlated_incident(self) -> None:
        compiled = compile_project(load_project(SECURITY))
        counts = dict.fromkeys(compiled.entity_order, 400)
        counts["authentication"] = 6000
        counts["user"] = 400

        engine = GenerationEngine(compiled, counts=counts)
        rows = [r.values for r in asyncio.run(engine.generate_batch("authentication", 6000))]

        affected = [row for row in rows if row.get("scenario")]
        assert affected, "no scenario fired at this scale"

        # Everything a scenario touched belongs to a selected identity, and the
        # same identity is affected consistently.
        for name in {row["scenario"] for row in affected}:
            subjects = {row["user"] for row in rows if row.get("scenario") == name}
            assert subjects

    def test_sign_ins_follow_the_working_week(self) -> None:
        compiled = compile_project(load_project(SECURITY))
        engine = GenerationEngine(
            compiled, counts={"user": 200, "authentication": 4000}, chaos=False
        )
        rows = [r.values for r in asyncio.run(engine.generate_batch("authentication", 4000))]
        weekend = sum(1 for row in rows if row["timestamp"].weekday() >= 5)
        assert weekend / len(rows) < 0.12
