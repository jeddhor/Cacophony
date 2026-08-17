"""Edge-case generation (design document section 79).

    A special QA mode should deliberately seek weird values.

    empty strings, maximum lengths, minimum lengths, Unicode names, emoji,
    apostrophes, hyphenated names, RTL text, huge integers, negative numbers,
    leap-day dates, DST boundaries, extreme coordinates

**This is not chaos, and the difference is the whole design.**

Entropy injection (section 24) produces data the schema forbids: a null in a
required column, a date written as ``31/02/2026``, a duplicated primary key. It
answers "what does my pipeline do with broken input".

Edge cases produce data the schema *permits* and naive code mishandles anyway.
``O'Brien-Smith`` is a real surname. ``Ω`` is a real name. A person really can
be born on 29 February. A coordinate really can be at the antimeridian. Every
value here satisfies its field's declared type and its declared constraints -
and an application that cannot store it has a bug, not bad input.

Conflating the two would waste both. A QA mode that emitted nulls would just be
chaos under a second name, and its findings would be indistinguishable from
"you sent me garbage". Findings from *this* mode are not arguable: the record
was valid, and the application still broke.

So every value produced here is validated against the field that will hold it,
and one that does not fit is **not used**. A field with ``max_length: 8`` cannot
hold an emoji-laden name, so it gets the longest legal thing instead. That
refusal is the feature: an edge case that fails validation has told you nothing
about your application and everything about this module's bugs.

**Reproducible, like everything else.** Which records get an edge case, and
which case they get, is derived from the record index (section 75). A run that
found a bug can be re-run to find it again, and a resumed run damages - or
rather, decorates - exactly the same records.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from ..core.seeds import mix_seed
from ..core.types import DataType

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from ..core.record import GeneratedRecord
    from ..schema.models import FieldSpec
    from ..schema.plan import CompiledEntity

__all__ = ["CATEGORIES", "EdgeCaseInjector", "EdgeCaseStats", "cases_for"]

#: Marks a record carrying a deliberate edge case, so a report can separate
#: "this record was designed to be awkward" from "this record is a surprise".
#: Not a field name - a record-level fact - hence the prefix.
EDGE_MARK = "@edge"

#: Distinguishes edge-case draws from every other seed derivation (section 75).
_SALT = 0xED9E5

#: The categories section 79 names, grouped by what they exercise.
CATEGORIES = (
    "boundary_length",
    "unicode_text",
    "emoji",
    "punctuation_names",
    "rtl_text",
    "extreme_numbers",
    "temporal_boundaries",
    "extreme_coordinates",
    "whitespace",
)

# --------------------------------------------------------------------------- #
# The catalogue
# --------------------------------------------------------------------------- #

#: Names that are real, legal, and routinely break form validation, address
#: parsers, CSV writers and anything that assumed ``[A-Za-z ]+``.
_PUNCTUATION_NAMES = (
    "O'Brien-Smith",
    "d'Arcy",
    "Mary-Jane Watson-Parker",
    "Ó Séaghdha",
    "van der Waals",
    "St. John-Stevas",
    "Jean-Luc de la Croix",
    "McDonald-O'Neill",
    "Abu-Bakr al-Rashid",
    "Ng-Chan",
)

#: Scripts, combining marks, and the specific characters that break naive
#: casefolding, width assumptions and normalisation.
_UNICODE_TEXT = (
    "Ω",  # a one-character name
    "Ærandwine Þorsteinsdóttir",
    "Zoë Straße",  # ß casefolds to two characters
    "İstanbul Işık",  # Turkish dotted/dotless I
    "élève",  # combining accents rather than precomposed
    "ﬁnancial ﬂow",  # ligatures NFKC will expand
    "Ｆｕｌｌｗｉｄｔｈ Ｎａｍｅ",
    "ᏣᎳᎩ ᎦᏬᏂᎯᏍᏗ",  # Cherokee
    "ꙮ",  # multiocular O
    "Ｖ𝕒𝕣𝕚𝕠𝕦𝕤 𝔰𝔠𝔯𝔦𝔭𝔱𝔰",
)

_EMOJI = (
    "🙂",
    "Ada 👩‍💻 Lovelace",  # zero-width joiner sequence
    "👨‍👩‍👧‍👦 Family Trust",
    "🏳️‍🌈 Pride Collective",
    "Café ☕ Ltd",
    "🇬🇧🇺🇸 Transatlantic",  # regional indicator pairs
    "Team 💯 Alpha",
    "🧑🏽‍🚀 Crew",
)

#: Right-to-left, and the bidirectional control characters that make a string
#: render differently from how it is stored.
_RTL_TEXT = (
    "محمد عبد الله",
    "דוד בן־גוריון",
    "علی رضا",
    "شركة الاتحاد للتجارة",
    "‮evil.txt.exe",  # RLO override: renders reversed
    "abc‏def",  # embedded RTL mark
)

#: Whitespace nobody expects to be in a name, and which trims differently in
#: different languages.
_WHITESPACE = (
    " leading space",
    "trailing space ",
    "double  space",
    "tab\tseparated",
    "non breaking space",
    "​zero width",
    "line\nbreak",
)


@dataclass(slots=True)
class EdgeCaseStats:
    """What was made awkward, for the run summary."""

    records_seen: int = 0
    records_marked: int = 0
    values_replaced: int = 0
    #: Cases proposed and then rejected because the field could not hold them.
    rejected: int = 0
    by_category: dict[str, int] = field(default_factory=dict)
    by_field: dict[str, int] = field(default_factory=dict)

    def note(self, category: str, field_name: str) -> None:
        self.by_category[category] = self.by_category.get(category, 0) + 1
        self.by_field[field_name] = self.by_field.get(field_name, 0) + 1

    @property
    def rate(self) -> float:
        return self.records_marked / self.records_seen if self.records_seen else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "records_seen": self.records_seen,
            "records_marked": self.records_marked,
            "values_replaced": self.values_replaced,
            "rejected_as_invalid": self.rejected,
            "rate": round(self.rate, 6),
            "by_category": dict(sorted(self.by_category.items())),
            "by_field": dict(sorted(self.by_field.items())),
        }


def cases_for(spec: FieldSpec, generator: Any = None) -> list[tuple[str, Any]]:
    """Every edge case that could legally go in this field.

    Legality is decided by the field's declared type and constraints, not by
    hope: a length-8 string is not offered an emoji-laden name, and an integer
    with ``max: 120`` is not offered 2^63. What comes back is already a
    candidate; the injector still validates it, because a constraint this
    function does not know about is a constraint it must not violate.

    ``generator`` is consulted for bounds the field did not declare as
    constraints. ``age: {generator: random, min: 18, max: 90}`` puts those
    numbers in the *generator's* options rather than in ``constraints``, so a
    function reading only constraints sees an unbounded integer and cheerfully
    proposes 2^63 as an age. Nothing would reject it - no constraint is
    violated - and the dataset would contain a person nine quadrillion years
    old, which is not an edge case, it is nonsense.
    """
    kind = spec.type
    constraints = spec.constraints

    if kind in (DataType.STRING, DataType.TEXT):
        return _text_cases(spec)
    if kind.is_numeric:
        return _numeric_cases(spec, generator)
    if kind in (DataType.DATE, DataType.DATETIME, DataType.TIME):
        return _temporal_cases(spec)
    if kind is DataType.GEO_POINT:
        return [
            ("extreme_coordinates", value)
            for value in (
                {"lat": 90.0, "lon": 0.0},
                {"lat": -90.0, "lon": 180.0},
                {"lat": 0.0, "lon": -180.0},
                {"lat": 0.0, "lon": 0.0},
                {"lat": -33.8688, "lon": 151.2093},
            )
        ]
    if kind is DataType.EMAIL:
        # Every one of these is a valid address under RFC 5321, and reserved
        # by RFC 2606 (section 62) so none of them can receive mail.
        return [
            ("punctuation_names", value)
            for value in (
                "o'brien-smith@example.com",
                "first.last+tag@example.org",
                "very.long.local.part.that.keeps.going.and.going@example.net",
                "a@example.com",
                "quoted\\ name@example.com",
            )
        ]
    if kind is DataType.PHONE:
        return [("boundary_length", value) for value in ("+1-555-0100", "555-0199")]
    if kind is DataType.DURATION:
        return [
            ("extreme_numbers", timedelta(seconds=0)),
            ("extreme_numbers", timedelta(days=36500)),
        ]
    if constraints.enum:
        # An enum's edges are its ends: first and last, which is where an
        # off-by-one in a mapping table shows up.
        return [
            ("boundary_length", constraints.enum[0]),
            ("boundary_length", constraints.enum[-1]),
        ]
    return []


def _text_cases(spec: FieldSpec) -> list[tuple[str, Any]]:
    constraints = spec.constraints
    low = constraints.min_length or 0
    high = constraints.max_length

    cases: list[tuple[str, Any]] = []

    # Lengths. The empty string only when the field actually permits it: a
    # field with `min_length: 1` given "" is chaos, not an edge case.
    if low == 0:
        cases.append(("boundary_length", ""))
    if low:
        cases.append(("boundary_length", "a" * low))
    if high:
        cases.append(("boundary_length", "W" * high))
        cases.append(("boundary_length", "Wi" * (high // 2) + ("W" if high % 2 else "")))

    for category, catalogue in (
        ("punctuation_names", _PUNCTUATION_NAMES),
        ("unicode_text", _UNICODE_TEXT),
        ("emoji", _EMOJI),
        ("rtl_text", _RTL_TEXT),
        ("whitespace", _WHITESPACE),
    ):
        cases.extend((category, value) for value in catalogue)
    return cases


def _numeric_cases(spec: FieldSpec, generator: Any = None) -> list[tuple[str, Any]]:
    kind = spec.type
    constraints = spec.constraints
    low, high = _numeric_bounds(constraints, generator)

    cases: list[tuple[str, Any]] = []
    if isinstance(low, (int, float)):
        cases.append(("extreme_numbers", _as_type(low, kind)))
        cases.append(("extreme_numbers", _as_type(low + 1, kind)))
    if isinstance(high, (int, float)):
        cases.append(("extreme_numbers", _as_type(high, kind)))
        cases.append(("extreme_numbers", _as_type(high - 1, kind)))

    # Unbounded: the values that break a 32-bit column, a float
    # round-trip, or an assumption that money is positive.
    if low is None and high is None:
        if kind is DataType.INTEGER:
            cases.extend(
                ("extreme_numbers", value)
                for value in (0, -1, 2**31 - 1, 2**31, -(2**31), 2**53, 2**63 - 1, -(2**63))
            )
        else:
            cases.extend(
                ("extreme_numbers", _as_type(value, kind))
                for value in (0, -1, 0.1, 1e-7, 1e15, -1e15)
            )
    elif low is None:
        cases.append(("extreme_numbers", _as_type(0, kind)))
        cases.append(("extreme_numbers", _as_type(-1, kind)))
    return cases


def _numeric_bounds(constraints: Any, generator: Any) -> tuple[Any, Any]:
    """The field's numeric range: declared constraints, else the generator's.

    ``low``/``high`` are what the numeric generators call their bounds; a
    generator without them leaves the range open, which is the only case where
    the 2^63 candidates below are appropriate.
    """
    low, high = constraints.min, constraints.max
    if generator is not None:
        if low is None:
            low = getattr(generator, "low", None)
        if high is None:
            high = getattr(generator, "high", None)
    return low, high


def _temporal_cases(spec: FieldSpec) -> list[tuple[str, Any]]:
    kind = spec.type
    cases: list[tuple[str, Any]] = []

    #: Leap day, the end of a year, the Unix epoch, and the two moments a
    #: naive local-time conversion loses or duplicates an hour.
    moments = (
        datetime(2024, 2, 29, 12, 0, tzinfo=UTC),
        datetime(2000, 2, 29, 0, 0, tzinfo=UTC),
        datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC),
        datetime(1970, 1, 1, 0, 0, tzinfo=UTC),
        # 2026-03-08 02:30 does not exist in US/Eastern, and 2026-11-01 01:30
        # happens twice. Stored as UTC, so the value is unambiguous and it is
        # the consumer's conversion that is under test.
        datetime(2026, 3, 8, 7, 30, tzinfo=UTC),
        datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
    )

    for moment in moments:
        if kind is DataType.DATE:
            cases.append(("temporal_boundaries", moment.date()))
        elif kind is DataType.TIME:
            cases.append(("temporal_boundaries", moment.timetz()))
        else:
            cases.append(("temporal_boundaries", moment))

    if kind is DataType.TIME:
        cases.append(("temporal_boundaries", time(0, 0, 0)))
        cases.append(("temporal_boundaries", time(23, 59, 59)))
    elif kind is DataType.DATE:
        cases.append(("temporal_boundaries", date(2026, 1, 1)))
    return cases


def _as_type(value: float, kind: DataType) -> Any:
    if kind is DataType.INTEGER:
        return int(value)
    if kind is DataType.DECIMAL:
        return Decimal(str(value))
    return float(value)


class EdgeCaseInjector:
    """Replaces a fraction of values with legal but awkward ones (section 79).

    One per entity per run. Every choice comes from the record's index, so a
    bug found once is found again.
    """

    def __init__(
        self,
        entity: CompiledEntity,
        *,
        fraction: float = 0.05,
        seed: int = 0,
        categories: Sequence[str] | None = None,
        fields: Sequence[str] | None = None,
        protected: Sequence[str] = (),
    ) -> None:
        self.entity = entity
        self.fraction = min(1.0, max(0.0, float(fraction)))
        self.seed = seed
        self.wanted = set(categories) if categories else set(CATEGORIES)
        self.stats = EdgeCaseStats()

        #: Never touched. A primary key or a reference replaced by an emoji is
        #: a broken dataset, not a robustness test - the joins stop resolving
        #: and every finding after that is about the fixture.
        self.protected = set(protected)
        self.protected |= {
            compiled.name
            for compiled in entity.fields
            if compiled.spec.primary_key
            or compiled.spec.unique
            or isinstance(getattr(compiled.generator, "target", None), str)
        }
        primary = entity.spec.resolved_primary_key()
        if primary:
            self.protected.add(primary)

        chosen = set(fields) if fields else None
        self.candidates: dict[str, list[tuple[str, Any]]] = {}
        for compiled in entity.fields:
            name = compiled.name
            if name in self.protected or (chosen is not None and name not in chosen):
                continue
            available = [
                (category, value)
                for category, value in cases_for(compiled.spec, compiled.generator)
                if category in self.wanted
            ]
            if available:
                self.candidates[name] = available

    @property
    def is_noop(self) -> bool:
        return self.fraction <= 0.0 or not self.candidates

    def plan(self, index: int) -> tuple[str, str, Any] | None:
        """Which field of record ``index`` gets which case, if any.

        Decided from the index alone, before any value exists, so the answer is
        the same however the record is produced - and a bug found once is found
        again.
        """
        if self.is_noop:
            return None

        draw = mix_seed(self.seed, _SALT, index) & 0xFFFFFFFF
        if draw / 0x100000000 >= self.fraction:
            return None

        names = sorted(self.candidates)
        name = names[(draw >> 8) % len(names)]
        available = self.candidates[name]
        category, value = available[(draw >> 16) % len(available)]
        return name, category, value

    def note_record(self) -> None:
        """Count a record as seen. Called once per record, whatever happens."""
        self.stats.records_seen += 1

    def apply_to_field(
        self, record: GeneratedRecord, index: int, name: str, validator: Any = None
    ) -> bool:
        """Replace ``name`` with its edge case, if this record is due one there.

        Called the moment the field is produced, *before* anything derived from
        it runs. That ordering is the point: an ``email`` templated from a
        first name should read ``o'brien-smith@example.com``, which tests the
        derived field too.

        The alternative - replacing values once the record is complete - was
        tried and produced a colleague whose name was " leading space" and whose
        model-written biography still began "Courtney specializes in...". Two
        fields disagreeing is a broken fixture, not a finding: cross-field
        coherence (section 14) has to survive this mode, or a tester learns
        about Cacophony's bugs instead of their own.

        ``validator`` decides legality. A candidate that would make the record
        invalid is discarded, because an edge case that fails validation is
        chaos wearing the wrong label.
        """
        planned = self.plan(index)
        if planned is None or planned[0] != name:
            return False

        _name, category, value = planned
        original = record.values.get(name)
        record.values[name] = value

        if validator is not None and not self._still_valid(record, name, validator):
            record.values[name] = original
            self.stats.rejected += 1
            return False

        self.stats.records_marked += 1
        self.stats.values_replaced += 1
        self.stats.note(category, name)
        record.damage[EDGE_MARK] = category
        return True

    def _still_valid(self, record: GeneratedRecord, name: str, validator: Any) -> bool:
        """Whether the replaced value satisfies the field that holds it."""
        result = validator.validate_field(name, record.values[name])
        return bool(result.ok)

    def describe(self) -> dict[str, Any]:
        return {
            "entity": self.entity.name,
            "fraction": self.fraction,
            "categories": sorted(self.wanted),
            "fields": sorted(self.candidates),
            **self.stats.to_dict(),
        }
