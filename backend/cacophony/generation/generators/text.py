"""Pattern, template and expression generators (design document section 8).

These three are how fields come to depend on one another without anyone
writing code:

* ``pattern``    - shape-based identifiers, ``SRV-{A-Z}{A-Z}-{0000}``
* ``template``   - string interpolation, ``"{first_name}.{last_name}"``
* ``expression`` - derived values, ``lower(first_name + "." + last_name)``

All three report their own dependencies, which is what lets the compiler order
fields correctly (section 101) without special-casing generator types.
"""

from __future__ import annotations

import ast
import hashlib
import re
import string
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from ...core.errors import GenerationError
from ...core.interfaces import SyncGenerator
from ..registry import register_generator
from .base import OptionsMixin

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from ...core.context import GenerationContext

__all__ = ["ExpressionGenerator", "PatternGenerator", "TemplateGenerator"]


# --------------------------------------------------------------------------- #
# Pattern
# --------------------------------------------------------------------------- #

_BRACE = re.compile(r"\{\{|\}\}|\{([^{}]*)\}")
_RANGE = re.compile(r"^([A-Za-z0-9])-([A-Za-z0-9])(?::(\d+))?$")


@register_generator("pattern", aliases=("shape", "mask"))
class PatternGenerator(OptionsMixin, SyncGenerator):
    """Template-based identifier generation (section 8).

    ``pattern: "SRV-{A-Z}{A-Z}-{0000}"`` yields values like ``SRV-KQ-4817``.

    Token forms:
        ``{A-Z}``    one character from the range
        ``{A-Z:3}``  three characters from the range
        ``{0000}``   four random digits (one per ``0`` or ``#``)
        ``{???}``    three random letters
        ``{***}``    three random alphanumerics
        ``{{``/``}}``  a literal brace

    A pattern describes a *shape*, not a regular expression. Full regex-driven
    generation is deliberately out of scope: shapes cover the identifiers real
    systems use, and they stay readable in a schema diff.
    """

    def prepare(self) -> None:
        pattern = self.opt_str("pattern", None, "format", "mask", "template")
        if pattern is None:
            raise self._fail("option 'pattern' is required")
        self.pattern = pattern
        self._tokens = self._parse(pattern)
        self.upper = self.opt_bool("upper", False)
        self.lower = self.opt_bool("lower", False)

    def _parse(self, pattern: str) -> list[tuple[str, Any]]:
        """Pre-compile the pattern into literal and alphabet segments."""
        tokens: list[tuple[str, Any]] = []
        position = 0

        for match in _BRACE.finditer(pattern):
            if match.start() > position:
                tokens.append(("literal", pattern[position : match.start()]))
            position = match.end()

            matched = match.group(0)
            if matched == "{{":
                tokens.append(("literal", "{"))
                continue
            if matched == "}}":
                tokens.append(("literal", "}"))
                continue

            body = match.group(1)
            tokens.append(("draw", self._alphabet_for(body)))

        if position < len(pattern):
            tokens.append(("literal", pattern[position:]))
        return tokens

    def _alphabet_for(self, body: str) -> tuple[str, int]:
        if not body:
            raise self._fail("empty '{}' token in pattern")

        range_match = _RANGE.match(body)
        if range_match:
            low, high, repeat = range_match.groups()
            if ord(low) > ord(high):
                raise self._fail(f"reversed character range '{body}'")
            alphabet = "".join(chr(code) for code in range(ord(low), ord(high) + 1))
            return alphabet, int(repeat or 1)

        unique = set(body)
        if unique <= {"0", "#"}:
            return string.digits, len(body)
        if unique == {"?"}:
            return string.ascii_uppercase, len(body)
        if unique == {"*"}:
            return string.ascii_uppercase + string.digits, len(body)

        raise self._fail(
            f"unrecognised pattern token '{{{body}}}'. Use a range like {{A-Z}}, "
            "digits like {0000}, letters like {???} or alphanumerics like {***}."
        )

    def generate_sync(self, context: GenerationContext) -> Any:
        rng = context.rng()
        parts: list[str] = []
        for kind, payload in self._tokens:
            if kind == "literal":
                parts.append(payload)
            else:
                alphabet, repeat = payload
                parts.append("".join(rng.choice(alphabet) for _ in range(repeat)))
        value = "".join(parts)
        if self.upper:
            return value.upper()
        if self.lower:
            return value.lower()
        return value

    def describe(self) -> str:
        return f"pattern({self.pattern})"


# --------------------------------------------------------------------------- #
# Template
# --------------------------------------------------------------------------- #

_PLACEHOLDER = re.compile(r"\{([A-Za-z_][\w.]*)((?:\|[a-z_]+(?::[^|{}]*)?)*)\}")

