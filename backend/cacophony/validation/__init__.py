"""Validation layer (design document sections 13, 57 and 58)."""

from .pipeline import RecordValidator
from .referential import (
    DistributionCheck,
    QualityReport,
    ReferentialValidator,
    StatisticalValidator,
)
from .results import Severity, ValidationIssue, ValidationResult, ValidationStats
from .validators import ConstraintValidator, StructuralValidator

#: Validation categories contributed by plugins (section 44).
#:
#: Section 57 names six categories and the platform implements them; a
#: `ValidatorPlugin` adds a seventh. Kept as a plain dict rather than a class so
#: a plugin needs no import from a private module to reach it.
_EXTRA_VALIDATORS: dict[str, type] = {}


def extra_validators() -> dict[str, type]:
    """The validators plugins have contributed."""
    return _EXTRA_VALIDATORS


__all__ = [
    "ConstraintValidator",
    "DistributionCheck",
    "QualityReport",
    "RecordValidator",
    "ReferentialValidator",
    "Severity",
    "StatisticalValidator",
    "StructuralValidator",
    "ValidationIssue",
    "ValidationResult",
    "ValidationStats",
    "extra_validators",
]
