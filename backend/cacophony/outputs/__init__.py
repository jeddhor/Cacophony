"""Output layer (design document sections 33 and 34)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..core.errors import OutputError
from ..core.interfaces import OutputWriter
from .database import SqliteWriter, SqlScriptWriter, align_table, sql_type_for
from .writers import (
    MAX_OPEN_PARTITIONS,
    CsvWriter,
    FileWriter,
    JsonLinesWriter,
    JsonWriter,
    ParquetWriter,
    PartitionedWriter,
    count_records,
)
from .writers import align_to_records as align_file

__all__ = [
    "OUTPUT_FORMATS",
    "SINGLE_FILE_FORMATS",
    "CsvWriter",
    "FileWriter",
    "JsonLinesWriter",
    "JsonWriter",
    "ParquetWriter",
    "PartitionedWriter",
    "SqlScriptWriter",
    "SqliteWriter",
    "align_file",
    "align_table",
    "align_to_records",
    "count_records",
    "create_writer",
    "output_path_for",
    "sql_type_for",
]

OUTPUT_FORMATS: dict[str, type[FileWriter]] = {
    "csv": CsvWriter,
    "json": JsonWriter,
    "jsonl": JsonLinesWriter,
    "ndjson": JsonLinesWriter,
    "parquet": ParquetWriter,
    "sqlite": SqliteWriter,
    "sql": SqlScriptWriter,
}
"""Registered output formats. Stream and object-store writers register here in
later phases without anything above having to change."""

SINGLE_FILE_FORMATS = frozenset({"sqlite"})
"""Formats where every entity writes into one destination.

A relational output split across three files is not a relational output: the
point of a SQLite database is that its foreign keys resolve, which they cannot
do if each table lives in a separate file.
"""


def create_writer(fmt: str, path: str | Path, **options: Any) -> OutputWriter:
    """Build a writer for ``fmt``, raising a listing error if it is unknown."""
    writer_class = OUTPUT_FORMATS.get(fmt.lower())
    if writer_class is None:
        known = ", ".join(sorted(OUTPUT_FORMATS))
        raise OutputError(f"Unknown output format '{fmt}'. Available formats: {known}")

    partition_by = [str(column) for column in (options.pop("partition_by", None) or [])]
    if partition_by:
        if fmt.lower() in SINGLE_FILE_FORMATS:
            raise OutputError(
                f"'{fmt}' writes one destination for the whole project, so there is nothing "
                "to partition. Partition a file format, or drop 'partition_by'."
            )
        entity_spec = options.pop("entity", None)
        options.pop("entities", None)
        options.pop("chaos", None)
        return PartitionedWriter(
            path,
            fmt=fmt.lower(),
            partition_by=partition_by,
            entity=getattr(entity_spec, "name", None) or Path(path).stem,
            max_partitions=int(options.pop("max_partitions", MAX_OPEN_PARTITIONS)),
            **options,
        )

    # Only the database writers know what to do with a compiled entity, or
    # care that the run is injecting damage; the rest would reject the keyword.
    if not issubclass(writer_class, (SqliteWriter, SqlScriptWriter)):
        options.pop("entity", None)
        options.pop("entities", None)
        options.pop("chaos", None)
    return writer_class(path, **options)


def align_to_records(path: str | Path, records: int, fmt: str, *, table: str | None = None) -> int:
    """Reconcile what is on disk with what a checkpoint claims.

    Format dispatch lives here rather than in either writer module, because
    "how do I count what has already been written" is a property of the format
    in the same way that "which class writes it" is.
    """
    if fmt.lower() == "sqlite":
        return align_table(path, records, table or Path(path).stem)
    target = Path(path)
    if target.is_dir():
        # A partitioned run: the records are spread over one file per partition,
        # so what is on disk is their sum. Nothing is trimmed - a partitioned
        # writer is not appendable, so a resume writes new parts rather than
        # continuing these.
        extension = OUTPUT_FORMATS[fmt.lower()].extension
        counted = [
            count_records(child, fmt)
            for child in sorted(target.rglob(f"*{extension}"))
            if child.is_file()
        ]
        if any(count is None for count in counted):
            return records
        return sum(count for count in counted if count is not None)
    return align_file(target, records, fmt)


def output_path_for(
    directory: str | Path,
    entity: str,
    fmt: str,
    *,
    database_name: str = "cacophony",
) -> Path:
    """Conventional file path for one entity in one format."""
    writer_class = OUTPUT_FORMATS.get(fmt.lower())
    extension = writer_class.extension if writer_class else f".{fmt}"
    if fmt.lower() in SINGLE_FILE_FORMATS:
        return Path(directory) / f"{database_name}{extension}"
    return Path(directory) / f"{entity}{extension}"
