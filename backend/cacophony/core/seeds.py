"""Deterministic hierarchical randomness (design document section 75).

Cacophony must produce identical output for identical configuration plus
identical seed, *even when records are generated out of order or in parallel*.
A single global RNG stream cannot do this: worker 3 finishing before worker 1
would change every subsequent value.

Instead every random draw is keyed by its position in a hierarchy::

    Project Seed
       -> Entity Seed
          -> Record Seed
             -> Field Seed

Each level is derived by hashing the parent seed together with the level's
label. Hashing (rather than sequential advancement) means the seed for record
4,823,913 can be computed directly without generating the 4,823,912 records
before it - which is what makes resumable, parallel, checkpointed runs possible
(sections 30 and 32).

Two derivation routines live here. :func:`derive_seed` hashes with BLAKE2b: it
accepts arbitrary labels, is stable across Python versions and platforms, and
is the right choice everywhere except the innermost loop. :func:`mix_seed` is
an integer mixer used by :class:`SeedChain` for the per-record and per-field
levels, where the derivation runs once per generated value and a hash
construction would show up in the profile. Both are deterministic,
order-independent and well distributed; neither is intended to be
cryptographically meaningful, because nothing here depends on that.
"""

from __future__ import annotations

import hashlib
import random
import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    import numpy as np

__all__ = ["SeedChain", "derive_seed", "mix_seed", "numpy_generator", "rng_for", "seed_to_bytes"]

_MASK64 = (1 << 64) - 1
_DIGEST_SIZE = 8

# SplitMix64 constants. The finalizer below is the standard one; it has good
# avalanche behaviour and is a handful of integer operations rather than a hash
# construction.
_GOLDEN = 0x9E3779B97F4A7C15
_MIX_A = 0xBF58476D1CE4E5B9
_MIX_B = 0x94D049BB133111EB
_SALT_MUL = 0xC2B2AE3D27D4EB4F

# Distinct salts per hierarchy level, so entity 3, record 3 and field 3 never
# collide even when the parent seed is the same.
_SALT_ENTITY = 0x1F0
_SALT_RECORD = 0x2E1
_SALT_FIELD = 0x3D2
_SALT_SUB = 0x4C3

#: Cache of label -> 64-bit key. Bounded by the number of distinct entity and
#: field *names* in a project, which is small; record indices never enter here.
_LABEL_KEYS: dict[str, int] = {}


def seed_to_bytes(seed: int) -> bytes:
    """Render a 64-bit seed as stable little-endian bytes."""
    return struct.pack("<Q", seed & _MASK64)


def derive_seed(parent: int, *labels: Any) -> int:
    """Derive a child seed from ``parent`` and one or more labels.

    Labels are stringified and length-prefixed so that ``("ab", "c")`` and
    ``("a", "bc")`` cannot collide.

    >>> derive_seed(42, "employee") == derive_seed(42, "employee")
    True
    >>> derive_seed(42, "employee") != derive_seed(42, "device")
    True
    """
    hasher = hashlib.blake2b(seed_to_bytes(parent), digest_size=_DIGEST_SIZE)
    for label in labels:
        encoded = str(label).encode("utf-8")
        hasher.update(struct.pack("<I", len(encoded)))
        hasher.update(encoded)
    return int.from_bytes(hasher.digest(), "little")


def mix_seed(parent: int, salt: int, component: int) -> int:
    """Derive a child seed with integer mixing rather than hashing.

    This is the hot-path counterpart to :func:`derive_seed`. A ten-million-row
    entity with fifteen fields performs 150 million derivations; at that volume
    the difference between a hash construction and six integer operations is
    the difference between the seed hierarchy being free and it being the
    bottleneck that section 89 says the core must not become.

    It keeps the properties the hierarchy actually depends on - deterministic,
    order-independent, directly addressable, well distributed - and gives up
    only the ones it never used, namely any cryptographic claim about
    recovering ``parent`` from the result.

    ``salt`` distinguishes hierarchy levels; ``component`` is a record index or
    a cached label key.
    """
    value = (parent ^ (salt * _SALT_MUL) ^ (component * _GOLDEN)) & _MASK64
    value = (value + _GOLDEN) & _MASK64
    value ^= value >> 30
    value = (value * _MIX_A) & _MASK64
    value ^= value >> 27
    value = (value * _MIX_B) & _MASK64
    return value ^ (value >> 31)


