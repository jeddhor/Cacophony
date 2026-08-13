"""The schema linter (design document section 102).

Before generation, warn about designs that are legal but questionable. The
linter runs against a *compiled* project so it can see resolved generators, not
just what the user typed - which is how it can tell that a field with no
generator was inferred to be a language-model field and still has no semantic
description to work from.

Nothing here blocks a run. These are judgements about data quality and cost,
and the user is entitled to overrule every one of them.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from ..core.types import DataType

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .plan import CompiledEntity, CompiledField, CompiledProject

__all__ = ["LintIssue", "LintReport", "Severity", "lint_project"]


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(slots=True)
class LintIssue:
    code: str
    severity: Severity
    location: str
    message: str
    hint: str | None = None

    def render(self) -> str:
        lines = [
            f"{self.severity.value.upper()} [{self.code}] {self.location}",
            f"  {self.message}",
        ]
        if self.hint:
            lines.append(f"  hint: {self.hint}")
        return "\n".join(lines)


@dataclass(slots=True)
class LintReport:
    issues: list[LintIssue]

    def __len__(self) -> int:
        return len(self.issues)

    def __iter__(self) -> Iterator[LintIssue]:
        return iter(self.issues)

    @property
    def errors(self) -> list[LintIssue]:
        return [issue for issue in self.issues if issue.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[LintIssue]:
        return [issue for issue in self.issues if issue.severity is Severity.WARNING]

    @property
    def ok(self) -> bool:
        return not self.errors

    def render(self) -> str:
        if not self.issues:
            return "No issues found."
        return "\n\n".join(issue.render() for issue in self.issues)


#: Above this many records, media generation deserves an explicit warning.
_MEDIA_WARNING_THRESHOLD = 10_000
#: Above this many language-model calls, warn about run duration.
_LLM_WARNING_THRESHOLD = 100_000
#: Names that suggest a human age, for the uniform-distribution check.
_AGE_NAMES = {"age", "employee_age", "customer_age", "patient_age", "user_age"}


def lint_project(compiled: CompiledProject) -> LintReport:
    """Return every issue the linter can find in a compiled project."""
    issues: list[LintIssue] = []

    for entity in compiled.ordered_entities():
        issues.extend(_lint_entity(entity))

    issues.extend(_lint_providers(compiled))
    issues.extend(_lint_chaos(compiled))

    return LintReport(issues=issues)


def _lint_entity(entity: CompiledEntity) -> list[LintIssue]:
    issues: list[LintIssue] = []

    if entity.count == 0:
        issues.append(
            LintIssue(
                code="empty-entity",
                severity=Severity.WARNING,
                location=entity.name,
                message="Entity count is 0, so no records will be produced.",
                hint="Set 'count' or remove the entity.",
            )
        )

    if entity.spec.resolved_primary_key() is None and entity.count > 1:
        issues.append(
            LintIssue(
                code="no-primary-key",
                severity=Severity.INFO,
                location=entity.name,
                message="No primary key is declared.",
                hint=(
                    "Mark a field 'primary_key: true' so other entities can reference "
                    "these records once relationships are in play."
                ),
            )
        )

    if entity.name in entity.depends_on:
        issues.append(
            LintIssue(
                code="self-reference",
                severity=Severity.WARNING,
                location=entity.name,
                message=(
                    f"{entity.name} references itself and may create an unresolved self-reference."
                ),
                hint="Ensure the referenced records are generated before they are consumed.",
            )
        )

    for compiled_field in entity.fields:
        issues.extend(_lint_field(entity, compiled_field))

    return issues


def _lint_field(entity: CompiledEntity, compiled_field: CompiledField) -> list[LintIssue]:
    issues: list[LintIssue] = []
    spec = compiled_field.spec
    location = f"{entity.name}.{compiled_field.name}"
    generator_type = type(compiled_field.generator)
    provider_kind = generator_type.requires_provider

    if provider_kind == "language_model" and not spec.meaning:
        issues.append(
            LintIssue(
                code="llm-without-semantics",
                severity=Severity.WARNING,
                location=location,
                message=(
                    f"{compiled_field.name} uses a language model but has no semantic description."
                ),
                hint=(
                    "Add 'semantic:' describing what the field means. The prompt compiler "
                    "has nothing else to work from."
                ),
            )
        )

    if provider_kind in ("image", "speech") and entity.count > _MEDIA_WARNING_THRESHOLD:
        issues.append(
            LintIssue(
                code="bulk-media",
                severity=Severity.WARNING,
                location=location,
                message=(
                    f"{provider_kind.replace('_', ' ')} generation requested for "
                    f"{entity.count:,} records."
                ),
                hint="Consider generating media for a sampled subset of records.",
            )
        )

    if provider_kind == "language_model" and entity.count > _LLM_WARNING_THRESHOLD:
        issues.append(
            LintIssue(
                code="bulk-llm",
                severity=Severity.WARNING,
                location=location,
                message=(
                    f"{entity.count:,} language-model calls will be made for this field alone."
                ),
                hint=(
                    "Contextual expansion (section 11) generates deterministic fields first "
                    "and enriches in batches, which is usually far cheaper."
                ),
            )
        )

    if _is_suspicious_age(compiled_field):
        issues.append(
            LintIssue(
                code="unrealistic-distribution",
                severity=Severity.WARNING,
                location=location,
                message=(
                    f'Field "{compiled_field.name}" uses a uniform distribution over its '
                    "whole range. This may be unrealistic for workforce data."
                ),
                hint="A normal or log-normal distribution usually models age far better.",
            )
        )

    if spec.type is DataType.TEXT and spec.constraints.max_length is None:
        issues.append(
            LintIssue(
                code="unbounded-text",
                severity=Severity.INFO,
                location=location,
                message="Long text field has no maximum length.",
                hint="Set constraints.max_length so generated prose stays predictable in size.",
            )
        )

    if spec.unique and _domain_too_small(compiled_field, entity.count):
        issues.append(
            LintIssue(
                code="unique-exhaustion",
                severity=Severity.ERROR,
                location=location,
                message=(
                    f"Field is marked unique but its generator can produce fewer than "
                    f"{entity.count:,} distinct values."
                ),
                hint="Widen the value domain or drop the uniqueness requirement.",
            )
        )

    if spec.effective_null_probability >= 0.5:
        issues.append(
            LintIssue(
                code="mostly-null",
                severity=Severity.INFO,
                location=location,
                message=(f"{spec.effective_null_probability:.0%} of values will be null."),
                hint="Confirm that is intended rather than a stray null_probability.",
            )
        )

    return issues


def _is_suspicious_age(compiled_field: CompiledField) -> bool:
    """Flag section 102's example: ``age`` drawn uniformly from 18-90."""
    if compiled_field.name.lower() not in _AGE_NAMES:
        return False
    options = compiled_field.generator.options
    generator_name = compiled_field.generator.name
    if generator_name == "distribution" and options.get("distribution", "uniform") != "uniform":
        return False
    if generator_name not in ("random", "distribution", "integer"):
        return False
    low = options.get("min", options.get("low"))
    high = options.get("max", options.get("high"))
    if not isinstance(low, (int, float)) or not isinstance(high, (int, float)):
        return False
    return (high - low) >= 40


