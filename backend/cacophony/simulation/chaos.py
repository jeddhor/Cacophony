"""Entropy injection, a.k.a. Discord (design document sections 24, 78, 108).

    Clean records:      95%
    Minor defects:       4%
    Severely malformed:  1%

Real data is ugly, and a pipeline tested only on clean data is a pipeline that
has not been tested. So Cacophony can damage its own output on purpose: nulls
where a value was required, misspellings, stray whitespace, mixed date formats,
truncated fields, duplicated records, stale references, outliers.

Two decisions run through all of it.

**Damage is recorded.** Every injected defect is written into the record's
provenance, so a dataset carrying deliberate corruption can still answer "which
of these rows did you break, and how?" Without that, chaos is indistinguishable
from a bug in the generator, which makes it useless for testing the thing it
was meant to test.

**Damage is exempt from validation.** This is the part that is easy to get
wrong. Validation exists to catch generators producing invalid values; chaos
produces invalid values *on purpose*. Running both without telling one about
the other turns a chaos run into a wall of validation failures and, with
``--drop-invalid``, silently discards exactly the records the user asked for.
So an injector marks the fields it damaged and the validator skips them - the
rest of the record is still checked.

Injection happens after generation and before validation, on a copy of nothing:
the record is mutated in place, because a duplicated record and its original
are meant to be indistinguishable except where they differ.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..core.seeds import mix_seed

if TYPE_CHECKING:  # pragma: no cover - typing only
    import random
    from collections.abc import Sequence

    from ..core.record import GeneratedRecord
    from ..schema.models import ChaosSpec

__all__ = ["CHAOS_PRESETS", "DAMAGE_KEY", "ChaosInjector", "ChaosStats"]

#: The column name deliberate damage is reported under in an output that
#: carries it. The record itself keeps damage in ``GeneratedRecord.damage``.
DAMAGE_KEY = "_chaos"

#: Marks a record that *is* a deliberate duplicate rather than one with a
#: damaged field. Not a field name - a record-level fact - so it is prefixed to
#: keep it out of the way of anything a schema could call a column. The
#: uniqueness validator reads it: a duplicated record has a duplicate key by
#: construction, and reporting that would be reporting the feature.
DUPLICATE_MARK = "@duplicate"

#: Distinguishes chaos draws from every other seed derivation (section 75).
_SALT = 0xD15C0DE5

#: Section 78's presets, as fractions of records affected.
CHAOS_PRESETS: dict[str, dict[str, float]] = {
    "pristine": {},
    "realistic": {
        "outliers": 0.005,
        "missing_data": 0.02,
        "duplicates": 0.001,
        "malformed_text": 0.004,
        "unexpected_unicode": 0.001,
        "temporal_anomalies": 0.0005,
    },
    "messy": {
        "outliers": 0.02,
        "missing_data": 0.06,
        "duplicates": 0.01,
        "malformed_text": 0.03,
        "unexpected_unicode": 0.01,
        "temporal_anomalies": 0.005,
        "referential_anomalies": 0.002,
    },
    "hostile_qa": {
        "outliers": 0.05,
        "missing_data": 0.10,
        "duplicates": 0.02,
        "malformed_text": 0.08,
        "unexpected_unicode": 0.05,
        "temporal_anomalies": 0.02,
        "referential_anomalies": 0.01,
    },
    "absolute": {
        "outliers": 0.15,
        "missing_data": 0.20,
        "duplicates": 0.05,
        "malformed_text": 0.20,
        "unexpected_unicode": 0.15,
        "temporal_anomalies": 0.08,
        "referential_anomalies": 0.05,
    },
}

#: Characters that break naive pipelines: combining marks, right-to-left
#: overrides, zero-width joiners, emoji, and a lone surrogate-safe replacement.
_UNICODE_GREMLINS = (
    "\u200b",  # zero-width space
    "\u200f",  # right-to-left mark
    "\u0301",  # combining acute accent
    "\ufeff",  # byte-order mark
    "é",
    "🙂",
    "ß",
    "\u00a0",  # non-breaking space
)

#: Ways to mangle a string without making it unrecognisable.
_TEXT_DEFECTS = (
    "double_space",
    "trailing_space",
    "leading_space",
    "upper",
    "lower",
    "transpose",
    "drop_character",
    "repeat_character",
    "truncate",
    "tab",
)

#: Date formats a badly-integrated system might emit instead of ISO-8601.
_DATE_FORMATS = ("%d/%m/%Y", "%m-%d-%Y", "%Y%m%d", "%d %b %Y", "%c")


@dataclass(slots=True)
class ChaosStats:
    """What was damaged, for the run summary."""

    records_seen: int = 0
    records_damaged: int = 0
    duplicates_emitted: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)

    def note(self, kind: str) -> None:
        self.by_kind[kind] = self.by_kind.get(kind, 0) + 1

    @property
    def damage_rate(self) -> float:
        return self.records_damaged / self.records_seen if self.records_seen else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "records_seen": self.records_seen,
            "records_damaged": self.records_damaged,
            "damage_rate": round(self.damage_rate, 6),
            "duplicates_emitted": self.duplicates_emitted,
            "by_kind": dict(sorted(self.by_kind.items())),
        }


class ChaosInjector:
    """Applies a :class:`~cacophony.schema.models.ChaosSpec` to records.

    One injector per entity per run. Every decision is derived from the
    record's index rather than from a stream, so the same run damages the same
    records whether it is resumed, reordered or parallelised - which is what
    makes a chaotic dataset reproducible, and therefore usable as a fixture.
    """

    def __init__(
        self,
        spec: ChaosSpec,
        *,
        seed: int = 0,
        entity: str = "",
        fields: Sequence[str] = (),
        protected: Sequence[str] = (),
    ) -> None:
        self.rates = _rates(spec)
        self.seed = seed
        self.entity = entity
        self.fields = list(fields)
        #: Never damaged. A primary key that goes missing is not "messy data",
        #: it is a dataset that cannot be loaded - and `duplicates` already
        #: covers duplicate ids deliberately.
        self.protected = set(protected)
        self.stats = ChaosStats()

    @property
    def is_noop(self) -> bool:
        return not self.rates or not any(self.rates.values())

    # -- application ----------------------------------------------------------- #

    def apply(self, record: GeneratedRecord, index: int) -> GeneratedRecord | None:
        """Damage a record in place; return a duplicate of it if one is due.

        The duplicate is returned rather than appended so the caller decides
        where it goes - immediately after, which is what a retried insert looks
        like, or later in the batch, which is what a replayed message looks
        like.
        """
        if self.is_noop:
            return None

        self.stats.records_seen += 1
        damage: dict[str, Any] = {}
        rng = _rng(self.seed, index)

        candidates = [name for name in self.fields if name not in self.protected]
        if candidates:
            self._maybe(rng, "missing_data", record, candidates, damage)
            self._maybe(rng, "malformed_text", record, candidates, damage)
            self._maybe(rng, "unexpected_unicode", record, candidates, damage)
            self._maybe(rng, "outliers", record, candidates, damage)
            self._maybe(rng, "temporal_anomalies", record, candidates, damage)
            self._maybe(rng, "referential_anomalies", record, candidates, damage)

        if damage:
            self.stats.records_damaged += 1
            record.damage.update(damage)
            if record.provenance is not None:
                record.provenance.extra["chaos"] = damage

        if rng.random() < self.rates.get("duplicates", 0.0):
            self.stats.duplicates_emitted += 1
            self.stats.note("duplicates")
            return _clone(record)
        return None

    def _maybe(
        self,
        rng: random.Random,
        kind: str,
        record: GeneratedRecord,
        candidates: list[str],
        damage: dict[str, Any],
    ) -> None:
        rate = self.rates.get(kind, 0.0)
        if rate <= 0.0 or rng.random() >= rate:
            return

        handler = getattr(self, f"_inject_{kind}")
        field_name = rng.choice(candidates)
        before = record.values.get(field_name)
        after = handler(before, rng)
        if after is _UNCHANGED:
            return

        record.values[field_name] = after
        damage[field_name] = kind
        self.stats.note(kind)

    # -- the injectors ---------------------------------------------------------- #

    def _inject_missing_data(self, value: Any, _rng: random.Random) -> Any:
        return None if value is not None else _UNCHANGED

    def _inject_malformed_text(self, value: Any, rng: random.Random) -> Any:
        if not isinstance(value, str) or not value:
            return _UNCHANGED
        defect = rng.choice(_TEXT_DEFECTS)

        if defect == "double_space":
            return value.replace(" ", "  ", 1)
        if defect == "trailing_space":
            return value + " " * rng.randint(1, 3)
        if defect == "leading_space":
            return " " * rng.randint(1, 3) + value
        if defect == "upper":
            return value.upper()
        if defect == "lower":
            return value.lower()
        if defect == "tab":
            return value.replace(" ", "\t", 1)
        if defect == "truncate" and len(value) > 3:
            return value[: max(1, len(value) - rng.randint(1, max(1, len(value) // 3)))]

        position = rng.randrange(len(value))
        if defect == "transpose" and len(value) > 1:
            position = min(position, len(value) - 2)
            return value[:position] + value[position + 1] + value[position] + value[position + 2 :]
        if defect == "drop_character":
            return value[:position] + value[position + 1 :]
        return value[:position] + value[position] + value[position:]

    def _inject_unexpected_unicode(self, value: Any, rng: random.Random) -> Any:
        if not isinstance(value, str) or not value:
            return _UNCHANGED
        gremlin = rng.choice(_UNICODE_GREMLINS)
        position = rng.randrange(len(value) + 1)
        return value[:position] + gremlin + value[position:]

    def _inject_outliers(self, value: Any, rng: random.Random) -> Any:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return _UNCHANGED
        # Orders of magnitude, and occasionally a negative: the shapes that
        # break a chart's axis and a validator's bounds.
        multiplier = rng.choice((1_000.0, 10_000.0, -1.0, -1_000.0, 1e6))
        scaled = value * multiplier if value else multiplier
        return int(scaled) if isinstance(value, int) else scaled

    def _inject_temporal_anomalies(self, value: Any, rng: random.Random) -> Any:
        if isinstance(value, (_dt.datetime, _dt.date)):
            choice = rng.random()
            if choice < 0.4:
                # The future, which breaks "created_at <= now" everywhere.
                shift = _dt.timedelta(days=rng.randint(400, 4000))
                return value + shift
            if choice < 0.7:
                shift = _dt.timedelta(days=rng.randint(4000, 40000))
                return value - shift
            # A different format entirely, as a string.
            moment = (
                value
                if isinstance(value, _dt.datetime)
                else _dt.datetime.combine(value, _dt.time.min)
            )
            return moment.strftime(rng.choice(_DATE_FORMATS))
        if isinstance(value, str) and len(value) >= 10 and value[4] == "-":
            return value.replace("-", "/")
        return _UNCHANGED

    def _inject_referential_anomalies(self, value: Any, rng: random.Random) -> Any:
        """A stale reference: a key that is well formed and points at nothing."""
        if value is None or isinstance(value, (dict, list)):
            return _UNCHANGED
        if isinstance(value, bool):
            return _UNCHANGED
        if isinstance(value, int):
            return value + rng.randint(10**6, 10**7)
        if isinstance(value, str) and value:
            return f"{value}-ORPHAN{rng.randrange(1000):03d}"
        return _UNCHANGED

    # -- description ------------------------------------------------------------- #

    def describe(self) -> dict[str, Any]:
        return {"entity": self.entity, **self.stats.to_dict()}


class _Unchanged:
    """Returned by an injector that had nothing it could damage."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<unchanged>"


_UNCHANGED = _Unchanged()


def _rates(spec: ChaosSpec) -> dict[str, float]:
    """The effective rates: a preset, then anything stated explicitly."""
    rates = dict(CHAOS_PRESETS.get(spec.preset or "pristine", {}))
    for kind in (
        "outliers",
        "missing_data",
        "duplicates",
        "malformed_text",
        "unexpected_unicode",
        "temporal_anomalies",
        "referential_anomalies",
    ):
        value = getattr(spec, kind, 0.0)
        if value > 0.0:
            rates[kind] = float(value)
    return rates


def _rng(seed: int, index: int) -> random.Random:
    import random as _random

    return _random.Random(mix_seed(seed, _SALT, index))


def _clone(record: GeneratedRecord) -> GeneratedRecord:
    from ..core.record import GeneratedRecord as Record

    return Record(
        entity=record.entity,
        id=record.id,
        values=dict(record.values),
        assets=list(record.assets),
        provenance=record.provenance,
        damage={**record.damage, DUPLICATE_MARK: "duplicates"},
    )
