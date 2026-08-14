"""Output layer (design document sections 33 and 34)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..core.errors import OutputError
from ..core.interfaces import OutputWriter
from .writers import (
    CsvWriter,
    FileWriter,
    JsonLinesWriter,
    JsonWriter,
    ParquetWriter,
    align_to_records,
)

__all__ = [
    "OUTPUT_FORMATS",
    "CsvWriter",
    "FileWriter",
    "JsonLinesWriter",
    "JsonWriter",
    "ParquetWriter",
    "align_to_records",
    "create_writer",
    "output_path_for",
]

OUTPUT_FORMATS: dict[str, type[FileWriter]] = {
    "csv": CsvWriter,
    "json": JsonWriter,
    "jsonl": JsonLinesWriter,
    "ndjson": JsonLinesWriter,
    "parquet": ParquetWriter,
}
"""Registered output formats. Database, stream and object-store writers register
here in later phases without anything above having to change."""


def create_writer(fmt: str, path: str | Path, **options: Any) -> OutputWriter:
    """Build a writer for ``fmt``, raising a listing error if it is unknown."""
    writer_class = OUTPUT_FORMATS.get(fmt.lower())
    if writer_class is None:
        known = ", ".join(sorted(OUTPUT_FORMATS))
        raise OutputError(f"Unknown output format '{fmt}'. Available formats: {known}")
    return writer_class(path, **options)


def output_path_for(directory: str | Path, entity: str, fmt: str) -> Path:
    """Conventional file path for one entity in one format."""
    writer_class = OUTPUT_FORMATS.get(fmt.lower())
    extension = writer_class.extension if writer_class else f".{fmt}"
    return Path(directory) / f"{entity}{extension}"
