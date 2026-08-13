"""Structured output enforcement (design document section 13).

    "Never trust raw model output."

Even with constrained decoding, a model's answer arrives as text and has to
survive six stages before it is allowed near a dataset::

    Generate -> Parse -> Validate -> Repair if possible -> Retry if necessary -> Accept

This module owns extraction, parsing and repair. Type and constraint validation
is delegated to :mod:`cacophony.validation`, because a value coming from a
language model must clear exactly the same bar as one coming from Faker - there
is no reason for two sets of rules, and having two would guarantee they drift.

Repair here means *deterministic* fixes to shape: pulling JSON out of a
markdown fence, closing an unterminated string, trimming a value to its
declared maximum length. Repairs that would require judgement are not attempted;
those become a retry with a repair prompt, which is the model's job to answer.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..core.types import coerce_value
from ..validation.results import Severity, ValidationResult

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from ..schema.plan import CompiledField

__all__ = ["ParsedRecord", "StructuredOutputError", "extract_json", "parse_record", "parse_records"]

#: ```json fenced blocks, the single most common wrapper models add.
_FENCE = re.compile(r"```(?:json|JSON)?\s*(.+?)\s*```", re.DOTALL)
#: Trailing commas before a closing brace or bracket.
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")
#: A leading label such as "Here is the JSON:" before the payload.
_PREAMBLE = re.compile(r"^[^{\[]*(?=[{\[])", re.DOTALL)


class StructuredOutputError(Exception):
    """Model output could not be turned into a record.

    Carries the raw text so the retry ladder can build a repair prompt from
    what actually came back (section 66).
    """

    def __init__(self, message: str, *, raw: str = "", stage: str = "parse") -> None:
        self.raw = raw
        self.stage = stage
        super().__init__(message)


@dataclass(slots=True)
class ParsedRecord:
    """One record's worth of model output, after parsing and validation."""

    values: dict[str, Any]
    result: ValidationResult = field(default_factory=ValidationResult)
    repairs: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.result.ok

    @property
    def problems(self) -> str:
        """A short account of what is wrong, for a repair prompt."""
        return "; ".join(issue.render() for issue in self.result.errors)


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #


def extract_json(text: str) -> Any:
    """Pull a JSON document out of whatever the model actually said.

    Tried in order, cheapest first: the text as-is, the contents of a fenced
    block, the span between the outermost braces, and finally the same span
    with a few deterministic syntax repairs applied.
    """
    if not text or not text.strip():
        raise StructuredOutputError("the model returned an empty response", raw=text)

    candidates: list[str] = [text.strip()]

    fenced = _FENCE.search(text)
    if fenced:
        candidates.append(fenced.group(1).strip())

    span = _outermost_span(text)
    if span:
        candidates.append(span)

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    for candidate in candidates:
        repaired = _repair_syntax(candidate)
        if repaired != candidate:
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                continue

    raise StructuredOutputError(
        f"the model's response is not valid JSON: {_snippet(text)}", raw=text
    )


def _outermost_span(text: str) -> str | None:
    """The substring from the first ``{`` or ``[`` to its matching close."""
    stripped = _PREAMBLE.sub("", text)
    if not stripped:
        return None
    opening = stripped[0]
    if opening not in "{[":
        return None
    closing = "}" if opening == "{" else "]"

    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(stripped):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = in_string
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return stripped[: index + 1]
    return stripped or None


def _repair_syntax(text: str) -> str:
    """Deterministic fixes for the ways models usually break JSON."""
    repaired = _TRAILING_COMMA.sub(r"\1", text)

    # An unterminated string, then unclosed containers - the classic shape of a
    # response cut off by a token limit.
    if repaired.count('"') % 2 == 1:
        repaired += '"'
    opens = repaired.count("{") - repaired.count("}")
    if opens > 0:
        repaired += "}" * opens
    brackets = repaired.count("[") - repaired.count("]")
    if brackets > 0:
        repaired += "]" * brackets

    return repaired


# --------------------------------------------------------------------------- #
# Parsing into records
# --------------------------------------------------------------------------- #


def parse_record(
    text: str,
    fields: Sequence[CompiledField],
    *,
    repair: bool = True,
) -> ParsedRecord:
    """Parse one record from model output and validate it against ``fields``."""
    payload = extract_json(text)

    if isinstance(payload, list):
        # Asked for one, given a list: take the first rather than fail, since
        # the content is right and only the wrapper is wrong.
        if not payload:
            raise StructuredOutputError("the model returned an empty array", raw=text)
        payload = payload[0]

    if not isinstance(payload, dict):
        raise StructuredOutputError(
            f"expected a JSON object, got {type(payload).__name__}", raw=text
        )

    return _validate_payload(payload, fields, repair=repair)


