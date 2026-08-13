"""Random and statistically distributed values (design document section 8)."""

from __future__ import annotations

import math
import string
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from ...core.interfaces import SyncGenerator
from ...core.types import DataType
from ..registry import register_generator
from .base import OptionsMixin

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ...core.context import GenerationContext

__all__ = ["BooleanGenerator", "DistributionGenerator", "RandomGenerator"]

_ALPHABETS = {
    "alpha": string.ascii_letters,
    "alphanumeric": string.ascii_letters + string.digits,
    "lower": string.ascii_lowercase,
    "upper": string.ascii_uppercase,
    "digits": string.digits,
    "hex": "0123456789abcdef",
}


@register_generator("random", aliases=("rand",))
class RandomGenerator(OptionsMixin, SyncGenerator):
    """Random values within the field's constraints.

    The field's declared type decides what "random" means: an ``integer`` field
    gets an integer between ``min`` and ``max``, a ``string`` field gets random
    characters of a given ``length``, a ``boolean`` field gets a coin flip.

    Options:
        ``min`` / ``max``       numeric or length bounds
        ``precision``           decimal places for float and decimal fields
        ``length``              exact string length (or use ``min``/``max``)
        ``charset``             ``alpha``, ``alphanumeric``, ``lower``, ``upper``,
                                ``digits``, ``hex``, or a literal set of characters
    """

    def prepare(self) -> None:
        self.data_type = self.field.type if self.field is not None else DataType.INTEGER
        constraints = self.field.constraints if self.field is not None else None

        raw_min = self.opt("min", None, "low", "minimum")
        raw_max = self.opt("max", None, "high", "maximum")
        if raw_min is None and constraints is not None:
            raw_min = constraints.min
        if raw_max is None and constraints is not None:
            raw_max = constraints.max

        self.precision = self.opt_int("precision", None) or (
            constraints.precision if constraints is not None else None
        )

        # A temporal field asking for "random" means a random moment in a
        # window, so hand the work to the generator that already knows how.
        self._delegate: SyncGenerator | None = None
        if self.data_type.is_temporal:
            from .temporal import TimestampGenerator

            self._delegate = TimestampGenerator(self.options, field=self.field, entity=self.entity)
        elif self.data_type.is_textual:
            self._prepare_string(constraints)
        else:
            self._prepare_numeric(raw_min, raw_max)

    def _prepare_numeric(self, raw_min: Any, raw_max: Any) -> None:
        default_low, default_high = (0, 100) if self.data_type is DataType.INTEGER else (0.0, 1.0)
        self.low = _as_number(self, "min", raw_min, default_low)
        self.high = _as_number(self, "max", raw_max, default_high)
        if self.low > self.high:
            raise self._fail(f"min ({self.low}) is greater than max ({self.high})")

    def _prepare_string(self, constraints: Any) -> None:
        length = self.opt_int("length", None)
        self.min_length = self.opt_int("min_length", length) or (
            (constraints.min_length if constraints is not None else None) or 8
        )
        self.max_length = self.opt_int("max_length", length) or (
            (constraints.max_length if constraints is not None else None) or self.min_length
        )
        if self.min_length > self.max_length:
            raise self._fail(
                f"min_length ({self.min_length}) is greater than max_length ({self.max_length})"
            )
        charset = self.opt_str("charset", "alphanumeric") or "alphanumeric"
        self.alphabet = _ALPHABETS.get(charset, charset)
        if not self.alphabet:
            raise self._fail("option 'charset' resolved to an empty alphabet")

    def generate_sync(self, context: GenerationContext) -> Any:
        if self._delegate is not None:
            return self._delegate.generate_sync(context)

        rng = context.rng()
        data_type = self.data_type

        if data_type is DataType.BOOLEAN:
            return rng.random() < 0.5
        if data_type is DataType.INTEGER:
            return rng.randint(int(self.low), int(self.high))
        if data_type is DataType.DECIMAL:
            value = rng.uniform(float(self.low), float(self.high))
            places = self.precision if self.precision is not None else 2
            return Decimal(f"{value:.{places}f}")
        if data_type is DataType.FLOAT:
            value = rng.uniform(float(self.low), float(self.high))
            return round(value, self.precision) if self.precision is not None else value
        if data_type.is_textual:
            length = rng.randint(self.min_length, self.max_length)
            return "".join(rng.choice(self.alphabet) for _ in range(length))

        # Anything else (JSON, object, custom) gets a plain uniform float.
        return rng.uniform(float(getattr(self, "low", 0.0)), float(getattr(self, "high", 1.0)))

    def describe(self) -> str:
        if self._delegate is not None:
            return self._delegate.describe()
        if self.data_type.is_textual:
            return f"random(str {self.min_length}-{self.max_length})"
        if self.data_type is DataType.BOOLEAN:
            return "random(bool)"
        if self.data_type is DataType.INTEGER:
            return f"random({int(self.low)}..{int(self.high)})"
        return f"random({self.low}..{self.high})"


