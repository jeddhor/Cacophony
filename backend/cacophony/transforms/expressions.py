"""Expressions over a record that already exists (sections 104, 105).

The ``expression`` generator evaluates against a record being built, through a
:class:`~cacophony.core.context.GenerationContext`. Patch rules and
``cacophony transform`` evaluate against a record that has already been written -
a plain mapping read back from a file - so they need the same evaluator with a
different namespace.

**The allow-lists are imported, not restated.** ``FUNCTIONS`` and the permitted
AST node types come from
:class:`~cacophony.generation.generators.text.ExpressionGenerator`, which is the
part that decides what a shared project file is allowed to do. Two copies of a
security boundary is one copy too many: the day they diverged, the safer one
would be the one nobody was using.

What that buys: no ``eval``, no imports, no comprehensions, no attribute access
beyond a dotted record lookup, and no name that is not a field of the record or
a listed function. A ``where:`` clause in a project somebody sent you cannot
reach outside the record it is describing.
"""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING, Any

from ..core.errors import SchemaError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping

__all__ = ["RecordExpression"]


def _allow_lists() -> tuple[dict[str, Any], tuple[type[ast.AST], ...]]:
    from ..generation.generators.text import ExpressionGenerator

    return dict(ExpressionGenerator.FUNCTIONS), tuple(ExpressionGenerator._ALLOWED_NODES)


class RecordExpression:
    """One expression, compiled once and evaluated per record.

    Compiled at construction so a bad expression is reported when a rule is
    read - during ``cacophony validate`` - rather than four million records into
    a transform.
    """

    def __init__(self, source: str, *, where: str = "expression") -> None:
        self.source = source.strip()
        self.where = where
        if not self.source:
            raise SchemaError(f"{where}: the expression is empty")

        functions, allowed = _allow_lists()
        self._functions = functions

        try:
            tree = ast.parse(self.source, mode="eval")
        except SyntaxError as exc:
            raise SchemaError(f"{where}: could not parse {self.source!r} - {exc.msg}") from exc

        for node in ast.walk(tree):
            if not isinstance(node, allowed):
                raise SchemaError(
                    f"{where}: {type(node).__name__} is not permitted in an expression. "
                    "Expressions may read fields, call the listed functions, compare and "
                    "combine - nothing else."
                )
        self._code = compile(tree, filename="<cacophony-record-expression>", mode="eval")

        #: Field names the expression mentions, for reporting and for checking a
        #: rule against a schema before it is run.
        self.names = frozenset(
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name) and node.id not in functions
        )

    def evaluate(self, record: Mapping[str, Any]) -> Any:
        """Evaluate against one record."""
        namespace = _RecordNamespace(record, self._functions, self.where, self.source)
        try:
            return eval(self._code, {"__builtins__": {}}, namespace)
        except SchemaError:
            raise
        except Exception as exc:
            raise SchemaError(f"{self.where}: {self.source!r} failed - {exc}") from exc

    def matches(self, record: Mapping[str, Any]) -> bool:
        return bool(self.evaluate(record))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"RecordExpression({self.source!r})"


class _RecordNamespace(dict):
    """Resolves names against a record read back from a file.

    A missing name names the field rather than raising a bare ``NameError``,
    because the person reading the message is editing a rule and needs to know
    which word was wrong.
    """

    def __init__(
        self,
        record: Mapping[str, Any],
        functions: dict[str, Any],
        where: str,
        source: str,
    ) -> None:
        super().__init__()
        self._record = record
        self._functions = functions
        self._where = where
        self._source = source

    #: What a schema author writes, because the document around the expression
    #: is YAML or JSON rather than Python. Without these, a rule about a
    #: nullable field has to say ``None`` - a Python-ism escaping into a file
    #: that has no other Python in it. A field of the same name still wins,
    #: since that is somebody's data and this is only a convenience.
    _LITERALS = {"null": None, "true": True, "false": False}

    def __missing__(self, key: str) -> Any:
        if key in self._record:
            return self._record[key]
        if key in self._functions:
            return self._functions[key]
        if key in self._LITERALS:
            return self._LITERALS[key]
        # Provenance and asset blocks are prefixed; a rule reading one is
        # almost certainly a typo for a real field.
        available = ", ".join(
            sorted(name for name in self._record if not str(name).startswith("_"))
        )
        raise SchemaError(
            f"{self._where}: {self._source!r} references '{key}', which is not a field of "
            f"this record. Fields: {available or '<none>'}"
        )
