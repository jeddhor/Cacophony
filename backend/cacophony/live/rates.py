"""Event rates and the clock that enforces them (design document section 35).

    Produce approximately:
    250 authentication events/sec
    50 endpoint events/sec
    8 alerts/minute

Two jobs, and the second is the one that is easy to get wrong.

**Reading a rate.** ``250/s``, ``8/minute``, ``1200/hour``, or a bare number
meaning per second. A stream is configured by people who think in "about two
hundred and fifty a second", not in inter-arrival microseconds.

**Holding a rate.** A naive loop that sleeps ``1/rate`` between records drifts:
every iteration the sleep overshoots slightly, the overshoot is never repaid,
and an hour later the stream is minutes behind and quietly producing fewer
events than it promised. So this is a *token bucket* against a monotonic clock.
Tokens accrue from elapsed time rather than from how long the last sleep
actually took, which means a slow batch is caught up on afterwards rather than
lost, and the long-run rate is the rate that was asked for.

The bucket has a ceiling. A stream that was paused for an hour must not
immediately emit an hour of events - that is a thundering herd, not a catch-up
- so unclaimed tokens stop accumulating after ``burst`` seconds' worth.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from ..core.errors import SchemaError

__all__ = ["Rate", "RateLimiter", "parse_rate"]

#: ``250/s``, ``8 per minute``, ``1200/hour``, ``0.5/s``, ``30``
_RATE = re.compile(
    r"^\s*(?P<count>\d+(?:\.\d+)?)\s*(?:(?:/|\s+per\s+)\s*(?P<unit>[a-z]+))?\s*$",
    re.IGNORECASE,
)

_UNITS: dict[str, float] = {
    "s": 1.0,
    "sec": 1.0,
    "secs": 1.0,
    "second": 1.0,
    "seconds": 1.0,
    "m": 60.0,
    "min": 60.0,
    "mins": 60.0,
    "minute": 60.0,
    "minutes": 60.0,
    "h": 3600.0,
    "hr": 3600.0,
    "hour": 3600.0,
    "hours": 3600.0,
    "d": 86_400.0,
    "day": 86_400.0,
    "days": 86_400.0,
}


@dataclass(frozen=True, slots=True)
class Rate:
    """A number of events per unit of time."""

    per_second: float
    #: How it was written, so the dashboard can echo the user's own words.
    source: str = ""

    def __post_init__(self) -> None:
        if self.per_second < 0:
            raise SchemaError(f"a rate cannot be negative: {self.source or self.per_second}")

    @property
    def interval(self) -> float:
        """Mean seconds between events. Infinite for a stopped stream."""
        return float("inf") if self.per_second <= 0 else 1.0 / self.per_second

    def render(self) -> str:
        if self.source:
            return self.source
        if self.per_second >= 1:
            return f"{self.per_second:g}/s"
        if self.per_second * 60 >= 1:
            return f"{self.per_second * 60:g}/min"
        return f"{self.per_second * 3600:g}/hour"

    def to_dict(self) -> dict[str, Any]:
        return {"per_second": self.per_second, "rate": self.render()}


def parse_rate(value: Any) -> Rate:
    """Read ``250/s``, ``8 per minute`` or ``30`` into a :class:`Rate`."""
    if isinstance(value, Rate):
        return value
    if isinstance(value, (int, float)):
        return Rate(per_second=float(value), source=f"{value:g}/s")

    text = str(value).strip()
    match = _RATE.match(text)
    if match is None:
        raise SchemaError(
            f"could not read '{value}' as a rate. Write it like '250/s', "
            "'8 per minute' or '1200/hour'."
        )

    count = float(match.group("count"))
    unit = (match.group("unit") or "s").lower()
    seconds = _UNITS.get(unit)
    if seconds is None:
        known = ", ".join(sorted({"s", "minute", "hour", "day"}))
        raise SchemaError(f"unknown time unit '{unit}' in '{value}'. Use one of: {known}")
    return Rate(per_second=count / seconds, source=text)


@dataclass(slots=True)
class RateLimiter:
    """A token bucket that holds a long-run rate without drifting.

    Not a sleep loop. Tokens accrue from the monotonic clock, so a batch that
    took longer than its share is caught up on rather than silently dropped,
    and an hour of running produces the number of events an hour was supposed
    to produce.
    """

    rate: Rate
    #: How many seconds' worth of unclaimed tokens may accumulate. A stream
    #: resumed after a long pause should catch up gently, not stampede.
    burst_seconds: float = 1.0

    _tokens: float = field(default=0.0, init=False)
    _last: float = field(default_factory=time.monotonic, init=False)
    #: Events this limiter has permitted, for the dashboard.
    issued: int = field(default=0, init=False)

    @property
    def ceiling(self) -> float:
        return max(1.0, self.rate.per_second * self.burst_seconds)

    def retarget(self, rate: Rate) -> None:
        """Change the rate mid-flight (section 94's adjustable rates).

        Accrued tokens are kept but trimmed to the new ceiling, so slowing a
        stream down takes effect immediately rather than after the backlog
        built at the old rate has drained.
        """
        self.rate = rate
        self._tokens = min(self._tokens, self.ceiling)

    def take(self, wanted: int = 1) -> int:
        """How many of ``wanted`` events may be emitted now. Never blocks."""
        if self.rate.per_second <= 0:
            return 0

        now = time.monotonic()
        self._tokens = min(self.ceiling, self._tokens + (now - self._last) * self.rate.per_second)
        self._last = now

        allowed = min(wanted, int(self._tokens))
        if allowed > 0:
            self._tokens -= allowed
            self.issued += allowed
        return allowed

    def wait_time(self) -> float:
        """Seconds until at least one more event is due."""
        if self.rate.per_second <= 0:
            return 0.05
        if self._tokens >= 1:
            return 0.0
        return (1.0 - self._tokens) / self.rate.per_second

    def to_dict(self) -> dict[str, Any]:
        return {**self.rate.to_dict(), "issued": self.issued}
