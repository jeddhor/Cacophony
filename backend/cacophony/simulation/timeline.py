"""Temporal simulation (design document section 25).

    Office logins: higher Monday-Friday, concentrated around work hours,
    reduced on holidays, affected by employee timezone.

    Web purchases: potentially evening-heavy, seasonal, promotional spikes.

Drawing a uniform timestamp between two dates is easy and produces data that is
obviously fake the moment anyone plots it: a flat line through nights, weekends
and Christmas. This module builds the *shape* instead - a weight for every hour
of the dataset's period - and draws from that.

How it stays compatible with the rest of Cacophony
--------------------------------------------------
Everything else here is index-addressable: record *n* is a pure function of its
position (section 75), which is what makes runs parallel, resumable and
order-free. Sorting a million timestamps to put an employee's logins in order
would throw all of that away.

So the shape is compiled once into a cumulative distribution over hourly
buckets, and a timestamp is produced by *inverse transform* at a quantile:

    at(0.0) -> the first moment of the period
    at(0.5) -> the moment half the activity has happened
    at(1.0) -> the last moment

Two consequences fall out for free. Drawing at a random quantile gives a
correctly shaped timestamp in O(log n). And drawing at quantile ``k / n`` for
the *k*-th of an entity's *n* events gives a run of timestamps that is already
in chronological order - no sort, no state, no memory, and the same answer
whichever record is generated first.
"""

from __future__ import annotations

import bisect
import datetime as _dt
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..core.errors import SchemaError

if TYPE_CHECKING:  # pragma: no cover - typing only
    import random

__all__ = [
    "SHAPES",
    "Timeline",
    "TimelineShape",
    "parse_moment",
]

#: Relative weight per weekday, Monday first. Office activity collapses at the
#: weekend; retail does not.
_BUSINESS_WEEK = (1.0, 1.05, 1.05, 1.0, 0.9, 0.12, 0.08)
_RETAIL_WEEK = (0.82, 0.8, 0.84, 0.9, 1.05, 1.25, 1.15)
_FLAT_WEEK = (1.0,) * 7

#: Relative weight per hour of the day, midnight first.
_OFFICE_HOURS = (
    0.02,
    0.01,
    0.01,
    0.01,
    0.02,
    0.05,
    0.15,
    0.45,
    0.95,
    1.00,
    1.00,
    0.95,
    0.70,
    0.90,
    1.00,
    0.95,
    0.85,
    0.55,
    0.25,
    0.15,
    0.10,
    0.07,
    0.05,
    0.03,
)
_EVENING = (
    0.15,
    0.08,
    0.05,
    0.04,
    0.04,
    0.06,
    0.12,
    0.25,
    0.40,
    0.50,
    0.55,
    0.60,
    0.65,
    0.60,
    0.58,
    0.60,
    0.70,
    0.85,
    1.00,
    1.00,
    0.95,
    0.80,
    0.55,
    0.30,
)
_FLAT_DAY = (1.0,) * 24


@dataclass(slots=True)
class TimelineShape:
    """How activity is distributed within a period.

    Every weight is *relative*: only the ratios matter, because the curve is
    normalised into a distribution before anything is drawn from it.
    """

    weekdays: tuple[float, ...] = _FLAT_WEEK
    hours: tuple[float, ...] = _FLAT_DAY
    #: Relative weight per calendar month, January first. Seasonality.
    months: tuple[float, ...] = (1.0,) * 12
    #: Dates with no activity at all - public holidays, shutdowns.
    holidays: frozenset[_dt.date] = frozenset()
    #: How much activity a holiday keeps. 0.0 silences it; offices are not
    #: quite silent on a bank holiday, so this is a knob rather than a switch.
    holiday_weight: float = 0.0
    #: ``(start, end, multiplier)`` windows: promotions, incidents, campaigns.
    spikes: tuple[tuple[_dt.date, _dt.date, float], ...] = ()
    #: A steady trend across the period. 2.0 means activity doubles from the
    #: first day to the last, which is what a growing business looks like.
    growth: float = 1.0

    def weight_at(self, moment: _dt.datetime, *, progress: float) -> float:
        """The relative weight of one hour."""
        if moment.date() in self.holidays:
            base = self.holiday_weight
            if base <= 0.0:
                return 0.0
        else:
            base = 1.0

        weight = (
            base
            * self.weekdays[moment.weekday() % 7]
            * self.hours[moment.hour % 24]
            * self.months[moment.month - 1]
        )
        if self.growth != 1.0:
            weight *= 1.0 + (self.growth - 1.0) * progress
        for start, end, multiplier in self.spikes:
            if start <= moment.date() <= end:
                weight *= multiplier
        return weight


