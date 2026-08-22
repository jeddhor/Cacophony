"""Keeping some of what was thrown away (design document section 56).

Section 56's run inspector asks to browse rejected records, and until now a run
reported only how many there were and a hundred truncated strings. "Four
thousand records failed constraint validation" tells nobody which constraint or
what the values looked like; the records themselves do.

Keeping all of them is not an option: rejections scale with the dataset, and
section 31 says nothing here may. So this keeps a bounded, *seeded* sample -
reservoir sampling with a generator derived from the run's own seed, so the
sample spans the whole run rather than its first few batches, and the same run
keeps the same examples every time it is generated.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..core.record import GeneratedRecord
    from .results import ValidationResult

__all__ = ["DEFAULT_KEEP", "RejectedRecord", "RejectionSample"]

#: How many rejected records one entity keeps. Enough to see a pattern, few
#: enough that a run which rejects everything still costs a fixed amount.
DEFAULT_KEEP = 200


@dataclass(slots=True)
class RejectedRecord:
    """One record that did not make it, and why."""

    entity: str
    index: int
    record_id: str
    categories: list[str]
    issues: list[str]
    values: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity": self.entity,
            "index": self.index,
            "record_id": self.record_id,
            "categories": self.categories,
            "issues": self.issues,
            "values": self.values,
        }


@dataclass(slots=True)
class RejectionSample:
    """A bounded sample of one entity's rejected records.

    Reservoir sampling (Algorithm R), seeded from the run: every rejection has
    an equal chance of being kept whether it happened in the first batch or the
    last, and the answer is the same on a re-run.
    """

    entity: str
    keep: int = DEFAULT_KEEP
    seed: int = 0
    seen: int = 0
    kept: list[RejectedRecord] = field(default_factory=list)
    _rng: random.Random | None = None

    def observe(self, rejected: RejectedRecord) -> None:
        self.seen += 1
        if self.keep <= 0:
            return
        if len(self.kept) < self.keep:
            self.kept.append(rejected)
            return

        if self._rng is None:
            self._rng = random.Random(self.seed)
        # Algorithm R: the nth rejection replaces a held one with probability
        # keep/n, which leaves a uniform sample of everything seen.
        position = self._rng.randrange(self.seen)
        if position < self.keep:
            self.kept[position] = rejected

    def summary(self) -> dict[str, Any]:
        """What was kept, and - just as important - what was not."""
        return {
            "entity": self.entity,
            "rejected": self.seen,
            "kept": len(self.kept),
            "cap": self.keep,
            # Said out loud: a sample that silently stood in for the whole
            # would be a number people would go on to divide by.
            "sampled": self.seen > len(self.kept),
        }


def describe(
    entity: str,
    index: int,
    record_id: str,
    record: GeneratedRecord,
    result: ValidationResult,
) -> RejectedRecord:
    """Turn a rejection into something worth writing down."""
    from ..core.record import to_jsonable

    return RejectedRecord(
        entity=entity,
        index=index,
        record_id=record_id,
        categories=sorted({issue.category for issue in result.errors}),
        issues=[issue.render() for issue in result.errors[:10]],
        values={key: to_jsonable(value) for key, value in record.values.items()},
    )