_FILTERS: dict[str, Any] = {
    "lower": lambda value, _arg: str(value).lower(),
    "upper": lambda value, _arg: str(value).upper(),
    "title": lambda value, _arg: str(value).title(),
    "strip": lambda value, _arg: str(value).strip(),
    "slug": lambda value, _arg: re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-"),
    "initial": lambda value, _arg: str(value)[:1],
    "trunc": lambda value, arg: str(value)[: int(arg or 10)],
    "pad": lambda value, arg: str(value).rjust(int(arg or 2), "0"),
    "nospace": lambda value, _arg: re.sub(r"\s+", "", str(value)),
}


@register_generator("template", aliases=("interpolate", "format"))
class TemplateGenerator(OptionsMixin, SyncGenerator):
    """Interpolate other fields into a string.

    ``template: "{first_name|lower}.{last_name|lower}@{company.domain}"``

    Filters are chained with ``|`` and may take an argument after ``:``.
    Available filters: ``lower``, ``upper``, ``title``, ``strip``, ``slug``,
    ``initial``, ``trunc:N``, ``pad:N``, ``nospace``.
    """

    def prepare(self) -> None:
        template = self.opt_str("template", None, "format", "pattern")
        if template is None:
            raise self._fail("option 'template' is required")
        self.template = template
        self._references = tuple(
            dict.fromkeys(match.group(1) for match in _PLACEHOLDER.finditer(template))
        )
        if not self._references:
            raise self._fail(
                f"template {template!r} interpolates nothing. Use 'constant' for a fixed value."
            )
        self.on_missing = self.opt_choice("on_missing", ("empty", "error", "keep"), "empty")

    def dependencies(self) -> Sequence[str]:
        return self._references

    def generate_sync(self, context: GenerationContext) -> Any:
        def substitute(match: re.Match[str]) -> str:
            name, filters = match.group(1), match.group(2)
            value = context.value(name, None)
            if value is None:
                if self.on_missing == "error":
                    raise GenerationError(
                        f"{context.location}: template references '{name}', which is null"
                    )
                if self.on_missing == "keep":
                    return match.group(0)
                return ""
            return _apply_filters(value, filters)

        return _PLACEHOLDER.sub(substitute, self.template)

    def describe(self) -> str:
        return f"template({self.template})"


def _apply_filters(value: Any, filters: str) -> str:
    result: Any = value
    for chunk in filters.split("|"):
        if not chunk:
            continue
        name, _, argument = chunk.partition(":")
        handler = _FILTERS.get(name)
        if handler is None:
            raise GenerationError(f"unknown template filter '{name}'")
        result = handler(result, argument or None)
    return str(result)


# --------------------------------------------------------------------------- #
# Expression
# --------------------------------------------------------------------------- #


