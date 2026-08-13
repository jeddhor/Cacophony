"""Weighted choice and dataset lookup (design document section 8)."""

from __future__ import annotations

import csv
import json
from bisect import bisect_left
from itertools import accumulate
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...core.interfaces import SyncGenerator
from ..registry import register_generator
from .base import OptionsMixin

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ...core.context import GenerationContext

__all__ = ["LookupGenerator", "WeightedGenerator"]


@register_generator("weighted", aliases=("choice", "categorical", "enum"))
class WeightedGenerator(OptionsMixin, SyncGenerator):
    """Choose from a weighted set of values (section 8).

    Weights need not sum to anything in particular; they are normalised. Both
    the mapping and the list forms are accepted::

        choices:
          Windows: 67
          macOS: 18
          Linux: 13
          Other: 2

        choices: [Windows, macOS, Linux]        # equal weights

        choices:
          - {value: Windows, weight: 67}
          - {value: macOS, weight: 18}

    Selection uses a prebuilt cumulative table and a binary search, so cost is
    logarithmic in the number of choices rather than linear - which matters
    when a category has thousands of members and the run has millions of rows.
    """

    def prepare(self) -> None:
        raw = self.opt("choices", None, "values", "options", "weights")
        if raw is None and self.field is not None and self.field.constraints.enum:
            raw = list(self.field.constraints.enum)
        if raw is None:
            raise self._fail("option 'choices' is required")

        values, weights = _normalise_choices(self, raw)
        if not values:
            raise self._fail("'choices' is empty")

        self.values = values
        self.weights = weights
        self._cumulative = list(accumulate(weights))
        self._total = self._cumulative[-1]
        if self._total <= 0:
            raise self._fail("choice weights sum to zero")

    def generate_sync(self, context: GenerationContext) -> Any:
        target = context.rng().random() * self._total
        return self.values[bisect_left(self._cumulative, target)]

    def distribution(self) -> dict[Any, float]:
        """Normalised weights, used by the distribution preview (section 52)."""
        return {
            value: weight / self._total
            for value, weight in zip(self.values, self.weights, strict=True)
        }

    def describe(self) -> str:
        preview = ", ".join(str(value) for value in self.values[:3])
        suffix = f", +{len(self.values) - 3} more" if len(self.values) > 3 else ""
        return f"weighted({preview}{suffix})"


@register_generator("lookup", aliases=("dataset", "from_list"))
class LookupGenerator(OptionsMixin, SyncGenerator):
    """Select a value from a static list or a data file (section 8).

    Options:
        ``values``    an inline list
        ``path``      a ``.csv``, ``.json`` or ``.txt`` file
        ``column``    which CSV column / JSON object key to read
        ``mode``      ``random`` (default) or ``cycle``

    ``cycle`` walks the table in order using ``record_index``, which is useful
    when a lookup table is meant to be exhausted rather than sampled.

    Parquet and SQL sources, and lookups against another entity, arrive with
    the relational phase.
    """

    def prepare(self) -> None:
        self.mode = self.opt_choice("mode", ("random", "cycle"), "random")
        self.column = self.opt_str("column", None, "key", "field")

        values = self.opt("values", None, "items", "list")
        path = self.opt_str("path", None, "file", "source")

        if values is not None:
            self.values = list(values)
        elif path is not None:
            self.values = _load_table(self, Path(path), self.column)
        else:
            raise self._fail("either 'values' or 'path' is required")

        if not self.values:
            raise self._fail("the lookup table is empty")

    def generate_sync(self, context: GenerationContext) -> Any:
        if self.mode == "cycle":
            return self.values[context.record_index % len(self.values)]
        return self.values[context.rng().randrange(len(self.values))]

    def describe(self) -> str:
        return f"lookup({len(self.values)} values, {self.mode})"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _normalise_choices(generator: OptionsMixin, raw: Any) -> tuple[list[Any], list[float]]:
    values: list[Any] = []
    weights: list[float] = []

    if isinstance(raw, dict):
        for value, weight in raw.items():
            values.append(value)
            weights.append(_as_weight(generator, value, weight))
        return values, weights

    if isinstance(raw, (list, tuple)):
        for index, item in enumerate(raw):
            if isinstance(item, dict) and ("value" in item or "weight" in item):
                value = item.get("value")
                weights.append(_as_weight(generator, value, item.get("weight", 1.0)))
                values.append(value)
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                values.append(item[0])
                weights.append(_as_weight(generator, item[0], item[1]))
            elif isinstance(item, dict):
                raise generator._fail(f"choices[{index}] needs a 'value' key")
            else:
                values.append(item)
                weights.append(1.0)
        return values, weights

    raise generator._fail(f"'choices' must be a list or a mapping, got {type(raw).__name__}")


def _as_weight(generator: OptionsMixin, value: Any, weight: Any) -> float:
    try:
        number = float(weight)
    except (TypeError, ValueError) as exc:
        raise generator._fail(f"weight for {value!r} must be numeric, got {weight!r}") from exc
    if number < 0:
        raise generator._fail(f"weight for {value!r} must not be negative")
    return number


def _load_table(generator: OptionsMixin, path: Path, column: str | None) -> list[Any]:
    if not path.exists():
        raise generator._fail(f"lookup file not found: {path}")

    suffix = path.suffix.lower()
    try:
        if suffix == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data = data.get(column) if column else list(data.values())
            if not isinstance(data, list):
                raise generator._fail(f"{path}: expected a JSON array")
            if column and data and isinstance(data[0], dict):
                return [row.get(column) for row in data]
            return list(data)

        if suffix == ".csv":
            with path.open(newline="", encoding="utf-8") as handle:
                if column:
                    reader = csv.DictReader(handle)
                    if reader.fieldnames is None or column not in reader.fieldnames:
                        raise generator._fail(f"{path}: no column named '{column}'")
                    return [row[column] for row in reader]
                return [row[0] for row in csv.reader(handle) if row]

        return [
            line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
    except OSError as exc:
        raise generator._fail(f"could not read lookup file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise generator._fail(f"{path}: invalid JSON - {exc}") from exc
