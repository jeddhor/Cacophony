"""Hierarchical deterministic randomness (design document section 75)."""

from __future__ import annotations

import collections

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cacophony.core.seeds import SeedChain, derive_seed, mix_seed, rng_for

MASK64 = (1 << 64) - 1


class TestDeriveSeed:
    def test_is_deterministic(self) -> None:
        assert derive_seed(42, "employee") == derive_seed(42, "employee")

    def test_distinguishes_labels(self) -> None:
        assert derive_seed(42, "employee") != derive_seed(42, "device")

    def test_distinguishes_parents(self) -> None:
        assert derive_seed(1, "x") != derive_seed(2, "x")

    def test_label_boundaries_cannot_collide(self) -> None:
        """Length-prefixing means ('ab','c') and ('a','bc') are different keys."""
        assert derive_seed(0, "ab", "c") != derive_seed(0, "a", "bc")

    @given(st.integers(min_value=0, max_value=MASK64), st.text(max_size=40))
    def test_always_in_range(self, parent: int, label: str) -> None:
        assert 0 <= derive_seed(parent, label) <= MASK64


class TestMixSeed:
    def test_is_deterministic(self) -> None:
        assert mix_seed(7, 1, 2) == mix_seed(7, 1, 2)

    def test_salts_separate_levels(self) -> None:
        """Entity 3, record 3 and field 3 must not collide under one parent."""
        assert len({mix_seed(99, salt, 3) for salt in (0x1F0, 0x2E1, 0x3D2, 0x4C3)}) == 4

    @given(
        st.integers(min_value=0, max_value=MASK64),
        st.integers(min_value=0, max_value=1 << 32),
        st.integers(min_value=0, max_value=1 << 32),
    )
    def test_always_in_range(self, parent: int, salt: int, component: int) -> None:
        assert 0 <= mix_seed(parent, salt, component) <= MASK64

    def test_consecutive_indices_do_not_correlate(self) -> None:
        """Adjacent record indices must not produce adjacent or patterned seeds."""
        seeds = [mix_seed(12345, 0x2E1, index) for index in range(4096)]
        assert len(set(seeds)) == 4096
        # Low byte should be close to uniform across 256 buckets.
        buckets = collections.Counter(seed & 0xFF for seed in seeds)
        assert len(buckets) == 256
        expected = 4096 / 256
        assert max(buckets.values()) < expected * 2.5


class TestSeedChain:
    def test_same_position_same_seed(self) -> None:
        chain = SeedChain.root(2026).entity("employee")
        assert chain.record(17).field("email").seed == chain.record(17).field("email").seed

    def test_different_positions_differ(self) -> None:
        chain = SeedChain.root(2026).entity("employee")
        assert chain.record(17).field("email").seed != chain.record(18).field("email").seed
        assert chain.record(17).field("email").seed != chain.record(17).field("name").seed

    def test_record_seed_is_directly_addressable(self) -> None:
        """Section 32: resuming at record 6,830,000 must not replay the first 6,829,999."""
        chain = SeedChain.root(1).entity("event")
        far = chain.record(6_830_000).field("id").seed
        assert far == SeedChain.root(1).entity("event").record(6_830_000).field("id").seed

    def test_entities_are_independent(self) -> None:
        root = SeedChain.root(500)
        assert root.entity("a").record(0).seed != root.entity("b").record(0).seed

    def test_project_seed_changes_everything(self) -> None:
        left = SeedChain.root(1).entity("e").record(3).field("f").seed
        right = SeedChain.root(2).entity("e").record(3).field("f").seed
        assert left != right

    def test_chain_is_immutable(self) -> None:
        chain = SeedChain.root(9)
        with pytest.raises(AttributeError):
            chain.seed = 10  # type: ignore[misc]

    def test_labelled_field_keeps_a_readable_path(self) -> None:
        chain = SeedChain.root(9).entity("employee").record(2)
        labelled = chain.labelled_field("email")
        assert labelled.label == "entity/employee/record/2/field/email"
        # ...and produces the same seed as the fast path.
        assert labelled.seed == chain.field("email").seed

    def test_rng_is_reproducible(self) -> None:
        seed = SeedChain.root(77).entity("x").record(1).field("y").seed
        assert [rng_for(seed).random() for _ in range(3)] == [
            rng_for(seed).random() for _ in range(3)
        ]

    def test_field_seeds_spread_across_many_names(self) -> None:
        record = SeedChain.root(4).entity("e").record(0)
        seeds = {record.field(f"field_{i}").seed for i in range(1000)}
        assert len(seeds) == 1000
