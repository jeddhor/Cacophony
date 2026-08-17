"""Patch rules (design document section 104).

    For small datasets, allow manual editing.

    For enormous datasets, editing individual rows is inappropriate. Instead
    support: regeneration, transformations, filtering, patch rules.

**A rule, not a mutation. This is the whole point of the module.**

A Cacophony dataset is a pure function of its schema and its seed. That is the
property everything else rests on: it is why a run can be resumed, why a shard
can be regenerated on another machine, why two people generating from the same
file get the same people. Editing a row in an output file breaks it silently -
the file no longer corresponds to anything, and the next run overwrites the edit
without noticing.

So an edit is recorded as a rule in the project:

    patches:
      mask_finance_emails:
        entity: employee
        where: "department == 'Finance'"
        set:
          email: "mask(email)"

which is applied *during generation*. The dataset stays a function of the
project - a longer function, with the patches in it - and regenerating record
4,823,913 next year produces the same masked address. The Studio's record editor
therefore offers "save as a patch rule" rather than "save this row": the second
would be a lie about what the file is.

The same rules can be applied to a file that already exists, which is what
``cacophony transform`` does - useful when the dataset took nine hours and the
mistake was in the last field.

Three kinds of rule, matching section 104's list:

``set``      transform or replace fields on matching records
``drop``     filter records out
``keep``     the same filter, stated the other way round
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import TYPE_CHECKING, Any

from ..core.errors import SchemaError
from .expressions import RecordExpression
from .operations import apply_operations, describe_operations, parse_step

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping, Sequence

    from ..schema.models import PatchSpec

__all__ = ["FieldEdit", "PatchRule", "PatchSet", "PatchStats"]


@dataclass(slots=True)
class FieldEdit:
    """What one rule does to one field.

    Exactly one of the three, checked when the rule is read: a rule that both
    transformed and replaced a field would apply in an order nobody could guess
    from the YAML.
    """

    field: str
    #: A pipeline of section 105 operations: ``mask:4``, ``hash``, ``round:2``.
    operations: list[tuple[str, str | None]] = dataclass_field(default_factory=list)
    #: An expression over the record, for a derived replacement.
    expression: RecordExpression | None = None
    #: A literal value.
    value: Any = None
    has_value: bool = False

    def apply(self, record: dict[str, Any]) -> Any:
        if self.operations:
            return apply_operations(record.get(self.field), self.operations)
        if self.expression is not None:
            return self.expression.evaluate(record)
        return self.value

    def describe(self) -> str:
        if self.operations:
            return f"{self.field} = {describe_operations(self.operations)}"
        if self.expression is not None:
            return f"{self.field} = {self.expression.source}"
        return f"{self.field} = {self.value!r}"

    @classmethod
    def parse(cls, name: str, raw: Any, *, where: str) -> FieldEdit:
        """Read one entry of a ``set:`` block.

        Three spellings, because three things are being said:

            email: "mask:4"                 a pipeline of operations
            email: {expression: "..."}      a derived value
            email: {value: "redacted"}      a literal
        """
        location = f"{where}.set.{name}"

        if isinstance(raw, str):
            # A bare string is a pipeline when every step names an operation,
            # and an expression otherwise. Checked rather than guessed: `mask:4`
            # is unambiguous, and `upper(name)` is too.
            steps = [part.strip() for part in raw.split("|") if part.strip()]
            try:
                parsed = [parse_step(step) for step in steps]
            except Exception:
                return cls(field=name, expression=RecordExpression(raw, where=location))
            return cls(field=name, operations=parsed)

        if isinstance(raw, list):
            return cls(field=name, operations=[parse_step(step) for step in raw])

        if isinstance(raw, dict):
            if "operations" in raw or "transform" in raw:
                steps = raw.get("operations") or raw.get("transform") or []
                if isinstance(steps, str):
                    steps = [steps]
                return cls(field=name, operations=[parse_step(step) for step in steps])
            if "expression" in raw:
                return cls(
                    field=name,
                    expression=RecordExpression(str(raw["expression"]), where=location),
                )
            if "value" in raw:
                return cls(field=name, value=raw["value"], has_value=True)
            raise SchemaError(
                f"{location}: needs 'operations', 'expression' or 'value'. "
                f"Got: {', '.join(sorted(raw)) or '<nothing>'}"
            )

        # A bare number, boolean or null is a literal.
        return cls(field=name, value=raw, has_value=True)


@dataclass(slots=True)
class PatchRule:
    """One named rule (section 104)."""

    name: str
    entity: str = ""
    description: str = ""
    condition: RecordExpression | None = None
    edits: list[FieldEdit] = dataclass_field(default_factory=list)
    #: Drop matching records rather than editing them.
    drop: bool = False
    #: Keep *only* matching records. The same filter, said the other way.
    keep: bool = False

    @property
    def is_filter(self) -> bool:
        return self.drop or self.keep

    def applies_to(self, entity: str) -> bool:
        return not self.entity or self.entity == entity

    def matches(self, record: Mapping[str, Any]) -> bool:
        return self.condition is None or self.condition.matches(record)

    def describe(self) -> str:
        parts = [self.name]
        if self.entity:
            parts.append(f"on {self.entity}")
        if self.condition is not None:
            parts.append(f"where {self.condition.source}")
        if self.drop:
            parts.append("drop")
        elif self.keep:
            parts.append("keep only")
        else:
            parts.append("set " + "; ".join(edit.describe() for edit in self.edits))
        return " ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "entity": self.entity,
            "description": self.description,
            "where": self.condition.source if self.condition else None,
            "drop": self.drop,
            "keep": self.keep,
            "set": [edit.describe() for edit in self.edits],
        }

    @classmethod
    def parse(cls, name: str, raw: Any) -> PatchRule:
        where = f"patches.{name}"
        if not isinstance(raw, dict):
            raise SchemaError(f"{where}: a patch rule must be a mapping")

        drop = bool(raw.get("drop", False))
        keep = bool(raw.get("keep", False))
        if drop and keep:
            raise SchemaError(f"{where}: a rule cannot both drop and keep. Choose one.")

        sets = raw.get("set") or {}
        if not isinstance(sets, dict):
            raise SchemaError(f"{where}: 'set' must be a mapping of field names")
        if (drop or keep) and sets:
            raise SchemaError(
                f"{where}: a filter rule has nothing to set. Split it into two rules if "
                "you meant to edit some records and drop others."
            )
        if not drop and not keep and not sets:
            raise SchemaError(
                f"{where}: does nothing. Give it a 'set' block, or 'drop: true', or 'keep: true'."
            )

        condition = raw.get("where") or raw.get("when")
        return cls(
            name=name,
            entity=str(raw.get("entity") or ""),
            description=str(raw.get("description") or "").strip(),
            condition=(
                RecordExpression(str(condition), where=f"{where}.where") if condition else None
            ),
            edits=[
                FieldEdit.parse(field_name, value, where=where)
                for field_name, value in sets.items()
            ],
            drop=drop,
            keep=keep,
        )

    @classmethod
    def from_spec(cls, name: str, spec: PatchSpec) -> PatchRule:
        return cls.parse(name, spec.model_dump(exclude_none=True, exclude_defaults=False))


@dataclass(slots=True)
class PatchStats:
    """What a patch pass did, for the run summary."""

    records_seen: int = 0
    records_edited: int = 0
    records_dropped: int = 0
    values_changed: int = 0
    by_rule: dict[str, int] = dataclass_field(default_factory=dict)

    def note(self, rule: str) -> None:
        self.by_rule[rule] = self.by_rule.get(rule, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "records_seen": self.records_seen,
            "records_edited": self.records_edited,
            "records_dropped": self.records_dropped,
            "values_changed": self.values_changed,
            "by_rule": dict(sorted(self.by_rule.items())),
        }


class PatchSet:
    """The rules for one entity, applied in declared order.

    Order is the authored order, deliberately. Rules compose - masking a column
    and then hashing it is a different thing from hashing and then masking - and
    a set that reordered them would produce a dataset nobody could predict from
    reading the file.
    """

    def __init__(self, rules: Sequence[PatchRule], *, entity: str = "") -> None:
        self.entity = entity
        self.rules = [rule for rule in rules if rule.applies_to(entity)] if entity else list(rules)
        self.stats = PatchStats()

    @property
    def is_noop(self) -> bool:
        return not self.rules

    def apply(self, record: dict[str, Any]) -> dict[str, Any] | None:
        """Apply every rule to one record. ``None`` means it was filtered out."""
        self.stats.records_seen += 1
        edited = False

        for rule in self.rules:
            matched = rule.matches(record)
            if rule.drop:
                if matched:
                    self.stats.records_dropped += 1
                    self.stats.note(rule.name)
                    return None
                continue
            if rule.keep:
                if not matched:
                    self.stats.records_dropped += 1
                    self.stats.note(rule.name)
                    return None
                continue
            if not matched:
                continue

            for edit in rule.edits:
                before = record.get(edit.field)
                after = edit.apply(record)
                record[edit.field] = after
                if after != before:
                    self.stats.values_changed += 1
            self.stats.note(rule.name)
            edited = True

        if edited:
            self.stats.records_edited += 1
        return record

    def describe(self) -> dict[str, Any]:
        return {
            "entity": self.entity,
            "rules": [rule.to_dict() for rule in self.rules],
            **self.stats.to_dict(),
        }


def load_rules(raw: Any) -> list[PatchRule]:
    """Read a ``patches:`` block into rules, in authored order."""
    if not raw:
        return []
    if not isinstance(raw, dict):
        raise SchemaError("'patches' must be a mapping of rule names")
    return [PatchRule.parse(str(name), value) for name, value in raw.items()]
