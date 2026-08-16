"""Event allocation (design document sections 15, 25, 26).

The problem this solves is the one that makes simulated data hard.

A login event belongs to an employee, happens at a time, and - if anything is
being simulated statefully - depends on that employee's earlier events. Phase 5
gave references: a child picks a parent at random, cheaply, at any scale. That
is right for a purchase order and wrong for a *timeline*, because two events
that reference the same employee arrive in whatever order the indices happened
to fall, and "the fortieth login of this employee" is not a question the record
can answer.

The fix is to stop choosing the parent and start *laying out* the events:

    subject 0   ####################          events 0..19
    subject 1   ########                       events 20..27
    subject 2   ##############################  events 28..57
    ...

Each subject gets a contiguous block sized by the chosen distribution. From a
record's index, a binary search yields three things in O(log P):

    which subject it belongs to
    which of that subject's events it is
    how many events that subject has in total

Which is exactly what the timeline needs (``ordered(k, n)``) and exactly what a
stateful fold needs (replay from the start of this block). Nothing is sorted,
nothing is held in memory, and record *n* remains a pure function of *n*.

The cost is that a subject's events are contiguous in the output file rather
than interleaved. For a dataset meant to be queried that is invisible; for one
meant to be read top to bottom it is obvious, so ``interleave`` shuffles the
emission order deterministically without disturbing the mapping.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..core.errors import SchemaError
from ..core.seeds import mix_seed

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

__all__ = ["SHARE_DISTRIBUTIONS", "Allocation", "Placement"]

#: How events are shared out among subjects.
SHARE_DISTRIBUTIONS = ("uniform", "skewed", "zipf")

#: Distinguishes this module's seed derivations from every other level of the
#: hierarchy (section 75), so an allocation and a field never draw alike.
_SALT = 0xA11C_5C47


@dataclass(frozen=True, slots=True)
class Placement:
    """Where one event sits: whose it is, and which of theirs."""

    subject: int
    ordinal: int
    total: int

    @property
    def quantile(self) -> float:
        """How far through this subject's own history the event falls."""
        return (self.ordinal + 0.5) / self.total if self.total else 0.0

    @property
    def is_first(self) -> bool:
        return self.ordinal == 0

    @property
    def is_last(self) -> bool:
        return self.ordinal == self.total - 1