def _domain_too_small(compiled_field: CompiledField, count: int) -> bool:
    """Detect uniqueness that a generator provably cannot satisfy."""
    options = compiled_field.generator.options
    name = compiled_field.generator.name

    if name == "constant":
        return count > 1
    if name in ("weighted", "choice"):
        choices = options.get("choices") or options.get("values") or []
        return len(choices) < count
    if name == "lookup":
        values = options.get("values")
        return isinstance(values, list) and len(values) < count
    if name in ("random", "integer") and compiled_field.spec.type is DataType.INTEGER:
        low, high = options.get("min"), options.get("max")
        if isinstance(low, int) and isinstance(high, int):
            return (high - low + 1) < count
    return False


def _lint_providers(compiled: CompiledProject) -> list[LintIssue]:
    issues: list[LintIssue] = []
    configured = set(compiled.spec.providers)

    needed: set[str] = set()
    for entity in compiled.ordered_entities():
        for compiled_field in entity.fields:
            if type(compiled_field.generator).requires_provider:
                referenced = compiled_field.generator.options.get("provider")
                if isinstance(referenced, str):
                    needed.add(referenced)

    for provider_id in sorted(needed - configured):
        issues.append(
            LintIssue(
                code="missing-provider",
                severity=Severity.ERROR,
                location=f"providers.{provider_id}",
                message=f"Provider '{provider_id}' is referenced by a field but not configured.",
                hint="Add it under 'providers:' with an adapter and base_url.",
            )
        )

    for provider_id in sorted(configured - needed):
        issues.append(
            LintIssue(
                code="unused-provider",
                severity=Severity.INFO,
                location=f"providers.{provider_id}",
                message=f"Provider '{provider_id}' is configured but never used.",
            )
        )

    return issues


def _lint_chaos(compiled: CompiledProject) -> list[LintIssue]:
    chaos = compiled.spec.chaos
    if not chaos.is_enabled():
        return []

    issues: list[LintIssue] = []
    if chaos.referential_anomalies > 0:
        issues.append(
            LintIssue(
                code="chaos-referential",
                severity=Severity.WARNING,
                location="chaos.referential_anomalies",
                message=(
                    f"{chaos.referential_anomalies:.1%} of references will be deliberately broken."
                ),
                hint="Referential validation will report these as failures. That is expected.",
            )
        )
    if chaos.preset == "absolute":
        issues.append(
            LintIssue(
                code="chaos-absolute",
                severity=Severity.INFO,
                location="chaos.preset",
                message="Preset 'absolute' produces deliberately hostile data.",
                hint="Useful for robustness testing; unsuitable as a realistic fixture.",
            )
        )
    return issues
