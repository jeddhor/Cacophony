"""Output layer (design document sections 33 and 34)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..core.errors import OutputError
from ..core.interfaces import OutputWriter
from .database import SqliteWriter, SqlScriptWriter, align_table, sql_type_for
from .writers import (
    CsvWriter,
    FileWriter,
    JsonLinesWriter,
    JsonWriter,
    ParquetWriter,
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
    "SqlScriptWriter",
    "SqliteWriter",
    "align_file",
    "align_table",
    "align_to_records",
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
    return align_file(Path(path), records, fmt)


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
