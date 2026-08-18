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
from dataclasses import replace
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
    "PartitionedWriter",
    "align_to_records",
    "count_records",
]

#: How many partition directories one run may hold open before it is asked to
#: reconsider. Each one is a file handle and, for Parquet, a buffered row group;
#: partitioning on a high-cardinality column is the classic way to turn one
#: dataset into a million tiny files.
MAX_OPEN_PARTITIONS = 512


class FileWriter(OutputWriter):
    """Shared behaviour for writers that target a single file."""

    def __init__(
        self,
        path: str | Path,
        *,
        columns: Sequence[str] | None = None,
        provenance: ProvenanceMode = ProvenanceMode.NONE,
        include_assets: bool = True,
        append: bool = False,
        part: int | None = None,
        **options: Any,
    ) -> None:
        self.path = _part_path(Path(path), part)
        #: Continue an existing file rather than truncating it. Only meaningful
        #: for appendable formats; the others take a new part number instead.
        self.append = append and type(self).appendable
        self.part = part
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
    appendable = True

    async def open(self) -> None:
        await super().open()
        try:
            self._handle = self.path.open(
                "a" if self.append else "w", encoding="utf-8", newline="\n"
            )
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
    appendable = True

    async def open(self) -> None:
        await super().open()
        # Appending to a file that already has a header must not write another.
        self._needs_header = not (self.append and self.path.exists() and self.path.stat().st_size)
        try:
            self._handle = self.path.open("a" if self.append else "w", encoding="utf-8", newline="")
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
            if self._needs_header:
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


class PartitionedWriter(OutputWriter):
    """Writes one dataset into a tree of directories keyed by column values.

    ``partition_by: [year, month]`` produces the layout every columnar reader
    already understands::

        out/analytics/employee/year=2026/month=03/employee.parquet

    One child writer per distinct combination, opened when the first record
    needing it arrives and closed with the parent.

    Whether the partition columns also stay *inside* the files depends on what
    the format's readers do with them, which is not a matter of taste:

    * Parquet is a dataset format with a partitioning convention, and its
      readers reconstruct those columns from the directory names. Leaving them
      in the files as well makes ``pyarrow`` refuse the directory outright -
      "Field region has incompatible types: string vs dictionary" - so they are
      dropped.
    * JSON Lines, CSV and JSON have no such convention. Nothing puts the column
      back, so dropping it would lose data every time somebody concatenated the
      files, and it is kept.

    ``drop_partition_columns`` in the profile's options overrides either way.

    Never appendable. A resumed run therefore writes new part files inside each
    partition, which readers accept, and which avoids having to work out how far
    through *each* partition the previous attempt got.
    """

    format = "partitioned"
    extension = ""
    appendable = False

    def __init__(
        self,
        path: str | Path,
        *,
        fmt: str,
        partition_by: Sequence[str],
        entity: str = "records",
        max_partitions: int = MAX_OPEN_PARTITIONS,
        **options: Any,
    ) -> None:
        if not partition_by:
            raise OutputError("a partitioned writer needs at least one column to partition by")
        self.path = Path(path)
        self.fmt = fmt
        self.partition_by = list(partition_by)
        self.entity = entity
        self.max_partitions = max_partitions
        self.drop_partition_columns = bool(
            options.pop("drop_partition_columns", fmt.lower() == "parquet")
        )
        if self.drop_partition_columns and options.get("columns"):
            options["columns"] = [
                column for column in options["columns"] if column not in self.partition_by
            ]
        self.options = options
        self.records_written = 0
        self._children: dict[tuple[str, ...], OutputWriter] = {}

    async def open(self) -> None:
        try:
            self.path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise OutputError(f"could not create {self.path}: {exc}") from exc

    async def write_batch(self, records: Sequence[GeneratedRecord]) -> None:
        if not records:
            return

        grouped: dict[tuple[str, ...], list[GeneratedRecord]] = {}
        for record in records:
            grouped.setdefault(self._key(record), []).append(record)

        for key, group in grouped.items():
            writer = self._children.get(key)
            if writer is None:
                if len(self._children) >= self.max_partitions:
                    raise OutputError(
                        f"partitioning by {', '.join(self.partition_by)} produced more than "
                        f"{self.max_partitions} partitions. Partition on a column with few "
                        "distinct values - a date part rather than a timestamp - or raise "
                        "max_partitions in the output profile's options."
                    )
                writer = await self._open_child(key)
                self._children[key] = writer
            await writer.write_batch([self._shed(record) for record in group])
        self.records_written += len(records)

    async def close(self) -> None:
        for writer in self._children.values():
            await writer.close()
        self._children.clear()

    def describe(self) -> str:
        return f"{self.fmt}:{self.path}/{'/'.join(f'{c}=*' for c in self.partition_by)}"

    # -- internals ---------------------------------------------------------- #

    def _shed(self, record: GeneratedRecord) -> GeneratedRecord:
        """The record as the child writer should see it.

        A copy rather than a mutation: the record belongs to the engine, and a
        writer that emptied a column would be a surprising thing for the next
        stage to meet.
        """
        if not self.drop_partition_columns:
            return record
        return replace(
            record,
            values={
                name: value
                for name, value in record.values.items()
                if name not in self.partition_by
            },
        )

    def _key(self, record: GeneratedRecord) -> tuple[str, ...]:
        return tuple(_partition_value(record.values.get(column)) for column in self.partition_by)

    async def _open_child(self, key: tuple[str, ...]) -> OutputWriter:
        from . import OUTPUT_FORMATS, create_writer

        directory = self.path
        for column, value in zip(self.partition_by, key, strict=True):
            directory = directory / f"{column}={value}"

        writer_class = OUTPUT_FORMATS[self.fmt]
        child = create_writer(
            self.fmt,
            directory / f"{self.entity}{writer_class.extension}",
            **self.options,
        )
        await child.open()
        return child