def parse_records(
    text: str,
    fields: Sequence[CompiledField],
    *,
    expected: int,
    repair: bool = True,
) -> list[ParsedRecord]:
    """Parse a batch of records (section 11, batch mode).

    A short batch is accepted rather than rejected: the caller decides whether
    to retry for the remainder or fall back per record, and throwing away good
    records because the model produced nine instead of ten would be wasteful.
    """
    payload = extract_json(text)

    if isinstance(payload, dict):
        # The prompt asks for {"records": [...]}, but models routinely answer
        # with some other single key wrapping the array, or with one bare
        # object when the batch size is 1.
        for key in ("records", "data", "items", "results", "rows"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
        else:
            payload = [payload]

    if not isinstance(payload, list):
        raise StructuredOutputError(
            f"expected a JSON array of records, got {type(payload).__name__}", raw=text
        )

    parsed = [
        _validate_payload(entry, fields, repair=repair)
        for entry in payload[:expected]
        if isinstance(entry, dict)
    ]
    if not parsed:
        raise StructuredOutputError("the model returned no usable records", raw=text)
    return parsed


def _validate_payload(
    payload: dict[str, Any],
    fields: Sequence[CompiledField],
    *,
    repair: bool,
) -> ParsedRecord:
    """Coerce, repair and validate one parsed object against the field specs."""
    from ..validation.validators import ConstraintValidator, StructuralValidator

    values: dict[str, Any] = {}
    result = ValidationResult()
    repairs: list[str] = []

    lowered = {str(key).lower(): key for key in payload}
    consumed: set[str] = set()

    for compiled in fields:
        spec = compiled.spec
        name = spec.name

        # Case-insensitive lookup: a model asked for "resolution_notes" will
        # occasionally answer with "Resolution_Notes", and rejecting a whole
        # record over its capitalisation would be absurd.
        key = name if name in payload else lowered.get(name.lower())
        if key is not None:
            consumed.add(key)
        if key is None:
            values[name] = None
            if spec.effective_null_probability <= 0:
                result.add(
                    "structured",
                    f"the model omitted required field '{name}'",
                    field_name=name,
                )
            continue

        value = coerce_value(payload[key], spec.type)

        if repair:
            value, applied = _repair_value(value, spec)
            repairs.extend(f"{name}: {note}" for note in applied)

        values[name] = value

        structural = StructuralValidator(spec).validate_sync(value)
        if structural.was_repaired:
            values[name] = structural.repaired_value
            value = structural.repaired_value
            repairs.append(f"{name}: coerced to {spec.type.value}")
        result.issues.extend(structural.issues)

        constraint = ConstraintValidator(spec)
        if not constraint.is_noop:
            result.issues.extend(constraint.validate_sync(value).issues)

    # Anything the model volunteered that no field claimed. A warning rather
    # than an error: the record is still usable, but a schema whose prompts
    # keep drawing extra fields is worth looking at.
    for name in sorted(set(payload) - consumed):
        result.add(
            "structured",
            f"the model invented a field '{name}' that is not in the schema",
            field_name=name,
            severity=Severity.WARNING,
        )

    return ParsedRecord(values=values, result=result, repairs=repairs)


def _repair_value(value: Any, spec: Any) -> tuple[Any, list[str]]:
    """Deterministic value repairs that need no judgement."""
    repairs: list[str] = []
    constraints = spec.constraints

    if isinstance(value, str):
        stripped = value.strip()
        # Models like to wrap a single value in quotes it was not asked for.
        if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in "\"'":
            stripped = stripped[1:-1].strip()
        if stripped != value:
            repairs.append("trimmed surrounding whitespace or quotes")
            value = stripped

        if constraints.max_length is not None and len(value) > constraints.max_length:
            # Cut at a sentence or word boundary where one is close, so a
            # trimmed biography does not end mid-syllable.
            value = _truncate(value, constraints.max_length)
            repairs.append(f"truncated to {constraints.max_length} characters")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(constraints.min, (int, float)) and value < constraints.min:
            value = constraints.min
            repairs.append(f"clamped up to the minimum {constraints.min}")
        if isinstance(constraints.max, (int, float)) and value > constraints.max:
            value = constraints.max
            repairs.append(f"clamped down to the maximum {constraints.max}")

    return value, repairs


def _truncate(text: str, limit: int) -> str:
    window = text[:limit]
    for boundary in (". ", "! ", "? "):
        cut = window.rfind(boundary)
        if cut >= limit * 0.6:
            return window[: cut + 1]
    space = window.rfind(" ")
    if space >= limit * 0.6:
        return window[:space].rstrip(" ,;:-")
    return window


def _snippet(text: str, limit: int = 160) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"
