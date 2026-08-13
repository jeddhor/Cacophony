"""Date and time generators.

Phase one covers the flat case: a value drawn from a window, optionally
business-hours weighted. The full temporal distribution engine of section 25 -
holidays, seasonality, per-employee timezones, promotional spikes - builds on
this interface rather than replacing it.
"""

from __future__ import annotations

import datetime as _dt
from typing import TYPE_CHECKING, Any

from ...core.interfaces import SyncGenerator
from ...core.types import DataType
from ..registry import register_generator
from .base import OptionsMixin

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ...core.context import GenerationContext

__all__ = ["TimestampGenerator"]

_SECONDS_PER_DAY = 86_400

#: Hour-of-day weights for ``business_hours: true``. Deliberately coarse -
#: section 25's proper temporal engine will supersede this.
_BUSINESS_HOUR_WEIGHTS: tuple[float, ...] = (
    0.2,
    0.1,
    0.1,
    0.1,
    0.2,
    0.6,  # 00-05
    1.5,
    3.0,
    6.0,
    8.0,
    8.5,
    8.0,  # 06-11
    6.0,
    7.5,
    8.0,
    8.0,
    7.0,
    5.0,  # 12-17
    3.0,
    2.0,
    1.5,
    1.0,
    0.7,
    0.4,  # 18-23
)


@register_generator("datetime", aliases=("date", "time", "timestamp", "temporal"))
class TimestampGenerator(OptionsMixin, SyncGenerator):
    """A date, time or datetime drawn from a window.

    Options:
        ``start`` / ``end``     ISO-8601 bounds (default: the last 365 days)
        ``business_hours``      weight the hour of day towards office hours
        ``weekdays_only``       resample until the date falls Monday-Friday
        ``timezone_offset``     hours to add to the drawn value

    The returned Python type follows the field's declared type, so the same
    generator serves ``date``, ``time`` and ``datetime`` fields.
    """

    def prepare(self) -> None:
        self.data_type = self.field.type if self.field is not None else DataType.DATETIME

        end_default = _dt.datetime.now().replace(microsecond=0)
        start_default = end_default - _dt.timedelta(days=365)

        self.start = self._parse_bound("start", start_default, "from", "after")
        self.end = self._parse_bound("end", end_default, "to", "before")
        if self.start > self.end:
            raise self._fail(
                f"start ({self.start.isoformat()}) is after end ({self.end.isoformat()})"
            )

        self.business_hours = self.opt_bool("business_hours", False, "office_hours")
        self.weekdays_only = self.opt_bool("weekdays_only", False, "business_days")
        self.timezone_offset = self.opt_float("timezone_offset", 0.0, "tz_offset") or 0.0
        self._span = max(int((self.end - self.start).total_seconds()), 0)

    def _parse_bound(self, key: str, default: _dt.datetime, *aliases: str) -> _dt.datetime:
        raw = self.opt(key, None, *aliases)
        if raw is None:
            return default
        if isinstance(raw, _dt.datetime):
            return raw
        if isinstance(raw, _dt.date):
            return _dt.datetime.combine(raw, _dt.time.min)
        text = str(raw).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = _dt.datetime.fromisoformat(text)
        except ValueError as exc:
            raise self._fail(
                f"option '{key}' must be an ISO-8601 date or datetime, got {raw!r}"
            ) from exc
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed

    def generate_sync(self, context: GenerationContext) -> Any:
        rng = context.rng()
        moment = self._draw(rng)

        if self.timezone_offset:
            moment += _dt.timedelta(hours=self.timezone_offset)

        if self.data_type is DataType.DATE:
            return moment.date()
        if self.data_type is DataType.TIME:
            return moment.time()
        if self.data_type.is_textual:
            return moment.isoformat()
        return moment

    def _draw(self, rng: Any) -> _dt.datetime:
        # Bounded resampling: never loop forever hunting for a weekday.
        for _ in range(12):
            moment = self.start + _dt.timedelta(seconds=rng.uniform(0, self._span))
            if self.business_hours:
                moment = moment.replace(
                    hour=_weighted_hour(rng),
                    minute=rng.randrange(60),
                    second=rng.randrange(60),
                )
            if not self.weekdays_only or moment.weekday() < 5:
                return moment
        # Give up on the weekday preference rather than distort the window.
        return moment

    def describe(self) -> str:
        window = f"{self.start.date().isoformat()}..{self.end.date().isoformat()}"
        return f"datetime({window})" + (" business-hours" if self.business_hours else "")


def _weighted_hour(rng: Any) -> int:
    target = rng.uniform(0, sum(_BUSINESS_HOUR_WEIGHTS))
    running = 0.0
    for hour, weight in enumerate(_BUSINESS_HOUR_WEIGHTS):
        running += weight
        if target <= running:
            return hour
    return 23
