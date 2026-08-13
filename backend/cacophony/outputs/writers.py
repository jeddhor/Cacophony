"""Output writers (design document sections 31, 33 and 34).

Phase one ships the file formats an MVP needs: CSV, JSON, JSON Lines and
Parquet. Databases, object storage, Kafka and HTTP destinations are later
phases; they implement the same three-method interface, so nothing above them
changes.

Every writer is streaming. ``write_batch`` must flush its batch and let it go -
section 31's whole point is that a dataset may be far larger than RAM.

CSV and Parquet both need a column list up front, and the schema knows it, so
the field order is passed in at construction rather than sniffed from the first
record. That also keeps columns in the authored order rather than in
dependency-resolution order, which is what a human expects to see.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from ..core.errors import OutputError
from ..core.interfaces import OutputWriter
from ..core.provenance import ProvenanceMode
from ..core.record import GeneratedRecord, to_jsonable

__all__ = [
    "CsvWriter",
    "FileWriter",
    "JsonLinesWriter",
    "JsonWriter",
    "ParquetWriter",
]


class FileWriter(OutputWriter):
    """Shared behaviour for writers that target a single file."""

    def __init__(
        self,
        path: str | Path,
        *,
        columns: Sequence[str] | None = None,
        provenance: ProvenanceMode = ProvenanceMode.NONE,
        include_assets: bool = True,
        **options: Any,
    ) -> None:
        self.path = Path(path)
        self.columns = list(columns) if columns else None
        self.provenance = provenance
        self.include_assets = include_assets
        self.options = options
        self.records_written = 0
        self._handle: Any = None

    def _row(self, record: GeneratedRecord, *, jsonable: bool = True) -> dict[str, Any]:
        return record.to_dict(
            provenance_mode=self.provenance,
            include_assets=self.include_assets,
            jsonable=jsonable,
        )

    async def open(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise OutputError(f"could not create {self.path.parent}: {exc}") from exc

    async def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def describe(self) -> str:
        return f"{self.format}:{self.path}"


class JsonLinesWriter(FileWriter):
    """One JSON object per line - the default for large datasets."""

    format = "jsonl"
    extension = ".jsonl"

    async def open(self) -> None:
        await super().open()
        try:
            self._handle = self.path.open("w", encoding="utf-8", newline="\n")
        except OSError as exc:
            raise OutputError(f"could not open {self.path} for writing: {exc}") from exc

    async def write_batch(self, records: Sequence[GeneratedRecord]) -> None:
        if self._handle is None:
            raise OutputError("writer is not open")
        # One join and one write per batch rather than per record: syscall
        # count is what dominates throughput here.
        self._handle.write(
            "".join(
                json.dumps(self._row(record), ensure_ascii=False, default=str) + "\n"
                for record in records
            )
        )
        self.records_written += len(records)


class JsonWriter(FileWriter):
    """A single JSON array.

    Unlike the other writers this one is not streaming-friendly by nature - a
    JSON array has to be closed. It writes incrementally and only holds one
    batch, but a consumer still has to parse the whole file, so JSON Lines is
    the better choice above a few hundred thousand records.
    """

    format = "json"
    extension = ".json"

    async def open(self) -> None:
        await super().open()
        try:
            self._handle = self.path.open("w", encoding="utf-8")
        except OSError as exc:
            raise OutputError(f"could not open {self.path} for writing: {exc}") from exc
        self._handle.write("[\n")
        self._first = True

    async def write_batch(self, records: Sequence[GeneratedRecord]) -> None:
        if self._handle is None:
            raise OutputError("writer is not open")
        indent = self.options.get("indent", 2)
        for record in records:
            if not self._first:
                self._handle.write(",\n")
            self._first = False
            self._handle.write(
                json.dumps(self._row(record), ensure_ascii=False, indent=indent, default=str)
            )
        self.records_written += len(records)

    async def close(self) -> None:
        if self._handle is not None:
            self._handle.write("\n]\n")
        await super().close()


class CsvWriter(FileWriter):
    """Comma-separated values.

    Nested values (objects, arrays, assets, provenance) have no native CSV
    representation, so they are written as compact JSON inside the cell. That
    is lossy for a consumer that does not expect it, which is one reason
    Parquet is the better default for anything analytical.
    """

    format = "csv"
    extension = ".csv"

    async def open(self) -> None:
        await super().open()
        try:
            self._handle = self.path.open("w", encoding="utf-8", newline="")
        except OSError as exc:
            raise OutputError(f"could not open {self.path} for writing: {exc}") from exc
        self._writer: Any = None
        self._delimiter = self.options.get("delimiter", ",")

    async def write_batch(self, records: Sequence[GeneratedRecord]) -> None:
        if self._handle is None:
            raise OutputError("writer is not open")
        if not records:
            return

        if self._writer is None:
            columns = self.columns or list(self._row(records[0]).keys())
            self._writer = csv.DictWriter(
                self._handle,
                fieldnames=columns,
                delimiter=self._delimiter,
                extrasaction="ignore",
                lineterminator="\n",
            )
            self._writer.writeheader()

        self._writer.writerows(_flatten_for_csv(self._row(record)) for record in records)
        self.records_written += len(records)


class ParquetWriter(FileWriter):
    """Apache Parquet, written incrementally with PyArrow.

    Row groups are the unit of streaming: each batch becomes one row group, so
    memory stays bounded while the file remains a single valid Parquet dataset.

    The schema is inferred from the first batch. If a later batch disagrees -
    a column that was all nulls in batch one and integers in batch two -
    PyArrow will refuse the write, and the resulting error names the column.
    """

    format = "parquet"
    extension = ".parquet"

    async def open(self) -> None:
        await super().open()
        try:
            import pyarrow  # noqa: F401
        except ImportError as exc:  # pragma: no cover - depends on the install
            raise OutputError(
                "Parquet output requires PyArrow. Install it with: pip install 'cacophony[parquet]'"
            ) from exc
        self._writer: Any = None
        self._arrow_schema: Any = None

    async def write_batch(self, records: Sequence[GeneratedRecord]) -> None:
        if not records:
            return

        import pyarrow as pa
        import pyarrow.parquet as pq

        # Native Python values, not JSON-ified ones: Parquet has real timestamp
        # and decimal types and it would be a waste to hand it strings.
        rows = [self._row(record, jsonable=False) for record in records]
        columns = self.columns or list(rows[0].keys())
        table_data = {
            column: [_parquet_value(row.get(column)) for row in rows] for column in columns
        }

        try:
            table = pa.table(table_data, schema=self._arrow_schema)
        except (pa.ArrowInvalid, pa.ArrowTypeError) as exc:
            raise OutputError(f"could not build a Parquet batch for {self.path}: {exc}") from exc

        if self._writer is None:
            self._arrow_schema = table.schema
            self._writer = pq.ParquetWriter(
                str(self.path),
                table.schema,
                compression=self.options.get("compression", "snappy"),
            )
        self._writer.write_table(table)
        self.records_written += len(records)

    async def close(self) -> None:
        if getattr(self, "_writer", None) is not None:
            self._writer.close()
            self._writer = None
        await super().close()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _flatten_for_csv(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: json.dumps(value, ensure_ascii=False, default=str)
        if isinstance(value, (dict, list))
        else value
        for key, value in row.items()
    }


def _parquet_value(value: Any) -> Any:
    """Render a value in a form PyArrow can infer a column type from.

    Dates, datetimes, decimals and bytes pass through as themselves so they
    land in native Parquet columns. Structured values become JSON strings -
    inferring a nested Arrow type from synthetic data that may legitimately
    vary in shape between batches causes more problems than it solves.
    """
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(to_jsonable(value), ensure_ascii=False, default=str)
    if isinstance(value, (UUID, Path)):
        return str(value)
    if isinstance(value, timedelta):
        return value.total_seconds()
    return value