def _label_key(label: str) -> int:
    """A stable 64-bit key for a name, hashed once and then cached.

    Python's own ``hash`` is salted per process and would break reproducibility
    across runs, so this hashes explicitly.
    """
    key = _LABEL_KEYS.get(label)
    if key is None:
        key = int.from_bytes(
            hashlib.blake2b(label.encode("utf-8"), digest_size=_DIGEST_SIZE).digest(), "little"
        )
        _LABEL_KEYS[label] = key
    return key


def rng_for(seed: int) -> random.Random:
    """Return a freshly seeded :class:`random.Random` for ``seed``."""
    return random.Random(seed & _MASK64)


def numpy_generator(seed: int) -> np.random.Generator:
    """Return a NumPy generator for ``seed``.

    NumPy is imported lazily: the deterministic generators that dominate
    throughput never need it, and importing NumPy costs real milliseconds.
    """
    import numpy as np

    return np.random.default_rng(seed & _MASK64)


@dataclass(frozen=True, slots=True)
class SeedChain:
    """A position in the project -> entity -> record -> field seed hierarchy.

    The chain is immutable; descending returns a new chain, so a chain handed
    to a generator can never be mutated by it.

    >>> chain = SeedChain.root(2026)
    >>> employee = chain.entity("employee")
    >>> record = employee.record(17)
    >>> record.field("email").seed == employee.record(17).field("email").seed
    True
    """

    seed: int
    path: tuple[str, ...] = ()

    @classmethod
    def root(cls, project_seed: int) -> SeedChain:
        """Start a chain at the project seed."""
        return cls(seed=project_seed & _MASK64, path=())

    def descend(self, *labels: Any) -> SeedChain:
        """Derive a child chain for an arbitrary label sequence.

        Uses :func:`derive_seed`, so this is the general-purpose route. The
        four named levels below take the faster path.
        """
        return SeedChain(
            seed=derive_seed(self.seed, *labels),
            path=(*self.path, *(str(label) for label in labels)),
        )

    def entity(self, name: str) -> SeedChain:
        return SeedChain(
            seed=mix_seed(self.seed, _SALT_ENTITY, _label_key(name)),
            path=(*self.path, "entity", name),
        )

    def record(self, index: int) -> SeedChain:
        # Called once per record; the path is what a debug log or a provenance
        # block prints, so it is still built here.
        return SeedChain(
            seed=mix_seed(self.seed, _SALT_RECORD, index),
            path=(*self.path, "record", str(index)),
        )

    def field(self, name: str) -> SeedChain:
        """Derive the seed for one field of the current record.

        The hottest call in the system - once per field per record - so the
        path is left empty rather than extended. Nothing on the generation path
        reads it, and building a tuple of strings per field value costs more
        than the seed derivation itself. Use :meth:`labelled_field` when the
        readable path matters, such as when writing full provenance.
        """
        return SeedChain(seed=mix_seed(self.seed, _SALT_FIELD, _label_key(name)))

    def labelled_field(self, name: str) -> SeedChain:
        """As :meth:`field`, but retaining the human-readable path."""
        return SeedChain(
            seed=mix_seed(self.seed, _SALT_FIELD, _label_key(name)),
            path=(*self.path, "field", name),
        )

    def sub(self, label: Any) -> SeedChain:
        """Derive a nested chain, e.g. for an element of an array field."""
        component = label if isinstance(label, int) else _label_key(str(label))
        return SeedChain(
            seed=mix_seed(self.seed, _SALT_SUB, component),
            path=(*self.path, "sub", str(label)),
        )

    def rng(self) -> random.Random:
        """A stdlib RNG bound to this position in the hierarchy."""
        return rng_for(self.seed)

    def numpy(self) -> np.random.Generator:
        """A NumPy generator bound to this position in the hierarchy."""
        return numpy_generator(self.seed)

    @property
    def label(self) -> str:
        """A human-readable path, useful in provenance and debug logs."""
        return "/".join(self.path) if self.path else "<project>"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SeedChain({self.label}, seed={self.seed})"