class Allocation:
    """Shares ``events`` out among ``subjects`` and answers questions about it.

    Built once per entity at compile time. The cumulative table is one integer
    per subject: ten thousand employees cost eighty kilobytes, and ten million
    events cost nothing extra because events are never enumerated.
    """

    def __init__(
        self,
        events: int,
        subjects: int,
        *,
        distribution: str = "uniform",
        skew: float = 1.6,
        seed: int = 0,
        minimum: int = 0,
    ) -> None:
        if subjects <= 0:
            raise SchemaError("cannot allocate events among no subjects")
        if distribution not in SHARE_DISTRIBUTIONS:
            raise SchemaError(
                f"unknown share distribution '{distribution}'. "
                f"Available: {', '.join(SHARE_DISTRIBUTIONS)}"
            )

        self.events = max(0, events)
        self.subjects = subjects
        self.distribution = distribution
        self.skew = max(0.05, skew)
        self.seed = seed
        #: Events every subject gets before the rest is shared out. An employee
        #: with no logins at all is usually a bug in the data, not a person.
        self.minimum = max(0, minimum)

        self._counts = self._share()
        self._starts = self._accumulate(self._counts)

    # -- the share ----------------------------------------------------------- #

    def _share(self) -> list[int]:
        """How many events each subject gets."""
        if self.events == 0:
            return [0] * self.subjects

        floor = min(self.minimum, self.events // self.subjects)
        remaining = self.events - floor * self.subjects
        if remaining <= 0:
            return _even(self.events, self.subjects)

        weights = self._weights()
        total_weight = sum(weights) or 1.0

        counts = [floor] * self.subjects
        assigned = floor * self.subjects
        # Largest-remainder apportionment: hand out the whole numbers, then the
        # leftovers to whoever was rounded down hardest. Every event is placed
        # exactly once, which a naive round() does not guarantee.
        remainders: list[tuple[float, int]] = []
        for index, weight in enumerate(weights):
            exact = remaining * weight / total_weight
            whole = int(exact)
            counts[index] += whole
            assigned += whole
            remainders.append((exact - whole, index))

        remainders.sort(key=lambda pair: (-pair[0], pair[1]))
        for _fraction, index in remainders[: self.events - assigned]:
            counts[index] += 1
        return counts

    def _weights(self) -> list[float]:
        """The relative share of each subject."""
        if self.distribution == "uniform":
            return [1.0] * self.subjects

        if self.distribution == "zipf":
            # Rank r gets 1/r: the classic long tail, steeper than `skewed`.
            return [1.0 / (rank + 1) for rank in range(self.subjects)]

        # `skewed`: a bounded power law over a *shuffled* rank, so the busy
        # subjects are scattered through the population rather than being the
        # first few - employee 1 is not inherently the busiest person.
        #
        # The exponent is chosen so that `skew` means here exactly what it
        # means for a reference distribution (see
        # :func:`cacophony.generation.relations.pick_index`): the busiest tenth
        # of subjects take ``0.1 ** (1 / skew)`` of the events. A schema that
        # sets `skew: 1.9` in two places should not get two different shapes.
        exponent = 1.0 / self.skew - 1.0
        weights = [0.0] * self.subjects
        for rank in range(self.subjects):
            position = (rank + 0.5) / self.subjects
            weights[self._scatter(rank)] = position**exponent
        return weights

    def _scatter(self, rank: int) -> int:
        """Map a rank to a subject, deterministically and without collisions.

        A multiplicative permutation: cheap, seed-dependent, and a bijection
        over the range as long as the multiplier is coprime with the modulus.
        """
        if self.subjects == 1:
            return 0
        multiplier = _coprime(self.subjects, self.seed)
        offset = mix_seed(self.seed, _SALT, 1) % self.subjects
        return (rank * multiplier + offset) % self.subjects

    @staticmethod
    def _accumulate(counts: Sequence[int]) -> list[int]:
        starts: list[int] = []
        running = 0
        for count in counts:
            running += count
            starts.append(running)
        return starts

    # -- lookups -------------------------------------------------------------- #

    def locate(self, index: int) -> Placement:
        """Which subject event ``index`` belongs to, and which of theirs it is."""
        if not 0 <= index < self.events:
            raise SchemaError(f"event {index} is outside this allocation of {self.events}")

        subject = bisect.bisect_right(self._starts, index)
        subject = min(subject, self.subjects - 1)
        start = self._starts[subject - 1] if subject else 0
        return Placement(subject=subject, ordinal=index - start, total=self._counts[subject])

    def count_for(self, subject: int) -> int:
        return self._counts[subject] if 0 <= subject < self.subjects else 0

    def start_of(self, subject: int) -> int:
        """The event index at which this subject's block begins."""
        if not 0 <= subject < self.subjects:
            return 0
        return self._starts[subject - 1] if subject else 0

    def counts(self) -> list[int]:
        return list(self._counts)

    def sample_subject(self, index: int) -> int:
        """A subject drawn from the same distribution, for one event.

        The block layout answers "whose is event *n* of a known total". A
        stream has no total, so this answers the same question by inverse
        transform over the cumulative shares, seeded by the index - the same
        distribution, the same busy subjects, and still reproducible, but
        interleaved the way a stream's events actually arrive.
        """
        if self.subjects == 1 or not self._starts:
            return 0
        total = self._starts[-1]
        if total <= 0:
            return index % self.subjects
        draw = (mix_seed(self.seed, _SALT, index) & 0xFFFFFFFF) / 0x100000000
        return min(bisect.bisect_right(self._starts, int(draw * total)), self.subjects - 1)

    # -- description ----------------------------------------------------------- #

    def describe(self) -> dict[str, Any]:
        counts = sorted(self._counts, reverse=True)
        busiest = sum(counts[: max(1, self.subjects // 10)])
        return {
            "events": self.events,
            "subjects": self.subjects,
            "distribution": self.distribution,
            "mean_per_subject": round(self.events / self.subjects, 2),
            "max_per_subject": counts[0] if counts else 0,
            "min_per_subject": counts[-1] if counts else 0,
            "top_decile_share": round(busiest / self.events, 4) if self.events else 0.0,
        }


def _even(events: int, subjects: int) -> list[int]:
    """As equal as integers allow, with the remainder spread from the front."""
    base, extra = divmod(events, subjects)
    return [base + (1 if index < extra else 0) for index in range(subjects)]


def _coprime(modulus: int, seed: int) -> int:
    """An odd multiplier coprime with ``modulus``, derived from ``seed``."""
    from math import gcd

    candidate = (mix_seed(seed, _SALT, 0) % modulus) | 1
    for _ in range(64):
        if gcd(candidate, modulus) == 1:
            return candidate
        candidate = (candidate + 2) % modulus or 1
    return 1
