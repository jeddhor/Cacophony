"""Output layer (design document sections 33 and 34)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.errors import OutputError
from ..core.interfaces import OutputWriter
from .database import SqliteWriter, SqlScriptWriter, align_table, sql_type_for
from .remote import ElasticsearchWriter, ObjectStoreWriter
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

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

__all__ = [
    "OUTPUT_FORMATS",
    "REQUIRED_OPTIONS",
    "SINGLE_FILE_FORMATS",
    "CsvWriter",
    "ElasticsearchWriter",
    "FileWriter",
    "JsonLinesWriter",
    "JsonWriter",
    "ObjectStoreWriter",
    "ParquetWriter",
    "PartitionedWriter",
    "SqlScriptWriter",
    "SqliteWriter",
    "align_file",
    "align_table",
    "align_to_records",
    "count_records",
    "create_writer",
    "describe_formats",
    "output_path_for",
    "sql_type_for",
]

# `OutputWriter` rather than `FileWriter`: two of these do not write a file,
# which is section 33's whole point and the reason `remote.py` exists.
OUTPUT_FORMATS: dict[str, type[OutputWriter]] = {
    "csv": CsvWriter,
    "json": JsonWriter,
    "jsonl": JsonLinesWriter,
    "ndjson": JsonLinesWriter,
    "parquet": ParquetWriter,
    "sqlite": SqliteWriter,
    "sql": SqlScriptWriter,
    # Section 33's "Later" destinations. Not files, and the module says how
    # each one deals with that.
    "elasticsearch": ElasticsearchWriter,
    "opensearch": ElasticsearchWriter,
    "s3": ObjectStoreWriter,
}
"""Registered output formats. Stream and object-store writers register here in
later phases without anything above having to change."""

#: Options a format cannot start without. Everything else has a default.
REQUIRED_OPTIONS: dict[str, tuple[str, ...]] = {
    "elasticsearch": ("url",),
    "opensearch": ("url",),
    "s3": ("bucket",),
}

SINGLE_FILE_FORMATS = frozenset({"sqlite"})
"""Formats where every entity writes into one destination.

A relational output split across three files is not a relational output: the
point of a SQLite database is that its foreign keys resolve, which they cannot
do if each table lives in a separate file.
"""


def describe_formats() -> list[dict[str, Any]]:
    """Every registered format, as something an interface can offer.

    The Studio used to carry a hand-written list of four, which is how it came
    to omit the two database formats entirely. Derived from the registry
    instead, so a format that exists is a format that can be chosen - including
    one a plugin registers.

    Aliases are folded into the format they name rather than listed beside it:
    `ndjson` is `jsonl` under another name, and offering both as separate
    choices only invites the question of how they differ.
    """
    by_writer: dict[type[OutputWriter], dict[str, Any]] = {}
    for name, writer_class in OUTPUT_FORMATS.items():
        entry = by_writer.get(writer_class)
        if entry is None:
            by_writer[writer_class] = {
                "name": name,
                "extension": writer_class.extension,
                "aliases": [],
                "single_file": name in SINGLE_FILE_FORMATS,
                "partitionable": name not in SINGLE_FILE_FORMATS,
                "summary": _summary(writer_class),
                # Options without which this format cannot run. An interface
                # that offers a destination it cannot configure is offering an
                # error message.
                "requires": list(REQUIRED_OPTIONS.get(name, ())),
            }
        else:
            entry["aliases"].append(name)
    return sorted(by_writer.values(), key=lambda entry: str(entry["name"]))


def _summary(writer_class: type[OutputWriter]) -> str:
    """The first line of a writer's docstring, which is what it is for.

    The design-document reference every docstring carries is for a reader of
    the source, not for a menu, so it is trimmed here.
    """
    doc = (writer_class.__doc__ or "").strip().splitlines()
    first = doc[0].strip() if doc else ""
    return re.sub(r"\s*\((?:design document )?sections? [\d, and]+\)", "", first)


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
        options.pop("zoned", None)
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
        options.pop("zoned", None)
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


def read_written_values(
    path: str | Path, fmt: str, columns: Sequence[str], *, table: str | None = None
) -> dict[str, list[Any]] | None:
    """Read back the values already written for these columns.

    For resuming a run that enforces `unique: true`: the tracker that remembers
    what has been seen lives in the engine, and a resumed run builds a new one -
    so without this, the second half of a run cannot see the first half's values
    and duplicates pass. Returns ``None`` when the format cannot be read back
    cheaply, which is the caller's cue to say so rather than to pretend.
    """
    import csv as _csv
    import json as _json

    target = Path(path)
    wanted = list(columns)
    if not wanted or not target.exists():
        return {name: [] for name in wanted}

    kind = fmt.lower()
    found: dict[str, list[Any]] = {name: [] for name in wanted}
    try:
        if kind in ("jsonl", "ndjson"):
            with target.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    row = _json.loads(line)
                    for name in wanted:
                        if name in row:
                            found[name].append(row[name])
            return found

        if kind == "json":
            rows = _json.loads(target.read_text(encoding="utf-8"))
            for row in rows if isinstance(rows, list) else []:
                for name in wanted:
                    if isinstance(row, dict) and name in row:
                        found[name].append(row[name])
            return found

        if kind == "csv":
            with target.open(newline="", encoding="utf-8") as handle:
                for row in _csv.DictReader(handle):
                    for name in wanted:
                        if name in row:
                            found[name].append(row[name])
            return found

        if kind == "sqlite":
            import sqlite3

            connection = sqlite3.connect(str(target))
            try:
                quoted = ", ".join(f'"{name}"' for name in wanted)
                cursor = connection.execute(f'SELECT {quoted} FROM "{table or target.stem}"')
                for row in cursor:
                    for name, value in zip(wanted, row, strict=True):
                        found[name].append(value)
            finally:
                connection.close()
            return found
    except (OSError, ValueError, LookupError):
        return None
    except Exception:  # pragma: no cover - a database that will not open
        return None

    # Parquet and the partitioned tree: readable, but not without pulling in
    # the whole dataset, which is the one thing this project does not do.
    return None


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