#: Named shapes, so a schema can say ``shape: business_hours`` and mean it.
SHAPES: dict[str, TimelineShape] = {
    "flat": TimelineShape(),
    "business_hours": TimelineShape(weekdays=_BUSINESS_WEEK, hours=_OFFICE_HOURS),
    "office": TimelineShape(weekdays=_BUSINESS_WEEK, hours=_OFFICE_HOURS),
    "retail": TimelineShape(weekdays=_RETAIL_WEEK, hours=_EVENING),
    "evening": TimelineShape(hours=_EVENING),
    "always_on": TimelineShape(),
}


def parse_moment(value: Any, *, what: str = "moment") -> _dt.datetime:
    """Read a date or datetime from a schema, or say why it could not."""
    if isinstance(value, _dt.datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, _dt.date):
        return _dt.datetime.combine(value, _dt.time.min)

    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise SchemaError(f"{what} must be an ISO-8601 date or datetime, got {value!r}") from exc
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


class Timeline:
    """A period, and the shape of activity within it.

    Compiled once per project and shared by every field that draws from it.
    The compilation cost is one weight per hour of the period - about 8,800
    floats for a year, 26,000 for three - which is nothing, and it buys O(log n)
    draws for the rest of the run.
    """

    #: Above this many hours the timeline buckets by day instead, so a
    #: ten-year period does not build a 90,000-entry table for no benefit.
    _HOURLY_LIMIT = 24 * 366 * 3

    def __init__(
        self,
        start: _dt.datetime,
        end: _dt.datetime,
        shape: TimelineShape | str | None = None,
    ) -> None:
        if end <= start:
            raise SchemaError(
                f"a timeline ends before it starts: {start.isoformat()} to {end.isoformat()}"
            )

        self.start = start
        self.end = end
        self.shape = _resolve_shape(shape)

        total_hours = int((end - start).total_seconds() // 3600) or 1
        #: Hourly resolution unless the period is long enough that daily will
        #: do; the hour-of-day curve is then folded into the daily weight.
        self.bucket_seconds = 3600 if total_hours <= self._HOURLY_LIMIT else 86_400
        self._cumulative, self._total = self._compile()

    # -- compilation --------------------------------------------------------- #

    def _compile(self) -> tuple[list[float], float]:
        """Accumulate the weight of every bucket across the period."""
        span = (self.end - self.start).total_seconds()
        buckets = max(1, math.ceil(span / self.bucket_seconds))

        cumulative: list[float] = []
        running = 0.0
        for index in range(buckets):
            moment = self.start + _dt.timedelta(seconds=index * self.bucket_seconds)
            progress = index / max(1, buckets - 1)
            if self.bucket_seconds == 3600:
                weight = self.shape.weight_at(moment, progress=progress)
            else:
                # A daily bucket carries the whole day's hourly curve, so a
                # coarse timeline still respects "nothing happens at 3am" once
                # the moment is placed inside the bucket.
                weight = sum(
                    self.shape.weight_at(moment.replace(hour=hour), progress=progress)
                    for hour in range(24)
                )
            running += max(0.0, weight)
            cumulative.append(running)

        if running <= 0.0:
            # Every bucket was silenced - every day a holiday, say. A timeline
            # that can produce no moment is a schema mistake, not a runtime
            # one, but falling back to flat is kinder than raising here and
            # leaves the run to the linter to complain about.
            cumulative = [float(index + 1) for index in range(buckets)]
            running = float(buckets)
        return cumulative, running

    # -- drawing ------------------------------------------------------------- #

    def at(self, quantile: float) -> _dt.datetime:
        """The moment by which ``quantile`` of the period's activity has happened.

        Monotonic in ``quantile``, which is the property that makes an
        entity's events come out in order without sorting them.
        """
        clamped = 0.0 if quantile < 0.0 else (1.0 if quantile > 1.0 else quantile)
        target = clamped * self._total

        index = bisect.bisect_left(self._cumulative, target)
        index = min(index, len(self._cumulative) - 1)

        below = self._cumulative[index - 1] if index else 0.0
        width = self._cumulative[index] - below
        # Where the target falls *inside* the bucket, so a timestamp is not
        # pinned to the top of every hour.
        fraction = (target - below) / width if width > 0 else 0.0

        offset = index * self.bucket_seconds + fraction * self.bucket_seconds
        moment = self.start + _dt.timedelta(seconds=offset)
        return moment if moment <= self.end else self.end

    def sample(self, rng: random.Random) -> _dt.datetime:
        """One correctly shaped moment."""
        return self.at(rng.random())

    def ordered(self, ordinal: int, total: int, *, jitter: float = 0.0) -> _dt.datetime:
        """The ``ordinal``-th of ``total`` events, in order.

        Events are spread across the period's activity rather than its
        duration, so an employee's fortieth login of a hundred lands where the
        fortieth login would - not at 40% of the calendar.

        ``jitter`` in [0, 1) breaks the regularity of an exact division without
        disturbing the order: it moves an event within its own slot and no
        further.
        """
        if total <= 0:
            return self.start
        slot = 1.0 / total
        return self.at((ordinal + min(max(jitter, 0.0), 0.999999)) * slot)

    # -- description ---------------------------------------------------------- #

    @property
    def days(self) -> float:
        return (self.end - self.start).total_seconds() / 86_400

    def describe(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "days": round(self.days, 2),
            "resolution": "hourly" if self.bucket_seconds == 3600 else "daily",
            "buckets": len(self._cumulative),
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Timeline({self.start.date()}..{self.end.date()}, {len(self._cumulative)} buckets)"


def _resolve_shape(shape: TimelineShape | str | None) -> TimelineShape:
    if shape is None:
        return SHAPES["flat"]
    if isinstance(shape, TimelineShape):
        return shape
    known = SHAPES.get(str(shape).lower())
    if known is None:
        raise SchemaError(
            f"unknown timeline shape '{shape}'. Available: {', '.join(sorted(SHAPES))}"
        )
    return known


@dataclass(slots=True)
class ShapeOverrides:
    """Schema-level adjustments to a named shape.

    Kept separate from :class:`TimelineShape` so a project can say "business
    hours, but closed on these dates and busier in December" without restating
    the whole curve.
    """

    holidays: list[str] = field(default_factory=list)
    holiday_weight: float = 0.0
    months: dict[str, float] = field(default_factory=dict)
    spikes: list[dict[str, Any]] = field(default_factory=list)
    growth: float = 1.0

    def apply(self, base: TimelineShape) -> TimelineShape:
        months = list(base.months)
        for key, value in self.months.items():
            index = _month_index(key)
            months[index] = float(value)

        spikes: list[tuple[_dt.date, _dt.date, float]] = list(base.spikes)
        for spike in self.spikes:
            start = parse_moment(spike.get("start"), what="a spike's start").date()
            end = parse_moment(spike.get("end", spike.get("start")), what="a spike's end").date()
            spikes.append((start, end, float(spike.get("multiplier", 2.0))))

        return TimelineShape(
            weekdays=base.weekdays,
            hours=base.hours,
            months=tuple(months),
            holidays=frozenset(parse_moment(day, what="a holiday").date() for day in self.holidays),
            holiday_weight=self.holiday_weight,
            spikes=tuple(spikes),
            growth=self.growth,
        )


_MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)


def _month_index(key: str) -> int:
    text = str(key).strip().lower()
    if text.isdigit():
        number = int(text)
        if 1 <= number <= 12:
            return number - 1
    for index, name in enumerate(_MONTHS):
        if name.startswith(text[:3]):
            return index
    raise SchemaError(f"'{key}' is not a month")