def _partition_value(value: Any) -> str:
    """A directory-safe rendering of a partition key.

    Dates become their ISO form and everything else its string form, with the
    characters a path cannot carry replaced. A null is ``__null__`` rather than
    an empty directory name, because ``year=/`` is not a path.
    """
    if value is None:
        return "__null__"
    text = value.isoformat() if hasattr(value, "isoformat") else str(value)
    safe = "".join("_" if character in '/\\:*?"<>|' else character for character in text)
    return safe.strip() or "__empty__"


def align_to_records(path: Path, records: int, fmt: str) -> int:
    """Trim a line-oriented file to exactly ``records`` records.

    Resuming appends to what is already on disk, so the two must agree
    precisely. They can disagree in either direction after an unclean stop: the
    store's checkpoint may lag the file, or a buffer lost to a SIGKILL may
    leave the file short of the checkpoint.

    Counting the file is the only answer that is right in both cases, so the
    file wins and the checkpoint is corrected to match. Returns the number of
    records actually present afterwards.
    """
    if not path.exists():
        return 0

    writer_class = _APPENDABLE.get(fmt.lower())
    if writer_class is None:
        # A format with a footer cannot be trimmed; the caller starts a new
        # part instead and keeps whatever the closed file legitimately holds.
        return records

    header_lines = 1 if writer_class is CsvWriter else 0
    kept = 0
    offset = 0
    with path.open("rb") as handle:
        for index, line in enumerate(handle):
            if index < header_lines:
                offset += len(line)
                continue
            if kept >= records:
                break
            # A final line without a newline is a partial write; drop it.
            if not line.endswith(b"\n"):
                break
            offset += len(line)
            kept += 1

    if offset != path.stat().st_size:
        with path.open("r+b") as handle:
            handle.truncate(offset)
    return kept


def count_records(path: Path, fmt: str) -> int | None:
    """How many records a finished file holds, without changing it.

    ``align_to_records`` trims; this only counts, which is what a partitioned
    directory needs - there is nothing to trim there, because a partitioned
    writer is not appendable. Returns None when counting would cost more than
    trusting the checkpoint.
    """
    if not path.exists():
        return 0

    writer_class = _APPENDABLE.get(fmt.lower())
    if writer_class is not None:
        header_lines = 1 if writer_class is CsvWriter else 0
        with path.open("rb") as handle:
            complete = sum(1 for line in handle if line.endswith(b"\n"))
        return max(0, complete - header_lines)

    if fmt.lower() == "parquet":
        try:
            import pyarrow.parquet as pq

            return int(pq.ParquetFile(str(path)).metadata.num_rows)
        except Exception:  # pragma: no cover - a corrupt or unreadable footer
            return None
    return None


def _part_path(path: Path, part: int | None) -> Path:
    """``employee.parquet`` plus part 2 becomes ``employee.part0002.parquet``.

    A run that resumes into a format with a footer cannot continue the file it
    was writing, so it starts a new part instead. Readers for these formats
    accept a directory of parts, so the dataset stays whole.
    """
    if part is None:
        return path
    return path.with_name(f"{path.stem}.part{part:04d}{path.suffix}")


#: Formats whose files can be continued in place.
_APPENDABLE: dict[str, type[FileWriter]] = {
    "jsonl": JsonLinesWriter,
    "ndjson": JsonLinesWriter,
    "csv": CsvWriter,
}


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
