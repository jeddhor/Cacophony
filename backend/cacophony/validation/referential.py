"""Referential and statistical validation (design document sections 57, 58).

Section 57 names six validation categories. Phase one implemented the two that
need only a value and its declaration; these are the two that need to know
something about the dataset around it:

``referential``
    The foreign key exists. Cacophony derives references from the parent's
    index rather than looking them up, so a key is valid by construction -
    which makes this a check on the *machinery*, not on the model. It earns
    its place because chaos injection deliberately breaks references
    (section 78), and because a schema can point at an entity that a
    particular run did not generate.

``statistical``
    Generated distributions approximately match the declared ones. This is the
    check that catches the mistakes nobody notices: a weighted choice whose
    weights were mistyped still produces valid records, and the dataset is
    quietly wrong in a way no per-record validator can see.

Both are sampling checks. Comparing ten million references against their
parents one at a time would cost more than generating them did, so referential
validation checks a bounded sample and reports what it checked - an honest
partial answer rather than an expensive complete one nobody waits for.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .results import Severity, ValidationResult

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from ..core.record import GeneratedRecord
    from ..generation.relations import EntityResolver
    from ..schema.plan import CompiledEntity

__all__ = ["DistributionCheck", "ReferentialValidator", "StatisticalValidator"]


class ReferentialValidator:
    """Checks that foreign keys point at records that exist (section 57)."""

    category = "referential"

    def __init__(
        self,
        entity: CompiledEntity,
        resolver: EntityResolver,
        *,
        sample_every: int = 1,
    ) -> None:
        self.entity = entity
        self.resolver = resolver
        #: Check one record in every ``sample_every``. 1 checks everything.
        self.sample_every = max(1, sample_every)
        self.checked = 0
        self.broken = 0
        self._seen = 0

        # Only reference fields have anything to check, and only those whose
        # target this run can actually resolve.
        self.references: list[tuple[str, str, str | None]] = []
        for compiled in entity.fields:
            target = getattr(compiled.generator, "target", None)
            if isinstance(target, str):
                self.references.append(
                    (compiled.name, target, getattr(compiled.generator, "target_field", None))
                )

    @property
    def is_noop(self) -> bool:
        return not self.references

    def validate(
        self, record: GeneratedRecord, *, skip: dict[str, str] | None = None
    ) -> ValidationResult:
        result = ValidationResult()
        if self.is_noop:
            return result

        self._seen += 1
        if (self._seen - 1) % self.sample_every != 0:
            return result

        for field_name, target, target_field in self.references:
            # A reference chaos deliberately made stale is not a broken
            # generator (section 24).
            if skip and field_name in skip:
                continue
            value = record.values.get(field_name)
            if value is None:
                continue
            self.checked += 1
            try:
                valid = self.resolver.is_valid_key(target, target_field, value)
            except Exception:
                valid = False
            if not valid:
                self.broken += 1
                result.add(
                    self.category,
                    f"{value!r} does not identify a record of '{target}'",
                    field_name=field_name,
                    value=value,
                )
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "references_checked": self.checked,
            "broken_references": self.broken,
            "integrity": round(1.0 - (self.broken / self.checked), 6) if self.checked else 1.0,
            "sample_every": self.sample_every,
        }


@dataclass(slots=True)
class DistributionCheck:
    """How closely one field's output matched what it declared."""

    entity: str
    field: str
    expected: dict[str, float]
    observed: dict[str, float]
    samples: int
    #: Total variation distance: half the sum of absolute differences. 0 means
    #: identical, 1 means disjoint. Chosen over chi-square because it is a
    #: proportion a person can read, not a statistic they have to look up.
    distance: float

    @property
    def match(self) -> float:
        return max(0.0, 1.0 - self.distance)

    @property
    def confident(self) -> bool:
        """Whether there were enough samples for the number to mean anything."""
        return self.samples >= 30 * max(1, len(self.expected))

    def worst(self) -> tuple[str, float, float] | None:
        """The value that missed its target by the most."""
        if not self.expected:
            return None
        name = max(
            self.expected, key=lambda key: abs(self.expected[key] - self.observed.get(key, 0.0))
        )
        return name, self.expected[name], self.observed.get(name, 0.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity": self.entity,
            "field": self.field,
            "samples": self.samples,
            "match": round(self.match, 6),
            "distance": round(self.distance, 6),
            "confident": self.confident,
            "expected": {key: round(value, 6) for key, value in self.expected.items()},
            "observed": {key: round(value, 6) for key, value in self.observed.items()},
        }


