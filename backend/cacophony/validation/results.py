"""Validation results (design document sections 13, 56, 57 and 58)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

__all__ = ["Severity", "ValidationIssue", "ValidationResult", "ValidationStats"]


class Severity(StrEnum):
    WARNING = "warning"
    ERROR = "error"


@dataclass(slots=True)
class ValidationIssue:
    """One thing wrong with one value."""

    category: str
    message: str
    field: str | None = None
    severity: Severity = Severity.ERROR
    value: Any = None

    def render(self) -> str:
        where = f"{self.field}: " if self.field else ""
        return f"[{self.category}] {where}{self.message}"


@dataclass(slots=True)
class ValidationResult:
    """The outcome of validating a value or a whole record."""

    issues: list[ValidationIssue] = field(default_factory=list)
    #: Set when a validator repaired the value rather than rejecting it.
    repaired_value: Any = None
    was_repaired: bool = False

    @property
    def ok(self) -> bool:
        return not any(issue.severity is Severity.ERROR for issue in self.issues)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.severity is Severity.WARNING]

    def add(
        self,
        category: str,
        message: str,
        *,
        field_name: str | None = None,
        severity: Severity = Severity.ERROR,
        value: Any = None,
    ) -> None:
        self.issues.append(
            ValidationIssue(
                category=category,
                message=message,
                field=field_name,
                severity=severity,
                value=value,
            )
        )

    def merge(self, other: ValidationResult) -> None:
        self.issues.extend(other.issues)
        if other.was_repaired:
            self.was_repaired = True
            self.repaired_value = other.repaired_value

    def render(self) -> str:
        return "\n".join(issue.render() for issue in self.issues) or "ok"

    @classmethod
    def valid(cls) -> ValidationResult:
        return cls()


@dataclass(slots=True)
class ValidationStats:
    """Running totals for a run, feeding the quality metrics of section 58."""

    records_checked: int = 0
    records_valid: int = 0
    records_repaired: int = 0
    records_rejected: int = 0
    issues_by_category: dict[str, int] = field(default_factory=dict)

    def record(self, result: ValidationResult) -> None:
        self.records_checked += 1
        if result.ok:
            self.records_valid += 1
        else:
            self.records_rejected += 1
        if result.was_repaired:
            self.records_repaired += 1
        for issue in result.issues:
            self.issues_by_category[issue.category] = (
                self.issues_by_category.get(issue.category, 0) + 1
            )

    @property
    def validity_rate(self) -> float:
        if self.records_checked == 0:
            return 1.0
        return self.records_valid / self.records_checked

    def to_dict(self) -> dict[str, Any]:
        return {
            "records_checked": self.records_checked,
            "records_valid": self.records_valid,
            "records_repaired": self.records_repaired,
            "records_rejected": self.records_rejected,
            "validity_rate": round(self.validity_rate, 6),
            "issues_by_category": dict(self.issues_by_category),
        }
