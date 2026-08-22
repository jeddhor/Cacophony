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
        ``values``      an inline list
        ``path``        a ``.csv``, ``.json``, ``.txt``, ``.parquet`` or
                        ``.db``/``.sqlite`` file
        ``query``       a SELECT against that database, for the SQL source
        ``from_entity`` ``employee.department`` - sample another entity's column
        ``column``      which CSV column / JSON object key / Parquet column
        ``mode``        ``random`` (default) or ``cycle``

    ``cycle`` walks the table in order using ``record_index``, which is useful
    when a lookup table is meant to be exhausted rather than sampled.

    ``from_entity`` is the one source that reads no file: it resolves the other
    entity's value at a position, through the same machinery a reference uses,
    so a hundred-million-row table costs nothing to sample from and the values
    stay consistent with the records that hold them (section 15).
    """

    def prepare(self) -> None:
        self.mode = self.opt_choice("mode", ("random", "cycle"), "random")
        self.column = self.opt_str("column", None, "key", "field")

        values = self.opt("values", None, "items", "list")
        path = self.opt_str("path", None, "file", "source")
        self.query = self.opt_str("query", None, "sql")
        from_entity = self.opt_str("from_entity", None, "entity_column", "sample_from")

        self.from_entity: tuple[str, str] | None = None
        self.values: list[Any] = []

        if from_entity is not None:
            entity_name, _, column = str(from_entity).partition(".")
            if not entity_name or not column:
                raise self._fail(
                    f"'from_entity' names an entity and a column - "
                    f"'employee.department', not {from_entity!r}"
                )
            self.from_entity = (entity_name, column)
        elif values is not None:
            self.values = list(values)
        elif path is not None:
            self.values = _load_table(self, self.resolve_path(path), self.column, self.query)
        else:
            raise self._fail("one of 'values', 'path' or 'from_entity' is required")

        if self.from_entity is None and not self.values:
            raise self._fail("the lookup table is empty")

    def generate_sync(self, context: GenerationContext) -> Any:
        if self.from_entity is not None:
            return self._from_entity(context)
        if self.mode == "cycle":
            return self.values[context.record_index % len(self.values)]
        return self.values[context.rng().randrange(len(self.values))]

    def _from_entity(self, context: GenerationContext) -> Any:
        """One value of another entity's column, resolved by position."""
        assert self.from_entity is not None
        entity_name, column = self.from_entity

        resolver = getattr(context, "resolver", None)
        if resolver is None:
            raise self._fail(
                f"this field samples '{entity_name}.{column}', but no entity resolver is "
                "attached. Generate that entity in the same run."
            )
        try:
            count = resolver.count_of(entity_name)
        except Exception as exc:
            raise self._fail(str(exc)) from exc
        if count <= 0:
            raise self._fail(f"entity '{entity_name}' generates no records to sample")

        index = (
            context.record_index % count if self.mode == "cycle" else context.rng().randrange(count)
        )
        try:
            return resolver.key_at(entity_name, index, column)
        except Exception as exc:
            raise self._fail(str(exc)) from exc

    def describe(self) -> str:
        if self.from_entity is not None:
            return f"lookup({self.from_entity[0]}.{self.from_entity[1]}, {self.mode})"
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


def _load_table(
    generator: OptionsMixin, path: Path, column: str | None, query: str | None = None
) -> list[Any]:
    if not path.exists():
        raise generator._fail(f"lookup file not found: {path}")

    suffix = path.suffix.lower()
    try:
        if suffix == ".parquet":
            return _from_parquet(generator, path, column)

        if suffix in (".db", ".sqlite", ".sqlite3"):
            return _from_sql(generator, path, column, query)

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


def _from_parquet(generator: OptionsMixin, path: Path, column: str | None) -> list[Any]:
    """One column of a Parquet file (section 8's third source).

    Read with the same library that writes Parquet here, so a lookup table can
    be the output of an earlier run without a conversion step.
    """
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - depends on the extra
        raise generator._fail(
            "reading a Parquet lookup table needs pyarrow: pip install 'cacophony[parquet]'"
        ) from exc

    try:
        table = pq.read_table(path, columns=[column] if column else None)
    except Exception as exc:
        raise generator._fail(f"could not read {path}: {exc}") from exc

    if not table.column_names:
        raise generator._fail(f"{path}: the file has no columns")
    chosen = column or table.column_names[0]
    if chosen not in table.column_names:
        available = ", ".join(table.column_names)
        raise generator._fail(f"{path}: no column named '{chosen}'. Columns: {available}")
    return [value for value in table.column(chosen).to_pylist() if value is not None]


def _from_sql(
    generator: OptionsMixin, path: Path, column: str | None, query: str | None
) -> list[Any]:
    """The first column of a query's result (section 8's SQL source).

    A read-only door onto a SQLite file: the query is the caller's, and this is
    their own machine, but the connection is opened read-only so a lookup table
    cannot become a way to write one.
    """
    import sqlite3

    statement = query
    if not statement:
        if not column:
            raise generator._fail("a database lookup needs either 'query' or 'column' plus 'table'")
        table = generator.opt_str("table", None) or path.stem
        statement = f'SELECT "{column}" FROM "{table}"'

    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise generator._fail(f"could not open {path}: {exc}") from exc
    try:
        rows = connection.execute(statement).fetchall()
    except sqlite3.Error as exc:
        raise generator._fail(f"{path}: {exc}") from exc
    finally:
        connection.close()

    # The first column of whatever the query returned: a lookup table is a
    # list of values, and a query that selects five columns has not said which.
    return [row[0] for row in rows if row and row[0] is not None]