@register_generator("expression", aliases=("expr", "derived", "computed"))
class ExpressionGenerator(OptionsMixin, SyncGenerator):
    """A value derived from other values (section 8).

    ``expression: 'lower(first_name + "." + last_name + "@" + company.domain)'``

    Expressions are evaluated from a parsed syntax tree with an allow-list of
    node types and functions. There is no ``eval``, no attribute access beyond
    dotted record lookups, no imports, no comprehensions and no name that is
    not either a field of the record or a listed function - so an expression in
    a shared project file cannot reach outside the record it is describing.
    """

    #: Functions an expression may call.
    FUNCTIONS: dict[str, Any] = {
        "lower": lambda value: str(value).lower(),
        "upper": lambda value: str(value).upper(),
        "title": lambda value: str(value).title(),
        "strip": lambda value: str(value).strip(),
        "len": len,
        "str": str,
        "int": int,
        "float": float,
        "bool": bool,
        "round": round,
        "abs": abs,
        "min": min,
        "max": max,
        "sum": lambda values: sum(values),
        "concat": lambda *parts: "".join(str(part) for part in parts),
        "join": lambda separator, values: str(separator).join(str(item) for item in values),
        "replace": lambda value, old, new: str(value).replace(str(old), str(new)),
        "substr": lambda value, start, end=None: str(value)[int(start) : end and int(end)],
        "slug": lambda value: re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-"),
        "coalesce": lambda *values: next((v for v in values if v is not None), None),
        # Not "if": that is a Python keyword, so `if(a, b, c)` would be a
        # syntax error rather than a call.
        "iif": lambda condition, when_true, when_false: when_true if condition else when_false,
        "when": lambda condition, when_true, when_false: when_true if condition else when_false,
        "hash": lambda value, length=12: hashlib.blake2b(
            str(value).encode("utf-8"), digest_size=16
        ).hexdigest()[: int(length)],
        "year": lambda value: _as_datetime(value).year,
        "month": lambda value: _as_datetime(value).month,
        "day": lambda value: _as_datetime(value).day,
        "format_date": lambda value, fmt="%Y-%m-%d": _as_datetime(value).strftime(str(fmt)),
        "contains": lambda haystack, needle: str(needle) in str(haystack),
        "startswith": lambda value, prefix: str(value).startswith(str(prefix)),
    }

    #: AST node types an expression may contain.
    _ALLOWED_NODES = (
        ast.Expression,
        ast.BinOp,
        ast.UnaryOp,
        ast.BoolOp,
        ast.Compare,
        ast.IfExp,
        ast.Call,
        ast.Name,
        ast.Load,
        ast.Attribute,
        ast.Constant,
        ast.List,
        ast.Tuple,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.FloorDiv,
        ast.Mod,
        ast.Pow,
        ast.USub,
        ast.UAdd,
        ast.Not,
        ast.And,
        ast.Or,
        ast.Eq,
        ast.NotEq,
        ast.Lt,
        ast.LtE,
        ast.Gt,
        ast.GtE,
        ast.In,
        ast.NotIn,
    )

    def prepare(self) -> None:
        source = self.opt_str("expression", None, "expr", "formula", "value")
        if source is None:
            raise self._fail("option 'expression' is required")
        self.source = source

        try:
            tree = ast.parse(source, mode="eval")
        except SyntaxError as exc:
            raise self._fail(f"could not parse expression: {exc.msg}") from exc

        self._validate(tree)
        self._tree = tree
        self._code = compile(tree, filename="<cacophony-expression>", mode="eval")
        self._references = self._collect_references(tree)

    def _validate(self, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            if not isinstance(node, self._ALLOWED_NODES):
                raise self._fail(
                    f"expression uses unsupported syntax ({type(node).__name__}). "
                    "Use the 'script' generator for anything this expression cannot express."
                )
            if isinstance(node, ast.Call):
                if not isinstance(node.func, ast.Name):
                    raise self._fail("expression may only call named functions")
                if node.func.id not in self.FUNCTIONS:
                    known = ", ".join(sorted(self.FUNCTIONS))
                    raise self._fail(f"unknown function '{node.func.id}'. Available: {known}")
                if node.keywords:
                    raise self._fail("expression function calls may not use keyword arguments")
            if isinstance(node, ast.Attribute):
                if not isinstance(node.value, ast.Name):
                    raise self._fail(
                        "only single-level dotted references such as 'company.domain' are supported"
                    )
                if node.attr.startswith("_"):
                    # `company.__class__` would resolve through normal attribute
                    # lookup on the proxy and hand back a type object, which is
                    # the first rung of the usual sandbox-escape ladder.
                    raise self._fail(f"attribute '{node.attr}' may not be read from an expression")

    def _collect_references(self, tree: ast.AST) -> tuple[str, ...]:
        """Every free name the expression reads, in ``entity.field`` form where dotted."""
        references: list[str] = []
        called: set[str] = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        dotted_roots: set[str] = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                dotted_roots.add(node.value.id)
                references.append(f"{node.value.id}.{node.attr}")

        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id not in called and node.id not in dotted_roots:
                references.append(node.id)

        return tuple(dict.fromkeys(references))

    def dependencies(self) -> Sequence[str]:
        return self._references

    def generate_sync(self, context: GenerationContext) -> Any:
        namespace = _ExpressionNamespace(context, self.FUNCTIONS)
        try:
            return eval(self._code, {"__builtins__": {}}, namespace)
        except GenerationError:
            raise
        except Exception as exc:
            raise GenerationError(
                f"{context.location}: expression {self.source!r} failed - {exc}"
            ) from exc

    def describe(self) -> str:
        return f"expression({self.source})"


class _ExpressionNamespace(dict):
    """Resolves names in an expression against the record being generated.

    A mapping is used rather than a pre-built dictionary so that dotted lookups
    (``company.domain``) can return a small proxy, and so that a missing name
    produces a message naming the field rather than a bare ``NameError``.
    """

    def __init__(self, context: GenerationContext, functions: dict[str, Any]) -> None:
        super().__init__()
        self._context = context
        self._functions = functions

    def __missing__(self, key: str) -> Any:
        if key in self._functions:
            return self._functions[key]
        if key in self._context.current_record:
            return self._context.current_record[key]
        if key in self._context.related_records:
            return _RelatedProxy(self._context.related_records[key])
        raise GenerationError(
            f"{self._context.location}: expression references '{key}', "
            "which is not a field of this record, a related record, or a known function"
        )


class _RelatedProxy:
    """Read-only attribute access over a related record's values."""

    __slots__ = ("_record",)

    def __init__(self, record: Any) -> None:
        self._record = record

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise GenerationError(f"attribute '{name}' may not be read from an expression")
        try:
            return self._record.values[name]
        except KeyError as exc:
            raise GenerationError(
                f"related record '{self._record.entity}' has no field '{name}'"
            ) from exc


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    return datetime.fromisoformat(str(value))