@register_generator("boolean", aliases=("bool", "flag"))
class BooleanGenerator(OptionsMixin, SyncGenerator):
    """A weighted coin flip.

    Options:
        ``probability``  chance of ``true`` (default ``0.5``)
    """

    def prepare(self) -> None:
        self.probability = self.opt_float("probability", 0.5, "p", "true_probability") or 0.0
        if not 0.0 <= self.probability <= 1.0:
            raise self._fail("option 'probability' must be between 0.0 and 1.0")

    def generate_sync(self, context: GenerationContext) -> Any:
        return context.rng().random() < self.probability

    def describe(self) -> str:
        return f"boolean(p={self.probability})"


@register_generator("distribution", aliases=("dist", "statistical"))
class DistributionGenerator(OptionsMixin, SyncGenerator):
    """Draw from a statistical distribution (section 8).

    Supported: ``uniform``, ``normal``, ``lognormal``, ``exponential``,
    ``poisson``, ``beta`` and ``histogram``.

    Options vary by distribution::

        distribution: normal        mean, stddev
        distribution: lognormal     mean, sigma
        distribution: exponential   rate (or scale)
        distribution: poisson       lam
        distribution: beta          alpha, beta, min, max
        distribution: uniform       min, max
        distribution: histogram     bins: [{value|range, weight}, ...]

    ``min``/``max`` clamp the result for every distribution, which keeps a
    normal draw for ``age`` from producing a negative number.

    NumPy is not used here. These are single scalar draws on the hot path, and
    ``random`` is markedly faster per call than constructing a NumPy generator
    per field per record.
    """

    _SUPPORTED = (
        "uniform",
        "normal",
        "gaussian",
        "lognormal",
        "exponential",
        "poisson",
        "beta",
        "histogram",
    )

    def prepare(self) -> None:
        self.kind = self.opt_choice("distribution", self._SUPPORTED, "uniform")
        if self.kind == "gaussian":
            self.kind = "normal"

        self.low = self.opt_float("min", None, "low")
        self.high = self.opt_float("max", None, "high")
        self.mean = self.opt_float("mean", 0.0, "mu") or 0.0
        self.stddev = self.opt_float("stddev", 1.0, "sigma", "std") or 1.0
        self.rate = self.opt_float("rate", None, "lambda")
        self.scale = self.opt_float("scale", None)
        self.lam = self.opt_float("lam", None, "lambda", "mean")
        self.alpha = self.opt_float("alpha", 2.0, "a") or 2.0
        self.beta = self.opt_float("beta", 2.0, "b") or 2.0
        self.bins = self.opt_list("bins", [])

        if self.kind == "normal" and self.stddev <= 0:
            raise self._fail("option 'stddev' must be positive")
        if self.kind == "histogram" and not self.bins:
            raise self._fail("histogram distribution requires a non-empty 'bins' list")
        if self.kind == "histogram":
            self._histogram = _parse_histogram(self, self.bins)
        if self.kind == "exponential":
            if self.scale is None:
                self.scale = 1.0 / self.rate if self.rate else 1.0
            if self.scale <= 0:
                raise self._fail("exponential 'scale' must be positive")
        if self.kind == "beta" and (self.alpha <= 0 or self.beta <= 0):
            raise self._fail("beta 'alpha' and 'beta' must be positive")

        self.is_integer = (
            self.field is not None and self.field.type is DataType.INTEGER
        ) or self.kind == "poisson"
        self.precision = self.opt_int("precision", None)

    def generate_sync(self, context: GenerationContext) -> Any:
        rng = context.rng()
        value = self._draw(rng)

        if self.low is not None:
            value = max(value, self.low)
        if self.high is not None:
            value = min(value, self.high)
        if self.is_integer:
            return round(value)
        if self.precision is not None:
            return round(value, self.precision)
        return value

    def _draw(self, rng: Any) -> float:
        kind = self.kind
        if kind == "uniform":
            return rng.uniform(
                self.low if self.low is not None else 0.0,
                self.high if self.high is not None else 1.0,
            )
        if kind == "normal":
            return rng.gauss(self.mean, self.stddev)
        if kind == "lognormal":
            return rng.lognormvariate(self.mean, self.stddev)
        if kind == "exponential":
            return rng.expovariate(1.0 / float(self.scale or 1.0))
        if kind == "poisson":
            return _poisson(rng, float(self.lam if self.lam is not None else 1.0))
        if kind == "beta":
            drawn = rng.betavariate(self.alpha, self.beta)
            low = self.low if self.low is not None else 0.0
            high = self.high if self.high is not None else 1.0
            return low + drawn * (high - low)
        return _draw_histogram(rng, self._histogram)

    def describe(self) -> str:
        return f"distribution({self.kind})"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _as_number(generator: OptionsMixin, key: str, value: Any, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise generator._fail(f"option '{key}' must be numeric, got {value!r}") from exc


def _poisson(rng: Any, lam: float) -> float:
    """Knuth's algorithm - adequate for the small lambdas synthetic data uses."""
    if lam <= 0:
        return 0.0
    if lam > 500:  # avoid underflow; the normal approximation is fine this far out
        return max(0.0, rng.gauss(lam, math.sqrt(lam)))
    threshold = math.exp(-lam)
    count, product = 0, 1.0
    while True:
        product *= rng.random()
        if product <= threshold:
            return float(count)
        count += 1


def _parse_histogram(generator: OptionsMixin, bins: list[Any]) -> list[tuple[float, float, float]]:
    """Normalise histogram bins into ``(low, high, cumulative_weight)`` triples."""
    parsed: list[tuple[float, float, float]] = []
    total = 0.0
    for index, item in enumerate(bins):
        if not isinstance(item, dict):
            raise generator._fail(f"bins[{index}] must be a mapping")
        weight = float(item.get("weight") or item.get("percent") or 1.0)
        if weight < 0:
            raise generator._fail(f"bins[{index}] weight must not be negative")
        if "range" in item:
            bounds = item["range"]
            if not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
                raise generator._fail(f"bins[{index}].range must be a two-element list")
            low, high = float(bounds[0]), float(bounds[1])
        else:
            if "value" not in item:
                raise generator._fail(f"bins[{index}] needs either 'value' or 'range'")
            low = high = float(item["value"])
        total += weight
        parsed.append((low, high, total))
    if total <= 0:
        raise generator._fail("histogram bin weights sum to zero")
    return [(low, high, cumulative / total) for low, high, cumulative in parsed]


def _draw_histogram(rng: Any, histogram: list[tuple[float, float, float]]) -> float:
    target = rng.random()
    for low, high, cumulative in histogram:
        if target <= cumulative:
            return low if low == high else rng.uniform(low, high)
    low, high, _ = histogram[-1]
    return low if low == high else rng.uniform(low, high)
