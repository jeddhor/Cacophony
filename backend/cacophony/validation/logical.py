"""Section 57's logical category: rules about a record, not about a value.

    termination_date >= hire_date

That is the section's own example, and it is a different kind of statement from
the ones the other validators make. A constraint asks whether a value is
acceptable; a logical assertion asks whether a *record* makes sense, which needs
more than one field to answer.

Assertions are evaluated with the same restricted evaluator as the ``expression``
generator and the ``--where`` filters: a parsed syntax tree, an allow-list of
node types and functions, no imports and no attribute access. A project file is
something people send each other, and an assertion is not a way around that.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..core.errors import SchemaError
from ..transforms.expressions import RecordExpression
from .results import Severity, ValidationResult

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..core.record import GeneratedRecord
    from ..schema.plan import CompiledEntity

__all__ = ["LogicalValidator"]


class LogicalValidator:
    """Checks an entity's ``assertions:`` against each record."""

    category = "logical"

    def __init__(self, entity: CompiledEntity) -> None:
        self.entity = entity
        self._rules: list[tuple[Any, str, frozenset[str]]] = []

        for index, spec in enumerate(entity.spec.assertions):
            where = f"{entity.name}.assertions[{index}]"
            # Compiled again here, cheaply: the schema compiler has already
            # refused anything unparseable or naming a field that does not
            # exist, so by this point this is only building what it will run.
            expression = RecordExpression(spec.expr, where=where)
            mentions = frozenset(_names_in(spec.expr, entity))
            self._rules.append((expression, spec.describe(), mentions))

    @property
    def is_noop(self) -> bool:
        return not self._rules

    def validate(
        self, record: GeneratedRecord, *, skip: set[str] | None = None
    ) -> ValidationResult:
        """Check every assertion, reporting the ones that are false.

        An assertion that names a field entropy injection damaged is skipped:
        chaos produces records that break the rules on purpose, and reporting
        that would be reporting the feature (sections 24, 78).
        """
        result = ValidationResult()
        damaged = skip or set()

        for expression, described, mentions in self._rules:
            if mentions & damaged:
                continue
            try:
                verdict = expression.evaluate(record.values)
            except SchemaError as exc:
                # A rule that cannot be evaluated is a schema problem, not a
                # record problem, and saying so is more useful than a silent
                # pass.
                result.add(
                    self.category,
                    f"assertion could not be evaluated: {exc}",
                    severity=Severity.ERROR,
                )
                continue
            if not verdict:
                result.add(self.category, f"assertion failed: {described}", severity=Severity.ERROR)
        return result


def _names_in(source: str, entity: CompiledEntity) -> set[str]:
    """Which of the entity's fields an assertion mentions.

    Used only to decide whether damage should silence it, so a generous match is
    the right kind of wrong: naming a field that is not really referenced costs
    a skipped check on a record that was deliberately broken anyway.
    """
    import re

    words = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", source))
    return {field.name for field in entity.fields if field.name in words}
