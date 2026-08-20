"""Enforcing `unique: true` without holding the dataset (section 31).

The check used to keep every value it had seen in a Python set, which is a
structure whose size is the number of records - the one thing section 31 says
nothing here should have. A ten-million-row run held ten million values for its
whole duration.

It is bounded now, and still exact. These tests are mostly about that second
word: a validator that reported duplicates which were not duplicates would be
worse than the memory it saved.
"""

from __future__ import annotations

import tracemalloc
from typing import Any

import pytest

from cacophony.generation.engine import GenerationEngine
from cacophony.schema.compiler import compile_project
from cacophony.schema.loader import load_project_data
from cacophony.validation.uniqueness import UniqueTracker


class TestTheTracker:
    def test_a_new_value_is_new_and_a_repeat_is_not(self) -> None:
        tracker = UniqueTracker("email")
        assert tracker.add("a@example.com") is True
        assert tracker.add("b@example.com") is True
        assert tracker.add("a@example.com") is False

    def test_types_do_not_collide(self) -> None:
        """1 and "1" are different values, and a shared key would confuse them."""
        tracker = UniqueTracker("id")
        assert tracker.add(1) is True
        assert tracker.add("1") is True

    def test_unhashable_values_are_handled(self) -> None:
        """Array and object fields arrive here too."""
        tracker = UniqueTracker("tags")
        assert tracker.add(["a", "b"]) is True
        assert tracker.add(["a", "b"]) is False
        assert tracker.add({"x": 1}) is True
        assert tracker.add({"x": 1}) is False

    def test_forgetting_gives_a_value_back(self) -> None:
        tracker = UniqueTracker("email")
        tracker.add("a@example.com")
        tracker.forget(["a@example.com"])
        assert tracker.add("a@example.com") is True

    def test_it_spills_at_the_ceiling(self) -> None:
        tracker = UniqueTracker("email", memory_ceiling=50)
        for index in range(60):
            tracker.add(f"{index}@example.com")
        summary = tracker.summary()
        assert summary["spilled"] is True
        assert summary["held_in_memory"] == 0
        tracker.close()

    def test_it_is_exact_across_the_spill(self) -> None:
        """The values that were in memory when it spilled are still known."""
        tracker = UniqueTracker("email", memory_ceiling=50)
        for index in range(200):
            assert tracker.add(f"{index}@example.com") is True
        # Every one of them, before and after the boundary.
        for index in range(200):
            assert tracker.add(f"{index}@example.com") is False
        assert tracker.add("new@example.com") is True
        tracker.close()

    def test_forgetting_works_after_the_spill_too(self) -> None:
        tracker = UniqueTracker("email", memory_ceiling=10)
        for index in range(30):
            tracker.add(f"{index}@example.com")
        tracker.forget(["5@example.com"])
        assert tracker.add("5@example.com") is True
        assert tracker.add("6@example.com") is False
        tracker.close()

    def test_closing_removes_the_scratch_file(self) -> None:
        tracker = UniqueTracker("email", memory_ceiling=5)
        for index in range(20):
            tracker.add(f"{index}@example.com")
        path = tracker._path
        assert path is not None and path.is_file()
        tracker.close()
        assert not path.exists()

    @pytest.mark.scale
    def test_memory_stays_bounded(self) -> None:
        """The claim, measured rather than asserted.

        Two hundred thousand values, with a ceiling low enough to spill early.
        The unbounded version of this holds every one of them.
        """
        values = 200_000
        tracker = UniqueTracker("email", memory_ceiling=5_000)
        tracemalloc.start()
        try:
            for index in range(values):
                tracker.add(f"person{index:06d}@example.com")
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
            tracker.close()

        # Twenty megabytes is generous for five thousand short strings plus
        # SQLite's own buffers, and far below the ~20 MB the values alone would
        # occupy if they were all still resident.
        assert peak < 20_000_000, f"peak was {peak / 1e6:.1f} MB"


class TestUniquenessInARun:
    SCHEMA: dict[str, Any] = {
        "project": {"name": "unique", "seed": 6},
        "entities": {
            "person": {
                "count": 400,
                "fields": {
                    # Two hundred possible values for four hundred records, so
                    # the second half are all duplicates.
                    "badge": {
                        "type": "integer",
                        "generator": "expression",
                        "expression": "int(index) % 200",
                        "unique": True,
                    },
                    "index": {"type": "integer", "generator": "sequence", "start": 0},
                },
            }
        },
    }

    def _engine(self, ceiling: int) -> GenerationEngine:
        compiled = compile_project(load_project_data(self.SCHEMA))
        return GenerationEngine(compiled, validation_policy="report", unique_memory_ceiling=ceiling)

    def test_duplicates_are_found_in_memory(self) -> None:
        engine = self._engine(10_000)
        engine.preview("person", 400)
        assert engine.stats["person"].rejected == 200

    def test_the_same_duplicates_are_found_after_spilling(self) -> None:
        """The point: where the memory lives must not change the answer."""
        engine = self._engine(50)
        engine.preview("person", 400)
        assert engine.stats["person"].rejected == 200

    def test_a_spill_is_reported(self) -> None:
        """A run whose memory profile changed shape should say so."""
        engine = self._engine(50)
        engine.preview("person", 400)
        spilled = engine.validation_stats()["person"]["uniqueness_spilled"]
        assert spilled[0]["field"] == "badge"
        assert spilled[0]["spilled_after"] == 51

    def test_nothing_is_reported_when_nothing_spilled(self) -> None:
        engine = self._engine(10_000)
        engine.preview("person", 400)
        assert "uniqueness_spilled" not in engine.validation_stats()["person"]