class StatisticalValidator:
    """Compares generated distributions with declared ones (sections 57, 58).

    Only fields that declare a distribution can be checked, which in practice
    means weighted choices. A normal distribution over a float has a shape but
    not a set of expected proportions, so it is left alone rather than
    subjected to a test it would fail for uninteresting reasons.
    """

    category = "statistical"

    def __init__(self, entity: CompiledEntity, *, tolerance: float = 0.08) -> None:
        self.entity = entity
        #: Total variation distance beyond which a field is reported.
        self.tolerance = tolerance
        self.samples = 0
        self.counts: dict[str, Counter[str]] = {}
        self.expected: dict[str, dict[str, float]] = {}

        for compiled in entity.fields:
            reporter = getattr(compiled.generator, "distribution", None)
            if reporter is None:
                continue
            try:
                declared = reporter()
            except Exception:
                continue
            if declared:
                self.expected[compiled.name] = {str(k): float(v) for k, v in declared.items()}
                self.counts[compiled.name] = Counter()

    @property
    def is_noop(self) -> bool:
        return not self.expected

    def observe(self, record: GeneratedRecord) -> None:
        if self.is_noop:
            return
        self.samples += 1
        for name, counter in self.counts.items():
            value = record.values.get(name)
            if value is not None:
                counter[str(value)] += 1

    def observe_all(self, records: Sequence[GeneratedRecord]) -> None:
        for record in records:
            self.observe(record)

    def checks(self) -> list[DistributionCheck]:
        results: list[DistributionCheck] = []
        for name, expected in self.expected.items():
            counter = self.counts[name]
            total = sum(counter.values())
            if total == 0:
                continue
            observed = {key: count / total for key, count in counter.items()}
            distance = 0.5 * sum(
                abs(expected.get(key, 0.0) - observed.get(key, 0.0))
                for key in set(expected) | set(observed)
            )
            results.append(
                DistributionCheck(
                    entity=self.entity.name,
                    field=name,
                    expected=expected,
                    observed=observed,
                    samples=total,
                    distance=distance,
                )
            )
        return results

    def report(self) -> ValidationResult:
        """Findings worth a human's attention.

        Always a warning, never an error: a distribution that came out 12% away
        from its declaration is a schema worth looking at, not a record worth
        discarding. Where the sample was too small for the number to mean much
        the message says so, rather than being suppressed - a mistyped weight
        on a two-hundred-record run is still a mistyped weight.
        """
        result = ValidationResult()
        for check in self.checks():
            if check.distance <= self.tolerance:
                continue
            worst = check.worst()
            detail = (
                f" - '{worst[0]}' was declared {worst[1]:.1%} and came out {worst[2]:.1%}"
                if worst
                else ""
            )
            caveat = "" if check.confident else f" (only {check.samples:,} samples)"
            result.add(
                self.category,
                f"generated distribution differs from the declared one by "
                f"{check.distance:.1%}{detail}{caveat}",
                field_name=check.field,
                severity=Severity.WARNING,
            )
        return result

    def to_dict(self) -> dict[str, Any]:
        checks = self.checks()
        return {
            "samples": self.samples,
            "fields_checked": len(checks),
            "distribution_match": round(sum(check.match for check in checks) / len(checks), 6)
            if checks
            else 1.0,
            "checks": [check.to_dict() for check in checks],
        }


@dataclass(slots=True)
class QualityReport:
    """Section 58's project score, assembled from what actually happened."""

    schema_validity: float = 1.0
    constraint_validity: float = 1.0
    referential_integrity: float = 1.0
    distribution_match: float = 1.0
    parse_success: float = 1.0
    extra: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, float]:
        return {
            "schema_validity": round(self.schema_validity, 6),
            "constraint_validity": round(self.constraint_validity, 6),
            "referential_integrity": round(self.referential_integrity, 6),
            "distribution_match": round(self.distribution_match, 6),
            "llm_parse_success": round(self.parse_success, 6),
            **{key: round(value, 6) for key, value in self.extra.items()},
        }

    def render(self) -> str:
        """The shape section 58 prints."""
        rows = self.to_dict()
        width = max(len(name) for name in rows)
        return "\n".join(
            f"{name.replace('_', ' ').title():<{width + 2}} {value:.2%}"
            for name, value in rows.items()
        )


def sample_size_for(values: int, *, confidence: float = 0.95) -> int:
    """How many records make a distribution check meaningful.

    A rule of thumb rather than a derivation: roughly thirty observations per
    category, floored at a hundred. Reported alongside the result so nobody
    reads a confident-looking percentage off twelve records.
    """
    return max(100, int(30 * values * (1.0 + math.log10(max(1.0, 1.0 / (1.0 - confidence))))))
